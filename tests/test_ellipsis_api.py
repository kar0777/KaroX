from __future__ import annotations

import json
import unittest

import httpx

import _path_setup
from karox.ellipsis_api import (
    EllipsisApiContract,
    EllipsisClient,
    EllipsisContractError,
    EllipsisError,
    assert_repository_free,
    repository_free_create_payload,
)
from karox.ellipsis_runtime import ELLIPSIS_SYSTEM_INSTRUCTION


class EllipsisApiTests(unittest.TestCase):
    def test_create_payload_is_repository_free(self) -> None:
        payload = repository_free_create_payload(
            model="claude-opus-5",
            system_instruction=ELLIPSIS_SYSTEM_INSTRUCTION,
            budget=0.10,
            currency="USD",
            repositories_mode="empty",
            client_install={"strategy": "preinstalled", "command": "karox-remote"},
        )
        self.assertEqual(payload["sandbox"]["repositories"], [])
        serialized = json.dumps(payload, sort_keys=True)
        self.assertNotIn('"repository":', serialized)
        self.assertNotIn("KAROX_REMOTE_URL", serialized)
        self.assertNotIn("KAROX_REMOTE_CREDENTIAL", serialized)
        self.assertNotIn("KAROX_SESSION_ID", serialized)
        self.assertNotIn("git@", serialized)
        self.assertNotIn("github.com", serialized)

    def test_repository_guard_rejects_every_nonempty_repository_field(self) -> None:
        bad_payloads = (
            {"repository": "owner/repo"},
            {"sandbox": {"repositories": ["owner/repo"]}},
            {"metadata": {"repositories": []}},
            {"nested": [{"repository": None}]},
        )
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(EllipsisContractError):
                    assert_repository_free(payload)

    def test_empty_repository_list_falls_back_to_omitted_on_schema_error(self) -> None:
        requests: list[dict[str, object]] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/v1/capabilities":
                return httpx.Response(
                    200,
                    json={
                        "session_modes": ["interactive"],
                        "sandbox_variables": "write_only",
                    },
                )
            if request.url.path == "/v1/interactive-sessions":
                payload = json.loads(request.content.decode("utf-8"))
                requests.append(payload)
                if "repositories" in payload.get("sandbox", {}):
                    return httpx.Response(422, json={"code": "schema_validation"})
                return httpx.Response(201, json={"id": "ell-session", "status": "draft"})
            raise AssertionError(request.url.path)

        client = EllipsisClient(
            "https://ellipsis.invalid",
            "unit-test-token",
            contract=EllipsisApiContract(repositories_mode="empty"),
            transport=httpx.MockTransport(handler),
        )
        try:
            self.assertEqual(client.preflight()["sandbox_variables"], "write_only")
            draft = client.create_draft(
                model="claude-opus-5",
                system_instruction=ELLIPSIS_SYSTEM_INSTRUCTION,
                budget=0.10,
            )
        finally:
            client.close()
        self.assertEqual(draft.session_id, "ell-session")
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0]["sandbox"]["repositories"], [])
        self.assertNotIn("repositories", requests[1]["sandbox"])
        for payload in requests:
            assert_repository_free(payload)

    def test_authentication_failure_is_not_retried(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(401, json={"code": "unauthorized"})

        client = EllipsisClient(
            "https://ellipsis.invalid",
            "unit-test-token",
            transport=httpx.MockTransport(handler),
        )
        try:
            with self.assertRaises(EllipsisError) as raised:
                client.create_draft(
                    model="claude-opus-5",
                    system_instruction=ELLIPSIS_SYSTEM_INSTRUCTION,
                    budget=0.10,
                )
        finally:
            client.close()
        self.assertEqual(raised.exception.status_code, 401)
        self.assertEqual(calls, 1)

    def test_connection_values_are_only_sent_as_write_only_variables(self) -> None:
        captured: dict[str, object] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["path"] = request.url.path
            captured["payload"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(204)

        client = EllipsisClient(
            "https://ellipsis.invalid",
            "unit-test-token",
            transport=httpx.MockTransport(handler),
        )
        try:
            client.set_write_only_variables(
                "ell-session",
                {
                    "KAROX_REMOTE_URL": "https://bridge.invalid",
                    "KAROX_REMOTE_CREDENTIAL": "opaque-value",
                    "KAROX_SESSION_ID": "local-session",
                },
            )
        finally:
            client.close()
        self.assertEqual(
            captured["path"],
            "/v1/interactive-sessions/ell-session/sandbox-variables",
        )
        variables = captured["payload"]["variables"]
        self.assertEqual({item["visibility"] for item in variables}, {"write_only"})
        self.assertEqual(
            {item["name"] for item in variables},
            {
                "KAROX_REMOTE_URL",
                "KAROX_REMOTE_CREDENTIAL",
                "KAROX_SESSION_ID",
            },
        )


if __name__ == "__main__":
    unittest.main()
