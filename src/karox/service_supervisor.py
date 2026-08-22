"""Generic managed-service supervisor: identity, lease, honest transactions.

Mandate rules this module owns (final-pass sections 12-24):

* **Not Node-specific.** A service is any long-lived, project-owned argv
  the user approved -- npm, uvicorn, cargo, gradle, dotnet, php artisan.
  The supervisor never inspects the stack, only the process contract.
* **PID alone is never identity.** A stored service remembers pid AND
  creation time, executable, and a command-line digest. Before any stop
  or restart the live process is re-verified against all of it; any
  mismatch is a REFUSE, because the OS may have reused the PID for
  someone else's program.
* **Bypass never widens the blast radius.** Even a fully trusted caller
  can only stop processes this supervisor started and recorded. Foreign
  processes, other projects, other workstreams, the control plane, and
  Tailscale are structurally out of reach -- there is no code path that
  kills an arbitrary PID.
* **Self-protection.** A stop that would take down the supervisor's own
  process tree or a protected executable is BLOCKED before any signal.
* **Serialized mutation.** Start/stop/restart take a per-service lease on
  disk; a second mutator gets a clean refusal instead of a race.
* **Restart is a transaction.** identify -> lease -> graceful stop ->
  confirm exit -> start -> readiness -> report. A failed step reports
  exactly what happened with log tails; it never pretends.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, List, Mapping, Optional, Sequence

from .remote_tools import _kill_pid_tree, _pid_alive, _read_log

#: Executable basenames that must never be stopped through this supervisor,
#: even when a recorded service somehow points at them. The control plane
#: protects itself and the transport it depends on.
PROTECTED_EXECUTABLES: frozenset[str] = frozenset(
    {"tailscale", "tailscaled", "cloudflared"}
)


def _now() -> float:
    return time.time()


def _digest(parts: Sequence[str]) -> str:
    joined = "\x1f".join(parts)
    return hashlib.sha256(joined.encode("utf-8", errors="replace")).hexdigest()


@dataclasses.dataclass(frozen=True)
class ProcessIdentity:
    """What we can prove about a live process beyond its PID."""

    pid: int
    created_at: Optional[float]
    executable: Optional[str]
    cmdline_digest: Optional[str]

    @classmethod
    def capture(cls, pid: int) -> "ProcessIdentity":
        """Best-effort identity of a live process; unknown fields stay None."""

        created: Optional[float] = None
        executable: Optional[str] = None
        cmdline_digest: Optional[str] = None
        try:
            import psutil  # type: ignore[import-untyped]

            process = psutil.Process(pid)
            try:
                created = float(process.create_time())
            except Exception:
                created = None
            try:
                executable = str(process.exe() or "") or None
            except Exception:
                executable = None
            try:
                cmdline = [str(item) for item in (process.cmdline() or [])]
                cmdline_digest = _digest(cmdline) if cmdline else None
            except Exception:
                cmdline_digest = None
        except Exception:
            pass
        return cls(
            pid=pid,
            created_at=created,
            executable=executable,
            cmdline_digest=cmdline_digest,
        )


@dataclasses.dataclass(frozen=True)
class IdentityVerdict:
    """The result of re-verifying a stored identity against the live OS."""

    ok: bool
    reason: str

    REFUSE_DEAD = "process is not running"
    REFUSE_CREATED_AT = "creation time differs: the OS reused this PID"
    REFUSE_EXECUTABLE = "executable differs: this PID belongs to another program"
    REFUSE_CMDLINE = "command line differs: this PID belongs to another program"
    REFUSE_UNPROVABLE = (
        "identity cannot be verified on this platform; refusing to signal a "
        "process that may not be ours"
    )
    OK = "identity verified"


def verify_identity(
    stored: ProcessIdentity, live: ProcessIdentity, *, alive: bool
) -> IdentityVerdict:
    """Compare identity captured at start time against the live process.

    Every provable mismatch refuses. When *nothing* about the live process
    can be proven (no psutil, access denied), the verdict also refuses:
    an unverifiable target is treated exactly like a foreign one.
    """

    if not alive:
        return IdentityVerdict(False, IdentityVerdict.REFUSE_DEAD)
    checked = False
    if stored.created_at is not None and live.created_at is not None:
        checked = True
        # Windows FILETIME jitter: identical processes can differ by <1s
        # across queries; a reused PID differs by far more.
        if abs(stored.created_at - live.created_at) > 1.0:
            return IdentityVerdict(False, IdentityVerdict.REFUSE_CREATED_AT)
    if stored.executable and live.executable:
        checked = True
        if os.path.normcase(stored.executable) != os.path.normcase(live.executable):
            return IdentityVerdict(False, IdentityVerdict.REFUSE_EXECUTABLE)
    if stored.cmdline_digest and live.cmdline_digest:
        checked = True
        if stored.cmdline_digest != live.cmdline_digest:
            return IdentityVerdict(False, IdentityVerdict.REFUSE_CMDLINE)
    if not checked:
        return IdentityVerdict(False, IdentityVerdict.REFUSE_UNPROVABLE)
    return IdentityVerdict(True, IdentityVerdict.OK)


@dataclasses.dataclass(frozen=True)
class ManagedService:
    """One supervisor-owned long-lived service and its full identity."""

    service_id: str
    project_id: str
    workstream_id: str
    argv: tuple[str, ...]
    cwd: str
    env_fingerprint: str
    identity: ProcessIdentity
    started_at: float
    stdout_path: str
    stderr_path: str
    ready_url: Optional[str] = None
    restart_policy: str = "manual"

    def to_dict(self) -> dict[str, Any]:
        payload = dataclasses.asdict(self)
        payload["identity"] = dataclasses.asdict(self.identity)
        return payload

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ManagedService":
        identity_raw = payload.get("identity")
        if not isinstance(identity_raw, Mapping):
            raise ValueError("service record has no identity")
        identity = ProcessIdentity(
            pid=int(identity_raw["pid"]),
            created_at=(
                float(identity_raw["created_at"])
                if identity_raw.get("created_at") is not None
                else None
            ),
            executable=(
                str(identity_raw["executable"])
                if identity_raw.get("executable")
                else None
            ),
            cmdline_digest=(
                str(identity_raw["cmdline_digest"])
                if identity_raw.get("cmdline_digest")
                else None
            ),
        )
        return cls(
            service_id=str(payload["service_id"]),
            project_id=str(payload["project_id"]),
            workstream_id=str(payload["workstream_id"]),
            argv=tuple(str(item) for item in payload["argv"]),
            cwd=str(payload["cwd"]),
            env_fingerprint=str(payload["env_fingerprint"]),
            identity=identity,
            started_at=float(payload["started_at"]),
            stdout_path=str(payload["stdout_path"]),
            stderr_path=str(payload["stderr_path"]),
            ready_url=(
                str(payload["ready_url"]) if payload.get("ready_url") else None
            ),
            restart_policy=str(payload.get("restart_policy") or "manual"),
        )


def env_fingerprint(names: Sequence[str]) -> str:
    """Fingerprint WHICH variables were passed -- never their values.

    Secrets must not reach the service record, so the fingerprint hashes
    the sorted names only. Equal fingerprints mean the same shape of
    environment, which is all a restart needs to reproduce honestly.
    """

    return _digest(sorted(str(name) for name in names))


class ServiceLeaseError(RuntimeError):
    """Another mutation currently owns this service."""


class ServiceLease:
    """A per-service on-disk mutation lease with expiry.

    Two agents restarting the same service must serialize: the second
    acquire fails cleanly instead of interleaving stop/start pairs.
    """

    def __init__(self, root: Path, service_id: str, *, owner: str, ttl: float = 120.0) -> None:
        self.path = root / f"{service_id}.lease"
        self.owner = owner
        self.ttl = ttl
        self._held = False

    def acquire(self) -> None:
        now = _now()
        payload = json.dumps({"owner": self.owner, "expires_at": now + self.ttl})
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                existing = json.loads(self.path.read_text(encoding="utf-8"))
                expires = float(existing.get("expires_at") or 0.0)
            except (OSError, ValueError):
                expires = 0.0
            if expires > now:
                raise ServiceLeaseError(
                    f"service is being mutated by {existing.get('owner')!r}"
                ) from None
            # Expired lease: replace atomically.
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        self._held = False
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def __enter__(self) -> "ServiceLease":
        self.acquire()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()


@dataclasses.dataclass(frozen=True)
class ServiceActionResult:
    """The honest outcome of one supervisor action."""

    ok: bool
    action: str
    service_id: str
    reason: str
    pid: Optional[int] = None
    exit_confirmed: Optional[bool] = None
    ready: Optional[bool] = None
    stdout_tail: Optional[dict[str, Any]] = None
    stderr_tail: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class ServiceSupervisor:
    """Owns start/status/logs/stop/restart for recorded project services.

    The store layout is one JSON record per service under the supervisor
    root. Every mutation re-verifies identity first and holds the
    service's lease for its duration. There is deliberately no API that
    accepts a bare PID: the only processes this class can signal are the
    ones it started and recorded itself.
    """

    def __init__(
        self,
        root: Path,
        *,
        project_id: str,
        workstream_id: str,
        owner: str,
        protected_pids: Sequence[int] = (),
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.project_id = project_id
        self.workstream_id = workstream_id
        self.owner = owner
        self._protected_pids = frozenset(
            {int(pid) for pid in protected_pids} | {os.getpid()}
        )

    # -- store -------------------------------------------------------------
    def _record_path(self, service_id: str) -> Path:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", service_id):
            raise ValueError("service_id is malformed")
        return self.root / f"{service_id}.json"

    def _save(self, record: ManagedService) -> None:
        path = self._record_path(record.service_id)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(record.to_dict(), handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
            os.replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def load(self, service_id: str) -> Optional[ManagedService]:
        try:
            payload = json.loads(
                self._record_path(service_id).read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return None
        try:
            return ManagedService.from_dict(payload)
        except (KeyError, TypeError, ValueError):
            return None

    def list(self) -> tuple[ManagedService, ...]:
        records: List[ManagedService] = []
        for path in sorted(self.root.glob("*.json")):
            record = self.load(path.stem)
            if record is not None:
                records.append(record)
        return tuple(records)

    # -- guards ------------------------------------------------------------
    def _ownership_guard(self, record: ManagedService) -> Optional[str]:
        """Why this record may not be touched, or None when it is ours."""

        if record.project_id != self.project_id:
            return (
                f"service belongs to project {record.project_id!r}, "
                f"not {self.project_id!r}"
            )
        if record.workstream_id != self.workstream_id:
            return (
                f"service belongs to workstream {record.workstream_id!r}, "
                f"not {self.workstream_id!r}"
            )
        return None

    def _self_protection_guard(self, record: ManagedService) -> Optional[str]:
        pid = record.identity.pid
        if pid in self._protected_pids:
            return "refusing to stop the control plane's own process tree"
        executable = (record.identity.executable or "").replace("\\", "/")
        basename = executable.rsplit("/", 1)[-1].lower()
        for suffix in (".exe", ".cmd", ".bat", ".com"):
            if basename.endswith(suffix):
                basename = basename[: -len(suffix)]
        if basename in PROTECTED_EXECUTABLES:
            return f"refusing to stop protected executable {basename!r}"
        return None

    # -- actions -----------------------------------------------------------
    def start(
        self,
        *,
        service_id: str,
        argv: Sequence[str],
        cwd: Path,
        env: Optional[Mapping[str, str]] = None,
        ready_url: Optional[str] = None,
        spawn: Optional[Any] = None,
    ) -> ServiceActionResult:
        """Launch and record one approved long-lived command.

        ``spawn`` is the process factory seam (tests inject a fake); the
        default uses ``subprocess.Popen`` detached from our console. The
        recorded identity is captured immediately after launch, when the
        PID provably belongs to the child we created.
        """

        existing = self.load(service_id)
        if existing is not None and _pid_alive(existing.identity.pid):
            return ServiceActionResult(
                ok=False,
                action="start",
                service_id=service_id,
                reason="service is already running; use restart",
                pid=existing.identity.pid,
            )
        with ServiceLease(self.root, service_id, owner=self.owner):
            stdout_path = self.root / f"{service_id}.stdout.log"
            stderr_path = self.root / f"{service_id}.stderr.log"
            child_env = dict(os.environ)
            child_env.update(dict(env or {}))
            factory = spawn or self._default_spawn
            try:
                pid = int(
                    factory(
                        list(argv),
                        cwd=str(cwd),
                        env=child_env,
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                    )
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return ServiceActionResult(
                    ok=False,
                    action="start",
                    service_id=service_id,
                    reason=f"launch failed: {exc}",
                )
            record = ManagedService(
                service_id=service_id,
                project_id=self.project_id,
                workstream_id=self.workstream_id,
                argv=tuple(str(item) for item in argv),
                cwd=str(cwd),
                env_fingerprint=env_fingerprint(sorted((env or {}).keys())),
                identity=ProcessIdentity.capture(pid),
                started_at=_now(),
                stdout_path=str(stdout_path),
                stderr_path=str(stderr_path),
                ready_url=ready_url,
            )
            self._save(record)
            return ServiceActionResult(
                ok=True,
                action="start",
                service_id=service_id,
                reason="started",
                pid=pid,
            )

    @staticmethod
    def _default_spawn(
        argv: List[str],
        *,
        cwd: str,
        env: Mapping[str, str],
        stdout_path: Path,
        stderr_path: Path,
    ) -> int:  # pragma: no cover - thin OS seam, tests inject fakes
        stdout_handle = open(stdout_path, "ab")
        stderr_handle = open(stderr_path, "ab")
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            ) | getattr(subprocess, "CREATE_NO_WINDOW", 0)
        else:
            kwargs["start_new_session"] = True
        process = subprocess.Popen(
            argv,
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=stdout_handle,
            stderr=stderr_handle,
            **kwargs,
        )
        return int(process.pid)

    def status(self, service_id: str) -> ServiceActionResult:
        record = self.load(service_id)
        if record is None:
            return ServiceActionResult(
                ok=False,
                action="status",
                service_id=service_id,
                reason="unknown service",
            )
        alive = _pid_alive(record.identity.pid)
        verdict = verify_identity(
            record.identity,
            ProcessIdentity.capture(record.identity.pid),
            alive=alive,
        )
        return ServiceActionResult(
            ok=True,
            action="status",
            service_id=service_id,
            reason=(
                "running" if verdict.ok else f"not running ({verdict.reason})"
            ),
            pid=record.identity.pid,
        )

    def logs(self, service_id: str) -> ServiceActionResult:
        record = self.load(service_id)
        if record is None:
            return ServiceActionResult(
                ok=False,
                action="logs",
                service_id=service_id,
                reason="unknown service",
            )
        return ServiceActionResult(
            ok=True,
            action="logs",
            service_id=service_id,
            reason="log tails attached",
            pid=record.identity.pid,
            stdout_tail=_read_log(Path(record.stdout_path)),
            stderr_tail=_read_log(Path(record.stderr_path)),
        )

    def stop(self, service_id: str) -> ServiceActionResult:
        record = self.load(service_id)
        if record is None:
            return ServiceActionResult(
                ok=False,
                action="stop",
                service_id=service_id,
                reason="unknown service",
            )
        guard = self._ownership_guard(record) or self._self_protection_guard(record)
        if guard is not None:
            return ServiceActionResult(
                ok=False,
                action="stop",
                service_id=service_id,
                reason=f"BLOCKED: {guard}",
                pid=record.identity.pid,
            )
        with ServiceLease(self.root, service_id, owner=self.owner):
            return self._stop_locked(record)

    def _stop_locked(self, record: ManagedService) -> ServiceActionResult:
        pid = record.identity.pid
        alive = _pid_alive(pid)
        if not alive:
            return ServiceActionResult(
                ok=True,
                action="stop",
                service_id=record.service_id,
                reason="already stopped",
                pid=pid,
                exit_confirmed=True,
            )
        verdict = verify_identity(
            record.identity, ProcessIdentity.capture(pid), alive=alive
        )
        if not verdict.ok:
            return ServiceActionResult(
                ok=False,
                action="stop",
                service_id=record.service_id,
                reason=f"REFUSED: {verdict.reason}",
                pid=pid,
            )
        _kill_pid_tree(pid)
        deadline = _now() + 10.0
        while _now() < deadline:
            if not _pid_alive(pid):
                return ServiceActionResult(
                    ok=True,
                    action="stop",
                    service_id=record.service_id,
                    reason="stopped",
                    pid=pid,
                    exit_confirmed=True,
                )
            time.sleep(0.1)
        return ServiceActionResult(
            ok=False,
            action="stop",
            service_id=record.service_id,
            reason="process did not exit within 10s",
            pid=pid,
            exit_confirmed=False,
            stdout_tail=_read_log(Path(record.stdout_path)),
            stderr_tail=_read_log(Path(record.stderr_path)),
        )

    def restart(
        self,
        service_id: str,
        *,
        spawn: Optional[Any] = None,
        readiness_probe: Optional[Any] = None,
    ) -> ServiceActionResult:
        """The restart transaction: never a blind kill+start.

        identify -> lease -> graceful stop -> confirm exit -> start ->
        readiness. Every failure returns the step it failed at plus log
        tails, so the caller reports evidence instead of hope.
        """

        record = self.load(service_id)
        if record is None:
            return ServiceActionResult(
                ok=False,
                action="restart",
                service_id=service_id,
                reason="unknown service",
            )
        guard = self._ownership_guard(record) or self._self_protection_guard(record)
        if guard is not None:
            return ServiceActionResult(
                ok=False,
                action="restart",
                service_id=service_id,
                reason=f"BLOCKED: {guard}",
                pid=record.identity.pid,
            )
        with ServiceLease(self.root, service_id, owner=self.owner):
            stopped = self._stop_locked(record)
            if not stopped.ok:
                return dataclasses.replace(stopped, action="restart")
            stdout_path = Path(record.stdout_path)
            stderr_path = Path(record.stderr_path)
            child_env = dict(os.environ)
            factory = spawn or self._default_spawn
            try:
                pid = int(
                    factory(
                        list(record.argv),
                        cwd=record.cwd,
                        env=child_env,
                        stdout_path=stdout_path,
                        stderr_path=stderr_path,
                    )
                )
            except (OSError, ValueError, subprocess.SubprocessError) as exc:
                return ServiceActionResult(
                    ok=False,
                    action="restart",
                    service_id=service_id,
                    reason=f"stop succeeded but relaunch failed: {exc}",
                    exit_confirmed=True,
                    stdout_tail=_read_log(stdout_path),
                    stderr_tail=_read_log(stderr_path),
                )
            refreshed = dataclasses.replace(
                record,
                identity=ProcessIdentity.capture(pid),
                started_at=_now(),
            )
            self._save(refreshed)
            ready: Optional[bool] = None
            if readiness_probe is not None:
                try:
                    ready = bool(readiness_probe(refreshed))
                except Exception:
                    ready = False
            if ready is False:
                return ServiceActionResult(
                    ok=False,
                    action="restart",
                    service_id=service_id,
                    reason="restarted process failed its readiness probe",
                    pid=pid,
                    exit_confirmed=True,
                    ready=False,
                    stdout_tail=_read_log(stdout_path),
                    stderr_tail=_read_log(stderr_path),
                )
            return ServiceActionResult(
                ok=True,
                action="restart",
                service_id=service_id,
                reason="restarted",
                pid=pid,
                exit_confirmed=True,
                ready=ready,
            )


__all__ = [
    "IdentityVerdict",
    "ManagedService",
    "PROTECTED_EXECUTABLES",
    "ProcessIdentity",
    "ServiceActionResult",
    "ServiceLease",
    "ServiceLeaseError",
    "ServiceSupervisor",
    "env_fingerprint",
    "verify_identity",
]
