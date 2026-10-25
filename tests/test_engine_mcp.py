"""MCP behind the karo-core engine loop: the kernel-integrator adapter.

These tests keep the release MCP surface callable from the engine-backed tool
architecture. The engine executes one bash command per step through
``CoreEngineConfig.command_runner``; the adapter exposes the selected MCP
tools as (a) a typed Python API and (b) an in-band ``mcp`` command the
engine's agent runs like any shell command, routed through that same runner
seam.

Every server here is the real fixture stdio/HTTP MCP server from ``tests/``:
the JSON-RPC handshake, discovery, schema re-validation, allow/ask/deny
selection, mutation lease and idempotent replay all run on the genuine wire.
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
from _mcp_support import ECHO_SERVER, child_process_cleanup, read_lines

from karox.core_engine import CoreEngine, CoreEngineConfig
from karox.credentials import CredentialError
from karox.engine_mcp import (
    EngineMcpAdapter,
    EngineMcpCallResult,
    McpProgressEvent,
    build_engine_command_runner,
)
from karox.mcp_client import (
    McpAccessDenied,
    McpClient,
    McpCredentialStore,
    McpError,
    McpRegistry,
    McpServerRecord,
    mcp_selection,
)
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.sessions import IdempotencyConflict, SessionError, SessionStore


# The engine loop needs the exact observation contract of a command runner.
def _text(result: Any) -> str:
    return result.result["content"][0]["text"]


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


class FakeModel:
    """Replays scripted engine messages; records the history it was shown."""

    def __init__(self, messages: list[dict]):
        self._messages = messages
        self.seen: list[list[dict]] = []

    def query(self, messages: list[dict], **kwargs) -> dict:
        self.seen.append([dict(m) for m in messages])
        index = min(len(self.seen) - 1, len(self._messages) - 1)
        return self._messages[index]

    def get_template_vars(self, **kwargs):
        return {}

    def format_message(self, **kwargs):
        return {"role": kwargs["role"], "content": kwargs["content"]}

    def format_observation_messages(self, message, outputs, template_vars=None):
        return [{"role": "user", "content": str(o.get("output", ""))} for o in outputs]

    def serialize(self):
        return {}


def action(command: str) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "extra": {"actions": [{"command": command}], "cost": 0.0},
    }


def _make_repo(root: Path) -> Path:
    repository = root / "repo"
    repository.mkdir(parents=True, exist_ok=True)
    return repository


def _stdio_record(
    registry_root: Path,
    *,
    server_id: str = "echo",
    namespace: str = "echo",
    environment: Optional[dict[str, str]] = None,
) -> McpServerRecord:
    record = McpServerRecord(
        server_id=server_id,
        namespace=namespace,
        transport="stdio",
        command=sys.executable,
        args=(str(ECHO_SERVER),),
        environment=environment or {},
        read_only_tools=("echo", "slow_echo"),
        timeout_seconds=20.0,
        max_transport_retries=0,
    )
    McpRegistry(registry_root / "mcp-servers.json").put(record)
    return record


def _origin_and_policy() -> tuple[Origin, CapabilityPolicy]:
    origin = Origin(OriginKind.NATIVE_AGENT, "engine-test")
    policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
    policy.set_grants(origin, {Capability.MCP_CALL})
    return origin, policy


def _session_with_selection(
    store: SessionStore, repository: Path, selection: Optional[dict[str, Any]]
) -> None:
    store.create(repository, "task", AccessProfile.WORKSPACE_WRITE, session_id="s")
    with store.mutate("s", "test-engine-mcp", ttl_seconds=30.0) as record:
        record.mcp_servers = [selection] if selection is not None else []


@pytest.fixture(scope="module")
def stdio_server(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """One real stdio server, discovered once, shared by the adapter tests."""

    root = tmp_path_factory.mktemp("engine-mcp-stdio")
    repository = _make_repo(root)
    calls = root / "calls.log"
    record = _stdio_record(root, environment={"KAROX_MCP_CALL_LOG": str(calls)})
    registry = McpRegistry(root / "mcp-servers.json")
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
        )
    finally:
        with child_process_cleanup(scripts=[ECHO_SERVER]):
            pass


@pytest.fixture
def adapter_env(stdio_server: Any, tmp_path: Path) -> Any:
    """A session-bound adapter with echo, slow_echo and write_note allowed.

    The registry file is per-test (a copy of the fixture record) so the
    tamper tests can mutate it without poisoning the module-scoped server.
    The calls log is shared with the module fixture, so call-count
    assertions must snapshot the log before acting.
    """

    store = SessionStore(tmp_path / "sessions")
    registry = McpRegistry(tmp_path / "mcp-servers.json")
    registry.put(stdio_server.record)
    selection = mcp_selection(
        stdio_server.record,
        stdio_server.tools,
        {"echo": "allow", "slow_echo": "allow", "write_note": "allow"},
    )
    _session_with_selection(store, stdio_server.repo, selection)
    origin, policy = _origin_and_policy()
    adapter = EngineMcpAdapter.from_session(
        record=store.load("s"),
        repository=stdio_server.repo,
        store=store,
        registry=registry,
        origin=origin,
        policy=policy,
        audit_path=tmp_path / "audit.jsonl",
    )
    return SimpleNamespace(
        adapter=adapter,
        store=store,
        audit=tmp_path / "audit.jsonl",
        calls=stdio_server.calls,
        repo=stdio_server.repo,
        registry=registry,
        record=stdio_server.record,
        tools=stdio_server.tools,
    )


# ------------------------------------------------------------- construction


def test_from_session_binds_only_allowed_tools(stdio_server: Any, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    selection = mcp_selection(
        stdio_server.record,
        stdio_server.tools,
        {"echo": "allow", "slow_echo": "ask", "write_note": "deny"},
    )
    _session_with_selection(store, stdio_server.repo, selection)
    origin, policy = _origin_and_policy()
    adapter = EngineMcpAdapter.from_session(
        record=store.load("s"),
        repository=stdio_server.repo,
        store=store,
        registry=McpRegistry(stdio_server.registry.path),
        origin=origin,
        policy=policy,
    )
    tools = adapter.tools()
    assert [item.name for item in tools] == ["mcp.echo.echo"]
    assert tools[0].mutates is False
    assert tools[0].remote_name == "echo"
    assert tools[0].input_schema["type"] == "object"
    with pytest.raises(McpAccessDenied, match="not bound"):
        adapter.call_tool("mcp.echo.slow_echo", {"message": "x"})
    with pytest.raises(McpAccessDenied, match="not bound"):
        adapter.call_tool("mcp.echo.write_note", {"name": "n", "content": "c"})


def test_from_session_rejects_a_stale_selection(stdio_server: Any, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    registry = McpRegistry(tmp_path / "mcp-servers.json")
    registry.put(stdio_server.record)
    selection = mcp_selection(
        stdio_server.record, stdio_server.tools, {"echo": "allow"}
    )
    _session_with_selection(store, stdio_server.repo, selection)
    # Change the registry after the selection was recorded: the digest no
    # longer matches, so the adapter must refuse to bind anything.
    changed = McpServerRecord(
        **{**stdio_server.record.to_dict(), "timeout_seconds": 25.0}
    )
    registry.put(changed)
    origin, policy = _origin_and_policy()
    with pytest.raises(McpAccessDenied, match="reselect"):
        EngineMcpAdapter.from_session(
            record=store.load("s"),
            repository=stdio_server.repo,
            store=store,
            registry=registry,
            origin=origin,
            policy=policy,
        )


def test_empty_selection_yields_a_fail_closed_adapter(stdio_server: Any, tmp_path: Path) -> None:
    store = SessionStore(tmp_path / "sessions")
    _session_with_selection(store, stdio_server.repo, None)
    origin, policy = _origin_and_policy()
    adapter = EngineMcpAdapter.from_session(
        record=store.load("s"),
        repository=stdio_server.repo,
        store=store,
        registry=McpRegistry(stdio_server.registry.path),
        origin=origin,
        policy=policy,
    )
    assert adapter.tools() == []
    observation = adapter.handle_raw("mcp call mcp.echo.echo '{}'", 300)
    assert observation is not None
    assert observation["returncode"] != 0
    assert "not bound" in observation["output"]


# ------------------------------------------------------------- typed calls


def test_read_only_call_returns_a_typed_result(adapter_env: Any) -> None:
    result = adapter_env.adapter.call_tool("mcp.echo.echo", {"message": "typed"})
    assert isinstance(result, EngineMcpCallResult)
    assert result.ok is True
    assert result.tool == "mcp.echo.echo"
    assert result.server_id == "echo"
    assert _text(result) == "echo: typed"
    assert result.bytes > 0
    assert result.duration_ms >= 0.0
    assert result.idempotent_replay is False
    assert result.to_dict()["tool"] == "mcp.echo.echo"
    # Read-only calls never take the mutation lease.
    assert not adapter_env.store.lease_path("s").exists()


def test_progress_events_reach_the_caller(adapter_env: Any) -> None:
    events: list[McpProgressEvent] = []
    result = adapter_env.adapter.call_tool(
        "mcp.echo.slow_echo", {"message": "p"}, progress=events.append
    )
    assert _text(result) == "echo: p"
    assert [event.progress for event in events] == [1.0, 2.0]
    assert all(event.total == 2.0 for event in events)
    assert all(event.tool == "mcp.echo.slow_echo" for event in events)
    assert events[0].message == "step 1.0/2.0"


# ------------------------------------------------------- mutation contracts


def test_mutating_call_requires_an_idempotency_key(adapter_env: Any) -> None:
    before = len(read_lines(adapter_env.calls))
    with pytest.raises(McpError, match="idempotency key"):
        adapter_env.adapter.call_tool(
            "mcp.echo.write_note", {"name": "n", "content": "c"}
        )
    assert "write_note" not in read_lines(adapter_env.calls)[before:]


def test_mutating_call_takes_and_releases_a_lease(adapter_env: Any) -> None:
    before = len(read_lines(adapter_env.calls))
    result = adapter_env.adapter.call_tool(
        "mcp.echo.write_note",
        {"name": "n1", "content": "c1"},
        idempotency_key="note-1",
    )
    assert result.ok is True
    assert _text(result) == "noted: n1"
    assert not adapter_env.store.lease_path("s").exists()
    entry = adapter_env.store.load("s").idempotency.get("note-1")
    assert entry is not None and entry["status"] == "complete"
    assert read_lines(adapter_env.calls)[before:].count("write_note") == 1


def test_mutating_call_replays_without_executing_twice(adapter_env: Any) -> None:
    before = len(read_lines(adapter_env.calls))
    first = adapter_env.adapter.call_tool(
        "mcp.echo.write_note",
        {"name": "n1", "content": "c1"},
        idempotency_key="note-1",
    )
    second = adapter_env.adapter.call_tool(
        "mcp.echo.write_note",
        {"name": "n1", "content": "c1"},
        idempotency_key="note-1",
    )
    assert first.idempotent_replay is False
    assert second.idempotent_replay is True
    assert second.result == first.result
    assert read_lines(adapter_env.calls)[before:].count("write_note") == 1


def test_replay_with_different_input_conflicts(adapter_env: Any) -> None:
    adapter_env.adapter.call_tool(
        "mcp.echo.write_note",
        {"name": "n1", "content": "c1"},
        idempotency_key="note-1",
    )
    with pytest.raises(IdempotencyConflict):
        adapter_env.adapter.call_tool(
            "mcp.echo.write_note",
            {"name": "n2", "content": "c2"},
            idempotency_key="note-1",
        )


def test_failed_mutation_requires_reconciliation(adapter_env: Any) -> None:
    with pytest.raises(McpError, match="error result"):
        adapter_env.adapter.call_tool(
            "mcp.echo.write_note",
            {"name": "boom", "content": "c"},
            idempotency_key="note-boom",
        )
    entry = adapter_env.store.load("s").idempotency.get("note-boom")
    assert entry is not None and entry["status"] == "pending"
    with pytest.raises(IdempotencyConflict, match="reconciliation"):
        adapter_env.adapter.call_tool(
            "mcp.echo.write_note",
            {"name": "boom", "content": "c"},
            idempotency_key="note-boom",
        )


# ------------------------------------------------------- tamper resistance


def test_tampered_schema_digest_is_refused_at_call_time(adapter_env: Any) -> None:
    selection = adapter_env.store.load("s").mcp_servers[0]
    selection["tools"]["echo"]["schema_digest"] = "deadbeef"
    with adapter_env.store.mutate("s", "tamper", ttl_seconds=30.0) as stored:
        stored.mcp_servers = [selection]
    with pytest.raises(McpAccessDenied, match="schema is not selected"):
        adapter_env.adapter.call_tool("mcp.echo.echo", {"message": "x"})


def test_tampered_registry_is_refused_at_call_time(adapter_env: Any) -> None:
    changed = McpServerRecord(
        **{**adapter_env.record.to_dict(), "timeout_seconds": 25.0}
    )
    adapter_env.registry.put(changed)
    with pytest.raises(McpAccessDenied, match="reselect"):
        adapter_env.adapter.call_tool("mcp.echo.echo", {"message": "x"})


def test_tampered_permission_is_refused_at_call_time(adapter_env: Any) -> None:
    selection = adapter_env.store.load("s").mcp_servers[0]
    selection["tools"]["echo"]["permission"] = "ask"
    with adapter_env.store.mutate("s", "tamper", ttl_seconds=30.0) as stored:
        stored.mcp_servers = [selection]
    with pytest.raises(McpAccessDenied, match="explicit approval"):
        adapter_env.adapter.call_tool("mcp.echo.echo", {"message": "x"})


def test_revoked_session_is_refused(adapter_env: Any) -> None:
    with adapter_env.store.mutate("s", "revoke", ttl_seconds=30.0) as stored:
        stored.revoked = True
    with pytest.raises(SessionError, match="revoked"):
        adapter_env.adapter.call_tool("mcp.echo.echo", {"message": "x"})


# ------------------------------------------------------ audit observability


def test_audit_rows_record_the_call(adapter_env: Any) -> None:
    adapter_env.adapter.call_tool("mcp.echo.echo", {"message": "audited"})
    rows = [json.loads(line) for line in read_lines(adapter_env.audit)]
    events = [row["event"] for row in rows]
    assert "engine-mcp.command.started" in events
    assert "engine-mcp.command.completed" in events
    completed = next(row for row in rows if row["event"] == "engine-mcp.command.completed")
    assert completed["tool"] == "mcp.echo.echo"
    assert completed["origin"] == "native_agent:engine-test"
    assert completed["session_id"] == "s"


# ------------------------------------------------------- in-band commands


def test_handle_raw_routes_only_mcp_commands(adapter_env: Any) -> None:
    assert adapter_env.adapter.handle_raw("echo not-mcp", 300) is None
    assert adapter_env.adapter.handle_raw("git status", 300) is None
    observation = adapter_env.adapter.handle_raw("mcp list", 300)
    assert observation is not None
    assert observation["returncode"] == 0
    assert "mcp.echo.echo" in observation["output"]
    assert "mcp.echo.write_note" in observation["output"]


def test_command_list_output_marks_mutating_tools(adapter_env: Any) -> None:
    output = adapter_env.adapter.handle_command(["mcp", "list"], 300)
    assert output["returncode"] == 0
    assert "read-only" in output["output"]
    assert "mutating" in output["output"]


def test_command_call_returns_observation_json(adapter_env: Any) -> None:
    observation = adapter_env.adapter.handle_raw(
        'mcp call mcp.echo.echo \'{"message": "wire"}\'', 300
    )
    assert observation is not None
    assert observation["returncode"] == 0
    assert "echo: wire" in observation["output"]
    payload = observation["output"].splitlines()[-1]
    assert json.loads(payload)["content"][0]["text"] == "echo: wire"


def test_command_mutating_call_needs_the_key_flag(adapter_env: Any) -> None:
    before = len(read_lines(adapter_env.calls))
    observation = adapter_env.adapter.handle_raw(
        'mcp call mcp.echo.write_note \'{"name": "n", "content": "c"}\'', 300
    )
    assert observation is not None
    assert observation["returncode"] != 0
    assert "idempotency" in observation["output"]
    assert "write_note" not in read_lines(adapter_env.calls)[before:]


def test_command_mutating_call_replays(adapter_env: Any) -> None:
    before = len(read_lines(adapter_env.calls))
    first = adapter_env.adapter.handle_raw(
        'mcp call mcp.echo.write_note \'{"name": "n1", "content": "c"}\' '
        "--idempotency-key cmd-note",
        300,
    )
    second = adapter_env.adapter.handle_raw(
        'mcp call mcp.echo.write_note \'{"name": "n1", "content": "c"}\' '
        "--idempotency-key cmd-note",
        300,
    )
    assert first["returncode"] == 0
    assert second["returncode"] == 0
    assert "replay" in second["output"]
    assert read_lines(adapter_env.calls)[before:].count("write_note") == 1


def test_command_usage_errors_are_rejected(adapter_env: Any) -> None:
    cases = [
        "mcp bogus",
        "mcp call",
        "mcp call mcp.echo.echo",
        "mcp call mcp.echo.echo '{bad json'",
        "mcp call mcp.echo.echo '[1, 2]'",
        'mcp call mcp.echo.echo \'{"message": NaN}\'',
        'mcp call mcp.echo.echo \'{"message": "x"}\' --idempotency-key',
    ]
    for command in cases:
        observation = adapter_env.adapter.handle_raw(command, 300)
        assert observation is not None, command
        assert observation["returncode"] == 2, (command, observation)
        assert observation["output"].startswith("usage: mcp")


def test_command_failure_output_is_typed_not_a_traceback(adapter_env: Any) -> None:
    observation = adapter_env.adapter.handle_raw(
        'mcp call mcp.echo.write_note \'{"name": "boom", "content": "c"}\' '
        "--idempotency-key boom-1",
        300,
    )
    assert observation is not None
    assert observation["returncode"] == 1
    assert "mcp error" in observation["output"]
    assert "Traceback" not in observation["output"]


def test_prompt_block_documents_the_protocol(adapter_env: Any) -> None:
    block = adapter_env.adapter.prompt_block()
    assert "mcp list" in block
    assert "mcp call" in block
    assert "mcp.echo.echo" in block
    assert "idempotency-key" in block


# ------------------------------------------------------ engine integration


def _engine(tmp_path: Path, model: FakeModel, runner: Any) -> CoreEngine:
    config = CoreEngineConfig(
        repository=tmp_path,
        task="use the mcp tools",
        model="deepseek-ai/DeepSeek-V4.1-Flash",
        max_steps=12,
        max_seconds=120,
        trajectory_path=tmp_path / "trajectory.json",
        model_factory=lambda c: model,
        command_runner=runner,
    )
    return CoreEngine(config)


def _history(model: FakeModel) -> str:
    return "\n".join(message["content"] for message in model.seen[-1])


def test_engine_agent_calls_an_mcp_tool_over_the_real_wire(
    adapter_env: Any, tmp_path: Path
) -> None:
    fallback_calls: list[str] = []

    def fallback(command: str, cwd: Path, timeout: int) -> dict:
        fallback_calls.append(command)
        return {"output": "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n", "returncode": 0}

    runner = build_engine_command_runner(adapter_env.adapter, fallback)
    model = FakeModel(
        [
            action("mcp list"),
            action('mcp call mcp.echo.echo \'{"message": "from-engine"}\''),
            action("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
        ]
    )
    report = _engine(tmp_path, model, runner).run()
    assert report.status == "submitted", report.status
    history = _history(model)
    assert "mcp.echo.echo" in history
    assert "echo: from-engine" in history
    assert fallback_calls == ["echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]


def test_engine_agent_mutating_call_replays(adapter_env: Any, tmp_path: Path) -> None:
    before = len(read_lines(adapter_env.calls))

    def fallback(command: str, cwd: Path, timeout: int) -> dict:
        return {"output": "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n", "returncode": 0}

    runner = build_engine_command_runner(adapter_env.adapter, fallback)
    model = FakeModel(
        [
            action(
                'mcp call mcp.echo.write_note \'{"name": "n1", "content": "c"}\' '
                "--idempotency-key engine-note"
            ),
            action(
                'mcp call mcp.echo.write_note \'{"name": "n1", "content": "c"}\' '
                "--idempotency-key engine-note"
            ),
            action("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
        ]
    )
    report = _engine(tmp_path, model, runner).run()
    assert report.status == "submitted", report.status
    assert read_lines(adapter_env.calls)[before:].count("write_note") == 1
    assert "replay" in _history(model)


def test_engine_agent_sees_the_failure_not_a_crash(adapter_env: Any, tmp_path: Path) -> None:
    before = len(read_lines(adapter_env.calls))

    def fallback(command: str, cwd: Path, timeout: int) -> dict:
        return {"output": "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n", "returncode": 0}

    runner = build_engine_command_runner(adapter_env.adapter, fallback)
    model = FakeModel(
        [
            action('mcp call mcp.echo.write_note \'{"name": "boom", "content": "c"}\''),
            action("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
        ]
    )
    report = _engine(tmp_path, model, runner).run()
    assert report.status == "submitted", report.status
    assert "idempotency" in _history(model)
    assert "write_note" not in read_lines(adapter_env.calls)[before:]


# ------------------------------------------------------- HTTP transport


@pytest.fixture(scope="module")
def http_server(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """A real Streamable HTTP MCP server on an ephemeral loopback port."""

    token = "engine-mcp-bearer-123456"
    previous = os.environ.get("KAROX_MCP_HTTP_TOKEN")
    os.environ["KAROX_MCP_HTTP_TOKEN"] = token
    app = build_asgi_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 15
        while (not server.started or not server.servers) and time.time() < deadline:
            time.sleep(0.02)
        assert server.started and server.servers, "HTTP MCP fixture did not start"
        port = server.servers[0].sockets[0].getsockname()[1]
        yield SimpleNamespace(root=tmp_path_factory.mktemp("engine-mcp-http"), port=port, token=token)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        if previous is None:
            os.environ.pop("KAROX_MCP_HTTP_TOKEN", None)
        else:
            os.environ["KAROX_MCP_HTTP_TOKEN"] = previous


def test_http_transport_call_through_the_adapter(http_server: Any, tmp_path: Path) -> None:
    repository = _make_repo(tmp_path)
    registry = McpRegistry(tmp_path / "mcp-servers.json")
    backend = _FakeCredentialBackend()
    credentials = McpCredentialStore(backend)
    record = McpServerRecord(
        server_id="http-echo",
        namespace="http",
        transport="streamable_http",
        url=f"http://127.0.0.1:{http_server.port}/mcp",
        credential_ref="os-keyring:mcp/http-token",
        credential_target="Authorization",
        read_only_tools=("echo",),
        timeout_seconds=20.0,
        max_transport_retries=0,
    )
    registry.put(record)
    credentials.set("http-token", http_server.token)
    client = McpClient(registry, credentials)
    tools = client.discover_record(record, repository)
    selection = mcp_selection(record, tools, {"echo": "allow"})
    store = SessionStore(tmp_path / "sessions")
    _session_with_selection(store, repository, selection)
    origin, policy = _origin_and_policy()
    adapter = EngineMcpAdapter.from_session(
        record=store.load("s"),
        repository=repository,
        store=store,
        registry=registry,
        origin=origin,
        policy=policy,
        client=client,
    )
    result = adapter.call_tool("mcp.http.echo", {"message": "over-http"})
    assert result.ok is True
    assert _text(result) == "echo: over-http"


def test_http_transport_missing_credential_fails_closed(http_server: Any, tmp_path: Path) -> None:
    repository = _make_repo(tmp_path)
    registry = McpRegistry(tmp_path / "mcp-servers.json")
    record = McpServerRecord(
        server_id="http-echo",
        namespace="http",
        transport="streamable_http",
        url=f"http://127.0.0.1:{http_server.port}/mcp",
        credential_ref="os-keyring:mcp/missing-token",
        credential_target="Authorization",
        read_only_tools=("echo",),
        timeout_seconds=20.0,
        max_transport_retries=0,
    )
    registry.put(record)
    with pytest.raises(McpError):
        client = McpClient(registry, McpCredentialStore(_FakeCredentialBackend()))
        client.discover_record(record, repository)
