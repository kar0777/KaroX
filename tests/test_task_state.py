from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.models import AccessProfile
from karox.sessions import SessionError, SessionStore
from karox.task_state import FactOrigin, TaskFact, TaskStateStore, fact


class TaskStateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        initialize_git_repository(self.repo)
        self.sessions = SessionStore(root / "sessions")
        self.sessions.create(
            self.repo,
            "Implement persistent task state",
            AccessProfile.WORKSPACE_WRITE,
            branch="feat/test",
            session_id="session-a",
        )
        self.store = TaskStateStore(self.sessions)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _baseline(self) -> dict[str, TaskFact]:
        return {
            "objective": fact(
                "Implement persistent task state",
                FactOrigin.VERIFIED,
                "session.task",
            ),
            "repository": fact(str(self.repo.resolve()), FactOrigin.VERIFIED, "session.repository"),
            "branch": fact("feat/test", FactOrigin.OBSERVED, "git.branch"),
            "repository_revision": fact("abc123", FactOrigin.OBSERVED, "git.head"),
            "connection_profile": fact("clickup-opus", FactOrigin.VERIFIED, "bridge.profile"),
            "access_profile": fact("workspace_write", FactOrigin.VERIFIED, "session.access_profile"),
            "completed_phases": fact([], FactOrigin.REPORTED_BY_AGENT),
            "current_phase": fact("D", FactOrigin.REPORTED_BY_AGENT),
            "next_safe_action": fact("run focused tests", FactOrigin.PENDING),
        }

    def test_bootstrap_persists_checksum_and_provenance(self) -> None:
        state = self.store.bootstrap("session-a", self._baseline())
        self.assertEqual(state.revision, 0)
        self.assertTrue(state.task_id.startswith("task-"))
        self.assertEqual(state.facts["branch"].origin, FactOrigin.OBSERVED)
        self.assertEqual(state.facts["access_profile"].origin, FactOrigin.VERIFIED)
        loaded = self.store.load("session-a")
        self.assertEqual(loaded.compact(), state.compact())
        raw = json.loads(self.store.path("session-a").read_text(encoding="utf-8"))
        self.assertEqual(len(raw["checksum"]), 64)

    def test_bootstrap_merges_fresh_verified_facts_without_replacing_task(self) -> None:
        first = self.store.bootstrap("session-a", self._baseline())
        second = self.store.bootstrap(
            "session-a",
            {
                "branch": fact("feat/new", FactOrigin.OBSERVED, "git.branch"),
                "files_inspected": fact(["src/karox/task_state.py"], FactOrigin.REPORTED_BY_AGENT),
            },
        )
        self.assertEqual(second.task_id, first.task_id)
        self.assertEqual(second.revision, 1)
        self.assertEqual(second.facts["branch"].value, "feat/new")
        self.assertEqual(second.facts["objective"].value, "Implement persistent task state")

    def test_checkpoint_is_atomic_and_revision_guarded(self) -> None:
        initial = self.store.bootstrap("session-a", self._baseline())
        current = self.store.checkpoint(
            "session-a",
            {
                "completed_phases": fact(["A", "B", "C", "D"], FactOrigin.REPORTED_BY_AGENT),
                "next_safe_action": fact("start phase E", FactOrigin.PENDING),
            },
            expected_revision=initial.revision,
        )
        self.assertEqual(current.revision, 1)
        self.assertIn("last_checkpoint_timestamp", current.facts)
        self.assertEqual(
            current.facts["last_checkpoint_timestamp"].origin,
            FactOrigin.VERIFIED,
        )
        with self.assertRaisesRegex(SessionError, "stale task state revision"):
            self.store.checkpoint(
                "session-a",
                {"current_phase": fact("E", FactOrigin.REPORTED_BY_AGENT)},
                expected_revision=initial.revision,
            )

    def test_checksum_tampering_is_rejected(self) -> None:
        self.store.bootstrap("session-a", self._baseline())
        path = self.store.path("session-a")
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["facts"]["branch"]["value"] = "tampered"
        path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaisesRegex(SessionError, "checksum mismatch"):
            self.store.load("session-a")

    def test_custom_fact_is_allowed_but_invalid_origin_is_rejected(self) -> None:
        self.store.bootstrap("session-a", self._baseline())
        state = self.store.checkpoint(
            "session-a",
            {"made_up": fact(True, FactOrigin.INFERRED)},
        )
        self.assertTrue(state.facts["made_up"].value)
        self.assertEqual(state.facts["made_up"].origin, FactOrigin.INFERRED)
        with self.assertRaisesRegex(SessionError, "invalid origin"):
            TaskFact.from_dict({"value": 1, "origin": "trusted_because_agent_said_so"})

    def test_reported_agent_fact_never_becomes_verified_on_resume(self) -> None:
        self.store.bootstrap(
            "session-a",
            {
                **self._baseline(),
                "current_blockers": fact(
                    ["full suite is green"],
                    FactOrigin.REPORTED_BY_AGENT,
                    "old-handoff",
                ),
            },
        )
        resumed = self.store.load("session-a")
        self.assertEqual(
            resumed.facts["current_blockers"].origin,
            FactOrigin.REPORTED_BY_AGENT,
        )
        self.assertNotEqual(
            resumed.facts["current_blockers"].origin,
            FactOrigin.VERIFIED,
        )

    def test_named_workstreams_are_independent_inside_one_session(self) -> None:
        frontend = self.store.bootstrap(
            "session-a",
            {**self._baseline(), "objective": fact("Frontend", FactOrigin.VERIFIED)},
            workstream_id="frontend",
        )
        backend = self.store.bootstrap(
            "session-a",
            {**self._baseline(), "objective": fact("Backend", FactOrigin.VERIFIED)},
            workstream_id="backend",
        )
        default = self.store.bootstrap("session-a", self._baseline())

        self.assertNotEqual(frontend.task_id, backend.task_id)
        self.assertNotEqual(frontend.task_id, default.task_id)
        self.assertEqual(
            self.store.load("session-a", workstream_id="frontend").facts["objective"].value,
            "Frontend",
        )
        self.assertEqual(
            self.store.load("session-a", workstream_id="backend").facts["objective"].value,
            "Backend",
        )
        self.assertEqual(self.store.list_workstreams("session-a"), ("backend", "frontend"))
        self.assertTrue(self.store.path("session-a", "frontend").is_file())
        self.assertTrue(self.store.path("session-a").is_file())

    def test_workstream_checkpoint_does_not_advance_a_sibling_revision(self) -> None:
        first = self.store.bootstrap(
            "session-a",
            {**self._baseline(), "objective": fact("One", FactOrigin.VERIFIED)},
            workstream_id="one",
        )
        sibling = self.store.bootstrap(
            "session-a",
            {**self._baseline(), "objective": fact("Two", FactOrigin.VERIFIED)},
            workstream_id="two",
        )
        advanced = self.store.checkpoint(
            "session-a",
            {"current_phase": fact("implementation", FactOrigin.REPORTED_BY_AGENT)},
            expected_revision=first.revision,
            workstream_id="one",
        )

        self.assertEqual(advanced.revision, first.revision + 1)
        self.assertEqual(
            self.store.load("session-a", workstream_id="two").revision,
            sibling.revision,
        )

    def test_workstream_project_binding_is_immutable_after_bootstrap(self) -> None:
        baseline = {
            **self._baseline(),
            "project_id": fact("karox-v5", FactOrigin.VERIFIED, "project.registry"),
        }
        first = self.store.bootstrap(
            "session-a",
            baseline,
            workstream_id="frontend",
        )
        self.assertEqual(first.facts["project_id"].value, "karox-v5")

        with self.assertRaisesRegex(SessionError, "project binding is immutable"):
            self.store.bootstrap(
                "session-a",
                {
                    **self._baseline(),
                    "project_id": fact("aqurium", FactOrigin.VERIFIED, "project.registry"),
                },
                workstream_id="frontend",
            )

        with self.assertRaisesRegex(SessionError, "system-verified"):
            self.store.checkpoint(
                "session-a",
                {"project_id": fact("aqurium", FactOrigin.REPORTED_BY_AGENT)},
                workstream_id="frontend",
            )

    def test_workstream_id_is_path_safe(self) -> None:
        with self.assertRaisesRegex(SessionError, "workstream_id"):
            self.store.bootstrap(
                "session-a",
                self._baseline(),
                workstream_id="../other-session",
            )


if __name__ == "__main__":
    unittest.main()
