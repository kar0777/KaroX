from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from karox.agent import AgentLimits
from karox.cli import _agent_provider, _route, main
from karox.credentials import CredentialStore
from karox.providers import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ProviderError,
    ProviderErrorKind,
)
from karox.registry import ModelRecord, ProviderRecord, ProviderRegistry


class MemoryCredentials:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.values[(service, account)] = secret

    def get(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def delete(self, service: str, account: str) -> None:
        del self.values[(service, account)]


class FakeProvider:
    provider_name = "fake"

    def __init__(self, outcome: ModelResponse | ProviderError) -> None:
        self.outcome = outcome

    def complete(self, request: ModelRequest) -> ModelResponse:
        if isinstance(self.outcome, ProviderError):
            raise self.outcome
        return self.outcome


class Factory:
    providers: dict[str, FakeProvider] = {}
    created: list[str] = []

    def create(self, record: ProviderRecord) -> FakeProvider:
        self.created.append(record.provider_id)
        return self.providers[record.provider_id]


class ProviderCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = patch.dict(
            os.environ,
            {
                "KAROX_VNEXT_CONFIG_DIR": str(self.root / "config"),
                "KAROX_VNEXT_RUNTIME_DIR": str(self.root / "runtime"),
            },
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

    def test_credential_value_never_reaches_output_or_registry(self) -> None:
        secret = "sk-test-secret-that-must-never-leak"
        store = CredentialStore(MemoryCredentials())
        with patch("karox.cli.CredentialStore", return_value=store), patch(
            "sys.stdin", io.StringIO(secret + "\n")
        ):
            code, stdout, stderr = self.invoke(
                "credential", "set", "openai", "--stdin", "--json"
            )
        self.assertEqual(code, 0, stderr)
        self.assertNotIn(secret, stdout + stderr)
        payload = json.loads(stdout)
        self.assertEqual(payload["reference"], "os-keyring:provider/openai")

        code, stdout, stderr = self.invoke(
            "provider",
            "add",
            "openai",
            "--adapter",
            "openai_responses",
            "--base-url",
            "https://api.openai.com/v1",
            "--credential-ref",
            payload["reference"],
            "--json",
        )
        self.assertEqual(code, 0, stderr)
        registry_text = (self.root / "config" / "vnext" / "providers.json").read_text(
            encoding="utf-8"
        )
        self.assertNotIn(secret, stdout + stderr + registry_text)

    def test_provider_model_crud_selection_and_cascade(self) -> None:
        code, _, stderr = self.invoke(
            "provider",
            "add",
            "local",
            "--adapter",
            "openai_compatible_chat",
            "--base-url",
            "http://127.0.0.1:11434/v1",
            "--privacy-class",
            "local",
        )
        self.assertEqual(code, 0, stderr)
        code, _, stderr = self.invoke(
            "model",
            "add",
            "local",
            "org/model/v1",
            "--alias",
            "default",
            "--tools",
            "true",
            "--streaming",
            "true",
        )
        self.assertEqual(code, 0, stderr)
        code, stdout, stderr = self.invoke(
            "model", "select", "local", "default", "--json"
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["model_id"], "org/model/v1")

        code, _, stderr = self.invoke("provider", "remove", "local")
        self.assertEqual(code, 2)
        self.assertIn("use cascade", stderr)
        code, _, stderr = self.invoke(
            "provider", "remove", "local", "--cascade"
        )
        self.assertEqual(code, 0, stderr)
        registry = ProviderRegistry(self.root / "config" / "vnext" / "providers.json")
        self.assertIsNone(registry.selected_model())

    def test_route_splits_only_at_first_slash(self) -> None:
        target = _route("provider/org/model/v1")
        self.assertEqual(target.provider_id, "provider")
        self.assertEqual(target.model, "org/model/v1")

    def test_direct_mode_rejects_every_routed_option(self) -> None:
        cases = (
            ("--route", "other/model"),
            ("--privacy-limit", "private"),
            ("--max-total-tokens", "10"),
            ("--max-cost", "1.0"),
            ("--currency", "USD"),
        )
        for index, option in enumerate(cases):
            with self.subTest(option=option[0]):
                session_id = f"direct-rejected-{index}"
                code, _, stderr = self.invoke(
                    "agent",
                    "run",
                    "--repository",
                    str(self.root),
                    "--task",
                    "work",
                    "--model",
                    "model",
                    "--base-url",
                    "https://provider.example/v1",
                    "--session-id",
                    session_id,
                    *option,
                )

                self.assertEqual(code, 2)
                self.assertIn(option[0], stderr)
                self.assertFalse(
                    (
                        self.root
                        / "runtime"
                        / "vnext"
                        / "sessions"
                        / session_id
                    ).exists()
                )

    def test_invalid_routes_and_limits_do_not_create_session_state(self) -> None:
        registry = ProviderRegistry(
            self.root / "config" / "vnext" / "providers.json"
        )
        registry.put_provider(
            ProviderRecord(
                provider_id="local",
                adapter_kind="openai_compatible_chat",
                base_url="http://127.0.0.1:11434/v1",
                privacy_class="local",
            )
        )
        registry.put_model(
            ModelRecord(
                provider_id="local",
                model_id="model",
                aliases=("default",),
                tools="true",
                streaming="true",
            )
        )
        cases = (
            ("missing-route", ("--route", "missing/model")),
            (
                "zero-token-budget",
                ("--route", "local/default", "--max-total-tokens", "0"),
            ),
            (
                "zero-cost-budget",
                (
                    "--route",
                    "local/default",
                    "--max-cost",
                    "0",
                    "--currency",
                    "USD",
                ),
            ),
            (
                "cost-without-currency",
                ("--route", "local/default", "--max-cost", "1"),
            ),
            (
                "invalid-currency",
                ("--route", "local/default", "--currency", "usd"),
            ),
        )
        for session_id, options in cases:
            with self.subTest(session_id=session_id):
                code, _, _ = self.invoke(
                    "agent",
                    "run",
                    "--repository",
                    str(self.root),
                    "--task",
                    "work",
                    "--session-id",
                    session_id,
                    *options,
                )

                self.assertEqual(code, 2)
                self.assertFalse(
                    (
                        self.root
                        / "runtime"
                        / "vnext"
                        / "sessions"
                        / session_id
                    ).exists()
                )

    def test_agent_routing_falls_back_and_resumed_budget_preflights(self) -> None:
        registry = ProviderRegistry(self.root / "providers.json")
        for provider_id in ("first", "second"):
            registry.put_provider(
                ProviderRecord(
                    provider_id=provider_id,
                    adapter_kind="openai_compatible_chat",
                    base_url=f"https://{provider_id}.example/v1",
                )
            )
            registry.put_model(
                ModelRecord(
                    provider_id=provider_id,
                    model_id=f"{provider_id}/model",
                    tools="true",
                    streaming="true",
                )
            )
        Factory.created = []
        Factory.providers = {
            "first": FakeProvider(
                ProviderError(ProviderErrorKind.RATE_LIMIT, "try another")
            ),
            "second": FakeProvider(
                ModelResponse("ok", (), "stop", {"total_tokens": 2})
            ),
        }
        args = SimpleNamespace(
            model=None,
            base_url=None,
            api_key_env=None,
            route=["first/first/model", "second/second/model"],
            privacy_limit="public",
            max_total_tokens=10,
            max_cost=None,
            currency=None,
        )
        with patch("karox.cli._registry", return_value=registry), patch(
            "karox.cli.ProviderFactory", Factory
        ):
            routed, _ = _agent_provider(args, SimpleNamespace(usage={}), AgentLimits())
            result = routed.complete(
                ModelRequest("ignored", (ModelMessage("user", "work"),))
            )
            self.assertEqual(result.selected_provider, "second")
            self.assertEqual(Factory.created, ["first", "second"])

            Factory.created = []
            args.route = ["first/first/model"]
            args.max_total_tokens = 10
            exhausted, _ = _agent_provider(
                args,
                SimpleNamespace(usage={"total_tokens": 10}),
                AgentLimits(),
            )
            with self.assertRaisesRegex(ProviderError, "already exhausted"):
                exhausted.complete(
                    ModelRequest("ignored", (ModelMessage("user", "work"),))
                )
            self.assertEqual(Factory.created, [])


if __name__ == "__main__":
    unittest.main()
