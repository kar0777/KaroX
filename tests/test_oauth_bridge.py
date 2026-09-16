"""OAuth/DCR/PKCE contract coverage for ChatGPT and Claude web MCP bridges."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch
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
        self.assertEqual(
            authorization["authorization_endpoint"],
            "https://karox.example/oauth/authorize",
        )
        self.assertEqual(
            authorization["token_endpoint"],
            "https://karox.example/oauth/token",
        )
        self.assertIn("S256", authorization["code_challenge_methods_supported"])
        self.assertIn("refresh_token", authorization["grant_types_supported"])

        redirect_uri = "https://search.clickup-prod.com/connect/mcp"
        client_id = service.register(
            {"client_name": "ClickUp", "redirect_uris": [redirect_uri]}
        )["client_id"]
        values = {
            "response_type": ["code"],
            "client_id": [client_id],
            "redirect_uri": [redirect_uri],
            "state": ["state-clickup"],
            "code_challenge": ["a" * 43],
            "code_challenge_method": ["S256"],
            "resource": [service.resource, service.resource],
        }
        request_id, _client = service.begin_authorization(values)
        self.assertTrue(request_id)

        values["resource"] = [service.resource, "https://other.example/mcp"]
        with self.assertRaisesRegex(OAuthBridgeError, "exactly one MCP server"):
            service.begin_authorization(values)


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

    def test_canonical_root_oauth_endpoints_and_legacy_alias_are_live(self) -> None:
        payload = {
            "client_name": "Notion detector",
            "redirect_uris": ["https://www.notion.so/external-auth/callback"],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        with httpx.Client(base_url=self.base, timeout=15.0) as client:
            canonical = client.post("/register", json=payload)
            legacy = client.post("/oauth/register", json=payload)
            authorize = client.get("/authorize")
            token = client.post("/token", data={})
        self.assertEqual(canonical.status_code, 201, canonical.text)
        self.assertEqual(legacy.status_code, 201, legacy.text)
        self.assertEqual(legacy.json()["client_id"], canonical.json()["client_id"])
        self.assertNotEqual(authorize.status_code, 404)
        self.assertNotEqual(token.status_code, 404)

    def test_discovery_aliases_cover_path_inserted_and_mcp_wrapped_forms(self) -> None:
        # Live connector platforms probe more discovery spellings than one
        # RFC quote suggests (ChatGPT's validation hit both base and /mcp
        # path-inserted variants; a single 404 there reads to them as
        # "does not implement OAuth").
        auth_aliases = (
            "/.well-known/oauth-authorization-server/mcp",
            "/mcp/.well-known/oauth-authorization-server",
            "/.well-known/openid-configuration/mcp",
            "/mcp/.well-known/openid-configuration",
        )
        with httpx.Client(base_url=self.base, timeout=15.0) as client:
            reference = client.get("/.well-known/oauth-authorization-server")
            self.assertEqual(reference.status_code, 200, reference.text)
            for alias in auth_aliases:
                with self.subTest(alias=alias):
                    response = client.get(alias)
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json(), reference.json())
            wrapped_protected = client.get("/mcp/.well-known/oauth-protected-resource")
            suffixed_protected = client.get(
                "/.well-known/oauth-protected-resource/mcp"
            )
        self.assertEqual(wrapped_protected.status_code, 200, wrapped_protected.text)
        self.assertEqual(
            wrapped_protected.json(),
            suffixed_protected.json(),
            suffixed_protected.text,
        )

    def test_registration_accepts_an_authorization_code_only_client(self) -> None:
        payload = {
            "client_name": "ChatGPT connector",
            "redirect_uris": ["https://chatgpt.com/connector/oauth/ab12cd34"],
            "grant_types": ["authorization_code"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        with httpx.Client(base_url=self.base, timeout=15.0) as client:
            response = client.post("/oauth/register", json=payload)
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(
            response.json()["grant_types"],
            ["authorization_code", "refresh_token"],
        )
        self.assertEqual(response.json()["client_name"], "ChatGPT connector")

    def test_registration_still_rejects_codeless_or_unknown_grants(self) -> None:
        redirect = "https://chatgpt.com/connector/oauth/ab12cd34"
        bad_grant_cases = (
            {"grant_types": ["implicit", "authorization_code"]},
            {"grant_types": ["refresh_token"]},
            {"grant_types": []},
            {"grant_types": "authorization_code"},
        )
        for case in bad_grant_cases:
            payload = {"client_name": "Probe", "redirect_uris": [redirect]}
            payload.update(case)
            with httpx.Client(base_url=self.base, timeout=15.0) as client:
                response = client.post("/oauth/register", json=payload)
            with self.subTest(case=case):
                self.assertEqual(response.status_code, 400, response.text)
                self.assertIn("grant", response.json()["error_description"])

    def _pending_request(
        self, client: httpx.Client, *, state: str = "state-123"
    ) -> tuple[str, str]:
        """Register a client and open an approval page, as any caller may."""
        registration = client.post(
            "/oauth/register",
            json={
                "client_name": "Claude",
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            },
        )
        self.assertEqual(registration.status_code, 201, registration.text)
        client_id = registration.json()["client_id"]
        authorization = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                "state": state,
                "code_challenge": _pkce("v" * 64),
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
        return client_id, request_id.group(1)

    def _authorize(self, client: httpx.Client) -> tuple[str, str, str]:
        state = "state-123"
        client_id, request_id = self._pending_request(client, state=state)
        verifier = "v" * 64

        denied = client.post(
            "/oauth/authorize",
            data={"request_id": request_id, "password": "wrong"},
        )
        self.assertEqual(denied.status_code, 400)
        approved = client.post(
            "/oauth/authorize",
            data={
                "request_id": request_id,
                "password": "approval-password",
            },
            follow_redirects=False,
        )
        self.assertEqual(approved.status_code, 200, approved.text)
        self.assertNotIn("form-action", approved.headers["content-security-policy"])
        refresh = approved.headers["refresh"]
        self.assertTrue(refresh.startswith("0; url="), refresh)
        redirect = urlsplit(refresh.split("url=", 1)[1])
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
            direct_bearer = client.post(
                "/mcp",
                headers={"Authorization": "Bearer approval-password"},
                json={
                    "jsonrpc": "2.0",
                    "id": 0,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-03-26",
                        "capabilities": {},
                        "clientInfo": {"name": "direct-bearer-test", "version": "1"},
                    },
                },
            )
            self.assertNotEqual(direct_bearer.status_code, 401, direct_bearer.text)
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
                },
            )
            self.assertEqual(replay.status_code, 400)
            revoked = client.post(
                "/mcp",
                headers={"Authorization": f"Bearer {second['access_token']}"},
                content=b"{}",
            )
            self.assertEqual(revoked.status_code, 401)

    def test_the_approval_page_permits_the_return_to_the_registered_client(self) -> None:
        """`form-action` has to cover the redirect, not just the form's own target.

        The form posts back to /oauth/authorize on this origin, but that handler
        answers 303 to the client's registered redirect_uri -- and Chromium applies
        `form-action` to every hop of a form submission's redirect chain. With
        `'self'` alone, Chrome and Edge refused the return to claude.ai: the OAuth
        tab went blank and never came back, so the connector could never finish
        authorizing.
        """
        with httpx.Client(base_url=self.base, timeout=15.0) as client:
            registration = client.post(
                "/oauth/register",
                json={
                    "client_name": "Claude",
                    "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                },
            )
            self.assertEqual(registration.status_code, 201, registration.text)
            page = client.get(
                "/oauth/authorize",
                params={
                    "response_type": "code",
                    "client_id": registration.json()["client_id"],
                    "redirect_uri": "https://claude.ai/api/mcp/auth_callback",
                    "state": "state-123",
                    "code_challenge": _pkce("v" * 64),
                    "code_challenge_method": "S256",
                    "resource": "https://karox.example/mcp",
                    "scope": "mcp:tools offline_access",
                },
            )

        self.assertEqual(page.status_code, 200, page.text)
        policy = page.headers["content-security-policy"]
        self.assertIn("form-action 'self' https://karox.example;", policy)
        self.assertNotIn("https://claude.ai", policy)
        # Widened for the redirect and nothing else: no scripts, no framing.
        self.assertIn("default-src 'none'", policy)
        self.assertIn("base-uri 'none'", policy)
        self.assertIn("frame-ancestors 'none'", policy)
        self.assertNotIn("script-src", policy)
        self.assertNotIn("*", policy)
        # ``no-referrer`` makes Chromium send ``Origin: null`` for some basic
        # form POSTs. The rebinding guard correctly rejects that opaque origin,
        # which used to make the bridge reject its own approval form with 421.
        self.assertEqual(page.headers["referrer-policy"], "same-origin")

    def test_chatgpt_russian_locale_explains_where_the_password_goes(self) -> None:
        with httpx.Client(base_url=self.base, timeout=15.0) as client:
            registration = client.post(
                "/oauth/register",
                json={
                    "client_name": "ChatGPT",
                    "redirect_uris": ["https://chatgpt.com/connector/oauth/callback"],
                },
            )
            page = client.get(
                "/oauth/authorize",
                params={
                    "response_type": "code",
                    "client_id": registration.json()["client_id"],
                    "redirect_uri": "https://chatgpt.com/connector/oauth/callback",
                    "state": "state-123",
                    "code_challenge": _pkce("v" * 64),
                    "code_challenge_method": "S256",
                    "resource": "https://karox.example/mcp",
                    "scope": "mcp:tools offline_access",
                    "ui_locales": "ru-RU en",
                },
            )

        self.assertEqual(page.status_code, 200, page.text)
        self.assertIn('<html lang="ru">', page.text)
        self.assertIn("Пароль подтверждения из окна KaroX", page.text)
        self.assertIn("Не закрывайте KaroX", page.text)
        self.assertIn(">Разрешить</button>", page.text)

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

    def test_non_ascii_approval_password_is_an_ordinary_invalid_request(self) -> None:
        with httpx.Client(base_url=self.base, timeout=15.0) as client:
            _client_id, request_id = self._pending_request(client)
            response = client.post(
                "/oauth/authorize",
                data={"request_id": request_id, "password": "é"},
                follow_redirects=False,
            )
        # Reachable with no credential at all: registration and the approval page
        # are open, so a typed password used to be an unauthenticated 500.
        self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(response.json()["error"], "invalid_request")

    def test_foreign_host_is_rejected_on_the_oauth_endpoints(self) -> None:
        # Intentional attack traffic: capture the guard's console diagnostics
        # so the release log stays clean, and assert them instead.
        captured = io.StringIO()
        with httpx.Client(base_url=self.base, timeout=15.0) as client:
            with redirect_stderr(captured):
                registration = client.post(
                    "/oauth/register",
                    json={"redirect_uris": ["https://claude.ai/api/mcp/auth_callback"]},
                    headers={"Host": "rebound.attacker.example"},
                )
                token = client.post(
                    "/oauth/token",
                    data={"grant_type": "authorization_code"},
                    headers={"Host": "rebound.attacker.example"},
                )
        self.assertEqual(registration.status_code, 421, registration.text)
        self.assertEqual(token.status_code, 421, token.text)
        diagnostics = captured.getvalue()
        self.assertEqual(diagnostics.count("[karox-rebind] 421"), 2, diagnostics)

    def test_the_declared_public_host_reaches_the_mcp_wire(self) -> None:
        with httpx.Client(base_url=self.base, timeout=15.0) as client:
            response = client.post(
                "/mcp", content=b"{}", headers={"Host": "karox.example"}
            )
        # 421 here would mean the wire underneath was never told the public host
        # this very app validated, so every real request through the tunnel would
        # look like a rebound name.
        self.assertEqual(response.status_code, 401, response.text)

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


class OAuthStatePersistenceTests(unittest.TestCase):
    """A connector must survive the bridge restarting, or it is not a connector.

    All of this state used to live only in RAM. Restarting made the client_id
    ChatGPT or Claude had stored unknown and every refresh token invalid, so the
    only way back was to delete the connector and add it again -- after a restart,
    a crash, or a reboot.
    """

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.state = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def _service(self, public_url: str = "https://karox.example") -> OAuthBridgeService:
        return OAuthBridgeService(public_url, "approval-password", state_dir=self.state)

    def _connect(self, service: OAuthBridgeService) -> tuple[str, dict[str, Any]]:
        """Run one full registration and code exchange, returning the tokens."""
        client_id = service.register(
            {
                "client_name": "Claude",
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
            }
        )["client_id"]
        verifier = "v" * 64
        request_id, _client = service.begin_authorization(
            {
                "response_type": ["code"],
                "client_id": [client_id],
                "redirect_uri": ["https://claude.ai/api/mcp/auth_callback"],
                "state": ["state-123"],
                "code_challenge": [_pkce(verifier)],
                "code_challenge_method": ["S256"],
                "resource": [service.resource],
                "scope": ["mcp:tools offline_access"],
            }
        )
        location = service.approve(request_id, "approval-password")
        code = parse_qs(urlsplit(location).query)["code"][0]
        tokens = service.exchange_code(
            {
                "code": [code],
                "client_id": [client_id],
                "redirect_uri": ["https://claude.ai/api/mcp/auth_callback"],
                "code_verifier": [verifier],
                "resource": [service.resource],
            }
        )
        return client_id, tokens

    def test_oauth_access_token_survives_keyring_failure_after_discovery(self) -> None:
        calls = 0

        def approval_secret() -> str:
            nonlocal calls
            calls += 1
            if calls <= 2:
                return "approval-password"
            raise RuntimeError("credential backend temporarily unavailable")

        service = OAuthBridgeService(
            "https://karox.example",
            approval_secret,
            state_dir=self.state,
        )
        _client_id, tokens = self._connect(service)

        # Model two consecutive authenticated MCP requests: discovery succeeds,
        # then the first real tool invocation arrives while Windows Credential
        # Manager is transiently unavailable. A valid OAuth access token is
        # self-contained in the persisted digest grant and must not depend on the
        # approval password/keyring after the authorization flow completed.
        self.assertTrue(service.authorize_access_token(tokens["access_token"]))
        self.assertTrue(service.authorize_access_token(tokens["access_token"]))

    def test_a_registered_client_and_its_refresh_token_outlive_the_process(
        self,
    ) -> None:
        first = self._service()
        client_id, tokens = self._connect(first)

        # A different instance on the same state: this is what a restart is.
        restarted = self._service()
        self.assertTrue(
            restarted.authorize_access_token(tokens["access_token"]),
            "an unexpired access token must remain valid across a bridge restart",
        )
        refreshed = restarted.refresh(
            {
                "refresh_token": [tokens["refresh_token"]],
                "client_id": [client_id],
                "resource": [restarted.resource],
            }
        )
        self.assertTrue(refreshed["access_token"])
        self.assertTrue(restarted.authorize_access_token(refreshed["access_token"]))
        # The client_id the connector stored still authorizes, so no re-add.
        request_id, _client = restarted.begin_authorization(
            {
                "response_type": ["code"],
                "client_id": [client_id],
                "redirect_uri": ["https://claude.ai/api/mcp/auth_callback"],
                "state": ["state-456"],
                "code_challenge": [_pkce("w" * 64)],
                "code_challenge_method": ["S256"],
                "resource": [restarted.resource],
                "scope": ["mcp:tools"],
            }
        )
        self.assertTrue(request_id)

    def test_pending_approval_outlives_a_bridge_restart(self) -> None:
        first = self._service()
        redirect_uri = "https://app.clickup.com/mcp/oauth/callback"
        client_id = first.register(
            {
                "client_name": "ClickUp",
                "redirect_uris": [redirect_uri],
            }
        )["client_id"]
        verifier = "v" * 64
        request_id, _client = first.begin_authorization(
            {
                "response_type": ["code"],
                "client_id": [client_id],
                "redirect_uri": [redirect_uri],
                "state": ["state-clickup"],
                "code_challenge": [_pkce(verifier)],
                "code_challenge_method": ["S256"],
                "resource": [first.resource],
                "scope": ["mcp:tools offline_access"],
            }
        )

        restarted = self._service()
        location = restarted.approve(request_id, "approval-password")
        values = parse_qs(urlsplit(location).query)
        self.assertEqual(values["state"], ["state-clickup"])
        tokens = restarted.exchange_code(
            {
                "code": [values["code"][0]],
                "client_id": [client_id],
                "redirect_uri": [redirect_uri],
                "code_verifier": [verifier],
                "resource": [restarted.resource],
            }
        )
        self.assertTrue(tokens["access_token"])

    def test_authorization_code_outlives_a_bridge_restart(self) -> None:
        first = self._service()
        redirect_uri = "https://app.clickup.com/mcp/oauth/callback"
        client_id = first.register(
            {
                "client_name": "ClickUp",
                "redirect_uris": [redirect_uri],
            }
        )["client_id"]
        verifier = "v" * 64
        request_id, _client = first.begin_authorization(
            {
                "response_type": ["code"],
                "client_id": [client_id],
                "redirect_uri": [redirect_uri],
                "state": ["state-clickup"],
                "code_challenge": [_pkce(verifier)],
                "code_challenge_method": ["S256"],
                "resource": [first.resource],
                "scope": ["mcp:tools"],
            }
        )
        location = first.approve(request_id, "approval-password")
        code = parse_qs(urlsplit(location).query)["code"][0]

        restarted = self._service()
        tokens = restarted.exchange_code(
            {
                "code": [code],
                "client_id": [client_id],
                "redirect_uri": [redirect_uri],
                "code_verifier": [verifier],
                "resource": [restarted.resource],
            }
        )
        self.assertTrue(tokens["access_token"])

    def test_duplicate_approval_submission_returns_the_same_redirect(self) -> None:
        service = self._service()
        redirect_uri = "https://app.clickup.com/mcp/oauth/callback"
        client_id = service.register(
            {
                "client_name": "ClickUp",
                "redirect_uris": [redirect_uri],
            }
        )["client_id"]
        request_id, _client = service.begin_authorization(
            {
                "response_type": ["code"],
                "client_id": [client_id],
                "redirect_uri": [redirect_uri],
                "state": ["state-clickup"],
                "code_challenge": [_pkce("v" * 64)],
                "code_challenge_method": ["S256"],
                "resource": [service.resource],
                "scope": ["mcp:tools"],
            }
        )
        first = service.approve(request_id, "approval-password")
        second = service.approve(request_id, "approval-password")
        self.assertEqual(second, first)

    def test_pending_and_code_state_contains_only_digests(self) -> None:
        service = self._service()
        redirect_uri = "https://app.clickup.com/mcp/oauth/callback"
        client_id = service.register(
            {
                "client_name": "ClickUp",
                "redirect_uris": [redirect_uri],
            }
        )["client_id"]
        request_id, _client = service.begin_authorization(
            {
                "response_type": ["code"],
                "client_id": [client_id],
                "redirect_uri": [redirect_uri],
                "state": ["state-clickup"],
                "code_challenge": [_pkce("v" * 64)],
                "code_challenge_method": ["S256"],
                "resource": [service.resource],
                "scope": ["mcp:tools"],
            }
        )
        location = service.approve(request_id, "approval-password")
        code = parse_qs(urlsplit(location).query)["code"][0]
        assert service.state_path is not None
        saved = service.state_path.read_text(encoding="utf-8")
        self.assertNotIn(request_id, saved)
        self.assertNotIn(code, saved)
        self.assertIn(
            hashlib.sha256(code.encode("utf-8")).hexdigest(),
            saved,
        )

    def test_the_saved_state_holds_no_bearer_token(self) -> None:
        service = self._service()
        _client_id, tokens = self._connect(service)
        assert service.state_path is not None
        saved = service.state_path.read_text(encoding="utf-8")
        for secret in (tokens["access_token"], tokens["refresh_token"]):
            self.assertNotIn(secret, saved)
        # What is stored recognises a token without being one.
        self.assertIn(
            hashlib.sha256(tokens["refresh_token"].encode("utf-8")).hexdigest(),
            saved,
        )
        self.assertNotIn("approval-password", saved)

    def test_a_grant_is_not_inherited_by_a_bridge_on_another_origin(self) -> None:
        first = self._service()
        client_id, tokens = self._connect(first)
        # A Cloudflare Quick Tunnel hands out a new host on every start, and a
        # grant minted for one resource must not be honoured for another.
        moved = self._service("https://other-tunnel.example")
        with self.assertRaises(OAuthBridgeError):
            moved.refresh(
                {
                    "refresh_token": [tokens["refresh_token"]],
                    "client_id": [client_id],
                    "resource": [moved.resource],
                }
            )

    def test_a_replayed_refresh_token_stays_revoked_across_a_restart(self) -> None:
        first = self._service()
        client_id, tokens = self._connect(first)
        rotated = first.refresh(
            {
                "refresh_token": [tokens["refresh_token"]],
                "client_id": [client_id],
                "resource": [first.resource],
            }
        )
        with self.assertRaises(OAuthBridgeError):
            first.refresh(
                {
                    "refresh_token": [tokens["refresh_token"]],
                    "client_id": [client_id],
                    "resource": [first.resource],
                }
            )
        # The replay killed the whole family. A restart must not bring it back:
        # persistence that resurrected a revoked token would be worse than none.
        restarted = self._service()
        with self.assertRaises(OAuthBridgeError):
            restarted.refresh(
                {
                    "refresh_token": [rotated["refresh_token"]],
                    "client_id": [client_id],
                    "resource": [restarted.resource],
                }
            )

    def test_unreadable_state_starts_empty_and_says_so(self) -> None:
        service = self._service()
        client_id, tokens = self._connect(service)
        assert service.state_path is not None
        service.state_path.write_text("{ this is not json", encoding="utf-8")
        reported = io.StringIO()
        with redirect_stdout(reported):
            restarted = self._service()
        self.assertIn("could not read its saved OAuth state", reported.getvalue())
        with self.assertRaises(OAuthBridgeError):
            restarted.refresh(
                {
                    "refresh_token": [tokens["refresh_token"]],
                    "client_id": [client_id],
                    "resource": [restarted.resource],
                }
            )

    def test_a_tampered_grant_is_dropped_rather_than_honoured(self) -> None:
        service = self._service()
        client_id, tokens = self._connect(service)
        assert service.state_path is not None
        payload = json.loads(service.state_path.read_text(encoding="utf-8"))
        for entry in payload["refresh"].values():
            entry["scopes"] = ["mcp:tools", "repo:admin"]
        service.state_path.write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )
        restarted = self._service()
        with self.assertRaises(OAuthBridgeError):
            restarted.refresh(
                {
                    "refresh_token": [tokens["refresh_token"]],
                    "client_id": [client_id],
                    "resource": [restarted.resource],
                }
            )

    @unittest.skipIf(os.name == "nt", "POSIX file modes are not enforced on Windows")
    def test_the_state_file_is_not_readable_by_other_accounts(self) -> None:
        service = self._service()
        self._connect(service)
        assert service.state_path is not None
        self.assertEqual(service.state_path.stat().st_mode & 0o077, 0)

    def test_without_a_state_directory_nothing_is_written(self) -> None:
        # The in-process default has to stay in RAM: a library caller that never
        # asked for a file must not leave grants on disk.
        service = OAuthBridgeService("https://karox.example", "approval-password")
        self._connect(service)
        self.assertIsNone(service.state_path)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_a_contended_rename_is_retried_instead_of_losing_the_client(self) -> None:
        # Losing this write is silent until the next restart, and then the user
        # sees only "OAuth client is not registered" from a connector they added
        # successfully. Windows denies the rename while any reader holds the file.
        service = self._service()
        real_replace = os.replace
        attempts: list[int] = []

        def flaky(src: object, dst: object) -> None:
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError(5, "Access is denied")
            real_replace(src, dst)

        with patch("karox.oauth_bridge.os.replace", side_effect=flaky):
            client_id = service.register(
                {
                    "client_name": "Hyperagent",
                    "redirect_uris": ["https://hyperagent.com/api/mcp"],
                }
            )["client_id"]

        self.assertGreaterEqual(len(attempts), 3)
        assert service.state_path is not None
        stored = json.loads(service.state_path.read_text(encoding="utf-8"))
        self.assertIn(client_id, stored["clients"])

    def test_a_permanent_rename_failure_leaves_no_temporary_file(self) -> None:
        service = self._service()
        with (
            patch(
                "karox.oauth_bridge.os.replace",
                side_effect=PermissionError(5, "Access is denied"),
            ),
            patch("karox.oauth_bridge._STATE_REPLACE_TIMEOUT_SECONDS", 0.05),
            redirect_stdout(io.StringIO()) as captured,
        ):
            service.register(
                {
                    "client_name": "Hyperagent",
                    "redirect_uris": ["https://hyperagent.com/api/mcp"],
                }
            )

        # Serving must continue, but the user has to be told the cache was lost.
        self.assertIn("could not save its OAuth state", captured.getvalue())
        self.assertEqual(list(self.state.glob("*.tmp")), [])

    def test_concurrent_writers_never_share_a_temporary_path(self) -> None:
        # A pid-only temp name let two threads of one bridge interleave bytes
        # into the same file, publishing a truncated state document.
        service = self._service()
        observed: list[str] = []
        real_open = os.open
        guard = threading.Lock()

        def recording(path: object, *args: Any, **kwargs: Any) -> int:
            text = str(path)
            if text.endswith(".tmp"):
                with guard:
                    observed.append(text)
            return real_open(path, *args, **kwargs)

        with patch("karox.oauth_bridge.os.open", side_effect=recording):
            threads = [
                threading.Thread(
                    target=service.register,
                    args=(
                        {
                            "client_name": f"Client {index}",
                            "redirect_uris": [f"https://hyperagent.com/cb/{index}"],
                        },
                    ),
                )
                for index in range(8)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        self.assertEqual(len(observed), len(set(observed)))


if __name__ == "__main__":
    unittest.main()
