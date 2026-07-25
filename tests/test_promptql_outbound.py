"""Contract tests for the outbound PromptQL Natural Language API client.

These tests assert the exact wire contract verified against the official
``hasura/promptql-python-sdk`` source: ``POST {base}/query``,
``Authorization: Bearer {api_key}``, v2 body with ``ddn.build_version`` or
``ddn.build_id`` and ``stream:false``.  No live PromptQL endpoint is contacted;
``httpx.Client`` is replaced with a fake that records the request and replays a
prepared JSON response.
"""

from __future__ import annotations

import json
import unittest
from typing import Any
from unittest.mock import patch

import httpx

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.promptql_outbound import (
    DEFAULT_API_BASE_URL,
    PromptQLAccessDenied,
    PromptQLInvocationError,
    PromptQLNaturalLanguageClient,
    PromptQLTargetConfig,
)


_API_KEY = "pql-test-key-1234567890"
_LOOPBACK_BASE = "http://127.0.0.1:0"


def _response(status: int, body: Any) -> httpx.Response:
    request = httpx.Request("POST", "https://api.promptql.pro.hasura.io/query")
    return httpx.Response(
        status,
        request=request,
        content=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )


class _FakeClient:
    """Mirrors the httpx.Client surface used by the outbound client."""

    def __init__(self, items: list[httpx.Response]) -> None:
        self.items = list(items)
        self.calls: list[dict[str, Any]] = []

    def __enter__(self) -> "_FakeClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"url": url, **kwargs})
        return self.items.pop(0)


class _FakeCredentialStore:
    """Stand-in for CredentialStore exposing accessor()."""

    def __init__(self, secret: str = _API_KEY) -> None:
        self._secret = secret

    def accessor(self, reference: str):  # type: ignore[no-untyped-def]
        def access() -> str:
            return self._secret

        return access


def _config(**overrides: Any) -> PromptQLTargetConfig:
    base = overrides.pop("api_base_url", DEFAULT_API_BASE_URL)
    return PromptQLTargetConfig(
        credential_ref="os-keyring:provider/promptql",
        api_base_url=base,
        build_version=overrides.pop("build_version", "2026.07"),
        **overrides,
    )


class PromptQLTargetConfigTests(unittest.TestCase):
    def test_requires_build_version_or_build_id(self) -> None:
        with self.assertRaisesRegex(ValueError, "build_version or build_id"):
            PromptQLTargetConfig(credential_ref="os-keyring:provider/promptql")

    def test_v1_ddn_url_only_is_rejected_with_honest_message(self) -> None:
        with self.assertRaisesRegex(ValueError, "ddn_url mode is not yet supported"):
            PromptQLTargetConfig(
                credential_ref="os-keyring:provider/promptql",
                build_version=None,
                ddn_url="https://ddn.example",
            )

    def test_build_version_and_build_id_are_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(ValueError, "not both"):
            PromptQLTargetConfig(
                credential_ref="os-keyring:provider/promptql",
                build_version="v1",
                build_id="b1",
            )

    def test_missing_credential_ref_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "no credential"):
            PromptQLTargetConfig(
                credential_ref="",
                build_version="2026.07",
            )

    def test_base_url_strips_trailing_slash(self) -> None:
        config = PromptQLTargetConfig(
            credential_ref="os-keyring:provider/promptql",
            api_base_url="https://api.promptql.pro.hasura.io/",
            build_version="2026.07",
        )
        self.assertEqual(config.api_base_url, DEFAULT_API_BASE_URL)


