"""Tests for the Phase 7 bridge profiles, credentials, and MCP proxy.

Unit tests cover the declarative profile registry, the dedicated bridge
credential store, and the proxy authorization boundaries.  The end-to-end test
drives a *real* stdio MCP server through ``McpProxy`` so the hosted-client proxy
path is proven against the genuine transport -- not a mock.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch
from pathlib import Path
from typing import Optional

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.bridge import (
    BridgeConfigurationError,
    BridgeCredentialReference,
    BridgeCredentialStore,
    BridgeProfile,
    BridgeRegistry,
    BridgeStatus,
    known_bridge_profiles,
)
from karox.cli import main
from karox.credentials import CredentialError
from karox.mcp_client import (
    McpClient,
    McpCredentialStore,
    McpRegistry,
    McpRemoteToolError,
    McpServerRecord,
    McpTransportError,
    mcp_selection,
)
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy, PolicyDenied
from karox.proxy import McpProxy, ProxyAccessDenied
from karox.proxy_server import build_proxy_asgi_app
from karox.sessions import SessionStore
from karox.web_bridge_launcher import (
    WebBridgeConnectConfig,
    WebBridgeLaunchError,
    _adopt_child,
    _child_options,
    _close_job,
    _create_child_job,
    claim_watchdog,
    reap_orphaned_web_bridges,
    write_watchdog,
)


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
        for p in profiles:
            if p.name in {"hyperagent", "promptql"}:
                self.assertEqual(p.status, BridgeStatus.EXPERIMENTAL)

    def test_notion_evidence_runs_the_legacy_gateway_not_this_runtime(self) -> None:
        """The label has to follow the evidence, not the other way round.

        Two assertions used to live here: ``notion.status == TESTED`` and, inside
        a loop, ``if p.status == TESTED: assertTrue(p.verified_versions)``. Both
        read the registry and compared it with itself, so they passed whatever
        the registry happened to say -- including a ``tested`` label whose only
        evidence exercises a different HTTP server. This reads the evidence file
        and derives what the label is allowed to be.
        """
        evidence = (
            Path(__file__).resolve().parents[1]
            / "scripts"
            / "test_notion_mcp_transport.py"
        ).read_text(encoding="utf-8")
        # It puts server/ on sys.path and drives notion_gateway; nothing in it
        # reaches src/karox, so it cannot vouch for this runtime's bridge.
        self.assertIn("import notion_gateway", evidence)
        self.assertNotIn("import karox", evidence)

        notion = BridgeRegistry().get("notion")
        self.assertEqual(notion.status, BridgeStatus.TESTED_LEGACY)
        self.assertNotEqual(notion.status, BridgeStatus.TESTED)
        # A reader of `bridge show notion` must be told where the evidence came
        # from, not just given a status word.
        self.assertTrue(
            any("legacy" in item.lower() for item in notion.limitations),
            notion.limitations,
        )
        self.assertTrue(any("legacy" in item for item in notion.verified_versions))

    def test_no_profile_claims_a_verified_run_against_this_runtime(self) -> None:
        """``tested`` is reserved for a recorded run against ``src/karox``.

        No profile has one yet. When the first real vNext end-to-end lands, this
        test is the thing that has to be updated -- deliberately, in that commit.
        """
        for p in known_bridge_profiles():
            self.assertNotEqual(p.status, BridgeStatus.TESTED, p.name)

    def test_tested_status_requires_verified_versions(self) -> None:
        # Both evidence-backed labels have to name what they were verified
        # against; a legacy claim with no version is as empty as a tested one.
        for status in (BridgeStatus.TESTED, BridgeStatus.TESTED_LEGACY):
            with self.assertRaisesRegex(ValueError, "require verified versions"):
                BridgeProfile(
                    name="x", transport="streamable_http", status=status,
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
            # A mutating call without a client-supplied idempotency key fails
            # closed. Generating one per attempt would make a retried mutation
            # run a second time, which is exactly what the key exists to stop.
            with self.assertRaises(McpRemoteToolError):
                wire_client.call_record(
                    wire_record,
                    mutating,
                    {"name": "n", "content": "c"},
                    self.repository,
                )
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


class WebBridgeOrphanTests(unittest.TestCase):
    """The public tunnel must not survive a hard kill of the launcher."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _dead_pid(self) -> int:
        finished = subprocess.Popen([sys.executable, "-c", "pass"])
        finished.wait(timeout=30)
        return finished.pid

    def test_default_access_profile_cannot_write(self) -> None:
        config = WebBridgeConnectConfig(
            profile="chatgpt-web", repository=Path("repo")
        )
        self.assertEqual(config.access_profile, AccessProfile.READ_ONLY)

    def test_children_die_with_the_launcher_instead_of_leaking(self) -> None:
        job = _create_child_job()
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            **_child_options(),
        )
        try:
            if os.name == "nt":
                self.assertIsNotNone(job)
                self.assertTrue(_adopt_child(job, child))
                # Closing the last job handle is what a hard kill of the
                # launcher does implicitly, so this is the orphan scenario.
                _close_job(job)
                # wait() would raise TimeoutExpired if the child were still
                # sleeping out its full minute, which is the leak being closed.
                child.wait(timeout=30)
                self.assertIsNotNone(child.poll())
            else:
                self.assertIsNone(job)
                self.assertEqual(os.getpgid(child.pid), child.pid)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=30)

    def test_watchdog_makes_an_orphan_detectable_and_reapable(self) -> None:
        credentials = MagicMock()
        sessions = MagicMock()
        orphan = self.root / "web-bridge" / "orphan.json"
        live = self.root / "web-bridge" / "live.json"
        with patch.dict(os.environ, {"KAROX_RUNTIME_DIR": str(self.root)}):
            write_watchdog(
                orphan,
                {"session_id": "orphan", "owner_pid": self._dead_pid()},
            )
            write_watchdog(
                live,
                {"session_id": "live", "owner_pid": os.getpid()},
            )
            with (
                patch(
                    "karox.web_bridge_launcher.BridgeCredentialStore",
                    return_value=credentials,
                ),
                patch(
                    "karox.web_bridge_launcher.SessionStore",
                    return_value=sessions,
                ),
            ):
                reaped = reap_orphaned_web_bridges()
        self.assertEqual(reaped, ("orphan",))
        credentials.delete.assert_called_once_with("orphan")
        sessions.revoke.assert_called_once_with("orphan")
        self.assertFalse(orphan.exists())
        self.assertTrue(live.exists())

    def test_a_second_bridge_cannot_take_over_a_live_session_record(self) -> None:
        """The record decides ownership, so the loser must not touch it.

        The launcher wrote its record before the session store -- the only thing
        enforcing session-id uniqueness -- had a chance to refuse, and deleted it
        unconditionally on the way out. A second `bridge connect --session-id X`
        therefore overwrote the live bridge's record with its own pid and then
        removed it, leaving a running public tunnel that nothing on disk could
        find.
        """
        record = self.root / "web-bridge" / "shared.json"
        with patch.dict(os.environ, {"KAROX_RUNTIME_DIR": str(self.root)}):
            claim_watchdog(
                record,
                {"session_id": "shared", "owner_pid": os.getpid(), "port": 8765},
            )
            original = record.read_text(encoding="utf-8")

            with self.assertRaisesRegex(WebBridgeLaunchError, "already serving this session"):
                claim_watchdog(
                    record,
                    {"session_id": "shared", "owner_pid": os.getpid() + 1, "port": 9999},
                )

        # Untouched: not rewritten with the loser's details, and not deleted.
        self.assertTrue(record.exists())
        self.assertEqual(record.read_text(encoding="utf-8"), original)

    def test_a_stale_record_is_kept_so_doctor_can_still_reap_it(self) -> None:
        """Overwriting a dead owner's record would strand its session.

        The record is what `bridge doctor` needs in order to revoke the orphan's
        credential and session, so a claim refuses rather than replacing it, and
        says which command clears it.
        """
        record = self.root / "web-bridge" / "stale.json"
        with patch.dict(os.environ, {"KAROX_RUNTIME_DIR": str(self.root)}):
            claim_watchdog(
                record,
                {"session_id": "stale", "owner_pid": self._dead_pid()},
            )
            original = record.read_text(encoding="utf-8")

            with self.assertRaisesRegex(WebBridgeLaunchError, "karox bridge doctor"):
                claim_watchdog(
                    record,
                    {"session_id": "stale", "owner_pid": os.getpid()},
                )

        self.assertEqual(record.read_text(encoding="utf-8"), original)

    def test_bridge_doctor_reaps_without_starting_another_bridge(self) -> None:
        credentials = MagicMock()
        sessions = MagicMock()
        orphan = self.root / "web-bridge" / "orphan.json"
        printed = io.StringIO()
        with patch.dict(os.environ, {"KAROX_RUNTIME_DIR": str(self.root)}):
            write_watchdog(
                orphan,
                {"session_id": "orphan", "owner_pid": self._dead_pid()},
            )
            with (
                patch(
                    "karox.web_bridge_launcher.BridgeCredentialStore",
                    return_value=credentials,
                ),
                patch(
                    "karox.web_bridge_launcher.SessionStore",
                    return_value=sessions,
                ),
                patch(
                    "karox.cli.BridgeCredentialStore",
                    return_value=MagicMock(
                        doctor=lambda: {"status": "ok", "scope": "bridge"}
                    ),
                ),
                redirect_stdout(printed),
            ):
                code = main(("bridge", "doctor", "--json"))
        self.assertEqual(code, 0)
        # Reaping only on the next connect meant an orphaned public tunnel stayed
        # up until someone happened to start another bridge.
        self.assertEqual(
            json.loads(printed.getvalue())["reaped_web_bridge_sessions"], ["orphan"]
        )
        sessions.revoke.assert_called_once_with("orphan")
        self.assertFalse(orphan.exists())


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
        self.assertEqual(notion["status"], "tested_legacy")
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

    def test_non_loopback_bind_is_refused_without_tls(self) -> None:
        repository = self.root / "repo"
        repository.mkdir(exist_ok=True)
        arguments = (
            "bridge",
            "serve",
            "--repository",
            str(repository),
            "--session-id",
            "missing",
            "--profile",
            "promptql",
            "--protocol",
            "openapi",
            "--tool",
            "karox.repo.read_file",
            "--credential",
            "missing",
            "--host",
            "0.0.0.0",
        )
        code, _, err = self._cli(*arguments, "--allow-network-bind")
        self.assertEqual(code, 2)
        self.assertIn("requires TLS", err)

        code, _, err = self._cli(
            *arguments, "--allow-network-bind", "--tls-certfile", "cert.pem"
        )
        self.assertEqual(code, 2)
        self.assertIn("both --tls-certfile and --tls-keyfile", err)

        code, _, err = self._cli(
            *arguments,
            "--allow-network-bind",
            "--tls-certfile",
            str(repository / "absent-cert.pem"),
            "--tls-keyfile",
            str(repository / "absent-key.pem"),
        )
        self.assertEqual(code, 2)
        self.assertIn("must point to an existing PEM file", err)

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
