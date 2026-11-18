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
import subprocess
import time
from pathlib import Path

WINDOW = 3  # consecutive meaningful lines that make a copy
MIN_LINE_CHARS = 4  # letters/digits a line needs to count as meaningful
MIN_WINDOW_CHARS = 30  # letters/digits a whole window needs
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
    one line the edit removed or changed."""
    kept = {_norm(line) for line in new.split("\n")}
    seq = [norm for _, norm in _meaningful(old.split("\n"))]
    windows: list[tuple[str, ...]] = []
    for start in range(len(seq) - WINDOW + 1):
        window = tuple(seq[start : start + WINDOW])
        if all(line in kept for line in window):
            continue
        if sum(_weight(line) for line in window) < MIN_WINDOW_CHARS:
            continue
        if window not in windows:
            windows.append(window)
    return windows


def _candidates(root: Path, edited: Path, suffix: str, deadline: float) -> list[str]:
    """Files of the same type, nearest to the edited file first (a sibling backend or
    module is found before the time budget runs out). Tracked files when ``root`` is a
    git repository (the index is read, not the working tree); else a bounded walk."""
    files: list[str] | None = None
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--cached"],
            cwd=root,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=max(deadline - time.monotonic(), 0.1),
            check=False,
        )
        if proc.returncode == 0:
            files = [p for p in proc.stdout.decode("utf-8", "replace").split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        files = None
    if files is None:
        files = []
        for dirpath, dirnames, filenames in os.walk(root):
            if time.monotonic() > deadline or len(files) >= MAX_FILES:
                break
            dirnames[:] = [d for d in dirnames if d not in _SKIP_PARTS]
            rel_dir = Path(dirpath).relative_to(root).as_posix()
            for name in filenames:
                if name.endswith(suffix):
                    files.append(name if rel_dir == "." else f"{rel_dir}/{name}")
    try:
        here = edited.relative_to(root).as_posix().split("/")
    except ValueError:
        here = []
    out = [p for p in files if p.endswith(suffix) and not _SKIP_PARTS.intersection(p.split("/"))][
        :MAX_FILES
    ]

    def distance(rel: str) -> int:
        parts = rel.split("/")
        common = 0
        for a, b in zip(parts[:-1], here[:-1], strict=False):
            if a != b:
                break
            common += 1
        return (len(parts) - 1 - common) + (len(here) - 1 - common)

    out.sort(key=distance)
    return out


def find_copies(
    root: Path,
    edited: Path,
    old: str,
    new: str,
    *,
    budget_s: float = TIME_BUDGET_S,
) -> list[tuple[str, int]]:
    """Places (workspace-relative path, 1-based line) where a replaced window still
    occurs, the edited file included (its other copies). Empty when nothing qualifies
    or the time budget runs out first."""
    windows = replaced_windows(old, new)
    suffix = edited.suffix
    if not windows or not suffix:
        return []
    deadline = time.monotonic() + budget_s
    # A cheap byte probe per window: its heaviest line as written (stripped). A copy whose
    # line differs only in inner spacing is missed; the probe keeps a large tree in budget.
    probes = [max(window, key=_weight).encode("utf-8") for window in windows]
    found: list[tuple[str, int]] = []
    for rel in _candidates(root, edited, suffix, deadline):
        if time.monotonic() > deadline:
            break
        path = root / rel
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            data = path.read_bytes()
        except OSError:
            continue
        live = [w for w, probe in zip(windows, probes, strict=True) if probe in data]
        if not live:
            continue
        text = data.decode("utf-8", errors="replace")
        seq = _meaningful(text.replace("\r\n", "\n").split("\n"))
        norms = [norm for _, norm in seq]
        hits: set[int] = set()
        for start in range(len(norms) - WINDOW + 1):
            if tuple(norms[start : start + WINDOW]) in live:
                hits.add(seq[start][0] + 1)
        # One location per copy: consecutive windows of the same copy overlap.
        last = -10
        for line in sorted(hits):
            if line - last > WINDOW:
                found.append((rel, line))
            last = line
    return found


def copies_note(found: list[tuple[str, int]], seen: set[str]) -> str:
    """The note for places not reported before in this run (``seen`` is updated)."""
    fresh = [(rel, line) for rel, line in found if f"{rel}:{line}" not in seen]
    if not fresh:
        return ""
    seen.update(f"{rel}:{line}" for rel, line in fresh)
    shown = ", ".join(f"{rel}:{line}" for rel, line in fresh[:MAX_SHOWN])
    more = f" and {len(fresh) - MAX_SHOWN} more" if len(fresh) > MAX_SHOWN else ""
    return (
        f"\nThe code this edit replaced also appears at {shown}{more}; "
        "check whether those places need the same change."
    )
