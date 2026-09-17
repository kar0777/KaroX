from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.models import AccessProfile
from karox.release_hot_actions import ReleaseActionError, execute_release_action
from karox.sessions import SessionStore
from karox.task_state import FactOrigin, TaskFact, TaskStateStore, fact
from karox.workspace_worker import execute_browser_command


class GuardedPrereleaseHotActionTests(unittest.TestCase):
    HEAD = "a" * 40
    MAIN_SHA = "b" * 40
    MERGED_HEAD = "c" * 40
    TAG = "v5.0.0rc1"
    RUN_ID = 35258998564
    WORKSTREAM = "release-test"

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        initialize_git_repository(self.repo)
        (self.repo / ".github" / "workflows").mkdir(parents=True)
        (self.repo / ".github" / "workflows" / "prerelease.yml").write_text(
            "name: prerelease\n", encoding="utf-8"
        )
        (self.repo / f"RELEASE_NOTES_{self.TAG}.md").write_text("# candidate\n", encoding="utf-8")
        self.sessions = SessionStore(root / "sessions")
        self.sessions.create(
            self.repo,
            "Publish the verified pre-release",
            AccessProfile.ELEVATED,
            branch="feat/release",
            session_id="session-release",
        )
        self.states = TaskStateStore(self.sessions)
        self.states.bootstrap(
            "session-release",
            {
                "objective": fact("Publish the verified pre-release", FactOrigin.VERIFIED, "session.task"),
                "repository": fact(str(self.repo.resolve()), FactOrigin.VERIFIED, "session.repository"),
                "branch": fact("feat/release", FactOrigin.OBSERVED, "git.branch"),
                "repository_revision": fact(self.HEAD, FactOrigin.OBSERVED, "git.revision"),
                "connection_profile": fact("chatgpt-web", FactOrigin.VERIFIED, "bridge.profile"),
                "access_profile": fact("elevated", FactOrigin.VERIFIED, "session.access_profile"),
            },
            workstream_id=self.WORKSTREAM,
        )
        self.runtime = SimpleNamespace(
            repository=self.repo,
            sessions=self.sessions,
            session_id="session-release",
            _access_profile=AccessProfile.ELEVATED,
        )
        self.calls: list[list[str]] = []
        self.merged = False
        self.remote_tag_target: str | None = None

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _fake_run(self, _repo: Path, argv: list[str], _deadline: float):
        self.calls.append(list(argv))
        stdout = ""
        returncode = 0
        current_head = self.MERGED_HEAD if self.merged else self.HEAD
        if argv[:3] == ["git", "rev-parse", "HEAD"]:
            stdout = current_head + "\n"
        elif argv[:4] == ["git", "remote", "get-url", "origin"]:
            stdout = "https://github.com/example/KaroX.git\n"
        elif argv[:2] == ["git", "remote"]:
            stdout = "origin\n"
        elif argv[:3] == ["git", "ls-remote", "--heads"]:
            stdout = f"{self.MAIN_SHA}\trefs/heads/main\n"
        elif argv[:3] == ["git", "ls-remote", "--tags"]:
            if self.remote_tag_target is not None:
                stdout = f"{self.remote_tag_target}\trefs/tags/{self.TAG}\n"
        elif argv[:3] == ["git", "merge-base", "--is-ancestor"]:
            returncode = 0 if self.merged else 1
        elif argv[:2] == ["git", "merge"]:
            self.merged = True
            stdout = "Merge made by the 'ours' strategy.\n"
        elif argv[:3] == ["git", "rev-list", "--parents"]:
            stdout = f"{self.MERGED_HEAD} {self.HEAD} {self.MAIN_SHA}\n"
        elif argv and argv[0] == "gh":
            stdout = json.dumps(
                {
                    "headSha": current_head,
                    "status": "completed",
                    "conclusion": "success",
                    "event": "push",
                }
            )
        elif argv[:2] == ["git", "push"]:
            self.remote_tag_target = current_head
            stdout = "Done\n"
        return subprocess.CompletedProcess(argv, returncode, stdout, "")

    def _payload(self) -> dict[str, object]:
        return {
            "tag": self.TAG,
            "remote": "origin",
            "ci_run_id": self.RUN_ID,
            "workstream_id": self.WORKSTREAM,
        }

    def _approve(self, *, tag: str | None = None) -> None:
        state = self.states.load("session-release", workstream_id=self.WORKSTREAM)
        self.states.checkpoint(
            "session-release",
            {
                "chat_user_approval": TaskFact(
                    {
                        "action_kind": "release.publish",
                        "approved": True,
                        "remote": "origin",
                        "tag": tag or self.TAG,
                        "head": self.HEAD,
                        "ci_run_id": self.RUN_ID,
                    },
                    FactOrigin.REPORTED_BY_AGENT,
                    evidence=("user.chat.explicit_approval",),
                )
            },
            expected_revision=state.revision,
            workstream_id=self.WORKSTREAM,
        )

    def test_stable_browser_command_routes_guarded_release_actions(self) -> None:
        with patch("importlib.reload", side_effect=lambda module: module):
            with patch(
                "karox.release_hot_actions.execute_release_action",
                return_value={"ready": True, "published": False},
            ) as handler:
                result = execute_browser_command(
                    self.runtime,
                    {"action": "release.check_prerelease", "payload": self._payload()},
                    30.0,
                )
        self.assertEqual(result["action"], "release.check_prerelease")
        self.assertTrue(result["ready"])
        handler.assert_called_once_with(
            self.runtime,
            "release.check_prerelease",
            self._payload(),
            30.0,
        )

    def test_integrate_main_lineage_creates_exact_ours_merge(self) -> None:
        payload = {
            "remote": "origin",
            "source_branch": "main",
            "source_sha": self.MAIN_SHA,
        }
        with patch("karox.release_hot_actions._run", side_effect=self._fake_run):
            result = execute_release_action(
                self.runtime, "release.integrate_main_lineage", payload, 30.0
            )
        self.assertTrue(result["integrated"])
        self.assertTrue(result["merge_created"])
        self.assertEqual(result["head"], self.MERGED_HEAD)
        self.assertIn(
            [
                "git",
                "merge",
                "--no-ff",
                "-s",
                "ours",
                self.MAIN_SHA,
                "-m",
                "release: integrate origin/main lineage for KaroX 5.0.0rc1",
            ],
            self.calls,
        )
        with patch("karox.release_hot_actions._run", side_effect=self._fake_run):
            replay = execute_release_action(
                self.runtime, "release.integrate_main_lineage", payload, 30.0
            )
        self.assertTrue(replay["reconciled"])
        self.assertFalse(replay["merge_created"])

    def test_check_prerelease_is_read_only_and_requires_exact_runtime_tag(self) -> None:
        self.runtime._access_profile = AccessProfile.WORKSPACE_WRITE
        with self.assertRaises(ReleaseActionError):
            execute_release_action(
                self.runtime, "release.check_prerelease", self._payload(), 30.0
            )
        self.runtime._access_profile = AccessProfile.ELEVATED
        with patch("karox.release_hot_actions._run", side_effect=self._fake_run):
            result = execute_release_action(
                self.runtime, "release.check_prerelease", self._payload(), 30.0
            )
            self.assertTrue(result["ready"])
            self.assertFalse(result["published"])
            self.assertFalse(any(call[:2] == ["git", "push"] for call in self.calls))
            with self.assertRaises(ReleaseActionError):
                execute_release_action(
                    self.runtime,
                    "release.check_prerelease",
                    {**self._payload(), "tag": "v5.0.0"},
                    30.0,
                )

    def test_publish_consumes_one_exact_chat_approval_before_tag_push(self) -> None:
        self._approve()
        with patch("karox.release_hot_actions._run", side_effect=self._fake_run):
            result = execute_release_action(
                self.runtime, "release.publish_prerelease", self._payload(), 30.0
            )
        self.assertTrue(result["tag_pushed"])
        self.assertIn(
            ["git", "push", "--porcelain", "origin", f"HEAD:refs/tags/{self.TAG}"],
            self.calls,
        )
        consumed = self.states.load(
            "session-release", workstream_id=self.WORKSTREAM
        ).facts["chat_user_approval"]
        self.assertEqual(consumed.origin, FactOrigin.VERIFIED)
        self.assertFalse(consumed.value["approved"])
        self.assertTrue(consumed.value["consumed"])
        pushes_before_replay = sum(call[:2] == ["git", "push"] for call in self.calls)
        with patch("karox.release_hot_actions._run", side_effect=self._fake_run):
            replay = execute_release_action(
                self.runtime, "release.publish_prerelease", self._payload(), 30.0
            )
        self.assertTrue(replay["published"])
        self.assertTrue(replay["reconciled"])
        self.assertFalse(replay["tag_pushed"])
        pushes_after_replay = sum(call[:2] == ["git", "push"] for call in self.calls)
        self.assertEqual(pushes_after_replay, pushes_before_replay)

    def test_mismatched_chat_approval_never_pushes(self) -> None:
        self._approve(tag="v5.0.0rc2")
        with patch("karox.release_hot_actions._run", side_effect=self._fake_run):
            with self.assertRaises(ReleaseActionError):
                execute_release_action(
                    self.runtime, "release.publish_prerelease", self._payload(), 30.0
                )
        self.assertFalse(any(call[:2] == ["git", "push"] for call in self.calls))

    def test_failed_ci_is_refused_before_approval_is_consumed(self) -> None:
        self._approve()

        def failed_ci(repo: Path, argv: list[str], deadline: float):
            result = self._fake_run(repo, argv, deadline)
            if argv and argv[0] == "gh":
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps(
                        {
                            "headSha": self.HEAD,
                            "status": "completed",
                            "conclusion": "failure",
                            "event": "push",
                        }
                    ),
                    "",
                )
            return result

        with patch("karox.release_hot_actions._run", side_effect=failed_ci):
            with self.assertRaises(ReleaseActionError):
                execute_release_action(
                    self.runtime, "release.publish_prerelease", self._payload(), 30.0
                )
        approval = self.states.load(
            "session-release", workstream_id=self.WORKSTREAM
        ).facts["chat_user_approval"]
        self.assertEqual(approval.origin, FactOrigin.REPORTED_BY_AGENT)
        self.assertTrue(approval.value["approved"])
        self.assertFalse(any(call[:2] == ["git", "push"] for call in self.calls))


if __name__ == "__main__":
    unittest.main()
