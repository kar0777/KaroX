"""ASGI runner for the local ``karox bridge serve`` child.

The lifecycle owner (:mod:`karox.bridge_launcher`) spawns this module through
``sys.executable -m karox.cli bridge serve ...``. This module does only four
things:

1. validate that the requested listener is loopback (or TLS-gated and
   explicitly allowed), and that the selected Core tools are real;
2. load the durable session and build one ``CoreToolBridge``;
3. wrap it in the OAuth ASGI facade (or the bearer MCP proxy for bearer
   profiles);
4. run uvicorn with graceful SIGTERM / CTRL_BREAK shutdown.

No credential is read from the command line: ``--credential`` names the OS
keyring account (the session id), and the value is resolved inside this process
and never printed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Sequence
from urllib.parse import urlsplit

from .bridge_profiles import (
    DEFAULT_BRIDGE_DEADLINE_SECONDS,
    WEB_BRIDGE_PROFILES,
    BridgeCredentialMissing,
    BridgeCredentialStore,
    BridgeProfileError,
    BridgeProfileStore,
    BridgeRegistry,
)
from .credentials import CredentialError
from .paths import oauth_state_dir, runtime_dir, session_dir
from .project_registry import ProjectRegistry, ProjectRegistryError
from .sessions import SessionError, SessionStore

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .hosted_core import HostedToolRuntime


DEFAULT_BRIDGE_HOST = "127.0.0.1"
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class BridgeServeError(RuntimeError):
    """The bridge child cannot serve the requested endpoint safely."""


@dataclass(frozen=True)
class BridgeServeConfig:
    """One validated ``bridge serve`` invocation."""

    profile: str
    repository: Path
    session_id: str
    credential: str
    host: str = DEFAULT_BRIDGE_HOST
    port: int = 8765
    protocol: str = "mcp"
    path: str = "/mcp"
    public_url: Optional[str] = None
    tools: tuple[str, ...] = ()
    projects: tuple[dict[str, str], ...] = ()
    default_project_id: Optional[str] = None
    verification_commands: tuple[tuple[str, ...], ...] = ()
    deadline_seconds: float = DEFAULT_BRIDGE_DEADLINE_SECONDS
    saved_profile_name: Optional[str] = None
    allowed_redirect_hosts: Optional[frozenset[str]] = None
    allow_network_bind: bool = False
    tls_certfile: Optional[str] = None
    tls_keyfile: Optional[str] = None
    state_dir: Optional[Path] = None
    bypass: bool = False

    def __post_init__(self) -> None:
        if self.profile not in WEB_BRIDGE_PROFILES:
            raise BridgeServeError(
                "bridge serve profile must be chatgpt-web, claude-web, notion, "
                "hyperagent-web, or adapt"
            )
        if self.protocol != "mcp":
            raise BridgeServeError(
                "KaroX 6 bridge serve supports --protocol mcp only"
            )
        if not _SAFE_ID.fullmatch(self.session_id):
            raise BridgeServeError("bridge session id is not a safe identifier")
        if not _SAFE_ID.fullmatch(self.credential):
            raise BridgeServeError("bridge credential name is not a safe identifier")
        if not 1 <= int(self.port) <= 65_535:
            raise BridgeServeError("bridge port must be between 1 and 65535")
        if (
            not isinstance(self.path, str)
            or not self.path.startswith("/")
            or "?" in self.path
            or "#" in self.path
            or "//" in self.path
        ):
            raise BridgeServeError("bridge MCP path must be one absolute URL path")
        if self.state_dir is not None and not isinstance(self.state_dir, Path):
            raise BridgeServeError("bridge state_dir must be a Path")
        loopback = self.host in _LOOPBACK_HOSTS
        if not loopback and not self.allow_network_bind:
            raise BridgeServeError(
                "non-loopback bridge bind requires --allow-network-bind"
            )
        if (self.tls_certfile is None) != (self.tls_keyfile is None):
            raise BridgeServeError(
                "bridge TLS requires both --tls-certfile and --tls-keyfile"
            )
        # Off loopback the bridge is reachable by anything that can route to
        # this host, and the bearer token plus every file it returns would
        # travel in clear text. --allow-network-bind gates that bind, so it may
        # not be the whole gate.
        if not loopback and self.tls_certfile is None:
            raise BridgeServeError(
                "non-loopback bridge bind requires TLS: pass --tls-certfile and "
                "--tls-keyfile, or keep --host on loopback behind a tunnel"
            )
        for option, value in (
            ("--tls-certfile", self.tls_certfile),
            ("--tls-keyfile", self.tls_keyfile),
        ):
            if value is not None and not Path(value).is_file():
                raise BridgeServeError(f"{option} must point to an existing PEM file")
        if not self.tools or len(set(self.tools)) != len(self.tools):
            raise BridgeServeError("bridge serve tools must be non-empty and unique")
        if not all(_SAFE_ID.fullmatch(name) for name in self.tools):
            raise BridgeServeError("bridge serve tools must be safe tool names")
        if any(
            not command
            or len(command) > 100
            or not all(isinstance(item, str) and item for item in command)
            for command in self.verification_commands
        ):
            raise BridgeServeError(
                "bridge verification commands must contain 1-100 non-empty strings"
            )
        if not 0.1 <= float(self.deadline_seconds) <= 3600.0:
            raise BridgeServeError(
                "bridge deadline must be between 0.1 and 3600 seconds"
            )
        if self.public_url:
            parsed = urlsplit(self.public_url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise BridgeServeError(
                    "--public-url must be an HTTPS origin without a path, query, "
                    "fragment, or user information"
                )
        if self.allowed_redirect_hosts is not None and not all(
            isinstance(host, str) and host.strip() for host in self.allowed_redirect_hosts
        ):
            raise BridgeServeError("allowed redirect hosts must be non-empty strings")


def _core_tools_for_launch(tools: Sequence[str]) -> tuple[str, ...]:
    """Validate the requested tools against the authoritative Core catalogue."""
    try:
        from .hosted_core import CORE_TOOL_NAMES
    except Exception as exc:  # pragma: no cover - packaging error
        raise BridgeServeError(
            f"cannot load the Core bridge runtime: {type(exc).__name__}"
        ) from exc
    unknown = sorted(set(tools) - set(CORE_TOOL_NAMES))
    if unknown:
        raise BridgeServeError(
            "bridge serve contains tools the Core bridge cannot serve: "
            + ", ".join(unknown)
        )
    if not tools:
        raise BridgeServeError("bridge serve requires at least one Core tool")
    return tuple(tools)


def _build_runtime(
    config: BridgeServeConfig,
    sessions: SessionStore,
    repository: Path,
) -> "HostedToolRuntime":
    from .hosted_core import CoreToolBridge
    from .models import Origin, OriginKind

    tools = _core_tools_for_launch(config.tools)
    try:
        project_registry = ProjectRegistry.from_profile(
            repository=str(repository),
            projects=config.projects,
            default_project_id=config.default_project_id,
        )
    except ProjectRegistryError as exc:
        raise BridgeServeError(f"invalid bridge project registry: {exc}") from exc
    if project_registry.default is None:
        raise BridgeServeError("bridge serve requires a default project")
    if project_registry.entry_for_path(repository) is None:
        raise BridgeServeError("bridge session anchor is not an approved project")
    try:
        return CoreToolBridge(
            repository,
            sessions,
            config.session_id,
            tools,
            hosted_origin=Origin(
                OriginKind.HOSTED_CLIENT,
                f"{config.profile}-core-{config.session_id}",
            ),
            audit_path=runtime_dir() / "vnext" / "audit.jsonl",
            verification_commands=(
                config.verification_commands or None
            ),
            project_registry=project_registry,
            bypass_mode=config.bypass,
            advertise_unavailable=(config.profile == "hyperagent-web"),
        )
    except Exception as exc:
        raise BridgeServeError(f"cannot build the Core bridge: {exc}") from exc


def _credential_resolver(credential: str) -> tuple[Any, str]:
    """Resolve the keyring value once and return a re-reading callable.

    Re-reading on each authorization lets an explicit rotation take effect
    without restarting the child, while the returned fingerprint is the only
    value that ever leaves the process.
    """
    store = BridgeCredentialStore()
    reference = f"os-keyring:bridge/{credential}"
    try:
        secret = store.resolve(reference)
    except BridgeCredentialMissing as exc:
        raise BridgeServeError("bridge credential is missing from the OS keyring") from exc
    except CredentialError as exc:
        raise BridgeServeError(
            f"bridge credential is unreadable: {type(exc).__name__}"
        ) from exc

    def resolve() -> str:
        return store.resolve(reference)

    return resolve, BridgeCredentialStore.fingerprint(secret)


def build_asgi_app(
    config: BridgeServeConfig,
    *,
    runtime: Optional["HostedToolRuntime"] = None,
) -> Any:
    """Build the authenticated ASGI app this child serves."""
    try:
        repository = config.repository.expanduser().resolve(strict=True)
    except OSError as exc:
        raise BridgeServeError(
            f"bridge repository is unavailable: {type(exc).__name__}"
        ) from exc
    if not repository.is_dir():
        raise BridgeServeError("bridge repository must be a directory")
    sessions = SessionStore(session_dir())
    try:
        record = sessions.load(config.session_id)
        sessions.validate_repository(record, repository)
    except SessionError as exc:
        raise BridgeServeError(f"bridge session is invalid: {exc}") from exc
    if record.revoked:
        raise BridgeServeError("bridge session access has been revoked")

    bridge_runtime = runtime or _build_runtime(config, sessions, repository)
    resolve_credential, _fingerprint = _credential_resolver(config.credential)
    profile_meta = BridgeRegistry().get(config.profile)
    if profile_meta.auth_scheme == "oauth":
        if not config.public_url:
            raise BridgeServeError(
                "OAuth bridge profiles require --public-url with the public HTTPS origin"
            )
        from .bridge_oauth import build_oauth_proxy_asgi_app

        return build_oauth_proxy_asgi_app(
            bridge_runtime,
            resolve_credential,
            public_url=config.public_url,
            path=config.path,
            deadline_seconds=config.deadline_seconds,
            # Without this the connector the user just added in the hosted
            # client stops working when this process exits.
            state_dir=config.state_dir or oauth_state_dir(),
            allowed_redirect_hosts=config.allowed_redirect_hosts,
        )
    if config.public_url:
        raise BridgeServeError(
            "--public-url is only valid for OAuth bridge profiles"
        )
    from .bridge_proxy import build_proxy_asgi_app

    return build_proxy_asgi_app(
        bridge_runtime,
        resolve_credential,
        path=config.path,
        deadline_seconds=config.deadline_seconds,
        # RFC 6750 requires a 401 from a bearer-protected resource to name the
        # scheme it wants.
        unauthorized_headers={"WWW-Authenticate": "Bearer"},
    )


def _install_shutdown_handlers(server: Any) -> dict[int, Any]:
    """Turn SIGTERM and Windows CTRL_BREAK into a graceful uvicorn shutdown."""
    previous: dict[int, Any] = {}

    def request_stop(_signum: int, _frame: Any) -> None:
        server.should_exit = True

    for name in ("SIGTERM", "SIGBREAK"):
        signum = getattr(signal, name, None)
        if signum is None:
            continue
        try:
            previous[int(signum)] = signal.signal(signum, request_stop)
        except (OSError, ValueError, RuntimeError):
            # Not the main thread, or the platform refuses the handler. uvicorn
            # still handles SIGINT itself.
            continue
    return previous


def _restore_shutdown_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError, RuntimeError):
            pass


def run_bridge_serve(config: BridgeServeConfig) -> int:
    """Run the ASGI app until Ctrl+C, SIGTERM, or CTRL_BREAK."""
    app = build_asgi_app(config)
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - dependency packaging error
        raise BridgeServeError(
            "bridge serve requires uvicorn, which is not installed in this runtime"
        ) from exc
    scheme = "https" if config.tls_certfile else "http"
    print(
        f"KaroX bridge: profile={config.profile} protocol=mcp "
        f"session={config.session_id}",
        flush=True,
    )
    print(
        f"Local endpoint: {scheme}://{config.host}:{config.port}{config.path}",
        flush=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host=config.host,
            port=config.port,
            log_level="warning",
            access_log=False,
            timeout_keep_alive=300,
            ssl_certfile=config.tls_certfile,
            ssl_keyfile=config.tls_keyfile,
        )
    )
    previous = _install_shutdown_handlers(server)
    try:
        server.run()
    finally:
        _restore_shutdown_handlers(previous)
    return 0


# --------------------------------------------------------------------------- #
# CLI integration                                                              #
# --------------------------------------------------------------------------- #


def _json_object(value: str, label: str) -> dict[str, Any]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise BridgeServeError(f"{label} must be a JSON object") from exc
    if not isinstance(decoded, dict):
        raise BridgeServeError(f"{label} must be a JSON object")
    return decoded


def _json_command(value: str) -> tuple[str, ...]:
    """Accept the v5 JSON argv form or an explicitly quoted shell command.

    The launcher emits JSON arrays. A hand-written ``--verification-command
    "python -m pytest -q"`` is a legitimate shell string, and is split with
    ``shlex`` (never executed through a shell).
    """
    decoded: Any
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        import shlex

        try:
            decoded = shlex.split(value, posix=os.name != "nt")
        except ValueError as exc:
            raise BridgeServeError(
                "--verification-command is not valid JSON or a shell command"
            ) from exc
    if (
        not isinstance(decoded, list)
        or not decoded
        or len(decoded) > 100
        or not all(isinstance(item, str) and item for item in decoded)
    ):
        raise BridgeServeError(
            "--verification-command must be a non-empty string list (max 100)"
        )
    return tuple(decoded)


def _saved_profile_bypass(saved_profile_name: Optional[str]) -> bool:
    if not saved_profile_name:
        return False
    try:
        return bool(BridgeProfileStore().get(saved_profile_name).bypass)
    except (BridgeProfileError, OSError):
        return False


def _redirect_hosts(args: argparse.Namespace) -> Optional[frozenset[str]]:
    """Accept the CLI's host list and the comma-separated legacy spelling."""
    raw = getattr(args, "allowed_redirect_host", None)
    values: list[str] = []
    if raw:
        if isinstance(raw, str):
            values.extend(host.strip() for host in raw.split(","))
        else:
            values.extend(str(host).strip() for host in raw)
    legacy = getattr(args, "allowed_redirect_hosts", None)
    if legacy:
        if isinstance(legacy, str):
            values.extend(host.strip() for host in legacy.split(","))
        else:
            values.extend(str(host).strip() for host in legacy)
    cleaned = frozenset(host.lower() for host in values if host)
    if not cleaned:
        return None
    return cleaned


