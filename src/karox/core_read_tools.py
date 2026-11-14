"""Windowed reads, git log/diff path listing and list_files."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Optional
from .core import (
    CoreError,
    InvalidCommand,
    InvalidPath,
    _path_label,
)
from .security import contains_credential, redact_content
from .text_lines import LATIN_1, decode_source, physical_lines


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


#: A revision a read or log may name: hashes, tags, branches, ^ and ~ suffixes.
_REVISION = re.compile(r"(?!-)[A-Za-z0-9._/@{}^~+-]{1,120}")
_REVISION_RANGE = re.compile(r"(?!-)[A-Za-z0-9._/@{}^~+-]{1,120}\.\.\.?[A-Za-z0-9._/@{}^~+-]{0,120}")


class CoreReadToolsMixin:
    """Methods composed into ExtendedCoreRuntime; see core_tools.py."""

    def _read_lines(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        rev = arguments.get("rev")
        if rev is not None:
            return self._read_lines_at(arguments, str(rev), deadline_seconds)
        path = self.safe_path(self._required(arguments, "path", str))
        if not path.is_file():
            raise FileNotFoundError(path)
        size = path.stat().st_size
        if size > self.MAX_FILE_BYTES:
            raise CoreError(f"file is larger than {self.MAX_FILE_BYTES} bytes")
        try:
            text, encoding = decode_source(path.read_bytes())
        except UnicodeDecodeError as exc:
            raise CoreError("repo.read_lines reads text files only; this one holds binary data") from exc
        window = self._line_window(arguments, path.relative_to(self.repository).as_posix(), text)
        if encoding == LATIN_1:
            window["encoding"] = LATIN_1
        return window

    def _read_lines_at(
        self, arguments: Dict[str, Any], rev: str, deadline_seconds: float
    ) -> Dict[str, Any]:
        """The same window of a file as it was at a commit, tag or branch.

        Benchmark 2026-11-13 (click-3239, v39): a regression task compared old
        versions of core.py through 25 separate python -c subprocess turns.
        A read at a revision is one batched call like any other read.
        """

        if not _REVISION.fullmatch(rev):
            raise InvalidCommand("rev must be a commit, tag or branch name (e.g. v8.2.1, HEAD~3, 9c632c0^)")
        relative = self._history_path(self._required(arguments, "path", str))
        result = self._git(["show", f"{rev}:{relative}"], deadline_seconds, redact_output=False)
        if result.get("exit_code") != 0:
            detail = str(result.get("stderr") or "").strip().splitlines()
            raise CoreError(f"git show {rev}:{relative} failed: {detail[-1] if detail else 'unknown error'}")
        if result.get("stdout_truncated"):
            raise CoreError("the file at that revision is larger than the read limit")
        window = self._line_window(arguments, relative, str(result.get("stdout") or ""))
        window["rev"] = rev
        return window

    def _line_window(self, arguments: Dict[str, Any], relative: str, text: str) -> Dict[str, Any]:
        start = self._bounded_number(arguments, "start", 1, 1)
        count = self._bounded_number(
            arguments, "count", self.DEFAULT_READ_LINES, 1, self.MAX_READ_LINES
        )
        lines = physical_lines(text)
        selected = lines[start - 1 : start - 1 + count]
        # The digest of the range as returned, not of the whole file: a caller
        # that compares two reads of the same window is asking about the window,
        # and a file edited outside the range has not changed the answer.
        returned = "\n".join(selected)
        return {
            "path": relative,
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

    def _history_path(self, path: Any) -> str:
        """The repository-relative spelling a history lookup passes to git.

        Tasks name files absolutely ("/app/src/x.py") and every other file tool
        accepts that spelling; git.log and read_lines(rev=...) refused it. A
        relative path may name a file that no longer exists, so it is checked
        lexically rather than resolved.
        """

        text = str(path)
        if text.startswith(("/", "\\")) or (len(text) > 2 and text[1] == ":"):
            return self.safe_path(text).relative_to(self.repository).as_posix()
        relative = text.replace(chr(92), "/").removeprefix("./")
        if not relative or relative.startswith("-") or ".." in relative.split("/") or ":" in relative:
            raise InvalidCommand("path must be repository-relative")
        return relative

    def _git_log(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        limit = self._bounded_number(
            arguments, "limit", 20, 1, self.MAX_LOG_ENTRIES
        )
        argv = [
            "log",
            f"--max-count={limit}",
            "--no-decorate",
            "--date=iso-strict",
            "--pretty=format:%h%x09%ad%x09%an%x09%s",
        ]
        grep = arguments.get("grep")
        if grep is not None:
            if not isinstance(grep, str) or not grep or len(grep) > 200 or "\x00" in grep:
                raise InvalidCommand("grep must be 1-200 characters")
            argv += ["-i", f"--grep={grep}"]
        rev = arguments.get("rev")
        if rev is not None:
            if not isinstance(rev, str) or not (_REVISION.fullmatch(rev) or _REVISION_RANGE.fullmatch(rev)):
                raise InvalidCommand("rev must be a commit, tag, branch or A..B range")
            argv.append(rev)
        path = arguments.get("path")
        if path is not None:
            argv += ["--", self._history_path(path)]
        result = self._git(argv, deadline_seconds)
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
        rev = arguments.get("rev")
        if rev is not None:
            # A past change, not the working tree: one call shows what a commit
            # (or an A..B range) did to the given paths. Benchmark 2026-11-13
            # (click-3239 v40): seven windowed reads of an old revision to
            # reconstruct one commit's change. It is not review evidence.
            if not isinstance(rev, str) or not (_REVISION.fullmatch(rev) or _REVISION_RANGE.fullmatch(rev)):
                raise InvalidCommand("rev must be a commit, tag, branch or A..B range")
            if staged or complete:
                raise InvalidCommand("rev cannot be combined with staged or complete")
            if ".." in rev:
                history = ["diff", "--no-ext-diff", "--no-color", rev]
            else:
                history = ["show", "--no-ext-diff", "--no-color", "--format=%h %s%n", rev]
            if not relatives:
                # Without paths a past commit can be a whole release; benchmark
                # click-3239 v41 got 19k characters. Name the files first.
                history.append("--stat")
            result = self._git([*history, "--", *relatives], deadline_seconds)
            if not relatives:
                result["note"] = "file summary only; pass paths to read the change itself"
            result["rev"] = rev
            result["paths"] = list(relatives)
            return result
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
        tail = pattern[3:] if pattern.startswith("**/") else ""
        if pattern == "**/*":
            try:
                result = self._git(
                    ["ls-files", "--cached", "--others", "--exclude-standard"],
                    deadline_seconds,
                )
            except OSError:
                # TB 4.1 images ship without a git binary at all: the call
                # raised FileNotFoundError and list_files refused outright.
                result = {"exit_code": None}
            # Outside a git checkout (a benchmark's /app, a downloaded tree)
            # ls-files fails and used to list nothing at all, which reads as
            # an empty project.
            candidates = (
                [line for line in result.get("stdout", "").splitlines() if line]
                if result.get("exit_code") == 0
                else self._walk_files()
            )
        elif tail and "**" not in tail and ".." not in tail:
            # "**/*.py" through Path.glob descends into .venv and node_modules
            # before the ignore filter sees a path; a pruned walk never enters
            # them. PurePosixPath.match compares from the right, as "**/" means.
            candidates = (
                path
                for path in self._walk_files()
                if PurePosixPath(path.relative_to(self.repository).as_posix()).match(tail)
            )
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

    def _walk_files(self) -> Iterable[Path]:
        """Every file under the repository, never entering an ignored directory."""

        root = self.repository
        real_root = os.path.realpath(root)
        for current, directories, files in os.walk(root):
            base = Path(current)
            relative = base.relative_to(root).as_posix()
            prefix = "" if relative == "." else relative + "/"
            directories[:] = sorted(
                name
                for name in directories
                if name.lower() not in {".git", ".karox"}
                and not is_ignored_path(f"{prefix}{name}/x")
            )
            for name in sorted(files):
                path = base / name
                if path.is_symlink():
                    # The dossier reads what this lists without safe_path: a
                    # link out of the repository must not lead it outside.
                    target = os.path.realpath(path)
                    if not (target == real_root or target.startswith(real_root + os.sep)):
                        continue
                yield path
