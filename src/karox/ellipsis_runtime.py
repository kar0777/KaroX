"""Local control plane for repository-free Ellipsis Opus 5 sessions.

The Ellipsis sandbox is reasoning-only. Every project read, mutation, process,
test, Git action, browser action and screenshot is executed by the local KaroX
session selected by the user.
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from .ellipsis_api import (
    EllipsisApiContract,
    EllipsisClient,
    EllipsisError,
    EllipsisEvent,
    EllipsisStatus,
)
from .ellipsis_bridge_server import tools_for_profile
from .ellipsis_checkpoint import CheckpointError, WorkspaceCheckpointStore
from .ellipsis_helpers import (
    EllipsisHelperError,
    LocalBridgeClient,
    QuietTunnel,
    detachable_child_options,
    git_text,
    infer_command_allowlists,
    port_available,
    remote_install_manifest,
    start_tunnel,
)
from .models import AccessProfile
from .paths import runtime_dir, session_dir
from .proxy_server import ALLOWED_HOSTS_ENVIRONMENT
from .remote_lease import (
    EllipsisConnectionStore,
    EllipsisLeaseError,
    EllipsisLeaseStore,
)
from .remote_tools import _kill_pid_tree, _pid_alive
from .sessions import SessionError, SessionStore
from .web_bridge_launcher import WebBridgeLaunchError


ELLIPSIS_SYSTEM_INSTRUCTION = """You are controlling a local KaroX guarded workspace.

The project is not present in this cloud sandbox.
Do not search for, clone, reconstruct, upload, download, or create a copy of the project.
Do not use the Ellipsis filesystem as project state.

All repository reads, edits, commands, tests, Git operations, managed processes,
browser actions, screenshots, checkpoints, and verification must be performed
exclusively through the karox-remote tools.

