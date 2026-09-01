"""Durable private-Tailscale Mission Control service launcher.

The mobile dashboard binds only to a Tailscale CGNAT IPv4 address.  It is never
started on 0.0.0.0, LAN addresses, or a public interface.  A short-lived pairing
code is returned once to the caller and is not persisted in the service registry.
The long-lived process is tracked with strong process identity; graceful shutdown
uses a local stop file rather than a PID-only kill.
"""

from __future__ import annotations

import dataclasses
import ipaddress
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional

from .detached_process import spawn_detached
from .mission_control import MissionControlStore
from .paths import runtime_dir
from .process_identity import ProcessIdentity, capture_process_identity, process_is_running, verify_process_identity

_SCHEMA_VERSION = 1
_TAILSCALE_V4 = ipaddress.ip_network("100.64.0.0/10")


class MobileMissionError(RuntimeError):
    pass


def _safe_run_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 100
        or any(ch not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for ch in value)
    ):
        raise MobileMissionError("mobile Mission Control run id is invalid")
    return value


def _tailscale_host(value: str) -> str:
    try:
        address = ipaddress.ip_address(value)
    except ValueError as exc:
        raise MobileMissionError("mobile Mission Control requires a Tailscale IPv4 address") from exc
    if address.version != 4 or address not in _TAILSCALE_V4:
        raise MobileMissionError("mobile Mission Control refuses non-Tailscale addresses")
    return str(address)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(raw)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


@dataclasses.dataclass(frozen=True)
class MobileMissionRecord:
    run_id: str
    host: str
    port: int
    pid: int
    process_identity: ProcessIdentity
    started_at: float
    launch_mechanism: str

    def __post_init__(self) -> None:
        _safe_run_id(self.run_id)
        _tailscale_host(self.host)
        if not 1 <= int(self.port) <= 65535:
            raise ValueError("mobile Mission Control port is invalid")
        if int(self.pid) != self.process_identity.pid:
            raise ValueError("mobile Mission Control PID and identity disagree")

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "run_id": self.run_id,
            "host": self.host,
            "port": self.port,
            "pid": self.pid,
            "process_identity": self.process_identity.to_dict(),
            "started_at": self.started_at,
            "launch_mechanism": self.launch_mechanism,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "MobileMissionRecord":
        if raw.get("schema_version") != _SCHEMA_VERSION:
            raise MobileMissionError("unsupported mobile Mission Control schema")
        return cls(
            run_id=str(raw["run_id"]),
            host=str(raw["host"]),
            port=int(raw["port"]),
            pid=int(raw["pid"]),
            process_identity=ProcessIdentity.from_dict(raw["process_identity"]),
            started_at=float(raw["started_at"]),
            launch_mechanism=str(raw.get("launch_mechanism") or "unknown"),
        )


@dataclasses.dataclass(frozen=True)
class MobileMissionLaunch:
    record: MobileMissionRecord
    pairing_code: str
    pairing_expires_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.record.to_dict(),
            "url": self.record.url,
            "pairing_code": self.pairing_code,
            "pairing_expires_at": self.pairing_expires_at,
        }


@dataclasses.dataclass(frozen=True)
class MobileMissionView:
    record: MobileMissionRecord
    alive: bool
    identity_proven: bool
    identity_reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.record.to_dict(),
            "url": self.record.url,
            "alive": self.alive,
            "identity_proven": self.identity_proven,
            "identity_reason": self.identity_reason,
        }


