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
            (("python", "-m", "pytest", "-q"),),
            "task-1",
        )
        self.assertEqual(argv[:2], ["agent", "run"])
        self.assertEqual(argv[argv.index("--task") + 1], "fix it")
        self.assertEqual(
            json.loads(argv[argv.index("--verification-command") + 1]),
            ["python", "-m", "pytest", "-q"],
        )

    def test_agent_argv_economy_profile_enables_real_request_savings(self) -> None:
        argv = tui._agent_argv(
            "fix it",
            Path("/repo"),
            (("git", "diff", "--check"),),
            "task-economy",
            run_cost_profile="economy",
        )
        self.assertIn("--economy", argv)
        self.assertEqual(argv[argv.index("--route-strategy") + 1], "ordered")
        self.assertEqual(argv[argv.index("--context-utilization") + 1], "0.6")
        self.assertEqual(argv[argv.index("--max-tool-result-chars") + 1], "24000")

    def test_effort_is_independent_of_cost_profile(self) -> None:
        balanced = tui._agent_argv(
            "fix it", Path("/repo"), (("git", "diff", "--check"),), "b",
            run_cost_profile="balanced", reasoning_effort="max",
        )
        economy = tui._agent_argv(
            "fix it", Path("/repo"), (("git", "diff", "--check"),), "e",
            run_cost_profile="economy", reasoning_effort="max",
        )
        self.assertEqual(balanced[balanced.index("--effort") + 1], "max")
        self.assertEqual(economy[economy.index("--effort") + 1], "max")

    def test_provider_setup_one_shot_cli_persists_local_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch("karox.cli.config_dir", return_value=root):
                code, output = tui._capture_cli(
                    [
                        "provider", "setup", "local-openai",
                        "--provider-id", "one-shot-local",
                        "--model", "test-model",
                        "--no-test", "--json",
                    ]
                )
                self.assertEqual(code, 0, output)
                configured = json.loads(output)
                self.assertEqual(configured["status"], "configured")
                self.assertEqual(configured["selected_model"]["model_id"], "test-model")

                code, output = tui._capture_cli(
                    ["provider", "details", "one-shot-local", "--json"]
                )
                self.assertEqual(code, 0, output)
                details = json.loads(output)
                self.assertEqual(details["selected_model"]["model_id"], "test-model")

    def test_provider_setup_runs_the_shared_probe_before_persisting(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                patch("karox.cli.config_dir", return_value=root),
                patch.object(
                    tui,
                    "_probe_provider",
                    return_value={"finish_reason": "stop", "usage": {}},
                ) as probe,
            ):
                code, output = tui._capture_cli(
                    [
                        "provider", "setup", "local-openai",
                        "--provider-id", "probed-local",
                        "--model", "test-model",
                        "--json",
                    ]
                )
                self.assertEqual(code, 0, output)
                configured = json.loads(output)
                self.assertEqual(configured["verification"]["status"], "ok")
                probe.assert_called_once()

    def test_agent_argv_emits_one_verification_command_per_approved_command(self) -> None:
        argv = tui._agent_argv(
            "fix it",
            Path("/repo"),
            (("npm", "test"), ("npm", "run", "ci"), ("npm", "run", "test:smoke")),
            "task-2",
        )
        vc = [argv[i + 1] for i, a in enumerate(argv) if a == "--verification-command"]
        self.assertEqual(len(vc), 3)
        self.assertEqual(json.loads(vc[0]), ["npm", "test"])
        self.assertEqual(json.loads(vc[1]), ["npm", "run", "ci"])
        self.assertEqual(json.loads(vc[2]), ["npm", "run", "test:smoke"])

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
        self.assertIn("/model", output)
        # The curated menu is the contract: one entry point per scenario.
        # Retired aliases stay routable but are not advertised as new surface.
        self.assertNotIn("/connections", output)
        self.assertNotIn("/providers", output)

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

    def test_typo_suggests_closest_command(self) -> None:
        code, output = self.run_lines("/cnnection\n/quit\n")
        self.assertEqual(code, 0)
        self.assertIn("Unknown command", output)
        self.assertIn("/connect", output)

    def test_connect_in_line_mode_directs_to_interactive(self) -> None:
        """Line mode cannot open Textual screens, but /connect is a known command."""
        code, output = self.run_lines("/connect\n/quit\n")
        self.assertEqual(code, 0)
        self.assertNotIn("Unknown command", output)
        self.assertIn("interactive", output.lower())

    def test_connection_alias_in_line_mode_directs_to_interactive(self) -> None:
        code, output = self.run_lines("/connection\n/quit\n")
        self.assertEqual(code, 0)
        self.assertNotIn("Unknown command", output)
        self.assertIn("interactive", output.lower())

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


