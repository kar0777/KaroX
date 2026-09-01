from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from karox import tui
from karox.effort import (
    effort_display_name,
    effort_summary,
    effort_user_summary,
)
from karox.intelligence_pool import IntelligenceEndpoint
from karox.map_service import render_preview, render_status
from karox.mission_control import AgentStatus, MissionSnapshot
from karox.registry import ModelRecord, ProviderRecord
from karox.tui_dashboard import EffortPickerScreen, ModePickerScreen, ModelPickerScreen


class HumanFirstEffortTests(unittest.TestCase):
    def test_effort_labels_explain_intent_without_hiding_canonical_level(self) -> None:
        self.assertEqual(effort_display_name("ultra", "en"), "ultra — maximum")
        self.assertEqual(effort_display_name("ultra", "ru"), "ultra — максимум")
        self.assertIn("standard", effort_display_name("medium", "en"))
        self.assertIn("обычно", effort_display_name("medium", "ru"))

    def test_default_effort_explanation_is_user_facing(self) -> None:
        text = effort_user_summary("ultra", "en")
        self.assertIn("Hardest work", text)
        for internal in ("96", "3600", "5/5", "rung", "max_steps"):
            self.assertNotIn(internal, text)

    def test_exact_effort_budget_remains_available_for_advanced_details(self) -> None:
        text = effort_summary("ultra", "en")
        self.assertIn("96 steps", text)
        self.assertIn("3600s", text)
        self.assertIn("5/5", text)


class HumanFirstMapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.status = {
            "exists": True,
            "level": "ultra",
            "fresh": True,
            "age_seconds": 123.4,
            "duration_ms": 9123.0,
            "files_scanned": 417,
            "validation": {"state": "VALID", "stale": []},
            "sources": {"state": "VALID", "stale": []},
        }
        self.preview = {
            "repository": "C:/private/project",
            "level": "ultra",
            "files_indexed": 417,
            "duration_range": {"seconds": [15.0, 300.0], "basis": "estimate"},
            "deep_inspection_files": [30, 320],
            "semantic_analysis": True,
            "git_history": True,
            "git_history_commits": 1000,
        }

    def test_map_status_default_is_result_first(self) -> None:
        text = render_status(self.status, "en")
        self.assertIn("Project map: ready", text)
        self.assertIn("ultra", text)
        self.assertIn("417 files", text)
        for internal in ("123.4", "9123", "Fact validation", "Sources:"):
            self.assertNotIn(internal, text)

    def test_map_status_details_preserve_provenance(self) -> None:
        text = render_status(self.status, "en", details=True)
        self.assertIn("Technical details", text)
        self.assertIn("Fact validation: VALID", text)
        self.assertIn("Sources: VALID", text)

    def test_map_preview_default_hides_engine_internals(self) -> None:
        text = render_preview(self.preview, "en")
        self.assertIn("Project map preview: ultra", text)
        self.assertIn("Scope: 417 files", text)
        self.assertIn("Expected time: 15.0–300.0s", text)
        for internal in (
            "C:/private/project",
            "Deep inspection",
            "Semantic analysis",
            "Git history",
            "1000 commits",
        ):
            self.assertNotIn(internal, text)

    def test_map_preview_details_preserve_advanced_information(self) -> None:
        text = render_preview(self.preview, "en", details=True)
        self.assertIn("C:/private/project", text)
        self.assertIn("Deep inspection", text)
        self.assertIn("Semantic analysis: yes", text)
        self.assertIn("Git history: yes, 1000 commits", text)


class HumanFirstHeaderTests(unittest.TestCase):
    def test_header_uses_human_localized_work_depth_label(self) -> None:
        russian = tui._header_line(
            repository="demo", model="provider/model", activity="", width=80, language="ru"
        )
        english = tui._header_line(
            repository="demo", model="provider/model", activity="", width=80, language="en"
        )
        self.assertIn("глубина auto", russian)
        self.assertNotIn("effort auto", russian)
        self.assertIn("effort auto", english)


