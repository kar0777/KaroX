from __future__ import annotations

import asyncio
from typing import Any, Optional
from unittest.mock import patch

from _support import SRC  # noqa: F401
from karox.proxy import ProxyToolDescriptor
from karox.proxy_server import build_proxy_asgi_app, transport_activity_snapshot
from test_hosted_bridge import _jsonrpc_result, _tools_call, _wire_requests


class _TelemetryRuntime:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True

    def descriptors(self) -> list[ProxyToolDescriptor]:
        return [
            ProxyToolDescriptor(
                name="karox.repo.read_file",
                description="Synthetic read",
                input_schema={"type": "object", "properties": {}},
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
        del tool_name, arguments, idempotency_key, deadline_seconds
        return {"ok": True, "summary": "done"}


def test_transport_diagnostics_correlate_http_and_mcp_tool_completion() -> None:
    token = "telemetry-token"
    process_before = transport_activity_snapshot()
    app = build_proxy_asgi_app(
        _TelemetryRuntime(),
        token,
        diagnostics={"schema_version": 1},
    )
    normal = _tools_call(token, "karox.repo.read_file", {})
    diagnostic = _tools_call(token, "karox.bridge.diagnostics", {})
    normal_response, diagnostic_response = _wire_requests(app, [normal, diagnostic])

    assert normal_response.status == 200
    assert not _jsonrpc_result(normal_response)["isError"]
    diagnostic_result = _jsonrpc_result(diagnostic_response)
    assert not diagnostic_result["isError"]
    transport = diagnostic_result["structuredContent"]["transport_runtime"]

    # The diagnostics call itself has started but cannot count as completed until
    # after its own response body is sent. The previous tool call is complete.
    assert transport["requests_started"] == 2
    assert transport["responses_completed"] == 1
    assert transport["mcp_tool_calls_started"] == 2
    assert transport["mcp_tool_calls_completed"] == 1
    assert transport["last_request_duration_ms"] >= 0
    assert transport["max_request_duration_ms"] >= transport["last_request_duration_ms"]
    assert transport["last_response_body_bytes"] > 0
    assert transport["max_response_body_bytes"] >= transport["last_response_body_bytes"]
    assert transport["last_mcp_tool_call_started_at"] is not None
    assert transport["last_mcp_tool_call_completed_at"] is not None

    process_after = transport_activity_snapshot()
    assert process_after["active_requests"] == process_before["active_requests"]
    assert (
        process_after["responses_completed"]
        == process_before["responses_completed"] + 2
    )


def test_process_activity_marks_completion_only_after_final_asgi_send_returns() -> None:
    runtime = _TelemetryRuntime()
    app = build_proxy_asgi_app(runtime, "wire-order-token")
    before = transport_activity_snapshot()
    observed_inside_final_send: list[dict[str, int]] = []

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "http",
        "method": "POST",
        "path": "/mcp",
        "raw_path": b"/mcp",
        "query_string": b"",
        "headers": [(b"host", b"localhost")],
        "client": ("127.0.0.1", 45678),
        "server": ("127.0.0.1", 8765),
    }

    async def receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, Any]) -> None:
        if message.get("type") == "http.response.body" and not message.get("more_body", False):
            observed_inside_final_send.append(transport_activity_snapshot())

    asyncio.run(app(scope, receive, send))

    assert len(observed_inside_final_send) == 1
    inside = observed_inside_final_send[0]
    assert inside["active_requests"] == before["active_requests"] + 1
    assert inside["responses_completed"] == before["responses_completed"]
    after = transport_activity_snapshot()
    assert after["active_requests"] == before["active_requests"]
    assert after["responses_completed"] == before["responses_completed"] + 1


def test_transport_diagnostics_reads_saved_supervisor_state_live() -> None:
    token = "telemetry-token"
    with patch(
        "karox.saved_bridge_supervisor.saved_bridge_supervisor_status",
        return_value={
            "desired_running": True,
            "supervisor_alive": True,
            "supervisor_pid": 7001,
            "heartbeat_at": 123.0,
            "last_owner_pid": 7002,
            "restart_count": 3,
            "last_restart_at": 122.0,
            "last_error": None,
        },
    ):
        app = build_proxy_asgi_app(
            _TelemetryRuntime(),
            token,
            diagnostics={"schema_version": 1, "saved_profile": "hyperagent-auto"},
        )
        response = _wire_requests(
            app,
            [_tools_call(token, "karox.bridge.diagnostics", {})],
        )[0]

    result = _jsonrpc_result(response)
    assert not result["isError"]
    supervisor = result["structuredContent"]["durable_supervisor"]
    assert supervisor["desired_running"] is True
    assert supervisor["supervisor_alive"] is True
    assert supervisor["supervisor_pid"] == 7001
    assert supervisor["last_owner_pid"] == 7002
    assert supervisor["restart_count"] == 3
    assert supervisor["last_error"] is None


def test_lifespan_shutdown_closes_runtime_resources() -> None:
    runtime = _TelemetryRuntime()
    app = build_proxy_asgi_app(runtime, "lifecycle-token")
    messages = iter(
        [
            {"type": "lifespan.startup"},
            {"type": "lifespan.shutdown"},
        ]
    )
    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, Any]:
        return next(messages)

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    asyncio.run(
        app(
            {"type": "lifespan", "asgi": {"version": "3.0"}},
            receive,
            send,
        )
    )

    assert runtime.closed
    assert {item["type"] for item in sent} == {
        "lifespan.startup.complete",
        "lifespan.shutdown.complete",
    }