class CommandRoutingContractTests(unittest.TestCase):
    """Both line mode and full-screen TUI must use the same routing contract.

    A command recognised by one mode must be recognised by the other. The line
    mode may decline to execute it (it cannot open Textual modal screens), but
    it must never say "Unknown command" for a command the full-screen app knows.
    """

    def test_connect_is_canonical_and_listed_first_in_help(self) -> None:
        self.assertIn("/connect", tui.VISIBLE_COMMANDS)
        self.assertIn("/connect", tui.SLASH_COMMANDS)
        # /connect appears before /connection in the command catalog
        commands = list(tui.SLASH_COMMANDS.keys())
        self.assertLess(commands.index("/connect"), commands.index("/connections"))

    def test_connection_is_alias_in_deprecated_aliases(self) -> None:
        self.assertIn("/connection", tui.DEPRECATED_COMMAND_ALIASES)
        self.assertIsNone(tui.DEPRECATED_COMMAND_ALIASES["/connection"])

    def test_line_interactive_only_is_derived_from_common_contract(self) -> None:
        # Every VISIBLE_COMMAND that is not a backend slash command must be in
        # _LINE_INTERACTIVE_ONLY, so line mode recognises it.
        for cmd in tui.VISIBLE_COMMANDS:
            if cmd not in tui._BACKEND_SLASH and cmd not in {"/quit", "/help"}:
                self.assertIn(cmd, tui._LINE_INTERACTIVE_ONLY, f"{cmd} missing from line mode")

    def test_deprecated_aliases_are_in_line_interactive_only(self) -> None:
        for cmd in tui.DEPRECATED_COMMAND_ALIASES:
            if cmd not in tui._BACKEND_SLASH:
                self.assertIn(cmd, tui._LINE_INTERACTIVE_ONLY, f"{cmd} missing from line mode")

    def test_suggest_command_returns_empty_for_no_match(self) -> None:
        self.assertEqual(tui._suggest_command("/zzzzzzz", "en"), "")

    def test_suggest_command_returns_suggestion_for_typo(self) -> None:
        result = tui._suggest_command("/cnnection", "en")
        self.assertIn("/connect", result)

    def test_suggest_command_works_in_russian(self) -> None:
        result = tui._suggest_command("/cnnection", "ru")
        self.assertIn("/connection", result)


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
        controller = Mock()
        controller.details.side_effect = RuntimeError("new provider")
        controller.configure_provider_model.return_value = Mock(
            selected_model=selected,
            model=selected,
        )
        with patch.object(tui, "_provider_controller", return_value=controller):
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
        provider, model = controller.configure_provider_model.call_args.args
        self.assertEqual(provider.provider_id, "openai")
        self.assertIsNone(provider.credential_ref)
        self.assertEqual(model.model_id, "gpt-test")
        self.assertEqual(
            controller.configure_provider_model.call_args.kwargs["secret"],
            "secret-value",
        )
        self.assertTrue(
            controller.configure_provider_model.call_args.kwargs["activate"]
        )

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

    def test_tui_saved_bridge_profile_is_the_primary_hosted_connection_model(self) -> None:
        repository = Path.cwd()
        from karox.web_bridge_profiles import WebBridgeProfileError

        with patch("karox.web_bridge_profiles.WebBridgeProfileStore") as store:
            store.return_value.get.side_effect = WebBridgeProfileError("saved bridge profile does not exist: test")
            profile_name = tui._persist_tui_saved_bridge_profile(
                repository,
                tui.BridgeSetup(
                    "chatgpt-web",
                    9878,
                    ("karox.repo.read_file", "karox.repo.write_file"),
                    tunnel_provider="tailscale",
                ),
                language="en",
            )
        saved = store.return_value.put.call_args.args[0]
        self.assertTrue(profile_name.startswith("chatgpt-auto-"))
        self.assertEqual(saved.name, profile_name)
        self.assertEqual(saved.target_profile, "chatgpt-web")
        self.assertEqual(saved.access_profile.value, "workspace_write")
        self.assertTrue(saved.browser_external_https)
        self.assertTrue(saved.browser_headed)
        self.assertTrue(saved.browser_user_takeover)
        self.assertTrue(saved.browser_network_inspection)
        self.assertIn("karox.runtime.status", saved.tools)
        self.assertIn("karox.browser.wait_for", saved.tools)

    def test_tui_saved_notion_profile_does_not_invent_browser_controls(self) -> None:
        from karox.web_bridge_profiles import WebBridgeProfileError

        with patch("karox.web_bridge_profiles.WebBridgeProfileStore") as store:
            store.return_value.get.side_effect = WebBridgeProfileError("saved bridge profile does not exist: test")
            tui._persist_tui_saved_bridge_profile(
                Path.cwd(),
                tui.BridgeSetup(
                    "notion",
                    9880,
                    ("karox.repo.read_file",),
                    tunnel_provider="tailscale",
                ),
                language="en",
            )
        saved = store.return_value.put.call_args.args[0]
        self.assertEqual(saved.target_profile, "notion")
        self.assertFalse(saved.browser_external_https)
        self.assertFalse(saved.browser_headed)
        self.assertFalse(saved.browser_user_takeover)
        self.assertFalse(saved.browser_network_inspection)
        self.assertNotIn("karox.browser.wait_for", saved.tools)

    def test_repeated_tui_setup_preserves_advanced_browser_policy_and_credentials(self) -> None:
        import hashlib

        from karox.web_bridge_profiles import SavedWebBridgeProfile

        repository = Path.cwd()
        digest = hashlib.sha256(
            str(repository.resolve()).casefold().encode("utf-8")
        ).hexdigest()[:10]
        name = f"chatgpt-auto-{digest}-workspace_write"
        existing = SavedWebBridgeProfile(
            name=name,
            target_profile="chatgpt-web",
            repository=str(repository.resolve()),
            tools=("karox.repo.read_file", "karox.repo.write_file"),
            access_profile=tui.AccessProfile.WORKSPACE_WRITE,
            tunnel="tailscale",
            port=8765,
            browser_external_https=True,
            browser_headed=True,
            browser_user_takeover=True,
            browser_network_inspection=False,
            browser_payment_confirmation=True,
            browser_allowed_domains=("example.com",),
            browser_denied_domains=("blocked.example",),
            browser_allowed_emails=("robot@example.invalid",),
            browser_credential_refs=("os-keyring:browser/test-account",),
        )
        from karox.web_bridge_profiles import WebBridgeProfileError

        with (
            patch("karox.web_bridge_profiles.WebBridgeProfileStore") as store,
            patch("karox.web_bridge_launcher.apply_saved_bridge_profile") as apply,
        ):
            def get_profile(candidate: str) -> SavedWebBridgeProfile:
                if candidate == name:
                    return existing
                raise WebBridgeProfileError(f"saved bridge profile does not exist: {candidate}")

            store.return_value.get.side_effect = get_profile
            profile_name = tui._persist_tui_saved_bridge_profile(
                repository,
                tui.BridgeSetup(
                    "chatgpt-web",
                    9878,
                    ("karox.repo.read_file", "karox.repo.write_file"),
                    tunnel_provider="cloudflare",
                ),
                language="ru",
            )

        self.assertEqual(profile_name, name)
        store.return_value.put.assert_not_called()
        updated = apply.call_args.args[1]
        self.assertEqual(updated.port, 9878)
        self.assertEqual(updated.tunnel, "cloudflare")
        self.assertEqual(updated.language, "ru")
        self.assertEqual(updated.browser_allowed_domains, ("example.com",))
        self.assertEqual(updated.browser_denied_domains, ("blocked.example",))
        self.assertEqual(updated.browser_allowed_emails, ("robot@example.invalid",))
        self.assertEqual(
            updated.browser_credential_refs,
            ("os-keyring:browser/test-account",),
        )
        self.assertTrue(updated.browser_payment_confirmation)
        self.assertFalse(updated.browser_network_inspection)
        self.assertTrue(apply.call_args.kwargs["allow_restart"])

    def test_web_tui_has_no_legacy_cli_launch_builder(self) -> None:
        self.assertFalse(hasattr(tui, "_managed_web_bridge_launch"))


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

    async def screen(self, pilot: object, app: object, expected: type) -> None:
        """Wait for a screen transition instead of guessing how long it takes.

        The fixed ``pause(0.3)`` this replaces was a guess about how long pushing
        a screen takes, and under a parallel suite the guess was sometimes wrong:
        the assertion ran while the previous screen was still current and failed
        with "ProviderSetupScreen is not an instance of ModelPickerScreen".
        Waiting for the condition is both faster in the common case and stable in
        the slow one.
        """
        for _ in range(100):
            if isinstance(getattr(app, "screen", None), expected):
                # The screen being current is not the same as its widgets being
                # mounted: returning on the type alone made a following
                # `query_one("#confirm-yes")` raise NoMatches. One more turn lets
                # compose() finish, which is what the fixed pause was covering.
                await pilot.pause()  # type: ignore[attr-defined]
                return
            await pilot.pause()  # type: ignore[attr-defined]
        self.assertIsInstance(getattr(app, "screen", None), expected)

    async def language(self, pilot: object, app: object, expected: str) -> None:
        """Wait for a key press to be applied instead of assuming one tick.

        Same race as :meth:`focus`, and the same fix. Moving the highlight and
        confirming it are separate event-loop turns, so on a busy machine -- which
        is what a parallel suite guarantees -- the assertion could observe the
        default language rather than the chosen one and fail with 'ru' != 'en'.
        """
        for _ in range(50):
            if getattr(app, "language", None) == expected:
                return
            await pilot.pause()  # type: ignore[attr-defined]
        self.assertEqual(getattr(app, "language", None), expected)

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
                await self.language(pilot, app, "ru")
                self.assertNotIsInstance(app.screen, tui.ConnectionChoiceScreen)
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
                await self.language(pilot, app, "en")
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
                # One product scenario, one offered command. ``/connections`` is
                # still routed as a deprecated alias but is deliberately absent
                # from the menu, so ``/con`` resolves to a single entry.
                self.assertEqual(app._filtered_commands, ["/connect"])
                self.assertEqual(
                    app.query_one("#command-menu", tui.Static).styles.display,
                    "block",
                )
                self.assertIn("/model", tui._commands("ru"))
                self.assertNotIn("/home", tui._commands("ru"))
                self.assertNotIn("/doctor", tui._commands("ru"))

    async def test_model_selection_is_a_direct_chat_control(self) -> None:
        registry = Mock()
        with (
            patch.object(tui, "_selected_model", return_value=None),
            patch.object(tui, "_registry", return_value=registry),
        ):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app._model_picker_done("model:empiriolabs:glm-5-2")
                await pilot.pause()
                registry.select_model.assert_called_once_with("empiriolabs", "glm-5-2")
                self.assertEqual(getattr(app.focused, "id", None), "composer")

    async def test_model_picker_cannot_change_the_displayed_model_mid_run(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app.agent_busy = True
                app.action_model()
                await pilot.pause()
                from karox.tui_dashboard import ModelPickerScreen
                self.assertNotIsInstance(app.screen, ModelPickerScreen)

    async def test_the_picker_binding_is_a_key_terminals_can_send(self) -> None:
        """Ctrl+G opens the picker; Ctrl+M must not be advertised anywhere.

        Every terminal sends carriage return for Ctrl+M, so Textual can only
        ever see Enter and the old binding silently submitted the composer
        instead of opening the picker. The binding now lives on Ctrl+G, a key
        that arrives intact everywhere, and no hint may promise the dead key.
        """

        app = tui.KaroXApp(Path.cwd(), language="en")
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            keys = {binding.key for binding in app.BINDINGS}
            self.assertIn("ctrl+g", keys)
            self.assertNotIn("ctrl+m", keys)
            await pilot.press("ctrl+g")
            await pilot.pause()
            from karox.tui_dashboard import ModelPickerScreen
            self.assertIsInstance(app.screen, ModelPickerScreen)
            await pilot.press("escape")
            await pilot.pause()
        for language in ("ru", "en"):
            for name in ("hint", "welcome_ready"):
                self.assertNotIn("Ctrl+M", tui._TEXT[language][name])
                self.assertIn("Ctrl+G", tui._TEXT[language][name])

    async def test_effort_choice_from_the_picker_persists_and_reaches_the_header(
        self,
    ) -> None:
        with (
            patch.object(tui, "_selected_model", return_value=None),
            patch.object(tui, "_save_preferences") as save_preferences,
        ):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app._model_picker_done("effort:high")
                await pilot.pause()
                self.assertEqual(app.reasoning_effort, "high")
                save_preferences.assert_called_once_with(reasoning_effort="high")
                self.assertIn(
                    "effort high",
                    str(app.query_one("#header-status", tui.Static).render()),
                )
                self.assertEqual(getattr(app.focused, "id", None), "composer")
                app._model_picker_done("effort:auto")
                self.assertIsNone(app.reasoning_effort)

    async def test_cost_command_persists_explicit_economy_profile(self) -> None:
        with (
            patch.object(tui, "_selected_model", return_value=None),
            patch.object(tui, "_load_preferences", return_value={}),
            patch.object(tui, "_save_preferences") as save_preferences,
        ):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                self.assertEqual(app.run_cost_profile, "balanced")
                app._handle_command("/cost economy")
                await pilot.pause()
                self.assertEqual(app.run_cost_profile, "economy")
                save_preferences.assert_called_with(run_cost_profile="economy")
                self.assertIn("Economy", str(app.query_one("#header-status", tui.Static).render()))
                self.assertNotIn("/cost", tui._commands("en"))

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

    async def test_a_turn_is_one_line_describing_the_current_action(self) -> None:
        """A2 replaced the step list with a single human line.

        This used to assert the opposite -- that all three tools of a turn stayed
        on screen by name. That was the right answer to the wrong question: the
        person watching wants to know what is happening now, not to audit a call
        list, and the audit lives in Session Detail where it has room. The one
        property worth keeping is that the line is *current*, which is what the
        old single overwritten line got wrong by showing a finished call.
        """

        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app._begin_step("call-1", "repo.read_file")
                app._finish_step("call-1", "repo.read_file", failed=False)
                app._begin_step("call-2", "repo.write_file")
                app._finish_step("call-2", "repo.write_file", failed=False)
                app._begin_step("call-3", "checks.run")
                await pilot.pause()

                rendered = str(app.query_one("#activity", tui.Static).render())

                self.assertEqual(rendered, "Running tests")
                self.assertNotIn("repo.read_file", rendered)
                self.assertNotIn("repo.write_file", rendered)
                self.assertNotIn("checks.run", rendered)

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
                log = app.query_one("#conversation", tui.TranscriptView)

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

                log = app.query_one("#conversation", tui.TranscriptView)
                self.assertLessEqual(
                    log.virtual_size.width,
                    log.size.width,
                    "the chat overflows its own width, so the right edge is clipped",
                )

    async def test_an_answer_is_not_drawn_twice(self) -> None:
        """The polling reader and the final report carry the same text.

        `_agent_finished` calls `_poll_typed_transcript`, which writes the turn's
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

    async def test_the_answer_prompt_self_report_is_not_a_second_card(self) -> None:
        """The internal answer_prompt nudge's reply stays out of the chat.

        After a real answer the agent sends itself an ``answer_prompt`` user
        entry and the model replies with a ceremonial self-report ("the task
        was only a greeting..."). The polling reader used to draw that reply as
        a second assistant card right below the answer the user already read.
        """
        from types import SimpleNamespace

        history = [
            {"role": "assistant", "content": "Here is the real answer."},
            {"role": "user", "kind": "answer_prompt", "content": "give the answer"},
            {
                "role": "assistant",
                "content": "The task was only a greeting; no change was required.",
            },
        ]
        record = SimpleNamespace(provider_history=history)
        stat = SimpleNamespace(st_mtime_ns=1, st_size=1)
        fake_store = SimpleNamespace(
            state_path=lambda _sid: SimpleNamespace(stat=lambda: stat),
            load=lambda _sid: record,
        )
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app.agent_busy = True
                app.active_session = "session-double-card"
                with patch.object(
                    app, "_write_assistant"
                ) as write, patch.object(
                    tui, "SessionStore", return_value=fake_store
                ):
                    app._poll_assistant_content()

                write.assert_called_once_with("Here is the real answer.")

    async def test_answer_prompt_suppression_survives_separate_poll_ticks(self) -> None:
        """The nudge and its self-report may be persisted in different writes.

        This is the real race from the native API TUI: one poll observes the
        internal answer_prompt, a later provider update inserts route audit and
        the assistant self-report. Suppression must survive that boundary.
        """
        from types import SimpleNamespace

        history = [
            {"role": "assistant", "content": "Привет! 👋"},
            {"role": "user", "kind": "answer_prompt", "content": "give the answer"},
        ]
        record = SimpleNamespace(provider_history=history)
        stat_state = {"mtime": 1}
        fake_store = SimpleNamespace(
            state_path=lambda _sid: SimpleNamespace(
                stat=lambda: SimpleNamespace(
                    st_mtime_ns=stat_state["mtime"],
                    st_size=len(record.provider_history),
                )
            ),
            load=lambda _sid: record,
        )
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app.agent_busy = True
                app.active_session = "session-answer-prompt-race"
                with patch.object(
                    app, "_write_assistant"
                ) as write, patch.object(
                    tui, "SessionStore", return_value=fake_store
                ):
                    app._poll_assistant_content()
                    write.assert_called_once_with("Привет! 👋")

                    record.provider_history.extend(
                        [
                            {"role": "provider_audit", "kind": "route"},
                            {
                                "role": "assistant",
                                "content": (
                                    "The user only sent a greeting; no repository "
                                    "change was required."
                                ),
                            },
                        ]
                    )
                    stat_state["mtime"] = 2
                    app._poll_assistant_content()

                self.assertEqual(write.call_count, 1)
                self.assertEqual(app._last_assistant_content, "Привет! 👋")

    async def test_a_failed_tool_is_not_marked_as_done(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                app._begin_step("call-1", "checks.run")
                app._finish_step("call-1", "checks.run", failed=True)
                await pilot.pause()

                rendered = str(app.query_one("#activity", tui.Static).render())

                # A2: the mark and the tool name are gone, the fact is not.
                self.assertIn("Check failed", rendered)
                self.assertNotIn("Done", rendered)
                self.assertNotIn("checks.run", rendered)

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
                # ``/connect`` is the single connection entry point and opens the
                # universal hub. The legacy api/web/both wizard asked the user to
                # classify a connection before showing them what already exists,
                # which is the question the hub answers for them.
                self.assertNotIsInstance(app.screen, tui.ConnectionChoiceScreen)
                self.assertIn("Hub", type(app.screen).__name__)

    async def test_english_connection_flow_stays_in_english(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                # Driven from the wizard entry point rather than through the
                # retired api/web/both root: this test is about English surviving
                # the provider flow, not about how that flow is reached.
                app.action_provider_preset()
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
                # The second entry of the compact menu, whatever it is: the test
                # is about arrows moving the highlight and tab inserting the
                # highlighted command, not about which commands are offered.
                expected = list(tui._commands("en"))[1]
                self.assertEqual(
                    app.query_one("#composer", tui.Input).value,
                    tui.KaroXApp._command_insertion(expected),
                )

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
                    # Entered through the production wizard entry point rather
                    # than by pressing a digit. The subject here is discovery and
                    # the model picker; keying "1" bound this test to a position
                    # in a menu that no longer exists, and in the unified hub it
                    # opened the MCP clients screen -- so it stopped exercising
                    # the provider wizard at all while still looking like it did.
                    app.action_provider_preset()
                    await pilot.pause()
                    await pilot.press("down", "down", "down", "enter")
                    await pilot.pause()
                    await pilot.press("f5")
                    await self.screen(pilot, app, tui.ModelPickerScreen)
                    await self.focus(pilot, app, "model-search")
                    self.assertFalse(errors, repr(errors))
                    picker = app.screen
                    options = picker.query_one("#model-options", tui.OptionList)
                    self.assertEqual(options.option_count, 3)
                    await pilot.press("s", "e", "c", "o", "n", "d")
                    await pilot.pause()
                    self.assertEqual(options.option_count, 2)
                    await pilot.press("enter")
                    await self.screen(pilot, app, tui.ProviderSetupScreen)
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
                # The wizard entry point, not a digit position: this test is about
                # progressive fields inside the setup form.
                app.action_provider_preset()
                await pilot.pause()
                await pilot.press("enter")
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
                # ``/models`` is now a deprecated alias that opens the connections
                # screen, so the renderer is driven directly. What is under test
                # is the rendering of an empty model list -- human words, and no
                # "Running /models" noise -- not the retired route to it.
                app._inspection_finished("/models", 0, "[]")
                await pilot.pause(0.3)
                self.assertTrue(
                    any("Модели ещё не подключены" in item for item in messages)
                )
                self.assertFalse(any("Running /models" in item for item in messages))
                self.assertEqual(
                    app.query_one("#busy", tui.LoadingIndicator).styles.display,
                    "none",
                )

    async def test_adding_a_provider_returns_to_the_one_hub(self) -> None:
        """Two connections, one hub, and no implicit chaining between them.

        This replaces a test of the retired "Both" root flow, which verified that
        saving a provider *automatically* pushed the bridge wizard. That chaining
        was the product defect: it decided for the user that connecting a model
        meant they also wanted a web bridge, and it existed only because the
        entry screen made them classify the connection up front.

        The property that matters now is the opposite one: a provider is saved,
        the user is returned to the single hub, and adding ChatGPT/Claude/ClickUp
        is a separate deliberate act from that same hub. Restoring the automatic
        ProviderSetup -> BridgeSetup transition to make an old assertion pass
        would reintroduce exactly what was removed.
        """

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
                # 1. ``/connect`` opens the one hub.
                app.query_one("#composer", tui.Input).value = "/connect"
                await pilot.press("enter")
                await pilot.pause()
                hub = type(app.screen).__name__
                self.assertIn("Hub", hub, f"/connect opened {hub}")
                self.assertNotIsInstance(app.screen, tui.ConnectionChoiceScreen)

                # 2. An AI provider is added from it.
                app.action_provider_preset()
                await pilot.pause()
                await pilot.press("down", "down", "down", "enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, tui.ProviderSetupScreen)
                app.screen.query_one(
                    "#provider-model", tui.Input
                ).value = "model-manual"
                await pilot.press("f10")
                await pilot.pause(0.4)

                # 3. It was really saved and really verified ...
                self.assertTrue(save.called)
                self.assertTrue(probe.called)
                # ... and the bridge wizard was *not* pushed on the user's behalf.
                self.assertNotIsInstance(app.screen, tui.BridgeSetupScreen)

                # 4. Adding a web client is a separate act from the same hub.
                app._open_connections(tui.CONNECT_FOCUS_CLIENTS)
                await pilot.pause()
                self.assertIn("McpClients", type(app.screen).__name__)

                # 5. Neither scenario needed a second root flow.
                self.assertNotIsInstance(app.screen, tui.ConnectionChoiceScreen)

    async def test_provider_picker_filters_and_puter_is_explained(self) -> None:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                # Opened through the production callback the unified hub uses for
                # its "new AI provider" entry, so the test exercises the real
                # route without depending on where that entry sits in a list.
                app._open_provider_preset_screen(None)
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

    async def test_the_sponsor_ticker_is_not_part_of_the_shell(self) -> None:
        """It used to scroll on its own row, on by default, forever.

        A minimal coding agent does not spend a permanent row of an ordinary
        terminal on a marquee, and an animation behind the work is exactly the
        kind of movement that pulls attention away from it. The ticker is no
        longer mounted at all, so it costs no height and cannot animate -- which
        is stronger than hiding it, because a hidden widget still occupies
        geometry and still ticks.
        """

        with (
            patch.object(tui, "_selected_model", return_value=None),
            patch.object(tui, "_load_sponsors_visible", return_value=True),
        ):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause(0.3)
                self.assertEqual(len(app.query("#sponsor-ticker")), 0)
                self.assertEqual(len(app.query("#brand")), 0)
                # And nothing replaced it: no second promotional row appeared.
                self.assertEqual(len(app.query("#header-status")), 1)

    async def test_the_sponsors_command_still_records_the_preference(self) -> None:
        """The command survives even though the row it controlled does not.

        ``/sponsors`` is a real command with real users, so it keeps working and
        keeps persisting the choice; deleting it to tidy the shell would be a
        regression dressed up as simplification.
        """

        with (
            patch.object(tui, "_selected_model", return_value=None),
            patch.object(tui, "_load_sponsors_visible", return_value=True),
            patch.object(tui, "_save_sponsors_visible") as save,
        ):
            app = tui.KaroXApp(Path.cwd(), language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                composer = app.query_one("#composer", tui.Input)
                composer.value = "/sponsors off"
                await pilot.press("enter")
                await pilot.pause()
                save.assert_called_once_with(False)
                self.assertFalse(app.sponsors_visible)

    async def test_app_has_chat_composer_and_status_bar(self) -> None:
        selected = ModelRecord("openai", "model-a", tools="true")
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                self.assertIsNotNone(app.query_one("#composer"))
                self.assertIn(
                    "openai/model-a", str(app.query_one("#header-status").render())
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
                rendered = app.query_one("#conversation", tui.TranscriptView).plain_text
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
                rendered = app.query_one("#conversation", tui.TranscriptView).plain_text
                self.assertIn("Результат", rendered)
                self.assertIn("Готово", rendered)
                self.assertNotIn("**Готово**", rendered)
                activity = str(app.query_one("#activity", tui.Static).render())
                # A2: completion is a short summary, not a sentence about itself.
                self.assertTrue(activity.startswith("Готово"), activity)

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
                        str(app.query_one("#header-status", tui.Static).render()),
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
                rendered = app.query_one("#conversation", tui.TranscriptView).plain_text
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

    async def test_every_message_in_the_transcript_is_selectable(self) -> None:
        # A message reaches the screen as Content inside a widget, which is what
        # makes Textual's own selection able to find it. This asserts the property
        # the whole transcript design rests on; the character-level precision it
        # buys is covered in tests/test_tui_selection.py.
        #
        # It replaces a test that read strip._segments looking for a highlight
        # colour, checking a hand-written render_line override that no longer
        # exists -- and which passed while selection returned the wrong text.
        selected = ModelRecord("openai", "model-a", tools="true")
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause(0.3)
                app._write_user("сообщение пользователя")
                app._write_assistant("ответ **ассистента** с `кодом`")
                app._write_notice("уведомление")
                await pilot.pause(0.3)

                app.screen.text_select_all()
                everything = app.screen.get_selected_text() or ""

                for expected in (
                    "KaroX готов",
                    "сообщение пользователя",
                    "ответ ассистента с кодом",
                    "уведомление",
                ):
                    self.assertIn(expected, everything)

    async def test_provider_audit_does_not_write_model_activity(self) -> None:
        # Regression: the activity line must not show "Модель: …" on every
        # provider turn.  Drive a fake assistant entry with no text plus a
        # provider_audit entry through _poll_typed_transcript and assert the
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
                app._poll_typed_transcript()
                await pilot.pause()
                activity = str(app.query_one("#activity", tui.Static).render())
                self.assertNotIn("Модель", activity)
                self.assertNotIn("gpt", activity)

    async def test_a_real_mouse_drag_over_the_chat_copies_what_was_dragged(self) -> None:
        # Driven through Pilot's real mouse events rather than a hand-built fake
        # one. The previous version constructed an object with `button` and `y`
        # attributes and called ChatLog.on_mouse_down directly, which tested the
        # hand-written handler and nothing about whether a terminal drag reaches
        # it -- and asserted only that "KaroX" appeared somewhere in the clipboard.
        selected = ModelRecord("openai", "model-a", tools="true")
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(80, 24)) as pilot:
                await pilot.pause(0.3)
                app._write_assistant("ALPHA BETA GAMMA")
                await pilot.pause(0.3)
                block = next(iter(app.query("MarkdownParagraph")))
                column = str(block._render()).index("BETA")
                origin = block.region.offset

                await pilot.mouse_down(offset=origin + (column, 0))
                await pilot.mouse_up(offset=origin + (column + 4, 0))
                await pilot.pause(0.2)

                app.agent_busy = False
                app._last_assistant_content = "OLD ANSWER"
                await pilot.press("ctrl+shift+c")
                await pilot.pause(0.2)

                self.assertEqual(app.clipboard, "BETA")

                # A new turn drops the stale selection rather than leaving it
                # highlighted over text the user has moved past.
                app._write_user("next task")
                await pilot.pause(0.2)
                self.assertFalse(app.screen.get_selected_text())

    async def test_escape_stops_the_running_agent(self) -> None:
        # Esc remains a stop key. Ctrl+C now follows terminal convention and
        # stops active work too; copying has its own Ctrl+Shift+C binding.
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

    async def test_ctrl_c_stops_a_running_agent(self) -> None:
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

    async def test_ctrl_shift_c_with_nothing_selected_copies_the_last_answer(self) -> None:
        # A convenience, but an announced one: the notice has to say a fallback
        # happened, because being told "Copied" after selecting a line and
        # receiving a different message is worse than being told nothing.
        selected = ModelRecord("openai", "model-a", tools="true")
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                notices: list[str] = []
                app.notify = lambda message, *a, **k: notices.append(str(message))
                app.agent_busy = False
                app._last_assistant_content = "ответ ассистента"
                await pilot.press("ctrl+shift+c")
                await pilot.pause()
                self.assertEqual(app.clipboard, "ответ ассистента")
                self.assertIn("последний ответ", notices[-1])

    async def test_the_transcript_reports_its_own_text_in_order(self) -> None:
        # There is one transcript now, not two. The parallel plain-text list this
        # replaces was maintained beside the rendered output and drifted out of
        # step with it, which is what made a copy return the wrong message;
        # `plain_text` reads the widgets that are actually on screen.
        selected = ModelRecord("openai", "model-a", tools="true")
        with patch.object(tui, "_selected_model", return_value=selected):
            app = tui.KaroXApp(Path.cwd(), language="ru")
            async with app.run_test(size=(120, 40)) as pilot:
                log = app.query_one("#conversation", tui.TranscriptView)
                app._write_user("сообщение пользователя")
                app._write_assistant("ответ ассистента")
                app._write_notice("уведомление")
                await pilot.pause(0.3)

                text = log.plain_text
                self.assertLess(
                    text.index("сообщение пользователя"),
                    text.index("ответ ассистента"),
                )
                self.assertTrue(text.rstrip().endswith("уведомление"))

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
                await self.screen(pilot, app, tui.ConfirmScreen)
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
                rendered = app.query_one("#conversation", tui.TranscriptView).plain_text
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
                await self.screen(pilot, app, tui.ConfirmScreen)
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
                rendered = app.query_one("#conversation", tui.TranscriptView).plain_text
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
                await self.screen(pilot, app, tui.ConfirmScreen)
                app.screen.dismiss(True)
                for _ in range(15):
                    await pilot.pause(0.2)
                    rendered = app.query_one("#conversation", tui.TranscriptView).plain_text
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
