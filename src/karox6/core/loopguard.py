"""Repeat-call guard, finish gate and no-progress wrap-up guard.

Repeat-call guard is a port of DSH's repeat-tool-reminder (MIT, see THIRD_PARTY_NOTICES.md):
a run of consecutive identical calls (same tool, same canonical arguments) gets an advisory
reminder at 3, 5 and 8 repeats. KaroX adds one hard stop (default 20) so a stuck model
cannot burn the whole budget.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from karox6.tools.files import is_generated
from karox6.tools.proc import kill_tree

FIRST_REMINDER = (
    "You are repeating the exact same tool call with identical arguments. Carefully analyze the "
    "previous result before calling again: if the task is not complete, try a different approach "
    "or different arguments instead of repeating the call."
)
REPEAT_REMINDER = (
    "Repeated tool call detected:\n- tool: {tool}\n- consecutive_calls: {count}\n"
    "- arguments: {arguments}\nThe repeated calls are not making progress. Do not call this tool "
    "with these exact arguments again. Inspect the latest result and choose a different action, "
    "different arguments, or finish the task if enough evidence has been gathered."
)
PREVIEW_CHARS = 500


def canonical_arguments(raw: str) -> str:
    try:
        value: Any = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        return raw.strip()
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


@dataclass(slots=True)
class LoopGuard:
    thresholds: tuple[int, ...] = (3, 5, 8)
    hard_stop: int = 20
    _key: tuple[str, str] | None = None
    _count: int = 0
    _pending: list[str] = field(default_factory=list)

    def observe(self, tool: str, raw_arguments: str) -> bool:
        """Record one executed call. Returns True when the hard stop is reached."""
        canonical = canonical_arguments(raw_arguments)
        key = (tool, canonical)
        if key == self._key:
            self._count += 1
        else:
            self._key, self._count = key, 1
        if self._count == self.thresholds[0]:
            self._pending.append(FIRST_REMINDER)
        elif self._count in self.thresholds[1:]:
            preview = canonical
            if len(preview) > PREVIEW_CHARS:
                extra = len(preview) - PREVIEW_CHARS
                preview = preview[:PREVIEW_CHARS] + f"… (+{extra} more chars)"
            self._pending.append(
                REPEAT_REMINDER.format(tool=tool, count=self._count, arguments=preview)
            )
        return self.hard_stop > 0 and self._count >= self.hard_stop

    def take_reminders(self) -> list[str]:
        out, self._pending = self._pending, []
        return out

    def reset(self) -> None:
        self._key, self._count = None, 0
        self._pending.clear()


NO_PROGRESS_NOTICE = (
    "You have made no progress for several steps. Pick the single most likely hypothesis, "
    "make the minimal change for it (or none), run the relevant test, and finish with a "
    "short summary of what is and is not resolved."
)

FINISH_GATE_MAX_STEPS = 8
FINISH_GATE_MIN_STEPS = 2
FINISH_GATE_MIN_SECONDS = 90.0
FINISH_GATE_MAX_FILES = 20
FINISH_GATE_CAP_NOTICE = (
    "You have reached the post-finish check step limit. Reply now with your final answer "
    "(what you changed, what is verified, what remains) without calling tools."
)
FINISH_GATE_TEMPLATE = (
    "Before you finish, check your change against the task once more. "
    "(1) Re-read the task. Check every stated example, input/output value, "
    "condition, option and case; name the command that exercised each, or run one now. "
    "Skip anything explicitly rejected or out of scope. "
    "(2) Find other implementations of each changed symbol (sibling modules, "
    "backends, subclasses); update them only if the task explicitly covers them; "
    "otherwise mention them in your final answer. "
    "(3) Run tests covering your changes. "
    "Changed files: {changed}.{modified_tests_clause} "
    "If everything holds, reply with your final answer without further edits."
)
FINISH_GATE_MODIFIED_TESTS_CLAUSE = (
    " Existing test files you modified: {modified_tests}. "
    "Keep changes the requested behaviour needs; revert only edits that weaken or delete "
    "assertions just to make tests pass."
)

_MAX_CONTENT_HASH_BYTES = 65_536
_MAX_UNTRACKED_CONTENT_HASH = 100
_MAX_SUBMODULE_DEPTH = 3


@dataclass(slots=True)
class ProgressGuard:
    """Detect prolonged stalls (no workspace diff change and no first-time file read, or
    excessive output tokens since the last diff change) and emit a single wrap-up notice."""

    min_step: int = 12
    stall_steps: int = 10
    token_threshold: int = 60_000
    token_no_diff_steps: int = 8
    _last_hash: str | None = None
    _last_read_count: int = 0
    _no_diff: int = 0
    _no_progress: int = 0
    _tokens_at_last_diff: int = 0
    _fired: bool = False

    def reset(
        self,
        initial_hash: str,
        initial_read_count: int = 0,
        initial_output_tokens: int = 0,
    ) -> None:
        self._last_hash = initial_hash
        self._last_read_count = initial_read_count
        self._no_diff = 0
        self._no_progress = 0
        self._tokens_at_last_diff = initial_output_tokens
        self._fired = False

    def observe(
        self,
        step: int,
        diff_hash: str,
        read_count: int,
        cumulative_output_tokens: int,
    ) -> str | None:
        """Record one completed step. Returns ``NO_PROGRESS_NOTICE`` at most once per run."""
        if self._fired:
            return None
        diff_changed = self._last_hash is not None and diff_hash != self._last_hash
        new_file_read = read_count > self._last_read_count
        self._last_hash = diff_hash
        self._last_read_count = read_count
        if diff_changed:
            self._no_diff = 0
            self._tokens_at_last_diff = cumulative_output_tokens
        else:
            self._no_diff += 1
        if diff_changed or new_file_read:
            self._no_progress = 0
        else:
            self._no_progress += 1
        tokens_since_diff = cumulative_output_tokens - self._tokens_at_last_diff
        stalled = step >= self.min_step and self._no_progress >= self.stall_steps
        over_tokens = (
            step >= self.min_step
            and tokens_since_diff > self.token_threshold
            and self._no_diff >= self.token_no_diff_steps
        )
        if stalled or over_tokens:
            self._fired = True
            return NO_PROGRESS_NOTICE
        return None


@dataclass(slots=True)
class GitChangeSummary:
    changed: list[str]
    modified_tests: list[str]


def is_test_file(path: str) -> bool:
    """True when ``path`` matches common test directory or filename patterns."""
    raw_parts = [p for p in path.replace("\\", "/").split("/") if p]
    if not raw_parts:
        return False
    dirs = [p.lower() for p in raw_parts[:-1]]
    raw_name = raw_parts[-1]
    name = raw_name.lower()
    if "tests" in dirs or "test" in dirs:
        return True
    if name == "conftest.py":
        return True
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith(("_test.py", "_test.go")):
        return True
    if raw_name.endswith(("Test.java", "Tests.java")):
        return True
    if ".test." in name and not name.startswith(".test."):
        return True
    return ".spec." in name and not name.startswith(".spec.")


def finish_gate_message(changed: list[str], modified_tests: list[str]) -> str:
    shown = changed[:FINISH_GATE_MAX_FILES]
    changed_text = ", ".join(shown)
    if len(changed) > FINISH_GATE_MAX_FILES:
        changed_text += f" (+{len(changed) - FINISH_GATE_MAX_FILES} more)"
    if modified_tests:
        tests_text = ", ".join(modified_tests[:FINISH_GATE_MAX_FILES])
        if len(modified_tests) > FINISH_GATE_MAX_FILES:
            tests_text += f" (+{len(modified_tests) - FINISH_GATE_MAX_FILES} more)"
        clause = FINISH_GATE_MODIFIED_TESTS_CLAUSE.format(modified_tests=tests_text)
    else:
        clause = ""
    return FINISH_GATE_TEMPLATE.format(changed=changed_text, modified_tests_clause=clause)


# -- process-tree-safe git execution ------------------------------------------------------


def _win_create_kill_job() -> tuple[Any, Any] | None:
    """Create a Windows Job Object with KILL_ON_JOB_CLOSE so a timed-out git command and
    any grandchildren holding its pipes can be terminated atomically."""
    if sys.platform != "win32":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        from karox6.lifetime import extended_limits

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        limits = extended_limits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel32.SetInformationJobObject(
            job, 9, ctypes.byref(limits), ctypes.sizeof(limits)
        ):
            kernel32.CloseHandle(job)
            return None
        return kernel32, job
    except (OSError, AttributeError, ValueError):
        return None


def _win_assign_job(win_job: tuple[Any, Any] | None, pid: int) -> bool:
    if win_job is None:
        return False
    kernel32, job = win_job
    try:
        proc_handle = kernel32.OpenProcess(0x0100 | 0x0001, False, pid)
        if not proc_handle:
            return False
        try:
            return bool(kernel32.AssignProcessToJobObject(job, proc_handle))
        finally:
            kernel32.CloseHandle(proc_handle)
    except (OSError, AttributeError, ValueError):
        return False


def _win_close_job(win_job: tuple[Any, Any] | None, *, terminate: bool = False) -> None:
    if win_job is None:
        return
    kernel32, job = win_job
    with contextlib.suppress(OSError, AttributeError, ValueError):
        if terminate:
            kernel32.TerminateJobObject(job, 1)
        kernel32.CloseHandle(job)


def _git_command(args: tuple[str, ...]) -> list[str]:
    extra = list(args)
    if extra and extra[0] == "diff":
        flags = [f for f in ("--no-ext-diff", "--no-textconv") if f not in extra]
        extra = ["diff", *flags, *extra[1:]]
    return ["git", "-c", "core.fsmonitor=false", *extra]


def _run_git(
    workspace: Path, *args: str, timeout: float = 20.0
) -> subprocess.CompletedProcess[bytes] | None:
    """Run git with optional locks disabled, fsmonitor off, and process-tree-safe timeout.

    Raises ``subprocess.TimeoutExpired`` on timeout (after killing the entire process tree)
    so callers can disable the progress guard. Returns ``None`` on other OS/subprocess errors.
    """
    cmd = _git_command(args)
    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"}
    popen_kwargs: dict[str, Any] = {}
    win_job: tuple[Any, Any] | None = None
    if sys.platform == "win32":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        win_job = _win_create_kill_job()
    else:
        popen_kwargs["start_new_session"] = True

    proc: subprocess.Popen[bytes] | None = None
    assigned = False
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=workspace,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            **popen_kwargs,
        )
        if sys.platform == "win32":
            assigned = _win_assign_job(win_job, proc.pid)
        stdout, stderr = proc.communicate(timeout=timeout)
        return subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    except subprocess.TimeoutExpired:
        if proc is not None:
            if sys.platform == "win32":
                if assigned and win_job is not None:
                    _win_close_job(win_job, terminate=True)
                    win_job = None
                else:
                    kill_tree(proc.pid)
            else:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    with contextlib.suppress(OSError):
                        proc.kill()
            for pipe in (proc.stdout, proc.stderr):
                if pipe is not None:
                    with contextlib.suppress(OSError):
                        pipe.close()
            with contextlib.suppress(OSError, subprocess.SubprocessError):
                proc.wait(timeout=2.0)
        raise
    except (OSError, subprocess.SubprocessError):
        return None
    finally:
        if win_job is not None:
            _win_close_job(win_job, terminate=False)


# -- porcelain v2 snapshot and workspace monitor ------------------------------------------


def _norm_path(path: str) -> str:
    return path.replace("\\", "/").strip("/")


def _is_ignored(path: str, ignore: tuple[str, ...]) -> bool:
    norm = _norm_path(path)
    if not norm or is_generated(norm):
        return True
    for raw_dir in ignore:
        d = _norm_path(raw_dir)
        if d and (norm == d or norm.startswith(d + "/")):
            return True
    return False


def _filter_paths(paths: Iterable[str], ignore: tuple[str, ...]) -> list[str]:
    return sorted({_norm_path(p) for p in paths if p and not _is_ignored(p, ignore)})


@dataclass(frozen=True, slots=True)
class _StatusItem:
    path: str
    tracked: bool
    xy: str
    sub: str
    head_oid: str
    index_oid: str


@dataclass(frozen=True, slots=True)
class _EntryState:
    tracked: bool
    existed_in_head: bool
    xy: str
    head_oid: str
    index_oid: str
    size: int
    mtime_ns: int
    blob: str | None = None


def _parse_porcelain_v2(data: bytes) -> list[_StatusItem]:
    tokens = data.decode("utf-8", "replace").split("\0")
    items: list[_StatusItem] = []
    i = 0
    while i < len(tokens):
        entry = tokens[i]
        i += 1
        if not entry:
            continue
        if entry.startswith("1 "):
            parts = entry.split(" ", 8)
            if len(parts) == 9:
                _, xy, sub, _mH, _mI, _mW, hH, hI, path = parts
                items.append(_StatusItem(path, True, xy, sub, hH, hI))
        elif entry.startswith("2 "):
            parts = entry.split(" ", 9)
            if i < len(tokens):
                i += 1  # consume origPath
            if len(parts) == 10:
                _, xy, sub, _mH, _mI, _mW, hH, hI, _score, path = parts
                items.append(_StatusItem(path, True, xy, sub, hH, hI))
        elif entry.startswith("u "):
            parts = entry.split(" ", 10)
            if len(parts) == 11:
                _, xy, sub, _m1, _m2, _m3, _mW, h1, h2, _h3, path = parts
                items.append(_StatusItem(path, True, xy, sub, h1, h2))
        elif entry.startswith("? "):
            items.append(_StatusItem(entry[2:], False, "??", "N...", "", ""))
    return items


def _stat_entry(root: Path, rel: str, item: _StatusItem, *, hash_content: bool) -> _EntryState:
    existed = item.tracked and bool(item.head_oid) and any(ch != "0" for ch in item.head_oid)
    path = root / rel
    try:
        st = path.lstat()
        size, mtime_ns = st.st_size, st.st_mtime_ns
        blob: str | None = None
        if hash_content and stat.S_ISREG(st.st_mode) and size <= _MAX_CONTENT_HASH_BYTES:
            blob = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        size, mtime_ns, blob = -1, -1, None
    return _EntryState(
        tracked=item.tracked,
        existed_in_head=existed,
        xy=item.xy,
        head_oid=item.head_oid,
        index_oid=item.index_oid,
        size=size,
        mtime_ns=mtime_ns,
        blob=blob,
    )


def _entry_changed(old: _EntryState | None, new: _EntryState) -> bool:
    if old is None:
        return True
    if (
        old.tracked != new.tracked
        or old.xy != new.xy
        or old.head_oid != new.head_oid
        or old.index_oid != new.index_oid
        or old.size != new.size
        or old.mtime_ns != new.mtime_ns
    ):
        return True
    if old.blob is not None and new.blob is not None:
        return old.blob != new.blob
    return False


def _git_snapshot(
    root: Path,
    ignore: tuple[str, ...],
    *,
    prefix: str = "",
    depth: int = 0,
) -> dict[str, _EntryState] | None:
    proc = _run_git(root, "status", "--porcelain=v2", "-z", "--untracked-files=all")
    if proc is None or proc.returncode != 0:
        return None
    items = _parse_porcelain_v2(proc.stdout)
    untracked_count = sum(1 for it in items if not it.tracked)
    hash_untracked = untracked_count <= _MAX_UNTRACKED_CONTENT_HASH
    entries: dict[str, _EntryState] = {}
    for item in items:
        rel = _norm_path(item.path)
        full_rel = f"{prefix}/{rel}" if prefix else rel
        if not full_rel or _is_ignored(full_rel, ignore):
            continue
        if item.sub.startswith("S") and depth < _MAX_SUBMODULE_DEPTH:
            sub_dirty = len(item.sub) >= 4 and (item.sub[2] == "M" or item.sub[3] == "U")
            sub_commit = len(item.sub) >= 2 and item.sub[1] == "C"
            child_entries: dict[str, _EntryState] | None = None
            if sub_dirty:
                child_entries = _git_snapshot(root / rel, ignore, prefix=full_rel, depth=depth + 1)
                if child_entries:
                    entries.update(child_entries)
            if sub_commit or (not sub_dirty) or (child_entries is None):
                sub_head = _run_git(root / rel, "rev-parse", "HEAD") if sub_commit else None
                sub_blob = (
                    sub_head.stdout.decode("ascii", "replace").strip()
                    if sub_head is not None and sub_head.returncode == 0
                    else None
                )
                entries[full_rel] = _EntryState(
                    tracked=item.tracked,
                    existed_in_head=True,
                    xy=item.xy,
                    head_oid=item.head_oid,
                    index_oid=item.index_oid,
                    size=0,
                    mtime_ns=0,
                    blob=sub_blob,
                )
            continue
        entries[full_rel] = _stat_entry(
            root,
            rel,
            item,
            hash_content=item.tracked or hash_untracked,
        )
    return entries


def _hash_snapshot(entries: dict[str, _EntryState]) -> str:
    hasher = hashlib.sha256()
    hasher.update(b"git\0")
    for path in sorted(entries):
        e = entries[path]
        hasher.update(path.encode("utf-8", "replace"))
        hasher.update(b"\0")
        hasher.update(
            f"{int(e.tracked)}:{e.xy}:{e.head_oid}:{e.index_oid}:"
            f"{e.size}:{e.mtime_ns}:{e.blob or ''}".encode("ascii", "replace")
        )
        hasher.update(b"\0")
    return hasher.hexdigest()


def _hash_nogit(
    workspace: Path,
    ignore: tuple[str, ...],
    fallback_files: Callable[[], list[str]] | None,
) -> str:
    hasher = hashlib.sha256()
    hasher.update(b"nogit\0")
    files = _filter_paths(fallback_files() if fallback_files is not None else (), ignore)
    hash_content = len(files) <= _MAX_UNTRACKED_CONTENT_HASH
    for rel in files:
        hasher.update(rel.encode("utf-8", "replace"))
        hasher.update(b"\0")
        path = workspace / rel
        try:
            st = path.lstat()
            blob = (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if hash_content
                and stat.S_ISREG(st.st_mode)
                and st.st_size <= _MAX_CONTENT_HASH_BYTES
                else ""
            )
            hasher.update(f"{st.st_size}:{st.st_mtime_ns}:{blob}".encode("ascii"))
        except OSError:
            pass
        hasher.update(b"\0")
    return hasher.hexdigest()


@dataclass(slots=True)
class WorkspaceMonitor:
    """Per-run workspace snapshot and diff-hash tracker.

    Decides ``in_git`` once at ``initialize()``, uses one ``git status --porcelain=v2 -z``
    call per check (recursing into dirty submodules), keeps the previous hash on transient
    git failures, and disables itself after a git timeout.
    """

    workspace: Path
    ignore: tuple[str, ...] = ()
    fallback_files: Callable[[], list[str]] | None = None
    in_git: bool = False
    disabled: bool = False
    _start_snapshot: dict[str, _EntryState] | None = None
    _last_hash: str = ""

    def initialize(self) -> str:
        """Capture the run-start baseline (one git call; decides ``in_git`` once)."""
        try:
            snap = _git_snapshot(self.workspace, self.ignore)
        except subprocess.TimeoutExpired:
            self.in_git = True
            self.disabled = True
            self._start_snapshot = None
            self._last_hash = ""
            return self._last_hash
        if snap is not None:
            self.in_git = True
            self._start_snapshot = snap
            self._last_hash = _hash_snapshot(snap)
            return self._last_hash
        self.in_git = False
        self._start_snapshot = None
        self._last_hash = _hash_nogit(self.workspace, self.ignore, self.fallback_files)
        return self._last_hash

    def diff_hash(self) -> str:
        """Current workspace hash. Keeps the previous hash on failure; disables on timeout."""
        if self.disabled:
            return self._last_hash
        if not self.in_git:
            self._last_hash = _hash_nogit(self.workspace, self.ignore, self.fallback_files)
            return self._last_hash
        try:
            snap = _git_snapshot(self.workspace, self.ignore)
        except subprocess.TimeoutExpired:
            self.disabled = True
            return self._last_hash
        if snap is None:
            return self._last_hash
        self._last_hash = _hash_snapshot(snap)
        return self._last_hash

    def changes_since_start(self) -> GitChangeSummary | None:
        """Files changed since ``initialize()`` in a git repo, or None if none/unavailable."""
        if self.disabled or not self.in_git or self._start_snapshot is None:
            return None
        try:
            now = _git_snapshot(self.workspace, self.ignore)
        except subprocess.TimeoutExpired:
            self.disabled = True
            return None
        if now is None:
            return None
        changed = sorted(
            path
            for path, entry in now.items()
            if _entry_changed(self._start_snapshot.get(path), entry)
        )
        if not changed:
            return None
        modified_tests = [
            path
            for path in changed
            if is_test_file(path)
            and ((now[path].tracked and now[path].existed_in_head) or path in self._start_snapshot)
        ]
        return GitChangeSummary(changed=changed, modified_tests=modified_tests)