class MobileMissionRegistry:
    def __init__(self, *, root: Optional[Path] = None) -> None:
        self.root = (root or (runtime_dir() / "vnext" / "mobile-mission")).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _record_path(self, run_id: str) -> Path:
        return self.root / f"{_safe_run_id(run_id)}.json"

    def _stop_path(self, run_id: str) -> Path:
        return self.root / f".{_safe_run_id(run_id)}.stop"

    def get(self, run_id: str) -> MobileMissionRecord:
        try:
            raw = json.loads(self._record_path(run_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MobileMissionError(f"mobile Mission Control service does not exist: {run_id}") from exc
        if not isinstance(raw, dict):
            raise MobileMissionError("mobile Mission Control record is malformed")
        return MobileMissionRecord.from_dict(raw)

    def view(self, run_id: str) -> MobileMissionView:
        record = self.get(run_id)
        verdict = verify_process_identity(
            record.process_identity,
            pid_alive=process_is_running,
            expected_executable=sys.executable,
        )
        return MobileMissionView(record, verdict.alive, verdict.proven, verdict.reason)

    def list_views(self) -> tuple[MobileMissionView, ...]:
        views: list[MobileMissionView] = []
        for path in sorted(self.root.glob("*.json")):
            try:
                views.append(self.view(path.stem))
            except Exception:
                continue
        views.sort(key=lambda row: row.record.started_at, reverse=True)
        return tuple(views)

    def start(self, *, run_id: str, host: str, timeout_seconds: float = 6.0) -> MobileMissionLaunch:
        run_id = _safe_run_id(run_id)
        host = _tailscale_host(host)
        if MissionControlStore(run_id).snapshot() is None:
            raise MobileMissionError(f"Mission Control run does not exist: {run_id}")
        path = self._record_path(run_id)
        if path.exists():
            try:
                existing = self.view(run_id)
            except Exception:
                existing = None
            if existing is not None and existing.alive:
                raise MobileMissionError(f"mobile Mission Control is already running for {run_id}")
            try:
                path.unlink()
            except OSError as exc:
                raise MobileMissionError("cannot replace stale mobile Mission Control record") from exc

        stamp = f"{os.getpid()}-{time.time_ns()}"
        request = self.root / f".launch-{run_id}-{stamp}.json"
        ready = self.root / f".ready-{run_id}-{stamp}.json"
        stop = self._stop_path(run_id)
        try:
            stop.unlink()
        except FileNotFoundError:
            pass
        _atomic_json(
            request,
            {
                "schema_version": _SCHEMA_VERSION,
                "run_id": run_id,
                "host": host,
                "ready_file": str(ready),
                "stop_file": str(stop),
            },
        )
        argv = [sys.executable, "-m", "karox.mission_control_worker", "--request-file", str(request)]
        process, mechanism = spawn_detached(argv)
        if process is None:
            try:
                request.unlink()
            except FileNotFoundError:
                pass
            raise MobileMissionError(mechanism)
        identity = capture_process_identity(int(process.pid), executable=sys.executable, argv=argv)
        deadline = time.monotonic() + max(1.0, min(float(timeout_seconds), 15.0))
        payload: Optional[dict[str, Any]] = None
        while time.monotonic() < deadline:
            if ready.exists():
                try:
                    candidate = json.loads(ready.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    candidate = None
                if isinstance(candidate, dict):
                    payload = candidate
                    break
            if process.poll() is not None:
                break
            time.sleep(0.05)
        try:
            ready.unlink()
        except FileNotFoundError:
            pass
        if payload is None:
            stop.touch(exist_ok=True)
            raise MobileMissionError("mobile Mission Control did not become ready")
        if str(payload.get("host")) != host:
            stop.touch(exist_ok=True)
            raise MobileMissionError("mobile Mission Control bound an unexpected address")
        port = int(payload.get("port", 0))
        pairing_code = str(payload.get("pairing_code") or "")
        expires_at = float(payload.get("pairing_expires_at", 0.0))
        if not 1 <= port <= 65535 or not pairing_code or expires_at <= time.time():
            stop.touch(exist_ok=True)
            raise MobileMissionError("mobile Mission Control returned invalid readiness data")
        record = MobileMissionRecord(
            run_id=run_id,
            host=host,
            port=port,
            pid=int(process.pid),
            process_identity=identity,
            started_at=time.time(),
            launch_mechanism=mechanism,
        )
        _atomic_json(path, record.to_dict())
        return MobileMissionLaunch(record, pairing_code, expires_at)

    def stop(self, run_id: str, *, wait_seconds: float = 5.0) -> MobileMissionView:
        view = self.view(run_id)
        if view.alive and not view.identity_proven:
            raise MobileMissionError("refusing to stop a mobile service whose process identity is not proven")
        if view.alive:
            stop = self._stop_path(run_id)
            stop.parent.mkdir(parents=True, exist_ok=True)
            stop.touch(exist_ok=True)
            deadline = time.monotonic() + max(0.0, min(float(wait_seconds), 15.0))
            while time.monotonic() < deadline and process_is_running(view.record.pid):
                time.sleep(0.05)
        updated = self.view(run_id)
        if not updated.alive:
            try:
                self._record_path(run_id).unlink()
            except FileNotFoundError:
                pass
        return updated


__all__ = [
    "MobileMissionError",
    "MobileMissionLaunch",
    "MobileMissionRecord",
    "MobileMissionRegistry",
    "MobileMissionView",
]
