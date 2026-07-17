#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server"))

from mcp_host_security import is_allowed_mcp_host, normalize_host  # noqa: E402


def main() -> int:
    assert normalize_host("LOCALHOST:8000") == "localhost"
    assert normalize_host("[::1]:8123") == "::1"
    assert normalize_host("bad host") == ""
    assert normalize_host("evil.com/path") == ""

    for host in (
        "localhost",
        "localhost:8000",
        "127.0.0.1:49152",
        "[::1]:8000",
        "transformation-founded-kathy-lexmark.trycloudflare.com",
        "another-random-name.trycloudflare.com:443",
        "machine.tailnet-name.ts.net",
    ):
        assert is_allowed_mcp_host(host), f"expected allowed host: {host}"

    for host in (
        "",
        "trycloudflare.com",
        "ts.net",
        "eviltrycloudflare.com",
        "good.trycloudflare.com.evil.example",
        "evil.example",
        "evil.example:443",
        "user@localhost",
    ):
        assert not is_allowed_mcp_host(host), f"expected rejected host: {host}"

    previous = os.environ.get("KAROX_MCP_ALLOWED_HOSTS")
    try:
        os.environ["KAROX_MCP_ALLOWED_HOSTS"] = "mcp.example.com, second.example.net:443"
        assert is_allowed_mcp_host("mcp.example.com")
        assert is_allowed_mcp_host("second.example.net")
        assert not is_allowed_mcp_host("sub.mcp.example.com")
    finally:
        if previous is None:
            os.environ.pop("KAROX_MCP_ALLOWED_HOSTS", None)
        else:
            os.environ["KAROX_MCP_ALLOWED_HOSTS"] = previous

    gateway_source = (ROOT / "server" / "notion_gateway.py").read_text(encoding="utf-8")
    assert "TransportSecuritySettings(enable_dns_rebinding_protection=False)" in gateway_source
    assert "is_allowed_mcp_host(host)" in gateway_source
    assert "invalid_host" in gateway_source
    assert "_json_response(" in gateway_source and "421," in gateway_source
    assert "BaseHTTPMiddleware" not in gateway_source
    assert "async def __call__" in gateway_source

    print("KaroX MCP tunnel host security checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
