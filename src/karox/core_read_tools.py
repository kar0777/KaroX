"""Windowed reads, git log/diff path listing and list_files."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional
from .core import (
    CoreError,
    InvalidCommand,
    InvalidPath,
    _path_label,
)
from .security import contains_credential, redact_content


def is_ignored_path(relative: str) -> bool:
    """True when a repository-relative path sits inside an ignored directory."""
    for depth, part in enumerate(Path(relative).parts[:-1] or ()):
        lowered = part.lower()
        if lowered in IGNORED_DIRECTORY_NAMES:
            return True
        if depth == 0 and lowered in ROOT_IGNORED_DIRECTORY_NAMES:
            return True
        if lowered.endswith(_IGNORED_DIRECTORY_SUFFIXES):
            return True
        if lowered.startswith(_IGNORED_DIRECTORY_PREFIXES):
            return True
    return False

_IGNORED_DIRECTORY_PREFIXES = ("adapt-v5-", "adapt-v6-")


_IGNORED_DIRECTORY_SUFFIXES = (".egg-info",)


IGNORED_DIRECTORY_NAMES = frozenset(
    {
        # Scratch trees of this repository, which hold copies of it: a search
        # that walks them returns three hits for one definition, and the model
        # cannot tell which one is the code it is changing.
        ".tmp",
        ".work",
        ".mypy_cache",
        ".netlify",
        ".next",
        ".nox",
        ".nuxt",
        ".output",
        ".pytest_cache",
        ".ruff_cache",
        ".svelte-kit",
        ".tox",
        ".turbo",
        ".venv",
        ".vite",
        ".zcode",
        "__pycache__",
        "node_modules",
        "site-packages",
        "venv",
    }
)


#: Generated-output names that are also ordinary package names deeper down
#: (pip's ``operations/build``, a ``lib/out`` module), so only the repository
#: root's copy is skipped. ``coverage`` is not here at all: it is the package
#: directory of coverage.py itself.
ROOT_IGNORED_DIRECTORY_NAMES = frozenset({"build", "dist", "out", "target"})


class CoreReadToolsMixin:
    """Methods composed into ExtendedCoreRuntime; see core_tools.py."""

    def _read_lines(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        path = self.safe_path(self._required(arguments, "path", str))
        if not path.is_file():
            raise FileNotFoundError(path)
        size = path.stat().st_size
        if size > self.MAX_FILE_BYTES:
            raise CoreError(f"file is larger than {self.MAX_FILE_BYTES} bytes")
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CoreError("repo.read_lines supports UTF-8 text only") from exc
        start = self._bounded_number(arguments, "start", 1, 1)
        count = self._bounded_number(
            arguments, "count", self.MAX_READ_LINES, 1, self.MAX_READ_LINES
        )
        lines = text.splitlines()
        selected = lines[start - 1 : start - 1 + count]
        # The digest of the range as returned, not of the whole file: a caller
        # that compares two reads of the same window is asking about the window,
        # and a file edited outside the range has not changed the answer.
        returned = "\n".join(selected)
        return {
            "path": path.relative_to(self.repository).as_posix(),
            "start": start,
            "count": len(selected),
            "total_lines": len(lines),
            "has_more": (start - 1 + len(selected)) < len(lines),
            "content_sha256": hashlib.sha256(returned.encode("utf-8")).hexdigest(),
            # Byte-faithful, for the same reason as repo.read_file: a line
            # rewritten by pattern redaction cannot be used as an edit anchor.
            "lines": [str(redact_content(item)) for item in selected],
            "secret_like": any(contains_credential(item) for item in selected),
        }

    @staticmethod
    def _bounded_number(
        arguments: Dict[str, Any],
        name: str,
        default: int,
        minimum: int,
        maximum: Optional[int] = None,
    ) -> int:
        raw = arguments.get(name, default)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise InvalidCommand(f"{name} must be number")
        value = int(raw)
        if value < minimum:
            raise InvalidCommand(f"{name} must be {minimum} or greater")
        if maximum is not None:
            value = min(value, maximum)
        return value

    def _git_log(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        limit = self._bounded_number(
            arguments, "limit", 20, 1, self.MAX_LOG_ENTRIES
        )
        result = self._git(
            [
                "log",
                f"--max-count={limit}",
                "--no-decorate",
                "--date=iso-strict",
                "--pretty=format:%h%x09%ad%x09%an%x09%s",
            ],
            deadline_seconds,
        )
        result["limit"] = limit
        result["_evidence"] = [self._git_evidence("git_log", result)]
        return result

    def _git_diff_paths(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        staged = arguments.get("staged", False)
        if not isinstance(staged, bool):
            raise InvalidCommand("staged must be boolean")
        complete = arguments.get("complete", False)
        if not isinstance(complete, bool):
            raise InvalidCommand("complete must be boolean")
        raw_paths = arguments.get("paths", [])
        if not isinstance(raw_paths, list):
            raise InvalidCommand("paths must be array")
        if len(raw_paths) > self.MAX_DIFF_PATHS:
            raise InvalidCommand(
                f"paths must contain at most {self.MAX_DIFF_PATHS} entries"
            )
        relatives: List[str] = []
        for item in raw_paths:
            if not isinstance(item, str):
                raise InvalidCommand("paths items must be string")
            # Same confinement as every other path-taking tool: traversal,
            # absolute paths, links and metadata directories are rejected before
            # Git is invoked.
            resolved = self.safe_path(item)
            relative = resolved.relative_to(self.repository).as_posix()
            if relative not in relatives:
                relatives.append(relative)
        if complete:
            if staged:
                raise InvalidCommand("complete cannot be combined with staged")
            if relatives:
                raise InvalidCommand("complete cannot be combined with paths")
            return self._complete_change_review(deadline_seconds)
        command = ["diff", "--no-ext-diff"]
        if staged:
            command.append("--cached")
        if relatives:
            command.append("--")
            command.extend(relatives)
        return self._git_diff_result(command, relatives, deadline_seconds)

    def _list_files(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        pattern = arguments.get("pattern", "**/*")
        raw_pattern = Path(pattern) if isinstance(pattern, str) else None
        if (
            raw_pattern is None
            or not pattern
            or len(pattern) > 1000
            or "\x00" in pattern
            or raw_pattern.is_absolute()
            or raw_pattern.drive
            or ".." in raw_pattern.parts
        ):
            raise InvalidCommand(
                "pattern must be repository-relative: "
                + _path_label(str(raw_pattern))
            )
        # The unqualified default is the hot path for hosted agents. Python's
        # ``Path.glob('**/*')`` descends into node_modules/.venv *before* our
        # ignore predicate sees a candidate, so a normal JS repository can hit
        # the hosted 60s tool timeout just to list files. Git already owns the
        # repository index and ignore rules; use it for the broad listing and
        # keep globbing only for an explicitly bounded pattern.
        candidates: Iterable[str | Path]
        if pattern == "**/*":
            result = self._git(
                ["ls-files", "--cached", "--others", "--exclude-standard"],
                deadline_seconds,
            )
            candidates = [
                line for line in result.get("stdout", "").splitlines() if line
            ]
        else:
            try:
                candidates = self.repository.glob(pattern)
            except (OSError, ValueError) as exc:
                raise InvalidCommand(f"invalid glob pattern: {exc}") from exc

        items: List[str] = []
        truncated = False
        for candidate in candidates:
            try:
                if isinstance(candidate, str):
                    lexical = candidate.replace("\\", "/")
                else:
                    if not candidate.is_file():
                        continue
                    lexical = candidate.relative_to(self.repository).as_posix()
                safe = self.safe_path(lexical)
                relative = safe.relative_to(self.repository)
            except (InvalidPath, ValueError):
                continue
            if relative.parts and relative.parts[0].lower() in {".git", ".karox"}:
                continue
            posix = relative.as_posix()
            # Filtering happens before the ceiling is applied, so dependency
            # directories cannot crowd real source out of a truncated result.
            if is_ignored_path(posix):
                continue
            if len(items) >= self.MAX_LISTED_FILES:
                truncated = True
                break
            items.append(posix)
        return {
            "files": sorted(items),
            "truncated": truncated,
        }
