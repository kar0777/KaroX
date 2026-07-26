"""Deterministic Streamable HTTP MCP server used by the Phase 5 end-to-end tests.

A *real* MCP streamable-http server built on the installed ``mcp`` SDK's
``StreamableHTTPSessionManager`` (which owns transport and session lifecycle)
and served by Uvicorn on an ephemeral loopback port.  ``McpClient`` therefore
exercises the genuine HTTP transport -- JSON-RPC initialize / tools list / tool
call over Streamable HTTP -- not a mock.

The server accepts a bearer token.  A request without the matching
``Authorization: Bearer <token>`` header is rejected with HTTP 401, which lets
the HTTP E2E prove that ``McpCredentialStore`` references are injected into the
transport headers rather than persisted in the registry.

It exposes the same two tools as the stdio echo server:

* ``echo``      -- read-only.
* ``write_note`` -- mutating.
"""

from __future__ import annotations

import json
import os

from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import TextContent, Tool
from starlette.responses import Response


# The expected bearer is read from the environment so the value never appears in
# the registry configuration or the test source.  Defaults to a test-only token.
def expected_token() -> str:
    return os.environ.get("KAROX_MCP_HTTP_TOKEN", "test-bearer-secret")


server = Server("karox-echo-http")


def _echo_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {"message": {"type": "string"}},
        "required": ["message"],
        "additionalProperties": False,
    }


def _note_schema() -> dict[str, object]:
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["name", "content"],
        "additionalProperties": False,
    }


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="echo",
            description="Echo a message back to the caller.",
            inputSchema=_echo_schema(),
        ),
        Tool(
            name="write_note",
            description="Record a named note.",
            inputSchema=_note_schema(),
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, object]) -> list[TextContent]:
    if name == "echo":
        return [TextContent(type="text", text=f"echo: {arguments.get('message', '')}")]
    if name == "write_note":
        return [TextContent(type="text", text=f"noted: {arguments.get('name', '')}")]
    raise ValueError(f"unknown tool: {name}")


def _authorized(scope: dict) -> bool:
    expected = f"Bearer {expected_token()}"
    for name, value in scope.get("headers", ()):  # type: ignore[union-attr]
        if name == b"authorization":
            return value.decode("latin-1") == expected
    return False


def build_asgi_app() -> "object":
    """Return a pure ASGI app: manual lifespan + exact ``/mcp`` path dispatch.

    A raw ASGI app (rather than a Starlette router) is used deliberately so the
    server answers ``POST /mcp`` exactly, without the trailing-slash redirect
    that a ``Mount`` would issue -- the MCP client disables redirect following.
    Lifespan is implemented by hand so ``manager.run()`` stays open for the
    whole process and shuts down cleanly on test teardown.
    """
    manager = StreamableHTTPSessionManager(
        app=server, json_response=True, stateless=True
    )

    async def asgi_app(scope: dict, receive: object, send: object) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            async with manager.run():
                while True:
                    message = await receive()  # type: ignore[misc]
                    if message["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif message["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
            return
        if scope_type != "http" or scope.get("path") != "/mcp":
            response = Response("not found", status_code=404)
            await response(scope, receive, send)  # type: ignore[arg-type]
            return
        if not _authorized(scope):
            response = Response(
                json.dumps({"error": "unauthorized"}),
                status_code=401,
                media_type="application/json",
            )
            await response(scope, receive, send)  # type: ignore[arg-type]
            return
        await manager.handle_request(scope, receive, send)  # type: ignore[arg-type]

    return asgi_app
