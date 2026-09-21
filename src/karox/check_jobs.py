"""Durable, bridge-independent managed jobs for long verification commands.

A hosted MCP request must never remain open for a multi-minute test suite.  The
bridge therefore starts a small detached worker and returns a job id immediately.
The worker owns the verification child, its Windows Job Object/process group,
redacted log and checksum-protected state.  Closing, cancelling, or interrupting
the MCP request cannot close the worker's process tree.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from .artifacts import ArtifactStore
from .command_guard import DeveloperCommandBlocked, validate_developer_command_argv
from .core import ProcessTree, VerificationRule
from .evidence_packets import ArtifactRef, checks_job_packet
from .paths import runtime_dir
from .process_identity import (
    ProcessIdentity,
    ProcessIdentityVerdict,
    capture_process_identity,
    verify_process_identity,
)
from .process_launcher import resolve_process_argv
from .repository_lease import RepositoryLease, RepositoryLeaseConflict, RepositoryLeaseStore
from .security import child_process_environment, contains_credential, redact

_SCHEMA_VERSION = 1
_JOB_SCOPE_SCHEMA_VERSION = 1
_JOB_ID = re.compile(r"job-[0-9a-f]{20}")
_JOB_SCOPE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_MAX_LOG_BYTES = 24 * 1024 * 1024
_DEFAULT_LOG_TAIL = 64 * 1024
_MAX_LOG_TAIL = 1024 * 1024
_FINAL_STATUSES = frozenset({"passed", "failed", "cancelled", "timed_out"})
_ALL_STATUSES = frozenset({"queued", "running", *_FINAL_STATUSES})
_LOCK_TIMEOUT_SECONDS = 5.0
_LOCK_STALE_SECONDS = 30.0
_WORKER_READY_TIMEOUT_SECONDS = 15.0
_WORKER_EXIT_STATE_GRACE_SECONDS = 2.0
_ATOMIC_REPLACE_TIMEOUT_SECONDS = 5.0
_ATOMIC_REPLACE_RETRY_SECONDS = 0.025
_LOG_TRIM_TO_BYTES = 16 * 1024 * 1024
_DEV_REPOSITORY_LEASE_TTL_SECONDS = 90.0
_DEV_REPOSITORY_LEASE_HEARTBEAT_SECONDS = 20.0
_DEV_REPOSITORY_LEASE_RETRY_SECONDS = 0.25


class CheckJobError(RuntimeError):
    """A managed verification job request or state is unsafe/invalid."""


@dataclass(frozen=True)
class CheckJobState:
    schema_version: int
    job_id: str
    session_id: str
    repository: str
    argv: tuple[str, ...]
    command_sha256: str
    idempotency_sha256: str
    status: str
    queued_at: float
    started_at: Optional[float]
    updated_at: float
    finished_at: Optional[float]
    timeout_seconds: float
    bridge_pid: int
    bridge_identity: Optional[ProcessIdentity]
    worker_identity: Optional[ProcessIdentity]
    child_identity: Optional[ProcessIdentity]
    process_group: str
    cancellation_source: Optional[str]
    signal_sent: Optional[str]
    exit_code: Optional[int]
    error_code: Optional[str]
    error: Optional[str]
    log_path: str
    artifact_id: Optional[str]
    log_truncated: bool

    def __post_init__(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise CheckJobError("unsupported check-job schema")
        if not _JOB_ID.fullmatch(self.job_id):
            raise CheckJobError("job_id is malformed")
        if not self.session_id or not self.repository or not self.argv:
            raise CheckJobError("check-job identity is incomplete")
        if self.status not in _ALL_STATUSES:
            raise CheckJobError("check-job status is invalid")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise CheckJobError("check-job timeout is invalid")
        if self.bridge_pid <= 0:
            raise CheckJobError("bridge pid is invalid")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "session_id": self.session_id,
            "repository": self.repository,
            "argv": list(self.argv),
            "command_sha256": self.command_sha256,
            "idempotency_sha256": self.idempotency_sha256,
            "status": self.status,
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "finished_at": self.finished_at,
            "timeout_seconds": self.timeout_seconds,
            "bridge_pid": self.bridge_pid,
            "bridge_identity": (
                self.bridge_identity.to_dict() if self.bridge_identity is not None else None
            ),
            "worker_identity": (
                self.worker_identity.to_dict() if self.worker_identity is not None else None
            ),
            "child_identity": (
                self.child_identity.to_dict() if self.child_identity is not None else None
            ),
            "process_group": self.process_group,
            "cancellation_source": self.cancellation_source,
            "signal_sent": self.signal_sent,
            "exit_code": self.exit_code,
            "error_code": self.error_code,
            "error": self.error,
            "log_path": self.log_path,
            "artifact_id": self.artifact_id,
            "log_truncated": self.log_truncated,
        }

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "CheckJobState":
        try:
            return cls(
                schema_version=int(payload["schema_version"]),
                job_id=str(payload["job_id"]),
                session_id=str(payload["session_id"]),
                repository=str(payload["repository"]),
                argv=tuple(str(item) for item in payload["argv"]),
                command_sha256=str(payload["command_sha256"]),
                idempotency_sha256=str(payload["idempotency_sha256"]),
                status=str(payload["status"]),
                queued_at=float(payload["queued_at"]),
                started_at=(
                    None if payload.get("started_at") is None else float(payload["started_at"])
                ),
                updated_at=float(payload["updated_at"]),
                finished_at=(
                    None if payload.get("finished_at") is None else float(payload["finished_at"])
                ),
                timeout_seconds=float(payload["timeout_seconds"]),
                bridge_pid=int(payload["bridge_pid"]),
                bridge_identity=(
                    None
                    if payload.get("bridge_identity") is None
                    else ProcessIdentity.from_dict(payload["bridge_identity"])
                ),
                worker_identity=(
                    None
                    if payload.get("worker_identity") is None
                    else ProcessIdentity.from_dict(payload["worker_identity"])
                ),
                child_identity=(
                    None
                    if payload.get("child_identity") is None
                    else ProcessIdentity.from_dict(payload["child_identity"])
                ),
                process_group=str(payload.get("process_group") or "unknown"),
                cancellation_source=(
                    None
                    if payload.get("cancellation_source") is None
                    else str(payload["cancellation_source"])
                ),
                signal_sent=(
                    None if payload.get("signal_sent") is None else str(payload["signal_sent"])
                ),
                exit_code=(
                    None if payload.get("exit_code") is None else int(payload["exit_code"])
                ),
                error_code=(
                    None if payload.get("error_code") is None else str(payload["error_code"])
                ),
                error=None if payload.get("error") is None else str(payload["error"]),
                log_path=str(payload["log_path"]),
                artifact_id=(
                    None if payload.get("artifact_id") is None else str(payload["artifact_id"])
                ),
                log_truncated=bool(payload.get("log_truncated", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckJobError("check-job state is malformed") from exc


def _checksum(payload: Mapping[str, Any]) -> str:
    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _command_fingerprint(argv: Sequence[str]) -> str:
    body = json.dumps(list(argv), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _idempotency_fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _workspace_state_fingerprint(repository: Path) -> str:
    """Return a cheap freshness token for check-job idempotency.

    This is intentionally not a cryptographic content digest. The goal is to
    distinguish a network retry from a deliberate re-run after the workspace
    changed, without hashing large untracked trees on every ``checks.start``.
    Git repositories use HEAD + porcelain status + file metadata for dirty and
    untracked paths. Non-Git repositories fall back to bounded recursive file
    metadata while skipping common generated/cache directories.
    """
    root = repository.expanduser().resolve(strict=True)

    def _git(*argv: str) -> Optional[subprocess.CompletedProcess[str]]:
        try:
            return subprocess.run(
                ["git", "-C", str(root), *argv],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdin=subprocess.DEVNULL,
                creationflags=(
                    getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
                ),
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError):
            return None

    head_result = _git("rev-parse", "--verify", "HEAD")
    status_result = _git(
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "-z",
    )
    ignored = {
        ".git",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "node_modules",
    }
    if status_result is not None and status_result.returncode == 0:
        head = (
            (head_result.stdout or "").strip()
            if head_result is not None and head_result.returncode == 0
            else "unborn"
        )
        raw_status = status_result.stdout or ""
        filtered_status: list[str] = []
        metadata: list[tuple[str, int, int, bool]] = []
        for entry in raw_status.split("\0"):
            if len(entry) < 4:
                continue
            raw_path = entry[3:]
            if " -> " in raw_path:
                raw_path = raw_path.split(" -> ", 1)[1]
            path_parts = Path(raw_path).parts
            if any(part in ignored for part in path_parts):
                # Test/lint/cache output must not turn an idempotent transport
                # retry into a new verification job. Source/config changes still
                # change the fingerprint immediately.
                continue
            filtered_status.append(entry)
            path = root / raw_path
            try:
                info = path.stat()
                metadata.append(
                    (raw_path, int(info.st_size), int(info.st_mtime_ns), path.is_dir())
                )
            except OSError:
                metadata.append((raw_path, -1, -1, False))
        payload: dict[str, Any] = {
            "mode": "git",
            "head": head,
            "status": filtered_status,
            "metadata": metadata,
        }
    else:
        metadata = []
        truncated = False
        for path in sorted(root.rglob("*")):
            relative = path.relative_to(root)
            if any(part in ignored for part in relative.parts):
                continue
            if len(metadata) >= 5000:
                truncated = True
                break
            try:
                info = path.stat()
                metadata.append(
                    (
                        relative.as_posix(),
                        int(info.st_size),
                        int(info.st_mtime_ns),
                        path.is_dir(),
                    )
                )
            except OSError:
                metadata.append((relative.as_posix(), -1, -1, False))
        payload = {"mode": "filesystem", "metadata": metadata, "truncated": truncated}

    body = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            process_query_limited_information = 0x1000
            still_active = 259
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.restype = wintypes.HANDLE
            handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
            if not handle:
                return False
            try:
                code = wintypes.DWORD()
                return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and int(
                    code.value
                ) == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def effective_job_status(
    state: CheckJobState,
    *,
    pid_alive: Callable[[int], bool] = _pid_alive,
) -> tuple[str, Optional[str]]:
    """Return the liveness-aware status used by continuation/read surfaces.

    Job state is durable across bridge loss, so a worker can disappear before it
    writes its terminal state. Treating the persisted ``running`` string as truth
    then leaves task.resume/status blocked forever on a process that no longer
    exists. This mirrors ``CheckJobManager.public_status`` without mutating the
    journal: callers can safely exclude a proven-dead worker from active jobs.
    """

    if state.status in {"queued", "running"} and state.worker_identity is not None:
        worker = verify_process_identity(state.worker_identity, pid_alive=pid_alive)
        return _status_from_worker(state, worker)
    return state.status, state.error_code


def _status_from_worker(
    state: CheckJobState, worker: ProcessIdentityVerdict
) -> tuple[str, Optional[str]]:
    if (
        state.status in {"queued", "running"}
        and state.worker_identity is not None
        and not worker.alive
    ):
        return "failed", "worker_exited_without_final_state"
    return state.status, state.error_code


def _retryable_atomic_replace_error(exc: OSError) -> bool:
    """Return whether Windows may clear this replace failure after a short retry."""

    if os.name != "nt":
        return False
    # ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION and ERROR_LOCK_VIOLATION.
    # Antivirus/indexers and a concurrent reader can briefly hold the destination
    # without delete sharing even though the temporary file is complete.
    return getattr(exc, "winerror", None) in {5, 32, 33}


def _atomic_replace(source: str | os.PathLike[str], destination: str | os.PathLike[str]) -> None:
    deadline = time.monotonic() + _ATOMIC_REPLACE_TIMEOUT_SECONDS
    while True:
        try:
            os.replace(source, destination)
            return
        except OSError as exc:
            if not _retryable_atomic_replace_error(exc) or time.monotonic() >= deadline:
                raise
            time.sleep(_ATOMIC_REPLACE_RETRY_SECONDS)


class _FileLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: Optional[int] = None

    def __enter__(self) -> "_FileLock":
        deadline = time.monotonic() + _LOCK_TIMEOUT_SECONDS
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.write(self.fd, f"{os.getpid()}\n".encode("ascii"))
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0.0
                if age > _LOCK_STALE_SECONDS:
                    try:
                        self.path.unlink()
                    except OSError:
                        pass
                    continue
                if time.monotonic() >= deadline:
                    raise CheckJobError("check-job store is busy")
                time.sleep(0.05)

    def __exit__(self, *_args: Any) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        try:
            self.path.unlink()
        except OSError:
            pass


class CheckJobStore:
    def __init__(self, session_id: str, *, root: Optional[Path] = None) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise CheckJobError("check-job store requires a session id")
        self.session_id = session_id
        base = Path(root) if root is not None else runtime_dir() / "vnext" / "check-jobs"
        self.root = (base / session_id).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock_path = self.root / ".store.lock"

    def state_path(self, job_id: str) -> Path:
        if not _JOB_ID.fullmatch(job_id):
            raise CheckJobError("job_id is malformed")
        return self.root / f"{job_id}.json"

    def cancel_path(self, job_id: str) -> Path:
        return self.root / f"{job_id}.cancel"

    def index_path(self, idempotency_sha256: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{64}", idempotency_sha256):
            raise CheckJobError("idempotency digest is malformed")
        directory = self.root / "idempotency"
        directory.mkdir(parents=True, exist_ok=True)
        return directory / f"{idempotency_sha256}.json"

    @staticmethod
    def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            _atomic_replace(temporary, path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def scope_path(self, job_id: str) -> Path:
        if not _JOB_ID.fullmatch(job_id):
            raise CheckJobError("job_id is malformed")
        return self.root / "scopes" / f"{job_id}.json"

    def put_scope(
        self,
        job_id: str,
        *,
        workstream_id: str,
        project_id: Optional[str] = None,
    ) -> None:
        """Persist secret-free logical ownership for accidental cross-lane isolation.

        The session remains the security boundary. This sidecar prevents sibling
        agents in one durable session from accidentally surfacing/cancelling each
        other's jobs while keeping the job-state schema backward compatible.
        """

        self.state_path(job_id)  # validates the durable job id
        if not isinstance(workstream_id, str) or not _JOB_SCOPE_ID.fullmatch(workstream_id):
            raise CheckJobError("job workstream scope is invalid")
        if project_id is not None and (
            not isinstance(project_id, str) or not _JOB_SCOPE_ID.fullmatch(project_id)
        ):
            raise CheckJobError("job project scope is invalid")
        payload: dict[str, Any] = {
            "schema_version": _JOB_SCOPE_SCHEMA_VERSION,
            "job_id": job_id,
            "session_id": self.session_id,
            "workstream_id": workstream_id,
            "project_id": project_id,
        }
        self._atomic_json(
            self.scope_path(job_id),
            {**payload, "checksum": _checksum(payload)},
        )

    def get_scope(self, job_id: str) -> Optional[dict[str, Any]]:
        path = self.scope_path(job_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckJobError("managed job scope is unreadable") from exc
        if not isinstance(raw, dict):
            raise CheckJobError("managed job scope is malformed")
        checksum = raw.pop("checksum", None)
        if not isinstance(checksum, str) or checksum != _checksum(raw):
            raise CheckJobError("managed job scope checksum mismatch")
        if raw.get("schema_version") != _JOB_SCOPE_SCHEMA_VERSION:
            raise CheckJobError("managed job scope schema is unsupported")
        if raw.get("job_id") != job_id or raw.get("session_id") != self.session_id:
            raise CheckJobError("managed job scope identity mismatch")
        workstream_id = raw.get("workstream_id")
        project_id = raw.get("project_id")
        if not isinstance(workstream_id, str) or not _JOB_SCOPE_ID.fullmatch(workstream_id):
            raise CheckJobError("managed job workstream scope is invalid")
        if project_id is not None and (
            not isinstance(project_id, str) or not _JOB_SCOPE_ID.fullmatch(project_id)
        ):
            raise CheckJobError("managed job project scope is invalid")
        return {
            "workstream_id": workstream_id,
            "project_id": project_id,
        }

    def put(self, state: CheckJobState) -> None:
        payload = state.to_payload()
        self._atomic_json(self.state_path(state.job_id), {**payload, "checksum": _checksum(payload)})

    def get(self, job_id: str) -> CheckJobState:
        path = self.state_path(job_id)
        raw: Any = None
        for attempt in range(3):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                break
            except FileNotFoundError as exc:
                raise CheckJobError("managed check job does not exist") from exc
            except (OSError, json.JSONDecodeError) as exc:
                if attempt < 2:
                    time.sleep(_ATOMIC_REPLACE_RETRY_SECONDS)
                    continue
                raise CheckJobError("managed check-job state is unreadable") from exc
        if not isinstance(raw, dict):
            raise CheckJobError("managed check-job state is malformed")
        checksum = raw.pop("checksum", None)
        if not isinstance(checksum, str) or checksum != _checksum(raw):
            raise CheckJobError("managed check-job checksum mismatch")
        return CheckJobState.from_payload(raw)

    def bind_idempotency(
        self,
        state: CheckJobState,
        *,
        workspace_sha256: str,
    ) -> None:
        self._atomic_json(
            self.index_path(state.idempotency_sha256),
            {
                "job_id": state.job_id,
                "command_sha256": state.command_sha256,
                "workspace_sha256": workspace_sha256,
            },
        )

    def lookup_idempotency(
        self, digest: str
    ) -> Optional[tuple[str, str, Optional[str]]]:
        try:
            payload = json.loads(self.index_path(digest).read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise CheckJobError("check-job idempotency index is unreadable") from exc
        if not isinstance(payload, dict):
            raise CheckJobError("check-job idempotency index is malformed")
        job_id = payload.get("job_id")
        command = payload.get("command_sha256")
        workspace = payload.get("workspace_sha256")
        if not isinstance(job_id, str) or not isinstance(command, str):
            raise CheckJobError("check-job idempotency index is malformed")
        if workspace is not None and (
            not isinstance(workspace, str) or re.fullmatch(r"[0-9a-f]{64}", workspace) is None
        ):
            raise CheckJobError("check-job idempotency index is malformed")
        return job_id, command, workspace

    def request_cancel(self, job_id: str) -> None:
        self._atomic_json(
            self.cancel_path(job_id),
            {"job_id": job_id, "session_id": self.root.name, "requested_at": time.time()},
        )


WorkerLauncher = Callable[[Path], subprocess.Popen[Any]]


def _worker_creationflags(breakaway: bool = True) -> int:
    if os.name != "nt":
        return 0
    flags = int(
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )
    if breakaway:
        flags |= int(getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0x01000000))
    return flags


def _child_spawn_kwargs(breakaway: bool = True) -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": _worker_creationflags(breakaway=breakaway)}
    return {"start_new_session": True}


def _popen_job_object_tolerant(**popen_kwargs: Any) -> subprocess.Popen[Any]:
    """Spawn a child, falling back without breakaway on restrictive job objects.

    Hosted runners refuse ``CREATE_BREAKAWAY_FROM_JOB`` at spawn time with
    ``PermissionError [WinError 5]``. The bit only changes process-tree teardown,
    which the worker tree caller owns, so a refused breakaway transparently
    retries the launch with the bit removed instead of failing the job.
    """
    try:
        return subprocess.Popen(**popen_kwargs)
    except PermissionError as exc:
        flags = popen_kwargs.get("creationflags")
        if (
            os.name != "nt"
            or not isinstance(flags, int)
            or not flags & 0x01000000
        ):
            raise
        # A job object can also add the bit itself; removing just our flag keeps
        # the rest of the spawn contract (new group, no console window).
        retry_kwargs = dict(popen_kwargs)
        retry_kwargs["creationflags"] = flags & ~0x01000000
        try:
            return subprocess.Popen(**retry_kwargs)
        except OSError:
            raise exc


def _worker_environment() -> dict[str, str]:
    environment = child_process_environment()
    module_path = Path(__file__).resolve()
    package_parent = module_path.parents[1]
    # A source-tree bridge must launch the worker from the same checkout. An
    # installed wheel has no ``src`` parent, so wheel acceptance remains free of
    # the repository and resolves the package from isolated site-packages.
    if package_parent.name == "src":
        environment["PYTHONPATH"] = str(package_parent)
    for name in (
        "KAROX_VNEXT_RUNTIME_DIR",
        "KAROX_RUNTIME_DIR",
        "KAROX_VNEXT_CONFIG_DIR",
        "KAROX_CONFIG_DIR",
    ):
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return environment


def _default_worker_launcher(state_path: Path) -> subprocess.Popen[Any]:
    argv = [sys.executable, "-m", "karox.check_jobs", "--worker", str(state_path)]
    return _popen_job_object_tolerant(
        args=argv,
        cwd=state_path.parent,
        env=_worker_environment(),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        creationflags=_worker_creationflags(),
        start_new_session=(os.name != "nt"),
    )


def developer_worker_launcher(state_path: Path) -> subprocess.Popen[Any]:
    """Launch a durable Full-developer worker with the bridge user environment."""
    argv = [sys.executable, "-m", "karox.check_jobs", "--worker", str(state_path)]
    environment = os.environ.copy()
    environment["PYTHONIOENCODING"] = "utf-8"
    environment["PYTHONUTF8"] = "1"
    package_parent = Path(__file__).resolve().parents[1]
    if package_parent.name == "src":
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = str(package_parent) + (
            os.pathsep + existing if existing else ""
        )
    # Both durable worker paths use the same restrictive-job fallback as their
    # guarded children: only optional breakaway may be dropped, never the
    # process-group or no-window contract.
    return _popen_job_object_tolerant(
        args=argv,
        cwd=state_path.parent,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        shell=False,
        creationflags=_worker_creationflags(),
        start_new_session=(os.name != "nt"),
    )


def _test_files(repository: Path) -> list[str]:
    return [
        path.relative_to(repository).as_posix()
        for path in sorted((repository / "tests").glob("test_*.py"))
        if path.is_file()
    ]


def _validated_targets(repository: Path, raw: Any) -> list[str]:
    if not isinstance(raw, list) or not raw or len(raw) > 200:
        raise CheckJobError("focused tests require 1-200 targets")
    result: list[str] = []
    root = repository.resolve(strict=True)
    for item in raw:
        if not isinstance(item, str) or not item or "\x00" in item:
            raise CheckJobError("test targets must be non-empty strings")
        path_text, separator, node = item.partition("::")
        try:
            candidate = (root / path_text).resolve(strict=True)
            relative = candidate.relative_to(root).as_posix()
        except FileNotFoundError as exc:
            raise CheckJobError("test target does not exist") from exc
        except (OSError, ValueError) as exc:
            raise CheckJobError("test target escapes repository") from exc
        if not relative.startswith("tests/") or not relative.endswith(".py"):
            raise CheckJobError("test target must be a Python file under tests/")
        normalized = relative + (separator + node if separator else "")
        if normalized not in result:
            result.append(normalized)
    return result


def build_job_argv(
    repository: Path,
    arguments: Mapping[str, Any],
    verification_commands: Iterable[Iterable[str]],
    *,
    allow_developer_commands: bool = False,
) -> tuple[str, ...]:
    kind = arguments.get("kind", "pytest")
    if kind == "dev":
        if not allow_developer_commands:
            raise CheckJobError("developer commands are not enabled for this job manager")
        raw = arguments.get("argv")
        if not isinstance(raw, list):
            raise CheckJobError("developer argv must be an array")
        try:
            checked = validate_developer_command_argv(raw)
        except DeveloperCommandBlocked as exc:
            raise CheckJobError(str(exc)) from exc
        if contains_credential(" ".join(checked)):
            raise CheckJobError(
                "credential-shaped argv cannot be persisted in a durable command job; "
                "use environment, keyring, or file references"
            )
        return checked
    if kind == "check":
        raw = arguments.get("argv")
        if not isinstance(raw, list) or not raw or not all(
            isinstance(item, str) and item and "\x00" not in item for item in raw
        ):
            raise CheckJobError("check argv must contain non-empty strings")
        checked_argv = tuple(raw)
        rules = tuple(VerificationRule.parse(item) for item in verification_commands)
        if not rules or not any(rule.matches(checked_argv) for rule in rules):
            raise CheckJobError("check command is not in the verification allowlist")
        return checked_argv
    if kind != "pytest":
        raise CheckJobError("kind must be pytest, check, or dev")
    if arguments.get("argv") is not None:
        raise CheckJobError("pytest jobs do not accept argv")
    suite = arguments.get("suite", "full")
    if suite not in {"full", "focused", "split"}:
        raise CheckJobError("pytest suite must be full, focused, or split")
    argv = [sys.executable, "-m", "pytest"]
    if suite == "focused":
        if arguments.get("split") is not None or arguments.get("part") is not None:
            raise CheckJobError("focused suite does not accept split or part")
        argv.extend(_validated_targets(repository, arguments.get("targets")))
    elif suite == "split":
        if arguments.get("targets") not in (None, []):
            raise CheckJobError("split suite does not accept targets")
        split = arguments.get("split")
        part = arguments.get("part")
        if isinstance(split, bool) or not isinstance(split, int) or not 2 <= split <= 32:
            raise CheckJobError("split must be an integer between 2 and 32")
        if isinstance(part, bool) or not isinstance(part, int) or not 1 <= part <= split:
            raise CheckJobError("part must be an integer between 1 and split")
        selected = [
            item for index, item in enumerate(_test_files(repository)) if index % split == part - 1
        ]
        if not selected:
            raise CheckJobError("selected test split is empty")
        argv.extend(selected)
    elif arguments.get("targets") not in (None, []) or arguments.get("split") is not None or arguments.get("part") is not None:
        raise CheckJobError("full suite does not accept targets, split, or part")
    return tuple(argv)


class CheckJobManager:
    def __init__(
        self,
        repository: Path,
        session_id: str,
        verification_commands: Iterable[Iterable[str]] = (),
        *,
        root: Optional[Path] = None,
        worker_launcher: WorkerLauncher = _default_worker_launcher,
        pid_alive: Callable[[int], bool] = _pid_alive,
        allow_developer_commands: bool = False,
        workspace_sensitive: bool = True,
        wait_for_child: bool = True,
    ) -> None:
        self.repository = repository.expanduser().resolve(strict=True)
        self.session_id = session_id
        self.verification_commands = tuple(tuple(item) for item in verification_commands)
        self.store = CheckJobStore(session_id, root=root)
        self.worker_launcher = worker_launcher
        self.pid_alive = pid_alive
        self.allow_developer_commands = bool(allow_developer_commands)
        self.workspace_sensitive = bool(workspace_sensitive)
        self.wait_for_child = bool(wait_for_child)
        self._thread_lock = threading.RLock()

    def start(
        self,
        arguments: Mapping[str, Any],
        *,
        idempotency_key: str,
        bridge_pid: Optional[int] = None,
    ) -> dict[str, Any]:
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise CheckJobError("checks.start requires an idempotency key")
        timeout = arguments.get("timeout_seconds", 3600.0)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise CheckJobError("timeout_seconds must be numeric")
        timeout_value = float(timeout)
        if not math.isfinite(timeout_value) or not 1 <= timeout_value <= 86400:
            raise CheckJobError("timeout_seconds must be between 1 and 86400")
        argv = build_job_argv(
            self.repository,
            arguments,
            self.verification_commands,
            allow_developer_commands=self.allow_developer_commands,
        )
        command_sha256 = _command_fingerprint(argv)
        workspace_sha256 = (
            _workspace_state_fingerprint(self.repository)
            if self.workspace_sensitive
            else hashlib.sha256(str(self.repository).encode("utf-8")).hexdigest()
        )
        idempotency_sha256 = _idempotency_fingerprint(idempotency_key)
        now = time.time()
        owner_pid = int(bridge_pid or os.getpid())
        with self._thread_lock, _FileLock(self.store.lock_path):
            existing = self.store.lookup_idempotency(idempotency_sha256)
            if existing is not None:
                job_id, existing_command, existing_workspace = existing
                if existing_command != command_sha256:
                    raise CheckJobError("idempotency key was reused for a different command")
                if existing_workspace == workspace_sha256:
                    return {
                        **self.public_status(self.store.get(job_id)),
                        "idempotent_replay": True,
                    }
            job_id = f"job-{hashlib.sha256((idempotency_key + command_sha256 + workspace_sha256).encode('utf-8')).hexdigest()[:20]}"
            # A workspace can return to an earlier fingerprint (A -> B -> A).
            # The index points only to B; reusing A's deterministic id would
            # overwrite its durable state/log and share its cancellation marker
            # with a second worker. Allocate a new generation under the lock.
            while self.store.state_path(job_id).exists():
                job_id = f"job-{os.urandom(10).hex()}"
            log_path = self.store.root / f"{job_id}.log"
            state = CheckJobState(
                schema_version=_SCHEMA_VERSION,
                job_id=job_id,
                session_id=self.session_id,
                repository=str(self.repository),
                argv=argv,
                command_sha256=command_sha256,
                idempotency_sha256=idempotency_sha256,
                status="queued",
                queued_at=now,
                started_at=None,
                updated_at=now,
                finished_at=None,
                timeout_seconds=timeout_value,
                bridge_pid=owner_pid,
                bridge_identity=capture_process_identity(owner_pid),
                worker_identity=None,
                child_identity=None,
                process_group=("windows-breakaway-worker" if os.name == "nt" else "posix-session-worker"),
                cancellation_source=None,
                signal_sent=None,
                exit_code=None,
                error_code=None,
                error=None,
                log_path=str(log_path),
                artifact_id=None,
                log_truncated=False,
            )
            self.store.put(state)
            self.store.bind_idempotency(
                state,
                workspace_sha256=workspace_sha256,
            )
            try:
                worker_process = self.worker_launcher(self.store.state_path(job_id))
            except Exception as exc:
                failed = replace(
                    state,
                    status="failed",
                    updated_at=time.time(),
                    finished_at=time.time(),
                    error_code="worker_launch_failed",
                    error=f"{type(exc).__name__}: {redact(str(exc))}",
                )
                self.store.put(failed)
                raise CheckJobError(
                    "managed check worker could not start: "
                    f"{type(exc).__name__}: {redact(str(exc))}"
                ) from exc
        if not self.wait_for_child:
            return {**self.public_status(state), "idempotent_replay": False}
        deadline = time.monotonic() + _WORKER_READY_TIMEOUT_SECONDS
        observed = state
        while time.monotonic() < deadline:
            observed = self.store.get(job_id)
            if observed.child_identity is not None or observed.status in _FINAL_STATUSES:
                break
            worker_exit = worker_process.poll()
            if worker_exit is not None:
                # Worker exit and its final atomic state publication are separate
                # scheduler events. Under full-suite CPU/disk pressure the state
                # replacement can become visible after the process handle reports
                # exit, so reconcile for a bounded grace window before declaring
                # an incomplete bootstrap.
                reconcile_deadline = time.monotonic() + _WORKER_EXIT_STATE_GRACE_SECONDS
                while time.monotonic() < reconcile_deadline:
                    observed = self.store.get(job_id)
                    if observed.status in _FINAL_STATUSES:
                        break
                    time.sleep(0.05)
                if observed.status not in _FINAL_STATUSES:
                    failed = replace(
                        observed,
                        status="failed",
                        updated_at=time.time(),
                        finished_at=time.time(),
                        cancellation_source="worker_exit",
                        exit_code=int(worker_exit),
                        error_code="worker_exited_before_child_identity",
                        error=(
                            "managed check worker exited before publishing child "
                            f"ownership (exit code {int(worker_exit)})"
                        ),
                    )
                    self.store.put(failed)
                    observed = failed
                break
            time.sleep(0.05)
        if observed.child_identity is None and observed.status not in _FINAL_STATUSES:
            self.store.request_cancel(job_id)
            failed = replace(
                observed,
                status="failed",
                updated_at=time.time(),
                finished_at=time.time(),
                cancellation_source="worker_start_timeout",
                error_code="worker_child_identity_timeout",
                error=(
                    "managed check worker did not publish child ownership within "
                    f"{_WORKER_READY_TIMEOUT_SECONDS:.0f} seconds"
                ),
            )
            self.store.put(failed)
            observed = failed
        return {**self.public_status(observed), "idempotent_replay": False}

    def _identity_verdict(self, identity: Optional[ProcessIdentity]) -> ProcessIdentityVerdict:
        return verify_process_identity(identity, pid_alive=self.pid_alive)

    def public_status(self, state: CheckJobState) -> dict[str, Any]:
        now = time.time()
        worker = self._identity_verdict(state.worker_identity)
        child = self._identity_verdict(state.child_identity)
        # Reuse this poll's verdict: a second probe can observe worker exit and
        # contradict the ownership details returned in the same response.
        effective_status, diagnostic = _status_from_worker(state, worker)
        log = read_job_log(Path(state.log_path), limit=_DEFAULT_LOG_TAIL)
        summary = summarize_log(log["text"])
        duration_seconds = round(
            max(0.0, (state.finished_at or now) - (state.started_at or state.queued_at)), 3
        )
        evidence_packet: Optional[dict[str, Any]] = None
        if effective_status in _FINAL_STATUSES:
            packet = checks_job_packet(
                status=effective_status,
                exit_code=state.exit_code,
                summary=summary["summary"],
                first_failure=summary["first_failure"],
                duration_seconds=duration_seconds,
                artifact=(
                    ArtifactRef(
                        artifact_id=state.artifact_id,
                        raw_bytes=int(log["bytes"]) or None,
                    )
                    if isinstance(state.artifact_id, str) and state.artifact_id
                    else None
                ),
                error_code=diagnostic,
            )
            evidence_packet = json.loads(packet.render())
        return {
            "job_id": state.job_id,
            "status": effective_status,
            "child_pid": state.child_identity.pid if state.child_identity is not None else None,
            "worker_pid": state.worker_identity.pid if state.worker_identity is not None else None,
            "bridge_pid": state.bridge_pid,
            "command_fingerprint": f"sha256:{state.command_sha256[:16]}",
            "started_at": state.started_at,
            "duration_seconds": duration_seconds,
            "timeout_budget": state.timeout_seconds,
            "exit_code": state.exit_code,
            "error_code": diagnostic,
            "error": str(redact(state.error))[:1000] if state.error else None,
            "artifact_id": state.artifact_id,
            "summary": summary["summary"],
            "first_failure": summary["first_failure"],
            "progress": summary["progress"],
            "evidence_packet": evidence_packet,
            "ownership": {
                "session_id": state.session_id,
                "worker": worker.to_dict(),
                "child": child.to_dict(),
            },
            "diagnostics": {
                "request_deadline": "detached_from_mcp_request",
                "command_deadline": state.timeout_seconds,
                "process_group": state.process_group,
                "cancellation_source": state.cancellation_source,
                "signal_sent": state.signal_sent,
                "error_code": diagnostic,
                "log_truncated": state.log_truncated or log["truncated"],
            },
        }

    def status(self, job_id: str) -> dict[str, Any]:
        return self.public_status(self.store.get(job_id))

    def logs(self, job_id: str, *, limit: int = _DEFAULT_LOG_TAIL) -> dict[str, Any]:
        state = self.store.get(job_id)
        bounded = max(1, min(int(limit), _MAX_LOG_TAIL))
        log = read_job_log(Path(state.log_path), limit=bounded)
        return {
            "job_id": job_id,
            "status": effective_job_status(state, pid_alive=self.pid_alive)[0],
            "log": log,
            "artifact_id": state.artifact_id,
        }

    def cancel(self, job_id: str) -> dict[str, Any]:
        state = self.store.get(job_id)
        if state.status in _FINAL_STATUSES:
            return {**self.public_status(state), "cancel_requested": False, "idempotent": True}
        verdict = self._identity_verdict(state.worker_identity)
        if state.worker_identity is not None and verdict.alive and not verdict.proven:
            raise CheckJobError("worker identity is unproven; refusing to signal a PID")
        self.store.request_cancel(job_id)
        return {**self.public_status(state), "cancel_requested": True, "idempotent": False}

    def cancel_active(self) -> dict[str, Any]:
        """Request cancellation for every non-final job in this manager's store.

        This is intentionally marker-only: the detached worker remains the sole
        owner allowed to signal its child tree. It is used when a durable bridge
        revokes the capability that originally authorized developer jobs.
        """
        requested: list[str] = []
        unreadable: list[str] = []
        for path in sorted(self.store.root.glob("job-*.json")):
            job_id = path.stem
            try:
                state = self.store.get(job_id)
            except CheckJobError:
                # A cancel marker does not signal a PID. Writing one for a
                # syntactically valid job id is safe even when its state cannot
                # be trusted, and is the fail-closed choice during revocation.
                self.store.request_cancel(job_id)
                requested.append(job_id)
                unreadable.append(job_id)
                continue
            if state.status in _FINAL_STATUSES:
                continue
            self.store.request_cancel(job_id)
            requested.append(job_id)
        return {"cancel_requested": requested, "unreadable_state": unreadable}


def read_job_log(path: Path, *, limit: int) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            # Measure the opened file, not a path that may have been replaced
            # or trimmed between stat and open.
            total = os.fstat(handle.fileno()).st_size
            if total > limit:
                handle.seek(total - limit)
            raw = handle.read(limit)
    except FileNotFoundError:
        total = 0
        raw = b""
    except OSError as exc:
        raise CheckJobError("managed check log is unreadable") from exc
    text = str(redact(raw.decode("utf-8", errors="replace")))
    return {
        "text": text,
        "bytes": total,
        "truncated": total > limit,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def summarize_log(text: str) -> dict[str, Any]:
    first_failure: Optional[str] = None
    progress: Optional[int] = None
    last_summary: Optional[str] = None
    for line in text.splitlines():
        stripped = line.strip()
        match = re.search(r"\[\s*(\d{1,3})%\]", stripped)
        if match:
            progress = max(0, min(100, int(match.group(1))))
        if first_failure is None and (
            stripped.startswith("FAILED ")
            or stripped.startswith("ERROR ")
            or stripped.startswith("E   ")
        ):
            first_failure = stripped[:500]
        if re.search(r"\b(passed|failed|skipped|errors?|xfailed)\b", stripped, re.IGNORECASE):
            last_summary = stripped[:500]
    return {
        "summary": last_summary,
        "first_failure": first_failure,
        "progress": progress,
    }


def _write_capped_log(handle: Any, data: bytes) -> bool:
    if not data:
        return False
    handle.seek(0, os.SEEK_END)
    current = int(handle.tell())
    if current + len(data) <= _MAX_LOG_BYTES:
        handle.write(data)
        handle.flush()
        return False
    retained_budget = max(0, _LOG_TRIM_TO_BYTES - min(len(data), _LOG_TRIM_TO_BYTES))
    retained = b""
    if retained_budget:
        handle.seek(max(0, current - retained_budget))
        retained = handle.read(retained_budget)
    payload = (retained + data)[-_MAX_LOG_BYTES:]
    handle.seek(0)
    handle.truncate(0)
    handle.write(payload)
    handle.flush()
    return True


def _append_redacted(
    stream: Any,
    output: Path,
    stop: threading.Event,
    truncated: list[bool],
) -> None:
    # The process is terminated before this reader is joined, so EOF is the only
    # safe completion signal. A cancellation flag could discard the final pytest
    # summary still buffered in the pipe.
    del stop
    pending = ""
    mode = "r+b" if output.exists() else "w+b"
    with output.open(mode) as handle:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                break
            pending += chunk
            lines = pending.splitlines(keepends=True)
            if lines and not lines[-1].endswith(("\n", "\r")):
                pending = lines.pop()
            else:
                pending = ""
            # Keep the redaction boundaries unchanged. Only batch writes that
            # fit without eviction: trimming depends on each individual write's
            # size, so a chunk that could cross the cap must use the old order.
            redacted = [str(redact(line)).encode("utf-8", errors="replace") for line in lines]
            if len(pending) > 8192:
                flush, pending = pending[:-512], pending[-512:]
                redacted.append(str(redact(flush)).encode("utf-8", errors="replace"))
            if redacted:
                data = b"".join(redacted)
                handle.seek(0, os.SEEK_END)
                if handle.tell() + len(data) <= _MAX_LOG_BYTES:
                    truncated[0] |= _write_capped_log(handle, data)
                else:
                    for part in redacted:
                        truncated[0] |= _write_capped_log(handle, part)
        if pending:
            truncated[0] |= _write_capped_log(
                handle, str(redact(pending)).encode("utf-8", errors="replace")
            )


def _persist_artifact(state: CheckJobState, store: CheckJobStore) -> CheckJobState:
    path = Path(state.log_path)
    try:
        raw = path.read_bytes()
    except OSError:
        return state
    truncated = len(raw) > _MAX_LOG_BYTES
    if truncated:
        raw = raw[-_MAX_LOG_BYTES:]
    try:
        record = ArtifactStore(state.session_id).put(
            raw,
            name=f"{state.job_id}-output.txt",
            mime="text/plain; charset=utf-8",
        )
    except Exception:
        return replace(state, log_truncated=state.log_truncated or truncated)
    return replace(
        state,
        artifact_id=record.artifact_id,
        log_truncated=state.log_truncated or truncated,
    )


def run_worker(state_path: Path) -> int:
    state_path = state_path.expanduser().resolve(strict=True)
    session_id = state_path.parent.name
    store = CheckJobStore(session_id, root=state_path.parent.parent)
    state = store.get(state_path.stem)
    developer_job = state_path.parent.parent.name == "dev-command-jobs"
    stop_reader = threading.Event()
    log_truncated = [state.log_truncated]
    tree: Optional[ProcessTree] = None
    reader: Optional[threading.Thread] = None
    process: Optional[subprocess.Popen[Any]] = None
    repository_lease_store: Optional[RepositoryLeaseStore] = None
    repository_lease: Optional[RepositoryLease] = None
    repository_lease_heartbeat_at = 0.0
    try:
        worker_argv = [
            sys.executable,
            "-m",
            "karox.check_jobs",
            "--worker",
            str(state_path),
        ]
        worker_identity = capture_process_identity(
            os.getpid(), executable=sys.executable, argv=worker_argv
        )
        state = replace(
            state,
            status="running",
            started_at=time.time(),
            updated_at=time.time(),
            worker_identity=worker_identity,
        )
        store.put(state)
        deadline = time.monotonic() + state.timeout_seconds
        if developer_job:
            repository = Path(state.repository).expanduser().resolve(strict=True)
            repository_lease_store = RepositoryLeaseStore()
            while True:
                if store.cancel_path(state.job_id).exists():
                    cancelled = replace(
                        state,
                        status="cancelled",
                        updated_at=time.time(),
                        finished_at=time.time(),
                        cancellation_source="worker_start_cancelled",
                    )
                    store.put(cancelled)
                    return 0
                try:
                    repository_lease, _recovered = repository_lease_store.acquire(
                        repository,
                        session_id=state.session_id,
                        task_id=state.job_id,
                        connection_id="durable-command-job",
                        current_operation="durable_developer_command",
                        ttl_seconds=_DEV_REPOSITORY_LEASE_TTL_SECONDS,
                    )
                    repository_lease_heartbeat_at = time.monotonic()
                    break
                except RepositoryLeaseConflict:
                    if time.monotonic() >= deadline:
                        failed = replace(
                            state,
                            status="failed",
                            updated_at=time.time(),
                            finished_at=time.time(),
                            cancellation_source="repository_busy",
                            error_code="repository_busy",
                            error="repository mutation lease remained busy until the command deadline",
                        )
                        store.put(failed)
                        return 1
                    time.sleep(_DEV_REPOSITORY_LEASE_RETRY_SECONDS)
        log_path = Path(state.log_path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if store.cancel_path(state.job_id).exists():
            cancelled = replace(
                state,
                status="cancelled",
                updated_at=time.time(),
                finished_at=time.time(),
                cancellation_source="worker_start_cancelled",
            )
            store.put(cancelled)
            return 0
        resolved_argv = resolve_process_argv(state.argv)
        child_environment = (
            os.environ.copy() if developer_job else child_process_environment()
        )
        child_environment["PYTHONIOENCODING"] = "utf-8"
        child_environment["PYTHONUTF8"] = "1"
        process = _popen_job_object_tolerant(
            args=resolved_argv,
            cwd=state.repository,
            env=child_environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
            **_child_spawn_kwargs(),
        )
        tree = ProcessTree(process)
        child_identity = capture_process_identity(
            process.pid, executable=resolved_argv[0], argv=state.argv
        )
        state = replace(
            state,
            child_identity=child_identity,
            updated_at=time.time(),
            process_group=(
                "windows-new-process-group+breakaway+no-window+job-object"
                if os.name == "nt"
                else "posix-new-session"
            ),
        )
        store.put(state)
        assert process.stdout is not None
        reader = threading.Thread(
            target=_append_redacted,
            args=(process.stdout, log_path, stop_reader, log_truncated),
            name=f"karox-check-log-{state.job_id}",
            daemon=True,
        )
        reader.start()
        final_status = "failed"
        cancellation_source: Optional[str] = None
        signal_sent: Optional[str] = None
        exit_code: Optional[int] = None
        while True:
            code = process.poll()
            if code is not None:
                exit_code = int(code)
                final_status = "passed" if code == 0 else "failed"
                break
            if store.cancel_path(state.job_id).exists():
                cancellation_source = "command.cancel" if developer_job else "checks.cancel"
                signal_sent = "terminate_owned_child_job"
                tree.terminate()
                exit_code = process.poll()
                final_status = "cancelled"
                break
            if (
                repository_lease_store is not None
                and repository_lease is not None
                and time.monotonic() - repository_lease_heartbeat_at
                >= _DEV_REPOSITORY_LEASE_HEARTBEAT_SECONDS
            ):
                repository_lease = repository_lease_store.heartbeat(
                    Path(state.repository),
                    repository_lease,
                    current_operation="durable_developer_command",
                    ttl_seconds=_DEV_REPOSITORY_LEASE_TTL_SECONDS,
                )
                repository_lease_heartbeat_at = time.monotonic()
            if time.monotonic() >= deadline:
                cancellation_source = "command_timeout"
                signal_sent = "terminate_owned_child_job"
                tree.terminate()
                exit_code = process.poll()
                final_status = "timed_out"
                break
            time.sleep(0.1)
        stop_reader.set()
        if reader is not None:
            reader.join(timeout=5.0)
        if process.stdout is not None:
            process.stdout.close()
        finished = replace(
            state,
            status=final_status,
            updated_at=time.time(),
            finished_at=time.time(),
            cancellation_source=cancellation_source,
            signal_sent=signal_sent,
            exit_code=exit_code,
            log_truncated=log_truncated[0],
        )
        finished = _persist_artifact(finished, store)
        store.put(finished)
        return 0
    except BaseException as exc:
        if tree is not None:
            tree.terminate()
        stop_reader.set()
        if reader is not None:
            reader.join(timeout=2.0)
        if process is not None and process.stdout is not None:
            process.stdout.close()
        failed = replace(
            state,
            status="failed",
            updated_at=time.time(),
            finished_at=time.time(),
            cancellation_source="worker_exception",
            signal_sent="terminate_owned_child_job" if tree is not None else None,
            error_code="worker_exception",
            error=f"{type(exc).__name__}: {redact(str(exc))}",
            log_truncated=log_truncated[0],
        )
        failed = _persist_artifact(failed, store)
        store.put(failed)
        return 1
    finally:
        if repository_lease_store is not None and repository_lease is not None:
            try:
                repository_lease_store.release(Path(state.repository), repository_lease)
            except Exception:
                # The worker is exiting, so strict process identity makes any
                # unreleased lease recoverable by the next owner. Never turn a
                # completed command into a second failure during cleanup.
                pass
        if tree is not None:
            tree.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    values = list(sys.argv[1:] if argv is None else argv)
    if len(values) == 2 and values[0] == "--worker":
        return run_worker(Path(values[1]))
    raise SystemExit("check_jobs is an internal worker entry point")


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "CheckJobError",
    "CheckJobManager",
    "CheckJobState",
    "CheckJobStore",
    "build_job_argv",
    "developer_worker_launcher",
    "read_job_log",
    "run_worker",
    "summarize_log",
]
