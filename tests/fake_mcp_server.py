"""Minimal stdio MCP server for client tests: tools `echo` and `add` (read-only)."""

import json
import sys

TOOLS = [
    {
        "name": "echo",
        "description": "Echo text back.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    },
    {
        "name": "add",
        "description": "Add two numbers.",
        "inputSchema": {
            "type": "object",
            "properties": {"a": {"type": "number"}, "b": {"type": ["number", "null"]}},
            "required": ["a"],
        },
        "annotations": {"readOnlyHint": True},
    },
]

print("fake server starting (a log line that is not JSON)", flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method, rid = message["method"], message["id"]
    params = message.get("params") or {}
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0"},
        }
    elif method == "tools/list":
        if params.get("cursor"):
            result = {"tools": TOOLS[1:]}
        else:
            result = {"tools": TOOLS[:1], "nextCursor": "page2"}
    elif method == "tools/call":
        args = params.get("arguments") or {}
        if params["name"] == "echo":
            result = {"content": [{"type": "text", "text": "echo: " + args["text"]}]}
        elif params["name"] == "add":
            result = {"content": [], "structuredContent": {"sum": args["a"] + (args.get("b") or 0)}}
        else:
            result = {"content": [{"type": "text", "text": "no such tool"}], "isError": True}
    else:
        print(
            json.dumps(
                {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "not found"}}
            ),
            flush=True,
        )
        continue
    print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": result}), flush=True)
