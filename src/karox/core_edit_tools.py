"""Edit/patch/plan tools and their mutation replay logic."""

from __future__ import annotations

import re

EDIT_DIFF_MAX_CHARS = 4_000
MAX_PLAN_ITEMS = 12
MAX_PLAN_ITEM_CHARS = 160

import difflib
import hashlib
from pathlib import Path
from typing import Any, Dict, List, Sequence
from .core import (
    CoreError,
    InvalidCommand,
)
from .edit_engine import ALL, UNIQUE, EditFailure, EditOp, apply_edits
from .models import CoreCommand, CoreResult, EvidenceRecord
from .security import contains_credential
from .sessions import SessionRecord


def _replaced_occurrences(text: str, ops: Sequence[EditOp]) -> int:
    """How many occurrences of the original text this batch replaces.

    Counted for reporting only. A batch that reached this point already applied
    cleanly, so an edit whose exact anchor the file does not contain was matched
    through the normalized retry, which accepts a unique match or nothing.
    """
    total = 0
    for op in ops:
        found = text.count(op.old)
        if found and op.occurrence == ALL:
            total += found
        else:
            total += 1
    return total

def _edit_diff(relative: str, before: str, after: str) -> str:
    """Unified diff of one edit, bounded so it cannot flood a turn."""
    diff = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
            n=3,
        )
    )
    if len(diff) > EDIT_DIFF_MAX_CHARS:
        return diff[: EDIT_DIFF_MAX_CHARS] + "…[truncated]"
    return diff

_SHA256 = re.compile(r"[0-9a-fA-F]{64}")


