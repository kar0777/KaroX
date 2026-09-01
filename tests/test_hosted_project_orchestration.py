from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _support import SRC  # noqa: F401

from karox.context_bus import ContextBus
from karox.hosted_project_orchestration import HostedProjectOrchestrationRuntime


class HostedProjectOrchestrationRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.bus = ContextBus("hosted-project-targets", path=Path(self.temp.name) / "bus.json")
        self.runtime = object.__new__(HostedProjectOrchestrationRuntime)
        self.runtime.context_bus = self.bus
        self.runtime.plan = SimpleNamespace(policy=SimpleNamespace(context_budget_chars=10000))
        self.runtime._known_context = {}
        self.runtime._paused = False
        self.runtime._stop_requested = False

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _step(step_id: str, role: str):
        return SimpleNamespace(
            step=SimpleNamespace(step_id=step_id, role=role),
            endpoint=SimpleNamespace(endpoint_id=f"api:{role}"),
        )

    def test_targeted_context_is_delivered_only_to_matching_role_or_step(self) -> None:
        self.bus.put_text(item_id="global", kind="task", content="global")
        self.bus.put_text(
            item_id="for-reviewer",
            kind="task",
            content="review this",
            tags=("target:reviewer",),
        )
        self.bus.put_text(
            item_id="for-implement",
            kind="task",
            content="implement this",
            tags=("target:implement",),
        )
        executor = SimpleNamespace(reuses_context_between_requests=False)

        reviewer = self.runtime._context_delta_for(self._step("review", "reviewer"), executor)
        implementer = self.runtime._context_delta_for(
            self._step("implement", "implementer"), executor
        )

        reviewer_ids = {item.item_id for item in reviewer.changed}
        implementer_ids = {item.item_id for item in implementer.changed}
        self.assertEqual(reviewer_ids, {"global", "for-reviewer"})
        self.assertEqual(implementer_ids, {"global", "for-implement"})

    def test_steer_command_is_persisted_with_target_tag(self) -> None:
        command = SimpleNamespace(
            command_type="steer",
            command_id="cmd1",
            target="reviewer",
            text="Check the OAuth boundary",
        )
        mission = mock.Mock()
        mission.pending_commands.return_value = [command]
        self.runtime.mission_control = mission

        result = self.runtime._consume_mobile_commands()

        self.assertIsNone(result)
        items = {item.item_id: item for item in self.bus.items()}
        self.assertIn("mobile-cmd1", items)
        self.assertIn("target:reviewer", items["mobile-cmd1"].tags)
        mission.consume.assert_called_once_with("cmd1")


if __name__ == "__main__":
    unittest.main()
