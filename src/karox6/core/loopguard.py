"""Repeat-call guard: the only mid-run intervention besides limits and format errors.

Port of DSH's repeat-tool-reminder (MIT, see THIRD_PARTY_NOTICES.md): a run of
consecutive identical calls (same tool, same canonical arguments) gets an advisory
reminder at 3, 5 and 8 repeats. KaroX adds one hard stop (default 20) so a stuck
model cannot burn the whole budget.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from karox6.tools.files import is_generated

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
FINISH_GATE_MAX_FILES = 20
FINISH_GATE_TEMPLATE = (
    "Before you finish, check your change against the task once more. "
    "(1) Re-read the original task. List every concrete example, input/output value, "
    "named condition, option and case it states, and for each name the command that "
    "exercised it, or run one now. Skip anything the task explicitly rejects or leaves "
    "out of scope. "
    "(2) Search for other implementations of each symbol you changed (sibling modules, "
    "backends, subclasses) and update them if the task applies to them. "
    "(3) Run the test files that cover the files you changed. "
    "Changed files: {changed}. "
    "Existing test files you modified: {modified_tests} — revert such edits unless the "
    "task asks for them. "
    "If everything already holds, reply with your final answer without further edits."
)

_MAX_HASH_FILE_BYTES = 1_048_576


@dataclass(slots=True)
class ProgressGuard:
    """Detect prolonged stalls (no workspace diff change and no first-time file read, or
    excessive output tokens without a diff change) and emit a single wrap-up notice."""

    min_step: int = 12
    stall_steps: int = 10
    token_threshold: int = 60_000
    token_no_diff_steps: int = 8
    _last_hash: str | None = None
    _last_read_count: int = 0
    _no_diff: int = 0
    _no_progress: int = 0
    _fired: bool = False

    def reset(self, initial_hash: str, initial_read_count: int = 0) -> None:
        self._last_hash = initial_hash
        self._last_read_count = initial_read_count
        self._no_diff = 0
        self._no_progress = 0
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
        else:
            self._no_diff += 1
        if diff_changed or new_file_read:
            self._no_progress = 0
        else:
            self._no_progress += 1
        stalled = step >= self.min_step and self._no_progress >= self.stall_steps
        over_tokens = (
            cumulative_output_tokens > self.token_threshold
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
    parts = [p.lower() for p in path.replace("\\", "/").split("/") if p]
    if not parts:
        return False
    dirs, name = parts[:-1], parts[-1]
    if "tests" in dirs or "test" in dirs:
        return True
    if name.startswith("test_") and name.endswith(".py"):
        return True
    if name.endswith("_test.py"):
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
    else:
        tests_text = "none"
    return FINISH_GATE_TEMPLATE.format(changed=changed_text, modified_tests=tests_text)


def _run_git(workspace: Path, *args: str) -> subprocess.CompletedProcess[bytes] | None:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _is_git_repo(workspace: Path) -> bool:
    proc = _run_git(workspace, "rev-parse", "--is-inside-work-tree")
    return proc is not None and proc.returncode == 0 and proc.stdout.strip() == b"true"


def _split_z(data: bytes) -> list[str]:
    return [p for p in data.decode("utf-8", "replace").split("\0") if p]


def _filter_paths(paths: Iterable[str], ignore: tuple[str, ...]) -> list[str]:
    return sorted(
        {
            p
            for p in paths
            if p
            and not is_generated(p)
            and not any(p == d or p.startswith(d + "/") for d in ignore)
        }
    )


def git_changes(workspace: Path, ignore: tuple[str, ...] = ()) -> GitChangeSummary | None:
    """Changed files and modified existing test files in a git repo, or None if not a git
    repo or no tracked diff / untracked non-ignored files exist."""
    if not _is_git_repo(workspace):
        return None
    head_diff = _run_git(workspace, "diff", "--name-only", "-z", "HEAD")
    if head_diff is not None and head_diff.returncode == 0:
        tracked = _split_z(head_diff.stdout)
        mod_proc = _run_git(workspace, "diff", "--name-only", "-z", "--diff-filter=MDTR", "HEAD")
        modified_existing = (
            _split_z(mod_proc.stdout)
            if mod_proc is not None and mod_proc.returncode == 0
            else tracked
        )
    else:
        unstaged_proc = _run_git(workspace, "diff", "--name-only", "-z")
        staged_proc = _run_git(workspace, "diff", "--cached", "--name-only", "-z")
        unstaged = (
            _split_z(unstaged_proc.stdout)
            if unstaged_proc is not None and unstaged_proc.returncode == 0
            else []
        )
        staged = (
            _split_z(staged_proc.stdout)
            if staged_proc is not None and staged_proc.returncode == 0
            else []
        )
        tracked = [*unstaged, *staged]
        modified_existing = unstaged
    untracked_proc = _run_git(workspace, "ls-files", "--others", "--exclude-standard", "-z")
    untracked = (
        _split_z(untracked_proc.stdout)
        if untracked_proc is not None and untracked_proc.returncode == 0
        else []
    )
    changed = _filter_paths([*tracked, *untracked], ignore)
    if not changed:
        return None
    modified_tests = [p for p in _filter_paths(modified_existing, ignore) if is_test_file(p)]
    return GitChangeSummary(changed=changed, modified_tests=modified_tests)


def _hash_paths(hasher: Any, workspace: Path, paths: Iterable[str]) -> None:
    for rel in paths:
        hasher.update(rel.encode("utf-8", "replace"))
        hasher.update(b"\0")
        path = workspace / rel
        try:
            st = path.stat()
            if st.st_size <= _MAX_HASH_FILE_BYTES:
                hasher.update(path.read_bytes())
            else:
                hasher.update(f"{st.st_size}:{st.st_mtime_ns}".encode("ascii"))
        except OSError:
            pass
        hasher.update(b"\0")


def workspace_diff_hash(
    workspace: Path,
    ignore: tuple[str, ...] = (),
    fallback_files: Callable[[], list[str]] | None = None,
) -> str:
    """Stable hash of ``git diff`` plus untracked non-ignored files (or tracked writes
    outside a git repository)."""
    hasher = hashlib.sha256()
    diff_proc = _run_git(workspace, "diff", "HEAD")
    in_git = diff_proc is not None and diff_proc.returncode == 0
    diff_bytes = diff_proc.stdout if in_git and diff_proc is not None else b""
    if not in_git and _is_git_repo(workspace):
        in_git = True
        unstaged = _run_git(workspace, "diff")
        staged = _run_git(workspace, "diff", "--cached")
        diff_bytes = (
            (unstaged.stdout if unstaged is not None and unstaged.returncode == 0 else b"")
            + b"\0"
            + (staged.stdout if staged is not None and staged.returncode == 0 else b"")
        )
    if in_git:
        hasher.update(b"git\0")
        hasher.update(diff_bytes)
        hasher.update(b"\0untracked\0")
        untracked_proc = _run_git(workspace, "ls-files", "--others", "--exclude-standard", "-z")
        untracked = (
            _filter_paths(_split_z(untracked_proc.stdout), ignore)
            if untracked_proc is not None and untracked_proc.returncode == 0
            else []
        )
        _hash_paths(hasher, workspace, untracked)
        return hasher.hexdigest()
    hasher.update(b"nogit\0")
    files = sorted(fallback_files()) if fallback_files is not None else []
    _hash_paths(hasher, workspace, files)
    return hasher.hexdigest()