class CoreEditToolsMixin:
    """Methods composed into ExtendedCoreRuntime; see core_tools.py."""

    def _preflight_mutation(
        self,
        command_name: str,
        arguments: Dict[str, Any],
        deadline_seconds: float,
    ) -> None:
        if command_name == "repo.edit_file":
            self._prepare_edit(arguments)
            return
        if command_name == "repo.apply_patch":
            self._prepare_apply_patch(arguments, deadline_seconds)
            return
        if command_name == "plan.update":
            self._prepare_plan_update(arguments)
            return
        if command_name == "repo.command":
            self._prepare_repo_command(arguments, deadline_seconds)
            return
        if command_name == "dev.command":
            self._prepare_dev_command(arguments, deadline_seconds)
            return
        if command_name == "dev.command_start":
            self._prepare_dev_command_start(arguments)
            return
        if command_name == "checks.start":
            self._prepare_checks_start(arguments)
            return
        if command_name == "tests.run":
            self._prepare_tests_run(arguments)
            return
        if command_name == "web.fetch":
            self._prepare_fetch(arguments)
            return
        super()._preflight_mutation(command_name, arguments, deadline_seconds)

    def _record_mutation(
        self, record: SessionRecord, command: CoreCommand, result: CoreResult
    ) -> None:
        super()._record_mutation(record, command, result)
        if command.name == "plan.update":
            # Re-validated from the arguments rather than echoed through the
            # result: the model just sent this list, and sending it back would
            # cost the request it was meant to save.
            record.plan_items = self._prepare_plan_update(command.arguments)
            return
        if command.name in {"repo.command", "repo.apply_patch"}:
            if result.data.get("dry_run") is True:
                return
            for changed_path in result.data.get("changed_files", []):
                if isinstance(changed_path, str) and changed_path not in record.changed_files:
                    record.changed_files.append(changed_path)
            for operation in result.data.get("operations", []) or []:
                if not isinstance(operation, dict) or operation.get("changed") is not True:
                    continue
                op_path = operation.get("path")
                if not isinstance(op_path, str):
                    continue
                if operation.get("op") == "write" and operation.get("previous_sha256") is None:
                    if op_path not in record.created_files:
                        record.created_files.append(op_path)
                elif operation.get("op") == "delete" and op_path in record.created_files:
                    record.created_files.remove(op_path)
        if command.name == "web.fetch":
            # A vendor write changed a repository file exactly as
            # repo.write_file would have, so the session's record of what this
            # run touched must show it; otherwise a run could end claiming it
            # changed nothing while a fetched file sits in the tree.
            #
            # It must also record files it created. A download is a scratch
            # copy, and the model routinely removes it before finishing. When
            # the path never reached ``created_files`` the destructive gate
            # treated that cleanup as deleting the user's source: it asked for
            # a confirmation an unattended run cannot give, and one benchmark
            # run burned 37 refused calls inventing confirmation fields.
            saved = result.data.get("path")
            if result.data.get("changed") is True and isinstance(saved, str):
                if saved not in record.changed_files:
                    record.changed_files.append(saved)
                if (
                    result.data.get("previous_sha256") is None
                    and saved not in record.created_files
                ):
                    record.created_files.append(saved)
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
        if command.name not in {"repo.command", "repo.apply_patch"} or result.data.get("dry_run") is True:
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

    def _prepare_edit(
        self, arguments: Dict[str, Any]
    ) -> tuple[str, List[EditOp], Path, bool, int]:
        """Validate the call and turn it into edit operations.

        This runs in preflight too, before an idempotency intent is reserved, so
        it may only look at the arguments: a deterministically bad call must be
        refused before anything durable names it.
        """
        relative = self._required(arguments, "path", str)
        batch = arguments.get("edits") is not None
        pair = "old_string" in arguments or "new_string" in arguments
        if batch and pair:
            raise InvalidCommand(
                "pass edits=[{old,new}] or old_string/new_string, not both"
            )
        if not batch and not pair:
            raise InvalidCommand(
                "pass edits=[{old,new}] for several replacements or "
                "old_string/new_string for one"
            )
        expected = 1
        if batch:
            ops = self._parse_edits(arguments.get("edits"))
        else:
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
            ops = [EditOp(old_string, new_string)]
        for op in ops:
            if contains_credential(op.new) and not self._secret_literal_allowed(
                arguments
            ):
                raise CoreError(
                    "edit blocked by credential scanner; pass allow_secret_literal "
                    "to author a fixture or documentation example on purpose"
                )
        path = self.safe_path(relative, for_write=True)
        return relative, ops, path, batch, expected

    def _parse_edits(self, value: Any) -> List[EditOp]:
        """The ``edits`` array as operations, or a refusal naming the bad entry."""
        if not isinstance(value, list) or not value:
            raise InvalidCommand("edits must be a non-empty array")
        if len(value) > self.MAX_EDIT_BATCH:
            raise InvalidCommand(
                f"edits accepts at most {self.MAX_EDIT_BATCH} replacements per call"
            )
        ops: List[EditOp] = []
        for index, item in enumerate(value):
            if not isinstance(item, dict):
                raise InvalidCommand(f"edits[{index}] must be an object")
            unknown = set(item).difference({"old", "new", "occurrence"})
            if unknown:
                raise InvalidCommand(
                    f"edits[{index}] has unknown keys: {sorted(unknown)}"
                )
            old = item.get("old")
            new = item.get("new")
            if not isinstance(old, str):
                raise InvalidCommand(f"edits[{index}].old must be a string")
            if not isinstance(new, str):
                raise InvalidCommand(f"edits[{index}].new must be a string")
            occurrence: str | int = UNIQUE
            if "occurrence" in item:
                raw = item["occurrence"]
                if isinstance(raw, bool) or not isinstance(raw, (str, int)):
                    raise InvalidCommand(
                        f"edits[{index}].occurrence must be {UNIQUE!r}, {ALL!r} "
                        "or an occurrence number"
                    )
                if isinstance(raw, str):
                    if raw not in {UNIQUE, ALL}:
                        raise InvalidCommand(
                            f"edits[{index}].occurrence must be {UNIQUE!r}, "
                            f"{ALL!r} or an occurrence number"
                        )
                    occurrence = raw
                elif raw < 1:
                    raise InvalidCommand(
                        f"edits[{index}].occurrence must be 1 or greater"
                    )
                else:
                    occurrence = raw
            ops.append(EditOp(old, new, occurrence))
        return ops

    def _edit_file(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        relative, ops, path, batch, expected = self._prepare_edit(arguments)
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
        if batch:
            updated, failures = apply_edits(text, ops)
            if failures:
                return self._edit_failure_payload(relative, failures, previous_digest)
            replacements = _replaced_occurrences(text, ops)
            # No-op entries in a mixed batch are skipped by the engine.
            edits_applied = sum(1 for op in ops if op.old != op.new)
            line_ending_adapted = False
        elif expected == 1 and _legacy_miss(text, ops[0]):
            # A missing old_string used to be a bare "found 0" refusal, while
            # the edits form retried with folded whitespace and indentation and
            # named the closest lines. The common one-edit form gets the same.
            updated, failures = apply_edits(text, [EditOp(ops[0].old, ops[0].new, UNIQUE)])
            if failures:
                return self._edit_failure_payload(relative, failures, previous_digest)
            replacements = found = 1
            edits_applied = 1
            line_ending_adapted = False
        else:
            replacement_old, replacement_new, found, line_ending_adapted = (
                self._legacy_match(text, ops[0], expected)
            )
            updated, failures = apply_edits(
                text,
                [
                    EditOp(
                        replacement_old,
                        replacement_new,
                        UNIQUE if expected == 1 else ALL,
                    )
                ],
            )
            if failures:
                # The count above already proved the anchor is in this text, so
                # this can only mean the anchor stopped being unique between the
                # two lookups. Report it as the refusal it is.
                raise InvalidCommand(
                    f"edit could not be applied: {failures[0].hint}"
                )
            replacements = found
            edits_applied = 1
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
        result["replacements"] = replacements
        result["edits_applied"] = edits_applied
        result["line_ending_adapted"] = line_ending_adapted
        result["previous_sha256"] = previous_digest
        result["diff"] = _edit_diff(relative, text, updated)
        if not batch:
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

    def _legacy_match(
        self, text: str, op: EditOp, expected: int
    ) -> tuple[str, str, int, bool]:
        """The one-replacement form's anchor, counted and adapted as before.

        ``old_string``/``new_string`` keep the contract they always had: an
        explicit occurrence count, and a CRLF anchor that still finds its line
        when the caller quoted what ``repo.read_lines`` returned.
        """
        replacement_old = op.old
        replacement_new = op.new
        found = text.count(replacement_old)
        line_ending_adapted = False
        if found == 0:
            if "\r\n" in text and "\r\n" not in op.old and "\n" in op.old:
                candidate_old = op.old.replace("\n", "\r\n")
                candidate_new = op.new.replace("\n", "\r\n")
                candidate_found = text.count(candidate_old)
                if candidate_found:
                    replacement_old = candidate_old
                    replacement_new = candidate_new
                    found = candidate_found
                    line_ending_adapted = True
            elif "\r\n" not in text and "\r\n" in op.old:
                candidate_old = op.old.replace("\r\n", "\n")
                candidate_new = op.new.replace("\r\n", "\n")
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
        return replacement_old, replacement_new, found, line_ending_adapted

    @staticmethod
    def _edit_failure_payload(
        relative: str, failures: List[EditFailure], previous_digest: str
    ) -> Dict[str, Any]:
        """A refused batch: no side effect, and the reason for every edit.

        The failures travel in the result rather than as an exception because
        they are the repair instructions -- a model that reads them can fix the
        anchor in the next call instead of guessing again.
        """
        return {
            "path": relative,
            "changed": False,
            "edits_applied": 0,
            "error_code": "edit_failed",
            "message": "; ".join(failure.hint for failure in failures[:3]),
            "failures": [failure.to_dict() for failure in failures],
            "previous_sha256": previous_digest,
            # Nothing was written, so the file's digest is also its post-state:
            # a replay of this refusal still validates against unchanged content.
            "sha256": previous_digest,
            "_evidence": [],
        }

    @staticmethod
    def _prepare_plan_update(arguments: Dict[str, Any]) -> List[Dict[str, str]]:
        """Validate the model's plan list.

        The whole list arrives every time: a plan that had to be patched item by
        item would need a diff of a document that is already tiny, and a partial
        update cannot express "I dropped this".
        """
        raw = arguments.get("items")
        if not isinstance(raw, list):
            raise InvalidCommand("items must be an array")
        if len(raw) > MAX_PLAN_ITEMS:
            raise InvalidCommand(
                "items accepts at most "
                f"{MAX_PLAN_ITEMS} entries"
            )
        items: List[Dict[str, str]] = []
        for index, entry in enumerate(raw):
            if not isinstance(entry, dict):
                raise InvalidCommand(f"items[{index}] must be an object")
            unknown = set(entry).difference({"text", "status"})
            if unknown:
                raise InvalidCommand(
                    f"items[{index}] has unknown keys: {sorted(unknown)}"
                )
            label = entry.get("text")
            status = entry.get("status")
            if not isinstance(label, str) or not label.strip():
                raise InvalidCommand(f"items[{index}].text must be a non-empty string")
            if len(label) > MAX_PLAN_ITEM_CHARS:
                raise InvalidCommand(
                    f"items[{index}].text must be at most "
                    f"{MAX_PLAN_ITEM_CHARS} characters"
                )
            if status not in {"todo", "doing", "done"}:
                raise InvalidCommand(
                    f"items[{index}].status must be todo, doing or done"
                )
            items.append({"text": label.strip(), "status": str(status)})
        return items

    def _plan_update(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        del deadline_seconds
        items = self._prepare_plan_update(arguments)
        return {
            "items": len(items),
            "done": sum(1 for item in items if item["status"] == "done"),
        }


def _legacy_miss(text: str, op: EditOp) -> bool:
    """Whether old_string is absent even after the CRLF/LF swap _legacy_match makes."""

    if op.old in text:
        return False
    if "\r\n" in text and "\r\n" not in op.old and op.old.replace("\n", "\r\n") in text:
        return False
    if "\r\n" not in text and "\r\n" in op.old and op.old.replace("\r\n", "\n") in text:
        return False
    return True
