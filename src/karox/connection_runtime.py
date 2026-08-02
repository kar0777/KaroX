"""Managed lifecycle state for saved MCP client connections.

A saved connection configuration is not the same thing as a running bridge.
This module keeps that distinction explicit:

* :class:`ConnectionRuntimeStore` persists only safe process metadata;
* :class:`ConnectionRuntimeManager` owns live stop callbacks in the current
  KaroX process and reconciles them with the persisted record;
* a record discovered after an application restart is adopted only when a
  strong process identity proves it is the same process KaroX launched, and is
  otherwise reported as ``unmanaged_running``.

The last rule is deliberate. A PID can be reused, so a stored PID is never
sufficient authority to terminate a process. :mod:`karox.process_identity`
supplies the missing proof: the operating-system process creation time plus
digests of the expected executable, argv and owner. When the identity verifies,
KaroX re-adopts the runtime after a restart and may stop it. When it does not
verify -- because the platform cannot report a creation time, because the PID
was reused, or because an older record predates identity capture -- KaroX
reports the honest unmanaged state and refuses the unsafe stop.
"""

from __future__ import annotations

import json
import os
import re
import signal
import threading
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Optional, Sequence

from .paths import runtime_dir
from .process_identity import (
    CreateTimeReader,
    ProcessIdentity,
    ProcessIdentityError,
    ProcessIdentityVerdict,
    capture_process_identity,
    read_process_create_time_ns,
    verify_process_identity,
)
from .remote_tools import _pid_alive


RUNTIME_SCHEMA_VERSION = 2
SUPPORTED_RUNTIME_SCHEMA_VERSIONS = frozenset({1, 2})
#: How long a terminate request is given to take effect before KaroX reports a
#: degraded stop instead of claiming success it cannot prove.
TERMINATE_GRACE_SECONDS = 5.0
_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class ConnectionRuntimeError(RuntimeError):
    """A connection runtime operation failed without exposing credentials."""


@dataclass(frozen=True)
class ConnectionRuntimeRecord:
    runtime_id: str
    connection_id: str
    session_id: str
    tunnel: str
    local_endpoint: str
    public_endpoint: str
    bridge_pid: Optional[int]
    tunnel_pid: Optional[int]
    started_at: float
    status: str = "running"
    stopped_at: Optional[float] = None
    bridge_identity: Optional[dict[str, Any]] = None
    tunnel_identity: Optional[dict[str, Any]] = None

    def __post_init__(self) -> None:
        for label, value in (
            ("runtime ID", self.runtime_id),
            ("connection ID", self.connection_id),
            ("session ID", self.session_id),
        ):
            if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
                raise ConnectionRuntimeError(f"{label} is invalid")
        if self.tunnel not in {"cloudflare", "tailscale", "local", "custom"}:
            raise ConnectionRuntimeError("runtime tunnel is invalid")
        for label, value in (
            ("local endpoint", self.local_endpoint),
            ("public endpoint", self.public_endpoint),
        ):
            if not isinstance(value, str) or not value or len(value) > 4096:
                raise ConnectionRuntimeError(f"{label} is invalid")
        for label, pid_value in (("bridge PID", self.bridge_pid), ("tunnel PID", self.tunnel_pid)):
            if pid_value is not None and (
                not isinstance(pid_value, int)
                or isinstance(pid_value, bool)
                or pid_value <= 0
            ):
                raise ConnectionRuntimeError(f"{label} is invalid")
        if not isinstance(self.started_at, (int, float)) or self.started_at <= 0:
            raise ConnectionRuntimeError("runtime start time is invalid")
        if self.status not in {
            "running",
            "degraded",
            "unmanaged_running",
            "stopped",
        }:
            raise ConnectionRuntimeError("runtime status is invalid")
        if self.stopped_at is not None and (
            not isinstance(self.stopped_at, (int, float)) or self.stopped_at <= 0
        ):
            raise ConnectionRuntimeError("runtime stop time is invalid")
        for label, pid, payload in (
            ("bridge", self.bridge_pid, self.bridge_identity),
            ("tunnel", self.tunnel_pid, self.tunnel_identity),
        ):
            if payload is None:
                continue
            try:
                identity = ProcessIdentity.from_dict(payload)
            except ProcessIdentityError as exc:
                raise ConnectionRuntimeError(
                    f"{label} process identity is invalid"
                ) from exc
            if pid is not None and identity.pid != pid:
                raise ConnectionRuntimeError(
                    f"{label} process identity does not match its PID"
                )

    def identity_for(self, role: str) -> Optional[ProcessIdentity]:
        """Return the parsed identity for ``bridge`` or ``tunnel``."""

        if role == "bridge":
            payload = self.bridge_identity
        elif role == "tunnel":
            payload = self.tunnel_identity
        else:
            raise ConnectionRuntimeError(f"unknown runtime process role: {role}")
        if payload is None:
            return None
        return ProcessIdentity.from_dict(payload)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> "ConnectionRuntimeRecord":
        if not isinstance(value, dict):
            raise ConnectionRuntimeError("runtime record must be an object")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(value).difference(allowed)
        if unknown:
            raise ConnectionRuntimeError(
                f"unknown runtime fields: {sorted(unknown)}"
            )
        try:
            return cls(**value)
        except (KeyError, TypeError, ValueError) as exc:
            raise ConnectionRuntimeError("runtime record is malformed") from exc


