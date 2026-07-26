"""Tests for the Phase 5 MCP client, registry, selection, and Core integration.

The unit tests exercise the authorization and validation boundaries without a
network or subprocess.  The end-to-end tests launch a *real* MCP server (stdio
and Streamable HTTP built on the installed ``mcp`` SDK) and drive the genuine
transport through ``McpClient`` and ``CoreRuntime``, so the happy path is
proven against the protocol, not a mock.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Optional

import uvicorn

from _support import SRC  # noqa: F401 - inserts src on sys.path
from _mcp_http_server import build_asgi_app, expected_token

from karox.core import CoreError, CoreRuntime, InvalidCommand
from karox.credentials import CredentialError
from karox.mcp_client import (
    McpAccessDenied,
    McpClient,
    McpConfigurationError,
    McpCredentialReference,
    McpCredentialStore,
    McpProtocolError,
    McpRegistry,
    McpRuntimeBinding,
    McpServerRecord,
    McpToolDescriptor,
    mcp_registry_digest,
    mcp_selection,
    validate_mcp_selection_registry,
)
from karox.models import AccessProfile, Capability, CoreCommand, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.sessions import SessionStore


class _FakeCredentialBackend:
    """In-memory backend used when the OS keyring is unavailable."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.store[(service, account)] = secret

    def get(self, service: str, account: str) -> Optional[str]:
        return self.store.get((service, account))

    def delete(self, service: str, account: str) -> None:
        if (service, account) not in self.store:
            from karox.credentials import CredentialError

            raise CredentialError("credential does not exist")
        self.store.pop((service, account), None)


def _stdio_record(
    registry_root: Path,
    *,
    server_id: str = "echo",
    namespace: str = "echo",
    read_only_tools: tuple[str, ...] = ("echo",),
) -> tuple[McpRegistry, McpServerRecord]:
    registry = McpRegistry(registry_root / "mcp-servers.json")
    record = McpServerRecord(
        server_id=server_id,
        namespace=namespace,
        transport="stdio",
        command=sys.executable,
        args=(str((Path(__file__).resolve().parent / "_mcp_echo_server.py")),),
        read_only_tools=read_only_tools,
        timeout_seconds=15.0,
    )
    registry.put(record)
    return registry, record