def config_from_namespace(args: argparse.Namespace) -> BridgeServeConfig:
    """Convert parsed ``bridge serve`` arguments into a validated config.

    Both the CLI integrator's namespace contract (``tool``,
    ``verification_command``, ``allowed_redirect_host``, ``state_dir``) and the
    older launcher spelling are accepted.
    """
    tools = tuple(getattr(args, "tool", None) or ())
    verification = tuple(
        _json_command(value)
        for value in (getattr(args, "verification_command", None) or ())
    )
    projects = tuple(
        _json_object(value, "--project")
        for value in (getattr(args, "project", None) or ())
    )
    state_dir = getattr(args, "state_dir", None)
    if state_dir is not None and not isinstance(state_dir, Path):
        state_dir = Path(state_dir)
    # A caller-supplied access profile is not accepted here: the durable session
    # record is the authority, and CoreToolBridge reads it itself.
    return BridgeServeConfig(
        profile=args.profile,
        repository=Path(args.repository).expanduser(),
        session_id=args.session_id,
        credential=args.credential,
        host=getattr(args, "host", None) or DEFAULT_BRIDGE_HOST,
        port=int(args.port),
        protocol=getattr(args, "protocol", "mcp"),
        path=getattr(args, "path", "/mcp") or "/mcp",
        public_url=getattr(args, "public_url", None),
        tools=tools,
        projects=projects,
        default_project_id=getattr(args, "default_project_id", None),
        verification_commands=verification,
        deadline_seconds=float(
            getattr(args, "deadline_seconds", DEFAULT_BRIDGE_DEADLINE_SECONDS)
        ),
        saved_profile_name=getattr(args, "saved_profile_name", None),
        allowed_redirect_hosts=_redirect_hosts(args),
        allow_network_bind=bool(getattr(args, "allow_network_bind", False)),
        tls_certfile=getattr(args, "tls_certfile", None),
        tls_keyfile=getattr(args, "tls_keyfile", None),
        state_dir=state_dir,
        bypass=_saved_profile_bypass(getattr(args, "saved_profile_name", None)),
    )