class PromptQLClientContractTests(unittest.TestCase):
    def _client(
        self, config: PromptQLTargetConfig, responses: list[httpx.Response]
    ) -> tuple[PromptQLNaturalLanguageClient, _FakeClient]:
        fake = _FakeClient(responses)
        client = PromptQLNaturalLanguageClient.from_config(
            config, credential_store=_FakeCredentialStore()  # type: ignore[arg-type]
        )
        return client, fake

    def _patch(self, fake: _FakeClient):  # type: ignore[no-untyped-def]
        return patch("karox.promptql_outbound.httpx.Client", return_value=fake)

    def test_v2_build_version_posts_query_endpoint_with_bearer_auth(self) -> None:
        config = _config()
        body = {"assistant_actions": [{"message": "hello"}], "modified_artifacts": []}
        client, fake = self._client(config, [_response(200, body)])
        with self._patch(fake):
            result = client.ask("What is the average temperature?")
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertEqual(call["url"], f"{DEFAULT_API_BASE_URL}/query")
        self.assertEqual(
            call["headers"]["Authorization"], f"Bearer {_API_KEY}"
        )
        self.assertEqual(call["headers"]["Content-Type"], "application/json")
        payload = call["json"]
        self.assertEqual(payload["ddn"], {"build_version": "2026.07"})
        self.assertFalse(payload["stream"])
        self.assertEqual(
            payload["interactions"][-1], {"user_message": "What is the average temperature?"}
        )
        self.assertEqual(result.assistant_actions, [{"message": "hello"}])
        self.assertEqual(result.modified_artifacts, [])

    def test_v2_build_id_alternative_body(self) -> None:
        config = _config(build_version=None, build_id="build-abc")
        body = {"assistant_actions": [{"message": "ok"}], "modified_artifacts": []}
        client, fake = self._client(config, [_response(200, body)])
        with self._patch(fake):
            client.ask("run")
        self.assertEqual(fake.calls[0]["json"]["ddn"], {"build_id": "build-abc"})

    def test_prior_interactions_append_to_body(self) -> None:
        config = _config()
        body = {"assistant_actions": [], "modified_artifacts": []}
        client, fake = self._client(config, [_response(200, body)])
        with self._patch(fake):
            client.ask(
                "follow-up",
                prior_interactions=[{"user_message": "first"}],
            )
        interactions = fake.calls[0]["json"]["interactions"]
        self.assertEqual(interactions, [{"user_message": "first"}, {"user_message": "follow-up"}])

    def test_http_401_raises_access_denied_without_leaking_key(self) -> None:
        config = _config()
        client, fake = self._client(config, [_response(401, {"error": "unauthorized"})])
        with self._patch(fake), self.assertRaises(PromptQLAccessDenied):
            client.ask("x")

    def test_http_500_raises_invocation_error_redacted(self) -> None:
        config = _config()
        client, fake = self._client(
            config, [_response(500, {"error": f"boom key={_API_KEY}"})]
        )
        with self._patch(fake), self.assertRaises(PromptQLInvocationError) as ctx:
            client.ask("x")
        message = str(ctx.exception)
        self.assertIn("HTTP 500", message)
        self.assertNotIn(_API_KEY, message)

    def test_response_redacts_embedded_api_key(self) -> None:
        config = _config()
        body = {
            "assistant_actions": [
                {"message": f"echo {_API_KEY}", "code_output": "ok"}
            ],
            "modified_artifacts": [],
        }
        client, fake = self._client(config, [_response(200, body)])
        with self._patch(fake):
            result = client.ask("x")
        encoded = json.dumps(result.raw)
        self.assertNotIn(_API_KEY, encoded)

    def test_non_loopback_http_base_rejects_credential_before_http(self) -> None:
        config = PromptQLTargetConfig(
            credential_ref="os-keyring:provider/promptql",
            api_base_url="http://api.promptql.example",
            build_version="2026.07",
        )
        client, fake = self._client(config, [])
        with self._patch(fake), self.assertRaisesRegex(ValueError, "HTTPS or a loopback"):
            client.ask("x")
        self.assertEqual(fake.calls, [])

    def test_loopback_http_is_allowed_for_local_testing(self) -> None:
        config = PromptQLTargetConfig(
            credential_ref="os-keyring:provider/promptql",
            api_base_url=_LOOPBACK_BASE,
            build_version="2026.07",
        )
        body = {"assistant_actions": [{"message": "local"}], "modified_artifacts": []}
        client, fake = self._client(config, [_response(200, body)])
        with self._patch(fake):
            result = client.ask("local ask")
        self.assertEqual(fake.calls[0]["url"], f"{_LOOPBACK_BASE}/query")
        self.assertEqual(result.assistant_actions[0]["message"], "local")

    def test_empty_message_rejected_before_http(self) -> None:
        config = _config()
        client, fake = self._client(config, [])
        with self._patch(fake), self.assertRaisesRegex(ValueError, "non-empty"):
            client.ask("   ")
        self.assertEqual(fake.calls, [])

    def test_unresolved_credential_raises_access_denied(self) -> None:
        class _FailingStore:
            def accessor(self, reference: str):  # type: ignore[no-untyped-def]
                def access() -> str:
                    raise RuntimeError("keyring locked")

                return access

        config = _config()
        client = PromptQLNaturalLanguageClient.from_config(
            config, credential_store=_FailingStore()  # type: ignore[arg-type]
        )
        with self.assertRaises(PromptQLAccessDenied):
            client.ask("x")


if __name__ == "__main__":
    unittest.main()
