"""End-to-end coverage for built-in Core tools exposed to hosted clients."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest.mock import patch

import httpx
import uvicorn

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.credentials import CredentialError
from karox.hosted_bridge import (
    CompositeHostedBridge,
    CoreToolBridge,
    HostedBridgeAccessDenied,
)
from karox.mcp_client import (
    McpClient,
    McpCredentialStore,
    McpRegistry,
    McpRemoteToolError,
    McpServerRecord,
)
from karox.models import AccessProfile
from karox.openapi_bridge import build_openapi_bridge_app
from karox.proxy import ProxyToolDescriptor
from karox.proxy_server import build_proxy_asgi_app, derive_idempotency_key
from karox.sessions import SessionBusy, SessionStore


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
            raise RuntimeError("bridge wire server did not start")
        self.port = self.server.servers[0].sockets[0].getsockname()[1]

    def close(self) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=10)
        if self.thread.is_alive():
            raise RuntimeError("bridge wire server did not stop")


async def _call_mcp_tool_async(
    url: str,
    token: str,
    name: str,
    arguments: dict[str, object],
    meta: Optional[dict[str, object]] = None,
) -> Any:
    """Call a tool over Streamable HTTP with full control over request _meta.

    The high-level :class:`McpClient` neither exposes ``meta`` nor preserves the
    server's error text when a tool returns ``isError``.  This helper drives a
    raw :class:`~mcp.ClientSession` so the wire tests can both supply an
    explicit ``_meta.karoxIdempotencyKey`` and inspect the error message.
    """
    from datetime import timedelta

    from mcp import ClientSession

    from karox.mcp_client import streamable_http_transport

    headers = {"Authorization": f"Bearer {token}"}
    async with streamable_http_transport(
        url,
        headers=headers,
        timeout_seconds=15.0,
    ) as streams:
        async with ClientSession(
            streams[0],
            streams[1],
            read_timeout_seconds=timedelta(seconds=15),
        ) as session:
            await session.initialize()
            with _expected_alias_client_warning(name):
                return await session.call_tool(name, arguments, meta=meta)


class _ExpectedAliasWarningFilter(logging.Filter):
    """Drop the SDK client's 'not listed by server' line for one dotted alias.

    Tests here deliberately call tools by the internal dotted spelling to prove
    the compatibility alias keeps working. The SDK cannot validate a name that
    never appeared in ``tools/list`` and warns about it -- an expected negative
    path, so it is contained at the call site instead of flooding the release
    console. Only the exact message for the exact alias is dropped.
    """

    def __init__(self, alias: str) -> None:
        super().__init__()
        self._alias = alias

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        return not ("not listed by server" in message and self._alias in message)


@contextlib.contextmanager
def _expected_alias_client_warning(name: str):
    if "." not in name:
        yield
        return
    scoped = _ExpectedAliasWarningFilter(name)
    targets = [logging.getLogger("client"), logging.getLogger("mcp.client.session")]
    for target in targets:
        target.addFilter(scoped)
    try:
        yield
    finally:
        for target in targets:
            target.removeFilter(scoped)


def _call_mcp_tool(
    url: str,
    token: str,
    name: str,
    arguments: dict[str, object],
    meta: Optional[dict[str, object]] = None,
) -> Any:
    import anyio

    return anyio.run(_call_mcp_tool_async, url, token, name, arguments, meta)


def _result_text(result: Any) -> str:
    return "".join(getattr(block, "text", "") for block in result.content)


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
    import anyio

    return anyio.create_memory_object_stream(8)


async def _drive_asgi(app: Any, requests: list[dict[str, Any]]) -> list[_AsgiResponse]:
    """Run an ASGI app in-process, lifespan included, without opening a socket.

    A real socket would make these tests depend on the loopback stack and on a
    client library's own header handling, and the header bytes are exactly what
    is under test here.
    """
    import anyio

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


def _wire_requests(app: Any, requests: list[dict[str, Any]]) -> list[_AsgiResponse]:
    import anyio

    return anyio.run(_drive_asgi, app, requests)


def _tools_call(
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
        deadline_seconds: float = 30.0,
    ) -> dict[str, Any]:
        raise self.error


class HostedCoreBridgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
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

    def tearDown(self) -> None:
        self.temporary.cleanup()

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

    def test_whole_drive_root_keeps_filesystem_tools_and_rejects_only_git(self) -> None:
        bridge = self._bridge("karox.repo.list_files", "karox.git.status")
        with patch("karox.hosted_bridge.is_drive_root", return_value=True):
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

        class _SlowCore:
            def tools(self):
                return real_core.tools()

            def execute(self, command, lease=None):
                time.sleep(0.05)
                return real_core.execute(command, lease=lease)

        with patch.object(bridge, "_core", return_value=_SlowCore()), patch(
            "karox.hosted_bridge._MUTATION_LEASE_HEARTBEAT_SECONDS", 0.01
        ), patch.object(
            self.sessions, "heartbeat", wraps=self.sessions.heartbeat
        ) as heartbeat:
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
            "karox.hosted_bridge.time.sleep"
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

    def test_elevated_exposes_all_eight_tools_and_workspace_write_rejects_commit(
        self,
    ) -> None:
        # Under workspace_write the commit capability is absent from the
        # profile, so the bridge constructor must refuse to expose
        # karox.git.commit (the "hosted session does not allow git.commit"
        # case described for the hosted bridge).
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
            verification_commands=[
                [
                    "python",
                    "-m",
                    "unittest",
                    "discover",
                    "-s",
                    "tests",
                    "-p",
                    "test_*.py",
                ],
                ["python", "-m", "compileall", "-q", "src", "tests"],
            ],
        )
        self.assertEqual(
            sorted(item.name for item in bridge.descriptors()),
            sorted(all_tools),
        )


class HostedBridgeWireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        (self.repository / "sample.txt").write_bytes(b"wire\n")
        self.sessions = SessionStore(self.root / "sessions")
        self.sessions.create(
            self.repository,
            "wire task",
            AccessProfile.WORKSPACE_WRITE,
            session_id="wire",
        )
        self.runtime = CompositeHostedBridge(
            [
                CoreToolBridge(
                    self.repository,
                    self.sessions,
                    "wire",
                    ["karox.repo.read_file", "karox.repo.write_file"],
                )
            ]
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_openapi_wire_auth_schema_call_idempotency_and_rotation(self) -> None:
        active = {"token": "first-wire-token"}
        diagnostics = {
            "schema_version": 1,
            "available_tools": [
                "karox.repo.read_file",
                "karox.repo.write_file",
            ],
            "disabled_tools": [
                {
                    "name": "karox.checks.run",
                    "reason": "no approved verification-command allowlist",
                }
            ],
            "effective_deadline_seconds": 600.0,
            "tunnel": "cloudflare",
            "url_stability": "ephemeral",
        }
        server = _WireServer(
            build_openapi_bridge_app(
                self.runtime,
                lambda: active["token"],
                diagnostics=diagnostics,
            )
        )
        base = f"http://127.0.0.1:{server.port}"
        try:
            with httpx.Client(base_url=base, timeout=15.0) as client:
                # The schema names every exposed tool and its description, so it
                # costs the same credential as calling one.
                self.assertEqual(client.get("/openapi.json").status_code, 401)
                headers = {"Authorization": "Bearer first-wire-token"}
                schema = client.get("/openapi.json", headers=headers)
                self.assertEqual(schema.status_code, 200)
                paths = schema.json()["paths"]
                self.assertIn("/tools/karox.repo.read_file", paths)
                self.assertIn("/diagnostics", paths)
                self.assertEqual(client.get("/diagnostics").status_code, 401)
                diagnostics_response = client.get("/diagnostics", headers=headers)
                self.assertEqual(diagnostics_response.status_code, 200)
                self.assertEqual(diagnostics_response.json(), diagnostics)
                self.assertTrue(
                    paths["/tools/karox.repo.write_file"]["post"]["parameters"][0][
                        "required"
                    ]
                )
                self.assertEqual(client.get("/health").status_code, 401)
                session = client.get("/session", headers=headers)
                self.assertEqual(session.status_code, 200)
                self.assertEqual(session.json()["session_id"], "wire")
                self.assertEqual(
                    session.json()["repository"], str(self.repository.resolve())
                )
                response = client.post(
                    "/tools/karox.repo.read_file",
                    headers=headers,
                    json={"path": "sample.txt"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()["data"]["content"], "wire\n")
                self.assertEqual(
                    client.post(
                        "/tools/karox.repo.write_file",
                        headers=headers,
                        json={"path": "sample.txt", "content": "changed\n"},
                    ).status_code,
                    400,
                )
                mutation_headers = {
                    **headers,
                    "X-KaroX-Idempotency-Key": "openapi-write-1",
                }
                response = client.post(
                    "/tools/karox.repo.write_file",
                    headers=mutation_headers,
                    json={"path": "sample.txt", "content": "changed\n"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                active["token"] = "second-wire-token"
                self.assertEqual(client.get("/health", headers=headers).status_code, 401)
                self.assertEqual(
                    client.get(
                        "/health",
                        headers={"X-API-Key": "second-wire-token"},
                    ).status_code,
                    200,
                )
        finally:
            server.close()

    def test_streamable_http_mcp_exposes_real_core_tool(self) -> None:
        token = "mcp-core-wire-token"
        diagnostics = {
            "schema_version": 1,
            "available_tools": ["karox.repo.read_file"],
            "disabled_tools": [
                {
                    "name": "karox.checks.run",
                    "reason": "no approved verification-command allowlist",
                }
            ],
            "effective_deadline_seconds": 600.0,
        }
        with patch.dict(
            os.environ,
            {"KAROX_BRIDGE_DIAGNOSTICS_JSON": json.dumps(diagnostics)},
            clear=False,
        ):
            server = _WireServer(build_proxy_asgi_app(self.runtime, token))
        backend = _FakeCredentialBackend()
        credentials = McpCredentialStore(backend=backend)
        info = credentials.set("core-wire", token)
        registry = McpRegistry(self.root / "mcp-registry.json")
        record = McpServerRecord(
            server_id="karox-core",
            namespace="karox-core",
            transport="streamable_http",
            url=f"http://127.0.0.1:{server.port}/mcp",
            credential_ref=info["reference"],
            credential_target="Authorization",
            read_only_tools=(
                "karox.repo.read_file",
                "karox.bridge.diagnostics",
            ),
            timeout_seconds=15.0,
        )
        registry.put(record)
        client = McpClient(registry, credentials)
        try:
            tools = client.discover(record.server_id, self.repository)
            descriptor = next(
                item for item in tools if item.remote_name == "karox.repo.read_file"
            )
            diagnostics_descriptor = next(
                item
                for item in tools
                if item.remote_name == "karox.bridge.diagnostics"
            )
            self.assertTrue(diagnostics_descriptor.read_only)
            diagnostics_result = _call_mcp_tool(
                record.url,
                token,
                "karox.bridge.diagnostics",
                {},
            )
            self.assertFalse(diagnostics_result.isError)
            live_diagnostics = diagnostics_result.structuredContent
            self.assertIsInstance(live_diagnostics, dict)
            for key, value in diagnostics.items():
                self.assertEqual(live_diagnostics[key], value)
            transport_runtime = live_diagnostics["transport_runtime"]
            self.assertGreaterEqual(transport_runtime["requests_started"], 1)
            self.assertGreaterEqual(
                transport_runtime["requests_started"],
                transport_runtime["responses_started"],
            )
            self.assertNotIn(token, json.dumps(live_diagnostics, ensure_ascii=False))
            result = client.call_record(
                record, descriptor, {"path": "sample.txt"}, self.repository
            )
            encoded = json.dumps(result)
            self.assertIn("wire", encoded)
            self.assertNotIn(token, encoded)
        finally:
            server.close()

    def test_transport_runtime_counts_arrival_before_mcp_handler(self) -> None:
        token = "transport-telemetry-token"
        app = build_proxy_asgi_app(
            self.runtime,
            token,
            diagnostics={"schema_version": 1},
        )
        unauthorized_request = _tools_call(
            "wrong-transport-token",
            "karox.repo.read_file",
            {"path": "sample.txt"},
        )
        diagnostics_request = _tools_call(
            token,
            "karox.bridge.diagnostics",
            {},
        )
        unauthorized, diagnostics_response = _wire_requests(
            app,
            [unauthorized_request, diagnostics_request],
        )
        self.assertEqual(unauthorized.status, 401)
        result = _jsonrpc_result(diagnostics_response)
        self.assertFalse(result["isError"], diagnostics_response.text)
        live = result["structuredContent"]
        transport = live["transport_runtime"]
        self.assertEqual(transport["requests_started"], 2)
        self.assertEqual(transport["authorized_requests"], 1)
        self.assertEqual(transport["unauthorized_requests"], 1)
        self.assertEqual(transport["responses_started"], 1)
        self.assertEqual(transport["responses_completed"], 1)
        self.assertEqual(transport["last_http_version"], "1.1")
        self.assertEqual(transport["last_method"], "POST")
        self.assertEqual(transport["last_status"], 401)
        encoded = json.dumps(live, ensure_ascii=False)
        for secret in (token, "wrong-transport-token", "sample.txt"):
            self.assertNotIn(secret, encoded)

    def test_mutating_mcp_call_without_meta_writes_once_over_two_attempts(self) -> None:
        token = "mcp-no-meta-wire-token"
        server = _WireServer(build_proxy_asgi_app(self.runtime, token))
        backend = _FakeCredentialBackend()
        credentials = McpCredentialStore(backend=backend)
        info = credentials.set("core-no-meta", token)
        registry = McpRegistry(self.root / "mcp-registry-no-meta.json")
        record = McpServerRecord(
            server_id="karox-core",
            namespace="karox-core",
            transport="streamable_http",
            url=f"http://127.0.0.1:{server.port}/mcp",
            credential_ref=info["reference"],
            credential_target="Authorization",
            read_only_tools=("karox.repo.read_file",),
            timeout_seconds=15.0,
        )
        registry.put(record)
        client = McpClient(registry, credentials)
        try:
            tools = client.discover(record.server_id, self.repository)
            descriptor = next(
                item for item in tools if item.remote_name == "karox.repo.write_file"
            )
            # This client sends no _meta.karoxIdempotencyKey, which is the only
            # shape ChatGPT and Claude can send. The call has to land -- and the
            # attempt after it has to be recognised as the same mutation, not
            # applied a second time.
            arguments = {"path": "sample.txt", "content": "no-meta-A\n"}
            first = client.call_record(
                record, descriptor, dict(arguments), self.repository
            )
            self.assertFalse(
                first["result"]["structuredContent"]["idempotent_replay"]
            )
            self.assertEqual(
                (self.repository / "sample.txt").read_text(encoding="utf-8"),
                "no-meta-A\n",
            )
            second = client.call_record(
                record, descriptor, dict(arguments), self.repository
            )
            self.assertTrue(
                second["result"]["structuredContent"]["idempotent_replay"]
            )
            self.assertEqual(
                (self.repository / "sample.txt").read_text(encoding="utf-8"),
                "no-meta-A\n",
            )

            # A later external rewrite is no longer the state described by the
            # stored result. Replaying that result as a fresh success would lie to
            # the caller, so Core rejects the stale replay without overwriting the
            # newer bytes.
            (self.repository / "sample.txt").write_text("clobbered\n", encoding="utf-8")
            with self.assertRaises(McpRemoteToolError):
                client.call_record(
                    record, descriptor, dict(arguments), self.repository
                )
            self.assertEqual(
                (self.repository / "sample.txt").read_text(encoding="utf-8"),
                "clobbered\n",
            )
        finally:
            server.close()

    def test_invalid_explicit_idempotency_key_is_rejected_and_valid_key_takes_priority(
        self,
    ) -> None:
        token = "mcp-meta-wire-token"
        server = _WireServer(build_proxy_asgi_app(self.runtime, token))
        url = f"http://127.0.0.1:{server.port}/mcp"
        arguments = {"path": "sample.txt", "content": "rejected\n"}
        try:
            empty = _call_mcp_tool(
                url,
                token,
                "karox.repo.write_file",
                arguments,
                meta={"karoxIdempotencyKey": ""},
            )
            self.assertTrue(empty.isError)
            self.assertIn(
                "must be a string of 1-256 characters", _result_text(empty)
            )
            self.assertEqual(
                (self.repository / "sample.txt").read_text(encoding="utf-8"),
                "wire\n",
            )

            too_long = _call_mcp_tool(
                url,
                token,
                "karox.repo.write_file",
                arguments,
                meta={"karoxIdempotencyKey": "x" * 257},
            )
            self.assertTrue(too_long.isError)
            self.assertIn(
                "must be a string of 1-256 characters", _result_text(too_long)
            )
            self.assertEqual(
                (self.repository / "sample.txt").read_text(encoding="utf-8"),
                "wire\n",
            )

            # An explicitly supplied valid key still takes priority over the
            # generated fallback: repeating the same key replays the first
            # result instead of executing a second time. (Core rejects a
            # repeated key with different input, so the replay uses identical
            # arguments -- the point is that the supplied key is honoured as
            # the dedup key, not replaced by a fresh generated one.)
            first = _call_mcp_tool(
                url,
                token,
                "karox.repo.write_file",
                {"path": "sample.txt", "content": "explicit-A\n"},
                meta={"karoxIdempotencyKey": "explicit-wire-1"},
            )
            self.assertFalse(first.isError)
            self.assertTrue(first.structuredContent["data"]["changed"])
            self.assertEqual(
                (self.repository / "sample.txt").read_text(encoding="utf-8"),
                "explicit-A\n",
            )
            second = _call_mcp_tool(
                url,
                token,
                "karox.repo.write_file",
                {"path": "sample.txt", "content": "explicit-A\n"},
                meta={"karoxIdempotencyKey": "explicit-wire-1"},
            )
            self.assertFalse(second.isError)
            self.assertTrue(second.structuredContent["idempotent_replay"])
            self.assertEqual(
                (self.repository / "sample.txt").read_text(encoding="utf-8"),
                "explicit-A\n",
            )
        finally:
            server.close()


class BridgeWireSecurityTests(unittest.TestCase):
    """ASGI-level coverage for the bridge's public HTTP boundary."""

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
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
        self.runtime = CompositeHostedBridge(
            [
                CoreToolBridge(
                    self.repository,
                    self.sessions,
                    "origin",
                    ["karox.repo.read_file", "karox.repo.write_file"],
                )
            ]
        )
        self.token = "origin-wire-token"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_foreign_origin_is_rejected_before_the_tool_runs(self) -> None:
        app = build_proxy_asgi_app(self.runtime, self.token)
        # The 421 below is this test's point; capture the guard's console
        # diagnostic so intentional attack traffic does not pollute the
        # release log, and assert it instead of discarding it.
        diagnostics = io.StringIO()
        with contextlib.redirect_stderr(diagnostics):
            rejected, accepted = _wire_requests(
                app,
                [
                    _tools_call(
                        self.token,
                        "karox.repo.read_file",
                        {"path": "sample.txt"},
                        origin="https://attacker.example",
                    ),
                    _tools_call(
                        self.token,
                        "karox.repo.read_file",
                        {"path": "sample.txt"},
                        origin="http://127.0.0.1:8765",
                        meta={"karoxIdempotencyKey": "read-1"},
                    ),
                ],
            )
        self.assertEqual(rejected.status, 421)
        self.assertEqual(accepted.status, 200)
        self.assertIn("[karox-rebind] 421", diagnostics.getvalue())

    def test_foreign_host_is_rejected_on_both_wires(self) -> None:
        import anyio

        mcp_app = build_proxy_asgi_app(self.runtime, self.token)
        openapi_app = build_openapi_bridge_app(self.runtime, self.token)
        headers = [
            ("host", "rebound.attacker.example"),
            ("authorization", f"Bearer {self.token}"),
            ("accept", "application/json"),
        ]
        created_streams: list[Any] = []
        lifespan_events: list[str] = []
        original_pair = _memory_stream_pair

        def captured_pair() -> tuple[Any, Any]:
            pair = original_pair()
            created_streams.extend(pair)
            return pair

        async def tracked_mcp_app(
            scope: dict[str, Any], receive: Any, send: Any
        ) -> None:
            if scope["type"] != "lifespan":
                await mcp_app(scope, receive, send)
                return

            async def tracked_receive() -> dict[str, Any]:
                message = await receive()
                lifespan_events.append(message["type"])
                return message

            await mcp_app(scope, tracked_receive, send)

        diagnostics = io.StringIO()
        with patch(f"{__name__}._memory_stream_pair", side_effect=captured_pair):
            with contextlib.redirect_stderr(diagnostics):
                (mcp_response,) = _wire_requests(
                    tracked_mcp_app,
                    [{"method": "GET", "path": "/mcp", "headers": headers}],
                )
                (openapi_response,) = _wire_requests(
                    openapi_app,
                    [{"method": "GET", "path": "/health", "headers": headers}],
                )
        self.assertEqual(mcp_response.status, 421)
        self.assertEqual(openapi_response.status, 421)
        self.assertIn("[karox-rebind] 421", diagnostics.getvalue())
        self.assertEqual(
            lifespan_events,
            ["lifespan.startup", "lifespan.shutdown"],
        )
        self.assertEqual(len(created_streams), 8)
        for stream in created_streams:
            if hasattr(stream, "send_nowait"):
                with self.assertRaises(anyio.ClosedResourceError):
                    stream.send_nowait({})
            else:
                with self.assertRaises(anyio.ClosedResourceError):
                    stream.receive_nowait()

    def test_declared_public_host_is_accepted_and_trailing_slash_is_served(
        self,
    ) -> None:
        app = build_proxy_asgi_app(
            self.runtime, self.token, allowed_hosts=("bridge.trycloudflare.com",)
        )
        request = _tools_call(
            self.token,
            "karox.repo.read_file",
            {"path": "sample.txt"},
            host="bridge.trycloudflare.com",
            origin="https://bridge.trycloudflare.com",
            meta={"karoxIdempotencyKey": "read-2"},
        )
        request["path"] = "/mcp/"
        (response,) = _wire_requests(app, [request])
        self.assertEqual(response.status, 200, response.text)
        self.assertFalse(_jsonrpc_result(response)["isError"])

    def test_a_mutating_call_without_meta_is_served_and_deduplicated(self) -> None:
        # ChatGPT and Claude send no `_meta`, so this is the only shape their write
        # calls ever have. Refusing it made `--write` advertise tools that could
        # never run; the key is derived from the call instead.
        app = build_proxy_asgi_app(self.runtime, self.token)

        def write() -> dict[str, Any]:
            return _tools_call(
                self.token,
                "karox.repo.write_file",
                {"path": "sample.txt", "content": "rebound\n"},
            )

        # Both attempts go down one connection, because the retry a lost response
        # produces is the case this has to get right.
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
                _tools_call(
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
                _tools_call(
                    self.token,
                    "karox.repo.write_file",
                    {"path": "sample.txt", "content": "named\n"},
                    meta={"karoxIdempotencyKey": "write-1"},
                ),
                # A key that is present but unusable is still an error: deriving
                # one here would paper over a client bug.
                _tools_call(
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

    def test_tool_failure_never_reflects_a_filesystem_path(self) -> None:
        secret_path = "/home/user/x"
        app = build_proxy_asgi_app(
            _RaisingRuntime(FileNotFoundError(secret_path)), self.token
        )
        (response,) = _wire_requests(
            app,
            [_tools_call(self.token, "karox.repo.read_file", {"path": "sample.txt"})],
        )
        result = _jsonrpc_result(response)
        self.assertTrue(result["isError"])
        self.assertEqual(result["structuredContent"]["error_code"], "not_found")
        for fragment in ("/home/user/x", "/home/user", "home", "user"):
            self.assertNotIn(fragment, response.text)

    def test_openapi_failure_never_reflects_a_filesystem_path(self) -> None:
        secret_path = "/home/user/x"
        request = {
            "method": "POST",
            "path": "/tools/karox.repo.read_file",
            "headers": [
                ("host", "127.0.0.1:8765"),
                ("authorization", f"Bearer {self.token}"),
                ("content-type", "application/json"),
            ],
            "body": b"{}",
        }
        (missing,) = _wire_requests(
            build_openapi_bridge_app(
                _RaisingRuntime(FileNotFoundError(secret_path)), self.token
            ),
            [dict(request)],
        )
        self.assertEqual(missing.status, 404)
        self.assertEqual(missing.json()["error_code"], "not_found")

        (failed,) = _wire_requests(
            build_openapi_bridge_app(
                _RaisingRuntime(RuntimeError(f"{secret_path} exploded")), self.token
            ),
            [dict(request)],
        )
        self.assertEqual(failed.status, 500)
        self.assertEqual(failed.json()["error_code"], "internal")
        # error_type named the exception class, which is a second thing the
        # third-party agent has no business learning about this host.
        self.assertNotIn("error_type", failed.json())
        for fragment in ("/home/user/x", "/home/user", "home", "user"):
            self.assertNotIn(fragment, missing.text)
            self.assertNotIn(fragment, failed.text)

    def test_bare_token_and_bearer_token_both_authorize(self) -> None:
        # ClickUp's "Authorization header" option asks the user for a *token*
        # ("you'll paste in a token generated from your MCP server's settings"),
        # so whether ``Bearer `` reaches the wire is the peer's decision, not the
        # user's.  Requiring the scheme made that peer detail the difference
        # between a working connection and a 401 that reads exactly like a wrong
        # secret.  Both spellings must authorize -- and a different scheme must
        # still be refused rather than misread as a bare token with a space.
        def _authorized_as(value: str) -> dict[str, Any]:
            request = _tools_call(
                self.token, "karox.repo.read_file", {"path": "sample.txt"}
            )
            request["headers"] = [
                (name, value if name == "authorization" else existing)
                for name, existing in request["headers"]
            ]
            return request

        cases: list[tuple[str, str, int]] = [
            # Both accepted spellings of the same credential.
            ("prefixed token", f"Bearer {self.token}", 200),
            ("bare token", self.token, 200),
            # Case-insensitive scheme, and surrounding whitespace tolerated.
            ("lowercase scheme", f"bearer {self.token}", 200),
            ("padded value", f"  Bearer  {self.token}  ", 200),
            # A wrong secret is still rejected in either spelling -- widening the
            # encoding must not widen the check.
            ("prefixed wrong secret", "Bearer wrong-secret", 401),
            ("bare wrong secret", "wrong-secret", 401),
            # A different scheme is refused, not read as a bare token with a space.
            ("basic scheme", f"Basic {self.token}", 401),
            ("token scheme", f"Token {self.token}", 401),
            # Nothing at all is refused.
            ("empty header", "", 401),
            ("scheme with no credential", "Bearer ", 401),
        ]

        # One app and one _wire_requests call: the session manager underneath
        # refuses to run twice, and driving the lifespan per case would test the
        # harness rather than the header parsing.
        responses = _wire_requests(
            build_proxy_asgi_app(self.runtime, self.token),
            [_authorized_as(value) for _, value, _ in cases],
        )

        for (label, _, expected), response in zip(cases, responses):
            with self.subTest(authorization=label):
                self.assertEqual(response.status, expected)

    def test_non_ascii_bearer_credential_is_unauthorized(self) -> None:
        app = build_proxy_asgi_app(self.runtime, self.token)
        mcp_request = _tools_call(
            self.token, "karox.repo.read_file", {"path": "sample.txt"}
        )
        mcp_request["headers"] = [
            (name, "Bearer é" if name == "authorization" else value)
            for name, value in mcp_request["headers"]
        ]
        (mcp_response,) = _wire_requests(app, [mcp_request])
        self.assertEqual(mcp_response.status, 401)

        openapi_app = build_openapi_bridge_app(self.runtime, self.token)
        (openapi_response,) = _wire_requests(
            openapi_app,
            [
                {
                    "method": "GET",
                    "path": "/health",
                    "headers": [
                        ("host", "127.0.0.1:8765"),
                        ("authorization", "Bearer é"),
                        ("accept", "application/json"),
                    ],
                }
            ],
        )
        self.assertEqual(openapi_response.status, 401)

    def test_unauthorized_declares_the_scheme_it_wants(self) -> None:
        """A 401 on a bearer-protected wire must name its scheme.

        RFC 6750 requires ``WWW-Authenticate`` on a 401 from a bearer-protected
        resource, and a client that discovers auth by probing reads exactly that
        header. Without it the bridge answers "no" without saying what a "yes"
        would look like: ClickUp's connector, set to its default OAuth, probed
        the discovery endpoints, got 404s and a bare 401, and reported
        "Authentication method not supported by this MCP Server" with nothing
        pointing at the scheme that would have worked.
        """
        app = build_proxy_asgi_app(
            self.runtime,
            self.token,
            unauthorized_headers={"WWW-Authenticate": 'Bearer realm="KaroX bridge"'},
        )
        request = _tools_call(self.token, "karox.repo.read_file", {"path": "sample.txt"})
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


class DerivedIdempotencyKeyTests(unittest.TestCase):
    """The derived key is only useful if it is stable across a client's retries."""

    def test_the_same_call_derives_the_same_key_whatever_the_argument_order(
        self,
    ) -> None:
        # A retry is not required to serialize its arguments in the order the first
        # attempt used, and two keys for one mutation would apply it twice.
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
    """Both wires answer from one fixed error table, so it has to be complete.

    The OpenAPI wire looks a code up in its own status map and its message in the
    table shared with the MCP wire. A code present in one and absent from the
    other is a KeyError raised while building an error response -- a 500 with a
    traceback, from the path whose whole job is to answer cleanly.
    """

    def test_every_code_has_both_a_message_and_a_status(self) -> None:
        from karox.openapi_bridge import _ERROR_STATUS
        from karox.proxy_server import BRIDGE_ERROR_MESSAGES

        self.assertEqual(
            sorted(_ERROR_STATUS), sorted(BRIDGE_ERROR_MESSAGES),
            "a bridge error code exists in only one of the two tables",
        )

    def test_a_fixed_detail_replaces_the_wording_but_not_the_code(self) -> None:
        """The specific reason is kept; the machine-readable code is still there.

        Three errors on the tool endpoint used to carry no `error_code` at all, so
        a client reading it found it absent for exactly the failures it could have
        corrected.
        """
        from karox.openapi_bridge import bridge_error_response

        response = bridge_error_response("invalid_request", "request body must be JSON")
        payload = json.loads(bytes(response.body))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["error_code"], "invalid_request")
        self.assertEqual(payload["error"], "request body must be JSON")
        self.assertIs(payload["ok"], False)

    def test_an_error_response_never_reflects_an_exception(self) -> None:
        """The generic wording is fixed text, not whatever was raised."""
        from karox.openapi_bridge import bridge_error_response
        from karox.proxy_server import BRIDGE_ERROR_MESSAGES

        payload = json.loads(bytes(bridge_error_response("internal").body))
        self.assertEqual(payload["error"], BRIDGE_ERROR_MESSAGES["internal"])
        self.assertNotIn("error_type", payload)


if __name__ == "__main__":
    unittest.main()
