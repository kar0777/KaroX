#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

TOKEN = "test-session-key-0123456789"
HOST = "stable-device.example.ts.net"


def auth_headers(**extra: str) -> dict[str, str]:
    headers = {"Host": HOST, "Authorization": f"Bearer {TOKEN}"}
    headers.update(extra)
    return headers


def rpc_headers() -> dict[str, str]:
    return auth_headers(**{"Accept": "application/json, text/event-stream", "Content-Type": "application/json"})


def initialize_payload(request_id: int = 1) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "karox-transport-test", "version": "1.0"},
        },
    }


def tool_call(client: TestClient, protocol: str, request_id: int, name: str, arguments: dict[str, object]):
    return client.post(
        "/mcp",
        headers={**rpc_headers(), "MCP-Protocol-Version": protocol},
        json={
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        follow_redirects=False,
    )


def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        temp_path = Path(temp)
        sample = temp_path / "sample.txt"
        sample.write_text("alpha\nbeta marker\ngamma\n", encoding="utf-8")
        os.environ["REPO_ROOT"] = str(temp_path)
        os.environ["REPO_TOOLS_API_KEY"] = TOKEN
        os.environ["REPO_TOOLS_MODE"] = "full"
        os.environ["REPO_TOOLS_HOME"] = str(temp_path / "home")
        os.environ["REPO_TOOLS_LOG_FILE"] = str(temp_path / "home" / "audit.jsonl")
        os.environ["REPO_TOOLS_RUNS_DIR"] = str(temp_path / "runs")

        import notion_gateway  # noqa: PLC0415
        import notion_entry  # noqa: PLC0415

        async def probe(request):
            return JSONResponse({"ok": True, "path": request.url.path})

        raw_app = Starlette(routes=[Route("/mcp", probe, methods=["POST"])])
        app = notion_gateway.McpAuthMiddleware(raw_app)

        with TestClient(app) as client:
            allowed = client.post("/mcp", headers=auth_headers())
            assert allowed.status_code == 200, allowed.text
            trailing = client.post("/mcp/", headers=auth_headers(), follow_redirects=False)
            assert trailing.status_code == 200, trailing.text
            assert trailing.json()["path"] == "/mcp"
            get_probe = client.get("/mcp", headers=auth_headers())
            assert get_probe.status_code == 405, get_probe.text
            assert get_probe.headers.get("allow") == "POST, DELETE"
            bad_host = client.post("/mcp", headers={"Host": "attacker.example", "Authorization": f"Bearer {TOKEN}"})
            assert bad_host.status_code == 421, bad_host.text
            bad_token = client.post("/mcp", headers={"Host": HOST, "Authorization": "Bearer wrong-key"})
            assert bad_token.status_code == 401, bad_token.text

        with TestClient(notion_entry.app) as client:
            initialized = client.post(
                "/mcp", headers=rpc_headers(), json=initialize_payload(), follow_redirects=False
            )
            assert initialized.status_code == 200, initialized.text
            assert initialized.headers["content-type"].startswith("application/json")
            assert initialized.headers.get("cache-control") == "no-store"
            protocol = str(initialized.json()["result"]["protocolVersion"])

            tools = client.post(
                "/mcp",
                headers={**rpc_headers(), "MCP-Protocol-Version": protocol},
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                follow_redirects=False,
            )
            assert tools.status_code == 200, tools.text
            names = {item["name"] for item in tools.json()["result"]["tools"]}
            assert {
                "karox_ping",
                "karox_preflight",
                "karox_run",
                "karox_list_dir",
                "karox_search",
                "karox_read_file_range",
                "karox_read_files",
                "karox_apply_edits",
                "karox_run_checks",
            }.issubset(names)

            ping = tool_call(client, protocol, 3, "karox_ping", {})
            assert ping.status_code == 200, ping.text
            assert ping.json()["result"]["isError"] is False, ping.text

            listed = tool_call(client, protocol, 4, "karox_list_dir", {"path": "", "max_entries": 20})
            assert listed.status_code == 200, listed.text
            assert listed.json()["result"]["isError"] is False, listed.text

            searched = tool_call(client, protocol, 5, "karox_search", {"query": "marker", "glob": "*.txt"})
            assert searched.status_code == 200, searched.text
            assert searched.json()["result"]["isError"] is False, searched.text

            ranged = tool_call(
                client,
                protocol,
                6,
                "karox_read_file_range",
                {"path": "sample.txt", "start_line": 2, "end_line": 2},
            )
            assert ranged.status_code == 200, ranged.text
            assert ranged.json()["result"]["isError"] is False, ranged.text

            edit_payload = json.dumps([{"old": "beta marker", "new": "beta changed", "count": 1}])
            edited = tool_call(
                client,
                protocol,
                7,
                "karox_apply_edits",
                {"path": "sample.txt", "edits_json": edit_payload},
            )
            assert edited.status_code == 200, edited.text
            assert edited.json()["result"]["isError"] is False, edited.text
            assert "beta changed" in sample.read_text(encoding="utf-8")

        security = notion_gateway.mcp.settings.transport_security
        assert security is not None
        assert security.enable_dns_rebinding_protection is False
        assert notion_gateway.mcp.settings.stateless_http is True
        assert notion_gateway.mcp.settings.json_response is True

    print("KaroX Notion MCP transport and extended agent workflow checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