class McpServerRecordTests(unittest.TestCase):
    def test_stdio_requires_command_and_rejects_url_and_headers(self) -> None:
        with self.assertRaisesRegex(ValueError, "stdio MCP server requires a command"):
            McpServerRecord(server_id="s", namespace="n", transport="stdio")
        with self.assertRaisesRegex(ValueError, "cannot define a URL or headers"):
            McpServerRecord(
                server_id="s", namespace="n", transport="stdio",
                command="python", url="https://example.invalid/mcp",
            )
        with self.assertRaisesRegex(ValueError, "cannot define a URL or headers"):
            McpServerRecord(
                server_id="s", namespace="n", transport="stdio",
                command="python", headers={"X": "y"},
            )

    def test_http_requires_https_or_loopback_and_rejects_command(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires a URL"):
            McpServerRecord(server_id="s", namespace="n", transport="streamable_http")
        with self.assertRaisesRegex(ValueError, "must use HTTPS"):
            McpServerRecord(
                server_id="s", namespace="n", transport="streamable_http",
                url="http://example.invalid/mcp",
            )
        # loopback HTTP is allowed.
        McpServerRecord(
            server_id="s", namespace="n", transport="streamable_http",
            url="http://127.0.0.1:8000/mcp",
        )
        with self.assertRaisesRegex(ValueError, "cannot define command or environment"):
            McpServerRecord(
                server_id="s", namespace="n", transport="streamable_http",
                url="https://example.invalid/mcp", command="python",
            )

    def test_url_rejects_userinfo_query_and_fragment(self) -> None:
        for url in (
            "https://user:pass@example.invalid/mcp",
            "https://example.invalid/mcp?q=1",
            "https://example.invalid/mcp#frag",
        ):
            with self.assertRaisesRegex(ValueError, "must not"):
                McpServerRecord(
                    server_id="s", namespace="n", transport="streamable_http", url=url,
                )

    def test_secrets_are_rejected_in_persisted_fields(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        with self.assertRaisesRegex(ValueError, "credential reference"):
            McpServerRecord(
                server_id="s", namespace="n", transport="stdio", command=secret,
            )
        with self.assertRaisesRegex(ValueError, "credential reference"):
            McpServerRecord(
                server_id="s", namespace="n", transport="streamable_http",
                url="https://example.invalid/mcp", headers={"X": secret},
            )

    def test_secret_like_env_and_headers_require_credential_reference(self) -> None:
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        with self.assertRaisesRegex(ValueError, "secret-like"):
            McpServerRecord(
                server_id="s", namespace="n", transport="stdio",
                command="python", environment={"API_KEY": secret},
            )
        with self.assertRaisesRegex(ValueError, "secret-like"):
            McpServerRecord(
                server_id="s", namespace="n", transport="streamable_http",
                url="https://example.invalid/mcp", headers={"Authorization": secret},
            )

    def test_credential_target_requires_reference_and_vice_versa(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires a credential reference"):
            McpServerRecord(
                server_id="s", namespace="n", transport="stdio",
                command="python", credential_target="API_KEY",
            )
        with self.assertRaisesRegex(ValueError, "MCP credential target is required"):
            McpServerRecord(
                server_id="s", namespace="n", transport="stdio",
                command="python", credential_ref="os-keyring:mcp/x",
            )

    def test_read_only_tool_names_are_validated_and_unique(self) -> None:
        with self.assertRaisesRegex(ValueError, "read-only tool names are invalid"):
            McpServerRecord(
                server_id="s", namespace="n", transport="stdio",
                command="python", read_only_tools=("bad name!",),
            )
        with self.assertRaisesRegex(ValueError, "read-only tool names must be unique"):
            McpServerRecord(
                server_id="s", namespace="n", transport="stdio",
                command="python", read_only_tools=("a", "a"),
            )

    def test_size_and_timeout_limits_are_enforced(self) -> None:
        with self.assertRaisesRegex(ValueError, "MCP timeout"):
            McpServerRecord(
                server_id="s", namespace="n", transport="stdio",
                command="python", timeout_seconds=0.0,
            )
        with self.assertRaisesRegex(ValueError, "between 1024 and 16000000"):
            McpServerRecord(
                server_id="s", namespace="n", transport="stdio",
                command="python", max_result_bytes=10,
            )
        with self.assertRaisesRegex(ValueError, "between 0 and 5"):
            McpServerRecord(
                server_id="s", namespace="n", transport="stdio",
                command="python", max_transport_retries=9,
            )

    def test_unsafe_identifiers_are_rejected(self) -> None:
        for bad in ("", "1-2 3", "x" * 65):
            with self.assertRaisesRegex(ValueError, "safe characters"):
                McpServerRecord(
                    server_id=bad, namespace="n", transport="stdio", command="python",
                )


class McpRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _record(self, server_id: str = "s", namespace: str = "n") -> McpServerRecord:
        return McpServerRecord(
            server_id=server_id, namespace=namespace,
            transport="stdio", command="python",
        )

    def test_put_get_remove_roundtrip_persists_atomically(self) -> None:
        registry = McpRegistry(self.root / "mcp-servers.json")
        registry.put(self._record("alpha", "ns-alpha"))
        registry2 = McpRegistry(self.root / "mcp-servers.json")
        self.assertEqual([item.server_id for item in registry2.list()], ["alpha"])
        self.assertEqual(registry2.get("alpha").namespace, "ns-alpha")
        removed = registry2.remove("alpha")
        self.assertEqual(removed.server_id, "alpha")
        self.assertEqual(registry2.list(), [])

    def test_duplicate_server_id_updates_and_duplicate_namespace_is_rejected(self) -> None:
        registry = McpRegistry(self.root / "mcp-servers.json")
        registry.put(self._record("s", "ns-a"))
        # The same server_id updates the existing record rather than raising.
        registry.put(self._record("s", "ns-b"))
        self.assertEqual(registry.get("s").namespace, "ns-b")
        # A different server_id colliding on a namespace is rejected.
        with self.assertRaisesRegex(McpConfigurationError, "namespace is already used"):
            registry.put(self._record("other", "ns-b"))

    def test_get_missing_raises_configuration_error(self) -> None:
        registry = McpRegistry(self.root / "mcp-servers.json")
        with self.assertRaisesRegex(McpConfigurationError, "does not exist"):
            registry.get("missing")

    def test_corrupt_registry_is_rejected(self) -> None:
        path = self.root / "mcp-servers.json"
        path.write_text('{"schema_version": 99, "servers": []}', encoding="utf-8")
        registry = McpRegistry(path)
        with self.assertRaisesRegex(McpConfigurationError, "unsupported schema"):
            registry.list()

    def test_to_dict_from_dict_roundtrip_rejects_unknown_fields(self) -> None:
        record = self._record()
        encoded = record.to_dict()
        encoded["surprise"] = True
        with self.assertRaisesRegex(ValueError, "unknown MCP server fields"):
            McpServerRecord.from_dict(encoded)


class McpCredentialStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = _FakeCredentialBackend()
        self.store = McpCredentialStore(backend=self.backend)

    def test_set_resolve_delete_with_fingerprint(self) -> None:
        info = self.store.set("http-echo", "secret-value")
        self.assertTrue(info["reference"].startswith("os-keyring:mcp/"))
        self.assertTrue(info["fingerprint"].startswith("sha256:"))
        self.assertEqual(self.store.resolve(info["reference"]), "secret-value")
        self.assertEqual(self.store.delete("http-echo")["status"], "deleted")

    def test_resolve_missing_reference_fails_closed(self) -> None:
        with self.assertRaises(CredentialError):
            self.store.resolve("os-keyring:mcp/missing")

    def test_invalid_secret_values_are_rejected(self) -> None:
        for bad in ("", "has\nnewline", "x" * 65_537):
            with self.assertRaises(ValueError):
                self.store.set("x", bad)

    def test_reference_parse_rejects_foreign_prefix(self) -> None:
        with self.assertRaises(ValueError):
            McpCredentialReference.parse("os-keyring:provider/x")

    def test_doctor_reports_keyring_scope(self) -> None:
        self.assertEqual(self.store.doctor()["scope"], "mcp")


class McpSelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry, self.record = _stdio_record(self.root)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _descriptors(self) -> list[McpToolDescriptor]:
        return McpClient(self.registry).discover(self.record.server_id, self.root)

    def test_selection_records_allow_ask_deny_with_schema_digest(self) -> None:
        tools = self._descriptors()
        selection = mcp_selection(
            self.record, tools, {"echo": "allow", "write_note": "deny"},
        )
        self.assertEqual(selection["server_id"], "echo")
        self.assertEqual(selection["registry_digest"], mcp_registry_digest(self.record))
        self.assertEqual(selection["tools"]["echo"]["permission"], "allow")
        self.assertEqual(selection["tools"]["write_note"]["permission"], "deny")
        self.assertTrue(selection["tools"]["echo"]["read_only"])

    def test_unknown_tool_permission_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown MCP tool permission"):
            mcp_selection(self.record, self._descriptors(), {"nope": "allow"})

    def test_invalid_permission_value_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "allow, ask, or deny"):
            mcp_selection(self.record, self._descriptors(), {"echo": "maybe"})

    def test_default_permission_is_ask(self) -> None:
        selection = mcp_selection(self.record, self._descriptors(), {})
        self.assertEqual(selection["tools"]["echo"]["permission"], "ask")

    def test_previous_allow_is_inherited_only_when_schema_matches(self) -> None:
        tools = self._descriptors()
        prior = mcp_selection(self.record, tools, {"echo": "allow"})
        inherited = mcp_selection(self.record, tools, {}, previous=prior)
        self.assertEqual(inherited["tools"]["echo"]["permission"], "allow")
        # A tampered schema_digest breaks inheritance and falls back to ask.
        broken = dict(prior)
        broken["tools"]["echo"] = dict(broken["tools"]["echo"])
        broken["tools"]["echo"]["schema_digest"] = "deadbeef"
        reselected = mcp_selection(self.record, tools, {}, previous=broken)
        self.assertEqual(reselected["tools"]["echo"]["permission"], "ask")

    def test_registry_digest_change_invalidates_previous_inheritance(self) -> None:
        tools = self._descriptors()
        prior = mcp_selection(self.record, tools, {"echo": "allow"})
        stale = dict(prior)
        stale["registry_digest"] = "different"
        reselected = mcp_selection(self.record, tools, {}, previous=stale)
        self.assertEqual(reselected["tools"]["echo"]["permission"], "ask")

    def test_validate_selection_rejects_identity_and_config_changes(self) -> None:
        selection = mcp_selection(self.record, self._descriptors(), {"echo": "allow"})
        wrong_id = dict(selection); wrong_id["server_id"] = "other"
        with self.assertRaisesRegex(McpAccessDenied, "identity changed"):
            validate_mcp_selection_registry(self.record, wrong_id)
        wrong_digest = dict(selection); wrong_digest["registry_digest"] = "x"
        with self.assertRaisesRegex(McpAccessDenied, "configuration changed"):
            validate_mcp_selection_registry(self.record, wrong_digest)


class McpRuntimeBindingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.registry, self.record = _stdio_record(self.root)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        self.client = McpClient(self.registry)
        self.tools = self.client.discover(self.record.server_id, self.repository)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _descriptor(self, remote_name: str) -> McpToolDescriptor:
        for item in self.tools:
            if item.remote_name == remote_name:
                return item
        raise AssertionError(f"missing tool {remote_name}")

    def _binding(
        self, descriptors: list[McpToolDescriptor], *, record: Optional[McpServerRecord] = None,
    ) -> McpRuntimeBinding:
        return McpRuntimeBinding(
            self.client, self.repository, descriptors, [record or self.record],
        )

    def _session_record(self, selection: Optional[dict]) -> object:
        from karox.sessions import SessionRecord
        record = SessionRecord(
            session_id="s", repository=str(self.repository),
            repo_fingerprint="fp", branch="", access_profile="workspace_write",
            task="t", created_at=0.0, updated_at=0.0,
        )
        record.checksum = ""  # bypass for the lightweight binding test
        if selection is not None:
            record.mcp_servers = [selection]
        return record

    def test_definitions_describe_only_allowed_tools_with_mcp_capability(self) -> None:
        binding = self._binding([self._descriptor("echo")])
        defs = binding.definitions()
        self.assertEqual(len(defs), 1)
        self.assertEqual(defs[0].name, "mcp.echo.echo")
        self.assertEqual(defs[0].capability, Capability.MCP_CALL)
        self.assertTrue(defs[0].external_schema)

    def test_namespace_collision_in_descriptors_is_rejected(self) -> None:
        dup = list(self.tools) + self.tools
        with self.assertRaisesRegex(McpProtocolError, "namespace collision"):
            McpRuntimeBinding(self.client, self.repository, dup, [self.record])

    def test_execute_requires_selection(self) -> None:
        binding = self._binding([self._descriptor("echo")])
        session = self._session_record(None)
        with self.assertRaisesRegex(McpAccessDenied, "not selected"):
            binding.execute("mcp.echo.echo", {"message": "x"}, session)  # type: ignore[arg-type]

    def test_execute_requires_allow_permission(self) -> None:
        selection = mcp_selection(self.record, self.tools, {"echo": "ask"})
        binding = self._binding([self._descriptor("echo")])
        session = self._session_record(selection)
        with self.assertRaisesRegex(McpAccessDenied, "explicit approval is required"):
            binding.execute("mcp.echo.echo", {"message": "x"}, session)  # type: ignore[arg-type]

    def test_execute_deny_is_blocked(self) -> None:
        selection = mcp_selection(self.record, self.tools, {"echo": "deny"})
        binding = self._binding([self._descriptor("echo")])
        session = self._session_record(selection)
        with self.assertRaisesRegex(McpAccessDenied, "tool is denied"):
            binding.execute("mcp.echo.echo", {"message": "x"}, session)  # type: ignore[arg-type]

    def test_execute_schema_change_is_detected_at_boundary(self) -> None:
        selection = mcp_selection(self.record, self.tools, {"echo": "allow"})
        binding = self._binding([self._descriptor("echo")])
        session = self._session_record(selection)
        # Tamper the stored schema digest so it no longer matches the descriptor.
        session.mcp_servers[0]["tools"]["echo"]["schema_digest"] = "deadbeef"  # type: ignore[index]
        with self.assertRaisesRegex(McpAccessDenied, "schema is not selected"):
            binding.execute("mcp.echo.echo", {"message": "x"}, session)  # type: ignore[arg-type]


class CoreMcpIntegrationTests(unittest.TestCase):
    """External schema validation and dynamic tool wiring in CoreRuntime."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository, "task", AccessProfile.WORKSPACE_WRITE, session_id="s",
        )
        self.origin = Origin(OriginKind.NATIVE_AGENT, "test")
        self.policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        self.policy.set_grants(self.origin, {Capability.MCP_CALL})

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _fake_descriptor(self, name: str, schema: dict, *, read_only: bool) -> McpToolDescriptor:
        import hashlib
        canonical = json.dumps(
            {"name": name, "input_schema": schema},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return McpToolDescriptor(
            server_id="srv", namespace="ns", remote_name=name,
            description="d", input_schema=schema, schema_digest=digest,
            read_only=read_only,
        )

    def _core(self, binding: McpRuntimeBinding) -> CoreRuntime:
        return CoreRuntime(
            self.repository, self.policy, self.sessions,
            self.root / "audit.jsonl", mcp_binding=binding,
        )

    def test_dynamic_mcp_tools_are_registered_and_namespace_collision_is_rejected(self) -> None:
        echo = self._fake_descriptor("echo", {"type": "object", "properties": {}}, read_only=True)
        # MCP tools are always namespaced under ``mcp.<ns>.<tool>``, so they can
        # never collide with a Core tool name; the real boundary is a duplicate
        # MCP tool name within the same binding.
        binding = McpRuntimeBinding.__new__(McpRuntimeBinding)
        binding.client = None  # type: ignore[assignment]
        binding.repository = self.repository.resolve()
        binding._descriptors = {echo.name: echo}
        binding._servers = {"srv": McpServerRecord(server_id="srv", namespace="ns", transport="stdio", command="python")}
        core = self._core(binding)
        names = [item.name for item in core.tools()]
        self.assertIn("mcp.ns.echo", names)

        with self.assertRaisesRegex(McpProtocolError, "namespace collision"):
            McpRuntimeBinding(
                self.client if hasattr(self, "client") else None,  # type: ignore[arg-type]
                self.repository,
                [echo, echo],
                [McpServerRecord(server_id="srv", namespace="ns", transport="stdio", command="python")],
            )

    def test_external_schema_validation_rejects_bad_arguments(self) -> None:
        schema = {
            "type": "object",
            "properties": {"message": {"type": "string"}},
            "required": ["message"],
            "additionalProperties": False,
        }
        echo = self._fake_descriptor("echo", schema, read_only=True)
        binding = McpRuntimeBinding.__new__(McpRuntimeBinding)
        binding.client = None; binding.repository = self.repository.resolve()
        binding._descriptors = {echo.name: echo}
        binding._servers = {"srv": McpServerRecord(server_id="srv", namespace="ns", transport="stdio", command="python")}
        core = self._core(binding)
        for args, pattern in (
            ({"message": 123}, "must be string"),
            ({"message": "ok", "extra": "x"}, "unknown arguments"),
            ({}, "missing arguments"),
        ):
            command = CoreCommand("mcp.ns.echo", args, "s", self.origin)
            with self.assertRaisesRegex(InvalidCommand, pattern):
                core.execute(command)

    def test_external_schema_rejects_unsupported_types(self) -> None:
        schema = {
            "type": "object",
            "properties": {"x": {"type": "anyOf"}},
            "required": [],
            "additionalProperties": False,
        }
        bad = self._fake_descriptor("t", schema, read_only=True)
        binding = McpRuntimeBinding.__new__(McpRuntimeBinding)
        binding.client = None; binding.repository = self.repository.resolve()
        binding._descriptors = {bad.name: bad}
        binding._servers = {"srv": McpServerRecord(server_id="srv", namespace="ns", transport="stdio", command="python")}
        core = self._core(binding)
        command = CoreCommand("mcp.ns.t", {"x": "value"}, "s", self.origin)
        with self.assertRaisesRegex(InvalidCommand, "unsupported types"):
            core.execute(command)


class StdioMcpEndToEndTests(unittest.TestCase):
    """Real stdio MCP server driven through CoreRuntime with idempotency."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        self.registry, self.record = _stdio_record(self.root)
        self.client = McpClient(self.registry)
        self.tools = self.client.discover(self.record.server_id, self.repository)
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository, "task", AccessProfile.WORKSPACE_WRITE, session_id="s",
        )
        self.origin = Origin(OriginKind.NATIVE_AGENT, "test")
        self.policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        self.policy.set_grants(self.origin, {Capability.MCP_CALL})
        self._echo = next(t for t in self.tools if t.remote_name == "echo")
        self._note = next(t for t in self.tools if t.remote_name == "write_note")
        self.selection = mcp_selection(
            self.record, self.tools, {"echo": "allow", "write_note": "allow"},
        )
        with self.sessions.mutate("s", "setup") as record:
            record.mcp_servers = [self.selection]
        self.binding = McpRuntimeBinding(
            self.client, self.repository, self.tools, [self.record],
        )
        self.core = CoreRuntime(
            self.repository, self.policy, self.sessions,
            self.root / "audit.jsonl", mcp_binding=self.binding,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_read_only_echo_call_succeeds(self) -> None:
        command = CoreCommand(
            "mcp.echo.echo", {"message": "round-trip"}, "s", self.origin,
        )
        result = self.core.execute(command)
        self.assertTrue(result.ok)
        self.assertEqual(result.data["tool"], "mcp.echo.echo")
        self.assertEqual(
            result.data["result"]["content"][0]["text"], "echo: round-trip",
        )

    def test_mutating_call_requires_lease_and_idempotency_key(self) -> None:
        command = CoreCommand(
            "mcp.echo.write_note", {"name": "n", "content": "c"}, "s", self.origin,
        )
        with self.assertRaisesRegex(Exception, "idempotency key|mutation lease"):
            self.core.execute(command)

    def test_mutating_call_is_idempotently_replayed(self) -> None:
        command = CoreCommand(
            "mcp.echo.write_note", {"name": "note", "content": "c"}, "s", self.origin,
            idempotency_key="note-once",
        )
        lease = self.sessions.acquire("s", "test", ttl_seconds=30.0)
        try:
            first = self.core.execute(command, lease=lease)
            second = self.core.execute(command, lease=lease)
        finally:
            self.sessions.release(lease)
        self.assertTrue(first.ok)
        self.assertEqual(first.data["result"]["content"][0]["text"], "noted: note")
        self.assertTrue(second.idempotent_replay)

    def test_injected_generic_credential_is_redacted_from_tool_result(self) -> None:
        secret = "secret-value-with-no-provider-prefix"
        backend = _FakeCredentialBackend()
        credentials = McpCredentialStore(backend=backend)
        info = credentials.set("stdio-reflect", secret)
        record = McpServerRecord(
            server_id="reflect",
            namespace="reflect",
            transport="stdio",
            command=sys.executable,
            args=(str(Path(__file__).resolve().parent / "_mcp_echo_server.py"),),
            credential_ref=info["reference"],
            credential_target="KAROX_TEST_SECRET",
            read_only_tools=("reflect_secret",),
            timeout_seconds=15.0,
        )
        registry = McpRegistry(self.root / "reflect-registry.json")
        registry.put(record)
        client = McpClient(registry, credentials)
        descriptor = next(
            item
            for item in client.discover(record.server_id, self.repository)
            if item.remote_name == "reflect_secret"
        )
        result = client.call_record(record, descriptor, {}, self.repository)
        encoded = json.dumps(result)
        self.assertNotIn(secret, encoded)
        self.assertIn("[REDACTED]", encoded)


class HttpMcpEndToEndTests(unittest.TestCase):
    """Real Streamable HTTP MCP server with bearer auth via McpCredentialStore."""

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
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        self.repository.mkdir()
        self.backend = _FakeCredentialBackend()
        self.credentials = McpCredentialStore(backend=self.backend)
        info = self.credentials.set("http-echo", expected_token())
        self.registry = McpRegistry(self.root / "mcp-servers.json")
        self.record = McpServerRecord(
            server_id="echo", namespace="echo", transport="streamable_http",
            url=f"http://127.0.0.1:{self.port}/mcp",
            credential_ref=info["reference"], credential_target="Authorization",
            read_only_tools=("echo",), timeout_seconds=15.0,
        )
        self.registry.put(self.record)
        self.client = McpClient(self.registry, self.credentials)
        self.tools = self.client.discover(self.record.server_id, self.repository)
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository, "task", AccessProfile.WORKSPACE_WRITE, session_id="s",
        )
        self.origin = Origin(OriginKind.USER, "test")
        self.policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
        self.policy.set_grants(self.origin, {Capability.MCP_CALL})
        self.selection = mcp_selection(
            self.record, self.tools, {"echo": "allow", "write_note": "allow"},
        )
        with self.sessions.mutate("s", "setup") as record:
            record.mcp_servers = [self.selection]
        self.binding = McpRuntimeBinding(
            self.client, self.repository, self.tools, [self.record],
        )
        self.core = CoreRuntime(
            self.repository, self.policy, self.sessions,
            self.root / "audit.jsonl", mcp_binding=self.binding,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_credential_is_injected_and_call_succeeds(self) -> None:
        command = CoreCommand(
            "mcp.echo.echo", {"message": "http-trip"}, "s", self.origin,
        )
        result = self.core.execute(command)
        self.assertTrue(result.ok)
        self.assertEqual(
            result.data["result"]["content"][0]["text"], "echo: http-trip",
        )

    def test_credential_never_persisted_in_registry_or_audit(self) -> None:
        # Exercise a real call so the audit log is actually written.
        command = CoreCommand(
            "mcp.echo.echo", {"message": "audit-trip"}, "s", self.origin,
        )
        self.core.execute(command)
        token = expected_token()
        persisted = (self.root / "mcp-servers.json").read_text(encoding="utf-8")
        self.assertNotIn(token, persisted)
        audit_path = self.root / "audit.jsonl"
        self.assertTrue(audit_path.exists())
        audit = audit_path.read_text(encoding="utf-8")
        self.assertNotIn(token, audit)

    def test_missing_credential_fails_closed(self) -> None:
        registry = McpRegistry(self.root / "other-mcp.json")
        record = McpServerRecord(
            server_id="echo", namespace="echo", transport="streamable_http",
            url=f"http://127.0.0.1:{self.port}/mcp",
            credential_ref="os-keyring:mcp/missing",
            credential_target="Authorization", read_only_tools=("echo",),
        )
        registry.put(record)
        client = McpClient(registry, self.credentials)
        with self.assertRaises(Exception):
            client.discover("echo", self.repository)


class McpDescriptorValidationTests(unittest.TestCase):
    """Tool-name and schema validation performed during discovery."""

    def _record(self) -> McpServerRecord:
        return McpServerRecord(
            server_id="srv", namespace="ns", transport="stdio", command="python",
        )

    def _tool(self, name: str, schema: object = None, description: object = "d") -> object:
        class _Tool:
            pass
        tool = _Tool()
        tool.name = name
        tool.inputSchema = schema if schema is not None else {
            "type": "object", "properties": {},
        }
        tool.description = description
        return tool

    def test_unsafe_tool_name_is_rejected(self) -> None:
        from karox.mcp_client import _descriptor
        with self.assertRaisesRegex(McpProtocolError, "unsafe tool name"):
            _descriptor(self._record(), self._tool("bad name!"))

    def test_non_object_input_schema_is_rejected(self) -> None:
        from karox.mcp_client import _descriptor
        with self.assertRaisesRegex(McpProtocolError, "non-object input schema"):
            _descriptor(self._record(), self._tool("good", {"type": "string"}))

    def test_description_is_redacted_and_truncated(self) -> None:
        from karox.mcp_client import _descriptor
        secret = "sk-abcdefghijklmnopqrstuvwxyz123456"
        descriptor = _descriptor(
            self._record(), self._tool("good", {"type": "object", "properties": {}}, secret),
        )
        self.assertNotIn(secret, descriptor.description)


if __name__ == "__main__":
    unittest.main()
