"""Offline smoke of an installed karox: version, chat CLI, MCP stdio handshake.

Usage: python scripts/smoke_installed.py PATH/TO/karox
No network and no model are needed: the MCP server answers initialize and tools/list
without calling a provider.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile


def main() -> int:
    exe = sys.argv[1]
    version = subprocess.run([exe, "--version"], capture_output=True, text=True, check=True)
    assert version.stdout.startswith("karox "), version.stdout
    chat = subprocess.run([exe, "chat", "--help"], capture_output=True, text=True, check=True)
    assert "--resume" in chat.stdout, chat.stdout
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "smoke", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    stdin = "".join(json.dumps(r) + "\n" for r in requests)
    with tempfile.TemporaryDirectory() as home:
        proc = subprocess.run(
            [exe, "mcp", "--model", "smoke", "--home", home],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )
    replies = {r.get("id"): r for r in map(json.loads, proc.stdout.splitlines()) if "id" in r}
    assert replies[1]["result"]["serverInfo"]["name"] == "karox", replies
    tools = {t["name"] for t in replies[2]["result"]["tools"]}
    assert {"karox_run", "karox_status", "karox_diff"} <= tools, tools
    print(f"ok: {version.stdout.strip()}, chat CLI, MCP stdio with {len(tools)} tools")
    return 0


if __name__ == "__main__":
    sys.exit(main())
