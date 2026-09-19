"""Atomic structured sessions with exclusive mutation leases."""

from __future__ import annotations

import contextlib
import contextvars
import copy
import hashlib
import hmac
import json
import os
import shutil
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


def _clean_session_name(value: str) -> str:
    """Return a short single-line, secret-filtered display name."""

    if not isinstance(value, str):
        raise SessionError("session name must be text")
    cleaned = " ".join(str(redact(value)).split()).strip()
    if len(cleaned) > 120:
        raise SessionError("session name must be 120 characters or fewer")
    return cleaned


def _fork_context(record: "SessionRecord", limit: int = 6000) -> str:
    """Render bounded structured lineage context without executable state."""

    payload = {
        "source_session": record.session_id,
        "source_task": record.task,
        "summary": record.summary,
        "plan": record.plan[-20:],
        "decisions": record.decisions[-20:],
        "changed_files": record.changed_files[-100:],
        "checks": record.checks[-20:],
        "evidence": record.evidence[-20:],
        "unfinished_actions": record.unfinished_actions[-20:],
    }
    text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return str(redact(text))[:limit]


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


def _process_is_running(pid: int) -> bool:
    """Conservatively report whether a local PID can still own a lease."""

    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # An unexpected OS error (observed once as a spurious WinError 87
        # from the existence probe) must not declare a live owner dead:
        # stealing its lease would fail the owner's in-flight mutation.
        # Keep the lease and let normal expiry recover it instead.
        return True
    return True


def _lease_owner_is_provably_dead(payload: Dict[str, Any]) -> bool:
    """Recover only same-host leases whose owner PID is definitely gone.

    A bridge child restart used to leave a still-unexpired session lease behind,
    blocking every writer for the full TTL even though the OS had already proved
    that its owner no longer existed. Cross-host or malformed ownership remains
    conservative and waits for normal expiry; PID reuse can only delay recovery,
    never steal a live lease.
    """

    if str(payload.get("hostname") or "") != socket.gethostname():
        return False
    pid = payload.get("pid")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    return not _process_is_running(pid)


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


_INHERITED_MUTATION_LEASE: contextvars.ContextVar[Optional[MutationLease]] = (
    contextvars.ContextVar("karox_inherited_mutation_lease", default=None)
)


@contextlib.contextmanager
def mutation_lease_context(lease: MutationLease) -> Iterator[MutationLease]:
    """Expose one already-owned lease to strictly nested calls in this context.

    Context variables are intentionally used instead of PID/owner matching: a
    parallel hosted request in another thread/task must still acquire its own
    lease and therefore remains fenced by :class:`SessionBusy`.
    """

    if not isinstance(lease, MutationLease):
        raise SessionBusy("a valid mutation lease is required")
    token = _INHERITED_MUTATION_LEASE.set(lease)
    try:
        yield lease
    finally:
        _INHERITED_MUTATION_LEASE.reset(token)


def current_mutation_lease(session_id: str) -> Optional[MutationLease]:
    """Return a same-session inherited lease for the current execution context."""

    lease = _INHERITED_MUTATION_LEASE.get()
    if lease is None or lease.session_id != session_id:
        return None
    return lease


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
    # The current user objective plus previous turn objectives. Keeping task as
    # the current turn preserves the existing agent contract, while task_history
    # makes resume/fork lineage inspectable without replaying transcripts.
    task_history: List[str] = field(default_factory=list)
    # Bounded structured context used only when a fork starts with a fresh model
    # history. It never contains raw tool arguments, credentials, or live jobs.
    continuation_context: str = ""
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
    # User-facing lifecycle metadata. These fields intentionally have defaults so
    # schema-v1 session files written before KaroX 5 can still be loaded without
    # migration; the checksum is always verified against the fields actually
    # present in the file before the dataclass defaults are applied.
    name: str = ""
    parent_session_id: str = ""
    archived: bool = False
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


_ATOMIC_REPLACE_RETRY_SECONDS = 1.5
_ATOMIC_REPLACE_RETRY_DELAY = 0.05


def _replace_with_retry(source: Path, destination: Path) -> None:
    """Atomically replace a session document, tolerating short Windows locks.

    Antivirus/indexing and a concurrently exiting reader can transiently hold the
    destination open on Windows and make ``os.replace`` raise ``PermissionError``
    / WinError 5. Session mutations already hold KaroX's cross-process state lock,
    so this retry is not masking a competing writer; it only bridges an external
    short-lived filesystem lock. The deadline is bounded so a persistent ACL or
    ownership problem is still surfaced promptly.
    """
    deadline = time.monotonic() + _ATOMIC_REPLACE_RETRY_SECONDS
    while True:
        try:
            os.replace(source, destination)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_ATOMIC_REPLACE_RETRY_DELAY)


