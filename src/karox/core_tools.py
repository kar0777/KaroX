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
import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .core import (
    CoreError,
    CoreRuntime,
    InvalidCommand,
    InvalidPath,
    ToolDefinition,
)
from .hot_worker import hot_worker_supervisor
from .models import Capability, CoreCommand, CoreResult, EvidenceRecord
from .security import contains_credential, redact_content
from .sessions import MutationLease, SessionRecord


# Directories that are never source material.  Matched on any path component,
# so a nested virtual environment is skipped too.  Git and runtime metadata are
# already rejected by CoreRuntime.safe_path and are not repeated here.
IGNORED_DIRECTORY_NAMES = frozenset(
    {
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
        "build",
        "coverage",
        "dist",
        "node_modules",
        "out",
        "site-packages",
        "target",
        "venv",
    }
)

_IGNORED_DIRECTORY_SUFFIXES = (".egg-info",)

_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


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

    # The base small-tree search fast path reads these attributes dynamically,
    # so dependency/build filtering stays owned by this Extended Core module
    # without creating a core.py -> core_tools.py import cycle.
    SEARCH_IGNORED_DIRECTORY_NAMES = IGNORED_DIRECTORY_NAMES
    SEARCH_IGNORED_DIRECTORY_SUFFIXES = _IGNORED_DIRECTORY_SUFFIXES

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
        handlers["repo.command"] = self._repo_command
        handlers["dev.command"] = self._dev_command
        handlers["tests.run"] = self._tests_run
        handlers["runtime.status"] = self._runtime_status
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
                        "expected_sha256": {
                            "type": "string",
                            "description": (
                                "sha256 of the file as it was read. The edit is "
                                "refused if the file changed since then."
                            ),
                        },
                        "allow_secret_literal": {
                            "type": "boolean",
                            "description": (
                                "Write text that looks like a credential on "
                                "purpose, such as a secret-scanner fixture or a "
                                "documentation example. Recorded in the result."
                            ),
                        },
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
            "repo.command": ToolDefinition(
                "repo.command",
                "Apply an atomic patch or batch through the hot-reload worker.",
                Capability.REPO_WRITE,
                True,
                {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["apply_patch", "batch"],
                            "description": (
                                "apply_patch applies one unified diff; batch atomically stages "
                                "write/delete/move/mkdir operations"
                            ),
                        },
                        "payload": {
                            "type": "object",
                            "description": (
                                "apply_patch payload: {patch, expected_sha256?, dry_run?, "
                                "allow_secret_literal?}. batch payload: {operations, dry_run?}; "
                                "each operation uses op=write|delete|move|mkdir with the "
                                "corresponding path/content/source/destination fields."
                            ),
                            "properties": {
                                "patch": {"type": "string"},
                                "expected_sha256": {
                                    "type": "object",
                                    "additionalProperties": {"type": "string"},
                                },
                                "dry_run": {"type": "boolean"},
                                "allow_secret_literal": {"type": "boolean"},
                                "operations": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {
                                            "op": {
                                                "type": "string",
                                                "enum": ["write", "delete", "move", "mkdir"],
                                            },
                                            "path": {"type": "string"},
                                            "content": {"type": "string"},
                                            "source": {"type": "string"},
                                            "destination": {"type": "string"},
                                            "expected_sha256": {"type": "string"},
                                            "allow_secret_literal": {"type": "boolean"},
                                            "missing_ok": {"type": "boolean"},
                                            "overwrite": {"type": "boolean"},
                                        },
                                        "required": ["op"],
                                        # The worker owns action-specific validation and
                                        # returns more precise unsupported-field errors.
                                        "additionalProperties": True,
                                    },
                                },
                            },
                            # Keep this schema descriptive for MCP clients; the
                            # authoritative action-specific validator lives in
                            # workspace_worker.validate_repo_command().
                            "additionalProperties": True,
                        },
                    },
                    "required": ["action", "payload"],
                    "additionalProperties": False,
                },
                external_schema=True,
            ),
            "dev.command": ToolDefinition(
                "dev.command",
                "Run a repository-scoped developer command without a shell. Available only when the hosted Full developer access profile explicitly selects it.",
                Capability.DEV_COMMAND,
                True,
                {
                    "type": "object",
                    "properties": {
                        "argv": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Executable and arguments. Shell syntax is not supported.",
                        },
                        "timeout_seconds": {"type": "number"},
                    },
                    "required": ["argv"],
                    "additionalProperties": False,
                },
                (Capability.PROCESS_RUN,),
                replayable=False,
            ),
            "tests.run": ToolDefinition(
                "tests.run",
                "Run a short synchronous project test verification. Python repositories use structured pytest; Node/Vite repositories without Python tests use the package test script (Vitest is forced to one-shot mode). Prefer karox.checks.start for long jobs that should survive request or tunnel interruptions.",
                Capability.CHECKS_RUN,
                True,
                {
                    "type": "object",
                    "properties": {
                        "suite": {"type": "string"},
                        "targets": {"type": "array", "items": {"type": "string"}},
                        "split": {"type": "number"},
                        "part": {"type": "number"},
                        "timeout_seconds": {"type": "number"},
                    },
                    "additionalProperties": False,
                },
                (Capability.PROCESS_RUN,),
                replayable=False,
            ),
            "runtime.status": ToolDefinition(
                "runtime.status",
                "Read hot-worker generation, source digest, and reload health.",
                Capability.REPO_READ,
                False,
                {
                    "type": "object",
                    "properties": {},
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

    # -- hot developer worker ---------------------------------------------

    def _repo_command(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        return hot_worker_supervisor().execute_repo(self, arguments, deadline_seconds)

    def _prepare_dev_command(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> tuple[List[str], float]:
        raw_argv = arguments.get("argv")
        if not isinstance(raw_argv, list):
            raise InvalidCommand("dev.command argv must be an array")
        # Full developer access is intentionally a trusted command surface. It
        # does not reuse CoreRuntime._validate_process(), because that validator
        # is the protected Project/checks boundary and deliberately rejects
        # shells, Git, authentication and publishing. The Full switch is the
        # explicit user grant that removes those command-class restrictions.
        #
        # Popen still receives argv with shell=False, so an argv call is executed
        # exactly as supplied. A shell is available by explicitly invoking one
        # (cmd /c, powershell -Command, bash -lc, ...), which is important for
        # real coding agents and makes the trust boundary unambiguous.
        argv = list(raw_argv)
        if not argv or len(argv) > 100 or not all(isinstance(item, str) for item in argv):
            raise InvalidCommand("dev.command argv must contain 1-100 strings")
        if any("\x00" in item or len(item) > 10_000 for item in argv):
            raise InvalidCommand("dev.command argv contains an invalid value")

        raw_timeout = arguments.get("timeout_seconds", deadline_seconds)
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)):
            raise InvalidCommand("dev.command timeout_seconds must be a number")
        timeout = float(raw_timeout)
        if not 0 < timeout <= float(deadline_seconds):
            raise InvalidCommand("dev.command timeout_seconds must be positive and within the request deadline")
        return argv, timeout

    def _dev_command(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        argv, timeout = self._prepare_dev_command(arguments, deadline_seconds)
        result = self._run(argv, timeout, inherit_environment=True)
        display_argv = result["argv"]
        result["_evidence"] = [
            EvidenceRecord(
                kind="command",
                summary=("Passed" if result.get("exit_code") == 0 else "Failed")
                + f": {' '.join(display_argv)}",
                command=display_argv,
                exit_code=result.get("exit_code"),
                metadata={
                    "timed_out": bool(result.get("timed_out")),
                    "repository_scoped": True,
                    "shell": False,
                },
            )
        ]
        return result

    def _tests_run(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        return hot_worker_supervisor().execute_tests(self, arguments, deadline_seconds)

    def _runtime_status(self, arguments: Dict[str, Any], deadline_seconds: float) -> Dict[str, Any]:
        del deadline_seconds
        if arguments:
            raise InvalidCommand("runtime.status takes no arguments")
        return hot_worker_supervisor().status()

    def _prepare_repo_command(self, arguments: Dict[str, Any], deadline_seconds: float) -> None:
        del deadline_seconds
        action = arguments.get("action")
        payload = arguments.get("payload")
        if action not in {"apply_patch", "batch"} or not isinstance(payload, dict):
            raise InvalidCommand("repo.command requires apply_patch or batch with object payload")
        hot_worker_supervisor().validate_repo(arguments)
        # The handler builds and validates one complete WorkspaceTransaction
        # before its first filesystem mutation. Running a separate dry-run here
        # created a second transaction, widened the preflight/commit race, and
        # made an immediate idempotent replay re-apply patch context to the
        # already-updated tree before the stored result could be returned.

    @staticmethod
    def _prepare_tests_run(arguments: Dict[str, Any]) -> None:
        if arguments.get("suite", "focused") not in {"focused", "full", "split"}:
            raise InvalidCommand("tests.run suite must be focused, full, or split")

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
        if command.name in {"git.log", "tests.run", "dev.command"} and (
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
        if command_name == "repo.command":
            self._prepare_repo_command(arguments, deadline_seconds)
            return
        if command_name == "dev.command":
            self._prepare_dev_command(arguments, deadline_seconds)
            return
        if command_name == "tests.run":
            self._prepare_tests_run(arguments)
            return
        super()._preflight_mutation(command_name, arguments, deadline_seconds)

    def _record_mutation(
        self, record: SessionRecord, command: CoreCommand, result: CoreResult
    ) -> None:
        super()._record_mutation(record, command, result)
        if command.name == "repo.command":
            for changed_path in result.data.get("changed_files", []):
                if isinstance(changed_path, str) and changed_path not in record.changed_files:
                    record.changed_files.append(changed_path)
            return
        if command.name == "tests.run":
            record.checks.append(
                {
                    "correlation_id": command.correlation_id,
                    "ok": not result.data.get("timed_out", False)
                    and result.data.get("exit_code") == 0,
                    "argv": result.data.get("argv", []),
                    "exit_code": result.data.get("exit_code"),
                    "timed_out": result.data.get("timed_out", False),
                    "suite": result.data.get("suite"),
                    "split": result.data.get("split"),
                    "part": result.data.get("part"),
                }
            )
            return
        if command.name != "repo.edit_file":
            return
        path = result.data.get("path")
        if (
            result.data.get("changed") is True
            and isinstance(path, str)
            and path not in record.changed_files
        ):
            record.changed_files.append(path)

    def _validate_idempotent_replay(
        self, command: CoreCommand, result: CoreResult
    ) -> None:
        super()._validate_idempotent_replay(command, result)
        if command.name != "repo.command" or result.data.get("dry_run") is True:
            return
        post_state = result.data.get("post_state")
        if not isinstance(post_state, dict):
            raise InvalidCommand("stored repo.command result has no post_state")
        for relative, expected in post_state.items():
            if not isinstance(relative, str) or (
                expected is not None and not isinstance(expected, str)
            ):
                raise InvalidCommand("stored repo.command post_state is invalid")
            path = self.safe_path(relative)
            current = self._file_sha256(path) if path.is_file() else None
            if current != expected:
                raise InvalidCommand(
                    "stored repo.command result no longer matches repository state; "
                    "use a new idempotency key"
                )
        post_directories = result.data.get("post_directories", {})
        if not isinstance(post_directories, dict):
            raise InvalidCommand("stored repo.command post_directories is invalid")
        for relative, expected_exists in post_directories.items():
            if not isinstance(relative, str) or not isinstance(expected_exists, bool):
                raise InvalidCommand("stored repo.command post_directories is invalid")
            path = self.safe_path(relative, for_write=True)
            if path.is_dir() != expected_exists:
                raise InvalidCommand(
                    "stored repo.command result no longer matches repository state; "
                    "use a new idempotency key"
                )

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
        if contains_credential(new_string) and not self._secret_literal_allowed(
            arguments
        ):
            raise CoreError(
                "edit blocked by credential scanner; pass allow_secret_literal "
                "to author a fixture or documentation example on purpose"
            )
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
        previous_digest = hashlib.sha256(raw).hexdigest()
        # An edit is written against a file the caller read earlier. Between the
        # read and the write, a check, a commit, a formatter or a second client
        # may have rewritten it -- and an exact-string match can still succeed on
        # the new content, quietly applying the edit to a file the caller never
        # saw. Naming the digest that was read turns that into a refusal.
        expected_digest = arguments.get("expected_sha256")
        if expected_digest is not None:
            if not isinstance(expected_digest, str) or not _SHA256.fullmatch(
                expected_digest
            ):
                raise InvalidCommand("expected_sha256 must be a sha256 hex digest")
            if expected_digest.lower() != previous_digest:
                raise InvalidCommand(
                    "file changed since it was read: expected "
                    f"{expected_digest.lower()} but found {previous_digest}"
                )
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CoreError("repo.edit_file supports UTF-8 text only") from exc
        replacement_old = old_string
        replacement_new = new_string
        found = text.count(replacement_old)
        line_ending_adapted = False
        if found == 0:
            if "\r\n" in text and "\r\n" not in old_string and "\n" in old_string:
                candidate_old = old_string.replace("\n", "\r\n")
                candidate_new = new_string.replace("\n", "\r\n")
                candidate_found = text.count(candidate_old)
                if candidate_found:
                    replacement_old = candidate_old
                    replacement_new = candidate_new
                    found = candidate_found
                    line_ending_adapted = True
            elif "\r\n" not in text and "\r\n" in old_string:
                candidate_old = old_string.replace("\r\n", "\n")
                candidate_new = new_string.replace("\r\n", "\n")
                candidate_found = text.count(candidate_old)
                if candidate_found:
                    replacement_old = candidate_old
                    replacement_new = candidate_new
                    found = candidate_found
                    line_ending_adapted = True
        if found != expected:
            raise InvalidCommand(
                f"expected {expected} occurrence(s) of old_string "
                f"but found {found}"
            )
        updated = text.replace(replacement_old, replacement_new)
        # Reuse the audited atomic writer so permissions, fsync, temporary file
        # cleanup and the size ceiling behave exactly as for repo.write_file.
        result = self._write_file(
            {
                "path": relative,
                "content": updated,
                # The edit already made this decision; the write must not make
                # it again and refuse content the caller was allowed to author.
                "allow_secret_literal": self._secret_literal_allowed(arguments),
            },
            deadline_seconds,
        )
        result["replacements"] = found
        result["line_ending_adapted"] = line_ending_adapted
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