class ConnectionRuntimeStore:
    """Atomic, schema-versioned storage for safe runtime metadata."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def _load(self) -> list[ConnectionRuntimeRecord]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConnectionRuntimeError(
                f"cannot read connection runtime registry: {type(exc).__name__}"
            ) from exc
        if not isinstance(payload, dict):
            raise ConnectionRuntimeError("connection runtime registry is invalid")
        # A version 1 registry is read forward: it simply carries no process
        # identity, which resolves to "not recorded" and keeps the safe refusal.
        if payload.get("schema_version") not in SUPPORTED_RUNTIME_SCHEMA_VERSIONS:
            raise ConnectionRuntimeError("connection runtime registry schema is unsupported")
        raw = payload.get("runtimes")
        if not isinstance(raw, list):
            raise ConnectionRuntimeError("connection runtime records must be an array")
        return [ConnectionRuntimeRecord.from_dict(item) for item in raw]

    def _save(self, records: Iterable[ConnectionRuntimeRecord]) -> None:
        items = sorted(records, key=lambda item: item.connection_id)
        seen: set[str] = set()
        for item in items:
            if item.connection_id in seen:
                raise ConnectionRuntimeError(
                    f"duplicate runtime for connection: {item.connection_id}"
                )
            seen.add(item.connection_id)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        try:
            with temp.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {
                        "schema_version": RUNTIME_SCHEMA_VERSION,
                        "runtimes": [item.to_dict() for item in items],
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def list(self) -> list[ConnectionRuntimeRecord]:
        return self._load()

    def get(self, connection_id: str) -> Optional[ConnectionRuntimeRecord]:
        return next(
            (item for item in self._load() if item.connection_id == connection_id),
            None,
        )

    def put(self, record: ConnectionRuntimeRecord) -> ConnectionRuntimeRecord:
        records = [
            item for item in self._load() if item.connection_id != record.connection_id
        ]
        records.append(record)
        self._save(records)
        return record

    def remove(self, connection_id: str) -> Optional[ConnectionRuntimeRecord]:
        existing = self.get(connection_id)
        if existing is None:
            return None
        self._save(
            item for item in self._load() if item.connection_id != connection_id
        )
        return existing


def connection_runtime_path() -> Path:
    return runtime_dir() / "vnext" / "connections" / "runtimes.json"


def _default_terminate(pid: int) -> None:
    """Ask one already identity-verified process to exit.

    This is only ever reached after :func:`verify_process_identity` returned a
    proven verdict for the exact PID, so it cannot signal a recycled PID.
    """

    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        raise ConnectionRuntimeError("cannot terminate an invalid PID")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except (OSError, ValueError) as exc:
        raise ConnectionRuntimeError(
            f"cannot terminate connection runtime process: {type(exc).__name__}"
        ) from exc


class ConnectionRuntimeManager:
    """Own live connection processes and expose an honest health state."""

    def __init__(
        self,
        store: Optional[ConnectionRuntimeStore] = None,
        *,
        pid_alive: Callable[[int], bool] = _pid_alive,
        create_time_reader: CreateTimeReader = read_process_create_time_ns,
        terminate: Optional[Callable[[int], None]] = None,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.store = store or ConnectionRuntimeStore(connection_runtime_path())
        self._pid_alive = pid_alive
        self._create_time_reader = create_time_reader
        self._terminate = terminate or _default_terminate
        self._sleep = sleep
        self._monotonic = monotonic
        self._stops: dict[str, Callable[[], None]] = {}
        self._lock = threading.RLock()

    def register(
        self,
        *,
        connection_id: str,
        session_id: str,
        tunnel: str,
        local_endpoint: str,
        public_endpoint: str,
        bridge_pid: Optional[int],
        tunnel_pid: Optional[int],
        stop: Callable[[], None],
        bridge_executable: Optional[str] = None,
        bridge_argv: Optional[Sequence[str]] = None,
        tunnel_executable: Optional[str] = None,
        tunnel_argv: Optional[Sequence[str]] = None,
    ) -> ConnectionRuntimeRecord:
        if not callable(stop):
            raise ConnectionRuntimeError("runtime stop callback is required")
        with self._lock:
            existing = self.status(connection_id)
            if existing["state"] in {"running", "degraded", "unmanaged_running"}:
                raise ConnectionRuntimeError(
                    f"connection runtime already exists: {connection_id}"
                )
            record = ConnectionRuntimeRecord(
                runtime_id=f"rt-{uuid.uuid4().hex[:16]}",
                connection_id=connection_id,
                session_id=session_id,
                tunnel=tunnel,
                local_endpoint=local_endpoint,
                public_endpoint=public_endpoint,
                bridge_pid=bridge_pid,
                tunnel_pid=tunnel_pid,
                started_at=time.time(),
                bridge_identity=self._capture(
                    bridge_pid, bridge_executable, bridge_argv
                ),
                tunnel_identity=self._capture(
                    tunnel_pid, tunnel_executable, tunnel_argv
                ),
            )
            self.store.put(record)
            self._stops[connection_id] = stop
            return record

    def _capture(
        self,
        pid: Optional[int],
        executable: Optional[str],
        argv: Optional[Sequence[str]],
    ) -> Optional[dict[str, Any]]:
        """Capture identity for a just-launched PID without failing the launch."""

        if pid is None:
            return None
        try:
            identity = capture_process_identity(
                pid,
                executable=executable,
                argv=argv,
                create_time_reader=self._create_time_reader,
            )
        except ProcessIdentityError:
            return None
        return identity.to_dict()

    def _verdict(
        self, record: ConnectionRuntimeRecord, role: str
    ) -> ProcessIdentityVerdict:
        try:
            identity = record.identity_for(role)
        except (ConnectionRuntimeError, ProcessIdentityError):
            identity = None
        return verify_process_identity(
            identity,
            pid_alive=lambda pid: bool(self._alive(pid)),
            create_time_reader=self._create_time_reader,
        )

    def _alive(self, pid: Optional[int]) -> Optional[bool]:
        if pid is None:
            return None
        try:
            return bool(self._pid_alive(pid))
        except Exception:
            return False

    def status(self, connection_id: str) -> dict[str, Any]:
        with self._lock:
            record = self.store.get(connection_id)
            if record is None:
                return {
                    "connection_id": connection_id,
                    "state": "configured_not_running",
                    "managed": False,
                }
            managed = connection_id in self._stops
            bridge_alive = self._alive(record.bridge_pid)
            tunnel_alive = self._alive(record.tunnel_pid)
            bridge_verdict = self._verdict(record, "bridge")
            tunnel_verdict = self._verdict(record, "tunnel")
            tunnel_optional = (
                record.tunnel in {"local", "custom"} or record.tunnel_pid is None
            )
            # A runtime is re-adoptable across a KaroX restart only when every
            # process it owns is proven, not merely alive on a stored PID.
            adoptable = bool(
                bridge_verdict.proven and (tunnel_optional or tunnel_verdict.proven)
            )

            if record.status == "stopped":
                state = "stopped"
            elif managed:
                bridge_ok = bridge_alive is not False
                tunnel_ok = tunnel_optional or tunnel_alive is not False
                state = "running" if bridge_ok and tunnel_ok else "degraded"
            elif adoptable:
                # Proven identity: this is the same process KaroX launched, so
                # the honest state is running and a stop is allowed.
                state = "running"
            elif bridge_alive or tunnel_alive:
                # An alive PID without proof. Report it, never adopt or kill it.
                state = "unmanaged_running"
            else:
                state = "stopped"

            if state != record.status:
                stopped_at = record.stopped_at
                if state == "stopped" and stopped_at is None:
                    stopped_at = time.time()
                record = replace(record, status=state, stopped_at=stopped_at)
                self.store.put(record)

            return {
                "connection_id": connection_id,
                "runtime_id": record.runtime_id,
                "session_id": record.session_id,
                "state": state,
                "managed": managed,
                "adopted": bool(state == "running" and not managed),
                "identity_verified": adoptable,
                "bridge_identity_state": bridge_verdict.reason,
                "tunnel_identity_state": tunnel_verdict.reason,
                "bridge_pid": record.bridge_pid,
                "bridge_alive": bridge_alive,
                "tunnel_pid": record.tunnel_pid,
                "tunnel_alive": tunnel_alive,
                "tunnel": record.tunnel,
                "local_endpoint": record.local_endpoint,
                "public_endpoint": record.public_endpoint,
                "started_at": record.started_at,
                "stopped_at": record.stopped_at,
            }

    def stop(self, connection_id: str) -> dict[str, Any]:
        with self._lock:
            current = self.status(connection_id)
            if current["state"] == "configured_not_running":
                return current
            callback = self._stops.pop(connection_id, None)
            if callback is None:
                if current["state"] == "stopped":
                    return current
                if current.get("identity_verified"):
                    return self._stop_adopted(connection_id)
                raise ConnectionRuntimeError(
                    "the runtime is not owned by this KaroX process and its "
                    "process identity cannot be proven; refusing to stop by PID alone"
                )
            try:
                callback()
            except Exception as exc:
                # Put ownership back so the user can retry and diagnostics stay
                # honest about the still-managed runtime.
                self._stops[connection_id] = callback
                raise ConnectionRuntimeError(
                    f"cannot stop connection runtime: {type(exc).__name__}"
                ) from exc
            record = self.store.get(connection_id)
            if record is not None:
                self.store.put(
                    replace(record, status="stopped", stopped_at=time.time())
                )
            return self.status(connection_id)

    def _stop_adopted(self, connection_id: str) -> dict[str, Any]:
        """Stop a runtime this KaroX run re-adopted through proven identity.

        Identity is re-verified immediately before any signal, so a PID that
        was recycled between the status read and the stop is never signalled.
        A stop is only reported as successful once the processes are gone.
        """

        record = self.store.get(connection_id)
        if record is None:
            return self.status(connection_id)
        proven: list[int] = []
        unproven: list[int] = []
        for role, pid in (("bridge", record.bridge_pid), ("tunnel", record.tunnel_pid)):
            if pid is None:
                continue
            if self._verdict(record, role).proven:
                proven.append(pid)
            elif self._alive(pid):
                unproven.append(pid)
        if unproven:
            raise ConnectionRuntimeError(
                "part of this connection runtime cannot be proven to be the "
                "process KaroX launched; refusing a partial stop"
            )
        for pid in proven:
            self._terminate(pid)
        deadline = self._monotonic() + TERMINATE_GRACE_SECONDS
        while True:
            still_alive = [pid for pid in proven if self._alive(pid)]
            if not still_alive or self._monotonic() >= deadline:
                break
            self._sleep(0.1)
        if still_alive:
            # Never report a stop KaroX cannot prove happened.
            raise ConnectionRuntimeError(
                "the connection runtime did not exit within the stop grace period"
            )
        self.store.put(replace(record, status="stopped", stopped_at=time.time()))
        return self.status(connection_id)

    def forget(self, connection_id: str) -> None:
        with self._lock:
            state = self.status(connection_id)["state"]
            if state in {"running", "degraded", "unmanaged_running"}:
                raise ConnectionRuntimeError(
                    "stop the connection runtime before forgetting it"
                )
            self._stops.pop(connection_id, None)
            self.store.remove(connection_id)


_MANAGERS: dict[str, ConnectionRuntimeManager] = {}
_MANAGERS_LOCK = threading.Lock()


def connection_runtime_manager() -> ConnectionRuntimeManager:
    path = str(connection_runtime_path().resolve())
    with _MANAGERS_LOCK:
        manager = _MANAGERS.get(path)
        if manager is None:
            manager = ConnectionRuntimeManager(ConnectionRuntimeStore(Path(path)))
            _MANAGERS[path] = manager
        return manager


__all__ = [
    "RUNTIME_SCHEMA_VERSION",
    "SUPPORTED_RUNTIME_SCHEMA_VERSIONS",
    "TERMINATE_GRACE_SECONDS",
    "ConnectionRuntimeError",
    "ConnectionRuntimeRecord",
    "ConnectionRuntimeStore",
    "ConnectionRuntimeManager",
    "connection_runtime_path",
    "connection_runtime_manager",
]
