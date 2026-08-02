"""Live connection test against a real Streamable HTTP MCP server with bearer auth.

Reuses the project's own ``_mcp_http_server`` fixture (a real ``mcp`` SDK
StreamableHTTPSessionManager on an ephemeral port, rejecting every call without
``Authorization: Bearer <token>``) so the test exercises the genuine MCP
``initialize`` / ``tools/list`` transport, not a mock.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import uvicorn

from _support import SRC  # noqa: F401 - inserts src on sys.path
from _mcp_http_server import build_asgi_app, expected_token

from karox.connection_tests import test_mcp_client_target as _run_test_mcp_client_target
from karox.connections import (
    ConnectionCredentialStore,
    build_target_from_preset,
)


class _FakeBackend:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set(self, service, account, secret) -> None:
        self.store[(service, account)] = secret

    def get(self, service, account):
        return self.store.get((service, account))

    def delete(self, service, account) -> None:
        self.store.pop((service, account), None)


class McpConnectionTestE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        os.environ["KAROX_MCP_HTTP_TOKEN"] = expected_token()
        cls.app = build_asgi_app()
        config = uvicorn.Config(cls.app, host="127.0.0.1", port=0, log_level="error")
        cls.server = uvicorn.Server(config)
        cls.thread = threading.Thread(target=cls.server.run, daemon=True)
        cls.thread.start()
        deadline = time.time() + 15
        while (not cls.server.started or not cls.server.servers) and time.time() < deadline:
            time.sleep(0.02)
        cls.port = cls.server.servers[0].sockets[0].getsockname()[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.should_exit = True
        cls.thread.join(timeout=5)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.backend = _FakeBackend()
        self.credentials = ConnectionCredentialStore(backend=self.backend)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _target(self, *, auth_scheme: str = "bearer", secret: str = expected_token()):
        if auth_scheme == "none":
            return build_target_from_preset(
                "generic-mcp",
                name="echo",
                auth_scheme="none",
                tunnel="local",
            )
        info = self.credentials.set("echo", secret)
        return build_target_from_preset(
            "generic-mcp",
            name="echo",
            credential_ref=info["reference"],
            credential_fingerprint=info["fingerprint"],
            auth_scheme=auth_scheme,
        )

    def test_correct_auth_passes_initialize_and_tools_list(self) -> None:
        target = self._target()
        url = f"http://127.0.0.1:{self.port}/mcp"
        result = _run_test_mcp_client_target(
            target, endpoint_url=url, secret=expected_token(), timeout_seconds=15.0
        )
        self.assertEqual(result["state"], "ok", result)
        self.assertEqual(result["wire"], "streamable_http")
        self.assertEqual(result["tool_count"], 2)

    def test_wrong_auth_is_rejected(self) -> None:
        target = self._target()
        url = f"http://127.0.0.1:{self.port}/mcp"
        result = _run_test_mcp_client_target(
            target, endpoint_url=url, secret="wrong-token", timeout_seconds=15.0
        )
        self.assertEqual(result["state"], "failed")
        self.assertIn(result["failure_kind"], {"unauthorized", "mcp_error"})

    def test_none_auth_is_reported_unsafe(self) -> None:
        target = self._target(auth_scheme="none")
        result = _run_test_mcp_client_target(
            target, endpoint_url=f"http://127.0.0.1:{self.port}/mcp", secret="", timeout_seconds=15.0
        )
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["failure_kind"], "unsafe")

    def test_oauth_reports_needs_flow(self) -> None:
        target = build_target_from_preset(
            "chatgpt-web",
            name="oauth",
            url_stability="stable",
            credential_ref=self.credentials.set("oauth", "sek").get("reference", ""),
            credential_fingerprint="sha256:abc",
        )
        result = _run_test_mcp_client_target(
            target, endpoint_url=f"http://127.0.0.1:{self.port}/mcp", secret="sek", timeout_seconds=15.0
        )
        self.assertEqual(result["state"], "needs_oauth_flow")

    def test_invalid_url_is_classified(self) -> None:
        target = self._target()
        result = _run_test_mcp_client_target(target, endpoint_url="not-a-url", secret="x")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["failure_kind"], "invalid_url")

    def test_dns_resolution_failure_is_classified_dns_failure(self) -> None:
        # Regression for the "mcp_error — getaddrinfo failed" defect: the
        # MCP SDK wraps the socket.gaierror in an anyio TaskGroup, and the
        # classifier used to drop it into the catch-all ``mcp_error`` bucket.
        # The real cause ("getaddrinfo failed") must surface as ``dns_failure``
        # so the ClickUp orchestrator can fall back to loopback (a generic
        # mcp_error verdict does not trigger the fallback).
        target = self._target()
        # An RFC6761 reserved TLD that MUST NOT resolve under any circumstance;
        # avoids dependence on a transiently-broken network for the assertion.
        url = "https://nonexistent-reserved.invalid./mcp"
        result = _run_test_mcp_client_target(target, endpoint_url=url, secret="x", timeout_seconds=10.0)
        self.assertEqual(result["state"], "failed")
        self.assertIn(result["failure_kind"], {"dns_failure", "network", "timeout"})


if __name__ == "__main__":
    unittest.main()
