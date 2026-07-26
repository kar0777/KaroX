"""Tests for the Phase 7 bridge profiles, credentials, and MCP proxy.

Unit tests cover the declarative profile registry, the dedicated bridge
credential store, and the proxy authorization boundaries.  The end-to-end test
drives a *real* stdio MCP server through ``McpProxy`` so the hosted-client proxy
path is proven against the genuine transport -- not a mock.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from pathlib import Path
from typing import Optional

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.bridge import (
    BridgeAccessDenied,
    BridgeConfigurationError,
    BridgeCredentialReference,
    BridgeCredentialStore,
    BridgeError,
    BridgeProfile,
    BridgeRegistry,
    BridgeStatus,
    known_bridge_profiles,
)
from karox.credentials import CredentialError
from karox.mcp_client import (
    McpClient,
    McpCredentialStore,
    McpRegistry,
    McpServerRecord,
    McpTransportError,
    mcp_selection,
)
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy, PolicyDenied
from karox.proxy import McpProxy, ProxyAccessDenied, ProxyError
from karox.proxy_server import build_proxy_asgi_app
from karox.sessions import SessionRecord, SessionStore


class _FakeCredentialBackend:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.store[(service, account)] = secret

    def get(self, service: str, account: str) -> Optional[str]:
        return self.store.get((service, account))

    def delete(self, service: str, account: str) -> None:
        if (service, account) not in self.store:
            raise CredentialError("credential does not exist")
        self.store.pop((service, account), None)


def _stdio_record(registry_root: Path, *, server_id: str = "echo", namespace: str = "echo") -> McpServerRecord:
    return McpServerRecord(
        server_id=server_id, namespace=namespace, transport="stdio",
        command=sys.executable,
        args=(str((Path(__file__).resolve().parent / "_mcp_echo_server.py")),),
        read_only_tools=("echo",), timeout_seconds=15.0,
    )


class BridgeProfileTests(unittest.TestCase):
    def test_known_profiles_have_honest_statuses(self) -> None:
        profiles = known_bridge_profiles()
        names = [p.name for p in profiles]
        self.assertIn("notion", names)
        notion = next(p for p in profiles if p.name == "notion")
        self.assertEqual(notion.status, BridgeStatus.TESTED)
        self.assertTrue(notion.verified_versions)
        for p in profiles:
            if p.name in {"hyperagent", "promptql"}:
                self.assertEqual(p.status, BridgeStatus.EXPERIMENTAL)
            # No profile claims tested without verified versions.
            if p.status == BridgeStatus.TESTED:
                self.assertTrue(p.verified_versions)

    def test_tested_status_requires_verified_versions(self) -> None:
        with self.assertRaisesRegex(ValueError, "require verified versions"):
            BridgeProfile(
                name="x", transport="streamable_http", status=BridgeStatus.TESTED,
            )

    def test_invalid_transport_and_status_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "transport must be"):
            BridgeProfile(name="x", transport="ws", status=BridgeStatus.PLANNED)
        with self.assertRaisesRegex(ValueError, "status must be"):
            BridgeProfile(name="x", transport="streamable_http", status="maybe")

    def test_planned_profile_cannot_use_stdio(self) -> None:
        with self.assertRaisesRegex(ValueError, "planned bridge profiles cannot use stdio"):
            BridgeProfile(name="x", transport="stdio", status=BridgeStatus.PLANNED)

    def test_unsafe_name_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "safe characters"):
            BridgeProfile(name="bad name", transport="streamable_http", status=BridgeStatus.EXPERIMENTAL)


class BridgeRegistryTests(unittest.TestCase):
    def test_get_unknown_raises_and_usable_filters(self) -> None:
        registry = BridgeRegistry()
        with self.assertRaisesRegex(BridgeConfigurationError, "unknown bridge profile"):
            registry.get("nope")
        usable = [p.name for p in registry.usable()]
        self.assertIn("notion", usable)
        self.assertNotIn("hyperagent", usable)

    def test_duplicate_profile_names_rejected(self) -> None:
        profile = BridgeProfile(name="dup", transport="streamable_http", status=BridgeStatus.EXPERIMENTAL)
        with self.assertRaisesRegex(BridgeConfigurationError, "names must be unique"):
            BridgeRegistry([profile, profile])


class BridgeCredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = _FakeCredentialBackend()
        self.store = BridgeCredentialStore(backend=self.backend)

    def test_generate_set_resolve_rotate_delete(self) -> None:
        token = self.store.generate()
        self.assertGreaterEqual(len(token), 32)
        info = self.store.set("bridge-1", token)
        self.assertTrue(info["reference"].startswith("os-keyring:bridge/"))
        self.assertTrue(info["fingerprint"].startswith("sha256:"))
        self.assertEqual(self.store.resolve(info["reference"]), token)
        rotated = self.store.rotate("bridge-1")
        self.assertIn("secret", rotated)
        self.assertNotEqual(
            self.store.resolve(rotated["reference"]), token,
        )
        self.assertEqual(self.store.delete("bridge-1")["status"], "revoked")

    def test_rotation_failure_preserves_previous_credential(self) -> None:
        self.store.set("bridge-1", "previous-value")
        with patch.object(self.backend, "set", side_effect=RuntimeError("offline")):
            with self.assertRaises(CredentialError):
                self.store.rotate("bridge-1")
        self.assertEqual(
            self.store.resolve("os-keyring:bridge/bridge-1"), "previous-value"
        )

    def test_resolve_missing_fails_closed(self) -> None:
        with self.assertRaises(CredentialError):
            self.store.resolve("os-keyring:bridge/missing")

    def test_invalid_secret_values_rejected(self) -> None:
        for bad in ("", "has\nnewline", "x" * 65_537):
            with self.assertRaises(ValueError):
                self.store.set("x", bad)

    def test_reference_parse_rejects_foreign_prefix(self) -> None:
        with self.assertRaises(ValueError):
            BridgeCredentialReference.parse("os-keyring:mcp/x")

    def test_doctor_reports_bridge_scope(self) -> None:
        self.assertEqual(self.store.doctor()["scope"], "bridge")


class McpProxyTests(unittest.TestCase):
    """Proxy authorization boundaries with a real stdio MCP server."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        self.registry = McpRegistry(self.root / "mcp-servers.json")
        self.record_server = _stdio_record(self.root)
        self.registry.put(self.record_server)
        self.client = McpClient(self.registry)
        self.tools = self.client.discover(self.record_server.server_id, self.repository)
        self.sessions = SessionStore(self.root / "sessions")
        self.session_record = self.sessions.create(
            self.repository, "task", session_id="s",
        )
        self.hosted_origin = Origin(OriginKind.HOSTED_CLIENT, "notion-bridge")
        self.proxied_origin = Origin(OriginKind.PROXIED_MCP, "notion-bridge-external")
        self.policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        self.policy.set_grants(self.hosted_origin, {Capability.MCP_CALL})
        self.policy.set_grants(self.proxied_origin, {Capability.MCP_CALL})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _select(self, decisions: dict[str, str]) -> None:
        selection = mcp_selection(self.record_server, self.tools, decisions)
        with self.sessions.mutate("s", "select") as record:
            record.mcp_servers = [selection]
        self.session_record = self.sessions.load("s")

    def _proxy(
        self,
        allowed: list[str],
        *,
        policy: Optional[CapabilityPolicy] = None,
        hosted_origin: Optional[Origin] = None,
        proxied_origin: Optional[Origin] = None,
    ) -> McpProxy:
        return McpProxy(
            self.client,
            self.repository,
            self.sessions,
            "s",
            allowed,
            policy=policy or self.policy,
            hosted_origin=hosted_origin or self.hosted_origin,
            proxied_origin=proxied_origin or self.proxied_origin,
            audit_path=self.root / "proxy-audit.jsonl",
        )

    def test_proxy_requires_hosted_client_origin(self) -> None:
        with self.assertRaisesRegex(ProxyAccessDenied, "must be a hosted client"):
            self._proxy(
                ["echo"], hosted_origin=Origin(OriginKind.NATIVE_AGENT, "native"),
            )

    def test_empty_allowlist_rejected(self) -> None:
        with self.assertRaisesRegex(ProxyAccessDenied, "allowlist must not be empty"):
            self._proxy([])

    def test_duplicate_allowlist_server_is_rejected(self) -> None:
        with self.assertRaisesRegex(ProxyAccessDenied, "duplicate"):
            self._proxy(["echo", "echo"])

    def test_proxy_only_exposes_allowed_servers_and_tools(self) -> None:
        self._select({"echo": "allow", "write_note": "deny"})
        proxy = self._proxy(["echo"])
        descriptors = proxy.descriptors()
        names = [d.name for d in descriptors]
        self.assertEqual(names, ["mcp.echo.echo"])
        # The deny-permitted tool is invisible to the hosted client.
        self.assertNotIn("mcp.echo.write_note", names)
        # Descriptors are secret-free.
        for d in descriptors:
            self.assertNotIn("command", d.to_dict())
            self.assertNotIn("url", d.to_dict())

    def test_proxy_rejects_unselected_server(self) -> None:
        with self.assertRaisesRegex(ProxyAccessDenied, "not selected for this session"):
            self._proxy(["echo"]).descriptors()

    def test_proxy_rejects_server_not_in_allowlist(self) -> None:
        # Add a second server that is selected in the session but kept out of
        # the proxy allowlist; its tools must be invisible to the hosted client.
        second = _stdio_record(self.root, server_id="second", namespace="second")
        self.registry.put(second)
        tools = self.client.discover("second", self.repository)
        all_tools = self.tools + tools
        self._select_two(all_tools)
        proxy = self._proxy(["echo"])
        names = [d.name for d in proxy.descriptors()]
        self.assertTrue(all(n.startswith("mcp.echo.") for n in names))
        self.assertFalse(any(n.startswith("mcp.second.") for n in names))

    def _select_two(self, tools: list) -> None:
        echo_server = self.record_server
        second_server = self.registry.get("second")
        sel1 = mcp_selection(echo_server, [t for t in tools if t.server_id == "echo"], {"echo": "allow", "write_note": "allow"})
        sel2 = mcp_selection(second_server, [t for t in tools if t.server_id == "second"], {"echo": "allow", "write_note": "allow"})
        with self.sessions.mutate("s", "select2") as record:
            record.mcp_servers = [sel1, sel2]
        self.session_record = self.sessions.load("s")

    def test_policy_boundary_must_grant_mcp_call(self) -> None:
        self._select({"echo": "allow", "write_note": "allow"})
        policy = CapabilityPolicy(AccessProfile.READ_ONLY)
        proxy = self._proxy(["echo"], policy=policy)
        # A read-only profile never includes MCP_CALL, so the hosted client
        # without an explicit grant is denied at the policy boundary.
        with self.assertRaises(PolicyDenied):
            proxy.descriptors()

    def test_proxy_requires_independent_proxied_origin_grant(self) -> None:
        self._select({"echo": "allow"})
        policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        policy.set_grants(self.hosted_origin, {Capability.MCP_CALL})
        proxy = self._proxy(["echo"], policy=policy)
        with self.assertRaises(PolicyDenied):
            proxy.descriptors()

    def test_e2e_proxy_executes_read_only_call_through_two_boundaries(self) -> None:
        self._select({"echo": "allow"})
        proxy = self._proxy(["echo"])
        result = proxy.execute("mcp.echo.echo", {"message": "proxied"})
        self.assertEqual(result["tool"], "mcp.echo.echo")
        self.assertEqual(result["result"]["content"][0]["text"], "echo: proxied")

    def test_streamable_http_wire_server_authenticates_and_forwards(self) -> None:
        import uvicorn

        self._select({"echo": "allow", "write_note": "allow"})
        token = "wire-test-token"
        active_token = {"value": token}
        app = build_proxy_asgi_app(
            self._proxy(["echo"]), lambda: active_token["value"]
        )
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
        )
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        deadline = time.time() + 15
        while (not server.started or not server.servers) and time.time() < deadline:
            time.sleep(0.02)
        self.assertTrue(server.started)
        port = server.servers[0].sockets[0].getsockname()[1]
        backend = _FakeCredentialBackend()
        credentials = McpCredentialStore(backend=backend)
        info = credentials.set("wire", token)
        wire_record = McpServerRecord(
            server_id="wire",
            namespace="wire",
            transport="streamable_http",
            url=f"http://127.0.0.1:{port}/mcp",
            credential_ref=info["reference"],
            credential_target="Authorization",
            read_only_tools=("mcp.echo.echo",),
            timeout_seconds=15.0,
        )
        wire_registry = McpRegistry(self.root / "wire-registry.json")
        wire_registry.put(wire_record)
        wire_client = McpClient(wire_registry, credentials)
        try:
            unauthorized = McpServerRecord(
                server_id="unauthorized",
                namespace="unauthorized",
                transport="streamable_http",
                url=f"http://127.0.0.1:{port}/mcp",
                read_only_tools=("mcp.echo.echo",),
                timeout_seconds=15.0,
            )
            with self.assertRaises(McpTransportError):
                McpClient(McpRegistry(self.root / "empty.json")).discover_record(
                    unauthorized, self.repository
                )
            descriptors = wire_client.discover("wire", self.repository)
            descriptor = next(
                item for item in descriptors if item.remote_name == "mcp.echo.echo"
            )
            mutating = next(
                item
                for item in descriptors
                if item.remote_name == "mcp.echo.write_note"
            )
            # A mutating call without client _meta now succeeds: the bridge
            # auto-generates a unique idempotency key so standards-compliant
            # clients (Notion Custom Agents, etc.) can still run mutating
            # tools. Only an explicitly supplied invalid key is rejected.
            mutation_result = wire_client.call_record(
                wire_record,
                mutating,
                {"name": "n", "content": "c"},
                self.repository,
            )
            self.assertFalse(mutation_result["result"]["isError"])
            result = wire_client.call_record(
                wire_record, descriptor, {"message": "over-http"}, self.repository
            )
            self.assertEqual(
                result["result"]["structuredContent"]["result"]["content"][0]["text"],
                "echo: over-http",
            )
            active_token["value"] = "rotated-wire-token"
            with self.assertRaises(McpTransportError):
                wire_client.discover("wire", self.repository)
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            self.assertFalse(thread.is_alive())

    def test_proxy_schema_change_is_detected(self) -> None:
        self._select({"echo": "allow"})
        proxy = self._proxy(["echo"])
        # Tamper the stored schema so the descriptor no longer matches.
        with self.sessions.mutate("s", "tamper") as record:
            record.mcp_servers[0]["tools"]["echo"]["schema_digest"] = "deadbeef"
        with self.assertRaisesRegex(ProxyAccessDenied, "allowed tool changed"):
            proxy.descriptors()

    def test_proxy_reloads_session_revocation(self) -> None:
        self._select({"echo": "allow"})
        proxy = self._proxy(["echo"])
        self.assertEqual([item.name for item in proxy.descriptors()], ["mcp.echo.echo"])
        with self.sessions.mutate("s", "revoke") as record:
            record.revoked = True
        with self.assertRaisesRegex(ProxyAccessDenied, "revoked"):
            proxy.descriptors()


