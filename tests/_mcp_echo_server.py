"""Deterministic stdio MCP server used by the Phase 5 end-to-end tests.

This is a *real* MCP server built on the installed ``mcp`` SDK.  It is launched
as a subprocess by ``McpClient`` so the tests exercise the genuine stdio
transport, JSON-RPC handshake, tool discovery, and tool invocation -- not a
mock.

The server exposes two tools:

* ``echo``  -- read-only, returns ``echo: <message>``.
* ``write_note`` -- mutating (not declared read-only), returns ``noted: <name>``.

``KAROX_MCP_MODE`` selects the ``echo`` input schema so the schema-change
detection test can make a discovered tool descriptor diverge from a stored
selection without editing the server code.
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
import mcp.types as types


server = Server("karox-echo-test")


def _mode() -> str:
    """Resolve the schema mode without touching the registry configuration.

    The registry record passes ``KAROX_MCP_MODE_FILE`` (a stable file path) so
    the server can change its ``echo`` schema between launches while the
    registry digest stays constant -- this lets the schema-change detection
    test diverge a stored descriptor from a freshly discovered one.
    """
    file_path = os.environ.get("KAROX_MCP_MODE_FILE")
    if file_path:
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                value = handle.read().strip()
            if value:
                return value
        except OSError:
            pass
    return os.environ.get("KAROX_MCP_MODE", "v1")


def _echo_schema() -> dict[str, object]:
    if _mode() == "v2":
        return {
            "type": "object",
            "properties": {
                "message": {"type": "string"},
                "lang": {"type": "string"},
            },
            "required": ["message", "lang"],
            "additionalProperties": False,
        }
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
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="echo",
            description="Echo a message back to the caller.",
            inputSchema=_echo_schema(),
        ),
        types.Tool(
            name="write_note",
            description="Record a named note.",
            inputSchema=_note_schema(),
        ),
        types.Tool(
            name="reflect_secret",
            description="Return the injected test credential.",
            inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict[str, object]) -> list[types.TextContent]:
    if name == "echo":
        message = arguments.get("message", "")
        return [types.TextContent(type="text", text=f"echo: {message}")]
    if name == "write_note":
        noted = arguments.get("name", "")
        return [types.TextContent(type="text", text=f"noted: {noted}")]
    if name == "reflect_secret":
        return [
            types.TextContent(
                type="text", text=os.environ.get("KAROX_TEST_SECRET", "missing")
            )
        ]
    raise ValueError(f"unknown tool: {name}")


async def main() -> None:
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
