"""Atomic structured sessions with exclusive mutation leases."""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import socket
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .models import AccessProfile, repository_fingerprint
from .security import redact


SCHEMA_VERSION = 1


class SessionError(RuntimeError):
    pass


class SessionBusy(SessionError):
    pass


class StaleSessionRevision(SessionError):
    pass


class IdempotencyConflict(SessionError):
    pass


def _checksum(value: Dict[str, Any]) -> str:
    payload = dict(value)
    payload.pop("checksum", None)
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class MutationLease:
    session_id: str
    session_revision: int
    owner: str
    fencing_token: str
    pid: int
    hostname: str
    acquired_at: float
    expires_at: float

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class SessionRecord:
    session_id: str
    repository: str
    repo_fingerprint: str
    branch: str
    access_profile: str
    task: str
    created_at: float
    updated_at: float
    revision: int = 0
    schema_version: int = SCHEMA_VERSION
    checksum: str = ""
    status: str = "active"
    phase: str = "planning"
    plan: List[Dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    decisions: List[Dict[str, Any]] = field(default_factory=list)
    changed_files: List[str] = field(default_factory=list)
    git_state: Dict[str, Any] = field(default_factory=dict)
    checkpoints: List[Dict[str, Any]] = field(default_factory=list)
    checks: List[Dict[str, Any]] = field(default_factory=list)
    failures: List[Dict[str, Any]] = field(default_factory=list)
    jobs: List[Dict[str, Any]] = field(default_factory=list)
    skills: List[Dict[str, Any]] = field(default_factory=list)
    mcp_servers: List[Dict[str, Any]] = field(default_factory=list)
    packs: List[Dict[str, Any]] = field(default_factory=list)
    provider_history: List[Dict[str, Any]] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    connected_clients: List[Dict[str, Any]] = field(default_factory=list)
    evidence: List[Dict[str, Any]] = field(default_factory=list)
    unfinished_actions: List[Dict[str, Any]] = field(default_factory=list)
    idempotency: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    revoked: bool = False

    def to_dict(self) -> Dict[str, Any]:
        value = asdict(self)
        value["checksum"] = _checksum(value)
        return value

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "SessionRecord":
        if int(value.get("schema_version", 0)) != SCHEMA_VERSION:
            raise SessionError(
                f"unsupported session schema {value.get('schema_version')!r}"
            )
        allowed = {item.name for item in fields(cls)}
        unknown = set(value).difference(allowed)
        if unknown:
            raise SessionError(f"unknown session fields: {sorted(unknown)}")
        expected = value.get("checksum")
        if not isinstance(expected, str) or len(expected) != 64:
            raise SessionError("session state has no valid checksum")
        actual = _checksum(value)
        if not hmac.compare_digest(expected, actual):
            raise SessionError("session state checksum mismatch")
        return cls(**value)


def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SessionError(f"session state does not exist: {path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise SessionError(f"cannot read session state {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise SessionError(f"session state must be an object: {path}")
    return value


@contextlib.contextmanager
def _exclusive_file_lock(path: Path) -> Iterator[None]:
    """Serialize session state transitions across processes.

    The lease token is only a fence when validation and replacement happen in
    one critical section.  Keep the lock file separate from the replaceable
    JSON documents so atomic renames cannot invalidate the OS lock.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class SessionStore:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _validate_id(session_id: str) -> str:
        if not session_id or len(session_id) > 100:
            raise SessionError("invalid session ID")
        if any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_" for char in session_id):
            raise SessionError("session ID contains unsafe characters")
        return session_id

    def session_dir(self, session_id: str) -> Path:
        return self.root / self._validate_id(session_id)

    def state_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "session.json"

    def lease_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / "lease.json"

    def lock_path(self, session_id: str) -> Path:
        return self.session_dir(session_id) / ".state.lock"

    def create(
        self,
        repository: Path,
        task: str,
        access_profile: AccessProfile = AccessProfile.WORKSPACE_WRITE,
        branch: str = "",
        session_id: Optional[str] = None,
    ) -> SessionRecord:
        if not isinstance(task, str) or not task.strip():
            raise SessionError("session task must be a non-empty string")
        repo = repository.expanduser().resolve(strict=True)
        if not repo.is_dir():
            raise SessionError(f"repository is not a directory: {repo}")
        sid = self._validate_id(session_id or uuid.uuid4().hex)
        target = self.state_path(sid)
        with _exclusive_file_lock(self.lock_path(sid)):
            if target.exists():
                raise SessionError(f"session already exists: {sid}")
            now = time.time()
            record = SessionRecord(
                session_id=sid,
                repository=str(repo),
                repo_fingerprint=repository_fingerprint(repo),
                branch=branch,
                access_profile=access_profile.value,
                task=str(redact(task)),
                created_at=now,
                updated_at=now,
            )
            payload = record.to_dict()
            _atomic_json(target, payload)
            record.checksum = payload["checksum"]
            return record

    def load(self, session_id: str) -> SessionRecord:
        return SessionRecord.from_dict(_read_json(self.state_path(session_id)))

    def list(self) -> List[SessionRecord]:
        records: List[SessionRecord] = []
        for state in sorted(self.root.glob("*/session.json")):
            try:
                records.append(SessionRecord.from_dict(_read_json(state)))
            except SessionError:
                continue
        return records

    def revoke(self, session_id: str) -> SessionRecord:
        """Atomically revoke a session and fence any active mutation owner."""
        with _exclusive_file_lock(self.lock_path(session_id)):
            record = self.load(session_id)
            record.revoked = True
            record.status = "revoked"
            payload = record.to_dict()
            payload["revision"] = record.revision + 1
            payload["updated_at"] = time.time()
            payload["checksum"] = _checksum(payload)
            _atomic_json(self.state_path(session_id), payload)
            lease_path = self.lease_path(session_id)
            try:
                lease_path.unlink()
            except FileNotFoundError:
                pass
            return SessionRecord.from_dict(payload)

    def acquire(
        self, session_id: str, owner: str, ttl_seconds: float = 30.0
    ) -> MutationLease:
        if ttl_seconds < 5 or ttl_seconds > 3600:
            raise ValueError("lease TTL must be between 5 and 3600 seconds")
        with _exclusive_file_lock(self.lock_path(session_id)):
            record = self.load(session_id)
            if record.revoked:
                raise SessionError(f"session has been revoked: {session_id}")
            path = self.lease_path(session_id)
            now = time.time()
            if path.exists():
                current = _read_json(path)
                if float(current.get("expires_at", 0)) >= now:
                    raise SessionBusy(
                        f"session {session_id} is locked by {current.get('owner', 'unknown')}"
                    )
                stale = path.with_name(f"lease.stale.{uuid.uuid4().hex}.json")
                os.replace(path, stale)
            lease = MutationLease(
                session_id=session_id,
                session_revision=record.revision,
                owner=owner,
                fencing_token=uuid.uuid4().hex,
                pid=os.getpid(),
                hostname=socket.gethostname(),
                acquired_at=now,
                expires_at=now + ttl_seconds,
            )
            _atomic_json(path, lease.to_dict())
            return lease

    def _validate_lease_unlocked(self, lease: MutationLease) -> None:
        if not isinstance(lease, MutationLease):
            raise SessionBusy("a valid mutation lease is required")
        try:
            current = _read_json(self.lease_path(lease.session_id))
        except SessionError as exc:
            raise SessionBusy("mutation lease is no longer active") from exc
        if current.get("fencing_token") != lease.fencing_token:
            raise SessionBusy("mutation lease fencing token is no longer current")
        if float(current.get("expires_at", 0)) < time.time():
            raise SessionBusy("mutation lease expired")

    def validate_lease(self, lease: MutationLease) -> None:
        with _exclusive_file_lock(self.lock_path(lease.session_id)):
            self._validate_lease_unlocked(lease)

    def validate_repository(self, record: SessionRecord, repository: Path) -> None:
        try:
            current = repository.expanduser().resolve(strict=True)
            recorded = Path(record.repository).expanduser().resolve(strict=True)
        except OSError as exc:
            raise SessionError(f"cannot resolve session repository: {exc}") from exc
        if os.path.normcase(str(current)) != os.path.normcase(str(recorded)):
            raise SessionError("session is bound to a different repository")
        if record.repo_fingerprint != repository_fingerprint(current):
            raise SessionError("session repository fingerprint changed")

    def heartbeat(self, lease: MutationLease, ttl_seconds: float = 30.0) -> MutationLease:
        if ttl_seconds < 5 or ttl_seconds > 3600:
            raise ValueError("lease TTL must be between 5 and 3600 seconds")
        with _exclusive_file_lock(self.lock_path(lease.session_id)):
            self._validate_lease_unlocked(lease)
            expires_at = time.time() + ttl_seconds
            session_revision = self.load(lease.session_id).revision
            payload = lease.to_dict()
            payload["expires_at"] = expires_at
            payload["session_revision"] = session_revision
            _atomic_json(self.lease_path(lease.session_id), payload)
            lease.expires_at = expires_at
            lease.session_revision = session_revision
            return lease

    def release(self, lease: MutationLease) -> None:
        with _exclusive_file_lock(self.lock_path(lease.session_id)):
            path = self.lease_path(lease.session_id)
            try:
                current = _read_json(path)
                if current.get("fencing_token") == lease.fencing_token:
                    path.unlink()
            except SessionError:
                return

    def save(
        self,
        record: SessionRecord,
        expected_revision: int,
        lease: MutationLease,
    ) -> SessionRecord:
        with _exclusive_file_lock(self.lock_path(lease.session_id)):
            self._validate_lease_unlocked(lease)
            if record.session_id != lease.session_id:
                raise SessionError("lease and session record do not match")
            current = self.load(record.session_id)
            if current.revision != expected_revision:
                raise StaleSessionRevision(
                    f"expected revision {expected_revision}, found {current.revision}"
                )
            next_revision = expected_revision + 1
            updated_at = time.time()
            payload = record.to_dict()
            payload["revision"] = next_revision
            payload["updated_at"] = updated_at
            payload["checksum"] = _checksum(payload)
            _atomic_json(self.state_path(record.session_id), payload)
            record.revision = next_revision
            record.updated_at = updated_at
            record.checksum = payload["checksum"]
            return record

    @contextlib.contextmanager
    def mutate(
        self, session_id: str, owner: str, ttl_seconds: float = 30.0
    ) -> Iterator[SessionRecord]:
        lease = self.acquire(session_id, owner, ttl_seconds)
        try:
            record = self.load(session_id)
            yield record
            self.save(record, record.revision, lease)
        finally:
            self.release(lease)

    def begin_idempotent(
        self,
        record: SessionRecord,
        lease: MutationLease,
        key: str,
        input_digest: str,
    ) -> Optional[Dict[str, Any]]:
        if not key:
            raise ValueError("mutation requires an idempotency key")
        existing = record.idempotency.get(key)
        if existing:
            if existing.get("input_digest") != input_digest:
                raise IdempotencyConflict("idempotency key was used for different input")
            if existing.get("status") == "complete":
                return dict(existing.get("result") or {})
            raise IdempotencyConflict("prior mutation outcome requires reconciliation")
        revision = record.revision
        pending = {
            "input_digest": input_digest,
            "status": "pending",
            "started_at": time.time(),
        }
        record.idempotency[key] = pending
        try:
            self.save(record, revision, lease)
        except Exception:
            if record.idempotency.get(key) is pending:
                record.idempotency.pop(key, None)
            raise
        return None

    def complete_idempotent(
        self,
        record: SessionRecord,
        lease: MutationLease,
        key: str,
        result: Dict[str, Any],
    ) -> None:
        entry = record.idempotency.get(key)
        if not entry or entry.get("status") != "pending":
            raise IdempotencyConflict("mutation was not reserved")
        revision = record.revision
        previous = dict(entry)
        entry.update({"status": "complete", "completed_at": time.time(), "result": result})
        try:
            self.save(record, revision, lease)
        except Exception:
            entry.clear()
            entry.update(previous)
            raise
