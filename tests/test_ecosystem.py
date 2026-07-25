from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import httpx

from _support import SRC  # noqa: F401
from karox.cli import main
from karox.credentials import CredentialStore
from karox.ecosystem import (
    INTEGRATION_PRESETS,
    TARGET_PRESETS,
    EcosystemRecord,
    EcosystemRegistry,
)


class _MemoryCredentials:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.values[(service, account)] = secret

    def get(self, service: str, account: str) -> Optional[str]:
        return self.values.get((service, account))

    def delete(self, service: str, account: str) -> None:
        del self.values[(service, account)]


class _FakeHttpPostClient:
    def __init__(self, response: httpx.Response) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    def __enter__(self) -> "_FakeHttpPostClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, url: str, **kwargs: object) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        return self._response



class EcosystemTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ, {"KAROX_VNEXT_CONFIG_DIR": str(self.root)}
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        self.temporary.cleanup()

    def invoke(self, *arguments: str) -> tuple[int, str, str]:
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_registries_are_separate_atomic_and_disabled_by_default(self) -> None:
        targets = EcosystemRegistry(self.root / "targets.json", TARGET_PRESETS)
        integrations = EcosystemRegistry(
            self.root / "integrations.json", INTEGRATION_PRESETS
        )
        target = targets.add("promptql")
        integration = integrations.add("sentry")
        self.assertFalse(target.enabled)
        self.assertFalse(integration.enabled)
        self.assertNotEqual(targets.path, integrations.path)
        self.assertEqual(targets.get("promptql").preset_id, "promptql")

    def test_wandb_is_an_observability_integration(self) -> None:
        preset = INTEGRATION_PRESETS["wandb"]
        self.assertEqual(preset.display_name, "Weights & Biases (W&B)")
        self.assertEqual(preset.data_boundary, "telemetry")

    def test_settings_reject_secret_material(self) -> None:
        with self.assertRaisesRegex(ValueError, "secrets"):
            EcosystemRecord("unsafe", "sentry", settings={"api_key": "secret-value"})

    def test_cli_target_tool_and_integration_lifecycle(self) -> None:
        code, output, error = self.invoke("target", "add", "promptql", "--json")
        self.assertEqual(code, 0, error)
        self.assertFalse(json.loads(output)["enabled"])
        code, output, error = self.invoke("target", "handoff", "promptql", "--json")
        self.assertEqual(code, 0, error)
        self.assertIn("OPENAPI", " ".join(json.loads(output)["transports"]).upper())

        code, _, error = self.invoke("tool", "add", "tavily")
        self.assertEqual(code, 0, error)
        code, output, error = self.invoke("tool", "enable", "tavily", "--json")
        self.assertEqual(code, 0, error)
        self.assertTrue(json.loads(output)["enabled"])

        code, _, error = self.invoke("integration", "add", "sentry")
        self.assertEqual(code, 0, error)
        code, output, error = self.invoke(
            "integration",
            "configure",
            "sentry",
            "--telemetry-field",
            "error_type",
            "--json",
        )
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)["telemetry_fields"], ["error_type"])

    def test_documentation_required_preset_cannot_be_enabled(self) -> None:
        self.assertEqual(self.invoke("tool", "add", "browser-use")[0], 0)
        code, _, error = self.invoke("tool", "enable", "browser-use")
        self.assertEqual(code, 2)
        self.assertIn("official contract is required", error)

    def test_target_ask_invokes_promptql_natural_language_api(self) -> None:
        api_key = "pql-cli-key-1234567890"
        backend = _MemoryCredentials()
        store = CredentialStore(backend)
        response = httpx.Response(
            200,
            request=httpx.Request("POST", "https://127.0.0.1:0/query"),
            content=json.dumps(
                {
                    "assistant_actions": [{"message": "summary", "plan": "step"}],
                    "modified_artifacts": [],
                }
            ).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        http_client = _FakeHttpPostClient(response)
        with (
            patch("karox.cli.CredentialStore", return_value=store),
            patch("karox.promptql_outbound.CredentialStore", return_value=store),
            patch("karox.promptql_outbound.httpx.Client", return_value=http_client),
            patch("sys.stdin", io.StringIO(api_key + "\n")),
        ):
            code, output, error = self.invoke(
                "credential", "set", "promptql", "--stdin", "--json"
            )
            self.assertEqual(code, 0, error)
            self.assertEqual(
                json.loads(output)["reference"], "os-keyring:provider/promptql"
            )

            code, _, error = self.invoke("target", "add", "promptql", "--json")
            self.assertEqual(code, 0, error)

            code, _, error = self.invoke(
                "target",
                "configure",
                "promptql",
                "--setting",
                "build_version=2026.07",
                "--setting",
                "api_base_url=http://127.0.0.1:0",
                "--credential-ref",
                "os-keyring:provider/promptql",
                "--json",
            )
            self.assertEqual(code, 0, error)

            code, output, error = self.invoke(
                "target",
                "ask",
                "promptql",
                "--message",
                "summarize sales by region",
                "--json",
            )
        self.assertEqual(code, 0, error)
        payload = json.loads(output)
        self.assertEqual(payload["item_id"], "promptql")
        self.assertEqual(
            payload["assistant_actions"],
            [{"message": "summary", "plan": "step"}],
        )
        self.assertEqual(len(http_client.calls), 1)
        call = http_client.calls[0]
        self.assertEqual(call["url"], "http://127.0.0.1:0/query")
        self.assertEqual(call["headers"]["Authorization"], f"Bearer {api_key}")
        encoded = output + error
        self.assertNotIn(api_key, encoded)


if __name__ == "__main__":
    unittest.main()