class BridgeCliTests(unittest.TestCase):
    """Bridge profile and credential CLI surface."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.runtime_dir = self.root / "runtime"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _cli(self, *args: str) -> tuple[int, str, str]:
        env = dict(os.environ)
        env.update(
            {
                "PYTHONPATH": str(SRC),
                "KAROX_CONFIG_DIR": str(self.root / "config"),
                "KAROX_RUNTIME_DIR": str(self.runtime_dir),
            }
        )
        proc = subprocess.run(
            [sys.executable, "-m", "karox.cli", *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env,
        )
        return proc.returncode, proc.stdout, proc.stderr

    def test_bridge_list_and_show_report_honest_status(self) -> None:
        code, out, _ = self._cli("bridge", "list", "--json")
        self.assertEqual(code, 0, out)
        profiles = json.loads(out)
        names = [p["name"] for p in profiles]
        self.assertIn("notion", names)
        self.assertIn("hyperagent", names)
        self.assertIn("chatgpt-web", names)
        self.assertIn("claude-web", names)
        notion = next(p for p in profiles if p["name"] == "notion")
        self.assertEqual(notion["status"], "tested")
        promptql = next(p for p in profiles if p["name"] == "promptql")
        self.assertEqual(promptql["transport"], "openapi")
        hyperagent = next(p for p in profiles if p["name"] == "hyperagent")
        self.assertEqual(hyperagent["status"], "experimental")
        chatgpt = next(p for p in profiles if p["name"] == "chatgpt-web")
        claude = next(p for p in profiles if p["name"] == "claude-web")
        self.assertEqual(chatgpt["auth_scheme"], "oauth")
        self.assertEqual(claude["auth_scheme"], "oauth")
        self.assertTrue(chatgpt["persistent_url"])

    def test_oauth_web_profile_requires_public_https_origin(self) -> None:
        repository = self.root / "repo"
        repository.mkdir()
        code, _, err = self._cli(
            "bridge",
            "serve",
            "--repository",
            str(repository),
            "--session-id",
            "missing",
            "--profile",
            "chatgpt-web",
            "--tool",
            "karox.repo.read_file",
            "--credential",
            "missing",
        )
        self.assertEqual(code, 2)
        self.assertIn("require --public-url", err)

        code, _, err = self._cli(
            "bridge",
            "serve",
            "--repository",
            str(repository),
            "--session-id",
            "missing",
            "--profile",
            "claude-web",
            "--protocol",
            "openapi",
            "--public-url",
            "https://karox.example",
            "--tool",
            "karox.repo.read_file",
            "--credential",
            "missing",
        )
        self.assertEqual(code, 2)
        self.assertIn("require --protocol mcp", err)

    def test_bridge_show_unknown_fails(self) -> None:
        code, _, err = self._cli("bridge", "show", "nope", "--json")
        self.assertEqual(code, 2)
        self.assertIn("unknown bridge profile", err)

    def test_bridge_doctor_reports_scope(self) -> None:
        code, out, err = self._cli("bridge", "doctor", "--json")
        if code != 0:
            self.skipTest(f"OS keyring unavailable: {err.strip()}")
        info = json.loads(out)
        self.assertEqual(info["scope"], "bridge")


if __name__ == "__main__":
    unittest.main()
