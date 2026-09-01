from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.autonomy_runtime import (
    INTELLIGENCE_LIST,
    ORCHESTRATE_CONTROL,
    ORCHESTRATE_PLAN,
    ORCHESTRATE_RECIPES,
    ORCHESTRATE_START,
    ORCHESTRATE_STATUS,
    AutonomyRuntime,
)
from karox.hosted_bridge import AUTONOMY_TOOL_NAMES
from karox.models import AccessProfile, Origin, OriginKind
from karox.sessions import SessionStore


class HostedOrchestrationAutonomyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.repo = root / "repo"
        initialize_git_repository(self.repo)
        self.sessions = SessionStore(root / "sessions")
        self.sessions.create(
            self.repo,
            "Exercise read-only hosted orchestration",
            AccessProfile.WORKSPACE_WRITE,
            branch="main",
            session_id="hosted-orchestration",
        )
        self.runtime = AutonomyRuntime(
            self.repo,
            self.sessions,
            "hosted-orchestration",
            (INTELLIGENCE_LIST, ORCHESTRATE_RECIPES, ORCHESTRATE_PLAN),
            access_profile=AccessProfile.WORKSPACE_WRITE,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-hosted-orchestration"),
            connection_profile="test-hosted",
        )

    def tearDown(self) -> None:
        self.runtime.close()
        self.temp.cleanup()

    def test_catalog_exposes_only_read_only_orchestration_surface(self) -> None:
        descriptors = {item.name: item for item in self.runtime.descriptors()}
        expected = {INTELLIGENCE_LIST, ORCHESTRATE_RECIPES, ORCHESTRATE_PLAN}
        self.assertEqual(set(descriptors), expected)
        self.assertTrue(expected.issubset(AUTONOMY_TOOL_NAMES))
        self.assertTrue(all(item.read_only for item in descriptors.values()))
        # Execution/control are now valid autonomy tools, but this deliberately
        # read-only runtime did not select them and therefore cannot invoke them.
        self.assertTrue(
            {ORCHESTRATE_START, ORCHESTRATE_STATUS, ORCHESTRATE_CONTROL}.issubset(
                AUTONOMY_TOOL_NAMES
            )
        )
        self.assertTrue(
            {ORCHESTRATE_START, ORCHESTRATE_STATUS, ORCHESTRATE_CONTROL}.isdisjoint(
                descriptors
            )
        )
        self.assertNotIn("karox.orchestrate.run", AUTONOMY_TOOL_NAMES)

    def test_intelligence_list_returns_inventory_without_execution(self) -> None:
        endpoint = mock.Mock()
        endpoint.to_dict.return_value = {
            "endpoint_id": "subscription:codex",
            "display_name": "Codex subscription",
            "source_kind": "subscription",
            "target_id": "codex",
            "enabled": True,
        }
        pool = mock.Mock()
        pool.list.return_value = [endpoint]
        with mock.patch("karox.intelligence_pool.IntelligencePool", return_value=pool):
            result = self.runtime.execute(
                INTELLIGENCE_LIST,
                {"include_disabled": False},
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(
            result["endpoints"][0]["endpoint_id"],
            "subscription:codex",
        )
        pool.list.assert_called_once_with(include_disabled=False)

    def test_recipe_listing_is_data_only(self) -> None:
        recipe = mock.Mock()
        recipe.to_dict.return_value = {
            "name": "feature",
            "steps": [{"step_id": "plan", "role": "planner"}],
        }
        registry = mock.Mock()
        registry.list.return_value = [recipe]
        with mock.patch("karox.recipe_registry.RecipeRegistry", return_value=registry):
            result = self.runtime.execute(ORCHESTRATE_RECIPES, {})

        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["recipes"][0]["name"], "feature")
        registry.list.assert_called_once_with()

    def test_plan_uses_pool_router_quota_and_effort_without_execution(self) -> None:
        fake_pool = mock.Mock(name="pool")
        fake_telemetry = mock.Mock(name="telemetry")
        fake_quota = mock.Mock(name="quota")
        fake_router = mock.Mock(name="router")
        fake_plan = mock.Mock(name="plan")
        fake_plan.to_dict.return_value = {
            "run_id": "run-hosted-test",
            "objective": "Review auth safely",
            "steps": [],
        }

        with (
            mock.patch(
                "karox.intelligence_pool.IntelligencePool",
                return_value=fake_pool,
            ),
            mock.patch(
                "karox.orchestration_routing.RoutingTelemetry",
                return_value=fake_telemetry,
            ),
            mock.patch("karox.quota_brain.QuotaBrain", return_value=fake_quota),
            mock.patch(
                "karox.orchestration_routing.VerifiedSmartRouter",
                return_value=fake_router,
            ) as router_cls,
            mock.patch("karox.orchestrator.Orchestrator") as orchestrator_cls,
        ):
            orchestrator_cls.return_value.plan.return_value = fake_plan
            result = self.runtime.execute(
                ORCHESTRATE_PLAN,
                {
                    "objective": " Review auth safely ",
                    "recipe": "feature",
                    "preset": "balanced",
                    "risk": "high",
                    "orchestrator_endpoint_id": "api:openai:gpt-test",
                    "role_assignments": {"reviewer": "subscription:claude"},
                    "effort_assignments": {"reviewer": "high"},
                    "run_id": "run-hosted-test",
                },
            )

        self.assertTrue(result["ok"])
        self.assertFalse(result["executed"])
        self.assertEqual(result["plan"]["run_id"], "run-hosted-test")
        router_cls.assert_called_once_with(
            pool=fake_pool,
            telemetry=fake_telemetry,
            quota_brain=fake_quota,
        )
        orchestrator_cls.assert_called_once_with(
            pool=fake_pool,
            router=fake_router,
            telemetry=fake_telemetry,
        )
        plan_call = orchestrator_cls.return_value.plan.call_args.kwargs
        self.assertEqual(plan_call["objective"], "Review auth safely")
        self.assertEqual(plan_call["risk_level"].value, "high")
        self.assertEqual(
            plan_call["role_assignments"],
            {"reviewer": "subscription:claude"},
        )
        self.assertEqual(
            plan_call["effort_assignments"],
            {"reviewer": "high"},
        )

    def test_plan_failure_is_structured_and_stays_read_only(self) -> None:
        fake_pool = mock.Mock(name="pool")
        fake_telemetry = mock.Mock(name="telemetry")
        with (
            mock.patch(
                "karox.intelligence_pool.IntelligencePool",
                return_value=fake_pool,
            ),
            mock.patch(
                "karox.orchestration_routing.RoutingTelemetry",
                return_value=fake_telemetry,
            ),
            mock.patch("karox.quota_brain.QuotaBrain"),
            mock.patch("karox.orchestration_routing.VerifiedSmartRouter"),
            mock.patch("karox.orchestrator.Orchestrator") as orchestrator_cls,
        ):
            from karox.orchestrator import OrchestrationError

            orchestrator_cls.return_value.plan.side_effect = OrchestrationError(
                "no eligible endpoint"
            )
            result = self.runtime.execute(
                ORCHESTRATE_PLAN,
                {"objective": "Plan only"},
            )

        self.assertFalse(result["ok"])
        self.assertEqual(result["error_code"], "orchestration_plan_rejected")
        self.assertIn("no eligible endpoint", result["error"])


if __name__ == "__main__":
    unittest.main()
