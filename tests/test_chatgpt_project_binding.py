from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.chatgpt_project_binding import (
    ChatGPTProjectBindingError,
    ChatGPTProjectBindingStore,
)
from karox.models import AccessProfile
from karox.sessions import SessionStore
from karox.task_state import FactOrigin, TaskStateStore, fact


class ChatGPTProjectBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        initialize_git_repository(self.repo)
        self.sessions = SessionStore(root / "sessions")
        self.sessions.create(
            self.repo,
            "Continue KaroX work",
            AccessProfile.WORKSPACE_WRITE,
            branch="main",
            session_id="session-project",
        )
        self.store = ChatGPTProjectBindingStore(self.sessions, "session-project")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_binding_is_durable_and_returns_project_instruction_capsule(self) -> None:
        first = self.store.bind("Ваня не смотри мои чаты")
        second = ChatGPTProjectBindingStore(
            self.sessions, "session-project"
        ).bind("Ваня не смотри мои чаты")

        self.assertEqual(first.binding_id, second.binding_id)
        self.assertIn("Ваня не смотри мои чаты", first.instruction_capsule())
        self.assertIn(first.binding_id, first.instruction_capsule())
        self.assertIn("not a secret", first.instruction_capsule())

    def test_binding_mismatch_is_rejected(self) -> None:
        self.store.bind("Ваня не смотри мои чаты")
        with self.assertRaises(ChatGPTProjectBindingError):
            self.store.compact_snapshot("kxp_wrong-project-binding-value")

    def test_compact_snapshot_includes_durable_session_and_parallel_workstreams(self) -> None:
        binding = self.store.bind("Ваня не смотри мои чаты")
        tasks = TaskStateStore(self.sessions)
        tasks.bootstrap(
            "session-project",
            {
                "objective": fact("Audit frontend", FactOrigin.REPORTED_BY_AGENT),
                "project_id": fact("main", FactOrigin.VERIFIED),
            },
            workstream_id="frontend",
        )
        tasks.bootstrap(
            "session-project",
            {
                "objective": fact("Run tests", FactOrigin.REPORTED_BY_AGENT),
                "project_id": fact("main", FactOrigin.VERIFIED),
            },
            workstream_id="tests",
        )

        snapshot = self.store.compact_snapshot(binding.binding_id)

        self.assertTrue(snapshot["ok"])
        self.assertEqual(
            snapshot["chatgpt_project"]["name"], "Ваня не смотри мои чаты"
        )
        self.assertFalse(snapshot["chatgpt_project"]["binding_is_authentication"])
        self.assertEqual(snapshot["session"]["session_id"], "session-project")
        self.assertEqual(
            {item["workstream_id"] for item in snapshot["workstreams"]},
            {"frontend", "tests"},
        )
        self.assertIn("do not replay", snapshot["continuation"]["instruction"])

    def test_compact_snapshot_hard_caps_many_workstreams(self) -> None:
        binding = self.store.bind("Large Project")
        tasks = TaskStateStore(self.sessions)
        for index in range(30):
            tasks.bootstrap(
                "session-project",
                {
                    "objective": fact(
                        f"lane-{index}:" + ("x" * 5000),
                        FactOrigin.REPORTED_BY_AGENT,
                    ),
                    "summary": fact("y" * 5000, FactOrigin.REPORTED_BY_AGENT),
                    "next_action": fact("z" * 5000, FactOrigin.PENDING),
                },
                workstream_id=f"lane-{index:02d}",
            )

        snapshot = self.store.compact_snapshot(binding.binding_id)
        encoded = json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode("utf-8")

        self.assertLessEqual(len(encoded), 24 * 1024)
        self.assertEqual(snapshot["continuation"]["workstream_count"], 30)
        self.assertEqual(
            snapshot["continuation"]["workstreams_returned"],
            len(snapshot["workstreams"]),
        )
        self.assertTrue(snapshot["continuation"]["workstreams_truncated"])

    def test_rotate_is_explicit_when_project_name_changes(self) -> None:
        original = self.store.bind("Project A")
        with self.assertRaises(ChatGPTProjectBindingError):
            self.store.bind("Project B")
        rotated = self.store.bind("Project B", rotate=True)
        self.assertNotEqual(original.binding_id, rotated.binding_id)
        self.assertEqual(rotated.project_name, "Project B")
        with self.assertRaises(ChatGPTProjectBindingError):
            self.store.require(original.binding_id)


if __name__ == "__main__":
    unittest.main()
