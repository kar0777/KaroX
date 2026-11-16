"""The one place that knows which files the agent has seen and changed.

* ``observe`` records the on-disk version (mtime, size) after a read or a write by a
  tool. ``write`` refuses to replace an existing file the agent has not seen, or one that
  changed on disk since it was seen (a formatter, a shell command) — the agent would be
  overwriting from a stale picture. ``edit`` needs neither when ``old_string`` matches
  exactly: the exact text proves the agent knows what it replaces; a fuzzy match on an
  unread or changed file is refused.
* ``changed_files`` answers "what did this session change": ``git status`` when the
  workspace is a git repository (covers shell edits too), plus every path a file tool
  wrote. There is no second bookkeeping anywhere else.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(slots=True)
class FileTracker:
    workspace: Path
    ignore: tuple[str, ...] = ()  # workspace-relative dirs never reported (KaroX's own home)
    _seen: dict[str, tuple[int, int]] = field(default_factory=dict)
    _written: set[str] = field(default_factory=set)
    _git_baseline: set[str] | None = None

    def __post_init__(self) -> None:
        self._git_baseline = _git_dirty(self.workspace)

    @staticmethod
    def _key(path: Path) -> str:
        return os.path.normcase(str(path))

    def observe(self, path: Path) -> None:
        try:
            st = path.stat()
        except OSError:
            self._seen.pop(self._key(path), None)
            return
        self._seen[self._key(path)] = (st.st_mtime_ns, st.st_size)

    def record_write(self, path: Path, *, observe: bool = True) -> None:
        """``observe=False``: the tool changed a file it has no full picture of (an edit of
        an unread or changed file), so a later ``write`` still has to read it first."""
        self._written.add(str(path))
        if observe:
            self.observe(path)

    def seen(self, path: Path) -> bool:
        return self._key(path) in self._seen

    def check_fresh(self, path: Path, *, require_seen: bool = True) -> str | None:
        """Error text when an existing file must be (re)read before it is changed.

        ``require_seen=False`` (edit) lets a never-read file through: an exact, unique
        ``old_string`` match already proves the agent knows the text it replaces.
        """
        if not path.exists():
            return None
        seen = self._seen.get(self._key(path))
        if seen is None:
            if not require_seen:
                return None
            return f"{path} has not been read in this session. Read it first, then retry."
        try:
            st = path.stat()
        except OSError as exc:
            return f"cannot stat {path}: {exc}"
        if (st.st_mtime_ns, st.st_size) != seen:
            return f"{path} changed on disk since it was last read. Read it again, then retry."
        return None

    def changed_files(self) -> list[str]:
        """Workspace-relative paths changed during the session (sorted)."""
        out: set[str] = set()
        now = _git_dirty(self.workspace)
        if now is not None:
            baseline = self._git_baseline or set()
            out |= now - baseline
            # A file that was already dirty and was written by a tool also counts.
            out |= {p for p in now & baseline if self._was_written(p)}
        for raw in self._written:
            path = Path(raw)
            try:
                out.add(path.relative_to(self.workspace).as_posix())
            except ValueError:
                out.add(path.as_posix())
        return sorted(
            p
            for p in out
            if not is_generated(p) and not any(p == d or p.startswith(d + "/") for d in self.ignore)
        )

    def _was_written(self, rel: str) -> bool:
        target = os.path.normcase(str(self.workspace / rel))
        return any(os.path.normcase(w) == target for w in self._written)


GENERATED_DIRS = frozenset({"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"})


def is_generated(path: str) -> bool:
    """Interpreter/tool caches: never reported as changes of the session."""
    parts = path.replace("\\", "/").split("/")
    return path.endswith((".pyc", ".pyo")) or any(part in GENERATED_DIRS for part in parts)


def _git_dirty(workspace: Path) -> set[str] | None:
    """Paths git reports as modified/added/deleted/untracked, or None outside a repo."""
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
            cwd=workspace,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    entries = proc.stdout.decode("utf-8", "replace").split("\0")
    out: set[str] = set()
    skip_next = False
    for entry in entries:
        if skip_next:
            skip_next = False
            continue
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        out.add(path)
        if status[0] in "RC":  # rename/copy: the next entry is the source path
            skip_next = True
    return out