def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp, path)
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

            # A byte range already locked by THIS process fails immediately
            # with EDEADLK instead of waiting: the CRT cannot wait on a lock
            # its own process holds. The lease heartbeat thread and the
            # calling thread legitimately serialize on the same state lock,
            # so wait out the sibling's critical section and retry, bounded
            # like LK_LOCK's own ten-second budget for cross-process waits.
            deadline = time.monotonic() + 10.0
            while True:
                try:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
                    break
                except OSError as exc:
                    if exc.errno != 36 or time.monotonic() >= deadline:
                        raise
                    time.sleep(0.01)
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
        *,
        name: str = "",
        parent_session_id: str = "",
    ) -> SessionRecord:
        if not isinstance(task, str) or not task.strip():
            raise SessionError("session task must be a non-empty string")
        repo = repository.expanduser().resolve(strict=True)
        if not repo.is_dir():
            raise SessionError(f"repository is not a directory: {repo}")
        sid = self._validate_id(session_id or uuid.uuid4().hex)
        clean_name = _clean_session_name(name)
        parent = self._validate_id(parent_session_id) if parent_session_id else ""
        if parent == sid:
            raise SessionError("a session cannot be its own parent")
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
                name=clean_name,
                parent_session_id=parent,
            )
            payload = record.to_dict()
            _atomic_json(target, payload)
            record.checksum = payload["checksum"]
            return record

    def reactivate(
        self,
        session_id: str,
        repository: Path,
        access_profile: AccessProfile = AccessProfile.WORKSPACE_WRITE,
    ) -> SessionRecord:
        """Reactivate a revoked/existing session for a persistent saved bridge profile."""
        sid = self._validate_id(session_id)
        target = self.state_path(sid)
        repo = repository.expanduser().resolve(strict=True)
        if not repo.is_dir():
            raise SessionError(f"repository is not a directory: {repo}")
        with _exclusive_file_lock(self.lock_path(sid)):
            if not target.exists():
                return self.create(repo, "reactivated persistent session", access_profile, session_id=sid)
            record = self.load(sid)
            record.repository = str(repo)
            record.repo_fingerprint = repository_fingerprint(repo)
            record.access_profile = access_profile.value
            record.revoked = False
            record.archived = False
            record.status = "active"
            payload = record.to_dict()
            payload["repository"] = str(repo)
            payload["repo_fingerprint"] = repository_fingerprint(repo)
            payload["access_profile"] = access_profile.value
            payload["revoked"] = False
            payload["archived"] = False
            payload["status"] = "active"
            payload["revision"] = record.revision + 1
            payload["updated_at"] = time.time()
            payload["checksum"] = _checksum(payload)
            _atomic_json(target, payload)
            return SessionRecord.from_dict(payload)

    def load(self, session_id: str) -> SessionRecord:
        return SessionRecord.from_dict(_read_json(self.state_path(session_id)))

    def list(self, *, include_archived: bool = True) -> List[SessionRecord]:
        records: List[SessionRecord] = []
        for state in sorted(self.root.glob("*/session.json")):
            try:
                record = SessionRecord.from_dict(_read_json(state))
            except SessionError:
                continue
            if record.archived and not include_archived:
                continue
            records.append(record)
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

    def _ensure_no_active_lease_unlocked(self, session_id: str) -> None:
        """Refuse lifecycle changes while another owner still holds the session."""

        path = self.lease_path(session_id)
        if not path.exists():
            return
        try:
            current = _read_json(path)
            expires_at = float(current.get("expires_at", 0))
        except (SessionError, TypeError, ValueError):
            # A malformed lease cannot prove ownership. Remove it only while the
            # session state lock is held, so a valid owner cannot race this path.
            expires_at = 0.0
        if expires_at >= time.time():
            raise SessionBusy(
                f"session {session_id} is locked by {current.get('owner', 'unknown')}"
            )
        with contextlib.suppress(FileNotFoundError):
            path.unlink()

    def _metadata_update_unlocked(
        self, record: SessionRecord, **changes: Any
    ) -> SessionRecord:
        payload = record.to_dict()
        payload.update(changes)
        payload["revision"] = record.revision + 1
        payload["updated_at"] = time.time()
        payload["checksum"] = _checksum(payload)
        _atomic_json(self.state_path(record.session_id), payload)
        return SessionRecord.from_dict(payload)

    def rename(self, session_id: str, name: str) -> SessionRecord:
        """Set or clear the short user-facing name of one durable session."""

        sid = self._validate_id(session_id)
        clean_name = _clean_session_name(name)
        with _exclusive_file_lock(self.lock_path(sid)):
            record = self.load(sid)
            return self._metadata_update_unlocked(record, name=clean_name)

    def archive(self, session_id: str) -> SessionRecord:
        """Hide a session from normal resume lists without deleting evidence."""

        sid = self._validate_id(session_id)
        with _exclusive_file_lock(self.lock_path(sid)):
            self._ensure_no_active_lease_unlocked(sid)
            record = self.load(sid)
            if record.archived:
                return record
            return self._metadata_update_unlocked(record, archived=True)

    def unarchive(self, session_id: str) -> SessionRecord:
        sid = self._validate_id(session_id)
        with _exclusive_file_lock(self.lock_path(sid)):
            record = self.load(sid)
            if not record.archived:
                return record
            return self._metadata_update_unlocked(record, archived=False)

    def continue_task(self, session_id: str, task: str) -> SessionRecord:
        """Append a new user turn to an existing durable session atomically."""

        if not isinstance(task, str) or not task.strip():
            raise SessionError("continuation task must be a non-empty string")
        safe_task = str(redact(task)).strip()
        sid = self._validate_id(session_id)
        with self.mutate(
            sid, f"session-continue-{os.getpid()}", ttl_seconds=10.0
        ) as record:
            if record.task != safe_task:
                record.task_history.append(record.task)
                record.task = safe_task
            if record.provider_history:
                last = record.provider_history[-1]
                duplicate = (
                    isinstance(last, dict)
                    and last.get("role") == "user"
                    and last.get("content") == safe_task
                )
                if not duplicate:
                    record.provider_history.append(
                        {
                            "role": "user",
                            "content": safe_task,
                            "kind": "continuation",
                        }
                    )
            record.status = "active"
            record.phase = "execution" if record.provider_history else "planning"
        return self.load(sid)

    def fork(
        self,
        session_id: str,
        *,
        session_id_new: Optional[str] = None,
        task: Optional[str] = None,
        name: str = "",
    ) -> SessionRecord:
        """Create an active child session carrying only safe structured context.

        Lineage/context is copied; ownership and side-effect state is not. In
        particular leases, idempotency reservations, live jobs, connected
        clients, usage/cost counters, and provider attempt history start empty.
        """

        source_id = self._validate_id(session_id)
        source = self.load(source_id)
        if source.revoked:
            raise SessionError(f"cannot fork a revoked session: {source_id}")
        repository = Path(source.repository).expanduser().resolve(strict=True)
        forked = self.create(
            repository,
            task if task is not None else source.task,
            AccessProfile(source.access_profile),
            branch=source.branch,
            session_id=session_id_new,
            name=name,
            parent_session_id=source_id,
        )
        lease = self.acquire(
            forked.session_id,
            f"session-fork-{os.getpid()}",
            ttl_seconds=10.0,
        )
        try:
            child = self.load(forked.session_id)
            child.phase = source.phase
            child.task_history = [*source.task_history, source.task]
            child.continuation_context = _fork_context(source)
            child.plan = copy.deepcopy(source.plan)
            child.summary = source.summary
            child.decisions = copy.deepcopy(source.decisions)
            child.changed_files = list(source.changed_files)
            child.git_state = copy.deepcopy(source.git_state)
            child.checkpoints = copy.deepcopy(source.checkpoints)
            child.checks = copy.deepcopy(source.checks)
            child.failures = copy.deepcopy(source.failures)
            child.skills = copy.deepcopy(source.skills)
            child.mcp_servers = copy.deepcopy(source.mcp_servers)
            child.packs = copy.deepcopy(source.packs)
            child.evidence = copy.deepcopy(source.evidence)
            child.unfinished_actions = copy.deepcopy(source.unfinished_actions)
            self.save(child, child.revision, lease)
            return child
        finally:
            self.release(lease)

    def delete_archived(self, session_id: str) -> None:
        """Permanently delete an archived session after fencing future mutation."""

        sid = self._validate_id(session_id)
        directory = self.session_dir(sid)
        with _exclusive_file_lock(self.lock_path(sid)):
            self._ensure_no_active_lease_unlocked(sid)
            record = self.load(sid)
            if not record.archived:
                raise SessionError("archive the session before deleting it")
            # Fence any process that obtained a stale record before this lock.
            self._metadata_update_unlocked(record, revoked=True, status="deleted_pending")
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise SessionError(f"cannot delete session {sid}: {exc}") from exc

    def acquire(
        self, session_id: str, owner: str, ttl_seconds: float = 30.0
    ) -> MutationLease:
        if ttl_seconds < 5 or ttl_seconds > 3600:
            raise ValueError("lease TTL must be between 5 and 3600 seconds")
        with _exclusive_file_lock(self.lock_path(session_id)):
            record = self.load(session_id)
            if record.revoked:
                raise SessionError(f"session has been revoked: {session_id}")
            if record.archived:
                raise SessionError(f"session is archived: {session_id}")
            path = self.lease_path(session_id)
            now = time.time()
            if path.exists():
                current = _read_json(path)
                unexpired = float(current.get("expires_at", 0)) >= now
                if unexpired and not _lease_owner_is_provably_dead(current):
                    raise SessionBusy(
                        f"session {session_id} is locked by {current.get('owner', 'unknown')}"
                    )
                # Expired leases are always stale. An unexpired lease is stale
                # only when its same-host owner PID is provably gone (for
                # example after the durable MCP child was recycled).
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
