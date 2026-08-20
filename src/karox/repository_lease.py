"""Repository-scoped mutation leases for concurrent hosted clients.

Many read-only sessions may inspect the same repository, but only one session may
mutate it at a time.  The lease is outside SessionStore because two clients use
different session IDs.  PID creation markers defeat PID reuse during strict
stale recovery; a live owner is never taken over automatically.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .paths import runtime_dir
from .sessions import _atomic_json, _exclusive_file_lock

LEASE_SCHEMA_VERSION = 1
DEFAULT_REPOSITORY_LEASE_TTL_SECONDS = 90.0


class RepositoryLeaseError(RuntimeError):
    pass


class RepositoryLeaseConflict(RepositoryLeaseError):
    def __init__(self, details: dict[str, Any]) -> None:
        super().__init__("repository mutation lease is held by another live session")
        self.details = details


def _process_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _windows_creation_marker(pid: int) -> Optional[str]:
    if os.name != "nt":
        return None
    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        return None
    try:
        creation = ctypes.c_ulonglong()
        exit_time = ctypes.c_ulonglong()
        kernel = ctypes.c_ulonglong()
        user = ctypes.c_ulonglong()
        ok = kernel32.GetProcessTimes(
            process,
            ctypes.byref(creation),
            ctypes.byref(exit_time),
            ctypes.byref(kernel),
            ctypes.byref(user),
        )
        return str(creation.value) if ok else None
    finally:
        kernel32.CloseHandle(process)


def _posix_creation_marker(pid: int) -> Optional[str]:
    stat = Path(f"/proc/{pid}/stat")
    try:
        raw = stat.read_text(encoding="utf-8")
    except OSError:
        return None
    closing = raw.rfind(")")
    if closing < 0:
        return None
    fields = raw[closing + 2 :].split()
    # /proc/<pid>/stat field 22 is process starttime. After removing pid/comm,
    # it is zero-based index 19 in the remaining field list.
    return fields[19] if len(fields) > 19 else None


def process_creation_marker(pid: int) -> Optional[str]:
    if os.name == "nt":
        return _windows_creation_marker(pid)
    return _posix_creation_marker(pid)


@dataclass(frozen=True)
class RepositoryLease:
    lease_id: str
    repository_identity: str
    repository: str
    session_id: str
    task_id: str
    connection_id: str
    owner_pid: int
    owner_creation_marker: Optional[str]
    created_at: float
    heartbeat_at: float
    expires_at: float
    current_operation: str
    schema_version: int = LEASE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "repository_identity": self.repository_identity,
            "repository": self.repository,
            "session_id": self.session_id,
            "task_id": self.task_id,
            "connection_id": self.connection_id,
            "owner_pid": self.owner_pid,
            "owner_creation_marker": self.owner_creation_marker,
            "created_at": self.created_at,
            "heartbeat_at": self.heartbeat_at,
            "expires_at": self.expires_at,
            "current_operation": self.current_operation,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "RepositoryLease":
        if int(payload.get("schema_version", 0)) != LEASE_SCHEMA_VERSION:
            raise RepositoryLeaseError("unsupported repository lease schema")
        return cls(
            lease_id=str(payload["lease_id"]),
            repository_identity=str(payload["repository_identity"]),
            repository=str(payload["repository"]),
            session_id=str(payload["session_id"]),
            task_id=str(payload["task_id"]),
            connection_id=str(payload["connection_id"]),
            owner_pid=int(payload["owner_pid"]),
            owner_creation_marker=(
                str(payload["owner_creation_marker"])
                if payload.get("owner_creation_marker") is not None
                else None
            ),
            created_at=float(payload["created_at"]),
            heartbeat_at=float(payload["heartbeat_at"]),
            expires_at=float(payload["expires_at"]),
            current_operation=str(payload.get("current_operation") or "unknown"),
            schema_version=int(payload["schema_version"]),
        )


class RepositoryLeaseStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = (root or (runtime_dir() / "vnext" / "repository-leases")).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def repository_identity(repository: Path) -> str:
        resolved = str(repository.expanduser().resolve(strict=True))
        normalized = os.path.normcase(resolved)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _paths(self, repository: Path) -> tuple[Path, Path]:
        identity = self.repository_identity(repository)
        return self.root / f"{identity}.json", self.root / f"{identity}.lock"

    def _load_path(self, path: Path) -> Optional[RepositoryLease]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise RepositoryLeaseError("repository lease is unreadable") from exc
        if not isinstance(payload, dict):
            raise RepositoryLeaseError("repository lease is malformed")
        return RepositoryLease.from_dict(payload)

    def load(self, repository: Path) -> Optional[RepositoryLease]:
        path, _lock = self._paths(repository)
        return self._load_path(path)

    @staticmethod
    def _same_process(lease: RepositoryLease) -> bool:
        if not _process_alive(lease.owner_pid):
            return False
        current = process_creation_marker(lease.owner_pid)
        if lease.owner_creation_marker is None or current is None:
            # When the platform cannot prove creation time, a live PID is treated
            # conservatively as the original owner. Never auto-take over.
            return True
        return current == lease.owner_creation_marker

    @staticmethod
    def conflict_details(lease: RepositoryLease, now: Optional[float] = None) -> dict[str, Any]:
        instant = time.time() if now is None else now
        return {
            "lease_owner": {
                "session_id": lease.session_id,
                "task_id": lease.task_id,
                "connection_id": lease.connection_id,
            },
            "lease_age_seconds": max(0.0, instant - lease.created_at),
            "heartbeat_age_seconds": max(0.0, instant - lease.heartbeat_at),
            "expires_at": lease.expires_at,
            "current_operation": lease.current_operation,
            "read_only_operations_allowed": True,
            "safe_options": [
                "wait",
                "continue_read_only",
                "request_user_takeover",
                "recover_stale_after_strict_process_check",
            ],
            "automatic_takeover_allowed": False,
        }

    def acquire(
        self,
        repository: Path,
        *,
        session_id: str,
        task_id: str,
        connection_id: str,
        current_operation: str,
        ttl_seconds: float = DEFAULT_REPOSITORY_LEASE_TTL_SECONDS,
    ) -> tuple[RepositoryLease, bool]:
        if not 10.0 <= ttl_seconds <= 3600.0:
            raise ValueError("repository lease ttl must be between 10 and 3600 seconds")
        path, lock_path = self._paths(repository)
        now = time.time()
        recovered_stale = False
        with _exclusive_file_lock(lock_path):
            existing = self._load_path(path)
            if existing is not None:
                same_owner = (
                    existing.session_id == session_id
                    and existing.task_id == task_id
                    and existing.connection_id == connection_id
                )
                if same_owner and self._same_process(existing):
                    refreshed = RepositoryLease(
                        lease_id=existing.lease_id,
                        repository_identity=existing.repository_identity,
                        repository=existing.repository,
                        session_id=existing.session_id,
                        task_id=existing.task_id,
                        connection_id=existing.connection_id,
                        owner_pid=existing.owner_pid,
                        owner_creation_marker=existing.owner_creation_marker,
                        created_at=existing.created_at,
                        heartbeat_at=now,
                        expires_at=now + ttl_seconds,
                        current_operation=current_operation,
                    )
                    _atomic_json(path, refreshed.to_dict())
                    return refreshed, False
                if existing.expires_at > now or self._same_process(existing):
                    raise RepositoryLeaseConflict(self.conflict_details(existing, now))
                recovered_stale = True
            pid = os.getpid()
            lease = RepositoryLease(
                lease_id=f"repo-lease-{uuid.uuid4().hex}",
                repository_identity=self.repository_identity(repository),
                repository=str(repository.expanduser().resolve(strict=True)),
                session_id=session_id,
                task_id=task_id,
                connection_id=connection_id,
                owner_pid=pid,
                owner_creation_marker=process_creation_marker(pid),
                created_at=now,
                heartbeat_at=now,
                expires_at=now + ttl_seconds,
                current_operation=current_operation,
            )
            _atomic_json(path, lease.to_dict())
            return lease, recovered_stale

    def heartbeat(
        self,
        repository: Path,
        lease: RepositoryLease,
        *,
        current_operation: str,
        ttl_seconds: float = DEFAULT_REPOSITORY_LEASE_TTL_SECONDS,
    ) -> RepositoryLease:
        path, lock_path = self._paths(repository)
        now = time.time()
        with _exclusive_file_lock(lock_path):
            current = self._load_path(path)
            if current is None or current.lease_id != lease.lease_id:
                raise RepositoryLeaseError("repository lease ownership was lost")
            updated = RepositoryLease(
                lease_id=current.lease_id,
                repository_identity=current.repository_identity,
                repository=current.repository,
                session_id=current.session_id,
                task_id=current.task_id,
                connection_id=current.connection_id,
                owner_pid=current.owner_pid,
                owner_creation_marker=current.owner_creation_marker,
                created_at=current.created_at,
                heartbeat_at=now,
                expires_at=now + ttl_seconds,
                current_operation=current_operation,
            )
            _atomic_json(path, updated.to_dict())
            return updated

    def release(self, repository: Path, lease: RepositoryLease) -> None:
        path, lock_path = self._paths(repository)
        with _exclusive_file_lock(lock_path):
            current = self._load_path(path)
            if current is None:
                return
            if current.lease_id != lease.lease_id:
                raise RepositoryLeaseError("cannot release another session's repository lease")
            try:
                path.unlink()
            except FileNotFoundError:
                pass