class HumanFirstCommandPaletteTests(unittest.IsolatedAsyncioTestCase):
    async def test_palette_uses_human_language_and_localizes_russian_copy(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        app = tui.KaroXApp(root, language="ru")
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause(0.05)
            commands = list(app.get_system_commands(app.screen))
            by_title = {str(command.title): command for command in commands}
            self.assertIn("Глубина работы", by_title)
            self.assertIn("Использование и расходы", by_title)
            depth = by_title["Глубина работы"]
            description = str(getattr(depth, "help", getattr(depth, "description", "")))
            self.assertIn("Auto", description)
            self.assertNotIn("reasoning/work budget", description)
            self.assertNotIn("one hub for models and services", "\n".join(
                str(getattr(command, "help", getattr(command, "description", "")))
                for command in commands
            ))


class HumanFirstModePickerTests(unittest.IsolatedAsyncioTestCase):
    async def test_mode_picker_explains_behavior_in_plain_language(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        app = tui.KaroXApp(root, language="en")
        async with app.run_test(size=(100, 34)) as pilot:
            screen = ModePickerScreen("en", mode="build")
            app.push_screen(screen)
            await pilot.pause(0.05)

            options = screen.query_one("#mode-picker-list", tui.OptionList)
            self.assertEqual(
                [str(getattr(option, "id", "")) for option in options.options],
                ["build", "plan", "ideate"],
            )
            details = screen.query_one("#mode-picker-details", tui.Static)
            text = str(details.render())
            self.assertIn("change code", text)
            self.assertNotIn("production-code", text)
            self.assertNotIn("artifact", text.casefold())

            options.highlighted = 1
            screen._render_details()
            plan = str(details.render())
            self.assertIn("without changing code", plan)

    async def test_bare_mode_command_opens_picker_instead_of_printing_syntax(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        app = tui.KaroXApp(root, language="en")
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause(0.05)
            with patch.object(app, "action_mode") as action_mode:
                app._handle_command("/mode")
            action_mode.assert_called_once_with()


class HumanFirstEffortPickerTests(unittest.IsolatedAsyncioTestCase):
    async def test_picker_defaults_to_plain_language_and_d_toggles_exact_budget(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        app = tui.KaroXApp(root, language="en")
        async with app.run_test(size=(100, 34)) as pilot:
            screen = EffortPickerScreen("en", effort="ultra")
            app.push_screen(screen)
            await pilot.pause(0.05)

            options = screen.query_one("#effort-picker-list", tui.OptionList)
            self.assertEqual(
                [str(getattr(option, "id", "")) for option in options.options],
                ["auto", "low", "medium", "high", "extra-high", "ultra"],
            )
            option_text = str(getattr(options.options[-1], "prompt", ""))
            self.assertIn("ultra", option_text)
            self.assertIn("maximum", option_text)

            details = screen.query_one("#effort-picker-details", tui.Static)
            plain = str(details.render())
            self.assertIn("Hardest work", plain)
            self.assertNotIn("96 steps", plain)

            await pilot.press("d")
            await pilot.pause(0.05)
            advanced = str(details.render())
            self.assertIn("96 steps", advanced)
            self.assertIn("verification rung 5/5", advanced)


class HumanFirstModelPickerTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_picker_is_simple_until_details_are_requested(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        registry = Mock()
        registry.selected_model.return_value = ModelRecord(
            "openrouter",
            "strong-model",
            display_name="Strong Model",
            tools="true",
            vision="true",
            structured_output="true",
            streaming="true",
        )
        registry.providers.return_value = [
            ProviderRecord(
                provider_id="openrouter",
                adapter_kind="openai_compatible_chat",
                base_url="https://example.invalid/v1",
            )
        ]
        registry.models.return_value = [registry.selected_model.return_value]

        with patch("karox.tui_dashboard.ProviderRegistry", return_value=registry):
            app = tui.KaroXApp(root, language="en")
            async with app.run_test(size=(110, 36)) as pilot:
                screen = ModelPickerScreen("en")
                app.push_screen(screen)
                await pilot.pause(0.05)

                options = screen.query_one("#model-picker-list", tui.OptionList)
                simple = str(getattr(options.options[0], "prompt", ""))
                self.assertIn("strong-model", simple)
                self.assertIn("openrouter", simple)
                self.assertNotIn("TVJS", simple)
                self.assertEqual(
                    str(screen.query_one("#model-picker-details", tui.Static).render()),
                    "",
                )
                self.assertFalse(screen.show_advanced)

                await pilot.press("d")
                await pilot.pause(0.05)
                self.assertTrue(screen.show_advanced)
                advanced = str(getattr(options.options[0], "prompt", ""))
                self.assertIn("TVJS", advanced)
                details = str(screen.query_one("#model-picker-details", tui.Static).render())
                self.assertIn("Tools yes", details)
                self.assertIn("Vision yes", details)


class HumanFirstOrchestrationTuiTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _plan_payload() -> dict:
        return {
            "run_id": "run-internal",
            "policy": {"preset": "balanced"},
            "orchestrator_endpoint": {
                "endpoint_id": "api:provider:orch",
                "display_name": "Strong Orchestrator",
            },
            "steps": [
                {
                    "step": {"step_id": "implement", "role": "implementer"},
                    "endpoint": {
                        "endpoint_id": "sub:codex",
                        "display_name": "Codex subscription",
                    },
                    "effort_level": "high",
                }
            ],
        }

    async def test_tui_plan_output_is_team_first(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        app = tui.KaroXApp(root, language="en")
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause(0.05)
            payload = self._plan_payload()
            payload["delegation"] = {"proposal": {"assignments": {}}}
            with patch.object(app, "_write") as write:
                app._orchestration_finished("plan", 0, __import__("json").dumps(payload))
            text = str(write.call_args.args[0])
            self.assertIn("KaroX plan", text)
            self.assertIn("Implementer", text)
            self.assertIn("Codex subscription", text)
            self.assertIn("validated by KaroX", text)
            self.assertNotIn("api:provider:orch", text)
            self.assertNotIn("sub:codex", text)
            self.assertNotIn("effort high", text)
            self.assertNotIn("Run ID", text)

    async def test_tui_run_output_hides_raw_telemetry_but_links_mission(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        app = tui.KaroXApp(root, language="en")
        async with app.run_test(size=(110, 36)) as pilot:
            await pilot.pause(0.05)
            payload = {
                "plan": self._plan_payload(),
                "status": "passed",
                "total_tokens": 987654,
                "total_cost_usd": 1.5,
                "steps": [
                    {
                        "step_id": "implement",
                        "endpoint_id": "sub:codex",
                        "status": "passed",
                    }
                ],
                "savings_receipt": {
                    "savings": {"amount": 2.67, "currency": "USD", "evidence": "measured"},
                    "savings_percent": 64.0,
                },
            }
            with patch.object(app, "_write") as write:
                app._orchestration_finished("run", 0, __import__("json").dumps(payload))
            text = str(write.call_args.args[0])
            self.assertIn("KaroX: passed", text)
            self.assertIn("Implementer", text)
            self.assertIn("Incremental cost: $1.5000", text)
            self.assertIn("Measured saving: 64.0%", text)
            self.assertIn("/mission run-internal", text)
            self.assertNotIn("987654", text)
            self.assertNotIn("sub:codex", text)


class HumanFirstAgentsTuiTests(unittest.IsolatedAsyncioTestCase):
    async def test_agents_default_hides_internal_endpoint_ids(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        endpoint = IntelligenceEndpoint(
            endpoint_id="sub:codex",
            display_name="Codex",
            source_kind="subscription",
            roles=("implementer", "tester"),
            target_id="codex",
            already_paid=True,
        )
        pool = Mock()
        pool.list.return_value = [endpoint]
        with (
            patch("karox.intelligence_pool.IntelligencePool", return_value=pool),
            patch("karox.subscription_cli.discover_subscription_clis", return_value=[]),
        ):
            app = tui.KaroXApp(root, language="en")
            async with app.run_test(size=(110, 36)) as pilot:
                await pilot.pause(0.05)
                with patch.object(app, "_write") as write:
                    app._agents_command("")
                text = str(write.call_args.args[0])
                self.assertIn("Codex", text)
                self.assertIn("already paid", text)
                self.assertIn("Implementer", text)
                self.assertIn("Tester", text)
                self.assertNotIn("sub:codex", text)
                self.assertIn("/agents details", text)

    async def test_agents_details_preserve_exact_connection_identity(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        endpoint = IntelligenceEndpoint(
            endpoint_id="sub:codex",
            display_name="Codex",
            source_kind="subscription",
            roles=("implementer",),
            target_id="codex",
            already_paid=True,
        )
        pool = Mock()
        pool.list.return_value = [endpoint]
        with (
            patch("karox.intelligence_pool.IntelligencePool", return_value=pool),
            patch("karox.subscription_cli.discover_subscription_clis", return_value=[]),
        ):
            app = tui.KaroXApp(root, language="en")
            async with app.run_test(size=(110, 36)) as pilot:
                await pilot.pause(0.05)
                with patch.object(app, "_write") as write:
                    app._agents_command("details")
                text = str(write.call_args.args[0])
                self.assertIn("subscription", text)
                self.assertIn("sub:codex", text)


class HumanFirstMissionTuiTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _snapshot() -> MissionSnapshot:
        return MissionSnapshot(
            run_id="run-1",
            task_id="task-1",
            objective="Build the feature safely",
            recipe="feature",
            orchestrator_endpoint_id="sub:orch",
            status="running",
            agents=(
                AgentStatus(
                    step_id="implement",
                    role="implementer",
                    endpoint_id="sub:codex",
                    status="running",
                    activity="editing guarded files",
                ),
            ),
            actual_cost_usd=1.25,
            total_tokens=987654,
        )

    @staticmethod
    def _pool() -> Mock:
        endpoints = {
            "sub:orch": IntelligenceEndpoint(
                endpoint_id="sub:orch",
                display_name="Strong Orchestrator",
                source_kind="subscription",
                roles=("orchestrator",),
                target_id="orch",
                already_paid=True,
            ),
            "sub:codex": IntelligenceEndpoint(
                endpoint_id="sub:codex",
                display_name="Codex",
                source_kind="subscription",
                roles=("implementer",),
                target_id="codex",
                already_paid=True,
            ),
        }
        pool = Mock()
        pool.get.side_effect = endpoints.__getitem__
        return pool

    async def test_mission_default_is_task_first_and_hides_raw_telemetry(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = Mock()
        store.snapshot.return_value = self._snapshot()
        with (
            patch("karox.background_orchestration.BackgroundOrchestrationRegistry", return_value=Mock()),
            patch("karox.intelligence_pool.IntelligencePool", return_value=self._pool()),
            patch("karox.mission_control.MissionControlStore", return_value=store),
        ):
            app = tui.KaroXApp(root, language="en")
            async with app.run_test(size=(110, 36)) as pilot:
                await pilot.pause(0.05)
                with patch.object(app, "_write") as write:
                    app._mission_command("run-1")
                text = str(write.call_args.args[0])
                self.assertIn("Build the feature safely", text)
                self.assertIn("Strong Orchestrator", text)
                self.assertIn("Implementer", text)
                self.assertIn("Codex", text)
                self.assertIn("Incremental cost", text)
                self.assertIn("/mission details run-1", text)
                self.assertNotIn("987654", text)
                self.assertNotIn("sub:codex", text)
                self.assertNotIn("sub:orch", text)

    async def test_mission_details_restore_exact_runtime_identity(self) -> None:
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        store = Mock()
        store.snapshot.return_value = self._snapshot()
        with (
            patch("karox.background_orchestration.BackgroundOrchestrationRegistry", return_value=Mock()),
            patch("karox.intelligence_pool.IntelligencePool", return_value=self._pool()),
            patch("karox.mission_control.MissionControlStore", return_value=store),
        ):
            app = tui.KaroXApp(root, language="en")
            async with app.run_test(size=(110, 36)) as pilot:
                await pilot.pause(0.05)
                with patch.object(app, "_write") as write:
                    app._mission_command("details run-1")
                text = str(write.call_args.args[0])
                self.assertIn("Run ID", text)
                self.assertIn("987654", text)
                self.assertIn("sub:orch", text)
                self.assertIn("sub:codex", text)


if __name__ == "__main__":
    unittest.main()
