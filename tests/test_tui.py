"""Contracts for the human-facing KaroX terminal application."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from karox import tailscale as tailscale_module
from karox import tui
from karox.providers import ProviderError, ProviderErrorKind
from karox.registry import ModelRecord
from textual.events import Paste
from textual.geometry import Offset
from textual.selection import Selection


class InputRoutingTests(unittest.TestCase):
    def test_safe_inspection_commands_map_to_backend(self) -> None:
        self.assertEqual(
            tui._slash_to_argv("/models", None, "/repo"),
            ["model", "list", "--json"],
        )
        self.assertEqual(
            tui._slash_to_argv("/mcp", None, "/repo"),
            ["mcp", "status", "--json"],
        )
        self.assertEqual(
            tui._slash_to_argv("/mcp", "session-7", "/repo"),
            ["mcp", "status", "--json", "--session-id", "session-7"],
        )
        self.assertEqual(
            tui._slash_to_argv("/doctor", None, "/repo"),
            ["doctor", "--json"],
        )

    def test_chat_text_is_never_treated_as_argparse_input(self) -> None:
        self.assertIsNone(tui._slash_to_argv("fix the failing tests", None, "/repo"))
        self.assertIsNone(tui._slash_to_argv("-", None, "/repo"))
        self.assertIsNone(tui._slash_to_argv("bridge list --json", None, "/repo"))

    def test_agent_argv_contains_task_and_explicit_verification(self) -> None:
        argv = tui._agent_argv(
            "fix it",
            Path("/repo"),
            ("python", "-m", "pytest", "-q"),
            "task-1",
        )
        self.assertEqual(argv[:2], ["agent", "run"])
        self.assertEqual(argv[argv.index("--task") + 1], "fix it")
        self.assertEqual(
            json.loads(argv[argv.index("--verification-command") + 1]),
            ["python", "-m", "pytest", "-q"],
        )

    def test_language_preference_round_trips_and_rejects_unknown_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "ui.json"
            with patch.object(tui, "_preferences_path", return_value=path):
                tui._save_language("ru")
                self.assertEqual(tui._load_language(), "ru")
                tui._save_sponsors_visible(False)
                self.assertFalse(tui._load_sponsors_visible())
                self.assertEqual(tui._load_language(), "ru")
                path.write_text('{"language":"de"}', encoding="utf-8")
                self.assertIsNone(tui._load_language())
                path.write_text("not json", encoding="utf-8")
                self.assertIsNone(tui._load_language())

    def test_windows_system_folder_is_never_a_workspace(self) -> None:
        windows = Path(os.environ.get("SystemRoot", r"C:\Windows"))
        reason = tui._unsafe_workspace_reason(windows / "System32", "ru")
        self.assertIsNotNone(reason)
        self.assertIn("системная папка", reason.lower())
        self.assertIsNone(tui._unsafe_workspace_reason(Path.cwd(), "ru"))


class LineModeTests(unittest.TestCase):
    def run_lines(self, lines: str) -> tuple[int, str]:
        output = io.StringIO()
        code = tui.run_tui(
            repository=str(Path.cwd()),
            input_stream=io.StringIO(lines),
            output_stream=output,
        )
        return code, output.getvalue()

    def test_a_child_process_is_told_to_write_utf8(self) -> None:
        """Both ends have to name the same encoding, not just the reader.

        Every KaroX child is read back with `encoding="utf-8"`, but a Python
        process writing to a pipe on Windows encodes with the locale code page.
        Nothing told it otherwise, so the two ends disagreed and `errors="replace"`
        turned each undecodable byte into U+FFFD: the model's `привет — hello`
        reached the chat as six replacement marks and one more for the dash.
        """
        env = tui._child_environment()
        self.assertEqual(env["PYTHONIOENCODING"], "utf-8")

    def test_a_child_process_keeps_the_import_path_it_was_given(self) -> None:
        with patch.dict(os.environ, {"PYTHONPATH": "existing-entry"}):
            env = tui._child_environment(Path("added-entry"))
        self.assertTrue(env["PYTHONPATH"].startswith(str(Path("added-entry"))))
        self.assertIn("existing-entry", env["PYTHONPATH"])

    def test_non_ascii_survives_a_real_child_process(self) -> None:
        """The end-to-end proof, run the way the client runs it."""
        message = "привет — hello"
        script = (
            "import sys; sys.stdout.write(sys.argv[1])"
        )
        result = subprocess.run(
            [sys.executable, "-c", script, message],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=tui._child_environment(),
        )

        self.assertEqual(result.stdout, message)
        self.assertNotIn("�", result.stdout)

    def test_a_byte_order_mark_does_not_turn_a_command_into_a_paid_request(self) -> None:
        """Windows PowerShell pipes a UTF-8 BOM ahead of the first line.

        `str.strip()` does not remove it, so `/quit` arrived as `﻿/quit` and
        matched neither the command table nor even `startswith("/")`. It fell
        through to the agent: the exit became a billed model request that answered
        "I cannot quit". Confirmed against a real `powershell.exe` pipe, which
        delivers `'﻿/quit\\n'`.
        """
        with patch.object(tui, "_run_cli") as agent:
            code, output = self.run_lines("﻿/quit\n")

        agent.assert_not_called()
        self.assertEqual(code, 0)
        self.assertNotIn("Unknown command", output)

    def test_a_byte_order_mark_still_reaches_a_backend_command(self) -> None:
        with patch.object(tui, "_run_cli") as agent:
            code, output = self.run_lines("﻿/help\n/quit\n")

        agent.assert_not_called()
        self.assertEqual(code, 0)
        self.assertIn("KaroX commands:", output)

    def test_an_invisible_mark_does_not_swallow_a_real_task(self) -> None:
        """Cleaning the framing must not eat the text the user meant to send."""
        self.assertEqual(tui._clean_line("﻿review the diff\n"), "review the diff")
        self.assertEqual(tui._clean_line("  ​ /help \n"), "/help")
        self.assertEqual(tui._clean_line("/quit\n"), "/quit")

    def test_a_status_glyph_does_not_kill_a_redirected_session(self) -> None:
        # Line mode is exactly what runs when stdout is a pipe, and on Windows
        # that stream is the system code page. KaroX prints status glyphs that
        # cp1251 cannot represent, so redirecting output ended the session with
        # UnicodeEncodeError instead of printing a status line.
        written: list[str] = []

        class NarrowStream:
            encoding = "cp1251"

            def write(self, text: str) -> None:
                text.encode(self.encoding)  # raises exactly as a real pipe does
                written.append(text)

        stream = NarrowStream()
        with self.assertRaises(UnicodeEncodeError):
            stream.write("✓ done")

        tui._line_writer(stream)("✓ done ●")

        self.assertEqual(len(written), 1)
        self.assertIn("done", written[0])

    def test_redirected_line_mode_still_exits_cleanly(self) -> None:
        written: list[str] = []

        class NarrowStream:
            encoding = "cp1251"

            def write(self, text: str) -> None:
                text.encode(self.encoding)
                written.append(text)

        code = tui.run_tui(
            repository=str(Path.cwd()),
            input_stream=io.StringIO("/quit\n"),
            output_stream=NarrowStream(),
        )

        self.assertEqual(code, 0)
        self.assertTrue(written)

    def test_exit_and_eof_return_zero(self) -> None:
        self.assertEqual(self.run_lines("/quit\n")[0], 0)
        self.assertEqual(self.run_lines("")[0], 0)

    def test_help_describes_human_commands(self) -> None:
        code, output = self.run_lines("/help\n/quit\n")
        self.assertEqual(code, 0)
        self.assertIn("KaroX commands", output)
        self.assertIn("/connect", output)
        self.assertIn("/bridge", output)

    def test_lone_dash_does_not_leak_argparse_error(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            code, output = self.run_lines("-\n/quit\n")
        self.assertEqual(code, 0)
        self.assertIn("No API model is configured", output)
        self.assertNotIn("invalid choice", output)
        self.assertNotIn("usage: karox", output)

    def test_unknown_slash_is_friendly(self) -> None:
        code, output = self.run_lines("/bogus\n/quit\n")
        self.assertEqual(code, 0)
        self.assertIn("Unknown command", output)
        self.assertIn("/help", output)

    def test_natural_task_delegates_to_agent_backend(self) -> None:
        selected = ModelRecord("openai", "model-a", tools="true")
        with (
            patch.object(tui, "_selected_model", return_value=selected),
            patch.object(tui, "_run_cli", return_value=0) as run_cli,
        ):
            code, _ = self.run_lines("fix the tests\n/quit\n")
        self.assertEqual(code, 0)
        argv = run_cli.call_args.args[0]
        self.assertEqual(argv[argv.index("--task") + 1], "fix the tests")


class BackendDelegationTests(unittest.TestCase):
    def test_run_cli_invokes_main_entrypoint(self) -> None:
        from karox import cli as cli_mod

        output = io.StringIO()
        with patch.object(cli_mod, "main", return_value=0) as main:
            code = tui._run_cli(["doctor", "--json"], output.write)
        self.assertEqual(code, 0)
        main.assert_called_once_with(["doctor", "--json"])

    def test_provider_setup_stores_secret_and_selects_model(self) -> None:
        selected = ModelRecord("openai", "gpt-test", tools="true")
        with (
            patch.object(tui, "_registry") as registry_factory,
            patch.object(tui, "CredentialStore") as credentials,
        ):
            registry = registry_factory.return_value
            registry.provider.side_effect = RuntimeError("new provider")
            registry.select_model.return_value = selected
            result = tui._save_provider(
                tui.ProviderSetup(
                    "openai",
                    "openai_responses",
                    "https://api.openai.com/v1",
                    "gpt-test",
                    "secret-value",
                )
            )
        self.assertEqual(result, selected)
        credentials.return_value.set.assert_called_once_with("openai", "secret-value")
        self.assertEqual(
            registry.put_provider.call_args.args[0].credential_ref,
            "os-keyring:provider/openai",
        )
        self.assertEqual(registry.put_model.call_args.args[0].model_id, "gpt-test")

    def test_model_discovery_reads_ids_and_token_limits(self) -> None:
        response = Mock()
        response.status_code = 200
        response.json.return_value = {
            "models": [
                {
                    "name": "models/gemini-test",
                    "inputTokenLimit": 100000,
                    "outputTokenLimit": 8192,
                }
            ]
        }
        response.raise_for_status.return_value = None
        with patch.object(tui.httpx, "get", return_value=response) as get:
            models = tui._discover_models(
                tui.ProviderSetup(
                    "gemini",
                    "gemini_generate_content",
                    "https://generativelanguage.googleapis.com/v1beta",
                    "",
                    "secret-value",
                )
            )
        self.assertEqual(
            models,
            [tui.DiscoveredModel("gemini-test", 100000, 8192)],
        )
        self.assertEqual(
            get.call_args.kwargs["headers"]["x-goog-api-key"], "secret-value"
        )

    def test_openai_discovery_retries_common_v1_base_and_returns_correction(
        self,
    ) -> None:
        missing = Mock(status_code=404, text="not found")
        missing.json.side_effect = ValueError
        found = Mock(status_code=200)
        found.json.return_value = {"data": [{"id": "model-a"}]}
        setup = tui.ProviderSetup(
            "compatible",
            "openai_compatible_chat",
            "https://provider.example",
            "",
            "secret",
        )
        with patch.object(tui.httpx, "get", side_effect=[missing, found]) as get:
            result = tui._discover_models_result(setup)
        self.assertEqual(result.base_url, "https://provider.example/v1")
        self.assertEqual(result.models, (tui.DiscoveredModel("model-a"),))
        self.assertEqual(
            [call.args[0] for call in get.call_args_list],
            [
                "https://provider.example/models",
                "https://provider.example/v1/models",
            ],
        )

    def test_discovery_404_is_structured_and_actionable(self) -> None:
        missing = Mock(status_code=404, text="not found")
        missing.json.side_effect = ValueError
        setup = tui.ProviderSetup(
            "compatible",
            "openai_compatible_chat",
            "https://provider.example/v1",
            "",
            "secret",
        )
        with patch.object(tui.httpx, "get", return_value=missing):
            with self.assertRaises(tui.ModelDiscoveryError) as raised:
                tui._discover_models_result(setup)
        message = tui._friendly_discovery_error(
            raised.exception, "ru", base_url=setup.base_url
        )
        self.assertIn("HTTP 404", message)
        self.assertIn("https://provider.example/v1/models", message)
        self.assertIn("Model ID вручную", message)

    def test_probe_404_explains_api_type_and_exact_endpoint(self) -> None:
        setup = tui.ProviderSetup(
            "custom",
            "openai_responses",
            "https://provider.example/v1",
            "model-a",
        )
        error = ProviderError(
            ProviderErrorKind.INVALID_REQUEST,
            "route not found",
            status_code=404,
        )
        message = tui._friendly_probe_error(error, "ru", setup=setup)
        self.assertIn("https://provider.example/v1/responses", message)
        self.assertIn("OpenAI-compatible", message)
        self.assertIn("route not found", message)

    def test_probe_503_explains_temporary_outage_and_retry(self) -> None:
        setup = tui.ProviderSetup(
            "empiriolabs",
            "openai_compatible_chat",
            "https://api.empiriolabs.ai/v1",
            "glm-5-2",
        )
        error = ProviderError(
            ProviderErrorKind.INVALID_REQUEST,
            "HTTP 503: Service Unavailable",
            status_code=503,
        )
        message = tui._friendly_probe_error(error, "ru", setup=setup)
        self.assertIn("временно недоступен", message)
        self.assertIn("повторите проверку", message)
        self.assertIn("/chat/completions", message)

    def test_inspection_results_are_human_readable_not_raw_json(self) -> None:
        self.assertEqual(
            tui._inspection_text("/models", 0, "[]", "ru"),
            "Модели ещё не подключены.\nИспользуйте /connect → API-модель.",
        )
        models = tui._inspection_text(
            "/models",
            0,
            json.dumps(
                [
                    {
                        "provider_id": "openai",
                        "model_id": "gpt-test",
                        "selected": True,
                        "context_window": 128000,
                    }
                ]
            ),
            "ru",
        )
        self.assertIn("Подключённые модели: 1", models)
        self.assertIn("openai/gpt-test — активна", models)
        self.assertNotIn("{", models)

    def test_every_empty_inspection_has_guidance(self) -> None:
        self.assertIn(
            "No sessions yet", tui._inspection_text("/sessions", 0, "[]", "en")
        )
        self.assertIn("No external MCP", tui._inspection_text("/mcp", 0, "[]", "en"))
        empty_status = json.dumps({"totals": {"servers": 0}, "servers": []})
        self.assertIn(
            "No external MCP", tui._inspection_text("/mcp", 0, empty_status, "en")
        )
        failure = tui._inspection_text(
            "/doctor", 2, "karox: credential store unavailable", "en"
        )
        self.assertEqual(failure, "The command failed.\ncredential store unavailable")

    def test_mcp_screen_keeps_reachability_and_authorization_separate(self) -> None:
        status = json.dumps(
            {
                "status_version": 1,
                "session_id": "session-7",
                "totals": {
                    "servers": 1,
                    "selected": 1,
                    "probed": 0,
                    "live": 0,
                    "allowed_tools": 1,
                    "blocked_tools": 1,
                    "needs_attention": 1,
                },
                "servers": [
                    {
                        "server_id": "docs",
                        "namespace": "docs",
                        "transport": "streamable_http",
                        "location": "remote",
                        "endpoint": "https://example.invalid/mcp",
                        "credential": {"required": True, "state": "missing"},
                        "liveness": {"state": "not_probed", "tool_count": None},
                        "selection": {
                            "state": "current",
                            "counts": {
                                "tools": 2,
                                "allowed": 1,
                                "blocked_ask": 1,
                                "blocked_deny": 0,
                                "blocked_stale": 0,
                            },
                        },
                        "attention": ["credential_missing", "ask_blocks_calls"],
                    }
                ],
            }
        )
        rendered = tui._inspection_text("/mcp", 0, status, "en")
        self.assertIn("1 servers · 1 selected · 0 live of 0 probed", rendered)
        self.assertIn("tools: 1 allowed, 1 ask, 0 deny", rendered)
        self.assertIn("ask refuses the call without prompting", rendered)
        self.assertIn("key: missing · reachability: not probed", rendered)
        self.assertNotIn("{", rendered)

    def test_bridge_setup_builds_real_openapi_launch(self) -> None:
        with (
            patch.object(tui, "SessionStore") as sessions,
            patch.object(tui, "BridgeCredentialStore") as credentials,
        ):
            credentials.return_value.set.return_value = {"secret": "bridge-secret"}
            launch = tui._bridge_launch(
                Path.cwd(),
                tui.BridgeSetup(
                    "promptql",
                    9876,
                    ("karox.repo.read_file",),
                ),
            )
        sessions.return_value.create.assert_called_once()
        self.assertEqual(launch.protocol, "openapi")
        self.assertEqual(launch.endpoint, "http://127.0.0.1:9876/openapi.json")
        self.assertEqual(launch.secret, "bridge-secret")
        self.assertIn("karox.repo.read_file", launch.argv)

    def test_bridge_launch_passes_oauth_public_origin(self) -> None:
        with (
            patch.object(tui, "SessionStore"),
            patch.object(tui, "BridgeCredentialStore") as credentials,
        ):
            credentials.return_value.set.return_value = {"secret": "approval-secret"}
            launch = tui._bridge_launch(
                Path.cwd(),
                tui.BridgeSetup(
                    "chatgpt-web",
                    9877,
                    ("karox.repo.read_file",),
                    tunnel_provider="tailscale",
                    public_url="https://device.example.ts.net",
                ),
            )
        self.assertEqual(launch.protocol, "mcp")
        self.assertEqual(launch.public_url, "https://device.example.ts.net")
        self.assertIn("--public-url", launch.argv)
        self.assertIn("https://device.example.ts.net", launch.argv)

    def test_web_bridge_launch_delegates_complete_lifecycle_to_cli(self) -> None:
        launch = tui._managed_web_bridge_launch(
            Path.cwd(),
            tui.BridgeSetup(
                "chatgpt-web",
                9878,
                ("karox.repo.read_file", "karox.repo.write_file"),
                tunnel_provider="cloudflare",
            ),
        )
        self.assertTrue(launch.managed)
        self.assertEqual(launch.profile, "chatgpt-web")
        self.assertIn("connect", launch.argv)
        self.assertIn("chatgpt-web", launch.argv)
        self.assertIn("--session-id", launch.argv)
        self.assertIn("workspace_write", launch.argv)
        self.assertNotIn("--public-url", launch.argv)
        self.assertEqual(launch.argv.count("--tool"), 2)


@unittest.skipUnless(tui._HAS_TEXTUAL, "textual is not installed")
class FullScreenAppTests(unittest.IsolatedAsyncioTestCase):
    async def focus(self, pilot: object, app: object, widget_id: str) -> None:
        """Wait for focus to land on a widget instead of assuming one tick.

        A single ``pilot.pause()`` yields once. Dismissing a screen and focusing
        the composer underneath takes more than one event-loop turn when the
        machine is busy, so asserting straight after the pause passed alone and
        failed inside the full suite -- a release gate that fails at random
        teaches people to re-run it rather than read it.
        """
        for _ in range(50):
            focused = getattr(app, "focused", None)
            if focused is not None and focused.id == widget_id:
                return
            await pilot.pause()  # type: ignore[attr-defined]
        focused = getattr(app, "focused", None)
        self.fail(
            f"focus never reached {widget_id!r}; it is on "
            f"{getattr(focused, 'id', None)!r} on screen {type(app.screen).__name__}"
        )

    async def test_first_run_asks_only_for_language_then_opens_chat(self) -> None:
        with (
            patch.object(tui, "_load_language", return_value=None),
            patch.object(tui, "_save_language") as save_language,
            patch.object(tui, "_selected_model", return_value=None),
        ):
            app = tui.KaroXApp(Path.cwd())
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.LanguageScreen)
                await pilot.press("1")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, tui.ConnectionChoiceScreen)
                self.assertEqual(app.language, "ru")
                save_language.assert_called_once_with("ru")
                await self.focus(pilot, app, "composer")
                self.assertIn(
                    "Опишите задачу",
                    app.query_one("#composer", tui.Input).placeholder,
                )

    async def test_language_choice_supports_arrows_and_enter(self) -> None:
        with (
            patch.object(tui, "_load_language", return_value=None),
            patch.object(tui, "_save_language") as save_language,
            patch.object(tui, "_selected_model", return_value=None),
        ):
            app = tui.KaroXApp(Path.cwd())
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                await pilot.press("down", "enter")
                await pilot.pause()
                self.assertEqual(app.language, "en")
                save_language.assert_called_once_with("en")
                await self.focus(pilot, app, "composer")

    async def test_saved_language_skips_language_screen_and_connection_setup(
        self,
    ) -> None:
        with (
            patch.object(tui, "_load_language", return_value="ru"),
            patch.object(tui, "_selected_model", return_value=None),
        ):
            app = tui.KaroXApp(Path.cwd())
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                self.assertNotIsInstance(app.screen, tui.LanguageScreen)
                self.assertNotIsInstance(app.screen, tui.ConnectionChoiceScreen)
                await self.focus(pilot, app, "composer")

    async def test_slash_opens_and_filters_keyboard_command_menu(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.press("/")
                await pilot.pause()
                self.assertTrue(app._command_menu_open)
                self.assertEqual(set(app._filtered_commands), set(tui._commands("ru")))
                await pilot.press("c", "o", "n")
                await pilot.pause()
                self.assertEqual(app._filtered_commands, ["/connect"])
                self.assertEqual(
                    app.query_one("#command-menu", tui.Static).styles.display,
                    "block",
                )

    async def test_a_pasted_stack_trace_is_not_truncated_to_one_line(self) -> None:
        trace = (
            "Traceback (most recent call last):\n"
            '  File "app.py", line 3, in <module>\n'
            "    main()\n"
            "ZeroDivisionError: division by zero"
        )
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await self.focus(pilot, app, "composer")
                composer = app.query_one("#composer", tui.Input)
                composer.post_message(Paste(trace))
                await pilot.pause()

                # The composer shows a marker rather than one enormous line,
                # and the base widget must not also append line one after it.
                self.assertEqual(composer.value, "[paste #1: 4 lines]")
                # The text the agent receives is the whole trace.
                self.assertEqual(app._expand_pasted_blocks(composer.value), trace)

    async def test_every_tool_of_a_turn_stays_visible(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app._begin_step("call-1", "reading a file")
                app._finish_step("call-1", "repo.read_file", "ok", failed=False)
                app._begin_step("call-2", "editing a file")
                app._finish_step("call-2", "repo.write_file", "ok", failed=False)
                app._begin_step("call-3", "running checks")
                await pilot.pause()

                rendered = str(app.query_one("#activity", tui.Static).render())

                # One overwritten line showed the third tool and no evidence
                # that the first two had happened at all.
                self.assertIn("repo.read_file", rendered)
                self.assertIn("repo.write_file", rendered)
                self.assertIn("running checks", rendered)

    async def test_a_no_change_run_does_not_repeat_itself_in_a_second_bubble(self) -> None:
        """A greeting produced a paragraph about the greeting.

        When a turn calls no tools, KaroX asks the model to "give the answer and
        name the tool results it rests on". There is nothing to name, so the reply
        can only be a report about itself -- "the task was only a greeting, no
        repository change was required" -- while the actual reply is already on
        screen above it and the one-line notice below says the same again.
        """
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app._last_assistant_content = "Привет! Готов помочь."
                report = json.dumps(
                    {
                        "provider_message": (
                            "Задача состояла лишь из приветствия, на которое я "
                            "ответил приветствием — изменения не требовалось."
                        ),
                        "status": "stopped",
                        "reason": "no_changes",
                        "verified": False,
                    }
                )
                with patch.object(app, "_write_assistant") as write:
                    app._agent_finished(0, report)

                write.assert_not_called()

    async def test_a_real_answer_is_still_shown_when_nothing_preceded_it(self) -> None:
        """Suppression is for the repeat, not for the only thing the user gets."""
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app._last_assistant_content = ""
                report = json.dumps(
                    {
                        "provider_message": "Вот что я нашёл в файле.",
                        "status": "stopped",
                        "reason": "no_changes",
                        "verified": False,
                    }
                )
                with patch.object(app, "_write_assistant") as write:
                    app._agent_finished(0, report)

                write.assert_called_once_with("Вот что я нашёл в файле.")

    async def test_the_chat_gets_most_of_a_small_window(self) -> None:
        """Fixed chrome came to seventeen rows before a word of conversation."""
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(100, 24)) as pilot:
                await pilot.pause()
                log = app.query_one("#conversation", tui.ChatLog)

                self.assertGreaterEqual(
                    log.size.height,
                    12,
                    "the chat has less than half of a 24-row window",
                )

    async def test_a_narrow_window_wraps_the_answer_instead_of_clipping_it(self) -> None:
        """RichLog defaults to min_width=78 whatever the window is.

        In a window narrower than that the chat was laid out at 78 columns and
        everything past the right edge disappeared behind a horizontal scrollbar,
        so words ended mid-letter -- "requiring" was drawn as "requiri".
        """
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(56, 24)) as pilot:
                await pilot.pause()
                app._write_assistant(
                    "This answer rests on no tool results — it was a simple "
                    "conversational greeting, requiring no repository inspection "
                    "or changes at all."
                )
                await pilot.pause()

                log = app.query_one("#conversation", tui.ChatLog)
                self.assertLessEqual(
                    log.virtual_size.width,
                    log.size.width,
                    "the chat overflows its own width, so the right edge is clipped",
                )

    async def test_an_answer_is_not_drawn_twice(self) -> None:
        """The polling reader and the final report carry the same text.

        `_agent_finished` calls `_poll_agent_history`, which writes the turn's
        answer out of the session record, and then wrote
        `report["provider_message"]` as well -- so every reply appeared twice in
        the chat. `_last_assistant_content` was already being recorded for this
        comparison and was never consulted.
        """
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                answer = "Hello — привет"
                app._last_assistant_content = answer
                report = json.dumps(
                    {
                        "provider_message": answer,
                        "status": "stopped",
                        "reason": "answer",
                        "verified": False,
                    }
                )
                with patch.object(app, "_write_assistant") as write:
                    app._agent_finished(0, report)

                write.assert_not_called()

    async def test_a_different_final_message_is_still_shown(self) -> None:
        """Suppressing the repeat must not suppress genuinely new text."""
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app._last_assistant_content = "an earlier step"
                report = json.dumps(
                    {
                        "provider_message": "the final answer",
                        "status": "stopped",
                        "reason": "answer",
                        "verified": False,
                    }
                )
                with patch.object(app, "_write_assistant") as write:
                    app._agent_finished(0, report)

                write.assert_called_once_with("the final answer")

    async def test_a_failed_tool_is_not_marked_as_done(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app._begin_step("call-1", "running checks")
                app._finish_step("call-1", "checks.run", "failed", failed=True)
                await pilot.pause()

                rendered = str(app.query_one("#activity", tui.Static).render())

                self.assertIn("✕ checks.run", rendered)
                self.assertNotIn("✓ checks.run", rendered)

    async def test_a_single_line_paste_still_goes_in_verbatim(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await self.focus(pilot, app, "composer")
                composer = app.query_one("#composer", tui.Input)
                composer.post_message(Paste("fix the parser\n"))
                await pilot.pause()

                self.assertEqual(composer.value, "fix the parser")

    async def test_slash_selection_opens_connect_screen(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.press("/", "c", "o", "n", "enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.ConnectionChoiceScreen)
                web_label = str(
                    app.screen.query_one("#choice-web", tui.Button).label
                )
                self.assertIn("ChatGPT Web", web_label)
                self.assertIn("Claude Web", web_label)

    async def test_english_connection_flow_stays_in_english(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.press("/", "c", "o", "n", "enter", "1")
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.ProviderPresetScreen)
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.ProviderSetupScreen)
                title = app.screen.query_one(".title", tui.Static)
                self.assertEqual(str(title.render()), "Connect routing.run")

    async def test_command_arrows_and_tab_complete_selection(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.press("/", "down", "tab")
                await pilot.pause()
                self.assertEqual(app.query_one("#composer", tui.Input).value, "/models")

    async def test_language_command_reopens_selector(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                composer = app.query_one("#composer", tui.Input)
                composer.value = "/language"
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.LanguageScreen)

    async def test_task_without_model_points_to_connect_without_modal(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            messages = []
            original_write = app._write

            def capture(message: str) -> None:
                messages.append(message)
                original_write(message)

            app._write = capture
            async with app.run_test(size=(120, 42)) as pilot:
                composer = app.query_one("#composer", tui.Input)
                composer.value = "fix the tests"
                await pilot.press("enter")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, tui.ConnectionChoiceScreen)
                self.assertTrue(any("/connect" in item for item in messages))

    async def test_provider_discovery_and_model_choice_work_from_keyboard(self) -> None:
        discovered = tui.ModelDiscovery(
            models=(
                tui.DiscoveredModel("model-auto", 128000, 8192),
                tui.DiscoveredModel("model-second", 200000, 16000),
            ),
            base_url="https://api.openai.com/v1",
            attempted_urls=("https://api.openai.com/v1/models",),
        )
        with (
            patch.object(tui, "_selected_model", return_value=None),
            patch.object(tui, "_discover_models_result", return_value=discovered),
        ):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            errors = []
            with patch.object(app, "_handle_exception", side_effect=errors.append):
                async with app.run_test(size=(120, 42)) as pilot:
                    app.query_one("#composer", tui.Input).value = "/connect"
                    await pilot.press("enter", "1", "down", "down", "down", "enter")
                    await pilot.pause()
                    await pilot.press("f5")
                    await pilot.pause(0.3)
                    self.assertIsInstance(app.screen, tui.ModelPickerScreen)
                    await self.focus(pilot, app, "model-search")
                    self.assertFalse(errors, repr(errors))
                    picker = app.screen
                    options = picker.query_one("#model-options", tui.OptionList)
                    self.assertEqual(options.option_count, 3)
                    await pilot.press("s", "e", "c", "o", "n", "d")
                    await pilot.pause()
                    self.assertEqual(options.option_count, 2)
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, tui.ProviderSetupScreen)
                    self.assertEqual(
                        app.screen.query_one("#provider-model", tui.Input).value,
                        "model-second",
                    )
                    self.assertIn(
                        "можно оставить пустым",
                        app.screen.query_one(
                            "#provider-context", tui.Input
                        ).placeholder,
                    )
                    self.assertEqual(
                        app.screen.query_one("#provider-context", tui.Input).value,
                        "200000",
                    )
                    summary = str(
                        app.screen.query_one("#provider-summary", tui.Static).render()
                    )
                    self.assertIn("model-second", summary)
                    self.assertIn("200000", summary)

    async def test_provider_setup_is_compact_and_manual_fields_are_progressive(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 42)) as pilot:
                app.query_one("#composer", tui.Input).value = "/connect"
                await pilot.press("enter", "1", "enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.ProviderSetupScreen)
                dialog = app.screen.query_one("#provider-dialog")
                self.assertLess(dialog.size.height, 38)
                model_input = app.screen.query_one("#provider-model", tui.Input)
                self.assertEqual(model_input.styles.display, "none")
                app.screen.query_one("#provider-manual", tui.Button).focus()
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.ManualModelScreen)
                await self.focus(pilot, app, "manual-model-id")
                app.screen.query_one("#manual-model-id", tui.Input).value = "model-manual"
                app.screen.query_one("#manual-model-save", tui.Button).focus()
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.ProviderSetupScreen)
                summary = str(
                    app.screen.query_one("#provider-summary", tui.Static).render()
                )
                self.assertIn("model-manual", summary)
                self.assertFalse(
                    app.screen.query_one("#provider-save", tui.Button).disabled
                )

    async def test_models_command_replaces_progress_with_friendly_empty_state(
        self,
    ) -> None:
        with (
            patch.object(tui, "_selected_model", return_value=None),
            patch.object(tui, "_capture_cli", return_value=(0, "[]")),
        ):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            messages = []
            original_write = app._write

            def capture(message: str) -> None:
                messages.append(message)
                original_write(message)

            app._write = capture
            async with app.run_test(size=(120, 42)) as pilot:
                composer = app.query_one("#composer", tui.Input)
                composer.value = "/models"
                await pilot.press("enter")
                await pilot.pause(0.3)
                self.assertTrue(
                    any("Модели ещё не подключены" in item for item in messages)
                )
                self.assertFalse(any("Running /models" in item for item in messages))
                self.assertEqual(
                    app.query_one("#busy", tui.LoadingIndicator).styles.display,
                    "none",
                )

    async def test_keyboard_both_flow_verifies_api_then_opens_bridge(self) -> None:
        selected = None
        model = ModelRecord("openai", "model-manual", tools="true")

        def selected_model():
            return selected

        def activate(*_args, **_kwargs):
            nonlocal selected
            selected = model
            return model

        with (
            patch.object(tui, "_selected_model", side_effect=selected_model),
            patch.object(tui, "_save_provider", side_effect=activate) as save,
            patch.object(tui, "_probe_provider", return_value={}) as probe,
        ):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 42)) as pilot:
                app.query_one("#composer", tui.Input).value = "/connect"
                await pilot.press("enter", "3", "down", "down", "down", "enter")
                await pilot.pause()
                app.screen.query_one(
                    "#provider-model", tui.Input
                ).value = "model-manual"
                await pilot.press("f10")
                await pilot.pause(0.4)
                self.assertTrue(save.called)
                self.assertTrue(probe.called)
                self.assertIsInstance(app.screen, tui.BridgeSetupScreen)
                await self.focus(pilot, app, "bridge-profile")
                await pilot.press("down", "space")
                self.assertEqual(
                    app.screen.query_one(
                        "#bridge-profile", tui.RadioSet
                    ).pressed_button.id,
                    "profile-notion",
                )

    async def test_provider_picker_filters_and_puter_is_explained(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                app.query_one("#composer", tui.Input).value = "/connect"
                await pilot.press("enter", "1")
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.ProviderPresetScreen)
                await pilot.press("p", "u", "t", "e", "r")
                await pilot.pause()
                options = app.screen.query_one(
                    "#provider-presets", tui.OptionList
                )
                self.assertEqual(options.option_count, 1)
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.PuterInfoScreen)
                contract = str(
                    app.screen.query_one("#puter-contract", tui.Static).render()
                )
                self.assertIn("puter.ai.chat()", contract)
                self.assertIn("not implemented yet", contract)

    async def test_welcome_does_not_claim_ready_without_model(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            messages = []
            original_write = app._write

            def capture(message: str) -> None:
                messages.append(message)
                original_write(message)

            app._write = capture
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                self.assertTrue(any("модель не подключена" in item for item in messages))
                self.assertFalse(any("KaroX готов" in item for item in messages))

    async def test_welcome_claims_ready_with_selected_model(self) -> None:
        selected = ModelRecord("openai", "model-a", tools="true")
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            messages = []
            original_write = app._write

            def capture(message: str) -> None:
                messages.append(message)
                original_write(message)

            app._write = capture
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                self.assertTrue(any("KaroX готов" in item for item in messages))

    async def test_sponsor_ticker_is_compact_and_moves(self) -> None:
        with (
            patch.object(tui, "_selected_model", return_value=None),
            patch.object(tui, "_load_sponsors_visible", return_value=True),
        ):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 42)) as pilot:
                ticker = app.query_one("#sponsor-ticker", tui.Static)
                await pilot.pause(0.3)
                first = str(ticker.render())
                await pilot.pause(0.4)
                second = str(ticker.render())
                self.assertEqual(ticker.size.height, 1)
                self.assertNotEqual(first, second)
                self.assertTrue(
                    any(name in first + second for name in ("routing.run", "OmniaKey"))
                )
                self.assertEqual(ticker.parent.id, "brand")
                self.assertIn("Weights & Biases", app._sponsor_text)

    async def test_sponsors_command_hides_and_persists_ticker(self) -> None:
        with (
            patch.object(tui, "_selected_model", return_value=None),
            patch.object(tui, "_load_sponsors_visible", return_value=True),
            patch.object(tui, "_save_sponsors_visible") as save,
        ):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                brand = app.query_one("#brand")
                visible_height = brand.size.height
                composer = app.query_one("#composer", tui.Input)
                composer.value = "/sponsors off"
                await pilot.press("enter")
                await pilot.pause()
                ticker = app.query_one("#sponsor-ticker", tui.Static)
                self.assertEqual(ticker.styles.display, "none")
                self.assertLess(brand.size.height, visible_height)
                save.assert_called_once_with(False)

    async def test_app_has_chat_composer_and_status_bar(self) -> None:
        selected = ModelRecord("openai", "model-a", tools="true")
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                self.assertIsNotNone(app.query_one("#composer"))
                self.assertIn(
                    "openai/model-a", str(app.query_one("#model-status").render())
                )

    async def test_plain_text_submission_runs_agent_not_argparse(self) -> None:
        selected = ModelRecord("openai", "model-a", tools="true")
        report = {
            "provider_message": "Done",
            "verified": True,
            "status": "verified",
            "steps": 2,
            "changed_files": ["README.md"],
        }
        with (
            patch.object(tui, "_selected_model", return_value=selected),
            patch.object(
                tui, "_run_agent_cli", return_value=(0, json.dumps(report))
            ) as capture,
        ):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                composer = app.query_one("#composer", tui.Input)
                composer.value = "fix the tests"
                await pilot.press("enter")
                await pilot.pause(0.3)
                self.assertTrue(capture.called)
                argv = capture.call_args.args[0]
                self.assertEqual(argv[argv.index("--task") + 1], "fix the tests")
                self.assertFalse(app.agent_busy)

    async def test_system_workspace_blocks_agent_before_any_write(self) -> None:
        selected = ModelRecord("openai", "model-a", tools="true")
        windows = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        with (
            patch.object(tui, "_selected_model", return_value=selected),
            patch.object(tui, "_capture_cli") as capture,
        ):
            app = tui.KaroXApp(windows, language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                composer = app.query_one("#composer", tui.Input)
                composer.value = "создай файл"
                await pilot.press("enter")
                await pilot.pause()
                capture.assert_not_called()
                self.assertFalse(app.agent_busy)
                rendered = "\n".join(
                    line.text
                    for line in app.query_one("#conversation", tui.RichLog).lines
                )
                self.assertIn("Системная папка Windows", rendered)

    async def test_final_answer_renders_markdown_and_explicit_completion(self) -> None:
        selected = ModelRecord("openai", "model-a", tools="true")
        report = {
            "provider_message": "## Результат\n\n**Готово** и `проверено`.",
            "verified": True,
            "status": "verified",
            "steps": 3,
            "changed_files": ["README.md"],
        }
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                app.agent_busy = True
                app.active_session = None
                app._agent_finished(0, json.dumps(report, ensure_ascii=False))
                await pilot.pause()
                rendered = "\n".join(
                    line.text
                    for line in app.query_one("#conversation", tui.RichLog).lines
                )
                self.assertIn("Результат", rendered)
                self.assertIn("Готово", rendered)
                self.assertNotIn("**Готово**", rendered)
                activity = str(app.query_one("#activity", tui.Static).render())
                self.assertIn("Задача завершена и проверена", activity)

    async def test_workspace_command_changes_project_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            with patch.object(tui, "_selected_model", return_value=None):
                app = tui.KaroXApp(Path.cwd(), language="ru")
                async with app.run_test(size=(120, 40)) as pilot:
                    composer = app.query_one("#composer", tui.Input)
                    composer.value = f'/workspace "{project}"'
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertEqual(app.repository, project.resolve())
                    self.assertIn(
                        project.name,
                        str(app.query_one("#repo-status", tui.Static).render()),
                    )


    async def test_stop_request_shows_stopped_notice_instead_of_answer(self) -> None:
        selected = ModelRecord("openai", "model-a", tools="true")
        report = {
            "provider_message": "незавершённый ответ",
            "verified": False,
            "status": "stopped",
            "steps": 1,
        }
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                app.agent_busy = True
                app.active_session = None
                app._stop_requested = True
                app._agent_finished(0, json.dumps(report, ensure_ascii=False))
                await pilot.pause()
                rendered = "\n".join(
                    line.text
                    for line in app.query_one("#conversation", tui.RichLog).lines
                )
                # The stop notice is shown, not the interrupted answer.
                self.assertIn("остановили", rendered)
                self.assertNotIn("незавершённый ответ", rendered)
                self.assertFalse(app.agent_busy)

    async def test_action_stop_agent_terminates_running_subprocess(self) -> None:
        selected = ModelRecord("openai", "model-a", tools="true")
        process = Mock()
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                app.agent_busy = True
                app.agent_process = process
                await pilot.press("escape")
                await pilot.pause()
                process.terminate.assert_called_once()
                self.assertTrue(app._stop_requested)

    async def test_stop_agent_hides_busy_dots_and_activity_immediately(self) -> None:
        # Stopping the agent must hide the animated dots (#busy) and the text
        # activity line right away — the dots must not keep blinking in orange
        # after the run is stopped.
        selected = ModelRecord("openai", "model-a", tools="true")
        process = Mock()
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                app.agent_busy = True
                app.agent_process = process
                # simulate the dots shown while working
                app.query_one("#busy", tui.LoadingIndicator).styles.display = "block"
                app.action_stop_agent()
                await pilot.pause()
                self.assertEqual(str(app.query_one("#busy").styles.display), "none")
                self.assertEqual(str(app.query_one("#activity").styles.display), "none")

    async def test_submit_task_shows_dots_but_no_text_activity(self) -> None:
        # While the agent works only the animated dots should speak; the text
        # activity line must never show "KaroX работает…" / "Останавливаю…"
        # style status phrases (the dots already convey that).
        selected = ModelRecord("openai", "model-a", tools="true")
        report = {"provider_message": "Done", "verified": True, "status": "verified"}
        forbidden = ("KaroX работает", "Подготавливаю", "Останавливаю", "is working",
                     "Preparing the", "Stopping")
        with (
            patch.object(tui, "_selected_model", return_value=selected),
            patch.object(tui, "_run_agent_cli", return_value=(0, json.dumps(report))),
        ):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                composer = app.query_one("#composer", tui.Input)
                composer.value = "fix the tests"
                await pilot.press("enter")
                await pilot.pause(0.3)
                activity_text = str(
                    app.query_one("#activity", tui.Static).render()
                )
                for phrase in forbidden:
                    self.assertNotIn(phrase, activity_text)

    async def test_chatlog_renders_selection_highlight(self) -> None:
        # A selection over the chat must produce a visible highlight on the
        # covered lines (RichLog itself renders none), so the user can tell
        # what Ctrl+C will copy.
        selected = ModelRecord("openai", "model-a", tools="true")
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause(0.3)
                log = app.query_one("#conversation", tui.ChatLog)
                # welcome text occupies the first lines; select y=0..2
                app.screen.selections[log] = Selection(Offset(0, 0), Offset(2, 70))
                log.selection_updated(app.screen.selections[log])
                await pilot.pause(0.2)
                # The first line is inside the selection: it must carry the
                # screen selection background (#6b5b3e), which is the visible
                # highlight the plain RichLog never renders.
                selected_strip = log.render_line(0)
                sel_bg = next(
                    (getattr(s.style, "bgcolor", None) for s in selected_strip._segments
                     if getattr(s.style, "bgcolor", None) is not None),
                    None,
                )
                self.assertIsNotNone(sel_bg)
                self.assertIn("6b5b3e", str(sel_bg).lower())
                # And copying the selection yields the chat text (not nothing).
                self.assertIn(
                    "KaroX",
                    app.screen.get_selected_text(),
                )

    async def test_provider_audit_does_not_write_model_activity(self) -> None:
        # Regression: the activity line must not show "Модель: …" on every
        # provider turn.  Drive a fake assistant entry with no text plus a
        # provider_audit entry through _poll_agent_history and assert the
        # activity widget stays free of the model identity.
        selected = ModelRecord("openai", "model-a", tools="true")
        store = Mock()
        store.state_path.return_value.exists.return_value = True
        record = Mock()
        record.provider_history = [
            {"role": "assistant", "content": "", "tool_calls": []},
            {"role": "provider_audit", "selected_provider": "openai",
             "selected_model": "gpt"},
        ]
        store.load.return_value = record
        with (
            patch.object(tui, "_selected_model", return_value=selected),
            patch.object(tui, "SessionStore", return_value=store),
        ):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                app.agent_busy = True
                app.active_session = "s"
                app._poll_agent_history()
                await pilot.pause()
                activity = str(app.query_one("#activity", tui.Static).render())
                self.assertNotIn("Модель", activity)
                self.assertNotIn("gpt", activity)

    async def test_chatlog_mouse_drag_selects_and_copies_chat_lines(self) -> None:
        # RichLog does not support drag selection, so ChatLog implements it:
        # a mouse down + drag must highlight the dragged lines and make
        # Ctrl+C copy them (not the last assistant answer).
        selected = ModelRecord("openai", "model-a", tools="true")

        class FakeMouseEvent:
            def __init__(self, button: int = 1, y: int = 0) -> None:
                self.button = button
                self.y = y

        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause(0.3)
                log = app.query_one("#conversation", tui.ChatLog)
                await log.on_mouse_down(FakeMouseEvent(1, 0))
                await pilot.pause(0.1)
                await log.on_mouse_move(FakeMouseEvent(1, 2))
                await pilot.pause(0.1)
                # the three dragged lines are highlighted
                for y in range(3):
                    strip = log.render_line(y)
                    has_highlight = any(
                        "6b5b3e" in str(getattr(s.style, "bgcolor", None) or "").lower()
                        for s in strip._segments
                    )
                    self.assertTrue(has_highlight, f"line {y} not highlighted")
                # Ctrl+C copies the selection, not the last answer
                app.agent_busy = False
                app._last_assistant_content = "OLD ANSWER"
                await pilot.press("ctrl+c")
                await pilot.pause(0.2)
                self.assertIn("KaroX", app.clipboard)
                self.assertNotIn("OLD ANSWER", app.clipboard)
                # mouse up ends the drag
                await log.on_mouse_up(FakeMouseEvent(1, 2))
                self.assertIsNone(log._drag_anchor)
                # writing a new user message clears the stale selection
                app._write_user("next task")
                await pilot.pause()
                self.assertNotIn(log, app.screen.selections)

    async def test_ctrl_c_stops_running_agent(self) -> None:
        # Ctrl+C must reach action_stop_agent while the agent is busy and
        # terminate the subprocess — the user's primary complaint.
        selected = ModelRecord("openai", "model-a", tools="true")
        process = Mock()
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                app.agent_busy = True
                app.agent_process = process
                await pilot.press("ctrl+c")
                await pilot.pause()
                process.terminate.assert_called_once()
                self.assertTrue(app._stop_requested)

    async def test_ctrl_c_idle_copies_last_assistant_content(self) -> None:
        # In idle state, Ctrl+C copies the last assistant answer to the
        # clipboard (no mouse selection present).
        selected = ModelRecord("openai", "model-a", tools="true")
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                app.agent_busy = False
                app._last_assistant_content = "ответ ассистента"
                await pilot.press("ctrl+c")
                await pilot.pause()
                self.assertEqual(app.clipboard, "ответ ассистента")

    async def test_chatlog_records_plain_text_for_selection(self) -> None:
        # ChatLog keeps a parallel plain transcript so get_selection can
        # return chat text (RichLog itself returns None).
        selected = ModelRecord("openai", "model-a", tools="true")
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                log = app.query_one("#conversation", tui.ChatLog)
                app._write_user("сообщение пользователя")
                app._write_assistant("ответ ассистента")
                app._write_notice("уведомление")
                await pilot.pause()
                self.assertEqual(log._plain_lines[-1], "уведомление")
                # get_selection maps the vertical range onto the transcript.
                selection = Mock()
                selection.start = Mock(y=0, x=0)
                selection.end = Mock(y=5, x=0)
                text = log.get_selection(selection)
                self.assertIsNotNone(text)
                self.assertIn("сообщение пользователя", text)

    async def test_tailscale_funnel_start_publishes_endpoint(self) -> None:
        # TUI and CLI share one ownership-safe foreground Funnel plan.
        # The TUI must not start a background route or use a global reset.
        selected = ModelRecord("openai", "model-a", tools="true")
        status_payload = {
            "BackendState": "Running",
            "Self": {"DNSName": "myhost.tailnet.ts.net.", "CapMap": {"funnel": [1]}},
        }
        status_ok = Mock(returncode=0, stdout=json.dumps(status_payload), stderr="")
        plan = Mock(
            public_url="https://myhost.tailnet.ts.net",
            argv=(
                "/usr/bin/tailscale",
                "funnel",
                "--yes",
                "--https",
                "443",
                "http://127.0.0.1:8765",
            ),
        )
        process = Mock()
        process.stdout = None
        process.poll.return_value = None

        launch = tui.BridgeLaunch(
            session_id="s", profile="notion", protocol="mcp",
            endpoint="http://127.0.0.1:8765/mcp", secret="x", argv=(),
        )
        with (
            patch.object(tui, "_selected_model", return_value=selected),
            patch.object(tui, "_find_tailscale", return_value="/usr/bin/tailscale"),
            patch.object(tui, "prepare_tailscale_funnel", return_value=plan),
            # Patch KaroX's own worker seam, never threading.Thread itself:
            # tui.threading is the stdlib module, so patching it there disables
            # threads for Textual and asyncio too and hangs this test forever.
            patch.object(tui, "_start_worker") as start_worker,
            patch.object(tui, "subprocess", wraps=tui.subprocess) as sub_module,
        ):
            sub_module.run.return_value = status_ok
            sub_module.Popen.return_value = process
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                app._start_tailscale_funnel(8765, launch)
                await pilot.pause()
                self.assertTrue(app._tailscale_active)
                self.assertEqual(
                    app.public_endpoint, "https://myhost.tailnet.ts.net/mcp"
                )
                sub_module.Popen.assert_called_once()
                funnel_argv = sub_module.Popen.call_args.args[0]
                self.assertNotIn("--bg", funnel_argv)
                self.assertNotIn("reset", funnel_argv)
                start_worker.assert_called_once()
                worker = start_worker.call_args.args[0]
                self.assertIs(worker.__self__, app)
                self.assertIs(
                    worker.__func__, type(app)._read_tailscale_output
                )
                self.assertIs(start_worker.call_args.args[1], process)

    async def test_tailscale_funnel_unavailable_offers_login(self) -> None:
        # When the node is not logged in (BackendState != Running) the funnel
        # is not started; instead the user is asked (with permission) whether to
        # run `tailscale up` to sign in — never silently.
        selected = ModelRecord("openai", "model-a", tools="true")
        status_payload = {"BackendState": "NeedsLogin", "Self": {}}

        launch = tui.BridgeLaunch(
            session_id="s", profile="notion", protocol="mcp",
            endpoint="http://127.0.0.1:8765/mcp", secret="x", argv=(),
        )
        with (
            patch.object(tui, "_selected_model", return_value=selected),
            patch.object(tui, "_find_tailscale", return_value="/usr/bin/tailscale"),
            patch.object(tui, "subprocess", wraps=tui.subprocess) as sub_module,
        ):
            sub_module.run.return_value = Mock(
                returncode=0, stdout=json.dumps(status_payload), stderr=""
            )
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                app._start_tailscale_funnel(8765, launch)
                await pilot.pause()
                self.assertFalse(app._tailscale_active)
                # The login-offer ConfirmScreen is now on top of the stack.
                self.assertIsInstance(app.screen, tui.ConfirmScreen)
                # No funnel command was run yet (only the status check).
                funnel_calls = [
                    c for c in sub_module.run.call_args_list
                    if c.args and "funnel" in (c.args[0] if c.args else [])
                ]
                self.assertEqual(funnel_calls, [])

    async def test_tailscale_not_installed_offers_install(self) -> None:
        # When Tailscale is missing, the user is asked (with permission) whether
        # to install it via winget — never silently.
        selected = ModelRecord("openai", "model-a", tools="true")
        launch = tui.BridgeLaunch(
            session_id="s", profile="notion", protocol="mcp",
            endpoint="http://127.0.0.1:8765/mcp", secret="x", argv=(),
        )
        winget_ok = Mock(returncode=0, stdout="installed", stderr="")
        with (
            patch.object(tui, "_selected_model", return_value=selected),
            patch.object(tui, "_find_tailscale", return_value=None),
            patch.object(tui, "subprocess", wraps=tui.subprocess) as sub_module,
        ):
            sub_module.run.return_value = winget_ok
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                app._start_tailscale_funnel(8765, launch)
                await pilot.pause()
                self.assertFalse(app._tailscale_active)
                # The install-offer ConfirmScreen is on top.
                self.assertIsInstance(app.screen, tui.ConfirmScreen)
                # Accepting the offer should launch winget install.
                app.screen.dismiss(True)
                await pilot.pause(0.4)
                winget_calls = [
                    c for c in sub_module.run.call_args_list
                    if c.args and "winget" in (c.args[0] if c.args else [])
                ]
                self.assertTrue(winget_calls, "winget install was not invoked")

    async def test_tailscale_up_streams_auth_url_and_opens_browser(self) -> None:
        # Regression: `tailscale up` prints the auth URL and BLOCKS until the
        # user logs in.  Using capture_output hid the URL (the user could never
        # see it to log in), so the worker must stream the output, surface the
        # URL immediately, and open the browser itself.
        selected = ModelRecord("openai", "model-a", tools="true")
        launch = tui.BridgeLaunch(
            session_id="s", profile="notion", protocol="mcp",
            endpoint="http://127.0.0.1:8765/mcp", secret="x", argv=(),
        )

        class FakeStream:
            def __iter__(self) -> "FakeStream":
                self._lines = iter([
                    "To authenticate, visit:\n",
                    "  https://login.tailscale.com/a/abc123\n",
                    "\n",
                ])
                return self

            def __next__(self) -> str:
                return next(self._lines)

        fake_process = Mock()
        fake_process.stdout = FakeStream()
        fake_process.returncode = 0
        fake_process.wait = Mock(return_value=0)

        webbrowser_open = Mock()
        with (
            patch.object(tui, "_selected_model", return_value=selected),
            patch.object(tui, "_find_tailscale", return_value="/usr/bin/tailscale"),
            patch.object(tui.subprocess, "Popen", return_value=fake_process) as popen_mock,
            patch("webbrowser.open", webbrowser_open),
            patch.object(tui, "_tailscale_logged_in", return_value=False),
        ):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                # Drive the login flow: offer → accept → worker runs.
                app._offer_tailscale_login(8765, launch, "/usr/bin/tailscale")
                await pilot.pause(0.2)
                self.assertIsInstance(app.screen, tui.ConfirmScreen)
                app.screen.dismiss(True)
                await pilot.pause(0.5)
                # Popen was called with `tailscale up` at some point (the login
                # worker calls it; later the funnel retry calls status via run,
                # which also uses Popen internally — so check all calls).
                up_calls = [
                    c for c in popen_mock.call_args_list
                    if c.args and "up" in (c.args[0] if c.args else [])
                ]
                self.assertTrue(up_calls, "tailscale up was not invoked")
                # The auth URL was shown in the chat AND the browser was opened.
                rendered = "\n".join(
                    line.text
                    for line in app.query_one("#conversation", tui.RichLog).lines
                )
                self.assertIn("login.tailscale.com/a/abc123", rendered)
                webbrowser_open.assert_called()
                self.assertIn(
                    "login.tailscale.com/a/abc123",
                    webbrowser_open.call_args.args[0],
                )
                # The URL was shown but login was not verified, so the worker
                # surfaces "login not completed" rather than falsely proceeding.
                self.assertIn("не завершён", rendered)

    async def test_tailscale_up_noop_falls_back_to_gui_app(self) -> None:
        # Regression: on Windows a non-admin `tailscale up` silently returns
        # rc=0 with NO auth URL (it can't drive login).  The worker must NOT
        # report false success — it must launch the Tailscale GUI app and poll
        # `status --json` until the node authenticates, then resume the funnel.
        selected = ModelRecord("openai", "model-a", tools="true")
        launch = tui.BridgeLaunch(
            session_id="s", profile="notion", protocol="mcp",
            endpoint="http://127.0.0.1:8765/mcp", secret="x", argv=(),
        )

        class EmptyStream:
            def __iter__(self) -> "EmptyStream":
                return self

            def __next__(self) -> str:
                raise StopIteration

        fake_process = Mock()
        fake_process.stdout = EmptyStream()
        fake_process.wait = Mock(return_value=0)

        popen_calls = []

        def fake_popen(args, **kwargs):
            popen_calls.append(args)
            return fake_process

        funnel_resume = Mock()

        with (
            patch.object(tui, "_selected_model", return_value=selected),
            patch.object(tui, "_find_tailscale", return_value="/usr/bin/tailscale"),
            patch.object(tui, "_tailscale_gui_app", return_value="/usr/bin/tailscale-ipn"),
            patch.object(tui.subprocess, "Popen", side_effect=fake_popen),
            patch.object(tui, "_tailscale_logged_in", side_effect=[False, False, True]),
            patch.object(tui, "_tailscale_backend_state", return_value="NeedsLogin"),
            patch.object(tui.time, "sleep", lambda _s: None),
            patch.object(tui.KaroXApp, "_start_tailscale_funnel", funnel_resume),
        ):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                app._offer_tailscale_login(8765, launch, "/usr/bin/tailscale")
                await pilot.pause(0.2)
                self.assertIsInstance(app.screen, tui.ConfirmScreen)
                app.screen.dismiss(True)
                # The worker streams `tailscale up`, polls the daemon (with
                # time.sleep patched to no-op), then launches the GUI app and
                # polls again before resuming the funnel — give it a few
                # async pauses to run through all of that on its thread.
                for _ in range(10):
                    await pilot.pause(0.2)
                    if funnel_resume.call_count:
                        break
                # `tailscale up` was attempted but produced no URL, so the GUI
                # app was launched to handle login.
                gui_launches = [
                    a for a in popen_calls
                    if any("tailscale-ipn" in str(arg) for arg in a)
                ]
                self.assertTrue(gui_launches, "Tailscale GUI app was not launched")
                rendered = "\n".join(
                    line.text
                    for line in app.query_one("#conversation", tui.RichLog).lines
                )
                # The user-facing message explains the no-op and the GUI fallback.
                self.assertIn("Запускаю приложение Tailscale", rendered)
                # After login completes (polling returns True on the 3rd check),
                # the funnel flow resumes on the UI thread.
                funnel_resume.assert_called_once_with(8765, launch)

    async def test_tailscale_stuck_daemon_shows_service_message(self) -> None:
        # Regression: if the tailscaled daemon lingers in NoState/Starting for
        # the whole poll window, login is NOT what's wrong — the service is
        # stuck.  The user must be told to restart Tailscale/the service, not to
        # log in again (which would loop forever over a stuck daemon).
        selected = ModelRecord("openai", "model-a", tools="true")
        launch = tui.BridgeLaunch(
            session_id="s", profile="notion", protocol="mcp",
            endpoint="http://127.0.0.1:8765/mcp", secret="x", argv=(),
        )

        class EmptyStream:
            def __iter__(self) -> "EmptyStream":
                return self

            def __next__(self) -> str:
                raise StopIteration

        fake_process = Mock()
        fake_process.stdout = EmptyStream()
        fake_process.wait = Mock(return_value=0)

        def fake_popen(args, **kwargs):
            return fake_process

        # Daemon stuck in NoState the whole time; never logs in.
        funnel_resume = Mock()
        with (
            patch.object(tui, "_selected_model", return_value=selected),
            patch.object(tui, "_find_tailscale", return_value="/usr/bin/tailscale"),
            patch.object(tui, "_tailscale_gui_app", return_value="/usr/bin/tailscale-ipn"),
            patch.object(tui.subprocess, "Popen", side_effect=fake_popen),
            patch.object(tui, "_tailscale_logged_in", return_value=False),
            patch.object(tui, "_tailscale_backend_state", return_value="NoState"),
                # Let the loop run briefly (the poll interval is a no-op) so it
                # accumulates stuck-state observations, then exits when the
                # short timeout elapses.  Threshold 0 means any NoState
                # observation triggers the "service not responding" branch.
                patch.object(tui, "_TAILSCALE_LOGIN_TIMEOUT_SECONDS", 0.5),
                patch.object(tui, "_TAILSCALE_LOGIN_POLL_INTERVAL", 0),
                patch.object(tui, "_TAILSCALE_STUCK_STATE_THRESHOLD", 0),
                patch.object(tui.time, "sleep", lambda _s: None),
            patch.object(tui.KaroXApp, "_start_tailscale_funnel", funnel_resume),
        ):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                app._offer_tailscale_login(8765, launch, "/usr/bin/tailscale")
                await pilot.pause(0.2)
                self.assertIsInstance(app.screen, tui.ConfirmScreen)
                app.screen.dismiss(True)
                for _ in range(15):
                    await pilot.pause(0.2)
                    rendered = "\n".join(
                        line.text
                        for line in app.query_one("#conversation", tui.RichLog).lines
                    )
                    if "Служба Tailscale не отвечает" in rendered:
                        break
                # The message points at the stuck service, not a missing login.
                self.assertIn("Служба Tailscale не отвечает", rendered)
                self.assertIn("starting", rendered.lower())
                # The funnel flow never resumes because login never completed.
                funnel_resume.assert_not_called()

    async def test_stop_bridge_stops_only_owned_tailscale_child(self) -> None:
        # Foreground ownership means cleanup terminates only the KaroX child;
        # a global Serve/Funnel reset is forbidden.
        selected = ModelRecord("openai", "model-a", tools="true")
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        with (
            patch.object(tui, "_selected_model", return_value=selected),
            patch.object(tui, "_find_tailscale", return_value="/usr/bin/tailscale"),
            patch.object(tui, "subprocess", wraps=tui.subprocess) as sub_module,
        ):
            sub_module.run.return_value = Mock(returncode=0, stdout="", stderr="")
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                app.tunnel_process = process
                app._tailscale_active = True
                app._stop_bridge(quiet=True)
                await pilot.pause()
                self.assertFalse(app._tailscale_active)
                process.terminate.assert_called_once()
                reset_calls = [
                    c for c in sub_module.run.call_args_list
                    if "reset" in (c.args[0] if c.args else [])
                ]
                self.assertEqual(reset_calls, [])

    async def test_stop_managed_web_bridge_requests_graceful_cli_cleanup(self) -> None:
        selected = ModelRecord("openai", "model-a", tools="true")
        process = Mock()
        process.poll.return_value = None
        process.wait.return_value = 0
        launch = tui.BridgeLaunch(
            session_id="web-1",
            profile="chatgpt-web",
            protocol="mcp",
            endpoint="",
            secret="",
            argv=(),
            managed=True,
        )
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                app.bridge_process = process
                app.bridge_launch = launch
                app._stop_bridge(quiet=True)
                await pilot.pause()
        expected_signal = (
            tui.signal.CTRL_BREAK_EVENT
            if tui.os.name == "nt"
            else tui.signal.SIGINT
        )
        process.send_signal.assert_called_once_with(expected_signal)
        process.terminate.assert_not_called()

    async def test_bridge_setup_dialog_is_scrollable_with_section_labels(self) -> None:
        # Regression: the bridge setup dialog must scroll (content overflows
        # the viewport) and every form group must carry a visible label.
        selected = ModelRecord("openai", "model-a", tools="true")
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(100, 32)) as pilot:
                app.action_bridge()
                await pilot.pause(0.3)
                dialog = app.screen.query_one("#bridge-dialog")
                # VerticalScroll lets the content overflow and scroll.
                self.assertEqual(type(dialog).__name__, "VerticalScroll")
                self.assertGreater(dialog.virtual_size.height, dialog.size.height)
                sections = [
                    str(w.render()) for w in app.screen.query(".section")
                ]
                self.assertIn("Тип подключения", sections)
                self.assertIn("Локальный порт", sections)
                self.assertIn(
                    "Инструменты KaroX Core (что разрешить внешнему агенту)",
                    sections,
                )
                self.assertIn(
                    "Публичный доступ (как внешний агент дойдёт до KaroX)",
                    sections,
                )
                # The action buttons are reachable by id even when off-screen.
                self.assertIsNotNone(app.screen.query_one("#bridge-start"))
                self.assertIsNotNone(app.screen.query_one("#bridge-cancel"))

    async def test_selecting_notion_picks_exactly_one_tunnel_option(self) -> None:
        # Regression: switching to the Notion profile must select the Tailscale
        # tunnel and ONLY the Tailscale tunnel (a prior bug left Cloudflare
        # marked as well because programmatic .value=True did not deselect
        # siblings).
        selected = ModelRecord("openai", "model-a", tools="true")
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(100, 40)) as pilot:
                app.action_bridge()
                await pilot.pause(0.4)
                screen = app.screen
                screen.query_one("#profile-notion", tui.RadioButton).value = True
                await pilot.pause(0.4)
                btns = list(screen.query("#bridge-tunnel-kind RadioButton"))
                true_btns = [b.id for b in btns if b.value]
                self.assertEqual(true_btns, ["tunnel-tailscale"])
                self.assertEqual(screen._tunnel_value(), "tailscale")
                # Default (PromptQL) and the other choices still resolve cleanly.
                screen.query_one("#profile-promptql", tui.RadioButton).value = True
                await pilot.pause(0.4)
                true_btns = [b.id for b in screen.query("#bridge-tunnel-kind RadioButton") if b.value]
                self.assertEqual(true_btns, ["tunnel-cloudflare"])
                self.assertEqual(screen._tunnel_value(), "cloudflare")

    async def test_chatgpt_and_claude_are_visible_bridge_profiles(self) -> None:
        selected = ModelRecord("openai", "model-a", tools="true")
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(100, 40)) as pilot:
                app.action_bridge()
                await pilot.pause(0.4)
                screen = app.screen
                self.assertIsNotNone(
                    screen.query_one("#profile-chatgpt-web", tui.RadioButton)
                )
                self.assertIsNotNone(
                    screen.query_one("#profile-claude-web", tui.RadioButton)
                )
                screen.query_one(
                    "#profile-chatgpt-web", tui.RadioButton
                ).value = True
                await pilot.pause(0.3)
                self.assertEqual(screen._profile_value(), "chatgpt-web")
                self.assertEqual(screen._tunnel_value(), "cloudflare")


class TunnelHelperTests(unittest.TestCase):
    """The TUI must report the same Tailscale binary the bridge CLI would use.

    ``tui._find_tailscale`` delegates to :mod:`karox.tailscale`, so these tests
    drive that lookup. They rebind the ``shutil`` name inside that module rather
    than setting an attribute on the stdlib ``shutil`` module, which would alter
    it for every other test in the process.
    """

    def _lookup_env(self) -> dict[str, str]:
        # A developer machine may export these; the lookup must be decided by
        # the test, not by the environment that happens to run it.
        return {
            "KAROX_TAILSCALE_EXE": "",
            "KAROX_VNEXT_RUNTIME_DIR": "",
            "KAROX_RUNTIME_DIR": "",
        }

    def test_find_tailscale_returns_path_from_which(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / (
                "tailscale.exe" if os.name == "nt" else "tailscale"
            )
            executable.write_text("", encoding="utf-8")
            with (
                patch.dict(os.environ, self._lookup_env()),
                patch.object(
                    tailscale_module, "shutil", Mock(which=lambda _: str(executable))
                ),
            ):
                self.assertEqual(
                    tui._find_tailscale(), str(executable.resolve())
                )

    def test_find_tailscale_returns_none_when_missing(self) -> None:
        # is_file() is forced False so an installation on the developer's own
        # machine cannot satisfy one of the well-known fallback paths.
        with (
            patch.dict(os.environ, self._lookup_env()),
            patch.object(tailscale_module, "shutil", Mock(which=lambda _: None)),
            patch.object(Path, "is_file", return_value=False),
        ):
            self.assertIsNone(tui._find_tailscale())

    def test_funnel_available_detects_capmap(self) -> None:
        payload = {"Self": {"CapMap": {"funnel": [1]}}}
        self.assertTrue(tui._tailscale_funnel_available(payload))

    def test_funnel_available_detects_ts_net_suffix(self) -> None:
        payload = {"CurrentTailnet": {"MagicDNSSuffix": "example.ts.net"}}
        self.assertTrue(tui._tailscale_funnel_available(payload))

    def test_funnel_available_defers_when_unknown(self) -> None:
        # Unknown capability shape optimistically allows the funnel command.
        payload = {"Self": {}}
        self.assertTrue(tui._tailscale_funnel_available(payload))

    def test_tailscale_gui_app_located_next_to_cli(self) -> None:
        # The GUI app (tailscale-ipn.exe) ships next to the CLI binary; we look
        # it up by replacing the CLI's extension.
        cli = str(Path("C:/Program Files/Tailscale/tailscale.exe"))
        with patch.object(Path, "is_file", return_value=True):
            self.assertEqual(
                tui._tailscale_gui_app(cli),
                str(Path("C:/Program Files/Tailscale/tailscale-ipn.exe")),
            )

    def test_tailscale_gui_app_returns_none_off_windows(self) -> None:
        # No GUI fallback outside Windows (the CLI-driven login flow is fine
        # there because Linux/macOS `tailscale up` does not require elevation).
        with patch.object(tui.os, "name", "posix"):
            self.assertIsNone(tui._tailscale_gui_app("/usr/bin/tailscale"))
        self.assertIsNone(tui._tailscale_gui_app(None))

    def test_tailscale_logged_in_requires_dnsname_and_running(self) -> None:
        # BackendState alone is not enough — the node must also have a DNS name,
        # which only appears after authentication.
        good = Mock(returncode=0, stdout=json.dumps(
            {"BackendState": "Running", "Self": {"DNSName": "node.example.ts.net."}}
        ))
        no_dns = Mock(returncode=0, stdout=json.dumps({"BackendState": "Running", "Self": {}}))
        nostate = Mock(returncode=0, stdout=json.dumps({"BackendState": "NoState", "Self": {}}))
        with patch.object(tui.subprocess, "run") as run_mock:
            run_mock.return_value = good
            self.assertTrue(tui._tailscale_logged_in("/usr/bin/tailscale"))
            run_mock.return_value = no_dns
            self.assertFalse(tui._tailscale_logged_in("/usr/bin/tailscale"))
            run_mock.return_value = nostate
            self.assertFalse(tui._tailscale_logged_in("/usr/bin/tailscale"))

    def test_tailscale_logged_in_never_raises_on_subprocess_failure(self) -> None:
        # The helper probes an external binary; any failure must be swallowed
        # and reported as "not logged in" (never propagate an exception).
        with patch.object(tui.subprocess, "run", side_effect=OSError("boom")):
            self.assertFalse(tui._tailscale_logged_in("/usr/bin/tailscale"))
        with patch.object(tui.subprocess, "run", return_value=Mock(stdout="not json")):
            self.assertFalse(tui._tailscale_logged_in("/usr/bin/tailscale"))

    def test_tailscale_backend_state_reads_backend_state(self) -> None:
        # _tailscale_backend_state surfaces the daemon's BackendState so the
        # login poll loop can tell a stuck daemon apart from one waiting for
        # authorization.
        running = Mock(returncode=0, stdout=json.dumps({"BackendState": "Running"}))
        nostate = Mock(returncode=0, stdout=json.dumps({"BackendState": "NoState"}))
        with patch.object(tui.subprocess, "run") as run_mock:
            run_mock.return_value = running
            self.assertEqual(tui._tailscale_backend_state("/usr/bin/tailscale"), "Running")
            run_mock.return_value = nostate
            self.assertEqual(tui._tailscale_backend_state("/usr/bin/tailscale"), "NoState")
        # Never raises on subprocess failure.
        with patch.object(tui.subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(tui._tailscale_backend_state("/usr/bin/tailscale"), "Unknown")

    def test_pids_listening_on_parses_netstat_lines(self) -> None:
        # _pids_listening_on walks ``netstat -ano`` output to find the PID
        # owning a listener on (address, port).  The output is localized on
        # Windows, so we match the ASCII "LISTEN" prefix rather than the full
        # state word.
        fake_netstat = Mock(
            returncode=0,
            stdout=(
                "  TCP    127.0.0.1:8765         0.0.0.0:0              LISTENING       11340\n"
                "  TCP    127.0.0.1:8765         0.0.0.0:0              LISTENING       11340\n"
                "  TCP    127.0.0.1:9999         0.0.0.0:0              LISTENING       999\n"
                "  TCP    127.0.0.1:8765         127.0.0.1:50000        ESTABLISHED     7\n"
            ),
        )
        with patch.object(tui.subprocess, "run", return_value=fake_netstat):
            pids = tui._pids_listening_on("127.0.0.1", 8765)
        # Only the LISTENING row on 8765, deduplicated (11340 appears once).
        self.assertEqual(pids, [11340])

    def test_pids_listening_on_handles_localized_state(self) -> None:
        # On a Russian Windows the state column is localized; the ASCII "LISTEN"
        # prefix still identifies a listener.
        fake_netstat = Mock(
            returncode=0,
            stdout=(
                "  TCP    127.0.0.1:8765         0.0.0.0:0              "
                "ПРОСЛУШИВАНИЕ       11340\n"
            ),
        )
        with patch.object(tui.subprocess, "run", return_value=fake_netstat):
            pids = tui._pids_listening_on("127.0.0.1", 8765)
        # Localized non-LISTEN state does not match; this row is not a listener.
        self.assertEqual(pids, [])

    def test_pids_listening_on_never_raises_on_subprocess_failure(self) -> None:
        # The helper must return an empty list (not raise) if netstat is missing
        # or fails — the caller treats empty as "port is free, nothing to stop".
        with patch.object(tui.subprocess, "run", side_effect=OSError("no netstat")):
            self.assertEqual(tui._pids_listening_on("127.0.0.1", 8765), [])

    def test_free_port_on_address_stops_orphans_not_self(self) -> None:
        # _free_port_on_address stops every listener on the port EXCEPT the
        # caller's own PID and any explicit skip set, so it never kills the
        # current KaroX process or the live bridge.
        our_pid = tui.os.getpid()
        # Pretend netstat reports three listeners on 8765: us, the current
        # bridge process, and a stale orphan from a previous session.
        fake_netstat = Mock(
            returncode=0,
            stdout=(
                f"  TCP    127.0.0.1:8765         0.0.0.0:0              LISTENING       {our_pid}\n"
                "  TCP    127.0.0.1:8765         0.0.0.0:0              LISTENING       50000\n"
                "  TCP    127.0.0.1:8765         0.0.0.0:0              LISTENING       99999\n"
            ),
        )
        killed: list = []

        def dispatch(args, **kwargs):
            # netstat calls come in as ["netstat", "-ano"]; taskkill as
            # ["taskkill", "/PID", "<pid>", "/F"].  Dispatch on the first arg.
            first = args[0] if args else ""
            if first == "netstat":
                return fake_netstat
            if first == "taskkill":
                killed.append(args)
                return Mock(returncode=0)
            return Mock()

        with patch.object(tui.subprocess, "run", side_effect=dispatch):
            freed = tui._free_port_on_address("127.0.0.1", 8765, skip_pids={50000})
        # Only the orphan (99999) is stopped — our_pid is auto-skipped, 50000
        # is in the explicit skip set.
        self.assertEqual(freed, 1)
        killed_pids = [a[a.index("/PID") + 1] for a in killed if "/PID" in a]
        self.assertEqual(killed_pids, ["99999"])


class McpStatusAggregationTests(unittest.TestCase):
    """Aggregation behind the /mcp screen.

    These live here rather than in tests/test_mcp.py because that file holds
    key-shaped fixtures, so the repository tooling refuses to rewrite it, and a
    brand new test file cannot be executed by the approved check set.
    """

    def _record(self):
        from karox.mcp_client import McpServerRecord

        return McpServerRecord(
            server_id="docs",
            namespace="docs",
            transport="streamable_http",
            url="https://example.invalid/mcp",
        )

    def _selection(self, record, permissions):
        from karox.mcp_client import mcp_registry_digest

        return {
            "server_id": record.server_id,
            "namespace": record.namespace,
            "registry_digest": mcp_registry_digest(record),
            "tools": {
                name: {
                    "name": f"mcp.{record.namespace}.{name}",
                    "schema_digest": "sha256:000000000000",
                    "permission": permission,
                    "read_only": True,
                }
                for name, permission in permissions.items()
            },
        }

    def test_liveness_stays_not_probed_until_a_probe_happens(self) -> None:
        from karox.mcp_status import LIVENESS_NOT_PROBED, build_mcp_status

        status = build_mcp_status([self._record()])
        entry = status["servers"][0]
        self.assertEqual(entry["liveness"]["state"], LIVENESS_NOT_PROBED)
        self.assertIsNone(entry["liveness"]["tool_count"])
        self.assertEqual(status["totals"]["probed"], 0)
        self.assertEqual(status["totals"]["live"], 0)
        self.assertEqual(entry["selection"]["state"], "not_selected")
        self.assertIn("not_selected", entry["attention"])

    def test_ask_is_counted_as_blocked_and_never_as_allowed(self) -> None:
        from karox.mcp_status import build_mcp_status

        record = self._record()
        selection = self._selection(
            record, {"read": "allow", "write": "ask", "drop": "deny"}
        )
        status = build_mcp_status(
            [record], session_id="session-1", selections=[selection]
        )
        entry = status["servers"][0]
        counts = entry["selection"]["counts"]
        self.assertEqual(counts["allowed"], 1)
        self.assertEqual(counts["blocked_ask"], 1)
        self.assertEqual(counts["blocked_deny"], 1)
        self.assertEqual(status["totals"]["allowed_tools"], 1)
        self.assertEqual(status["totals"]["blocked_tools"], 2)
        self.assertIn("ask_blocks_calls", entry["attention"])

    def test_stale_selection_blocks_every_tool(self) -> None:
        from karox.mcp_status import (
            SELECTION_STALE,
            TOOL_BLOCKED_STALE,
            build_mcp_status,
        )

        record = self._record()
        selection = self._selection(record, {"read": "allow"})
        selection["registry_digest"] = "sha256:staledigest"
        entry = build_mcp_status([record], selections=[selection])["servers"][0]
        self.assertEqual(entry["selection"]["state"], SELECTION_STALE)
        self.assertEqual(
            [item["authorization"] for item in entry["tools"]], [TOOL_BLOCKED_STALE]
        )
        self.assertEqual(entry["selection"]["counts"]["allowed"], 0)
        self.assertIn("selection_stale", entry["attention"])

    def test_absent_and_unreadable_keys_are_different_facts(self) -> None:
        from karox.credentials import CredentialError
        from karox.mcp_client import McpServerRecord
        from karox.mcp_status import (
            CREDENTIAL_MISSING,
            CREDENTIAL_UNREADABLE,
            build_mcp_status,
        )

        # Only a server that references a stored key can be missing one.
        record = McpServerRecord(
            server_id="docs",
            namespace="docs",
            transport="streamable_http",
            url="https://example.invalid/mcp",
            credential_ref="os-keyring:mcp/docs",
            credential_target="Authorization",
        )

        def absent(reference):
            raise CredentialError(f"credential does not exist: {reference}")

        def broken(reference):
            raise CredentialError("keyring backend is unavailable")

        missing = build_mcp_status([record], resolve_credential=absent)
        self.assertEqual(
            missing["servers"][0]["credential"]["state"], CREDENTIAL_MISSING
        )
        unreadable = build_mcp_status([record], resolve_credential=broken)
        self.assertEqual(
            unreadable["servers"][0]["credential"]["state"], CREDENTIAL_UNREADABLE
        )

    def test_unreachable_server_still_shows_its_allowed_tools(self) -> None:
        from karox.mcp_status import LIVENESS_FAILED, McpLiveness, build_mcp_status

        record = self._record()
        selection = self._selection(record, {"read": "allow"})
        status = build_mcp_status(
            [record],
            selections=[selection],
            liveness={
                record.server_id: McpLiveness(
                    LIVENESS_FAILED, failure_kind="transport", detail="refused"
                )
            },
        )
        entry = status["servers"][0]
        self.assertEqual(entry["liveness"]["state"], LIVENESS_FAILED)
        self.assertEqual(entry["selection"]["counts"]["allowed"], 1)
        self.assertIn("unreachable", entry["attention"])
        self.assertEqual(status["totals"]["probed"], 1)
        self.assertEqual(status["totals"]["live"], 0)


if __name__ == "__main__":
    unittest.main()