def serve_bridge(args: argparse.Namespace) -> int:
    """CLI entrypoint: validate, build the Core bridge, and run uvicorn."""
    return run_bridge_serve(config_from_namespace(args))


# Retained for callers that used the earlier spelling.
serve_from_namespace = serve_bridge


def add_serve_arguments(
    result: argparse.ArgumentParser, *, profile_required: bool = True
) -> None:
    """The full ``bridge serve`` option surface, shared by both parsers.

    ``karox.cli bridge serve`` -- the command the launcher spawns -- and the
    standalone ``python -m karox.bridge_serve`` must accept the same argv.
    One definition stops the launcher's flags from drifting out of the CLI
    parser, which made every spawned bridge child exit 2.
    """
    if profile_required:
        result.add_argument(
            "--profile", required=True, choices=sorted(WEB_BRIDGE_PROFILES)
        )
    else:
        # The interactive CLI keeps its documented default; the launcher
        # always passes the profile explicitly.
        result.add_argument(
            "--profile", choices=sorted(WEB_BRIDGE_PROFILES), default="chatgpt-web"
        )
    result.add_argument("--protocol", default="mcp", choices=["mcp"])
    result.add_argument("--repository", required=True)
    result.add_argument("--session-id", required=True)
    result.add_argument("--credential", required=True)
    result.add_argument("--port", type=int, default=8765)
    result.add_argument("--host", default=DEFAULT_BRIDGE_HOST)
    result.add_argument("--path", default="/mcp")
    result.add_argument(
        "--deadline-seconds", type=float, default=DEFAULT_BRIDGE_DEADLINE_SECONDS
    )
    result.add_argument("--saved-profile-name")
    result.add_argument("--project", action="append")
    result.add_argument("--default-project-id")
    result.add_argument("--public-url")
    result.add_argument("--allowed-redirect-host", action="append")
    result.add_argument("--allowed-redirect-hosts")
    result.add_argument("--state-dir")
    result.add_argument("--tool", action="append")
    result.add_argument("--verification-command", action="append")
    result.add_argument("--allow-network-bind", action="store_true")
    result.add_argument("--tls-certfile")
    result.add_argument("--tls-keyfile")


def parser() -> argparse.ArgumentParser:
    """Standalone parser for ``python -m karox.bridge_serve`` / tests."""
    result = argparse.ArgumentParser(prog="karox bridge serve")
    add_serve_arguments(result)
    return result


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parser().parse_args(argv)
    try:
        return serve_bridge(args)
    except BridgeServeError as exc:
        print(f"karox bridge serve: {exc}", file=sys.stderr, flush=True)
        return 2


__all__ = [
    "BridgeServeConfig",
    "BridgeServeError",
    "DEFAULT_BRIDGE_HOST",
    "add_serve_arguments",
    "build_asgi_app",
    "config_from_namespace",
    "main",
    "parser",
    "run_bridge_serve",
    "serve_bridge",
    "serve_from_namespace",
]


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())