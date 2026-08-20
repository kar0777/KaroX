from __future__ import annotations

import asyncio
import json
from typing import Any, Optional

from _support import SRC  # noqa: F401
from karox.proxy import ProxyToolDescriptor
from karox.proxy_server import MODERN_MCP_PROTOCOL_VERSION, build_proxy_asgi_app
from test_hosted_bridge import _asgi_request


class _ModernRuntime:
    def descriptors(self) -> list[ProxyToolDescriptor]:
        return [
            ProxyToolDescriptor(
                name="karox.repo.read_file",
                description="Synthetic read",
                input_schema={
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
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
        del idempotency_key, deadline_seconds
        return {"ok": True, "tool": tool_name, "path": arguments.get("path")}


def _modern_request(method: str, params: dict[str, Any], *, name: str | None = None) -> dict[str, Any]:
    headers = [
        ("host", "127.0.0.1:8765"),
        ("authorization", "Bearer modern-test-token"),
        ("content-type", "application/json"),
        ("accept", "application/json, text/event-stream"),
        ("mcp-protocol-version", MODERN_MCP_PROTOCOL_VERSION),
        ("mcp-method", method),
    ]
    if name is not None:
        headers.append(("mcp-name", name))
    meta = dict(params.get("_meta") or {})
    meta.update(
        {
            "io.modelcontextprotocol/protocolVersion": MODERN_MCP_PROTOCOL_VERSION,
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {"name": "test-client", "version": "1"},
        }
    )
    params = dict(params)
    params["_meta"] = meta
    return {
        "method": "POST",
        "path": "/mcp",
        "headers": headers,
        "body": json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode("utf-8"),
    }


def test_modern_discover_lists_2026_and_tools_capability() -> None:
    app = build_proxy_asgi_app(_ModernRuntime(), "modern-test-token")
    response = asyncio.run(_asgi_request(app, _modern_request("server/discover", {})))
    assert response.status == 200
    result = response.json()["result"]
    assert result["supportedVersions"][0] == MODERN_MCP_PROTOCOL_VERSION
    assert result["capabilities"]["tools"] == {}
    assert result["resultType"] == "complete"


def test_modern_tools_list_and_call_use_same_runtime() -> None:
    app = build_proxy_asgi_app(_ModernRuntime(), "modern-test-token")
    listed = asyncio.run(_asgi_request(app, _modern_request("tools/list", {})))
    assert listed.status == 200
    tools = listed.json()["result"]["tools"]
    assert [item["name"] for item in tools] == ["karox_repo_read_file"]

    called = asyncio.run(
        _asgi_request(
            app,
            _modern_request(
                "tools/call",
                {"name": "karox_repo_read_file", "arguments": {"path": "README.md"}},
                name="karox_repo_read_file",
            ),
        )
    )
    assert called.status == 200
    result = called.json()["result"]
    assert result["resultType"] == "complete"
    assert result["isError"] is False
    assert result["structuredContent"] == {
        "ok": True,
        "tool": "karox.repo.read_file",
        "path": "README.md",
    }


def test_modern_header_mismatch_fails_without_execution() -> None:
    app = build_proxy_asgi_app(_ModernRuntime(), "modern-test-token")
    request = _modern_request("tools/list", {})
    request["headers"] = [
        (header, "tools/call" if header == "mcp-method" else value)
        for header, value in request["headers"]
    ]
    response = asyncio.run(_asgi_request(app, request))
    assert response.status == 400
    assert response.json()["error"]["code"] == -32600
