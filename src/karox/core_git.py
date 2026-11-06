"""Git tools: read state and diffs, commit, and the guarded push.

Split from :mod:`karox.core` so the Git surface can be read apart from the
process runner and the file tools it shares.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from .core_types import CoreError, InvalidCommand
from .models import EvidenceRecord
from .security import contains_credential, redact


class GitToolsMixin:
    """Git methods mixed into :class:`karox.core.CoreRuntime`."""

    #: A complete change review appends every new untracked file as its own
    #: new-file diff. These bounds keep one closing review from turning a
    #: repository with an untracked dependency tree into an unbounded read: the
    #: listing fails closed past the file ceiling, and each file's text is
    #: bounded like a read, with a digest marker beyond it.
    REVIEW_MAX_UNTRACKED_FILES = 200
    REVIEW_MAX_UNTRACKED_CHARS = 20_000
    REVIEW_MAX_UNTRACKED_TOTAL_CHARS = 400_000

    def _git(
        self,
        arguments: List[str],
        deadline_seconds: float,
        *,
        redact_output: bool = True,
    ) -> Dict[str, Any]:
        return self._run(
            ["git", *arguments],
            min(60.0, deadline_seconds),
            redact_output=redact_output,
        )

    @staticmethod
    def _git_evidence(kind: str, result: Dict[str, Any]) -> EvidenceRecord:
        digest = result.get("stdout_sha256") or hashlib.sha256(
            result["stdout"].encode("utf-8")
        ).hexdigest()
        result["sha256"] = digest
        return EvidenceRecord(
            kind=kind,
            summary=("Read" if result["exit_code"] == 0 else "Failed to read")
            + f" {kind.replace('_', ' ')}",
            command=result["argv"],
            exit_code=result["exit_code"],
            artifact_sha256=digest,
            metadata={"timed_out": result["timed_out"]},
        )

    def _git_status(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        if arguments:
            raise InvalidCommand("git.status takes no arguments")
        result = self._git(["status", "--short", "--branch"], deadline_seconds)
        result["_evidence"] = [self._git_evidence("git_status", result)]
        return result

    def _git_diff(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        staged = arguments.get("staged", False)
        if not isinstance(staged, bool):
            raise InvalidCommand("staged must be boolean")
        complete = arguments.get("complete", False)
        if not isinstance(complete, bool):
            raise InvalidCommand("complete must be boolean")
        if complete:
            if staged:
                raise InvalidCommand("complete cannot be combined with staged")
            if arguments.get("paths"):
                raise InvalidCommand("complete cannot be combined with paths")
            return self._complete_change_review(deadline_seconds)
        command = ["diff", "--no-ext-diff"]
        if staged:
            command.append("--cached")
        return self._git_diff_result(command, [], deadline_seconds)

    def _git_diff_result(
        self,
        command: List[str],
        relatives: List[str],
        deadline_seconds: float,
    ) -> Dict[str, Any]:
        """Run one scoped diff and record the scope it actually covered."""

        result = self._git(command, deadline_seconds)
        result["paths"] = list(relatives)
        result["review"] = {
            "version": 1,
            "mode": "staged" if "--cached" in command else "worktree",
            "base": None,
            "paths": list(relatives),
            "complete": False,
            "truncated": bool(result.get("stdout_truncated")),
        }
        result["_evidence"] = [self._git_evidence("git_diff", result)]
        return result

    def _complete_change_review(self, deadline_seconds: float) -> Dict[str, Any]:
        """Read the whole current change against HEAD, new files included.

        ``git diff HEAD`` is the one tracked view that holds both staged and
        unstaged edits; a plain ``git diff`` shows only the unstaged half and
        ``--cached`` only the index half, so neither can stand for the change a
        run is about to hand over. New files are absent from every view, so
        each untracked file is appended as its own new-file diff. A repository
        without a commit has no HEAD to compare with: there the index plays
        that part, and the untracked half covers the rest.

        The returned ``review`` block is the coverage the trusted Core actually
        executed. The completion gate reads it instead of the caller's claim,
        so a model asking for ``complete`` cannot widen what the commands saw.
        """

        head = self._git(["rev-parse", "--verify", "HEAD"], deadline_seconds)
        has_head = head.get("exit_code") == 0 and not head.get("timed_out")
        command = ["diff", "--no-ext-diff", "HEAD" if has_head else "--cached"]
        result = self._git(command, deadline_seconds)
        result["paths"] = []
        tracked_ok = result.get("exit_code") == 0 and not result.get("timed_out")
        untracked: Dict[str, Any] = {
            "listed": 0,
            "shown": [],
            "limited": [],
            "excluded": [],
            "truncated": False,
        }
        if tracked_ok:
            result["stdout"] = self._append_untracked_review(
                str(result.get("stdout") or ""), untracked, deadline_seconds
            )
            text = str(result["stdout"])
            result["stdout_bytes"] = len(text.encode("utf-8"))
            result["stdout_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        reviewed = bool(
            tracked_ok
            and not result.get("stdout_truncated")
            and not untracked["truncated"]
        )
        result["review"] = {
            "version": 1,
            "mode": "complete",
            "base": "HEAD" if has_head else "index",
            "paths": [],
            "complete": reviewed,
            "truncated": bool(
                result.get("stdout_truncated") or untracked["truncated"]
            ),
            "untracked": untracked,
        }
        result["_evidence"] = [self._git_evidence("git_diff", result)]
        return result

    def _append_untracked_review(
        self,
        text: str,
        untracked: Dict[str, Any],
        deadline_seconds: float,
    ) -> str:
        """Append every new unreferenced file to the tracked diff as a review."""

        # Imported lazily: ``core_tools`` builds on this module, and the ignore
        # rule is the same one the file tools use, so a vendored dependency tree
        # cannot turn a review into thousands of irrelevant new-file diffs.
        from .core_tools import is_ignored_path

        listed = self._git(
            ["ls-files", "--others", "--exclude-standard", "-z"], deadline_seconds
        )
        if listed.get("exit_code") != 0 or listed.get("timed_out"):
            untracked["truncated"] = True
            return text + (
                "\n[karox: the untracked-file list could not be read; "
                "this review does not cover new files]\n"
            )
        raw_paths = [
            item for item in str(listed.get("stdout") or "").split("\0") if item
        ]
        untracked["listed"] = len(raw_paths)
        relative_paths = []
        for relative in raw_paths:
            parts = Path(relative).parts
            if not parts or parts[0].lower() in {".git", ".karox"}:
                continue
            if is_ignored_path(relative):
                # Not source material: the file tools already refuse to list
                # it. A run can still write there, so the path is recorded as
                # excluded and the gate refuses to call the change reviewed.
                untracked["excluded"].append(relative)
                continue
            relative_paths.append(relative)
        if len(relative_paths) > self.REVIEW_MAX_UNTRACKED_FILES:
            untracked["truncated"] = True
            relative_paths = relative_paths[: self.REVIEW_MAX_UNTRACKED_FILES]
        sections: List[str] = []
        budget = self.REVIEW_MAX_UNTRACKED_TOTAL_CHARS
        for relative in relative_paths:
            section, state, spent = self._untracked_review_section(relative, budget)
            sections.append(section)
            untracked[state].append(relative)
            budget -= spent
        return text + "".join(sections)

    def _untracked_review_section(
        self, relative: str, budget: int
    ) -> tuple[str, str, int]:
        """One new-file diff, bounded and redacted; returns (text, state, spent).

        ``shown`` means the whole textual file is in the review. ``limited``
        means it is represented by a marker instead: binary content, a file
        over the read ceiling, a symlink outside the readable tree, or a file
        the total budget no longer had room for. The marker names the path and,
        where it was read, its size and digest -- never silence.
        """

        section, state, spent = self._untracked_review_text(relative, budget)
        # Untracked bytes never pass through ``_run``'s stdout redaction, so the
        # captured-output display rule is applied here instead.
        return (str(redact(section)), state, spent)

    def _untracked_review_text(
        self, relative: str, budget: int
    ) -> tuple[str, str, int]:
        header = (
            f"\ndiff --git a/{relative} b/{relative}\n"
            "new file mode 100644\n"
            "--- /dev/null\n"
            f"+++ b/{relative}\n"
        )
        try:
            path = self.safe_path(relative)
        except Exception:  # noqa: BLE001 - an unreadable new path is a marker
            return (
                header
                + f"[karox: new path not shown: {relative} "
                "(outside the readable repository tree)]\n",
                "limited",
                0,
            )
        try:
            if not path.is_file():
                return (
                    header
                    + f"[karox: untracked path vanished before its review: "
                    f"{relative}]\n",
                    "limited",
                    0,
                )
            size = path.stat().st_size
        except OSError:
            return (
                header + f"[karox: untracked path not readable: {relative}]\n",
                "limited",
                0,
            )
        if budget <= 0:
            return (
                header
                + f"[karox: not shown, the review content budget is spent: "
                f"{relative} ({size} bytes)]\n",
                "limited",
                0,
            )
        raw = b""
        digest = None
        if size <= self.MAX_FILE_BYTES:
            try:
                raw = path.read_bytes()
            except OSError:
                return (
                    header + f"[karox: untracked path not readable: {relative}]\n",
                    "limited",
                    0,
                )
            digest = hashlib.sha256(raw).hexdigest()
        identity = f"{size} bytes" + (f", sha256 {digest}" if digest else "")
        if size > self.MAX_FILE_BYTES:
            return (
                header
                + f"[karox: new file not shown, larger than the review read "
                f"limit: {relative} ({identity})]\n",
                "limited",
                0,
            )
        if b"\0" in raw[:8192]:
            return (
                header
                + f"Binary file {relative} ({identity}) has no textual diff\n",
                "limited",
                0,
            )
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return (
                header
                + f"Binary file {relative} ({identity}) has no textual diff\n",
                "limited",
                0,
            )
        if not content:
            return (header + "new empty file\n", "shown", 0)
        limit = min(self.REVIEW_MAX_UNTRACKED_CHARS, max(0, budget))
        shown = content[:limit]
        lines = shown.splitlines()
        body = "".join(f"+{line}\n" for line in lines)
        section = header + f"@@ -0,0 +1,{len(lines)} @@\n" + body
        limited = len(shown) < len(content)
        if limited:
            section += (
                f"[karox: only the first {len(shown)} characters of this "
                f"{identity} file are shown]\n"
            )
        return (section, "limited" if limited else "shown", len(shown))

    def _prepare_commit(self, arguments: Dict[str, Any]) -> tuple[str, List[str]]:
        message = self._required(arguments, "message", str)
        if not message.strip():
            raise InvalidCommand("commit message must not be empty")
        if len(message.encode("utf-8")) > self.MAX_COMMIT_MESSAGE_BYTES:
            raise InvalidCommand(
                f"commit message is longer than {self.MAX_COMMIT_MESSAGE_BYTES} bytes"
            )
        if any(ord(item) < 32 and item != "\n" for item in message):
            raise InvalidCommand("commit message contains control characters")
        if contains_credential(message):
            raise InvalidCommand("commit message blocked by credential scanner")
        raw_paths = self._required(arguments, "paths", list)
        if not raw_paths or len(raw_paths) > self.MAX_COMMIT_PATHS:
            raise InvalidCommand(
                f"paths must contain 1-{self.MAX_COMMIT_PATHS} entries"
            )
        relatives: List[str] = []
        for item in raw_paths:
            if not isinstance(item, str):
                raise InvalidCommand("paths items must be string")
            resolved = self.safe_path(item)
            relative = resolved.relative_to(self.repository).as_posix()
            if relative in relatives:
                raise InvalidCommand(f"duplicate commit path: {relative}")
            relatives.append(relative)
        return message, relatives

    def _commit_evidence(
        self,
        result: Dict[str, Any],
        relatives: List[str],
        committed: bool,
        commit_sha: Optional[str],
    ) -> EvidenceRecord:
        return EvidenceRecord(
            kind="git_commit",
            summary=("Committed " if committed else "Failed to commit ")
            + f"{len(relatives)} path(s)",
            command=result["argv"],
            exit_code=result["exit_code"],
            artifact_sha256=commit_sha,
            metadata={
                "timed_out": result["timed_out"],
                "paths": list(relatives),
                "committed": committed,
            },
        )

    def _git_commit(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        message, relatives = self._prepare_commit(arguments)
        staged = self._git(["add", "--", *relatives], deadline_seconds)
        if staged["timed_out"] or staged["exit_code"] != 0:
            staged["paths"] = list(relatives)
            staged["committed"] = False
            staged["commit_sha"] = None
            staged["stage_failed"] = True
            staged["_evidence"] = [
                self._commit_evidence(staged, relatives, False, None)
            ]
            return staged
        result = self._git(
            [
                "commit",
                "--no-verify",
                "--only",
                "--message",
                message,
                "--",
                *relatives,
            ],
            deadline_seconds,
        )
        committed = not result["timed_out"] and result["exit_code"] == 0
        commit_sha: Optional[str] = None
        if committed:
            revision = self._git(["rev-parse", "HEAD"], deadline_seconds)
            if revision["exit_code"] == 0:
                commit_sha = revision["stdout"].strip() or None
        result["paths"] = list(relatives)
        result["committed"] = committed
        result["commit_sha"] = commit_sha
        result["stage_failed"] = False
        result["_evidence"] = [
            self._commit_evidence(result, relatives, committed, commit_sha)
        ]
        return result

    def _prepare_push(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> tuple[str, str]:
        remote = self._required(arguments, "remote", str).strip()
        branch = self._required(arguments, "branch", str).strip()
        if (
            not remote
            or len(remote) > 200
            or remote.startswith("-")
            or any(ord(ch) < 33 or ch.isspace() for ch in remote)
        ):
            raise InvalidCommand("git.push remote must be a configured remote name")
        if not branch or len(branch) > 240 or any(ord(ch) < 33 or ch.isspace() for ch in branch):
            raise InvalidCommand("git.push branch must be a valid branch name")

        remotes = self._git(["remote"], deadline_seconds)
        if remotes["timed_out"] or remotes["exit_code"] != 0:
            raise CoreError("cannot enumerate configured Git remotes")
        configured = {line.strip() for line in remotes["stdout"].splitlines() if line.strip()}
        if remote not in configured:
            raise InvalidCommand("git.push remote is not configured in this repository")

        checked = self._git(["check-ref-format", "--branch", branch], deadline_seconds)
        if checked["timed_out"] or checked["exit_code"] != 0:
            raise InvalidCommand("git.push branch is not a valid Git branch name")
        return remote, branch

    def _git_push(
        self, arguments: Dict[str, Any], deadline_seconds: float
    ) -> Dict[str, Any]:
        remote, branch = self._prepare_push(arguments, deadline_seconds)
        revision = self._git(["rev-parse", "HEAD"], deadline_seconds)
        commit_sha = revision["stdout"].strip()
        if (
            revision["timed_out"]
            or revision["exit_code"] != 0
            or len(commit_sha) != 40
        ):
            raise InvalidCommand("git.push cannot prove the current Git HEAD")
        result = self._git(
            ["push", "--porcelain", remote, f"HEAD:refs/heads/{branch}"],
            deadline_seconds,
        )
        pushed = not result["timed_out"] and result["exit_code"] == 0
        result["remote"] = remote
        result["branch"] = branch
        result["commit_sha"] = commit_sha
        result["pushed"] = pushed
        result["_evidence"] = [
            EvidenceRecord(
                kind="git_push",
                summary=("Pushed" if pushed else "Failed to push")
                + f" current HEAD to {remote}/{branch}",
                command=result["argv"],
                exit_code=result["exit_code"],
                artifact_sha256=commit_sha,
                metadata={
                    "timed_out": result["timed_out"],
                    "remote": remote,
                    "branch": branch,
                    "pushed": pushed,
                },
            )
        ]
        return result
