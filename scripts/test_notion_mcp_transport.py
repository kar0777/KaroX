#!/usr/bin/env python3
from __future__ import annotations

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


def main() -> int:
    with tempfile.TemporaryDirectory() as temp:
        temp_path = Path(temp)
        os.environ["REPO_ROOT"] = str(temp_path)
        os.environ["REPO_TOOLS_API_KEY"] = "test-session-key-0123456789"
        os.environ["REPO_TOOLS_MODE"] = "read_only"
        os.environ["REPO_TOOLS_HOME"] = str(temp_path / "home")
        os.environ["REPO_TOOLS_LOG_FILE"] = str(temp_path / "home" / "audit.jsonl")
        os.environ["REPO_TOOLS_RUNS_DIR"] = str(temp_path / "runs")

        import notion_gateway  # noqa: PLC0415

        async def probe(_request):
            return JSONResponse({"ok": True})

        app = Starlette(routes=[Route("/", probe)])
        app.add_middleware(notion_gateway.McpAuthMiddleware)

        with TestClient(app) as client:
            allowed = client.get(
                "/",
                headers={
                    "Host": "transformation-founded-kathy-lexmark.trycloudflare.com",
                    "Authorization": "Bearer test-session-key-0123456789",
                },
            )
            assert allowed.status_code == 200, allowed.text

            bad_host = client.get(
                "/",
                headers={
                    "Host": "attacker.example",
                    "Authorization": "Bearer test-session-key-0123456789",
                },
            )
            assert bad_host.status_code == 421, bad_host.text

            bad_token = client.get(
                "/",
                headers={
                    "Host": "valid-name.trycloudflare.com",
                    "Authorization": "Bearer wrong-key",
                },
            )
            assert bad_token.status_code == 401, bad_token.text

        security = notion_gateway.mcp.settings.transport_security
        assert security is not None
        assert security.enable_dns_rebinding_protection is False

    print("KaroX Notion MCP transport middleware checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
