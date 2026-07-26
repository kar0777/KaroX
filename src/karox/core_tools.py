"""Additional Core tools needed by an external coding agent.

These tools live in a subclass instead of in :mod:`karox.core` so the audited
boundary file stays untouched.  Every tool still goes through the same
``CoreRuntime.execute`` path, so policy, repository confinement, mutation
leases, idempotency and evidence apply unchanged.

Why each tool exists:

``repo.edit_file``
    ``repo.write_file`` replaces a whole file.  That is unusable for a module of
    several thousand lines, because the caller has to reproduce the entire file
    to change three lines and any transcription slip silently corrupts working
    code.  A bounded, exact-match replacement makes large files editable safely.

``repo.read_lines``
    ``repo.read_file`` returns the whole file.  A windowed read lets a caller
    inspect a specific region of a large module without paying for the rest.

``git.log``
    ``git.status`` and ``git.diff`` describe the working tree but say nothing
    about history, so a caller could not tell what had already been committed.

The file listing is also overridden to skip dependency and build directories.
The base implementation walks everything, so a virtual environment can consume
the whole result budget and make search useless in a real checkout.
"""

from __future__ import annotations

import hashlib
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core import (
    CoreError,
    CoreRuntime,
    InvalidCommand,
    InvalidPath,
    ToolDefinition,
)
from .models import Capability, CoreCommand, CoreResult, EvidenceRecord
from .security import contains_credential, redact, redact_content
from .sessions import MutationLease, SessionRecord


# Directories that are never source material.  Matched on any path component,
# so a nested virtual environment is skipped too.  Git and runtime metadata are
# already rejected by CoreRuntime.safe_path and are not repeated here.
IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".mypy_cache",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        ".zcode",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "site-packages",
        "venv",
    }
)

_IGNORED_DIRECTORY_SUFFIXES = (".egg-info",)


def is_ignored_path(relative: str) -> bool:
    """True when a repository-relative path sits inside an ignored directory."""
    for part in Path(relative).parts[:-1] or ():
        lowered = part.lower()
        if lowered in IGNORED_DIRECTORY_NAMES:
            return True
        if lowered.endswith(_IGNORED_DIRECTORY_SUFFIXES):
            return True
    return False