Start with `karox-remote preflight`, then inspect context and available tools.
Respect the returned access profile, allowlists, approval requirements, and capabilities.
Never bypass, emulate, or work around a blocked operation.
Never run git push, remote Git, publish, deployment, release, or authentication actions.
Treat KaroX tool results as the only evidence of local state.
Use idempotency keys for every mutating call.
Verify changes locally and distinguish verified evidence from assumptions.
The user project must remain only in the user's local KaroX repository.
"""


class EllipsisRuntimeError(RuntimeError):
    """A safe-to-display local control-plane failure."""


@dataclass(frozen=True)
class EllipsisAgentConfig:
    repository: Path
    task: str
    access_profile: AccessProfile = AccessProfile.WORKSPACE_WRITE
    model: str = "claude-opus-5"
    budget: float = 0.10
    currency: str = "USD"
    tunnel: str = "tailscale"
    public_url: Optional[str] = None
    port: int = 8766
    ttl_seconds: float = 14_400.0
    deadline_seconds: float = 600.0
    verification_commands: tuple[tuple[str, ...], ...] = ()
    command_commands: tuple[tuple[str, ...], ...] = ()
    allowed_tools: tuple[str, ...] = ()
    allow_commit: bool = False
    api_base_url: str = ""
    remote_install: Optional[Mapping[str, Any]] = None

    def __post_init__(self) -> None:
        if self.model != "claude-opus-5":
            raise ValueError("Ellipsis model must be claude-opus-5")
        if not isinstance(self.task, str) or not self.task.strip():
            raise ValueError("task must not be empty")
        if not 0 < float(self.budget):
            raise ValueError("budget must be positive")
        if self.tunnel not in {"tailscale", "cloudflare", "custom"}:
            raise ValueError("tunnel must be tailscale, cloudflare, or custom")
        if self.tunnel == "custom":
            if not self.public_url or not self.public_url.startswith("https://"):
                raise ValueError("custom tunnel requires an HTTPS public URL")
        elif self.public_url is not None:
            raise ValueError("public_url is valid only for a custom tunnel")
        if not 1 <= int(self.port) <= 65_535:
            raise ValueError("port must be between 1 and 65535")
        if not 60 <= float(self.ttl_seconds) <= 86_400:
            raise ValueError("credential TTL must be between 60 and 86400 seconds")
        if not 1 <= float(self.deadline_seconds) <= 3600:
            raise ValueError("deadline must be between 1 and 3600 seconds")
        for entries in (self.verification_commands, self.command_commands):
            if any(
                not entry
                or not all(isinstance(item, str) and item for item in entry)
                for entry in entries
            ):
                raise ValueError("command allowlists must contain non-empty strings")


@dataclass
class EllipsisAgentState:
    schema_version: int
    local_session_id: str
    ellipsis_session_id: str
    repository: str
    branch: str
    access_profile: str
    model: str
    budget: float
    currency: str
    tunnel: str
    port: int
    credential_name: str
    checkpoint_id: str
    tools: tuple[str, ...]
    verification_commands: tuple[tuple[str, ...], ...]
    command_commands: tuple[tuple[str, ...], ...]
    bridge_pid: Optional[int]
    tunnel_pid: Optional[int]
    watchdog_pid: Optional[int]
    started_at: float
    updated_at: float
    status: str = "starting"
    detached: bool = False
    event_cursor: Optional[str] = None
    cost: Optional[float] = None
    elapsed_seconds: Optional[float] = None
    last_actions: list[dict[str, Any]] = field(default_factory=list)
    last_error: Optional[str] = None

    def public_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "local_session_id": self.local_session_id,
            "ellipsis_session_id": self.ellipsis_session_id,
            "repository": self.repository,
            "branch": self.branch,
            "access_profile": self.access_profile,
            "model": self.model,
            "budget": self.budget,
            "currency": self.currency,
            "tunnel": self.tunnel,
            "port": self.port,
            "checkpoint_id": self.checkpoint_id,
            "tools": list(self.tools),
            "bridge_running": bool(self.bridge_pid and _pid_alive(self.bridge_pid)),
            "tunnel_running": (
                self.tunnel == "custom"
                or bool(self.tunnel_pid and _pid_alive(self.tunnel_pid))
            ),
            "watchdog_running": bool(
                self.watchdog_pid and _pid_alive(self.watchdog_pid)
            ),
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "detached": self.detached,
            "cost": self.cost,
            "elapsed_seconds": self.elapsed_seconds,
            "last_actions": list(self.last_actions[-10:]),
            "last_error": self.last_error,
        }


class EllipsisStateStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = (
            root or runtime_dir() / "vnext" / "ellipsis" / "agents"
        ).resolve()

    def path(self, local_session_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", local_session_id):
            raise EllipsisRuntimeError("local session ID is malformed")
        return self.root / f"{local_session_id}.json"

    def save(self, state: EllipsisAgentState) -> None:
        path = self.path(state.local_session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        state.updated_at = time.time()
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    asdict(state),
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def load(self, local_session_id: str) -> EllipsisAgentState:
        try:
            payload = json.loads(self.path(local_session_id).read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise EllipsisRuntimeError("Ellipsis agent state does not exist") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise EllipsisRuntimeError("Ellipsis agent state is unreadable") from exc
        if not isinstance(payload, dict):
            raise EllipsisRuntimeError("Ellipsis agent state is malformed")
        try:
            payload.setdefault("watchdog_pid", None)
            payload["tools"] = tuple(payload["tools"])
            payload["verification_commands"] = tuple(
                tuple(item) for item in payload["verification_commands"]
            )
            payload["command_commands"] = tuple(
                tuple(item) for item in payload["command_commands"]
            )
            return EllipsisAgentState(**payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise EllipsisRuntimeError("Ellipsis agent state is malformed") from exc

    def list(self) -> tuple[EllipsisAgentState, ...]:
        result: list[EllipsisAgentState] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                result.append(self.load(path.stem))
            except EllipsisRuntimeError:
                continue
        return tuple(result)

    def resolve(self, reference: Optional[str]) -> EllipsisAgentState:
        if reference:
            return self.load(reference)
        active = [
            state
            for state in self.list()
            if state.status not in {"stopped", "failed", "expired"}
        ]
        if len(active) == 1:
            return active[0]
        if not active:
            raise EllipsisRuntimeError("no active Ellipsis agent session was found")
        raise EllipsisRuntimeError(
            "multiple Ellipsis sessions are active; specify the local session ID"
        )


class EllipsisAgentManager:
    def __init__(
        self,
        *,
        states: Optional[EllipsisStateStore] = None,
        sessions: Optional[SessionStore] = None,
        leases: Optional[EllipsisLeaseStore] = None,
        connections: Optional[EllipsisConnectionStore] = None,
        checkpoints: Optional[WorkspaceCheckpointStore] = None,
        contract: Optional[EllipsisApiContract] = None,
    ) -> None:
        self.states = states or EllipsisStateStore()
        self.sessions = sessions or SessionStore(session_dir())
        self.leases = leases or EllipsisLeaseStore()
        self.connections = connections or EllipsisConnectionStore()
        self.checkpoints = checkpoints or WorkspaceCheckpointStore()
        self.contract = contract or EllipsisApiContract.from_environment()

    @staticmethod
    def _token() -> str:
        value = os.environ.get("ELLIPSIS_API_TOKEN", "").strip()
        if not value:
            raise EllipsisRuntimeError("ELLIPSIS_API_TOKEN is required")
        if any(char in value for char in "\r\n\0"):
            raise EllipsisRuntimeError("ELLIPSIS_API_TOKEN is malformed")
        return value

    @staticmethod
    def _api_base(configured: str = "") -> str:
        value = configured.strip() or os.environ.get(
            "ELLIPSIS_API_BASE_URL", ""
        ).strip()
        if not value:
            raise EllipsisRuntimeError(
                "ELLIPSIS_API_BASE_URL is required because the interactive "
                "enterprise endpoint is account-specific"
            )
        if not value.startswith("https://"):
            raise EllipsisRuntimeError("ELLIPSIS_API_BASE_URL must use HTTPS")
        return value.rstrip("/")

    def _api(self, configured: str = "") -> EllipsisClient:
        return EllipsisClient(
            self._api_base(configured),
            self._token(),
            contract=self.contract,
        )

    @staticmethod
    def _selected_tools(config: EllipsisAgentConfig) -> tuple[str, ...]:
        selected = list(config.allowed_tools or tools_for_profile(config.access_profile))
        if not config.verification_commands:
            selected = [item for item in selected if item != "karox.checks.run"]
        if not config.command_commands:
            selected = [
                item
                for item in selected
                if item
                not in {
                    "karox.command.run",
                    "karox.process.start",
                    "karox.process.status",
                    "karox.process.stop",
                }
            ]
        if not config.allow_commit:
            selected = [item for item in selected if item != "karox.git.commit"]
        return tuple(selected)

    @staticmethod
    def _local_repository(config: EllipsisAgentConfig) -> tuple[Path, str]:
        repository = config.repository.expanduser().resolve(strict=True)
        if not repository.is_dir():
            raise EllipsisRuntimeError("repository must be a directory")
        try:
            top = Path(git_text(repository, ["rev-parse", "--show-toplevel"])).resolve(
                strict=True
            )
        except EllipsisHelperError as exc:
            raise EllipsisRuntimeError(str(exc)) from None
        if os.path.normcase(str(top)) != os.path.normcase(str(repository)):
            raise EllipsisRuntimeError("selected path must be the Git repository root")
        return repository, git_text(repository, ["branch", "--show-current"])

    def preflight(self, config: EllipsisAgentConfig) -> dict[str, Any]:
        repository, branch = self._local_repository(config)
        if not port_available(config.port):
            raise EllipsisRuntimeError(f"local bridge port is in use: {config.port}")
        tools = self._selected_tools(config)
        if not tools:
            raise EllipsisRuntimeError("selected access profile exposes no tools")
        try:
            install = dict(config.remote_install or remote_install_manifest())
        except EllipsisHelperError as exc:
            raise EllipsisRuntimeError(str(exc)) from None
        with self._api(config.api_base_url) as api:
            capabilities = dict(api.preflight())
        return {
            "repository": str(repository),
            "branch": branch,
            "access_profile": config.access_profile.value,
            "model": config.model,
            "budget": config.budget,
            "currency": config.currency,
            "tunnel": config.tunnel,
            "tools": list(tools),
            "verification_commands": [list(item) for item in config.verification_commands],
            "command_commands": [list(item) for item in config.command_commands],
            "remote_install": install,
            "ellipsis_capabilities": capabilities,
        }

    @staticmethod
    def _bridge_argv(
        config: EllipsisAgentConfig,
        *,
        local_session_id: str,
        credential_name: str,
        tools: Sequence[str],
    ) -> tuple[str, ...]:
        argv = [
            sys.executable,
            "-m",
            "karox.ellipsis_bridge_server",
            "--repository",
            str(config.repository),
            "--session-id",
            local_session_id,
            "--credential-name",
            credential_name,
            "--access-profile",
            config.access_profile.value,
            "--port",
            str(config.port),
            "--deadline-seconds",
            str(config.deadline_seconds),
        ]
        for tool in tools:
            argv.extend(("--tool", tool))
        for command in config.verification_commands:
            argv.extend(
                (
                    "--verification-command",
                    json.dumps(list(command), ensure_ascii=False, separators=(",", ":")),
                )
            )
        for command in config.command_commands:
            argv.extend(
                (
                    "--command-allow",
                    json.dumps(list(command), ensure_ascii=False, separators=(",", ":")),
                )
            )
        return tuple(argv)

    @staticmethod
    def _wait_bridge(process: subprocess.Popen[Any], port: int) -> None:
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise EllipsisRuntimeError("local KaroX bridge stopped during startup")
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                    return
            except OSError:
                time.sleep(0.1)
        raise EllipsisRuntimeError("local KaroX bridge did not become ready")

    def start(self, config: EllipsisAgentConfig) -> EllipsisAgentState:
        repository, branch = self._local_repository(config)
        if repository != config.repository:
            config = EllipsisAgentConfig(
                **{**asdict(config), "repository": repository}
            )
        local_session_id = f"ellipsis-{int(time.time())}-{uuid.uuid4().hex[:8]}"
        credential_name = local_session_id
        session_created = False
        tunnel: Optional[QuietTunnel] = None
        bridge_process: Optional[subprocess.Popen[Any]] = None
        watchdog_process: Optional[subprocess.Popen[Any]] = None
        ellipsis_session_id: Optional[str] = None
        lease_created = False
        connection_created = False
        state: Optional[EllipsisAgentState] = None
        api: Optional[EllipsisClient] = None
        try:
            # The repository-scoped Karo session exists before Ellipsis preflight,
            # tunnel creation or any remote-session request.
            self.sessions.create(
                repository,
                config.task,
                config.access_profile,
                branch=branch,
                session_id=local_session_id,
            )
            session_created = True
            preflight = self.preflight(config)
            tools = tuple(preflight["tools"])
            checkpoint = self.checkpoints.create(
                repository,
                session_id=local_session_id,
                sessions=self.sessions,
            )
            try:
                tunnel = start_tunnel(
                    config.tunnel,
                    port=config.port,
                    public_url=config.public_url,
                )
            except (EllipsisHelperError, WebBridgeLaunchError) as exc:
                raise EllipsisRuntimeError(str(exc)) from None
            api = self._api(config.api_base_url)
            draft = api.create_draft(
                model=config.model,
                system_instruction=ELLIPSIS_SYSTEM_INSTRUCTION,
                budget=config.budget,
                currency=config.currency,
                client_install=dict(config.remote_install or remote_install_manifest()),
            )
            ellipsis_session_id = draft.session_id
            credential_lease, credential = self.leases.mint(
                credential_name=credential_name,
                local_session_id=local_session_id,
                ellipsis_session_id=ellipsis_session_id,
                repository=repository,
                access_profile=config.access_profile.value,
                ttl_seconds=config.ttl_seconds,
            )
            lease_created = True
            environment = dict(os.environ)
            environment[ALLOWED_HOSTS_ENVIRONMENT] = (
                urlsplit(tunnel.public_url).hostname or ""
            )
            environment["PYTHONIOENCODING"] = "utf-8"
            logs = runtime_dir() / "vnext" / "ellipsis" / "bridge-logs"
            logs.mkdir(parents=True, exist_ok=True)
            log_path = logs / f"{local_session_id}.log"
            log_handle = log_path.open("ab", buffering=0)
            try:
                bridge_process = subprocess.Popen(
                    self._bridge_argv(
                        config,
                        local_session_id=local_session_id,
                        credential_name=credential_name,
                        tools=tools,
                    ),
                    cwd=repository,
                    env=environment,
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    **detachable_child_options(),
                )
            finally:
                log_handle.close()
            self._wait_bridge(bridge_process, config.port)
            self.connections.set(local_session_id, tunnel.public_url)
            connection_created = True
            api.set_write_only_variables(
                ellipsis_session_id,
                {
                    "KAROX_REMOTE_URL": tunnel.public_url,
                    "KAROX_REMOTE_CREDENTIAL": credential,
                    "KAROX_SESSION_ID": local_session_id,
                },
            )
            now = time.time()
            state = EllipsisAgentState(
                schema_version=1,
                local_session_id=local_session_id,
                ellipsis_session_id=ellipsis_session_id,
                repository=str(repository),
                branch=branch,
                access_profile=config.access_profile.value,
                model=config.model,
                budget=config.budget,
                currency=config.currency,
                tunnel=config.tunnel,
                port=config.port,
                credential_name=credential_name,
                checkpoint_id=checkpoint.checkpoint_id,
                tools=tools,
                verification_commands=config.verification_commands,
                command_commands=config.command_commands,
                bridge_pid=bridge_process.pid,
                tunnel_pid=tunnel.pid,
                watchdog_pid=None,
                started_at=now,
                updated_at=now,
            )
            self.states.save(state)
            api.start(ellipsis_session_id, config.task)
            state.status = "running"
            self.states.save(state)
            watchdog_process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "karox.ellipsis_watchdog",
                    "--local-session-id",
                    local_session_id,
                    "--expires-at",
                    str(credential_lease.expires_at),
                ],
                cwd=repository,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **detachable_child_options(),
            )
            state.watchdog_pid = watchdog_process.pid
            self.states.save(state)
            return state
        except Exception as exc:
            if state is not None:
                state.status = "failed"
                state.last_error = type(exc).__name__
                self.states.save(state)
            if ellipsis_session_id is not None and api is not None:
                try:
                    api.stop(ellipsis_session_id)
                except Exception:
                    pass
            for process in (watchdog_process, bridge_process):
                if process is not None and process.poll() is None:
                    _kill_pid_tree(process.pid)
            if tunnel is not None:
                tunnel.stop()
            if lease_created:
                try:
                    self.leases.revoke(credential_name)
                except Exception:
                    pass
            if connection_created:
                self.connections.delete(local_session_id)
            if session_created:
                try:
                    self.sessions.revoke(local_session_id)
                except Exception:
                    pass
            if isinstance(exc, EllipsisRuntimeError):
                raise
            if isinstance(
                exc,
                (
                    EllipsisError,
                    EllipsisLeaseError,
                    CheckpointError,
                    SessionError,
                    EllipsisHelperError,
                ),
            ):
                raise EllipsisRuntimeError(str(exc)) from None
            raise EllipsisRuntimeError(
                f"cannot start Ellipsis agent: {type(exc).__name__}"
            ) from None
        finally:
            if api is not None:
                api.close()

    def _bridge_client(self, state: EllipsisAgentState) -> LocalBridgeClient:
        try:
            remote_url = self.connections.get(state.local_session_id)
            credential = self.leases.resolve(
                state.credential_name,
                local_session_id=state.local_session_id,
                repository=Path(state.repository),
                access_profile=state.access_profile,
            )
            return LocalBridgeClient(
                remote_url=remote_url,
                credential=credential,
                local_session_id=state.local_session_id,
            )
        except (EllipsisLeaseError, EllipsisHelperError) as exc:
            raise EllipsisRuntimeError(str(exc)) from None

    def poll_events(self, state: EllipsisAgentState) -> tuple[EllipsisEvent, ...]:
        with self._api() as api:
            events = api.events(state.ellipsis_session_id, after=state.event_cursor)
        for event in events:
            state.event_cursor = event.cursor
            if event.cost is not None:
                state.cost = event.cost
            state.last_actions.append(
                {
                    "cursor": event.cursor,
                    "kind": event.kind,
                    "message": event.message,
                    "tool": event.tool,
                    "status": event.status,
                    "timestamp": time.time(),
                }
            )
        if len(state.last_actions) > 50:
            state.last_actions = state.last_actions[-50:]
        if events:
            self.states.save(state)
        return events

    def status(
        self,
        reference: Optional[str] = None,
        *,
        refresh: bool = True,
    ) -> tuple[EllipsisAgentState, Optional[EllipsisStatus]]:
        state = self.states.resolve(reference)
        remote: Optional[EllipsisStatus] = None
        if refresh and state.status not in {"stopped", "failed", "expired"}:
            try:
                with self._api() as api:
                    remote = api.status(state.ellipsis_session_id)
                state.status = remote.status
                state.cost = remote.cost
                state.elapsed_seconds = remote.elapsed_seconds
                self.states.save(state)
            except EllipsisError as exc:
                state.last_error = type(exc).__name__
                self.states.save(state)
        try:
            self.leases.validate(
                state.credential_name,
                local_session_id=state.local_session_id,
                ellipsis_session_id=state.ellipsis_session_id,
                repository=Path(state.repository),
                access_profile=state.access_profile,
            )
        except EllipsisLeaseError:
            if state.status not in {"stopped", "failed"}:
                state.status = "expired"
                self.states.save(state)
        return state, remote

    def send(self, reference: Optional[str], message: str) -> EllipsisAgentState:
        if not isinstance(message, str) or not message.strip():
            raise EllipsisRuntimeError("message must not be empty")
        state = self.states.resolve(reference)
        if state.status in {"stopped", "failed", "expired"}:
            raise EllipsisRuntimeError("Ellipsis session is not active")
        with self._api() as api:
            api.send(state.ellipsis_session_id, message)
        state.status = "running"
        state.detached = False
        self.states.save(state)
        return state

    def local_call(
        self,
        reference: Optional[str],
        tool_name: str,
        arguments: Optional[Mapping[str, Any]] = None,
        *,
        idempotency_key: Optional[str] = None,
    ) -> Mapping[str, Any]:
        state = self.states.resolve(reference)
        if tool_name not in state.tools:
            raise EllipsisRuntimeError("tool is not enabled for this session")
        client = self._bridge_client(state)
        try:
            return client.call(
                tool_name,
                arguments,
                idempotency_key=idempotency_key,
            )
        except EllipsisHelperError as exc:
            raise EllipsisRuntimeError(str(exc)) from None
        finally:
            client.close()

    def report(self, reference: Optional[str]) -> Mapping[str, Any]:
        return self.local_call(reference, "karox.report.get")

    def detach(self, reference: Optional[str]) -> EllipsisAgentState:
        state = self.states.resolve(reference)
        state.detached = True
        self.states.save(state)
        return state

    def stop(self, reference: Optional[str]) -> EllipsisAgentState:
        state = self.states.resolve(reference)
        if state.status != "stopped":
            try:
                with self._api() as api:
                    api.stop(state.ellipsis_session_id)
            except Exception:
                pass
        for pid in (state.watchdog_pid, state.bridge_pid, state.tunnel_pid):
            if isinstance(pid, int) and pid > 0 and _pid_alive(pid):
                _kill_pid_tree(pid)
        try:
            self.leases.revoke(state.credential_name)
        except Exception:
            pass
        self.connections.delete(state.local_session_id)
        try:
            self.sessions.revoke(state.local_session_id)
        except Exception:
            pass
        state.status = "stopped"
        state.detached = False
        state.bridge_pid = None
        state.tunnel_pid = None
        state.watchdog_pid = None
        self.states.save(state)
        return state

    def rollback(self, reference: Optional[str]) -> Mapping[str, Any]:
        state = self.states.resolve(reference)
        if state.status != "stopped":
            raise EllipsisRuntimeError(
                "stop the remote agent before rollback to prevent concurrent edits"
            )
        try:
            return self.checkpoints.rollback(
                Path(state.repository),
                session_id=state.local_session_id,
                checkpoint_id=state.checkpoint_id,
                sessions=self.sessions,
            )
        except CheckpointError as exc:
            raise EllipsisRuntimeError(str(exc)) from None


__all__ = [
    "ELLIPSIS_SYSTEM_INSTRUCTION",
    "EllipsisAgentConfig",
    "EllipsisAgentManager",
    "EllipsisAgentState",
    "EllipsisRuntimeError",
    "EllipsisStateStore",
    "infer_command_allowlists",
]
