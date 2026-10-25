"""MCP client security and functional contracts against real MCP servers.

The unit cases pin the configuration, credential, selection and authorization
boundaries without any subprocess.  The end-to-end cases launch the genuine
stdio and Streamable HTTP fixture servers under ``tests/`` so a regression such
as "a mutating MCP call executed without a lease" cannot pass silently again.

Everything is local: loopback HTTP only, a temporary registry per test, and an
in-memory credential backend so the developer's OS keyring is never touched.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional

import pytest
import uvicorn

from _mcp_http_server import build_asgi_app
from _mcp_support import (
    ECHO_SERVER,
    child_process_cleanup,
    read_lines,
)

from karox import cli as karox_cli
from karox.core import CoreRuntime, InvalidCommand
from karox.credentials import CredentialError
from karox.mcp_client import (
    McpAccessDenied,
    McpClient,
    McpConfigurationError,
    McpCredentialReference,
    McpCredentialStore,
    McpError,
    McpProtocolError,
    McpRegistry,
    McpRuntimeBinding,
    McpServerRecord,
    McpToolDescriptor,
    _descriptor,
    _normalize_discovered_tools,
    mcp_registry_digest,
    mcp_selection,
    validate_mcp_selection_registry,
)
from karox.models import AccessProfile, Capability, CoreCommand, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.sessions import IdempotencyConflict, SessionError, SessionStore


SECRET = "sk-abcdefghijklmnopqrstuvwxyz123456"
CREDENTIAL_SECRET = "secret-value-with-no-provider-prefix"


class _FakeCredentialBackend:
    """In-memory backend so tests never read or write the OS keyring."""

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


def _unit_record(
    server_id: str = "srv",
    namespace: str = "ns",
    **overrides: Any,
) -> McpServerRecord:
    values: dict[str, Any] = {
        "server_id": server_id,
        "namespace": namespace,
        "transport": "stdio",
        "command": "python",
    }
    values.update(overrides)
    return McpServerRecord(**values)


def _stdio_record(
    registry_root: Path,
    *,
    server_id: str = "echo",
    namespace: str = "echo",
    read_only_tools: tuple[str, ...] = ("echo", "slow_echo"),
    environment: Optional[dict[str, str]] = None,
    credential_ref: Optional[str] = None,
    credential_target: Optional[str] = None,
    timeout_seconds: float = 20.0,
) -> tuple[McpRegistry, McpServerRecord]:
    registry = McpRegistry(registry_root / "mcp-servers.json")
    record = McpServerRecord(
        server_id=server_id,
        namespace=namespace,
        transport="stdio",
        command=sys.executable,
        args=(str(ECHO_SERVER),),
        environment=environment or {},
        credential_ref=credential_ref,
        credential_target=credential_target,
        read_only_tools=read_only_tools,
        timeout_seconds=timeout_seconds,
        max_transport_retries=0,
    )
    registry.put(record)
    return registry, record


def _tool(
    name: str,
    schema: Optional[dict[str, Any]] = None,
    description: Any = "d",
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        inputSchema=schema if schema is not None else {"type": "object", "properties": {}},
        description=description,
    )


def _descriptor_for(
    record: McpServerRecord,
    name: str,
    schema: Optional[dict[str, Any]] = None,
    description: Any = "d",
) -> McpToolDescriptor:
    return _descriptor(record, _tool(name, schema, description))


def _make_repo(root: Path) -> Path:
    repository = root / "repo"
    repository.mkdir(parents=True, exist_ok=True)
    return repository


def _origin_and_policy() -> tuple[Origin, CapabilityPolicy]:
    origin = Origin(OriginKind.NATIVE_AGENT, "test")
    policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
    policy.set_grants(origin, {Capability.MCP_CALL})
    return origin, policy


def _session_with_selection(
    store: SessionStore,
    repository: Path,
    selection: Optional[dict[str, Any]],
    *,
    session_id: str = "s",
) -> Any:
    store.create(
        repository, "task", AccessProfile.WORKSPACE_WRITE, session_id=session_id
    )
    with store.mutate(session_id, "test-mcp", ttl_seconds=30.0) as record:
        record.mcp_servers = [selection] if selection is not None else []
    return store.load(session_id)


def _core(
    repository: Path,
    store: SessionStore,
    binding: McpRuntimeBinding,
    audit_path: Path,
) -> tuple[CoreRuntime, Origin]:
    origin, policy = _origin_and_policy()
    runtime = CoreRuntime(
        repository, policy, store, audit_path, mcp_binding=binding
    )
    return runtime, origin


def _text(result: dict[str, Any]) -> str:
    return result["result"]["content"][0]["text"]


@pytest.fixture(scope="module")
def stdio_server(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """One real stdio discovery shared by the reasonably expensive cases."""

    root = tmp_path_factory.mktemp("mcp-stdio")
    repository = _make_repo(root)
    calls = root / "calls.log"
    registry, record = _stdio_record(
        root, environment={"KAROX_MCP_CALL_LOG": str(calls)}
    )
    client = McpClient(registry)
    tools = client.discover_record(record, repository)
    try:
        yield SimpleNamespace(
            root=root,
            repo=repository,
            calls=calls,
            registry=registry,
            record=record,
            client=client,
            tools=tools,
            echo=next(item for item in tools if item.remote_name == "echo"),
            note=next(item for item in tools if item.remote_name == "write_note"),
        )
    finally:
        with child_process_cleanup(scripts=[ECHO_SERVER]):
            pass


@pytest.fixture(scope="module")
def http_server(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """A real Streamable HTTP MCP server on an ephemeral loopback port."""

    token = "test-bearer-secret-123456"
    previous = os.environ.get("KAROX_MCP_HTTP_TOKEN")
    os.environ["KAROX_MCP_HTTP_TOKEN"] = token
    app = build_asgi_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while (
            not server.started or not server.servers
        ) and time.time() < deadline:
            time.sleep(0.02)
        assert server.started and server.servers, "HTTP MCP fixture did not start"
        port = server.servers[0].sockets[0].getsockname()[1]
        yield SimpleNamespace(root=tmp_path_factory.mktemp("mcp-http"), port=port, token=token)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if previous is None:
            os.environ.pop("KAROX_MCP_HTTP_TOKEN", None)
        else:
            os.environ["KAROX_MCP_HTTP_TOKEN"] = previous


# ------------------------------------------------------------------ records


def test_stdio_record_requires_a_command_and_rejects_url_or_headers() -> None:
    with pytest.raises(ValueError, match="stdio MCP server requires a command"):
        McpServerRecord(server_id="s", namespace="n", transport="stdio")
    with pytest.raises(ValueError, match="cannot define a URL or headers"):
        McpServerRecord(
            server_id="s",
            namespace="n",
            transport="stdio",
            command="python",
            url="https://example.invalid/mcp",
        )
    with pytest.raises(ValueError, match="cannot define a URL or headers"):
        McpServerRecord(
            server_id="s",
            namespace="n",
            transport="stdio",
            command="python",
            headers={"X-Test": "value"},
        )


def test_http_record_requires_url_and_rejects_command_or_environment() -> None:
    with pytest.raises(ValueError, match="HTTP MCP server requires a URL"):
        McpServerRecord(server_id="s", namespace="n", transport="streamable_http")
    with pytest.raises(ValueError, match="cannot define command or environment"):
        McpServerRecord(
            server_id="s",
            namespace="n",
            transport="streamable_http",
            url="https://example.invalid/mcp",
            command="python",
        )
    with pytest.raises(ValueError, match="cannot define command or environment"):
        McpServerRecord(
            server_id="s",
            namespace="n",
            transport="streamable_http",
            url="https://example.invalid/mcp",
            environment={"MODE": "test"},
        )


def test_http_url_requires_https_unless_loopback_and_no_userinfo_query_or_fragment() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        McpServerRecord(
            server_id="s",
            namespace="n",
            transport="streamable_http",
            url="http://example.invalid/mcp",
        )
    for url, message in (
        ("https://user:pass@example.invalid/mcp", "user information"),
        ("https://example.invalid/mcp?q=1", "query or fragment"),
        ("https://example.invalid/mcp#frag", "query or fragment"),
    ):
        with pytest.raises(ValueError, match=message):
            McpServerRecord(
                server_id="s", namespace="n", transport="streamable_http", url=url
            )
    # Loopback HTTP is the only permitted cleartext form.
    McpServerRecord(
        server_id="s",
        namespace="n",
        transport="streamable_http",
        url="http://127.0.0.1:8000/mcp",
    )


def test_persisted_secret_material_is_rejected_everywhere() -> None:
    for kwargs in (
        {"transport": "stdio", "command": SECRET},
        {"transport": "stdio", "command": "python", "args": ("--token", SECRET)},
        {"transport": "streamable_http", "url": f"https://example.invalid/{SECRET}"},
    ):
        with pytest.raises(ValueError, match="credential reference"):
            McpServerRecord(server_id="s", namespace="n", **kwargs)


def test_secret_like_environment_and_headers_require_a_credential_reference() -> None:
    with pytest.raises(ValueError, match="secret-like"):
        _unit_record(environment={"API_KEY": "plain-value"})
    with pytest.raises(ValueError, match="secret-like"):
        _unit_record(environment={"PLAIN": SECRET})
    with pytest.raises(ValueError, match="secret-like"):
        _unit_record(
            transport="streamable_http",
            command=None,
            url="https://example.invalid/mcp",
            headers={"Authorization": "plain-value"},
        )
    with pytest.raises(ValueError, match="control characters"):
        _unit_record(environment={"PLAIN": "line\nbreak"})


def test_credential_reference_and_target_are_paired_and_unambiguous() -> None:
    with pytest.raises(ValueError, match="requires a credential reference"):
        _unit_record(credential_target="KAROX_VALUE")
    with pytest.raises(ValueError, match="target is required"):
        _unit_record(credential_ref="os-keyring:mcp/x")
    with pytest.raises(ValueError, match="collides with environment"):
        _unit_record(
            credential_ref="os-keyring:mcp/x",
            credential_target="KAROX_VALUE",
            environment={"KAROX_VALUE": "placeholder"},
        )
    with pytest.raises(ValueError, match="collides with headers"):
        _unit_record(
            transport="streamable_http",
            command=None,
            url="https://example.invalid/mcp",
            credential_ref="os-keyring:mcp/x",
            credential_target="X-Client",
            headers={"x-client": "placeholder"},
        )


def test_read_only_tool_names_and_server_identifiers_are_validated() -> None:
    with pytest.raises(ValueError, match="read-only tool names are invalid"):
        _unit_record(read_only_tools=("bad name!",))
    with pytest.raises(ValueError, match="read-only tool names must be unique"):
        _unit_record(read_only_tools=("a", "a"))
    for bad in ("", "1-2 3", "x" * 65):
        with pytest.raises(ValueError, match="safe characters"):
            McpServerRecord(
                server_id=bad, namespace="n", transport="stdio", command="python"
            )
        with pytest.raises(ValueError, match="safe characters"):
            McpServerRecord(
                server_id="s", namespace=bad, transport="stdio", command="python"
            )


def test_timeout_size_and_retry_limits_are_enforced() -> None:
    with pytest.raises(ValueError, match="MCP timeout"):
        _unit_record(timeout_seconds=0.0)
    with pytest.raises(ValueError, match="between 1024 and 16000000"):
        _unit_record(max_result_bytes=10)
    with pytest.raises(ValueError, match="between 1024 and 16000000"):
        _unit_record(max_message_bytes=10)
    with pytest.raises(ValueError, match="between 0 and 5"):
        _unit_record(max_transport_retries=9)


def test_record_roundtrip_rejects_unknown_and_missing_fields() -> None:
    encoded = _unit_record().to_dict()
    encoded["surprise"] = True
    with pytest.raises(ValueError, match="unknown MCP server fields"):
        McpServerRecord.from_dict(encoded)
    with pytest.raises(ValueError, match="missing MCP server fields"):
        McpServerRecord.from_dict({"server_id": "s"})
    with pytest.raises(ValueError, match="must be an object"):
        McpServerRecord.from_dict([])
    with pytest.raises(ValueError, match="must be an array"):
        McpServerRecord.from_dict(
            {"server_id": "s", "namespace": "n", "transport": "stdio", "args": "x"}
        )


# ----------------------------------------------------------------- registry


def test_registry_put_get_remove_roundtrip_is_durable(tmp_path: Path) -> None:
    path = tmp_path / "mcp-servers.json"
    registry = McpRegistry(path)
    registry.put(_unit_record("alpha", "ns-alpha"))
    reopened = McpRegistry(path)
    assert [item.server_id for item in reopened.list()] == ["alpha"]
    assert reopened.get("alpha").namespace == "ns-alpha"
    assert reopened.remove("alpha").server_id == "alpha"
    assert reopened.list() == []
    assert set(json.loads(path.read_text(encoding="utf-8"))) == {
        "schema_version",
        "servers",
    }


def test_registry_updates_same_id_but_refuses_collisions_and_corruption(
    tmp_path: Path,
) -> None:
    path = tmp_path / "mcp-servers.json"
    registry = McpRegistry(path)
    registry.put(_unit_record("s", "ns-a"))
    registry.put(_unit_record("s", "ns-b"))
    assert registry.get("s").namespace == "ns-b"
    with pytest.raises(McpConfigurationError, match="namespace is already used"):
        registry.put(_unit_record("other", "ns-b"))
    with pytest.raises(McpConfigurationError, match="does not exist"):
        registry.get("missing")

    path.write_text('{"schema_version": 99, "servers": []}', encoding="utf-8")
    with pytest.raises(McpConfigurationError, match="unsupported schema"):
        McpRegistry(path).list()

    duplicate = _unit_record().to_dict()
    path.write_text(
        json.dumps({"schema_version": 1, "servers": [duplicate, duplicate]}),
        encoding="utf-8",
    )
    with pytest.raises(McpConfigurationError, match="duplicate server IDs"):
        McpRegistry(path).list()


def test_registry_digest_binds_the_exact_configuration() -> None:
    first = _unit_record()
    same = _unit_record()
    changed = _unit_record(command="python3")
    assert mcp_registry_digest(first) == mcp_registry_digest(same)
    assert mcp_registry_digest(first) != mcp_registry_digest(changed)


# ---------------------------------------------------------------- credentials


def test_credential_store_set_resolve_delete_with_fingerprint() -> None:
    store = McpCredentialStore(backend=_FakeCredentialBackend())
    info = store.set("http-echo", "secret-value")
    assert info["reference"] == "os-keyring:mcp/http-echo"
    assert info["fingerprint"].startswith("sha256:")
    assert store.resolve(info["reference"]) == "secret-value"
    assert store.delete("http-echo")["status"] == "deleted"
    with pytest.raises(CredentialError):
        store.resolve(info["reference"])


def test_credential_store_rejects_missing_and_invalid_values() -> None:
    store = McpCredentialStore(backend=_FakeCredentialBackend())
    with pytest.raises(CredentialError):
        store.resolve("os-keyring:mcp/missing")
    for bad in ("", "has\nnewline", "x" * 65_537):
        with pytest.raises(ValueError):
            store.set("x", bad)


def test_credential_reference_rejects_foreign_prefix_and_doctor_scope() -> None:
    with pytest.raises(ValueError, match="os-keyring:mcp"):
        McpCredentialReference.parse("os-keyring:provider/x")
    store = McpCredentialStore(backend=_FakeCredentialBackend())
    assert store.doctor()["scope"] == "mcp"


# ------------------------------------------------------------ descriptor rules


def test_unsafe_remote_tool_name_is_rejected() -> None:
    with pytest.raises(McpProtocolError, match="unsafe tool name"):
        _descriptor(_unit_record(), _tool("bad name!"))


def test_non_object_input_schema_is_rejected() -> None:
    with pytest.raises(McpProtocolError, match="non-object input schema"):
        _descriptor(_unit_record(), _tool("good", {"type": "string"}))


def test_unsupported_schema_type_is_rejected() -> None:
    schema = {
        "type": "object",
        "properties": {"x": {"type": "anyOf"}},
        "additionalProperties": False,
    }
    with pytest.raises(McpProtocolError, match="unsupported types"):
        _descriptor(_unit_record(), _tool("good", schema))


def test_tool_description_is_redacted() -> None:
    descriptor = _descriptor(
        _unit_record(), _tool("good", None, f"helper {SECRET}")
    )
    assert SECRET not in descriptor.description
    assert "[REDACTED]" in descriptor.description


def test_configured_read_only_tools_must_exist_and_names_must_be_unique() -> None:
    record = _unit_record(read_only_tools=("ghost",))
    with pytest.raises(McpProtocolError, match="do not exist"):
        _normalize_discovered_tools(record, [_descriptor_for(record, "echo")])
    duplicate = _descriptor_for(_unit_record(), "echo")
    with pytest.raises(McpProtocolError, match="duplicate tool names"):
        _normalize_discovered_tools(_unit_record(), [duplicate, duplicate])


# ----------------------------------------------------------------- selection


def _selection_tools(record: McpServerRecord) -> list[McpToolDescriptor]:
    schema = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
        "additionalProperties": False,
    }
    return [
        _descriptor_for(record, "echo", schema),
        _descriptor_for(record, "write_note", {"type": "object", "properties": {}}),
    ]


def test_selection_records_permissions_digests_and_read_only_flags() -> None:
    record = _unit_record(read_only_tools=("echo",))
    tools = _selection_tools(record)
    selection = mcp_selection(
        record, tools, {"echo": "allow", "write_note": "deny"}
    )
    assert selection["server_id"] == "srv"
    assert selection["namespace"] == "ns"
    assert selection["registry_digest"] == mcp_registry_digest(record)
    assert selection["tools"]["echo"]["name"] == "mcp.ns.echo"
    assert selection["tools"]["echo"]["permission"] == "allow"
    assert selection["tools"]["write_note"]["permission"] == "deny"
    assert selection["tools"]["echo"]["read_only"] is True
    assert selection["tools"]["write_note"]["read_only"] is False


def test_selection_rejects_unknown_tools_and_non_permission_values() -> None:
    record = _unit_record()
    tools = _selection_tools(record)
    with pytest.raises(ValueError, match="unknown MCP tool permission"):
        mcp_selection(record, tools, {"nope": "allow"})
    with pytest.raises(ValueError, match="allow, ask, or deny"):
        mcp_selection(record, tools, {"echo": "maybe"})


def test_selection_defaults_to_ask() -> None:
    record = _unit_record()
    selection = mcp_selection(record, _selection_tools(record), {})
    assert selection["tools"]["echo"]["permission"] == "ask"
    assert selection["tools"]["write_note"]["permission"] == "ask"


def test_selection_inherits_allow_only_while_the_schema_digest_matches() -> None:
    record = _unit_record()
    tools = _selection_tools(record)
    prior = mcp_selection(record, tools, {"echo": "allow"})
    inherited = mcp_selection(record, tools, {}, previous=prior)
    assert inherited["tools"]["echo"]["permission"] == "allow"

    tampered = json.loads(json.dumps(prior))
    tampered["tools"]["echo"]["schema_digest"] = "deadbeef"
    reselected = mcp_selection(record, tools, {}, previous=tampered)
    assert reselected["tools"]["echo"]["permission"] == "ask"


def test_selection_invalidates_on_registry_change_and_rejects_stale_identity() -> None:
    record = _unit_record()
    tools = _selection_tools(record)
    prior = mcp_selection(record, tools, {"echo": "allow"})
    stale = dict(prior)
    stale["registry_digest"] = "different"
    assert mcp_selection(record, tools, {}, previous=stale)["tools"]["echo"][
        "permission"
    ] == "ask"

    selection = mcp_selection(record, tools, {"echo": "allow"})
    wrong_id = dict(selection)
    wrong_id["server_id"] = "other"
    with pytest.raises(McpAccessDenied, match="identity changed"):
        validate_mcp_selection_registry(record, wrong_id)
    wrong_digest = dict(selection)
    wrong_digest["registry_digest"] = "x"
    with pytest.raises(McpAccessDenied, match="configuration changed"):
        validate_mcp_selection_registry(record, wrong_digest)


# ---------------------------------------------------------- runtime binding


def _binding_setup(
    tmp_path: Path,
    *,
    selection_decisions: Optional[dict[str, str]] = None,
    selection: Optional[dict[str, Any]] = None,
) -> tuple[McpRuntimeBinding, McpServerRecord, Any, SessionStore, Path]:
    repository = _make_repo(tmp_path)
    registry = McpRegistry(tmp_path / "mcp-servers.json")
    record = _unit_record(read_only_tools=("echo",))
    registry.put(record)
    tools = _selection_tools(record)
    binding = McpRuntimeBinding(McpClient(registry), repository, tools, [record])
    if selection is None and selection_decisions is not None:
        selection = mcp_selection(record, tools, selection_decisions)
    store = SessionStore(tmp_path / "sessions")
    session = _session_with_selection(store, repository, selection)
    return binding, record, session, store, repository


def test_binding_definitions_use_mcp_capability_and_external_schema(
    tmp_path: Path,
) -> None:
    binding, _record, _session, _store, _repo = _binding_setup(
        tmp_path, selection_decisions={}
    )
    definitions = binding.definitions()
    assert [item.name for item in definitions] == ["mcp.ns.echo", "mcp.ns.write_note"]
    assert all(item.capability is Capability.MCP_CALL for item in definitions)
    assert all(item.external_schema for item in definitions)
    echo = next(item for item in definitions if item.name == "mcp.ns.echo")
    note = next(item for item in definitions if item.name == "mcp.ns.write_note")
    assert echo.mutates is False
    assert note.mutates is True


def test_binding_rejects_duplicate_namespaced_tool_names(tmp_path: Path) -> None:
    repository = _make_repo(tmp_path)
    registry = McpRegistry(tmp_path / "mcp-servers.json")
    record = _unit_record()
    registry.put(record)
    duplicate = _descriptor_for(record, "echo")
    with pytest.raises(McpProtocolError, match="namespace collision"):
        McpRuntimeBinding(
            McpClient(registry), repository, [duplicate, duplicate], [record]
        )


def test_binding_requires_a_recorded_selection(tmp_path: Path) -> None:
    binding, _record, session, _store, _repo = _binding_setup(tmp_path)
    with pytest.raises(McpAccessDenied, match="not selected"):
        binding.execute("mcp.ns.echo", {"message": "x"}, session)


def test_binding_refuses_ask_and_deny_permissions(tmp_path: Path) -> None:
    for permission, message in (
        ("ask", "explicit approval is required"),
        ("deny", "tool is denied"),
    ):
        binding, _record, session, _store, _repo = _binding_setup(
            tmp_path / permission, selection_decisions={"echo": permission}
        )
        with pytest.raises(McpAccessDenied, match=message):
            binding.execute("mcp.ns.echo", {"message": "x"}, session)


def test_binding_detects_a_tampered_schema_digest(tmp_path: Path) -> None:
    binding, record, _session, store, _repository = _binding_setup(
        tmp_path, selection_decisions={}
    )
    selection = mcp_selection(record, _selection_tools(record), {"echo": "allow"})
    selection["tools"]["echo"]["schema_digest"] = "deadbeef"
    with store.mutate("s", "tamper", ttl_seconds=30.0) as stored:
        stored.mcp_servers = [selection]
    with pytest.raises(McpAccessDenied, match="schema is not selected"):
        binding.execute("mcp.ns.echo", {"message": "x"}, store.load("s"))


def test_binding_detects_a_tampered_read_only_classification(tmp_path: Path) -> None:
    binding, record, _session, store, _repository = _binding_setup(
        tmp_path, selection_decisions={}
    )
    selection = mcp_selection(record, _selection_tools(record), {"echo": "allow"})
    selection["tools"]["echo"]["read_only"] = False
    with store.mutate("s", "tamper", ttl_seconds=30.0) as stored:
        stored.mcp_servers = [selection]
    with pytest.raises(McpAccessDenied, match="mutation classification changed"):
        binding.execute("mcp.ns.echo", {"message": "x"}, store.load("s"))


def test_binding_refuses_an_unknown_tool(tmp_path: Path) -> None:
    binding, _record, session, _store, _repo = _binding_setup(
        tmp_path, selection_decisions={"echo": "allow"}
    )
    with pytest.raises(McpAccessDenied, match="not bound"):
        binding.execute("mcp.ns.unknown", {"name": "n"}, session)


# ------------------------------------------------------- Core schema boundary


def _core_with_fake_descriptor(
    tmp_path: Path, schema: dict[str, Any]
) -> tuple[CoreRuntime, Origin, SessionStore]:
    repository = _make_repo(tmp_path)
    registry = McpRegistry(tmp_path / "mcp-servers.json")
    record = _unit_record()
    registry.put(record)
    descriptor = _descriptor_for(record, "echo", schema)
    binding = McpRuntimeBinding(McpClient(registry), repository, [descriptor], [record])
    store = SessionStore(tmp_path / "sessions")
    _session_with_selection(store, repository, None)
    runtime, origin = _core(repository, store, binding, tmp_path / "audit.jsonl")
    return runtime, origin, store


def test_core_validates_external_mcp_arguments(tmp_path: Path) -> None:
    schema = {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
        "additionalProperties": False,
    }
    runtime, origin, _store = _core_with_fake_descriptor(tmp_path, schema)
    for arguments, pattern in (
        ({"message": 123}, "must be string"),
        ({"message": "ok", "extra": "x"}, "unknown arguments"),
        ({}, "missing arguments"),
    ):
        command = CoreCommand("mcp.ns.echo", arguments, "s", origin)
        with pytest.raises(InvalidCommand, match=pattern):
            runtime.execute(command)


def test_core_rejects_unsupported_external_schema_types(tmp_path: Path) -> None:
    # ``_descriptor`` refuses this schema itself, so build the descriptor by
    # hand to prove Core's independent external-schema boundary also refuses it.
    schema = {
        "type": "object",
        "properties": {"x": {"type": "anyOf"}},
        "required": [],
        "additionalProperties": False,
    }
    repository = _make_repo(tmp_path)
    registry = McpRegistry(tmp_path / "mcp-servers.json")
    record = _unit_record()
    registry.put(record)
    descriptor = McpToolDescriptor(
        server_id="srv",
        namespace="ns",
        remote_name="echo",
        description="d",
        input_schema=schema,
        schema_digest="deadbeef",
        read_only=True,
    )
    binding = McpRuntimeBinding(McpClient(registry), repository, [descriptor], [record])
    store = SessionStore(tmp_path / "sessions")
    _session_with_selection(store, repository, None)
    runtime, origin = _core(repository, store, binding, tmp_path / "audit.jsonl")
    command = CoreCommand("mcp.ns.echo", {"x": "value"}, "s", origin)
    with pytest.raises(InvalidCommand, match="unsupported types"):
        runtime.execute(command)


# ------------------------------------------------------------- stdio end to end


def _stdio_core(
    server: Any, tmp_path: Path, decisions: dict[str, str]
) -> tuple[CoreRuntime, Origin, SessionStore, dict[str, Any]]:
    repository = _make_repo(tmp_path)
    store = SessionStore(tmp_path / "sessions")
    selection = mcp_selection(server.record, server.tools, decisions)
    _session_with_selection(store, repository, selection)
    binding = McpRuntimeBinding(
        server.client, repository, server.tools, [server.record]
    )
    runtime, origin = _core(
        repository, store, binding, tmp_path / "audit.jsonl"
    )
    return runtime, origin, store, selection


def test_stdio_discovery_classifies_read_only_tools(stdio_server: Any) -> None:
    by_name = {item.remote_name: item for item in stdio_server.tools}
    assert set(by_name) == {"echo", "slow_echo", "write_note", "reflect_secret"}
    assert by_name["echo"].read_only is True
    assert by_name["slow_echo"].read_only is True
    assert by_name["write_note"].read_only is False
    assert by_name["reflect_secret"].read_only is False
    assert by_name["echo"].name == "mcp.echo.echo"
    assert by_name["echo"].input_schema["type"] == "object"


def test_stdio_read_only_call_succeeds(stdio_server: Any, tmp_path: Path) -> None:
    runtime, origin, _store, _selection = _stdio_core(
        stdio_server, tmp_path, {"echo": "allow"}
    )
    command = CoreCommand("mcp.echo.echo", {"message": "round-trip"}, "s", origin)
    result = runtime.execute(command)
    assert result.ok is True
    assert result.data["tool"] == "mcp.echo.echo"
    assert _text(result.data) == "echo: round-trip"
    assert result.idempotent_replay is False


def test_stdio_mutating_call_requires_a_lease_and_an_idempotency_key(
    stdio_server: Any, tmp_path: Path
) -> None:
    runtime, origin, store, _selection = _stdio_core(
        stdio_server, tmp_path, {"write_note": "allow"}
    )
    keyed = CoreCommand(
        "mcp.echo.write_note",
        {"name": "n", "content": "c"},
        "s",
        origin,
        idempotency_key="lease-required",
    )
    with pytest.raises(SessionError, match="mutation lease"):
        runtime.execute(keyed)

    unkeyed = CoreCommand(
        "mcp.echo.write_note", {"name": "n", "content": "c"}, "s", origin
    )
    lease = store.acquire("s", "test-mcp", ttl_seconds=30.0)
    try:
        with pytest.raises(InvalidCommand, match="idempotency key"):
            runtime.execute(unkeyed, lease=lease)
    finally:
        store.release(lease)


def test_stdio_mutating_call_replays_without_executing_twice(
    stdio_server: Any, tmp_path: Path
) -> None:
    runtime, origin, store, _selection = _stdio_core(
        stdio_server, tmp_path, {"write_note": "allow"}
    )
    command = CoreCommand(
        "mcp.echo.write_note",
        {"name": "note", "content": "c"},
        "s",
        origin,
        idempotency_key="note-once",
    )
    before = len(read_lines(stdio_server.calls))
    lease = store.acquire("s", "test-mcp", ttl_seconds=30.0)
    try:
        first = runtime.execute(command, lease=lease)
        second = runtime.execute(command, lease=lease)
    finally:
        store.release(lease)
    assert first.ok is True
    assert first.idempotent_replay is False
    assert _text(first.data) == "noted: note"
    assert second.idempotent_replay is True
    assert second.data == first.data
    calls = read_lines(stdio_server.calls)[before:]
    assert calls.count("write_note") == 1


def test_stdio_mutating_replay_with_different_input_conflicts(
    stdio_server: Any, tmp_path: Path
) -> None:
    runtime, origin, store, _selection = _stdio_core(
        stdio_server, tmp_path, {"write_note": "allow"}
    )
    key = "conflict-key"
    first = CoreCommand(
        "mcp.echo.write_note",
        {"name": "note", "content": "c"},
        "s",
        origin,
        idempotency_key=key,
    )
    lease = store.acquire("s", "test-mcp", ttl_seconds=30.0)
    try:
        runtime.execute(first, lease=lease)
    finally:
        store.release(lease)

    different = CoreCommand(
        "mcp.echo.write_note",
        {"name": "note", "content": "different"},
        "s",
        origin,
        idempotency_key=key,
    )
    before = len(read_lines(stdio_server.calls))
    lease = store.acquire("s", "test-mcp", ttl_seconds=30.0)
    try:
        with pytest.raises(IdempotencyConflict, match="different input"):
            runtime.execute(different, lease=lease)
    finally:
        store.release(lease)
    assert len(read_lines(stdio_server.calls)) == before


def test_stdio_injected_credential_is_redacted_and_not_persisted(
    stdio_server: Any, tmp_path: Path
) -> None:
    repository = _make_repo(tmp_path)
    backend = _FakeCredentialBackend()
    credentials = McpCredentialStore(backend=backend)
    info = credentials.set("stdio-reflect", CREDENTIAL_SECRET)
    registry, record = _stdio_record(
        tmp_path,
        server_id="reflect",
        namespace="reflect",
        read_only_tools=("reflect_secret",),
        credential_ref=info["reference"],
        credential_target="KAROX_TEST_SECRET",
    )
    client = McpClient(registry, credentials)
    descriptor = next(
        item
        for item in client.discover_record(record, repository)
        if item.remote_name == "reflect_secret"
    )
    result = client.call_record(record, descriptor, {}, repository)
    encoded = json.dumps(result)
    assert CREDENTIAL_SECRET not in encoded
    assert "[REDACTED]" in encoded
    persisted = (tmp_path / "mcp-servers.json").read_text(encoding="utf-8")
    assert CREDENTIAL_SECRET not in persisted
    assert info["reference"] in persisted


def test_cli_runtime_rejects_a_tampered_selection_name(
    stdio_server: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _make_repo(tmp_path)
    store = SessionStore(tmp_path / "sessions")
    selection = mcp_selection(stdio_server.record, stdio_server.tools, {"echo": "allow"})
    selection["tools"]["echo"]["name"] = "mcp.other.echo"
    _session_with_selection(store, repository, selection, session_id="tamper")
    monkeypatch.setattr(
        karox_cli, "_mcp_registry", lambda: stdio_server.registry
    )
    with pytest.raises(McpAccessDenied, match="changed"):
        karox_cli._selected_mcp_runtime(store.load("tamper"), repository)


# -------------------------------------------------------------- HTTP end to end


def _http_record(
    root: Path,
    server: Any,
    *,
    credentials: Optional[McpCredentialStore] = None,
    credential_name: str = "http-echo",
    with_credential: bool = True,
) -> tuple[McpRegistry, McpServerRecord, McpCredentialStore]:
    credentials = credentials or McpCredentialStore(
        backend=_FakeCredentialBackend()
    )
    reference = (
        credentials.set(credential_name, server.token)["reference"]
        if with_credential
        else None
    )
    registry = McpRegistry(root / "mcp-servers.json")
    record = McpServerRecord(
        server_id="echo",
        namespace="echo",
        transport="streamable_http",
        url=f"http://127.0.0.1:{server.port}/mcp",
        credential_ref=reference,
        credential_target="Authorization" if reference else None,
        read_only_tools=("echo",),
        timeout_seconds=20.0,
        max_transport_retries=0,
    )
    registry.put(record)
    return registry, record, credentials


def test_http_discovery_and_call_use_the_injected_bearer_credential(
    http_server: Any, tmp_path: Path
) -> None:
    repository = _make_repo(tmp_path)
    registry, record, credentials = _http_record(tmp_path, http_server)
    client = McpClient(registry, credentials)
    tools = client.discover_record(record, repository)
    by_name = {item.remote_name: item for item in tools}
    assert set(by_name) == {"echo", "write_note"}
    assert by_name["echo"].read_only is True
    assert by_name["write_note"].read_only is False
    result = client.call_record(record, by_name["echo"], {"message": "http"}, repository)
    assert _text(result) == "echo: http"


def test_http_credential_is_not_persisted_in_registry_or_audit(
    http_server: Any, tmp_path: Path
) -> None:
    repository = _make_repo(tmp_path)
    registry, record, credentials = _http_record(tmp_path, http_server)
    client = McpClient(registry, credentials)
    tools = client.discover_record(record, repository)
    selection = mcp_selection(record, tools, {"echo": "allow"})
    store = SessionStore(tmp_path / "sessions")
    _session_with_selection(store, repository, selection)
    binding = McpRuntimeBinding(client, repository, tools, [record])
    runtime, origin = _core(
        repository, store, binding, tmp_path / "audit.jsonl"
    )
    result = runtime.execute(
        CoreCommand("mcp.echo.echo", {"message": "audit-trip"}, "s", origin)
    )
    assert _text(result.data) == "echo: audit-trip"
    persisted = (tmp_path / "mcp-servers.json").read_text(encoding="utf-8")
    assert http_server.token not in persisted
    audit_path = tmp_path / "audit.jsonl"
    assert audit_path.exists()
    assert http_server.token not in audit_path.read_text(encoding="utf-8")


def test_http_missing_credential_fails_closed(
    http_server: Any, tmp_path: Path
) -> None:
    repository = _make_repo(tmp_path)
    registry, record, credentials = _http_record(
        tmp_path, http_server, with_credential=False
    )
    record = McpServerRecord(
        **{
            **record.to_dict(),
            "credential_ref": "os-keyring:mcp/missing",
            "credential_target": "Authorization",
        }
    )
    registry.put(record)
    client = McpClient(registry, credentials)
    with pytest.raises(McpConfigurationError, match="cannot resolve MCP credential"):
        client.discover_record(record, repository)


def test_http_call_without_authorization_is_refused(
    http_server: Any, tmp_path: Path
) -> None:
    repository = _make_repo(tmp_path)
    registry, record, credentials = _http_record(
        tmp_path, http_server, with_credential=False
    )
    client = McpClient(registry, credentials)
    with pytest.raises(McpError):
        client.discover_record(record, repository)