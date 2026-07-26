"""OAuth/DCR/PKCE contract coverage for ChatGPT and Claude web MCP bridges."""

from __future__ import annotations

import base64
import hashlib
import re
import threading
import time
import unittest
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn

from _support import SRC  # noqa: F401

from karox.oauth_bridge import OAuthBridgeError, OAuthBridgeService, build_oauth_proxy_asgi_app
from karox.proxy import ProxyToolDescriptor


class _Runtime:
    def descriptors(self) -> list[ProxyToolDescriptor]:
        return [
            ProxyToolDescriptor(
                name="echo",
                description="Echo text",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
                read_only=True,
            )
        ]

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: str | None = None,
        deadline_seconds: float | None = None,
    ) -> dict[str, Any]:
        return {"tool": tool_name, "text": arguments["text"]}


class _WireServer:
    def __init__(self, app: object) -> None:
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        deadline = time.time() + 15
        while (
            not self.server.started or not self.server.servers
        ) and time.time() < deadline:
            time.sleep(0.02)
        if not self.server.started or not self.server.servers:
            raise RuntimeError("OAuth bridge wire server did not start")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("OAuth bridge wire server did not stop")


def _pkce(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


class OAuthBridgeServiceTests(unittest.TestCase):
    def test_public_url_and_redirect_validation_fail_closed(self) -> None:
        for value in (
            "",
            "http://public.example",
            "https://user@example.com",
            "https://example.com?query=1",
            "https://example.com/prefix",
        ):
            with self.subTest(value=value), self.assertRaises(OAuthBridgeError):
                OAuthBridgeService(value, "secret")

        service = OAuthBridgeService("https://karox.example", "secret")
        with self.assertRaisesRegex(OAuthBridgeError, "redirect URI"):
            service.register({"redirect_uris": ["http://remote.example/callback"]})
        registered = service.register(
            {
                "redirect_uris": ["http://127.0.0.1:9000/callback"],
                "client_uri": "https://client.example/about",
            }
        )
        self.assertTrue(registered["client_id"])

    def test_metadata_describes_one_bound_mcp_resource(self) -> None:
        service = OAuthBridgeService("https://karox.example/", "secret")
        protected = service.protected_resource_metadata()
        authorization = service.authorization_server_metadata()
        self.assertEqual(protected["resource"], "https://karox.example/mcp")
        self.assertEqual(
            protected["authorization_servers"], ["https://karox.example"]
        )
        self.assertEqual(
            authorization["registration_endpoint"],
            "https://karox.example/oauth/register",
        )
        self.assertIn("S256", authorization["code_challenge_methods_supported"])
        self.assertIn("refresh_token", authorization["grant_types_supported"])


class OAuthBridgeWireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = build_oauth_proxy_asgi_app(
            _Runtime(),
            "approval-password",
            public_url="https://karox.example",
        )
        self.server = _WireServer(self.app)
        self.base = f"http://127.0.0.1:{self.server.port}"

    def tearDown(self) -> None:
        self.server.close()

    def _authorize(self, client: httpx.Client) -> tuple[str, str, str]:
        registration = client.post(
            "/oauth/register",
            json={
                "client_name": "Claude",
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            },
        )
        self.assertEqual(registration.status_code, 201, registration.text)
        client_id = registration.json()["client_id"]
        verifier = "v" * 64
        state = "state-123"
        authorization = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "state": state,
                "code_challenge": _pkce(verifier),
                "code_challenge_method": "S256",
                "resource": "https://karox.example/mcp",
                "scope": "mcp:tools offline_access",
            },
        )
        self.assertEqual(authorization.status_code, 200, authorization.text)
        request_id = re.search(
            r'name="request_id" value="([^"]+)"', authorization.text
        )
        self.assertIsNotNone(request_id)

        denied = client.post(
            "/oauth/authorize",
            data={"request_id": request_id.group(1), "password": "wrong"},
        )
        self.assertEqual(denied.status_code, 400)
        approved = client.post(
            "/oauth/authorize",
            data={
                "request_id": request_id.group(1),
                "password": "approval-password",
            },
            follow_redirects=False,
        )
        self.assertEqual(approved.status_code, 303, approved.text)
        redirect = urlsplit(approved.headers["location"])
        values = parse_qs(redirect.query)
        self.assertEqual(values["state"], [state])
        return client_id, verifier, values["code"][0]

    def test_discovery_code_exchange_refresh_rotation_and_mcp_auth(self) -> None:
        with httpx.Client(base_url=self.base, timeout=15.0) as client:
            unauthorized = client.post("/mcp", content=b"{}")
            self.assertEqual(unauthorized.status_code, 401)
            self.assertIn(
                "oauth-protected-resource/mcp",
                unauthorized.headers["www-authenticate"],
            )
            protected = client.get("/.well-known/oauth-protected-resource/mcp")
            self.assertEqual(protected.status_code, 200)
            self.assertEqual(
                protected.json()["resource"], "https://karox.example/mcp"
            )
            client_id, verifier, code = self._authorize(client)
            token = client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                    "code_verifier": verifier,
                    "resource": "https://karox.example/mcp",
                },
            )
            self.assertEqual(token.status_code, 200, token.text)
            first = token.json()
            self.assertEqual(first["token_type"], "Bearer")

            accepted = client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {first['access_token']}"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "wire-test", "version": "1"},
                    },
                },
            )
            self.assertNotEqual(accepted.status_code, 401, accepted.text)

            refreshed = client.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": first["refresh_token"],
                    "client_id": client_id,
                    "resource": "https://karox.example/mcp",
                },
            )
            self.assertEqual(refreshed.status_code, 200, refreshed.text)
            second = refreshed.json()
            self.assertNotEqual(second["refresh_token"], first["refresh_token"])

            replay = client.post(
                "/oauth/token",
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": first["refresh_token"],
                    "client_id": client_id,
                    "resource": "https://karox.example/mcp",
                },
            )
            self.assertEqual(replay.status_code, 400)
            revoked = client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {second['access_token']}"},
                content=b"{}",
            )
            self.assertEqual(revoked.status_code, 401)

    def test_code_is_bound_to_pkce_client_redirect_and_resource(self) -> None:
        with httpx.Client(base_url=self.base, timeout=15.0) as client:
            client_id, _verifier, code = self._authorize(client)
            rejected = client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                    "code_verifier": "x" * 64,
                    "resource": "https://karox.example/mcp",
                },
            )
            self.assertEqual(rejected.status_code, 400)
            # A failed binding check does not consume the legitimate code.
            accepted = client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                    "code_verifier": "v" * 64,
                    "resource": "https://karox.example/mcp",
                },
            )
            self.assertEqual(accepted.status_code, 200, accepted.text)

    def test_registration_rejects_duplicate_json_fields(self) -> None:
        with httpx.Client(base_url=self.base, timeout=15.0) as client:
            response = client.post(
                "/oauth/register",
                content=(
                    b'{"redirect_uris":["https://client.example/callback"],'
                    b'"redirect_uris":["https://attacker.example/callback"],'
                    b'"token_endpoint_auth_method":"none"}'
                ),
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertIn("duplicate", response.text)


if __name__ == "__main__":
    unittest.main()
