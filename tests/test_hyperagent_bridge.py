"""Hyperagent-web bridge: Host policy, OAuth discovery, DCR, PKCE, profile wiring.

Covers the ``hyperagent-web`` OAuth profile end to end at the contract level:
the rebinding Host/Origin guard (including IDNA and trusted-loopback
``Forwarded`` headers), the protected-resource and authorization-server
metadata (plus the ``openid-configuration`` compatibility alias), a strict
Dynamic Client Registration whose redirect host is pinned to ``hyperagent.com``,
the full Authorization Code + PKCE S256 flow, and the managed-launcher profile
that ``karox bridge connect hyperagent-web`` assembles.

Live verification with a real Hyperagent workspace is a separate step; these
tests prove the contract, not the live connection.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlsplit

import httpx
import uvicorn

from _support import SRC  # noqa: F401

from karox.bridge import BridgeProfile, BridgeStatus, known_bridge_profiles
from karox.oauth_bridge import OAuthBridgeError, build_oauth_proxy_asgi_app
from karox.proxy import ProxyToolDescriptor
from karox.proxy_server import (
    normalize_host,
    forwarded_host,
    peer_is_loopback,
    resolve_allowed_hosts,
)
from karox.web_bridge_launcher import (
    WEB_BRIDGE_PROFILES,
    WebBridgeConnectConfig,
    profile_redirect_hosts,
    web_bridge_connection_instructions,
    web_bridge_diagnostics,
)
from karox.web_bridge_profiles import SavedWebBridgeProfile


HYPERAGENT_REDIRECT = "https://hyperagent.com/api/mcp-serve"


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
            raise RuntimeError("wire server did not start")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("wire server did not stop")


def _pkce(verifier: str) -> str:
    return (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
        .rstrip(b"=")
        .decode("ascii")
    )


def _asgi_get(
    app: Any,
    path: str,
    *,
    headers: list[tuple[str, str]],
    client: tuple[str, int] = ("127.0.0.1", 54321),
) -> tuple[int, bytes]:
    """Drive one GET through an ASGI app with a controllable peer address.

    A real uvicorn server bound to 127.0.0.1 always reports a loopback client,
    so the loopback-trusted Forwarded path cannot be exercised that way. Raw
    ASGI lets the test set ``client`` to a remote address, which is the only way
    to prove an external caller cannot smuggle a host through Forwarded. The
    well-known discovery routes do not start the MCP manager, so no lifespan is
    needed for them.
    """
    import anyio

    async def drive() -> tuple[int, bytes]:
        scope: dict[str, Any] = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "GET",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "root_path": "",
            "scheme": "http",
            "query_string": b"",
            "headers": [(n.lower().encode(), v.encode()) for n, v in headers],
            "client": client,
            "server": ("127.0.0.1", 8765),
        }
        state: dict[str, Any] = {"status": 0, "chunks": []}

        async def receive() -> dict[str, Any]:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                state["status"] = message["status"]
            elif message["type"] == "http.response.body":
                state["chunks"].append(message.get("body", b""))

        await app(scope, receive, send)
        return state["status"], b"".join(state["chunks"])

    return anyio.run(drive)


class HostNormalizationTests(unittest.TestCase):
    def test_idna_host_normalizes_to_punycode(self) -> None:
        self.assertEqual(normalize_host("MÜnchen.de"), "xn--mnchen-3ya.de")
        self.assertEqual(normalize_host("münich.example:443"),
                         "xn--mnich-kva.example")

    def test_host_with_port_is_normalized_safely(self) -> None:
        self.assertEqual(normalize_host("monsterpc.tail81286.ts.net:443"),
                         "monsterpc.tail81286.ts.net")
        self.assertEqual(normalize_host("[::1]:8765"), "::1")

    def test_ipv6_and_loopback_are_handled_explicitly(self) -> None:
        self.assertEqual(normalize_host("[::1]"), "::1")
        self.assertEqual(normalize_host("127.0.0.1"), "127.0.0.1")
        self.assertEqual(normalize_host("localhost"), "localhost")

    def test_unusable_hosts_become_empty(self) -> None:
        for value in ("", "has space", "has@info", "has/slash", "[broken"):
            self.assertEqual(normalize_host(value), "")

    def test_forwarded_host_takes_leftmost_hop(self) -> None:
        self.assertEqual(
            forwarded_host({"forwarded": "host=monsterpc.tail81286.ts.net"}),
            "monsterpc.tail81286.ts.net",
        )
        self.assertEqual(
            forwarded_host({"x-forwarded-host": "a.example, b.example"}),
            "a.example",
        )

    def test_peer_is_loopback_only_for_local_addresses(self) -> None:
        self.assertTrue(peer_is_loopback({"client": ["127.0.0.1", 8]}))
        self.assertTrue(peer_is_loopback({"client": ["::1", 8]}))
        self.assertFalse(peer_is_loopback({"client": ["5.6.7.8", 8]}))
        self.assertFalse(peer_is_loopback({"client": None}))


class HyperagentOAuthWireTests(unittest.TestCase):
    """The full discovery -> DCR -> authorize -> token -> /mcp chain."""

    def setUp(self) -> None:
        self.app = build_oauth_proxy_asgi_app(
            _Runtime(),
            "approval-password",
            public_url="https://monsterpc.tail81286.ts.net",
            allowed_redirect_hosts=frozenset({"hyperagent.com"}),
        )
        self.server = _WireServer(self.app)
        self.base = f"http://127.0.0.1:{self.server.port}"
        self.host = "monsterpc.tail81286.ts.net"

    def tearDown(self) -> None:
        self.server.close()

    def _client(self) -> httpx.Client:
        return httpx.Client(base_url=self.base, timeout=15.0, headers={"Host": self.host})

    def test_allowed_tailscale_host_reaches_discovery(self) -> None:
        with self._client() as client:
            protected = client.get("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(protected.status_code, 200, protected.text)

    def test_unknown_host_is_421(self) -> None:
        with httpx.Client(base_url=self.base, timeout=15.0,
                          headers={"Host": "rebound.attacker.example"}) as client:
            response = client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(response.status_code, 421, response.text)

    def test_public_url_with_port_is_allowed(self) -> None:
        app = build_oauth_proxy_asgi_app(
            _Runtime(),
            "approval-password",
            public_url="https://example.com:8443",
        )
        server = _WireServer(app)
        try:
            base = f"http://127.0.0.1:{server.port}"
            with httpx.Client(base_url=base, timeout=15.0,
                              headers={"Host": "example.com:8443"}) as client:
                response = client.get("/.well-known/oauth-authorization-server")
            self.assertEqual(response.status_code, 200, response.text)
        finally:
            server.close()

    def test_forwarded_host_from_untrusted_client_is_rejected(self) -> None:
        # A remote peer claiming the allowed host in Forwarded must not bypass
        # the guard; only a loopback peer (the tunnel child) may do that.
        app = build_oauth_proxy_asgi_app(
            _Runtime(),
            "approval-password",
            public_url=f"https://{self.host}",
            allowed_redirect_hosts=frozenset({"hyperagent.com"}),
        )
        status, _body = _asgi_get(
            app,
            "/.well-known/oauth-authorization-server",
            headers=[("host", "rebound.attacker.example"),
                     ("x-forwarded-host", self.host)],
            client=("5.6.7.8", 44000),
        )
        self.assertEqual(status, 421)

    def test_forwarded_host_from_trusted_loopback_peer_is_accepted(self) -> None:
        # The tunnel child connects from loopback and may preserve the public
        # host in Forwarded while rewriting Host to its loopback target.
        app = build_oauth_proxy_asgi_app(
            _Runtime(),
            "approval-password",
            public_url=f"https://{self.host}",
            allowed_redirect_hosts=frozenset({"hyperagent.com"}),
        )
        status, body = _asgi_get(
            app,
            "/.well-known/oauth-authorization-server",
            headers=[("host", "127.0.0.1:8765"),
                     ("x-forwarded-host", self.host)],
            client=("127.0.0.1", 44000),
        )
        self.assertEqual(status, 200, body.decode("utf-8", "replace"))

    def test_421_logs_redacted_diagnostics_without_secrets(self) -> None:
        captured = io.StringIO()
        with httpx.Client(base_url=self.base, timeout=15.0,
                          headers={"Host": "rebound.attacker.example"}) as client:
            with redirect_stderr(captured):
                response = client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(response.status_code, 421)
        log = captured.getvalue()
        self.assertIn("[karox-rebind] 421", log)
        self.assertIn("host_not_allowed", log)
        self.assertIn("request_id=", log)
        # No secret material is logged on a discovery GET with no body.
        self.assertNotIn("approval-password", log)

    def test_protected_resource_metadata_for_mcp(self) -> None:
        with self._client() as client:
            response = client.get("/.well-known/oauth-protected-resource/mcp")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["resource"], "https://monsterpc.tail81286.ts.net/mcp")
        self.assertEqual(body["authorization_servers"],
                         ["https://monsterpc.tail81286.ts.net"])
        self.assertNotIn("127.0.0.1", json.dumps(body))

    def test_authorization_server_metadata(self) -> None:
        with self._client() as client:
            response = client.get("/.well-known/oauth-authorization-server")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["issuer"], "https://monsterpc.tail81286.ts.net")
        self.assertEqual(body["authorization_endpoint"],
                         "https://monsterpc.tail81286.ts.net/oauth/authorize")
        self.assertEqual(body["token_endpoint"],
                         "https://monsterpc.tail81286.ts.net/oauth/token")
        self.assertEqual(body["registration_endpoint"],
                         "https://monsterpc.tail81286.ts.net/oauth/register")
        self.assertIn("S256", body["code_challenge_methods_supported"])
        self.assertIn("none", body["token_endpoint_auth_methods_supported"])
        self.assertNotIn("127.0.0.1", json.dumps(body))

    def test_openid_configuration_is_a_compatibility_alias(self) -> None:
        with self._client() as client:
            auth_server = client.get("/.well-known/oauth-authorization-server")
            openid = client.get("/.well-known/openid-configuration")
        self.assertEqual(openid.status_code, 200, openid.text)
        self.assertEqual(openid.json(), auth_server.json())
        # KaroX is not an OIDC provider: no ID-token claims are advertised.
        self.assertNotIn("userinfo_endpoint", openid.json())

    def test_discovery_endpoints_do_not_require_a_bearer_token(self) -> None:
        with self._client() as client:
            for path in (
                "/.well-known/oauth-protected-resource/mcp",
                "/.well-known/oauth-authorization-server",
                "/.well-known/openid-configuration",
            ):
                response = client.get(path)
                self.assertEqual(response.status_code, 200, path)

    def test_dcr_registers_a_public_pkce_client(self) -> None:
        with self._client() as client:
            response = client.post(
                "/oauth/register",
                json={
                    "client_name": "HyperAgent",
                    "redirect_uris": [HYPERAGENT_REDIRECT],
                    "token_endpoint_auth_method": "none",
                    "grant_types": ["authorization_code", "refresh_token"],
                    "response_types": ["code"],
                },
            )
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertTrue(body["client_id"])
        self.assertEqual(body["token_endpoint_auth_method"], "none")
        self.assertNotIn("client_secret", body)
        self.assertEqual(body["redirect_uris"], [HYPERAGENT_REDIRECT])

    def test_dcr_accepts_exact_hyperagent_redirect_uri(self) -> None:
        with self._client() as client:
            response = client.post(
                "/oauth/register",
                json={"redirect_uris": ["https://hyperagent.com/api/mcp"]},
            )
        self.assertEqual(response.status_code, 201, response.text)

    def test_dcr_rejects_a_redirect_uri_on_another_host(self) -> None:
        with self._client() as client:
            response = client.post(
                "/oauth/register",
                json={"redirect_uris": ["https://chatgpt.com/connector/oauth/callback"]},
            )
        self.assertEqual(response.status_code, 400, response.text)

    def test_dcr_rejects_wildcard_redirect(self) -> None:
        with self._client() as client:
            response = client.post(
                "/oauth/register",
                json={"redirect_uris": ["https://*.hyperagent.com/cb"]},
            )
        self.assertEqual(response.status_code, 400)

    def test_dcr_rejects_http_redirect_for_an_external_client(self) -> None:
        with self._client() as client:
            response = client.post(
                "/oauth/register",
                json={"redirect_uris": ["http://hyperagent.com/cb"]},
            )
        self.assertEqual(response.status_code, 400)

    def test_dcr_rejects_non_web_scheme(self) -> None:
        with self._client() as client:
            for scheme in ("javascript:alert(1)", "data:text/plain,x", "file:///etc"):
                with self.subTest(scheme=scheme):
                    response = client.post(
                        "/oauth/register",
                        json={"redirect_uris": [scheme]},
                    )
                    self.assertEqual(response.status_code, 400)

    def test_dcr_enforces_body_size_limit(self) -> None:
        oversized = json.dumps(
            {"redirect_uris": ["https://hyperagent.com/" + "x" * 70_000]}
        ).encode("utf-8")
        with self._client() as client:
            response = client.post(
                "/oauth/register",
                content=oversized,
                headers={"Content-Type": "application/json"},
            )
        self.assertEqual(response.status_code, 400)

    def test_identical_re_registration_is_idempotent(self) -> None:
        with self._client() as client:
            first = client.post(
                "/oauth/register",
                json={"client_name": "HyperAgent",
                      "redirect_uris": [HYPERAGENT_REDIRECT]},
            )
            second = client.post(
                "/oauth/register",
                json={"client_name": "HyperAgent",
                      "redirect_uris": [HYPERAGENT_REDIRECT]},
            )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 201)
        self.assertEqual(first.json()["client_id"], second.json()["client_id"])

    # --- the full PKCE flow ---

    def _authorize(self, client: httpx.Client) -> tuple[str, str, str]:
        registration = client.post(
            "/oauth/register",
            json={"client_name": "HyperAgent",
                  "redirect_uris": [HYPERAGENT_REDIRECT]},
        )
        client_id = registration.json()["client_id"]
        verifier = "v" * 64
        state = "state-abc"
        auth = client.get(
            "/oauth/authorize",
            params={
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": HYPERAGENT_REDIRECT,
                "state": state,
                "code_challenge": _pkce(verifier),
                "code_challenge_method": "S256",
                "resource": "https://monsterpc.tail81286.ts.net/mcp",
                "scope": "mcp:tools offline_access",
            },
        )
        self.assertEqual(auth.status_code, 200, auth.text)
        request_id = re.search(r'name="request_id" value="([^"]+)"', auth.text)
        assert request_id is not None
        approved = client.post(
            "/oauth/authorize",
            data={"request_id": request_id.group(1), "password": "approval-password"},
            follow_redirects=False,
        )
        self.assertEqual(approved.status_code, 303, approved.text)
        query = parse_qs(urlsplit(approved.headers["location"]).query)
        self.assertEqual(query["state"], [state])
        return client_id, verifier, query["code"][0]

    def test_pkce_s256_flow_and_initialize_then_tools_list(self) -> None:
        with self._client() as client:
            client_id, verifier, code = self._authorize(client)
            token = client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "redirect_uri": HYPERAGENT_REDIRECT,
                    "code_verifier": verifier,
                    "resource": "https://monsterpc.tail81286.ts.net/mcp",
                },
            )
            self.assertEqual(token.status_code, 200, token.text)
            access = token.json()["access_token"]
            self.assertNotIn(access, token.text.replace(access, "<redacted>"))
            headers = {
                "Authorization": f"Bearer {access}",
                "accept": "application/json",
                "content-type": "application/json",
            }
            init = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "hyperagent", "version": "1"}},
            })
            self.assertNotEqual(init.status_code, 401, init.text)
            listing = client.post("/mcp", headers=headers, json={
                "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
            })
            self.assertNotEqual(listing.status_code, 401, listing.text)
            self.assertIn("echo", listing.text)

    def test_plain_pkce_is_rejected(self) -> None:
        with self._client() as client:
            registration = client.post(
                "/oauth/register",
                json={"redirect_uris": [HYPERAGENT_REDIRECT]},
            )
            client_id = registration.json()["client_id"]
            response = client.get(
                "/oauth/authorize",
                params={
                    "response_type": "code",
                    "client_id": client_id,
                    "redirect_uri": HYPERAGENT_REDIRECT,
                    "state": "s",
                    "code_challenge": _pkce("v" * 64),
                    "code_challenge_method": "plain",
                    "resource": "https://monsterpc.tail81286.ts.net/mcp",
                },
            )
        self.assertEqual(response.status_code, 400, response.text)

    def test_authorization_code_is_one_time(self) -> None:
        with self._client() as client:
            client_id, verifier, code = self._authorize(client)
            data = {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": client_id,
                "redirect_uri": HYPERAGENT_REDIRECT,
                "code_verifier": verifier,
                "resource": "https://monsterpc.tail81286.ts.net/mcp",
            }
            first = client.post("/oauth/token", data=data)
            self.assertEqual(first.status_code, 200, first.text)
            second = client.post("/oauth/token", data=data)
        self.assertEqual(second.status_code, 400)

    def test_code_is_bound_to_exact_redirect_uri(self) -> None:
        with self._client() as client:
            client_id, verifier, code = self._authorize(client)
            response = client.post(
                "/oauth/token",
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "redirect_uri": "https://hyperagent.com/different",
                    "code_verifier": verifier,
                    "resource": "https://monsterpc.tail81286.ts.net/mcp",
                },
            )
        self.assertEqual(response.status_code, 400)

    def test_state_is_preserved_in_the_redirect(self) -> None:
        with self._client() as client:
            registration = client.post(
                "/oauth/register",
                json={"redirect_uris": [HYPERAGENT_REDIRECT]},
            )
            client_id = registration.json()["client_id"]
            auth = client.get(
                "/oauth/authorize",
                params={
                    "response_type": "code", "client_id": client_id,
                    "redirect_uri": HYPERAGENT_REDIRECT, "state": "xyz-123",
                    "code_challenge": _pkce("v" * 64),
                    "code_challenge_method": "S256",
                    "resource": "https://monsterpc.tail81286.ts.net/mcp",
                },
            )
            request_id = re.search(r'name="request_id" value="([^"]+)"', auth.text)
            assert request_id is not None
            approved = client.post(
                "/oauth/authorize",
                data={"request_id": request_id.group(1),
                      "password": "approval-password"},
                follow_redirects=False,
            )
        self.assertEqual(parse_qs(urlsplit(approved.headers["location"]).query)["state"],
                         ["xyz-123"])

    def test_anonymous_mcp_gets_an_oauth_challenge(self) -> None:
        with self._client() as client:
            response = client.post("/mcp", content=b"{}")
        self.assertEqual(response.status_code, 401, response.text)
        self.assertIn("oauth-protected-resource/mcp",
                      response.headers["www-authenticate"])

    def test_approval_password_is_not_a_client_secret(self) -> None:
        # The approval password is the KaroX-side gate on the consent page; it is
        # never returned by DCR and never exchanged as a client_secret.
        with self._client() as client:
            registration = client.post(
                "/oauth/register",
                json={"redirect_uris": [HYPERAGENT_REDIRECT]},
            )
        body = registration.json()
        self.assertNotIn("client_secret", body)
        self.assertNotIn("approval-password", json.dumps(body))


class PermissiveDefaultStillWorksTests(unittest.TestCase):
    """chatgpt-web/claude-web keep the permissive redirect policy."""

    def test_chatgpt_redirect_is_accepted_without_a_strict_policy(self) -> None:
        app = build_oauth_proxy_asgi_app(
            _Runtime(),
            "approval-password",
            public_url="https://karox.example",
        )
        server = _WireServer(app)
        try:
            base = f"http://127.0.0.1:{server.port}"
            with httpx.Client(base_url=base, timeout=15.0,
                              headers={"Host": "karox.example"}) as client:
                response = client.post(
                    "/oauth/register",
                    json={"redirect_uris":
                          ["https://chatgpt.com/connector/oauth/callback"]},
                )
            self.assertEqual(response.status_code, 201, response.text)
        finally:
            server.close()


class HyperagentProfileTests(unittest.TestCase):
    def test_hyperagent_web_is_a_known_oauth_profile(self) -> None:
        profile = next(
            p for p in known_bridge_profiles() if p.name == "hyperagent-web"
        )
        self.assertEqual(profile.auth_scheme, "oauth")
        self.assertEqual(profile.transport, "streamable_http")
        self.assertTrue(profile.persistent_url)

    def test_hyperagent_web_is_in_the_web_bridge_profiles_tuple(self) -> None:
        self.assertIn("hyperagent-web", WEB_BRIDGE_PROFILES)
        self.assertEqual(profile_redirect_hosts("hyperagent-web"),
                         frozenset({"hyperagent.com"}))
        self.assertIsNone(profile_redirect_hosts("chatgpt-web"))

    def test_saved_profile_accepts_hyperagent_web_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            profile = SavedWebBridgeProfile(
                name="hyperagent-dev",
                target_profile="hyperagent-web",
                repository=tmp,
                tools=("karox.repo.read_file", "karox.git.status"),
                tunnel="tailscale",
            )
            self.assertEqual(profile.target_profile, "hyperagent-web")

    def test_connect_config_passes_allowed_redirect_hosts_to_argv(self) -> None:
        from karox.web_bridge_launcher import _bridge_argv

        config = WebBridgeConnectConfig(
            profile="hyperagent-web",
            repository=Path.cwd(),
            tools=("karox.repo.read_file",),
            tunnel="tailscale",
        )
        argv = _bridge_argv(
            config,
            session_id="sess-1",
            public_url="https://monsterpc.tail81286.ts.net",
        )
        self.assertIn("--allowed-redirect-hosts", argv)
        idx = argv.index("--allowed-redirect-hosts")
        self.assertEqual(argv[idx + 1], "hyperagent.com")

    def test_connect_config_omits_flag_for_permissive_profile(self) -> None:
        from karox.web_bridge_launcher import _bridge_argv

        config = WebBridgeConnectConfig(
            profile="chatgpt-web",
            repository=Path.cwd(),
            tools=("karox.repo.read_file",),
            tunnel="cloudflare",
        )
        argv = _bridge_argv(
            config,
            session_id="sess-2",
            public_url="https://abc.trycloudflare.com",
        )
        self.assertNotIn("--allowed-redirect-hosts", argv)

    def test_diagnostics_describe_hyperagent_profile(self) -> None:
        config = WebBridgeConnectConfig(
            profile="hyperagent-web",
            repository=Path.cwd(),
            tools=("karox.repo.read_file",),
            tunnel="tailscale",
        )
        report = web_bridge_diagnostics(config)
        self.assertEqual(report["target_profile"], "hyperagent-web")
        self.assertEqual(report["url_stability"], "stable_device_hostname")

    def test_instructions_for_hyperagent_tell_the_user_not_to_use_byo_oauth(self) -> None:
        en = web_bridge_connection_instructions("hyperagent-web")
        self.assertTrue(any("Bring my own OAuth app" in line for line in en))
        self.assertTrue(any("Client Secret" in line for line in en))
        ru = web_bridge_connection_instructions("hyperagent-web", language="ru")
        self.assertTrue(any("Bring my own OAuth app" in line for line in ru))
        self.assertTrue(any("Client Secret" in line for line in ru))

    def test_metadata_carries_no_localhost_for_an_external_origin(self) -> None:
        app = build_oauth_proxy_asgi_app(
            _Runtime(),
            "approval-password",
            public_url="https://monsterpc.tail81286.ts.net",
        )
        server = _WireServer(app)
        try:
            base = f"http://127.0.0.1:{server.port}"
            with httpx.Client(base_url=base, timeout=15.0,
                              headers={"Host": "monsterpc.tail81286.ts.net"}) as client:
                protected = client.get("/.well-known/oauth-protected-resource/mcp").json()
                auth = client.get("/.well-known/oauth-authorization-server").json()
                openid = client.get("/.well-known/openid-configuration").json()
        finally:
            server.close()
        for body in (protected, auth, openid):
            blob = json.dumps(body)
            self.assertNotIn("127.0.0.1", blob)
            self.assertNotIn("localhost", blob)


if __name__ == "__main__":
    unittest.main()