class ExtendedCoreRuntime(CoreRuntime):
    """Core runtime with editing, windowed reads and history inspection."""

    MAX_READ_LINES = 1_500
    MAX_EDIT_OCCURRENCES = 200
    MAX_LOG_ENTRIES = 200
    MAX_LISTED_FILES = 2_000
    MAX_DIFF_PATHS = 100

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        handlers = dict(self._handlers)
        handlers["repo.edit_file"] = self._edit_file
        handlers["repo.read_lines"] = self._read_lines
        handlers["git.log"] = self._git_log
        self._handlers = handlers
        additional = {
            "repo.edit_file": ToolDefinition(
                "repo.edit_file",
                "Replace an exact string inside a repository file.",
                Capability.REPO_WRITE,
                True,
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "old_string": {"type": "string"},
                        "new_string": {"type": "string"},
                        "expected_occurrences": {"type": "number"},
                    },
                    "required": ["path", "old_string", "new_string"],
                    "additionalProperties": False,
                },
            ),
            "repo.read_lines": ToolDefinition(
                "repo.read_lines",
                "Read a bounded line range of a UTF-8 repository file.",
                Capability.REPO_READ,
                False,
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string"},
                        "start": {"type": "number"},
                        "count": {"type": "number"},
                    },
                    "required": ["path"],
                    "additionalProperties": False,
                },
            ),
            "git.log": ToolDefinition(
                "git.log",
                "Read recent commit history.",
                Capability.GIT_READ,
                False,
                {
                    "type": "object",
                    "properties": {"limit": {"type": "number"}},
                    "additionalProperties": False,
                },
            ),
        }
        collisions = set(additional).intersection(self._definitions)
        if collisions:
            raise CoreError(
                f"extended tools collide with existing tools: {sorted(collisions)}"
            )
        self._definitions.update(additional)
        # git.diff without a path filter returns the whole working tree diff.
        # In a branch with dozens of touched files that answer is too large to
        # read and crowds out the change actually under review, so accept an
        # explicit path list. The property is optional, so existing callers that
        # pass only `staged` keep working unchanged.
        self._handlers = dict(self._handlers)
        self._handlers["git.diff"] = self._git_diff_paths
        base_diff = self._definitions["git.diff"]
        self._definitions["git.diff"] = replace(
            base_diff,
            description=(
                "Read the current Git diff, optionally limited to given paths."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "staged": {"type": "boolean"},
                    "paths": {"type": "array", "items": {"type": "string"}},
                },
                "additionalProperties": False,
            },
        )

    # -- result honesty ---------------------------------------------------

    def execute(
        self,
        command: CoreCommand,
        capability_token: Optional[str] = None,
        lease: Optional[MutationLease] = None,
    ) -> CoreResult:
        result = super().execute(command, capability_token, lease)
        # git.log is a read-only process call, so the base class does not treat a
        # non-zero exit as a failure. It has no idempotency record, so adjusting
        # the flag here cannot desynchronise a stored replay.
        if command.name == "git.log" and (
            result.data.get("timed_out") or result.data.get("exit_code") != 0
        ):
            result.ok = False
        return result

    def _preflight_mutation(
        self,
        command_name: str,
        arguments: Dict[str, Any],
        deadline_seconds: float,
    ) -> None:
        if command_name == "repo.edit_file":
            self._prepare_edit(arguments)
            return
        super()._preflight_mutation(command_name, arguments, deadline_seconds)

    def _record_mutation(
        self, record: SessionRecord, command: CoreCommand, result: CoreResult
    ) -> None:
        super()._record_mutation(record, command, result)
        if command.name != "repo.edit_file":
            return
        path = result.data.get("path")
        if (
            result.data.get("changed") is True
            and isinstance(path, str)
            and path not in record.changed_files
        ):
            record.changed_files.append(path)

    # -- repo.edit_file ---------------------------------------------------

    def _prepare_edit(
        self, arguments: Dict[str, Any]
    ) -> tuple[str, str, str, int, Path]:
        relative = self._required(arguments, "path", str)
        old_string = self._required(arguments, "old_string", str)
        new_string = self._required(arguments, "new_string", str)
        if not old_string:
            raise InvalidCommand("old_string must not be empty")
        if old_string == new_string:
            raise InvalidCommand("old_string and new_string are identical")
        requested = arguments.get("expected_occurrences", 1)
        if isinstance(requested, bool) or not isinstance(requested, (int, float)):
            raise InvalidCommand("expected_occurrences must be number")
        expected = int(requested)
        if expected < 1 or expected > self.MAX_EDIT_OCCURRENCES:
            raise InvalidCommand(
                "expected_occurrences must be between 1 and "
                f"{self.MAX_EDIT_OCCURRENCES}"
            )
        if contains_credential(new_string):
            raise CoreError("edit blocked by credential scanner")
        path = self.safe_path(relative, for_write=True)
        return relative, old_string, new_string, expected, path

    def _edit_file(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        relative, old_string, new_string, expected, path = self._prepare_edit(
            arguments
        )
        if not path.is_file():
            raise FileNotFoundError(path)
        size = path.stat().st_size
        if size > self.MAX_FILE_BYTES:
            raise CoreError(f"file is larger than {self.MAX_FILE_BYTES} bytes")
        raw = path.read_bytes()
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CoreError("repo.edit_file supports UTF-8 text only") from exc
        found = text.count(old_string)
        if found != expected:
            raise InvalidCommand(
                f"expected {expected} occurrence(s) of old_string "
                f"but found {found}"
            )
        updated = text.replace(old_string, new_string)
        previous_digest = hashlib.sha256(raw).hexdigest()
        # Reuse the audited atomic writer so permissions, fsync, temporary file
        # cleanup and the size ceiling behave exactly as for repo.write_file.
        result = self._write_file(
            {"path": relative, "content": updated}, deadline_seconds
        )
        result["replacements"] = found
        result["previous_sha256"] = previous_digest
        result["_evidence"] = [
            EvidenceRecord(
                kind="file_edit",
                summary=f"Replaced {found} occurrence(s) in {relative}",
                artifact_sha256=result.get("sha256"),
                metadata={
                    "path": relative,
                    "replacements": found,
                    "changed": bool(result.get("changed")),
                },
            )
        ]
        return result

    # -- repo.read_lines --------------------------------------------------

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
        return {
            "path": path.relative_to(self.repository).as_posix(),
            "start": start,
            "count": len(selected),
            "total_lines": len(lines),
            "has_more": (start - 1 + len(selected)) < len(lines),
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

    # -- git.log ----------------------------------------------------------

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

    # -- git.diff ---------------------------------------------------------

    def _git_diff_paths(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        staged = arguments.get("staged", False)
        if not isinstance(staged, bool):
            raise InvalidCommand("staged must be boolean")
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
        command = ["diff", "--no-ext-diff"]
        if staged:
            command.append("--cached")
        if relatives:
            command.append("--")
            command.extend(relatives)
        result = self._git(command, deadline_seconds)
        result["paths"] = list(relatives)
        result["_evidence"] = [self._git_evidence("git_diff", result)]
        return result

    # -- repo.list_files --------------------------------------------------

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
            raise InvalidCommand("pattern must be repository-relative")
        try:
            matches = self.repository.glob(pattern)
        except (OSError, ValueError) as exc:
            raise InvalidCommand(f"invalid glob pattern: {exc}") from exc
        items: List[str] = []
        for candidate in matches:
            if not candidate.is_file():
                continue
            try:
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
            items.append(posix)
            if len(items) >= self.MAX_LISTED_FILES:
                break
        return {
            "files": sorted(items),
            "truncated": len(items) >= self.MAX_LISTED_FILES,
        }
