"""Durable background commands for hosted clients.

A web chat's tool call has a deadline of roughly a minute, and its tunnel can
drop at any moment. A test suite, a build or a benchmark that runs longer than
that cannot be a synchronous ``dev.command``/``checks.run``: the call times out,
the result is lost, and the model retries a command that may still be running.

A job is started and returned at once; the caller then polls ``status`` and
reads ``logs`` incrementally, and may ``cancel``. Each job runs under a small
*runner* process started detached from the bridge, so restarting the bridge or
losing the connection does not kill it. The runner owns the command's whole
process tree (a Windows job object, a POSIX process group), enforces the
timeout, rotates the log and records the outcome in the job directory:

    <runtime>/command-jobs/<repository digest>/<job id>/
        job.json      what was asked (argv redacted), written once at start
        spawn.json    the runner's PID and creation time, written by the bridge
        state.json    "running" plus the command's PID, written by the runner
        result.json   the outcome, written once by whoever finished the job
        output.log    combined stdout/stderr; output.1.log is the previous part

The exact argv and the environment reach the runner through its stdin and its
inherited environment, never through a file, so a token passed on a command
line or present in the environment is not persisted. Log text is redacted when
it is read, like every other command output KaroX returns.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from .atomic_io import atomic_write
from .core_types import InvalidCommand
from .detached_process import detached_flags
from .process_identity import process_is_running, read_process_create_time_ns
from .security import redact

JOB_SCHEMA = 1
DEFAULT_TIMEOUT_SECONDS = 1800.0
MAX_TIMEOUT_SECONDS = 24 * 3600.0
DEFAULT_LOG_LIMIT = 64 * 1024
MAX_LOG_LIMIT = 1024 * 1024
LOG_ROTATE_BYTES = 8 * 1024 * 1024
MAX_FINISHED_JOBS_KEPT = 50
_POLL_SECONDS = 0.25
_START_GRACE_SECONDS = 15.0
_JOB_KINDS = ("dev", "check")
_DONE = frozenset({"passed", "failed", "timed_out", "cancelled", "failed_to_start", "lost"})

_RUNNER_SHIM = (
    "import sys\n"
    "sys.path.insert(0, sys.argv[1])\n"
    "from karox.command_jobs import run_job\n"
    "raise SystemExit(run_job(sys.argv[2]))\n"
)
_PACKAGE_PARENT = str(Path(__file__).resolve().parent.parent)


class CommandJobError(InvalidCommand):
    """A job request or lookup the caller can fix; the message is caller-safe."""


def _now() -> float:
    return time.time()


def _read_json(path: Path) -> Optional[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    atomic_write(path, json.dumps(dict(value), ensure_ascii=False, sort_keys=True).encode("utf-8"))


def _write_once(path: Path, value: Mapping[str, Any]) -> bool:
    """Create ``path`` only if it does not exist yet: the first outcome wins."""
    payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True).encode("utf-8")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_bytes(payload)
    try:
        os.link(temporary, path)
    except FileExistsError:
        return False
    except OSError:
        # Filesystems without hard links: fall back to an exclusive create.
        try:
            with open(path, "xb") as handle:
                handle.write(payload)
        except FileExistsError:
            return False
    finally:
        try:
            temporary.unlink()
        except OSError:
            pass
    return True


@dataclass(frozen=True)
class _JobPaths:
    directory: Path

    @property
    def spec(self) -> Path:
        return self.directory / "job.json"

    @property
    def spawn(self) -> Path:
        return self.directory / "spawn.json"

    @property
    def state(self) -> Path:
        return self.directory / "state.json"

    @property
    def result(self) -> Path:
        return self.directory / "result.json"

    @property
    def log(self) -> Path:
        return self.directory / "output.log"

    @property
    def previous_log(self) -> Path:
        return self.directory / "output.1.log"

    @property
    def rotated(self) -> Path:
        return self.directory / "rotated_bytes"

    @property
    def cancel(self) -> Path:
        return self.directory / "cancel.request"


def _rotated_bytes(paths: _JobPaths) -> int:
    try:
        return max(0, int(paths.rotated.read_text(encoding="ascii").strip() or 0))
    except (OSError, ValueError):
        return 0


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _alive(pid: Any, create_time_ns: Any) -> bool:
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    if not process_is_running(pid):
        return False
    if isinstance(create_time_ns, int) and create_time_ns > 0:
        current = read_process_create_time_ns(pid)
        if current is not None and current != create_time_ns:
            return False  # the PID was reused by an unrelated process
    return True


def job_id_for(kind: str, request_id: str) -> str:
    digest = hashlib.sha256(f"{kind}\0{request_id}".encode("utf-8")).hexdigest()[:20]
    return f"{kind}-{digest}"


def _valid_request_id(value: Any) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 128
        or not value[0].isalnum()
        or not all(char.isalnum() or char in "._-" for char in value)
    ):
        raise CommandJobError("request_id must be 1-128 characters of [A-Za-z0-9._-]")
    return value


def job_timeout(value: Any) -> float:
    if value is None:
        return DEFAULT_TIMEOUT_SECONDS
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CommandJobError("timeout_seconds must be a number")
    timeout = float(value)
    if not 1.0 <= timeout <= MAX_TIMEOUT_SECONDS:
        raise CommandJobError(f"timeout_seconds must be between 1 and {int(MAX_TIMEOUT_SECONDS)}")
    return timeout


def repository_job_root(base: Path, repository: Path) -> Path:
    digest = hashlib.sha256(str(repository).encode("utf-8")).hexdigest()[:16]
    return base / "command-jobs" / digest


# Runners this process started. Holding them lets ``_reap`` collect their exit
# status, so a long-lived bridge does not accumulate zombies on POSIX.
_SPAWNED: list[subprocess.Popen[bytes]] = []
_SPAWNED_LOCK = threading.Lock()


def _reap() -> None:
    with _SPAWNED_LOCK:
        _SPAWNED[:] = [item for item in _SPAWNED if item.poll() is None]


def _spawn_runner(
    directory: Path, environment: Mapping[str, str], handoff: bytes
) -> tuple[Optional[subprocess.Popen[bytes]], str]:
    argv = [sys.executable, "-I", "-X", "utf8", "-c", _RUNNER_SHIM, _PACKAGE_PARENT, str(directory)]
    attempts = [("breakaway", detached_flags())]
    fallback = detached_flags(breakaway=False)
    if fallback != attempts[0][1]:
        attempts.append(("in_caller_job", fallback))
    failures: list[str] = []
    for name, flags in attempts:
        try:
            process = subprocess.Popen(  # noqa: S603 - fixed interpreter + shim
                argv,
                cwd=str(directory),
                env=dict(environment),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
                start_new_session=(os.name != "nt"),
            )
        except OSError as exc:
            failures.append(f"{name}:{getattr(exc, 'winerror', None) or type(exc).__name__}")
            continue
        try:
            assert process.stdin is not None
            process.stdin.write(handoff)
            process.stdin.close()
        except OSError:
            pass
        with _SPAWNED_LOCK:
            _SPAWNED.append(process)
        return process, name if not failures else f"{name} after {','.join(failures)}"
    return None, "spawn_failed:" + ",".join(failures)


class CommandJobStore:
    """Jobs of one repository. Every method is safe to call from any process."""

    def __init__(self, root: Path) -> None:
        self.root = root

    # ------------------------------------------------------------------ start

    def start(
        self,
        *,
        kind: str,
        request_id: str,
        argv: Sequence[str],
        launch_argv: Sequence[str],
        cwd: Path,
        timeout_seconds: float,
        environment: Mapping[str, str],
    ) -> dict[str, Any]:
        if kind not in _JOB_KINDS:
            raise CommandJobError(f"unknown job kind: {kind}")
        request_id = _valid_request_id(request_id)
        job_id = job_id_for(kind, request_id)
        paths = _JobPaths(self.root / job_id)
        if paths.spec.exists():
            payload = self.status(job_id, kind=kind)
            payload["replayed"] = True
            return payload
        self.root.mkdir(parents=True, exist_ok=True)
        paths.directory.mkdir(exist_ok=True)
        created = _now()
        _write_json(
            paths.spec,
            {
                "schema": JOB_SCHEMA,
                "job_id": job_id,
                "kind": kind,
                "request_id": request_id,
                "argv": [str(item) for item in redact(list(argv))],
                "timeout_seconds": float(timeout_seconds),
                "created_at": created,
            },
        )
        handoff = json.dumps(
            {"argv": list(launch_argv), "cwd": str(cwd), "timeout_seconds": float(timeout_seconds)},
            ensure_ascii=False,
        ).encode("utf-8")
        process, mechanism = _spawn_runner(paths.directory, environment, handoff)
        if process is None:
            _write_once(
                paths.result,
                {"status": "failed_to_start", "error": mechanism, "ended_at": _now()},
            )
        else:
            _write_json(
                paths.spawn,
                {
                    "runner_pid": process.pid,
                    "runner_create_time_ns": read_process_create_time_ns(process.pid),
                    "mechanism": mechanism,
                    "spawned_at": _now(),
                },
            )
        self._prune(keep=job_id)
        return self.status(job_id, kind=kind)

    # ----------------------------------------------------------------- status

    def _paths(self, job_id: Any, kind: Optional[str]) -> tuple[_JobPaths, dict[str, Any]]:
        if not isinstance(job_id, str) or not job_id or "/" in job_id or "\\" in job_id or ".." in job_id:
            raise CommandJobError("job_id is required")
        paths = _JobPaths(self.root / job_id)
        spec = _read_json(paths.spec)
        if spec is None or (kind is not None and spec.get("kind") != kind):
            raise CommandJobError(f"unknown job: {job_id}")
        return paths, spec

    def status(self, job_id: str, *, kind: Optional[str] = None) -> dict[str, Any]:
        _reap()
        paths, spec = self._paths(job_id, kind)
        spawn = _read_json(paths.spawn) or {}
        state = _read_json(paths.state) or {}
        result = _read_json(paths.result)
        now = _now()
        created = float(spec.get("created_at") or now)
        child_running: Optional[bool] = None
        if result is not None:
            status = str(result.get("status") or "failed")
        elif _alive(spawn.get("runner_pid"), spawn.get("runner_create_time_ns")):
            status = "running" if state else "starting"
        elif not spawn and now - created < _START_GRACE_SECONDS:
            status = "starting"
        else:
            # The runner records the outcome just before it exits: read again,
            # so a job that finished between the two reads is not "lost".
            result = _read_json(paths.result)
            if result is not None:
                status = str(result.get("status") or "failed")
            else:
                status = "lost"
                child_running = _alive(state.get("child_pid"), state.get("child_create_time_ns"))
        started = state.get("started_at") or spawn.get("spawned_at")
        ended = result.get("ended_at") if result else None
        payload: dict[str, Any] = {
            "job_id": spec.get("job_id", job_id),
            "kind": spec.get("kind"),
            "request_id": spec.get("request_id"),
            "argv": spec.get("argv"),
            "status": status,
            "done": status in _DONE,
            "exit_code": result.get("exit_code") if result else None,
            "timeout_seconds": spec.get("timeout_seconds"),
            "created_at": created,
            "started_at": started,
            "ended_at": ended,
            "elapsed_seconds": round(float((ended or now) - float(started or created)), 2),
            "log_bytes": _rotated_bytes(paths) + _size(paths.log),
            "cancel_requested": paths.cancel.exists(),
        }
        if result and result.get("error"):
            payload["error"] = str(redact(str(result["error"])))[:500]
        if status == "lost":
            payload["child_running"] = bool(child_running)
            payload["hint"] = (
                "the job's runner is gone without recording an outcome (the machine "
                "restarted or the runner was killed); read its logs and start it again "
                "with a new request_id"
                + ("; the command itself is still running, cancel the job to stop it" if child_running else "")
            )
        return payload

    def list(self, *, kind: Optional[str] = None, limit: int = 20) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        entries = []
        for directory in self.root.iterdir():
            spec = _read_json(directory / "job.json")
            if spec is None or (kind is not None and spec.get("kind") != kind):
                continue
            entries.append((float(spec.get("created_at") or 0.0), directory.name))
        entries.sort(reverse=True)
        jobs = []
        for _created, job_id in entries[: max(1, min(int(limit), 100))]:
            try:
                item = self.status(job_id, kind=kind)
            except CommandJobError:
                continue
            jobs.append({key: item[key] for key in ("job_id", "request_id", "argv", "status", "exit_code", "elapsed_seconds")})
        return jobs

    # ------------------------------------------------------------------- logs

    def logs(
        self,
        job_id: str,
        *,
        kind: Optional[str] = None,
        limit: Any = DEFAULT_LOG_LIMIT,
        offset: Any = None,
    ) -> dict[str, Any]:
        from .core_process import _decode_captured_bytes

        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LOG_LIMIT:
            raise CommandJobError(f"limit must be an integer between 1 and {MAX_LOG_LIMIT}")
        if offset is not None and (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0):
            raise CommandJobError("offset must be a non-negative integer")
        paths, _spec = self._paths(job_id, kind)
        status = self.status(job_id, kind=kind)
        # Read the rotation counter around the sizes: a rotation in between is
        # visible as a changed counter, and the read is simply retried.
        for _attempt in range(3):
            rotated = _rotated_bytes(paths)
            current = _size(paths.log)
            previous = _size(paths.previous_log)
            if _rotated_bytes(paths) == rotated:
                break
        total = rotated + current
        start = max(0, total - limit) if offset is None else min(offset, total)
        end = min(total, start + limit)
        chunks: list[bytes] = []
        unavailable = 0
        if start < rotated:
            previous_start = rotated - previous
            if start < previous_start:
                unavailable = previous_start - start
                start = previous_start
            if previous and start < rotated:
                with open(paths.previous_log, "rb") as handle:
                    handle.seek(start - previous_start)
                    chunks.append(handle.read(min(end, rotated) - start))
        if end > rotated:
            with open(paths.log, "rb") as handle:
                handle.seek(max(0, start - rotated))
                chunks.append(handle.read(end - max(start, rotated)))
        raw = b"".join(chunks)
        text, _consumed = _decode_captured_bytes(raw, mid_stream_start=start > 0)
        return {
            **status,
            "text": str(redact(text)),
            "offset": start,
            "next_offset": start + len(raw),
            "bytes_total": total,
            "truncated": start > 0 or start + len(raw) < total,
            "unavailable_bytes": unavailable,
        }

    # ----------------------------------------------------------------- cancel

    def cancel(self, job_id: str, *, kind: Optional[str] = None, wait_seconds: float = 5.0) -> dict[str, Any]:
        paths, _spec = self._paths(job_id, kind)
        current = self.status(job_id, kind=kind)
        if current["done"] and current["status"] != "lost":
            return {**current, "cancelled": False, "already_finished": True}
        if current["status"] != "lost":
            # The runner polls for this file, before it starts the command and
            # while the command runs, and records the outcome itself.
            paths.cancel.write_text(str(_now()), encoding="ascii")
            deadline = time.monotonic() + max(0.0, wait_seconds)
            while time.monotonic() < deadline:
                current = self.status(job_id, kind=kind)
                if current["done"]:
                    break
                time.sleep(_POLL_SECONDS)
        if current["status"] == "lost":
            # Nobody is left to record an outcome: stop the command the runner
            # left behind -- only a process whose creation time proves it is
            # the same one -- and record the cancellation here.
            state = _read_json(paths.state) or {}
            if _alive(state.get("child_pid"), state.get("child_create_time_ns")):
                _terminate_tree(int(state["child_pid"]))
            _write_once(
                paths.result,
                {
                    "status": "cancelled",
                    "exit_code": None,
                    "ended_at": _now(),
                    "error": "cancelled after the job's runner was lost",
                },
            )
            current = self.status(job_id, kind=kind)
        return {**current, "cancelled": current["status"] == "cancelled"}

    # ------------------------------------------------------------------ prune

    def _prune(self, *, keep: str) -> None:
        if not self.root.is_dir():
            return
        finished = []
        for directory in self.root.iterdir():
            if directory.name == keep or not directory.is_dir():
                continue
            if not (directory / "result.json").exists():
                try:
                    if not self.status(directory.name)["done"]:
                        continue  # still running: never pruned
                except CommandJobError:
                    pass  # a directory without a readable job.json
            spec = _read_json(directory / "job.json") or {}
            finished.append((float(spec.get("created_at") or 0.0), directory))
        finished.sort(reverse=True)
        for _created, directory in finished[MAX_FINISHED_JOBS_KEPT:]:
            shutil.rmtree(directory, ignore_errors=True)


def _terminate_tree(pid: int) -> None:
    from .core_process import _kill_posix_process_group, _kill_windows_process_tree

    if os.name == "nt":
        _kill_windows_process_tree(pid)
    else:
        _kill_posix_process_group(pid)


# ---------------------------------------------------------------------- runner


class _RotatingLog:
    def __init__(self, paths: _JobPaths) -> None:
        self._paths = paths
        self._handle = open(paths.log, "ab")
        self._size = _size(paths.log)
        self._rotated = _rotated_bytes(paths)

    def write(self, chunk: bytes) -> None:
        if self._size and self._size + len(chunk) > LOG_ROTATE_BYTES:
            self._rotate()
        self._handle.write(chunk)
        self._handle.flush()
        self._size += len(chunk)

    def _rotate(self) -> None:
        self._handle.close()
        try:
            os.replace(self._paths.log, self._paths.previous_log)
        except OSError:
            # A reader holds the file open (Windows sharing rules): keep
            # appending and try again on a later chunk.
            self._handle = open(self._paths.log, "ab")
            return
        self._rotated += self._size
        atomic_write(self._paths.rotated, str(self._rotated).encode("ascii"))
        self._handle = open(self._paths.log, "wb")
        self._size = 0

    def close(self) -> None:
        try:
            self._handle.close()
        except OSError:
            pass


def run_job(directory: str) -> int:
    """The detached runner: execute one job and record its outcome."""
    from .core_process import ProcessTree, _new_process_group_kwargs

    paths = _JobPaths(Path(directory))
    try:
        handoff = json.loads(sys.stdin.buffer.read().decode("utf-8"))
        argv = [str(item) for item in handoff["argv"]]
        cwd = str(handoff["cwd"])
        timeout = float(handoff["timeout_seconds"])
    except Exception as exc:  # noqa: BLE001 - recorded, never raised
        _write_once(paths.result, {"status": "failed_to_start", "error": f"bad handoff ({type(exc).__name__})", "ended_at": _now()})
        return 2
    finally:
        try:
            sys.stdin.close()
        except OSError:
            pass
    if paths.cancel.exists():
        _write_once(paths.result, {"status": "cancelled", "exit_code": None, "ended_at": _now()})
        return 0
    started = _now()
    log = _RotatingLog(paths)
    try:
        process = subprocess.Popen(  # noqa: S603 - argv validated by the bridge
            argv,
            cwd=cwd,
            env=os.environ.copy(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            shell=False,
            **_new_process_group_kwargs(),
        )
    except OSError as exc:
        log.write(f"KaroX: could not start the command: {type(exc).__name__}: {exc.strerror or exc}\n".encode("utf-8"))
        log.close()
        _write_once(paths.result, {"status": "failed_to_start", "error": f"{type(exc).__name__}: {exc.strerror or exc}", "ended_at": _now(), "exit_code": None})
        return 1
    tree = ProcessTree(process)
    _write_json(
        paths.state,
        {
            "status": "running",
            "started_at": started,
            "child_pid": process.pid,
            "child_create_time_ns": read_process_create_time_ns(process.pid),
            "runner_pid": os.getpid(),
        },
    )

    def pump() -> None:
        assert process.stdout is not None
        reader = getattr(process.stdout, "read1", process.stdout.read)
        while True:
            try:
                chunk = reader(65536)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            try:
                log.write(chunk)
            except OSError:
                return

    thread = threading.Thread(target=pump, name="karox-job-log", daemon=True)
    thread.start()
    outcome: Optional[str] = None
    deadline = started + timeout
    try:
        while process.poll() is None:
            if paths.cancel.exists():
                outcome = "cancelled"
                tree.terminate()
                break
            if _now() > deadline:
                outcome = "timed_out"
                tree.terminate()
                break
            time.sleep(_POLL_SECONDS)
    finally:
        thread.join(timeout=5.0)
        tree.close()
    exit_code = process.poll()
    if outcome is None:
        outcome = "passed" if exit_code == 0 else "failed"
    if outcome == "timed_out":
        log.write(f"\nKaroX: stopped after the {timeout:g} s timeout\n".encode("utf-8"))
    elif outcome == "cancelled":
        log.write(b"\nKaroX: cancelled on request\n")
    log.close()
    _write_once(
        paths.result,
        {
            "status": outcome,
            "exit_code": exit_code,
            "ended_at": _now(),
            "duration_seconds": round(_now() - started, 2),
        },
    )
    return 0


__all__ = [
    "CommandJobError",
    "CommandJobStore",
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "job_id_for",
    "job_timeout",
    "repository_job_root",
    "run_job",
]
