from __future__ import annotations

import hashlib
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC, initialize_git_repository
from karox.core import CoreError, CoreRuntime, InvalidCommand, InvalidPath
from karox.models import (
    AccessProfile,
    Capability,
    CoreCommand,
    Origin,
    OriginKind,
)
from karox.policy import CapabilityPolicy, PolicyDenied
from karox.sessions import IdempotencyConflict, SessionError, SessionStore


class CoreRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "sample.txt").write_text("before\n", encoding="utf-8")
        self.sessions = SessionStore(self.root / "sessions")
        self.session = self.sessions.create(
            self.repository,
            "fix sample",
            AccessProfile.WORKSPACE_WRITE,
            session_id="session",
        )
        self.origin = Origin(OriginKind.NATIVE_AGENT, "test-agent")
        self.policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        self.policy.set_grants(
            self.origin,
            {
                Capability.REPO_READ,
                Capability.REPO_WRITE,
                Capability.CHECKS_RUN,
                Capability.PROCESS_RUN,
                Capability.GIT_READ,
            },
        )
        self.runtime = CoreRuntime(
            self.repository,
            self.policy,
            self.sessions,
            self.root / "audit.jsonl",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def command(
        self,
        name: str,
        arguments: dict[str, object],
        *,
        key: str | None = None,
        session_id: str = "session",
    ) -> CoreCommand:
        return CoreCommand(
            name,
            arguments,
            session_id,
            self.origin,
            idempotency_key=key,
        )

    def execute_mutation(self, command: CoreCommand):
        lease = self.sessions.acquire(command.session_id, "test")
        try:
            return self.runtime.execute(command, lease=lease)
        finally:
            self.sessions.release(lease)

    def test_unknown_arguments_fail_before_session_and_policy_lookup(self) -> None:
        command = self.command(
            "repo.read_file", {"path": "sample.txt", "surprise": True}, session_id="missing"
        )
        with self.assertRaisesRegex(InvalidCommand, "unknown arguments"):
            self.runtime.execute(command)

    def test_repository_mismatch_and_revocation_are_rejected(self) -> None:
        other_repo = self.root / "other"
        other_repo.mkdir()
        mismatch_runtime = CoreRuntime(other_repo, self.policy, self.sessions)
        with self.assertRaisesRegex(SessionError, "different repository"):
            mismatch_runtime.execute(self.command("repo.read_file", {"path": "sample.txt"}))

        lease = self.sessions.acquire("session", "revoke")
        record = self.sessions.load("session")
        record.revoked = True
        self.sessions.save(record, record.revision, lease)
        self.sessions.release(lease)
        with self.assertRaisesRegex(SessionError, "revoked"):
            self.runtime.execute(self.command("repo.read_file", {"path": "sample.txt"}))

    def test_traversal_and_symlink_paths_are_denied(self) -> None:
        with self.assertRaises(InvalidPath):
            self.runtime.execute(self.command("repo.read_file", {"path": "../outside.txt"}))
        with self.assertRaises(InvalidPath):
            self.runtime.execute(self.command("repo.read_file", {"path": ".git/config"}))

        outside = self.root / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.repository / "linked.txt"
        try:
            os.symlink(outside, link)
        except OSError as exc:
            self.skipTest(f"symlinks unavailable: {exc}")
        with self.assertRaises(InvalidPath):
            self.runtime.execute(self.command("repo.read_file", {"path": "linked.txt"}))

    def test_checks_require_checks_and_process_capabilities(self) -> None:
        restricted = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        restricted.set_grants(self.origin, {Capability.CHECKS_RUN})
        runtime = CoreRuntime(self.repository, restricted, self.sessions)
        command = self.command(
            "checks.run", {"argv": [sys.executable, "-c", "print('ok')"]}, key="check"
        )
        with self.assertRaisesRegex(PolicyDenied, "process.run"):
            runtime.execute(command)

    def test_shell_and_direct_git_commands_are_denied_without_pending_intent(self) -> None:
        for index, argv in enumerate(
            (
                ["git", "status"],
                ["git.exe", "status"],
                ["git.cmd", "status"],
                ["pwsh", "-Command", "echo ok"],
            )
        ):
            command = self.command(
                "checks.run", {"argv": argv}, key=f"denied-{index}"
            )
            lease = self.sessions.acquire("session", f"denied-{index}")
            try:
                with self.assertRaises(InvalidCommand):
                    self.runtime.execute(command, lease=lease)
            finally:
                self.sessions.release(lease)
        self.assertEqual(self.sessions.load("session").idempotency, {})

    def test_invalid_write_preflight_leaves_no_pending_intent(self) -> None:
        command = self.command(
            "repo.write_file",
            {"path": "../escaped.txt", "content": "bad"},
            key="invalid-write",
        )
        lease = self.sessions.acquire("session", "invalid-write")
        try:
            with self.assertRaises(InvalidPath):
                self.runtime.execute(command, lease=lease)
        finally:
            self.sessions.release(lease)
        self.assertNotIn("invalid-write", self.sessions.load("session").idempotency)

    def test_write_is_replayed_and_key_reuse_with_new_input_conflicts(self) -> None:
        first = self.command(
            "repo.write_file",
            {"path": "sample.txt", "content": "after\n"},
            key="write-once",
        )
        result = self.execute_mutation(first)
        self.assertTrue(result.ok)
        replay = self.execute_mutation(first)
        self.assertTrue(replay.idempotent_replay)
        self.assertEqual((self.repository / "sample.txt").read_text(encoding="utf-8"), "after\n")

        conflict = self.command(
            "repo.write_file",
            {"path": "sample.txt", "content": "different\n"},
            key="write-once",
        )
        with self.assertRaises(IdempotencyConflict):
            self.execute_mutation(conflict)

    def test_file_hashes_are_byte_accurate_and_noop_writes_are_not_changes(self) -> None:
        raw = b"first\r\nsecond\r\n"
        (self.repository / "sample.txt").write_bytes(raw)
        read = self.runtime.execute(
            self.command("repo.read_file", {"path": "sample.txt"})
        )
        digest = hashlib.sha256(raw).hexdigest()
        self.assertEqual(read.data["sha256"], digest)
        self.assertEqual(read.data["bytes"], len(raw))

        noop = self.execute_mutation(
            self.command(
                "repo.write_file",
                {"path": "sample.txt", "content": raw.decode("utf-8")},
                key="noop-write",
            )
        )
        self.assertFalse(noop.data["changed"])
        self.assertEqual(noop.data["previous_sha256"], digest)
        self.assertEqual(noop.data["sha256"], digest)
        self.assertEqual(self.sessions.load("session").changed_files, [])

        changed = self.execute_mutation(
            self.command(
                "repo.write_file",
                {"path": "sample.txt", "content": "changed\n"},
                key="changed-write",
            )
        )
        self.assertTrue(changed.data["changed"])
        self.assertEqual(changed.data["previous_sha256"], digest)
        self.assertEqual(self.sessions.load("session").changed_files, ["sample.txt"])

    @unittest.skipIf(os.name == "nt", "POSIX mode bits are not available on Windows")
    def test_atomic_write_preserves_existing_posix_mode(self) -> None:
        target = self.repository / "sample.txt"
        target.chmod(0o755)
        self.execute_mutation(
            self.command(
                "repo.write_file",
                {"path": "sample.txt", "content": "mode-preserved\n"},
                key="mode-write",
            )
        )
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o755)

    def test_git_status_and_diff_are_evidence_backed_and_fail_closed(self) -> None:
        status = self.runtime.execute(self.command("git.status", {}))
        diff = self.runtime.execute(self.command("git.diff", {}))
        self.assertTrue(status.ok)
        self.assertTrue(diff.ok)
        self.assertEqual(status.evidence[0].kind, "git_status")
        self.assertEqual(diff.evidence[0].kind, "git_diff")
        self.assertEqual(status.evidence[0].artifact_sha256, status.data["sha256"])
        self.assertEqual(diff.evidence[0].artifact_sha256, diff.data["sha256"])

        failed_result = {
            "argv": ["git", "status", "--short", "--branch"],
            "exit_code": 128,
            "stdout": "",
            "stderr": "not a repository",
            "timed_out": False,
            "duration_ms": 1.0,
        }
        with patch.object(self.runtime, "_run", return_value=failed_result):
            failed = self.runtime.execute(self.command("git.status", {}))
        self.assertFalse(failed.ok)
        self.assertEqual(failed.evidence[0].exit_code, 128)
        self.assertIn("Failed", failed.evidence[0].summary)

    def test_failed_and_timed_out_checks_are_evidence_backed(self) -> None:
        failed = self.execute_mutation(
            self.command(
                "checks.run",
                {"argv": [sys.executable, "-c", "raise SystemExit(7)"]},
                key="failed-check",
            )
        )
        self.assertFalse(failed.ok)
        self.assertEqual(failed.data["exit_code"], 7)
        self.assertEqual(failed.evidence[0].kind, "check")

        timed_out = self.execute_mutation(
            self.command(
                "checks.run",
                {
                    "argv": [sys.executable, "-c", "import time; time.sleep(2)"],
                    "timeout_seconds": 0.1,
                },
                key="timed-out-check",
            )
        )
        self.assertFalse(timed_out.ok)
        self.assertTrue(timed_out.data["timed_out"])
        checks = self.sessions.load("session").checks
        self.assertEqual([item["ok"] for item in checks], [False, False])

    def test_process_output_limit_is_measured_in_utf8_bytes(self) -> None:
        self.runtime.MAX_OUTPUT_BYTES = 5
        result = self.runtime._run(
            [
                sys.executable,
                "-c",
                "import sys; "
                "sys.stdout.buffer.write(b'a' + bytes.fromhex('f09f9982f09f9982')); "
                "sys.stderr.buffer.write(bytes.fromhex('c3a9c3a9c3a9'))",
            ],
            1.0,
        )

        self.assertEqual(result["stdout"], "🙂")
        self.assertEqual(result["stderr"], "éé")
        self.assertLessEqual(len(result["stdout"].encode("utf-8")), 5)
        self.assertLessEqual(len(result["stderr"].encode("utf-8")), 5)

    def test_non_finite_check_timeouts_are_rejected_without_pending_intent(self) -> None:
        for index, timeout in enumerate((float("nan"), float("inf"), float("-inf"))):
            command = self.command(
                "checks.run",
                {"argv": [sys.executable, "-c", "print('no')"], "timeout_seconds": timeout},
                key=f"non-finite-{index}",
            )
            lease = self.sessions.acquire("session", f"non-finite-{index}")
            try:
                with self.assertRaisesRegex(InvalidCommand, "must be positive"):
                    self.runtime.execute(command, lease=lease)
            finally:
                self.sessions.release(lease)
        self.assertEqual(self.sessions.load("session").idempotency, {})

    def test_outputs_and_audit_redact_credentials(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        result = self.execute_mutation(
            self.command(
                "checks.run",
                {"argv": [sys.executable, "-c", f"print('{secret}')"]},
                key="redaction",
            )
        )
        self.assertNotIn(secret, result.data["stdout"])
        self.assertNotIn(secret, json.dumps(result.to_dict()))
        self.assertNotIn(secret, json.dumps(self.sessions.load("session").to_dict()))
        audit = (self.root / "audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(secret, audit)

    def test_child_process_does_not_inherit_arbitrary_environment_secrets(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        previous = os.environ.get("HARMLESS_LOOKING_VALUE")
        os.environ["HARMLESS_LOOKING_VALUE"] = secret
        try:
            result = self.execute_mutation(
                self.command(
                    "checks.run",
                    {
                        "argv": [
                            sys.executable,
                            "-c",
                            "import os; print(os.getenv('HARMLESS_LOOKING_VALUE', 'missing'))",
                        ]
                    },
                    key="environment-isolation",
                )
            )
        finally:
            if previous is None:
                os.environ.pop("HARMLESS_LOOKING_VALUE", None)
            else:
                os.environ["HARMLESS_LOOKING_VALUE"] = previous
        self.assertEqual(result.data["stdout"].strip(), "missing")

    # --- repo.search -----------------------------------------------------

    def test_repo_search_finds_literal_with_path_and_line(self) -> None:
        (self.repository / "searchable.md").write_text(
            "alpha\nBANANA marker\ngamma\n", encoding="utf-8"
        )
        result = self.runtime.execute(
            self.command("repo.search", {"query": "BANANA"})
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["match_count"], 1)
        match = result.data["matches"][0]
        self.assertEqual(match["path"], "searchable.md")
        self.assertEqual(match["line"], 2)
        self.assertIn("BANANA", match["text"])

    def test_repo_search_regex_works_and_invalid_regex_rejected(self) -> None:
        (self.repository / "digits.txt").write_text(
            "abc 123 def\nno digits here\n", encoding="utf-8"
        )
        result = self.runtime.execute(
            self.command("repo.search", {"query": r"\d+", "regex": True})
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.data["match_count"], 1)
        self.assertEqual(result.data["matches"][0]["line"], 1)
        with self.assertRaisesRegex(InvalidCommand, "invalid regular expression"):
            self.runtime.execute(
                self.command("repo.search", {"query": "[", "regex": True})
            )

    def test_repo_search_case_insensitive_default_and_sensitive_override(self) -> None:
        (self.repository / "case.txt").write_text("Hello World\n", encoding="utf-8")
        insensitive = self.runtime.execute(
            self.command("repo.search", {"query": "hello"})
        )
        self.assertEqual(insensitive.data["match_count"], 1)
        sensitive = self.runtime.execute(
            self.command("repo.search", {"query": "hello", "case_sensitive": True})
        )
        self.assertEqual(sensitive.data["match_count"], 0)

    def test_repo_search_max_results_truncates_output(self) -> None:
        (self.repository / "many.txt").write_text(
            "".join(f"match line {i}\n" for i in range(20)),
            encoding="utf-8",
        )
        result = self.runtime.execute(
            self.command("repo.search", {"query": "match", "max_results": 2})
        )
        self.assertEqual(result.data["match_count"], 2)
        self.assertTrue(result.data["truncated"])

    def test_repo_search_excludes_git_metadata(self) -> None:
        marker = "UNIQUE_GIT_MARKER_ZZZ"
        (self.repository / ".git" / "karox_marker.txt").write_text(
            marker + "\n", encoding="utf-8"
        )
        result = self.runtime.execute(
            self.command("repo.search", {"query": marker})
        )
        self.assertEqual(result.data["match_count"], 0)
        self.assertEqual(result.data["matches"], [])

    # --- git.commit ------------------------------------------------------

    def _elevated_runtime(self) -> tuple[CoreRuntime, str]:
        self.sessions.create(
            self.repository,
            "elevated commit task",
            AccessProfile.ELEVATED,
            session_id="elevated",
        )
        policy = CapabilityPolicy(AccessProfile.ELEVATED)
        policy.set_grants(
            self.origin,
            {
                Capability.REPO_READ,
                Capability.REPO_WRITE,
                Capability.CHECKS_RUN,
                Capability.PROCESS_RUN,
                Capability.GIT_READ,
                Capability.GIT_COMMIT,
            },
        )
        runtime = CoreRuntime(
            self.repository,
            policy,
            self.sessions,
            self.root / "audit-elevated.jsonl",
        )
        return runtime, "elevated"

    def _execute_on(self, runtime: CoreRuntime, command: CoreCommand):
        lease = self.sessions.acquire(command.session_id, "test")
        try:
            return runtime.execute(command, lease=lease)
        finally:
            self.sessions.release(lease)

    def test_git_commit_succeeds_and_records_evidence(self) -> None:
        runtime, session_id = self._elevated_runtime()
        (self.repository / "sample.txt").write_text("after\n", encoding="utf-8")
        result = self._execute_on(
            runtime,
            self.command(
                "git.commit",
                {"message": "commit sample", "paths": ["sample.txt"]},
                key="commit-ok",
                session_id=session_id,
            ),
        )
        self.assertTrue(result.ok)
        self.assertTrue(result.data["committed"])
        self.assertTrue(result.data["commit_sha"])
        self.assertEqual(result.evidence[0].kind, "git_commit")
        status = runtime._git(["status", "--porcelain"], 60.0)["stdout"]
        self.assertNotIn("sample.txt", status)

    def test_git_commit_without_changes_fails_closed(self) -> None:
        runtime, session_id = self._elevated_runtime()
        first = self._execute_on(
            runtime,
            self.command(
                "git.commit",
                {"message": "initial", "paths": ["sample.txt"]},
                key="initial-commit",
                session_id=session_id,
            ),
        )
        self.assertTrue(first.data["committed"])
        second = self._execute_on(
            runtime,
            self.command(
                "git.commit",
                {"message": "no changes", "paths": ["sample.txt"]},
                key="no-change-commit",
                session_id=session_id,
            ),
        )
        self.assertFalse(second.ok)
        self.assertFalse(second.data["committed"])

    def test_git_commit_rejects_path_escape_before_git(self) -> None:
        runtime, session_id = self._elevated_runtime()
        with self.assertRaises(InvalidPath):
            self._execute_on(
                runtime,
                self.command(
                    "git.commit",
                    {"message": "escape", "paths": ["../secret"]},
                    key="escape-commit",
                    session_id=session_id,
                ),
            )
        self.assertNotIn(
            "escape-commit",
            self.sessions.load(session_id).idempotency,
        )

    def test_git_commit_rejects_empty_message(self) -> None:
        runtime, session_id = self._elevated_runtime()
        with self.assertRaisesRegex(InvalidCommand, "empty"):
            self._execute_on(
                runtime,
                self.command(
                    "git.commit",
                    {"message": "   ", "paths": ["sample.txt"]},
                    key="empty-message-commit",
                    session_id=session_id,
                ),
            )

    def test_git_commit_replays_safely_without_second_commit(self) -> None:
        runtime, session_id = self._elevated_runtime()
        (self.repository / "sample.txt").write_text("replay\n", encoding="utf-8")
        first = self._execute_on(
            runtime,
            self.command(
                "git.commit",
                {"message": "replay commit", "paths": ["sample.txt"]},
                key="replay-commit",
                session_id=session_id,
            ),
        )
        self.assertTrue(first.ok)
        self.assertTrue(first.data["committed"])
        first_sha = first.data["commit_sha"]
        self.assertTrue(first_sha)
        second = self._execute_on(
            runtime,
            self.command(
                "git.commit",
                {"message": "replay commit", "paths": ["sample.txt"]},
                key="replay-commit",
                session_id=session_id,
            ),
        )
        self.assertTrue(second.idempotent_replay)
        self.assertEqual(second.data["commit_sha"], first_sha)
        count = runtime._git(["rev-list", "--count", "HEAD"], 60.0)["stdout"].strip()
        self.assertEqual(count, "1")


if __name__ == "__main__":
    unittest.main()
