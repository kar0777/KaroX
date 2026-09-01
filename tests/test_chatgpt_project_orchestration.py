from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.autonomy_runtime import (
    CHATGPT_PROJECT_BIND,
    CHATGPT_PROJECT_RESUME,
    ORCHESTRATE_CONTROL,
    ORCHESTRATE_START,
    ORCHESTRATE_STATUS,
    AutonomyRuntime,
)
from karox.hosted_bridge import HostedBridgeAccessDenied
from karox.models import AccessProfile, Origin, OriginKind
from karox.sessions import SessionStore
from karox.web_bridge_launcher import DEFAULT_WEB_TOOLS, WRITE_WEB_TOOLS


class ChatGPTProjectOrchestrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        initialize_git_repository(self.repo)
        self.sessions = SessionStore(root / "sessions")
        self.sessions.create(
            self.repo,
            "Hosted subagent orchestration",
            AccessProfile.WORKSPACE_WRITE,
            branch="main",
            session_id="chatgpt-project-orch",
        )
        self.runtime = AutonomyRuntime(
            self.repo,
            self.sessions,
            "chatgpt-project-orch",
            (
                CHATGPT_PROJECT_BIND,
                CHATGPT_PROJECT_RESUME,
                ORCHESTRATE_START,
                ORCHESTRATE_STATUS,
                ORCHESTRATE_CONTROL,
            ),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "chatgpt-web-project-orch"),
            connection_profile="chatgpt-web",
            client_kind="chatgpt-web",
            verification_commands=(("python", "-m", "pytest", "-q"),),
        )
        self.binding = self.runtime.execute(
            CHATGPT_PROJECT_BIND,
            {"project_name": "Ваня не смотри мои чаты"},
        )["binding"]

    def tearDown(self) -> None:
        self.runtime.close()
        self.temp.cleanup()

    @staticmethod
    def _selection(run_id: str):
        orchestrator = SimpleNamespace(endpoint_id="api:orchestrator")
        implement = SimpleNamespace(
            step=SimpleNamespace(step_id="implement", role="implementer"),
            endpoint=SimpleNamespace(endpoint_id="api:implementer"),
            effort_level="high",
        )
        review = SimpleNamespace(
            step=SimpleNamespace(step_id="review", role="reviewer"),
            endpoint=SimpleNamespace(endpoint_id="api:reviewer"),
            effort_level="medium",
        )
        plan = SimpleNamespace(
            run_id=run_id,
            orchestrator_endpoint=orchestrator,
            steps=(implement, review),
        )
        return SimpleNamespace(plan=plan)

    def test_pending_start_retry_recovers_owned_run_without_relaunch(self) -> None:
        key = "retry-start-after-disconnect"
        run_id = self.runtime._hosted_run_id(
            "chatgpt-project-orch",
            None,
            stable_key=key,
        )
        record = SimpleNamespace(run_id=run_id)
        registry = mock.Mock()
        registry.view.return_value = SimpleNamespace()
        with mock.patch.object(
            self.runtime,
            "_require_owned_orchestration_run",
            return_value=(registry, record),
        ), mock.patch.object(
            self.runtime,
            "_compact_orchestration_view",
            return_value={"run_id": run_id, "status": "running", "agents": []},
        ), mock.patch(
            "karox.orchestration_planning.build_orchestration_plan"
        ) as planner:
            result = self.runtime._orchestration_start(
                {
                    "binding": self.binding,
                    "objective": "finish the interrupted run",
                },
                key,
                reconcile_pending=True,
            )

        self.assertTrue(result["ok"])
        self.assertTrue(result["recovered_pending_start"])
        self.assertEqual(result["run_id"], run_id)
        planner.assert_not_called()

    def test_tools_ship_in_the_correct_default_capability_tiers(self) -> None:
        self.assertIn(ORCHESTRATE_STATUS, DEFAULT_WEB_TOOLS)
        self.assertIn(ORCHESTRATE_START, WRITE_WEB_TOOLS)
        self.assertIn(ORCHESTRATE_CONTROL, WRITE_WEB_TOOLS)
        self.assertNotIn(ORCHESTRATE_START, DEFAULT_WEB_TOOLS)
        self.assertNotIn(ORCHESTRATE_CONTROL, DEFAULT_WEB_TOOLS)

    def test_start_preflights_pins_routes_and_forces_isolated_implementers(self) -> None:
        fake_registry = mock.Mock()
        fake_registry.start.return_value = SimpleNamespace(run_id="web-owned-run")
        fake_mission = mock.Mock()

        with (
            mock.patch(
                "karox.orchestration_planning.build_orchestration_plan",
                side_effect=lambda **kwargs: self._selection(kwargs["run_id"]),
            ) as planner,
            mock.patch("karox.orchestration_cli._validate_cli_execution_plan") as validate,
            mock.patch(
                "karox.background_orchestration.BackgroundOrchestrationRegistry",
                return_value=fake_registry,
            ),
            mock.patch(
                "karox.mission_control.MissionControlStore",
                return_value=fake_mission,
            ),
        ):
            result = self.runtime.execute(
                ORCHESTRATE_START,
                {
                    "binding": self.binding,
                    "objective": "Audit and improve the project",
                    "run_id": "audit",
                    "max_steps": 40,
                    "max_seconds": 900,
                },
                idempotency_key="start-audit-1",
            )

        self.assertTrue(result["ok"])
        planner.assert_called_once()
        self.assertFalse(planner.call_args.kwargs["delegate_workers"])
        validate.assert_called_once()
        self.assertTrue(validate.call_args.kwargs["isolate_implementers"])
        fake_mission.bind_owner.assert_called_once()

        start_kwargs = fake_registry.start.call_args.kwargs
        argv = start_kwargs["cli_argv"]
        self.assertIn("--no-delegate-workers", argv)
        self.assertIn("--isolate-implementers", argv)
        self.assertIn("--hosted-project-run", argv)
        self.assertIn("implement=api:implementer", argv)
        self.assertIn("review=api:reviewer", argv)
        self.assertIn("orchestrator=api:orchestrator", argv)
        self.assertIn('["python","-m","pytest","-q"]', argv)
        self.assertTrue(result["guards"]["routing_pinned_after_preflight"])
        self.assertTrue(result["guards"]["implementers_isolated"])

    def test_control_rejects_unknown_target_after_snapshot_exists(self) -> None:
        record = SimpleNamespace(run_id="web-owned-run")
        snapshot = SimpleNamespace(
            agents=(
                SimpleNamespace(step_id="implement", role="implementer"),
                SimpleNamespace(step_id="review", role="reviewer"),
            )
        )
        registry = mock.Mock()
        registry.view.return_value = SimpleNamespace(
            snapshot=snapshot,
            alive=True,
            identity_proven=True,
        )
        with mock.patch.object(
            self.runtime,
            "_require_owned_orchestration_run",
            return_value=(registry, record),
        ):
            with self.assertRaises(HostedBridgeAccessDenied):
                self.runtime.execute(
                    ORCHESTRATE_CONTROL,
                    {
                        "binding": self.binding,
                        "run_id": record.run_id,
                        "command": "steer",
                        "target": "does-not-exist",
                        "text": "check OAuth",
                    },
                    idempotency_key="bad-steer-1",
                )

        registry.request.assert_not_called()

    def test_control_rejects_dead_or_unproven_owned_process(self) -> None:
        record = SimpleNamespace(run_id="web-owned-run")
        for suffix, alive, identity_proven in (
            ("dead", False, False),
            ("reused-pid", True, False),
        ):
            with self.subTest(suffix=suffix):
                registry = mock.Mock()
                registry.view.return_value = SimpleNamespace(
                    snapshot=None,
                    alive=alive,
                    identity_proven=identity_proven,
                )
                with mock.patch.object(
                    self.runtime,
                    "_require_owned_orchestration_run",
                    return_value=(registry, record),
                ):
                    with self.assertRaises(HostedBridgeAccessDenied):
                        self.runtime.execute(
                            ORCHESTRATE_CONTROL,
                            {
                                "binding": self.binding,
                                "run_id": record.run_id,
                                "command": "pause",
                            },
                            idempotency_key=f"identity-{suffix}",
                        )
                registry.request.assert_not_called()

    def test_project_resume_includes_compact_owned_subagent_runs(self) -> None:
        compact = [{"run_id": "web-owned-run", "status": "running", "agents": []}]
        with mock.patch.object(
            self.runtime, "_owned_orchestration_runs", return_value=compact
        ):
            result = self.runtime.execute(
                CHATGPT_PROJECT_RESUME,
                {"binding": self.binding},
            )

        self.assertEqual(result["orchestration_runs"], compact)
        self.assertEqual(result["continuation"]["orchestration_run_count"], 1)


if __name__ == "__main__":
    unittest.main()
