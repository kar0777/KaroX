"""Git tools: read state and diffs, commit, and the guarded push.

Split from :mod:`karox.core` so the Git surface can be read apart from the
process runner and the file tools it shares.
"""

from __future__ import annotations

import hashlib
from typing import Any, Dict, List, Optional

from .core_types import CoreError, InvalidCommand
from .models import EvidenceRecord
from .security import contains_credential


class GitToolsMixin:
    """Git methods mixed into :class:`karox.core.CoreRuntime`."""

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
        command = ["diff", "--no-ext-diff"]
        if staged:
            command.append("--cached")
        result = self._git(command, deadline_seconds)
        result["_evidence"] = [self._git_evidence("git_diff", result)]
        return result

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
