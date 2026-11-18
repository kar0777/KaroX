"""Copies of replaced code elsewhere in the workspace.

A defect fixed in one copy of duplicated code is often still present in the others
(a sibling backend, a sync/async pair, a second parser). After an edit, the lines it
replaced are looked up in the other files of the same type; places that still hold
three consecutive of those lines, at least one of which the edit changed, are listed
so the agent can decide whether they need the same change. Nothing is changed and no
step is forced: the note is information, bounded in time and length.
"""

from __future__ import annotations

import os
import re
import stat
import subprocess
import time
from pathlib import Path

WINDOW = 3  # consecutive meaningful lines that make a copy
MIN_LINE_CHARS = 4  # letters/digits a line needs to count as meaningful
MIN_WINDOW_CHARS = 30  # letters/digits a whole window needs
MAX_WINDOWS = 200  # a huge replacement is probed by its first windows only
MAX_SHOWN = 5
MAX_FILES = 20_000
MAX_FILE_BYTES = 1024 * 1024
TIME_BUDGET_S = 1.5
_SKIP_PARTS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".nox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    "site-packages",
    "dist",
    "build",
}
_ALNUM = re.compile(r"[^\W_]", re.UNICODE)
# Import blocks repeat across files without being copies of logic.
_IMPORT = re.compile(
    r"^(import\s|from\s+\S+\s+import\s|#\s*include\b|using\s|use\s|package\s|"
    r"(const|let|var)\s+\S+\s*=\s*require\()"
)
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _norm(line: str) -> str:
    return " ".join(line.split())


def _weight(norm: str) -> int:
    return len(_ALNUM.findall(norm))


def _meaningful(lines: list[str]) -> list[tuple[int, str]]:
    """(0-based line index, normalized text) of the lines that carry code."""
    out: list[tuple[int, str]] = []
    for index, line in enumerate(lines):
        norm = _norm(line)
        if _weight(norm) >= MIN_LINE_CHARS and not _IMPORT.match(norm):
            out.append((index, norm))
    return out


def replaced_windows(old: str, new: str) -> list[tuple[str, ...]]:
    """Windows of ``WINDOW`` consecutive meaningful lines of ``old`` that contain at least
    one line the edit removed or changed (first ``MAX_WINDOWS``, in order)."""
    kept = {_norm(line) for line in new.split("\n")}
    seq = [norm for _, norm in _meaningful(old.split("\n"))]
    windows: dict[tuple[str, ...], None] = {}
    for start in range(len(seq) - WINDOW + 1):
        window = tuple(seq[start : start + WINDOW])
        if all(line in kept for line in window):
            continue
        if sum(_weight(line) for line in window) < MIN_WINDOW_CHARS:
            continue
        windows[window] = None
        if len(windows) >= MAX_WINDOWS:
            break
    return list(windows)


def _git_files(root: Path, deadline: float) -> list[str] | None:
    """Tracked paths, or None when ``root`` is not a git work tree. A repository whose
    index cannot be read in time yields no paths rather than its untracked files."""
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=max(deadline - time.monotonic(), 0.05),
            check=False,
        )
    except FileNotFoundError:  # no git installed
        return None
    except (OSError, subprocess.SubprocessError):  # timed out: no time left to walk either
        return []
    if proc.returncode != 0:
        return None if b"not a git repository" in proc.stderr.lower() else []
    raw = proc.stdout.split(b"\0")
    return [os.fsdecode(p).replace("\\", "/") for p in raw if p]


def _candidates(root: Path, edited: Path, suffix: str, deadline: float) -> list[str]:
    """Files of the same type, nearest to the edited file first (a sibling backend or
    module is found before the time budget runs out)."""
    suffix = suffix.lower()
    files = _git_files(root, deadline)
    if files is None:
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            if time.monotonic() > deadline or len(files) >= MAX_FILES:
                break
            dirnames[:] = [d for d in dirnames if d not in _SKIP_PARTS]
            rel_dir = Path(dirpath).relative_to(root).as_posix()
            for name in filenames:
                if name.lower().endswith(suffix):
                    files.append(name if rel_dir == "." else f"{rel_dir}/{name}")
    try:
        here = edited.relative_to(root).as_posix().split("/")[:-1]
    except ValueError:
        here = []
    unique = {
        p: None
        for p in files
        if p.lower().endswith(suffix) and not _SKIP_PARTS.intersection(p.split("/"))
    }

    def distance(rel: str) -> int:
        parts = rel.split("/")[:-1]
        common = 0
        for a, b in zip(parts, here, strict=False):
            if a != b:
                break
            common += 1
        return (len(parts) - common) + (len(here) - common)

    return sorted(unique, key=distance)[:MAX_FILES]


def _read_regular(root: Path, rel: str) -> bytes | None:
    """Bytes of a regular file inside ``root``; symlinks, FIFOs, devices, large files and
    paths that leave the workspace are skipped (a read must not block or wander)."""
    path = root / rel
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
            return None
        if ".." in Path(rel).parts:
            return None
        return path.read_bytes()
    except OSError:
        return None


def find_copies(
    root: Path,
    edited: Path,
    old: str,
    new: str,
    *,
    budget_s: float = TIME_BUDGET_S,
) -> list[tuple[str, int]]:
    """Places (workspace-relative path, 1-based line) where a replaced window still
    occurs, the edited file included (its other copies). Partial or empty when the time
    budget runs out."""
    deadline = time.monotonic() + budget_s
    windows = replaced_windows(old, new)
    suffix = edited.suffix
    if not windows or not suffix or time.monotonic() > deadline:
        return []
    window_set = set(windows)
    # A cheap byte probe per window: its heaviest line as written (stripped). A copy whose
    # line differs only in inner spacing is missed; the probe keeps a large tree in budget.
    probes = {max(window, key=_weight).encode("utf-8") for window in windows}
    found: list[tuple[str, int]] = []
    for rel in _candidates(root, edited, suffix, deadline):
        if time.monotonic() > deadline:
            break
        data = _read_regular(root, rel)
        if data is None or not any(probe in data for probe in probes):
            continue
        text = data.decode("utf-8", errors="replace").lstrip("\ufeff")
        seq = _meaningful(text.replace("\r\n", "\n").split("\n"))
        norms = [norm for _, norm in seq]
        previous = -2
        for start in range(len(norms) - WINDOW + 1):
            if tuple(norms[start : start + WINDOW]) in window_set:
                # One location per copy: a copy is a run of consecutive matching windows.
                if start != previous + 1:
                    found.append((rel, seq[start][0] + 1))
                previous = start
    return found


def _shown_path(rel: str) -> str:
    return repr(rel) if _CONTROL.search(rel) else rel


def copies_note(found: list[tuple[str, int]], seen: set[str]) -> str:
    """The note for places not shown before in this run; only shown places join
    ``seen``, so a place cut by ``MAX_SHOWN`` can still be shown after a later edit."""
    fresh: dict[str, None] = {}
    for rel, line in found:
        key = f"{_shown_path(rel)}:{line}"
        if key not in seen:
            fresh[key] = None
    if not fresh:
        return ""
    keys = list(fresh)
    shown = keys[:MAX_SHOWN]
    seen.update(shown)
    more = f" and {len(keys) - MAX_SHOWN} more" if len(keys) > MAX_SHOWN else ""
    return (
        f"\nLines of the code this edit replaced also appear at {', '.join(shown)}{more}; "
        "check whether those places need the same change."
    )
