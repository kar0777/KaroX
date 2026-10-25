"""Security-critical coverage for the hosted Core bridge and its MCP wire.

Every test here is loopback-only: the ASGI app is driven in process, and the
wire tests that need a socket bind ``127.0.0.1:0``.  Nothing reaches the
network, and no test prints a bearer token.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import anyio

from karox.bridge_proxy import (
    BRIDGE_ERROR_MESSAGES,
    _DIAGNOSTICS_TOOL_NAME,
    build_proxy_asgi_app,
    derive_idempotency_key,
    wire_tool_name,
)
from karox.hosted_core import (
    CORE_TOOL_NAMES,
    DEFAULT_HOSTED_DEADLINE_SECONDS,
    KNOWN_HOSTED_TOOL_NAMES,
    CoreToolBridge,
    HostedBridgeAccessDenied,
    HostedToolRuntime,
    ProxyToolDescriptor,
)
from karox.models import AccessProfile
from karox.repository_lease import RepositoryLeaseStore
from karox.sessions import SessionBusy, SessionStore
from karox.task_state import FactOrigin, TaskStateStore, fact


def initialize_git_repository(path: Path) -> None:
    """Create a deterministic repository with no inherited git identity."""

    path.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "KaroX Test",
            "GIT_AUTHOR_EMAIL": "karox@example.invalid",
            "GIT_COMMITTER_NAME": "KaroX Test",
            "GIT_COMMITTER_EMAIL": "karox@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=path,
        env=environment,
        check=True,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=120,
    )


class _AsgiResponse:
    def __init__(self, status: int, headers: list[tuple[bytes, bytes]], body: bytes):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def text(self) -> str:
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


def _memory_stream_pair() -> tuple[Any, Any]:
    return anyio.create_memory_object_stream(8)


async def _asgi_request(app: Any, request: dict[str, Any]) -> _AsgiResponse:
    body: bytes = request.get("body", b"")
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": request.get("method", "POST"),
        "path": request["path"],
        "raw_path": request["path"].encode("utf-8"),
        "root_path": "",
        "scheme": request.get("scheme", "http"),
        "query_string": request.get("query_string", b""),
        "headers": [
            (name.lower().encode("utf-8"), value.encode("utf-8"))
            for name, value in request.get("headers", ())
        ],
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8765),
    }
    pending = [{"type": "http.request", "body": body, "more_body": False}]
    state: dict[str, Any] = {"status": 0, "headers": [], "chunks": []}

    async def receive() -> dict[str, Any]:
        return pending.pop(0) if pending else {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            state["status"] = message["status"]
            state["headers"] = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            state["chunks"].append(message.get("body", b""))

    await app(scope, receive, send)
    return _AsgiResponse(state["status"], state["headers"], b"".join(state["chunks"]))


async def _drive_asgi(app: Any, requests: list[dict[str, Any]]) -> list[_AsgiResponse]:
    """Run an ASGI app in-process, lifespan included, without opening a socket."""

    to_app_send, to_app_receive = _memory_stream_pair()
    from_app_send, from_app_receive = _memory_stream_pair()
    responses: list[_AsgiResponse] = []
    async with (
        to_app_send,
        to_app_receive,
        from_app_send,
        from_app_receive,
        anyio.create_task_group() as group,
    ):
        group.start_soon(
            app, {"type": "lifespan"}, to_app_receive.receive, from_app_send.send
        )
        await to_app_send.send({"type": "lifespan.startup"})
        await from_app_receive.receive()
        for request in requests:
            responses.append(await _asgi_request(app, request))
        await to_app_send.send({"type": "lifespan.shutdown"})
        await from_app_receive.receive()
    return responses


def _wire_requests(app: Any, requests: list[dict[str, Any]]) -> list[_AsgiResponse]:
    return anyio.run(_drive_asgi, app, requests)


def _jsonrpc_call(
    token: str,
    name: str,
    arguments: dict[str, Any],
    *,
    host: str = "127.0.0.1:8765",
    origin: Optional[str] = None,
    meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name, "arguments": arguments}
    if meta is not None:
        params["_meta"] = meta
    headers = [
        ("host", host),
        ("authorization", f"Bearer {token}"),
        ("content-type", "application/json"),
        ("accept", "application/json, text/event-stream"),
    ]
    if origin is not None:
        headers.append(("origin", origin))
    return {
        "method": "POST",
        "path": "/mcp",
        "headers": headers,
        "body": json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": params}
        ).encode("utf-8"),
    }


def _tools_list(token: str) -> dict[str, Any]:
    return {
        "method": "POST",
        "path": "/mcp",
        "headers": [
            ("host", "127.0.0.1:8765"),
            ("authorization", f"Bearer {token}"),
            ("content-type", "application/json"),
            ("accept", "application/json, text/event-stream"),
        ],
        "body": json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        ).encode("utf-8"),
    }


def _jsonrpc_result(response: _AsgiResponse) -> dict[str, Any]:
    return response.json()["result"]


class _RaisingRuntime:
    """A hosted runtime whose tool fails with a host absolute path."""

    def __init__(self, error: BaseException) -> None:
        self.error = error

    def descriptors(self) -> list[ProxyToolDescriptor]:
        return [
            ProxyToolDescriptor(
                name="karox.repo.read_file",
                description="Read a file",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                },
                read_only=True,
            )
        ]

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
    ) -> dict[str, Any]:
        raise self.error


class HostedCoreBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "sample.txt").write_bytes(b"before\n")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "hosted task",
            AccessProfile.WORKSPACE_WRITE,
            session_id="hosted",
        )

    def _bridge(self, *tools: str) -> CoreToolBridge:
        return CoreToolBridge(
            self.repository,
            self.sessions,
            "hosted",
            list(tools),
            audit_path=self.root / "audit.jsonl",
        )

    def test_allowlist_exposes_and_executes_only_selected_core_tools(self) -> None:
        bridge = self._bridge("karox.repo.read_file", "karox.git.status")
        self.assertEqual(
            [item.name for item in bridge.descriptors()],
            ["karox.repo.read_file", "karox.git.status"],
        )
        result = bridge.execute("karox.repo.read_file", {"path": "sample.txt"})
        self.assertEqual(result["data"]["content"], "before\n")
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "not exposed"):
            bridge.execute("karox.repo.list_files", {})

    def test_construction_rejects_an_empty_or_unknown_allowlist(self) -> None:
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "must not be empty"):
            self._bridge()
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "unknown Core bridge"):
            self._bridge("karox.browser.open")
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "duplicates"):
            self._bridge("karox.repo.read_file", "karox.repo.read_file")

    def test_whole_drive_root_keeps_filesystem_tools_and_rejects_only_git(self) -> None:
        bridge = self._bridge("karox.repo.list_files", "karox.git.status")
        with patch("karox.hosted_core.is_drive_root", return_value=True):
            listed = bridge.execute(
                "karox.repo.list_files",
                {"pattern": "sample.txt"},
            )
            with self.assertRaisesRegex(
                HostedBridgeAccessDenied,
                "Git metadata is not applicable",
            ):
                bridge.execute("karox.git.status", {})

        self.assertTrue(listed["ok"])
        self.assertEqual(listed["data"]["files"], ["sample.txt"])

    def test_mutation_requires_idempotency_and_replays_safely(self) -> None:
        bridge = self._bridge("karox.repo.write_file")
        arguments = {"path": "sample.txt", "content": "after\n"}
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "idempotency"):
            bridge.execute("karox.repo.write_file", arguments)
        real_core = bridge._core()

        with patch.object(
            self.sessions, "heartbeat", wraps=self.sessions.heartbeat
        ) as heartbeat:

            class _SlowCore:
                # The mutation stays in flight until the lease heartbeat has
                # demonstrably fired, so "the write was heartbeated" cannot race
                # the mutation's own completion on a loaded runner.
                def tools(self):
                    return real_core.tools()

                def execute(self, command, lease=None):
                    deadline = time.monotonic() + 5.0
                    while time.monotonic() < deadline and heartbeat.call_count < 1:
                        time.sleep(0.005)
                    time.sleep(0.05)
                    return real_core.execute(command, lease=lease)

            with patch.object(bridge, "_core", return_value=_SlowCore()), patch(
                "karox.hosted_core._MUTATION_LEASE_HEARTBEAT_SECONDS", 0.01
            ):
                first = bridge.execute(
                    "karox.repo.write_file", arguments, idempotency_key="hosted-write-1"
                )
        self.assertGreaterEqual(heartbeat.call_count, 1)
        self.assertFalse(self.sessions.lease_path("hosted").exists())
        second = bridge.execute(
            "karox.repo.write_file", arguments, idempotency_key="hosted-write-1"
        )
        self.assertTrue(first["data"]["changed"])
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "after\n",
        )

    def test_short_parallel_mutation_contention_queues_instead_of_failing(self) -> None:
        bridge = self._bridge("karox.repo.write_file")
        real_acquire = self.sessions.acquire
        attempts = 0

        def contended_acquire(session_id, owner, ttl_seconds=30.0):
            nonlocal attempts
            attempts += 1
            if attempts <= 2:
                raise SessionBusy("synthetic sibling mutation")
            return real_acquire(session_id, owner, ttl_seconds)

        with patch.object(self.sessions, "acquire", side_effect=contended_acquire), patch(
            "karox.hosted_core.time.sleep"
        ) as sleeper:
            result = bridge.execute(
                "karox.repo.write_file",
                {"path": "sample.txt", "content": "queued\n"},
                idempotency_key="queued-write-1",
            )

        self.assertTrue(result["data"]["changed"])
        self.assertEqual(attempts, 3)
        self.assertEqual(sleeper.call_count, 2)
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "queued\n",
        )

    def test_checks_require_an_explicit_command_allowlist(self) -> None:
        with self.assertRaisesRegex(
            HostedBridgeAccessDenied, "verification commands"
        ):
            self._bridge("karox.checks.run")
        bridge = CoreToolBridge(
            self.repository,
            self.sessions,
            "hosted",
            ["karox.checks.run"],
            verification_commands=[["python", "-c", "print('ok')"]],
        )
        with self.assertRaisesRegex(Exception, "user-approved"):
            bridge.execute(
                "karox.checks.run",
                {"argv": ["python", "-c", "print('other')"]},
                idempotency_key="wrong-check",
            )

    def test_profile_and_revocation_fail_closed(self) -> None:
        self.sessions.create(
            self.repository,
            "read only",
            AccessProfile.READ_ONLY,
            session_id="read-only",
        )
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "does not allow"):
            CoreToolBridge(
                self.repository,
                self.sessions,
                "read-only",
                ["karox.repo.write_file"],
            )
        bridge = self._bridge("karox.repo.read_file")
        self.sessions.revoke("hosted")
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "revoked"):
            bridge.descriptors()
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "revoked"):
            bridge.execute("karox.repo.read_file", {"path": "sample.txt"})

    def test_workspace_write_rejects_commit_and_elevated_allows_it(self) -> None:
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "does not allow"):
            CoreToolBridge(
                self.repository,
                self.sessions,
                "hosted",
                ["karox.git.commit"],
                audit_path=self.root / "audit.jsonl",
            )

        self.sessions.create(
            self.repository,
            "elevated task",
            AccessProfile.ELEVATED,
            session_id="elevated",
        )
        all_tools = [
            "karox.repo.read_file",
            "karox.repo.write_file",
            "karox.repo.list_files",
            "karox.repo.search",
            "karox.checks.run",
            "karox.git.status",
            "karox.git.diff",
            "karox.git.commit",
        ]
        bridge = CoreToolBridge(
            self.repository,
            self.sessions,
            "elevated",
            list(all_tools),
            audit_path=self.root / "audit-elevated.jsonl",
            verification_commands=[["python", "-m", "compileall", "-q", "src"]],
        )
        self.assertEqual(
            sorted(item.name for item in bridge.descriptors()),
            sorted(all_tools),
        )

    def test_tool_name_universes_are_stable_and_include_every_core_tool(self) -> None:
        self.assertLessEqual(set(CORE_TOOL_NAMES), set(KNOWN_HOSTED_TOOL_NAMES))
        # The compatibility catalogue a hosted launch validates against must
        # keep the names v5 clients already cached.
        for name in (
            "karox.browser.open",
            "karox.dev_server.start",
            "karox.task.bootstrap",
            "karox.orchestrate.plan",
            "karox.memory.recall",
        ):
            self.assertIn(name, KNOWN_HOSTED_TOOL_NAMES)
        self.assertEqual(DEFAULT_HOSTED_DEADLINE_SECONDS, 600.0)

    def test_hosted_tool_runtime_protocol_is_satisfied_by_core_bridge(self) -> None:
        runtime: HostedToolRuntime = self._bridge("karox.repo.read_file")
        self.assertEqual(runtime.session_info()["session_id"], "hosted")


class RepositoryLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        self.sessions = SessionStore(self.root / "sessions")

    def _bridge(self, session_id: str = "session-a") -> CoreToolBridge:
        bridge = CoreToolBridge(
            self.repository,
            self.sessions,
            session_id,
            ["karox.repo.read_file", "karox.repo.write_file"],
            audit_path=self.root / "audit.jsonl",
        )
        bridge.repository_leases = RepositoryLeaseStore(self.root / "repository-leases")
        return bridge

    def test_direct_hosted_mutation_respects_live_repository_lease_but_reads_continue(
        self,
    ) -> None:
        (self.repository / "before.txt").write_text("before\n", encoding="utf-8")
        self.sessions.create(
            self.repository,
            "direct lease guard",
            AccessProfile.WORKSPACE_WRITE,
            branch="main",
            session_id="session-a",
        )
        bridge = self._bridge()
        leases = bridge.repository_leases
        held, _ = leases.acquire(
            self.repository,
            session_id="session-b",
            task_id="task-b",
            connection_id="other-agent",
            current_operation="long verification",
            ttl_seconds=30,
        )
        try:
            read = bridge.execute("karox.repo.read_file", {"path": "before.txt"})
            self.assertTrue(read["ok"])
            self.assertIn("before", read["data"]["content"])

            with self.assertRaisesRegex(HostedBridgeAccessDenied, "repository is busy"):
                bridge.execute(
                    "karox.repo.write_file",
                    {"path": "blocked.txt", "content": "must-not-land\n"},
                    idempotency_key="blocked-write",
                    deadline_seconds=1,
                )
            self.assertFalse((self.repository / "blocked.txt").exists())
        finally:
            leases.release(self.repository, held)

        written = bridge.execute(
            "karox.repo.write_file",
            {"path": "after.txt", "content": "after\n"},
            idempotency_key="after-release-write",
            deadline_seconds=5,
        )
        self.assertTrue(written["ok"])
        self.assertEqual(
            (self.repository / "after.txt").read_text(encoding="utf-8"), "after\n"
        )

    def test_nested_high_level_lease_is_reused_for_same_session_and_task(self) -> None:
        self.sessions.create(
            self.repository,
            "nested lease guard",
            AccessProfile.WORKSPACE_WRITE,
            branch="main",
            session_id="session-a",
        )
        task = TaskStateStore(self.sessions).bootstrap(
            "session-a",
            {"objective": fact("nested plan", FactOrigin.VERIFIED, "test")},
        )
        bridge = self._bridge()
        leases = bridge.repository_leases
        held, _ = leases.acquire(
            self.repository,
            session_id="session-a",
            task_id=task.task_id,
            connection_id="plan-executor",
            current_operation="task.execute_plan",
            ttl_seconds=30,
        )
        try:
            result = bridge.execute(
                "karox.repo.write_file",
                {"path": "nested.txt", "content": "nested\n"},
                idempotency_key="nested-write",
                deadline_seconds=5,
            )
            self.assertTrue(result["ok"])
            loaded = leases.load(self.repository)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.lease_id, held.lease_id)
        finally:
            leases.release(self.repository, held)

        self.assertEqual(
            (self.repository / "nested.txt").read_text(encoding="utf-8"), "nested\n"
        )


class BridgeWireSecurityTests(unittest.TestCase):
    """ASGI-level coverage for the bridge's public HTTP boundary."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "sample.txt").write_bytes(b"origin\n")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "origin task",
            AccessProfile.WORKSPACE_WRITE,
            session_id="origin",
        )
        self.runtime = CoreToolBridge(
            self.repository,
            self.sessions,
            "origin",
            ["karox.repo.read_file", "karox.repo.write_file"],
        )
        self.token = "origin-wire-token"

    def test_foreign_origin_is_rejected_before_the_tool_runs(self) -> None:
        app = build_proxy_asgi_app(self.runtime, self.token)
        diagnostics = io.StringIO()
        with contextlib.redirect_stderr(diagnostics):
            rejected, accepted = _wire_requests(
                app,
                [
                    _jsonrpc_call(
                        self.token,
                        "karox.repo.read_file",
                        {"path": "sample.txt"},
                        origin="https://attacker.example",
                    ),
                    _jsonrpc_call(
                        self.token,
                        "karox.repo.read_file",
                        {"path": "sample.txt"},
                        origin="http://127.0.0.1:8765",
                    ),
                ],
            )
        self.assertEqual(rejected.status, 421)
        self.assertEqual(accepted.status, 200, accepted.text)
        self.assertIn("[karox-rebind] 421", diagnostics.getvalue())

    def test_foreign_host_is_rejected(self) -> None:
        app = build_proxy_asgi_app(self.runtime, self.token)
        request = _jsonrpc_call(
            self.token, "karox.repo.read_file", {"path": "sample.txt"}
        )
        request["headers"] = [
            (name, "rebound.attacker.example" if name == "host" else value)
            for name, value in request["headers"]
        ]
        diagnostics = io.StringIO()
        with contextlib.redirect_stderr(diagnostics):
            (response,) = _wire_requests(app, [request])
        self.assertEqual(response.status, 421)
        self.assertIn("[karox-rebind] 421", diagnostics.getvalue())

    def test_declared_public_host_is_accepted_and_trailing_slash_is_served(self) -> None:
        app = build_proxy_asgi_app(
            self.runtime, self.token, allowed_hosts=("bridge.trycloudflare.com",)
        )
        request = _jsonrpc_call(
            self.token,
            "karox.repo.read_file",
            {"path": "sample.txt"},
            host="bridge.trycloudflare.com",
            origin="https://bridge.trycloudflare.com",
        )
        request["path"] = "/mcp/"
        (response,) = _wire_requests(app, [request])
        self.assertEqual(response.status, 200, response.text)
        self.assertFalse(_jsonrpc_result(response)["isError"])

    def test_catalog_advertises_wire_names_and_accepts_the_dotted_alias(self) -> None:
        app = build_proxy_asgi_app(self.runtime, self.token)
        listed, by_wire, by_dotted = _wire_requests(
            app,
            [
                _tools_list(self.token),
                _jsonrpc_call(
                    self.token,
                    wire_tool_name("karox.repo.read_file"),
                    {"path": "sample.txt"},
                ),
                _jsonrpc_call(
                    self.token, "karox.repo.read_file", {"path": "sample.txt"}
                ),
            ],
        )
        tools = _jsonrpc_result(listed)["tools"]
        self.assertEqual(
            sorted(item["name"] for item in tools),
            ["karox_repo_read_file", "karox_repo_write_file"],
        )
        for response in (by_wire, by_dotted):
            self.assertEqual(response.status, 200, response.text)
            self.assertFalse(_jsonrpc_result(response)["isError"])

    def test_a_mutating_call_without_meta_is_served_and_deduplicated(self) -> None:
        # ChatGPT and Claude send no `_meta`, so this is the only shape their
        # write calls ever have. The key is derived from the call instead.
        app = build_proxy_asgi_app(self.runtime, self.token)

        def write() -> dict[str, Any]:
            return _jsonrpc_call(
                self.token,
                "karox.repo.write_file",
                {"path": "sample.txt", "content": "rebound\n"},
            )

        first, second = _wire_requests(app, [write(), write()])
        result = _jsonrpc_result(first)
        self.assertFalse(result["isError"], first.text)
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "rebound\n",
        )
        self.assertFalse(result["structuredContent"]["idempotent_replay"])
        replayed = _jsonrpc_result(second)
        self.assertFalse(replayed["isError"], second.text)
        self.assertTrue(replayed["structuredContent"]["idempotent_replay"])

    def test_a_second_distinct_mutation_is_not_mistaken_for_a_retry(self) -> None:
        app = build_proxy_asgi_app(self.runtime, self.token)
        responses = _wire_requests(
            app,
            [
                _jsonrpc_call(
                    self.token,
                    "karox.repo.write_file",
                    {"path": "sample.txt", "content": content},
                )
                for content in ("first\n", "second\n")
            ],
        )
        for response in responses:
            result = _jsonrpc_result(response)
            self.assertFalse(result["isError"], response.text)
            self.assertFalse(result["structuredContent"]["idempotent_replay"])
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "second\n",
        )

    def test_a_client_supplied_key_is_still_honoured(self) -> None:
        app = build_proxy_asgi_app(self.runtime, self.token)
        accepted, rejected = _wire_requests(
            app,
            [
                _jsonrpc_call(
                    self.token,
                    "karox.repo.write_file",
                    {"path": "sample.txt", "content": "named\n"},
                    meta={"karoxIdempotencyKey": "write-1"},
                ),
                # A key that is present but unusable is still an error: deriving
                # one here would paper over a client bug.
                _jsonrpc_call(
                    self.token,
                    "karox.repo.write_file",
                    {"path": "sample.txt", "content": "named\n"},
                    meta={"karoxIdempotencyKey": ""},
                ),
            ],
        )
        self.assertFalse(_jsonrpc_result(accepted)["isError"], accepted.text)
        result = _jsonrpc_result(rejected)
        self.assertTrue(result["isError"])
        self.assertEqual(
            result["structuredContent"]["error_code"], "idempotency_key_invalid"
        )
        self.assertEqual(
            (self.repository / "sample.txt").read_text(encoding="utf-8"),
            "named\n",
        )

    def test_tool_failure_never_reflects_a_filesystem_path(self) -> None:
        secret_path = "/home/user/x"
        app = build_proxy_asgi_app(
            _RaisingRuntime(FileNotFoundError(secret_path)), self.token
        )
        (response,) = _wire_requests(
            app,
            [
                _jsonrpc_call(
                    self.token, "karox.repo.read_file", {"path": "sample.txt"}
                )
            ],
        )
        result = _jsonrpc_result(response)
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error_code"], "not_found")
        for fragment in ("/home/user/x", "/home/user", "home", "user"):
            self.assertNotIn(fragment, response.text)

    def test_unknown_tool_is_refused_without_echoing_arguments(self) -> None:
        app = build_proxy_asgi_app(self.runtime, self.token)
        (response,) = _wire_requests(
            app,
            [_jsonrpc_call(self.token, "karox.repo.delete_everything", {"path": "x"})],
        )
        result = _jsonrpc_result(response)
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error_code"], "tool_not_exposed")
        self.assertNotIn("delete_everything", response.text)
        self.assertNotIn(self.token, response.text)

    def test_bare_token_and_bearer_token_both_authorize(self) -> None:
        def _authorized_as(value: str) -> dict[str, Any]:
            request = _jsonrpc_call(
                self.token, "karox.repo.read_file", {"path": "sample.txt"}
            )
            request["headers"] = [
                (name, value if name == "authorization" else existing)
                for name, existing in request["headers"]
            ]
            return request

        cases: list[tuple[str, str, int]] = [
            ("prefixed token", f"Bearer {self.token}", 200),
            ("bare token", self.token, 200),
            ("lowercase scheme", f"bearer {self.token}", 200),
            ("padded value", f"  Bearer  {self.token}  ", 200),
            ("prefixed wrong secret", "Bearer wrong-secret", 401),
            ("bare wrong secret", "wrong-secret", 401),
            ("basic scheme", f"Basic {self.token}", 401),
            ("token scheme", f"Token {self.token}", 401),
            ("empty header", "", 401),
            ("scheme with no credential", "Bearer ", 401),
        ]

        responses = _wire_requests(
            build_proxy_asgi_app(self.runtime, self.token),
            [_authorized_as(value) for _, value, _ in cases],
        )

        for (label, _, expected), response in zip(cases, responses):
            with self.subTest(authorization=label):
                self.assertEqual(response.status, expected)

    def test_non_ascii_bearer_credential_is_unauthorized(self) -> None:
        app = build_proxy_asgi_app(self.runtime, self.token)
        request = _jsonrpc_call(
            self.token, "karox.repo.read_file", {"path": "sample.txt"}
        )
        request["headers"] = [
            (name, "Bearer é" if name == "authorization" else value)
            for name, value in request["headers"]
        ]
        (response,) = _wire_requests(app, [request])
        self.assertEqual(response.status, 401)

    def test_unauthorized_declares_the_scheme_it_wants(self) -> None:
        app = build_proxy_asgi_app(
            self.runtime,
            self.token,
            unauthorized_headers={"WWW-Authenticate": 'Bearer realm="KaroX bridge"'},
        )
        request = _jsonrpc_call(
            self.token, "karox.repo.read_file", {"path": "sample.txt"}
        )
        request["headers"] = [
            (name, "Bearer wrong-secret" if name == "authorization" else value)
            for name, value in request["headers"]
        ]
        (response,) = _wire_requests(app, [request])
        self.assertEqual(response.status, 401)
        headers = {
            name.decode("latin-1").lower(): value.decode("latin-1")
            for name, value in response.headers
        }
        self.assertEqual(headers.get("www-authenticate"), 'Bearer realm="KaroX bridge"')

    def test_diagnostics_is_authenticated_and_carries_no_credential(self) -> None:
        app = build_proxy_asgi_app(
            self.runtime,
            self.token,
            diagnostics={"schema_version": 1, "available_tools": ["karox.repo.read_file"]},
        )
        (unauthorized,) = _wire_requests(
            app,
            [
                _jsonrpc_call(
                    "wrong-secret", wire_tool_name(_DIAGNOSTICS_TOOL_NAME), {}
                )
            ],
        )
        self.assertEqual(unauthorized.status, 401)

        # The session manager serves one lifespan per app, so the authorized
        # probe needs its own instance rather than a second lifespan.
        authorized_app = build_proxy_asgi_app(
            self.runtime,
            self.token,
            diagnostics={"schema_version": 1, "available_tools": ["karox.repo.read_file"]},
        )
        (authorized,) = _wire_requests(
            authorized_app,
            [
                _jsonrpc_call(
                    self.token, wire_tool_name(_DIAGNOSTICS_TOOL_NAME), {}
                )
            ],
        )
        result = _jsonrpc_result(authorized)
        self.assertFalse(result["isError"], authorized.text)
        live = result["structuredContent"]
        self.assertGreaterEqual(live["transport_runtime"]["requests_started"], 1)
        encoded = json.dumps(live, ensure_ascii=False)
        self.assertNotIn(self.token, encoded)
        self.assertNotIn("wrong-secret", encoded)


