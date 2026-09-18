from __future__ import annotations

from _unittest_compat import enter_context

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox import tui
from karox.models import AccessProfile
from karox.provider_presets import provider_preset
from karox.registry import ModelRecord, ProviderRecord
from karox.sessions import SessionStore


class CommandUxV5Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.root = Path(enter_context(self, tempfile.TemporaryDirectory()))
        enter_context(self, patch.object(tui, "_load_language", return_value="en"))
        enter_context(self, patch.object(tui, "session_dir", lambda: self.root))

    async def test_provider_model_picker_has_visible_apply_and_final_save_buttons(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                picker = tui.ModelPickerScreen(
                    [tui.DiscoveredModel("stealth/ox-alpha", context_window=1_048_576)],
                    "en",
                )
                await app.push_screen(picker)
                await pilot.pause()
                apply_button = picker.query_one("#model-picker-apply", tui.Button)
                self.assertIn("Use model", str(apply_button.label))
                await picker.dismiss(None)
                await pilot.pause()

                setup = tui.ProviderSetupScreen("en", provider_preset("openrouter"))
                await app.push_screen(setup)
                await pilot.pause()
                save_button = setup.query_one("#provider-save", tui.Button)
                self.assertIn("Use and save", str(save_button.label))
                discovered = tui.DiscoveredModel(
                    "stealth/ox-alpha", context_window=1_048_576
                )
                setup._discovered_models = [discovered]
                with patch.object(setup, "action_save") as save:
                    setup._model_picker_done(discovered)
                save.assert_called_once_with()

    async def test_missing_remote_key_is_explained_before_agent_launch(self) -> None:
        model = ModelRecord(provider_id="openrouter", model_id="stealth/ox-alpha")
        provider = ProviderRecord(
            provider_id="openrouter",
            adapter_kind="openai_compatible_chat",
            base_url="https://openrouter.ai/api/v1",
            credential_ref=None,
        )
        controller = SimpleNamespace(
            details=lambda _provider_id: SimpleNamespace(
                provider=provider,
                credential={"configured": False, "available": False},
            )
        )
        with (
            patch.object(tui, "_selected_model", return_value=model),
            patch.object(tui, "_provider_controller", return_value=controller),
        ):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                with (
                    patch.object(app, "run_worker") as run_worker,
                    patch.object(app, "_write_notice") as notice,
                ):
                    app._submit_task("hello")
                run_worker.assert_not_called()
                self.assertTrue(notice.called)
                text = str(notice.call_args.args[0])
                self.assertIn("No API key is saved for openrouter", text)
                self.assertIn("Use and save", text)

    async def test_review_command_uses_non_mutating_plan_mode(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                with patch.object(app, "_submit_task") as submit:
                    app._handle_command("/review authentication changes")
                submit.assert_called_once()
                task = str(submit.call_args.args[0])
                self.assertIn("Do not modify files", task)
                self.assertIn("authentication changes", task)
                self.assertEqual(submit.call_args.kwargs["agent_mode_override"], "plan")

    async def test_economy_summary_hides_unavailable_cockpit_rows(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                rows = [
                    "Economy: ON",
                    "Context Compiler: applied · — · UNAVAILABLE",
                    "Provider Cache: HIT · MEASURED",
                    "Quality Guard: unchanged contract",
                ]
                with (
                    patch("karox.usage_report.economy_status_lines", return_value=rows),
                    patch.object(app, "_write") as write,
                ):
                    app._economy_status_command(verbose=False)
                text = str(write.call_args.args[0])
                self.assertNotIn("UNAVAILABLE", text)
                self.assertIn("Provider Cache", text)
                self.assertIn("Quality Guard", text)

                with (
                    patch("karox.usage_report.economy_status_lines", return_value=rows),
                    patch.object(app, "_write") as write,
                ):
                    app._economy_status_command(verbose=True)
                self.assertIn("UNAVAILABLE", str(write.call_args.args[0]))

    async def test_orchestrate_help_is_task_first_not_recipe_syntax(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                with patch.object(app, "_write") as write:
                    app._orchestrate_command("")
                text = str(write.call_args.args[0])
                self.assertIn("/orchestrate TASK", text)
                self.assertIn("/orchestrate run TASK", text)
                self.assertNotIn("feature ::", text)

    async def test_mcp_command_opens_management_in_full_tui(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                with (
                    patch.object(app, "_open_connections") as opened,
                    patch.object(app, "_run_inspection") as inspection,
                ):
                    app._handle_command("/mcp")
                opened.assert_called_once_with(tui.CONNECT_FOCUS_CLIENTS)
                inspection.assert_not_called()

    async def test_help_all_exposes_karox_power_features_without_bare_menu_noise(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                with patch.object(app, "_write") as write:
                    app._handle_command("/help")
                short = str(write.call_args.args[0])
                self.assertIn("/map", short)
                self.assertNotIn("/memory", short)
                self.assertIn("/help all", short)

                with patch.object(app, "_write") as write:
                    app._handle_command("/help all")
                full = str(write.call_args.args[0])
                for command in ("/map", "/memory", "/usage", "/mission", "/agents", "/compact"):
                    self.assertIn(command, full)

    async def test_map_status_explains_that_normal_tasks_use_the_map_automatically(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                status = {
                    "exists": True,
                    "level": "ultra",
                    "fresh": True,
                    "files_scanned": 123,
                }
                with (
                    patch("karox.map_service.MapService.status", return_value=status),
                    patch.object(app, "_write") as write,
                ):
                    app._handle_command("/map")
                text = str(write.call_args.args[0])
                self.assertIn("KaroX uses the map automatically", text)
                self.assertNotIn("Technical details", text)

    async def test_authentication_machine_error_is_actionable(self) -> None:
        model = ModelRecord(provider_id="openrouter", model_id="stealth/ox-alpha")
        with patch.object(tui, "_selected_model", return_value=model):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                text = app._friendly_provider_message("provider_error:authentication")
                self.assertNotIn("provider_error:authentication", text)
                self.assertIn("openrouter rejected the API key", text)
                self.assertIn("/connect", text)
                self.assertIn("Use and save", text)

    async def test_resume_arms_the_next_prompt_for_the_same_durable_session(self) -> None:
        store = SessionStore(self.root)
        store.create(
            self.root,
            "first task",
            AccessProfile.WORKSPACE_WRITE,
            session_id="resume-me",
        )
        model = ModelRecord(provider_id="local", model_id="model-a")
        with patch.object(tui, "_selected_model", return_value=model):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app._resume_session("resume-me")
                self.assertEqual(app.active_session, "resume-me")
                self.assertTrue(app._resume_next_task)
                with (
                    patch.object(app, "_model_auth_problem", return_value=None),
                    patch.object(app, "run_worker") as run_worker,
                    patch.object(tui, "_agent_argv", return_value=["agent"]) as argv,
                ):
                    app._submit_task("second task")
                run_worker.assert_called_once()
                self.assertEqual(argv.call_args.args[3], "resume-me")
                self.assertTrue(argv.call_args.kwargs["continue_task"])
                self.assertFalse(app._resume_next_task)

    async def test_fork_creates_child_lineage_and_arms_next_prompt(self) -> None:
        store = SessionStore(self.root)
        store.create(
            self.root,
            "original task",
            AccessProfile.WORKSPACE_WRITE,
            session_id="source",
            name="source name",
        )
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app.active_session = "source"
                app._fork_session("source :: experiment")
                self.assertNotEqual(app.active_session, "source")
                self.assertTrue(app._resume_next_task)
                child = store.load(str(app.active_session))
                self.assertEqual(child.parent_session_id, "source")
                self.assertEqual(child.name, "experiment")
                self.assertIn('"source_session": "source"', child.continuation_context)
                self.assertEqual(child.provider_history, [])

    async def test_background_orchestration_start_does_not_block_the_composer(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                with patch.object(app, "run_worker") as run_worker:
                    app._orchestrate_command("start inspect the project")
                self.assertFalse(app.agent_busy)
                self.assertFalse(app.query_one("#composer", tui.Input).disabled)
                run_worker.assert_called_once()
                self.assertFalse(run_worker.call_args.kwargs["exclusive"])

    async def test_background_start_callback_tracks_run_without_blocking_chat(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app._orchestration_start_finished(
                    0,
                    '{"status":"started","run_id":"run-background","pid":123}',
                )
                self.assertEqual(app._last_orchestration_run_id, "run-background")
                self.assertFalse(app.agent_busy)
                self.assertEqual(getattr(app.focused, "id", None), "composer")

    async def test_skill_selection_permissions_and_agent_wiring_are_real(self) -> None:
        skill_dir = self.root / ".karox" / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: demo\n"
            "description: Demo guarded skill\n"
            "version: 1.0.0\n"
            "permissions:\n"
            "  - repo.write\n"
            "---\n"
            "Review the task and use repository tools only when permitted.\n",
            encoding="utf-8",
        )
        model = ModelRecord(provider_id="local", model_id="model-a")
        with patch.object(tui, "_selected_model", return_value=model):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app._skills_command("use demo")
                self.assertEqual(app.active_skill, "demo")
                app._skills_command("allow demo repo.write")
                self.assertEqual(app._skill_permissions["demo"]["repo.write"], "allow")
                with (
                    patch.object(app, "_model_auth_problem", return_value=None),
                    patch.object(app, "run_worker") as run_worker,
                    patch.object(tui, "_agent_argv", return_value=["agent"]) as argv,
                ):
                    app._submit_task("use the selected skill")
                run_worker.assert_called_once()
                self.assertEqual(argv.call_args.kwargs["skill"], "demo")
                self.assertEqual(
                    argv.call_args.kwargs["skill_permissions"],
                    ("repo.write=allow",),
                )

    async def test_skill_cannot_grant_a_capability_it_did_not_declare(self) -> None:
        skill_dir = self.root / ".karox" / "skills" / "demo"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\n"
            "name: demo\n"
            "description: Demo guarded skill\n"
            "version: 1.0.0\n"
            "permissions:\n"
            "  - repo.read\n"
            "---\n"
            "Read only.\n",
            encoding="utf-8",
        )
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                with patch.object(app, "_write_notice") as notice:
                    app._skills_command("allow demo git.push")
                self.assertNotIn("git.push", app._skill_permissions.get("demo", {}))
                self.assertTrue(notice.called)
                self.assertIn("did not declare", str(notice.call_args.args[0]))

    async def test_permissions_preview_uses_real_policy_and_manage_does_not_silently_elevate(self) -> None:
        model = ModelRecord(provider_id="openrouter", model_id="stealth/ox-alpha")
        provider = ProviderRecord(
            provider_id="openrouter",
            adapter_kind="openai_compatible_chat",
            base_url="https://openrouter.ai/api/v1",
            bypass=False,
        )
        controller = SimpleNamespace(
            details=lambda _provider_id: SimpleNamespace(provider=provider),
            edit_provider=lambda *_a, **_k: None,
        )
        with (
            patch.object(tui, "_selected_model", return_value=model),
            patch.object(tui, "_provider_controller", return_value=controller),
        ):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                with patch.object(app, "_write") as write:
                    app._permissions_command("")
                text = str(write.call_args.args[0])
                self.assertIn("Normal — work inside the project", text)
                self.assertIn("project files: read and edit", text)
                self.assertNotIn("workspace_write", text)
                self.assertNotIn("repo.write", text)
                self.assertIn("/permissions details", text)
                with patch.object(app, "_write") as write:
                    app._permissions_command("details")
                technical = str(write.call_args.args[0])
                self.assertIn("workspace_write", technical)
                self.assertIn("repo.write", technical)
                self.assertNotIn("git.push", technical.split("Capability IDs:", 1)[-1].splitlines()[0])
                with (
                    patch.object(app, "_open_connection_detail") as opened,
                    patch.object(app, "_write_notice"),
                ):
                    app._permissions_command("elevated")
                opened.assert_called_once_with("provider", "openrouter")

    async def test_permissions_normal_turns_off_elevated_for_future_sessions(self) -> None:
        model = ModelRecord(provider_id="openrouter", model_id="stealth/ox-alpha")
        provider = ProviderRecord(
            provider_id="openrouter",
            adapter_kind="openai_compatible_chat",
            base_url="https://openrouter.ai/api/v1",
            bypass=True,
        )
        edited: list[bool] = []

        def details(_provider_id):
            current = ProviderRecord(
                provider_id="openrouter",
                adapter_kind="openai_compatible_chat",
                base_url="https://openrouter.ai/api/v1",
                bypass=False if edited else provider.bypass,
            )
            return SimpleNamespace(provider=current)

        controller = SimpleNamespace(
            details=details,
            edit_provider=lambda _provider_id, **kw: edited.append(bool(kw["bypass"])),
        )
        with (
            patch.object(tui, "_selected_model", return_value=model),
            patch.object(tui, "_provider_controller", return_value=controller),
        ):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app._permissions_command("normal")
                self.assertEqual(edited, [False])

    async def test_sessions_lifecycle_requires_archive_and_explicit_delete_confirmation(self) -> None:
        store = SessionStore(self.root)
        store.create(
            self.root,
            "managed task",
            AccessProfile.WORKSPACE_WRITE,
            session_id="managed",
        )
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(self.root, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app._sessions_command("archive managed")
                self.assertTrue(store.load("managed").archived)
                app._sessions_command("delete managed --confirm wrong")
                self.assertTrue(store.state_path("managed").exists())
                app._sessions_command("delete managed --confirm managed")
                self.assertFalse(store.session_dir("managed").exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
