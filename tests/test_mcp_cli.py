"""Real CLI-path coverage for MCP server, session, and call commands.

Each case runs ``karox.cli.main`` in-process against an isolated temporary
configuration/runtime root and the genuine stdio echo server.  The point is to
prove the shipped command path -- not only the library API -- still enforces the
mutation lease, idempotency key, and replay contract.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable, Optional

import pytest

from _mcp_support import (
    ECHO_SERVER,
    child_process_cleanup,
    cli_json,
    isolate_karox_environment,
    read_lines,
)

from karox.sessions import SessionStore


READ_ONLY_TOOL = "mcp.echo.echo"
MUTATING_TOOL = "mcp.echo.write_note"
MUTATING_ARGUMENTS = {"name": "note", "content": "c"}


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    config = tmp_path / "config"
    runtime = tmp_path / "runtime"
    isolate_karox_environment(
        monkeypatch, config_dir=config, runtime_dir=runtime
    )
    repository = tmp_path / "repo"
    repository.mkdir()
    calls = tmp_path / "calls.log"
    pid_file = tmp_path / "server.pid"
    try:
        yield SimpleNamespace(
            tmp_path=tmp_path,
            config=config,
            runtime=runtime,
            repo=repository,
            calls=calls,
            pid_file=pid_file,
        )
    finally:
        with child_process_cleanup(paths=[pid_file], scripts=[ECHO_SERVER]):
            pass


def _add_server(
    env: Any,
    *,
    server_id: str = "echo",
    read_only_tools: Iterable[str] = ("echo",),
    environment: Optional[Iterable[str]] = None,
) -> tuple[int, Any, str]:
    argv = [
        "mcp",
        "server",
        "add",
        server_id,
        "--namespace",
        server_id,
        "--transport",
        "stdio",
        "--command",
        sys.executable,
        "--arg",
        str(ECHO_SERVER),
        "--timeout-seconds",
        "20",
        "--max-transport-retries",
        "0",
        "--json",
    ]
    for name in read_only_tools:
        argv.extend(["--read-only-tool", name])
    for pair in environment or ():
        argv.extend(["--env", pair])
    return cli_json(argv)


def _prepare(env: Any, *, permissions: Iterable[str] = ("echo=allow", "write_note=allow")) -> None:
    code, payload, error = cli_json(
        [
            "session",
            "create",
            "--repository",
            str(env.repo),
            "--task",
            "exercise the MCP CLI",
            "--id",
            "s1",
            "--json",
        ]
    )
    assert code == 0, error
    assert payload["session_id"] == "s1"
    code, payload, error = _add_server(
        env,
        environment=(
            f"KAROX_MCP_CALL_LOG={env.calls}",
            f"KAROX_MCP_PID_FILE={env.pid_file}",
        ),
    )
    assert code == 0, error
    assert payload["server_id"] == "echo"
    code, payload, error = _select(env, permissions)
    assert code == 0, error
    assert payload["selection"]["server_id"] == "echo"


def _select(env: Any, permissions: Iterable[str]) -> tuple[int, Any, str]:
    argv = [
        "mcp",
        "session",
        "select",
        "echo",
        "--session-id",
        "s1",
        "--repository",
        str(env.repo),
        "--json",
    ]
    for decision in permissions:
        argv.extend(["--permission", decision])
    return cli_json(argv)


def _call(
    env: Any,
    tool: str,
    arguments: dict[str, Any],
    *,
    idempotency_key: Optional[str] = None,
) -> tuple[int, Any, str]:
    argv = [
        "mcp",
        "call",
        tool,
        "--session-id",
        "s1",
        "--repository",
        str(env.repo),
        "--arguments",
        json.dumps(arguments),
        "--json",
    ]
    if idempotency_key is not None:
        argv.extend(["--idempotency-key", idempotency_key])
    return cli_json(argv)


def _text(payload: dict[str, Any]) -> str:
    return payload["data"]["result"]["content"][0]["text"]


def test_cli_read_only_call_succeeds_without_an_idempotency_key(
    cli_env: Any,
) -> None:
    _prepare(cli_env)
    assert read_lines(cli_env.calls) == []
    code, payload, error = _call(
        cli_env, READ_ONLY_TOOL, {"message": "cli-trip"}
    )
    assert code == 0, error
    assert payload["ok"] is True
    assert payload["mutation"] is False
    assert payload["idempotent_replay"] is False
    assert payload["data"]["tool"] == READ_ONLY_TOOL
    assert _text(payload) == "echo: cli-trip"
    assert read_lines(cli_env.calls) == ["echo"]


def test_cli_read_only_call_does_not_take_a_mutation_lease(
    cli_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(cli_env)
    acquired: list[tuple[str, str]] = []
    original = SessionStore.acquire

    def recording_acquire(
        self: SessionStore, session_id: str, owner: str, ttl_seconds: float = 30.0
    ) -> Any:
        lease = original(self, session_id, owner, ttl_seconds)
        acquired.append((session_id, owner))
        return lease

    monkeypatch.setattr(SessionStore, "acquire", recording_acquire)
    code, _payload, error = _call(cli_env, READ_ONLY_TOOL, {"message": "no-lease"})
    assert code == 0, error
    assert acquired == []
    lock_code, lock, lock_error = cli_json(
        ["session", "lock", "s1", "--json"]
    )
    assert lock_code == 0, lock_error
    assert lock["locked"] is False


def test_cli_mutating_call_requires_an_idempotency_key(cli_env: Any) -> None:
    _prepare(cli_env)
    before = read_lines(cli_env.calls)
    code, payload, error = _call(cli_env, MUTATING_TOOL, MUTATING_ARGUMENTS)
    assert code == 2
    assert payload is None
    assert "--idempotency-key" in error
    assert read_lines(cli_env.calls) == before


def test_cli_mutating_call_takes_a_lease_and_replays_without_executing_twice(
    cli_env: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prepare(cli_env)
    acquired: list[tuple[str, str]] = []
    original = SessionStore.acquire

    def recording_acquire(
        self: SessionStore, session_id: str, owner: str, ttl_seconds: float = 30.0
    ) -> Any:
        lease = original(self, session_id, owner, ttl_seconds)
        acquired.append((session_id, owner))
        return lease

    monkeypatch.setattr(SessionStore, "acquire", recording_acquire)
    before = len(read_lines(cli_env.calls))

    code, first, error = _call(
        cli_env, MUTATING_TOOL, MUTATING_ARGUMENTS, idempotency_key="cli-key"
    )
    assert code == 0, error
    assert first["ok"] is True
    assert first["mutation"] is True
    assert first["idempotent_replay"] is False
    assert _text(first) == "noted: note"
    assert acquired and acquired[-1][0] == "s1"
    assert acquired[-1][1].startswith("mcp-call-")
    assert read_lines(cli_env.calls)[before:].count("write_note") == 1

    code, second, error = _call(
        cli_env, MUTATING_TOOL, MUTATING_ARGUMENTS, idempotency_key="cli-key"
    )
    assert code == 0, error
    assert second["idempotent_replay"] is True
    assert second["data"] == first["data"]
    assert read_lines(cli_env.calls)[before:].count("write_note") == 1

    lock_code, lock, lock_error = cli_json(
        ["session", "lock", "s1", "--json"]
    )
    assert lock_code == 0, lock_error
    assert lock["locked"] is False


def test_cli_replay_with_different_input_conflicts(cli_env: Any) -> None:
    _prepare(cli_env)
    before = len(read_lines(cli_env.calls))
    code, _payload, error = _call(
        cli_env, MUTATING_TOOL, MUTATING_ARGUMENTS, idempotency_key="conflict-key"
    )
    assert code == 0, error
    code, payload, error = _call(
        cli_env,
        MUTATING_TOOL,
        {"name": "note", "content": "different"},
        idempotency_key="conflict-key",
    )
    assert code == 2
    assert payload is None
    assert "different input" in error
    assert read_lines(cli_env.calls)[before:].count("write_note") == 1


def test_cli_call_without_a_selection_is_refused(cli_env: Any) -> None:
    code, _payload, error = cli_json(
        [
            "session",
            "create",
            "--repository",
            str(cli_env.repo),
            "--task",
            "no MCP selection",
            "--id",
            "s1",
            "--json",
        ]
    )
    assert code == 0, error
    code, payload, error = _call(cli_env, READ_ONLY_TOOL, {"message": "x"})
    assert code == 2
    assert payload is None
    assert "no selected MCP servers" in error


def test_cli_denied_tool_is_not_callable(cli_env: Any) -> None:
    _prepare(cli_env, permissions=("echo=deny", "write_note=allow"))
    code, payload, error = _call(cli_env, READ_ONLY_TOOL, {"message": "x"})
    assert code == 2
    assert payload is None
    assert "not selected" in error
    assert read_lines(cli_env.calls) == []


def test_cli_select_rejects_an_unknown_permission(cli_env: Any) -> None:
    code, _payload, error = cli_json(
        [
            "session",
            "create",
            "--repository",
            str(cli_env.repo),
            "--task",
            "bad permission",
            "--id",
            "s1",
            "--json",
        ]
    )
    assert code == 0, error
    code, _payload, error = _add_server(cli_env)
    assert code == 0, error
    code, payload, error = _select(cli_env, ("echo=maybe",))
    assert code == 2
    assert payload is None
    assert "allow, ask, or deny" in error


def test_cli_server_add_rejects_persisted_secret_material(cli_env: Any) -> None:
    code, payload, error = _add_server(
        cli_env, server_id="secret", environment=("API_KEY=plain-value",)
    )
    assert code == 2
    assert payload is None
    assert "secret-like" in error
    assert not (cli_env.config / "vnext" / "mcp-servers.json").exists()

def test_agent_run_mcp_server_flag_stores_the_chosen_scope(cli_env: Any) -> None:
    """``agent run --mcp-server`` is how the TUI attaches a server to a new session.

    The flag is the explicit approval: the chosen tools become ``allow`` and the
    rest ``deny`` -- never ``ask``, which would hide them from the agent.
    """

    from karox import cli
    from karox.paths import session_dir

    code, _payload, error = cli_json(
        [
            "session",
            "create",
            "--repository",
            str(cli_env.repo),
            "--task",
            "use the echo server",
            "--id",
            "s1",
            "--json",
        ]
    )
    assert code == 0, error
    code, _payload, error = _add_server(
        cli_env,
        environment=(
            f"KAROX_MCP_CALL_LOG={cli_env.calls}",
            f"KAROX_MCP_PID_FILE={cli_env.pid_file}",
        ),
    )
    assert code == 0, error
    store = SessionStore(session_dir())
    repository = cli_env.repo.resolve()
    record = cli._attach_mcp_servers(store, store.load("s1"), repository, ["echo"])
    tools = record.mcp_servers[0]["tools"]
    assert tools["echo"]["permission"] == "allow"
    assert {tools[name]["permission"] for name in ("slow_echo", "write_note", "reflect_secret")} == {
        "deny"
    }
    _binding, descriptors = cli._selected_mcp_runtime(record, repository)
    assert [item.name for item in descriptors] == ["mcp.echo.echo"]

    record = cli._attach_mcp_servers(store, record, repository, ["echo=all"])
    assert len(record.mcp_servers) == 1
    _binding, descriptors = cli._selected_mcp_runtime(record, repository)
    assert sorted(item.name for item in descriptors) == [
        "mcp.echo.echo",
        "mcp.echo.reflect_secret",
        "mcp.echo.slow_echo",
        "mcp.echo.write_note",
    ]


def test_cli_error_result_carries_the_server_text(cli_env: Any) -> None:
    _prepare(cli_env)
    code, _payload, error = _call(
        cli_env,
        MUTATING_TOOL,
        {"name": "boom", "content": "c"},
        idempotency_key="boom-once",
    )
    assert code != 0
    assert "note storage failed" in error