class DerivedIdempotencyKeyTests(unittest.TestCase):
    """The derived key is only useful if it is stable across a client's retries."""

    def test_the_same_call_derives_the_same_key_whatever_the_argument_order(
        self,
    ) -> None:
        first = derive_idempotency_key(
            "karox.repo.write_file", {"path": "a.txt", "content": "x"}
        )
        second = derive_idempotency_key(
            "karox.repo.write_file", {"content": "x", "path": "a.txt"}
        )
        self.assertEqual(first, second)

    def test_a_different_call_derives_a_different_key(self) -> None:
        base = derive_idempotency_key("karox.repo.write_file", {"path": "a.txt"})
        self.assertNotEqual(
            base,
            derive_idempotency_key("karox.repo.write_file", {"path": "b.txt"}),
        )
        self.assertNotEqual(
            base,
            derive_idempotency_key("karox.repo.edit_file", {"path": "a.txt"}),
        )

    def test_the_key_fits_the_length_the_wire_accepts(self) -> None:
        key = derive_idempotency_key("karox.repo.write_file", {"content": "x" * 5000})
        self.assertLessEqual(len(key), 256)
        self.assertTrue(key)


class BridgeErrorVocabularyTests(unittest.TestCase):
    def test_every_code_has_a_fixed_message(self) -> None:
        for code in (
            "tool_not_exposed",
            "idempotency_key_required",
            "idempotency_key_invalid",
            "denied",
            "not_found",
            "executable_not_found",
            "invalid_request",
            "verification_not_approved",
            "request_interrupted",
            "internal",
        ):
            self.assertIn(code, BRIDGE_ERROR_MESSAGES)
        self.assertNotIn("{", BRIDGE_ERROR_MESSAGES["internal"])


if __name__ == "__main__":
    unittest.main()