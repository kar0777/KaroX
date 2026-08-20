from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from _support import SRC  # noqa: F401
from _tui_harness import isolated_karox_directories
from karox import cli, tui
from karox.credentials import CredentialStore
from karox.provider_controller import ProviderController

SECRET = "e2e-provider-secret-not-for-display"
MODEL = "e2e-model"


class Handler(BaseHTTPRequestHandler):
    requests: list[dict[str, object]] = []

    def log_message(self, _format: str, *_args: object) -> None:
        return

    def send_body(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        type(self).requests.append({"method": "GET", "path": self.path, "authorization": self.headers.get("Authorization", "")})
        body = json.dumps({"object": "list", "data": [{"id": MODEL, "object": "model", "context_window": 32768, "max_output_tokens": 4096}]}).encode()
        self.send_body(body, "application/json")

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        self.rfile.read(length)
        type(self).requests.append({"method": "POST", "path": self.path, "authorization": self.headers.get("Authorization", "")})
        events = [
            {"id": "chatcmpl-e2e", "choices": [{"delta": {"content": "OK"}, "finish_reason": None}]},
            {"id": "chatcmpl-e2e", "choices": [{"delta": {}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
        ]
        text = "".join(f"data: {json.dumps(event)}\n\n" for event in events) + "data: [DONE]\n\n"
        self.send_body(text.encode(), "text/event-stream")


class CliProviderE2ETests(unittest.TestCase):
    def setUp(self) -> None:
        Handler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base_url = f"http://{host}:{port}/v1"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def run_cli(self, *args: str) -> object:
        env = dict(os.environ)
        env["KAROX_PROVIDER_E2E_API_KEY"] = SECRET
        env["PYTHONPATH"] = str(SRC) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        launcher = shutil.which("karox")
        self.assertIsNotNone(launcher, "the karox console script is not installed on PATH")
        result = subprocess.run([str(launcher), *args], cwd=SRC.parent, env=env, capture_output=True, text=True, timeout=30, check=False)
        self.assertNotIn(SECRET, result.stdout)
        self.assertNotIn(SECRET, result.stderr)
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        return json.loads(result.stdout)

    def test_real_cli_provider_round_trip(self) -> None:
        with isolated_karox_directories():
            self.run_cli("provider", "add", "e2e-provider", "--adapter", "openai_compatible_chat", "--base-url", self.base_url, "--credential-ref", "env:KAROX_PROVIDER_E2E_API_KEY", "--json")
            self.run_cli("model", "add", "e2e-provider", MODEL, "--context-window", "32768", "--max-output-tokens", "4096", "--json")
            self.run_cli("model", "select", "e2e-provider", MODEL, "--json")
            discovered = self.run_cli("model", "discover", "--provider", "e2e-provider", "--json")
            self.assertIn(MODEL, json.dumps(discovered))
            tested = self.run_cli("provider", "test", "e2e-provider", "--model", MODEL, "--json")
            self.assertEqual(tested["status"], "ok")
            details = self.run_cli("provider", "details", "e2e-provider", "--json")
            shown = json.dumps(details)
            self.assertIn("env:KAROX_PROVIDER_E2E_API_KEY", shown)
            self.assertNotIn(SECRET, shown)
        self.assertTrue(any(item["method"] == "GET" for item in Handler.requests))
        self.assertTrue(any(item["method"] == "POST" for item in Handler.requests))
        self.assertTrue(any(item["authorization"] == f"Bearer {SECRET}" for item in Handler.requests if item["method"] == "POST"))

    def test_cli_accepts_every_supported_provider_api_family(self) -> None:
        adapters = (
            "openai_compatible_chat",
            "openai_responses",
            "anthropic_messages",
            "gemini_generate_content",
        )
        with isolated_karox_directories():
            for index, adapter in enumerate(adapters):
                provider_id = f"e2e-adapter-{index}"
                saved = self.run_cli(
                    "provider",
                    "add",
                    provider_id,
                    "--adapter",
                    adapter,
                    "--base-url",
                    self.base_url,
                    "--credential-ref",
                    "env:KAROX_PROVIDER_E2E_API_KEY",
                    "--json",
                )
                self.assertIn(adapter, json.dumps(saved))
            listed = self.run_cli("provider", "list", "--json")
            rendered = json.dumps(listed)
            for adapter in adapters:
                with self.subTest(adapter=adapter):
                    self.assertIn(adapter, rendered)

    def test_preset_catalog_exposes_supported_families_and_blocks_unimplemented_protocols(self) -> None:
        with isolated_karox_directories():
            presets = self.run_cli("provider", "presets", "--json")
            catalog = {item["preset_id"]: item for item in presets}
            expected = {
                "openai": "openai_responses",
                "anthropic": "anthropic_messages",
                "gemini": "gemini_generate_content",
                "openai-compatible": "openai_compatible_chat",
                "anthropic-compatible": "anthropic_messages",
                "local-openai": "openai_compatible_chat",
            }
            for preset_id, adapter in expected.items():
                with self.subTest(preset_id=preset_id):
                    self.assertTrue(catalog[preset_id]["installable"])
                    self.assertEqual(catalog[preset_id]["adapter_kind"], adapter)
            self.assertFalse(catalog["puter"]["installable"])
            self.assertIsNone(catalog["puter"]["adapter_kind"])

            for preset_id in ("openai", "anthropic", "gemini"):
                saved = self.run_cli(
                    "provider",
                    "add-preset",
                    preset_id,
                    "--provider-id",
                    f"preset-{preset_id}",
                    "--credential-ref",
                    "env:KAROX_PROVIDER_E2E_API_KEY",
                    "--json",
                )
                self.assertEqual(saved["provider_id"], f"preset-{preset_id}")

    def test_installed_karox_console_script_launches_real_cli(self) -> None:
        launcher = shutil.which("karox")
        self.assertIsNotNone(launcher, "the karox console script is not installed on PATH")
        with isolated_karox_directories():
            result = subprocess.run(
                [str(launcher), "paths", "--json"],
                env=dict(os.environ),
                capture_output=True,
                text=True,
                timeout=20,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
        payload = json.loads(result.stdout)
        self.assertIn("config_dir", payload)
        self.assertIn("runtime_dir", payload)

    def test_installed_cli_exposes_safe_copy_and_connect_commands(self) -> None:
        launcher = shutil.which("karox")
        self.assertIsNotNone(launcher, "the karox console script is not installed on PATH")
        cases = (
            (("connections", "copy-auth", "--help"), "copy"),
            (("bridge", "credential", "copy", "--help"), "Bearer"),
            (("connect", "--help"), "chatgpt"),
        )
        for arguments, expected in cases:
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [str(launcher), *arguments],
                    capture_output=True,
                    text=True,
                    timeout=20,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr or result.stdout)
                self.assertIn(expected.casefold(), result.stdout.casefold())

    def test_no_argument_cli_dispatches_to_tui(self) -> None:
        with isolated_karox_directories():
            with mock.patch("karox.tui.run_tui", return_value=0) as run_tui:
                self.assertEqual(cli.main([]), 0)
            run_tui.assert_called_once_with(repository=str(Path.cwd().resolve()))


class _MemoryCredentialBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.values[(service, account)] = secret

    def get(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def delete(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


class TuiProviderSetupE2ETests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        Handler.requests = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        self.base_url = f"http://{host}:{port}/v1"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    async def test_real_tui_form_probes_then_saves_provider_and_selected_model(self) -> None:
        with isolated_karox_directories() as repository:
            backend = _MemoryCredentialBackend()
            controller = ProviderController(
                registry=tui._registry(),
                credentials=CredentialStore(backend),
            )
            with (
                mock.patch.object(tui, "_load_language", return_value="en"),
                mock.patch.object(tui, "_selected_model", return_value=None),
                mock.patch.object(tui, "_provider_controller", return_value=controller),
            ):
                app = tui.KaroXApp(repository, language="en")
                async with app.run_test(size=(120, 42)) as pilot:
                    await pilot.pause()
                    screen = tui.ProviderSetupScreen("en", None)
                    app.push_screen(screen)
                    await pilot.pause()
                    screen.query_one("#adapter-compatible", tui.RadioButton).value = True
                    screen.query_one("#provider-id", tui.Input).value = "e2e-tui"
                    screen.query_one("#provider-url", tui.Input).value = self.base_url
                    screen.query_one("#provider-key", tui.Input).value = SECRET
                    screen.query_one("#provider-model", tui.Input).value = MODEL
                    screen.action_save()
                    for _ in range(30):
                        await pilot.pause(0.2)
                        if screen not in app.screen_stack:
                            break
                    status = ""
                    if screen in app.screen_stack:
                        status = str(
                            screen.query_one("#provider-error", tui.Static).render()
                        )
                    self.assertNotIn(screen, app.screen_stack, status)

            details = controller.details("e2e-tui")
            self.assertEqual(details.provider.adapter_kind, "openai_compatible_chat")
            self.assertEqual(details.models[0].model_id, MODEL)
            self.assertIsNotNone(details.selected_model)
            self.assertEqual(details.selected_model.model_id, MODEL)
            self.assertTrue(details.credential["available"])
            self.assertTrue(any(value == SECRET for value in backend.values.values()))
            registry_text = (Path(os.environ["KAROX_VNEXT_CONFIG_DIR"]) / "vnext" / "providers.json").read_text(encoding="utf-8")
            self.assertNotIn(SECRET, registry_text)
            self.assertTrue(any(item["method"] == "POST" for item in Handler.requests))


if __name__ == "__main__":
    unittest.main()
