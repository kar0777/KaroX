"""Minimal stdio MCP server for client tests: tools `echo` and `add` (read-only)."""

import json
import sys
import time

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

CANCELLED = []
RESOURCES_ONLY = "--resources-only" in sys.argv  # no tools: tools/list is "not found"


def hidden_tool(name, args):
    """Unlisted tools that exercise the client's protocol handling."""
    if name == "ping_me":  # the server asks the client something and waits for the answers
        for sid, method in (("srv-1", "ping"), ("srv-2", "sampling/createMessage")):
            print(json.dumps({"jsonrpc": "2.0", "id": sid, "method": method}), flush=True)
        replies = [json.loads(sys.stdin.readline()) for _ in range(2)]
        return {"content": [{"type": "text", "text": json.dumps(replies)}]}
    if name == "slow":
        time.sleep(float(args.get("seconds", 1)))
        return {"content": [{"type": "text", "text": "slept"}]}
    if name == "picture":  # a 1x1 PNG, as a browser server returns a screenshot
        png = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9a"
            "wAAAABJRU5ErkJggg=="
        )
        return {
            "content": [
                {"type": "text", "text": "shot taken"},
                {"type": "image", "data": png, "mimeType": "image/png"},
            ]
        }
    if name == "cancelled":
        return {"content": [{"type": "text", "text": json.dumps(CANCELLED)}]}
    if name == "crash":
        sys.stderr.write("fake crashed on purpose\n")
        sys.stderr.flush()
        sys.exit(5)
    return None


sys.stderr.write("fake server stderr line\n")
print("fake server starting (a log line that is not JSON)", flush=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get("method") == "notifications/cancelled":
        CANCELLED.append((message.get("params") or {}).get("requestId"))
    if "id" not in message or "method" not in message:
        continue
    method, rid = message["method"], message["id"]
    params = message.get("params") or {}
    hidden = hidden_tool(params.get("name"), params.get("arguments") or {})
    if method == "tools/call" and hidden is not None:
        print(json.dumps({"jsonrpc": "2.0", "id": rid, "result": hidden}), flush=True)
        continue
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"resources": {}} if RESOURCES_ONLY else {"tools": {}},
            "serverInfo": {"name": "fake", "version": "0"},
        }
    elif method == "tools/list" and not RESOURCES_ONLY:
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

if "--linger" in sys.argv:  # a server that outlives its stdin, as node under npx.cmd can
    time.sleep(120)
