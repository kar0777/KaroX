"""Host validation for KaroX remote MCP transports.

The MCP Python SDK enables localhost-only DNS-rebinding protection when FastMCP
uses its default 127.0.0.1 host. KaroX intentionally binds Uvicorn to localhost
and publishes it through an authenticated Cloudflare Tunnel or Tailscale Funnel,
so the public Host header must be validated by KaroX rather than rejected by the
SDK's localhost-only allowlist.
"""
from __future__ import annotations

import os
from collections.abc import Iterable

_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_TUNNEL_SUFFIXES = (".trycloudflare.com", ".ts.net")


def normalize_host(value: str | None) -> str:
    """Return a lowercase hostname without a port, or an empty string if invalid."""
    raw = (value or "").strip().lower().rstrip(".")
    if not raw or any(char in raw for char in ("/", "\\", "@", "\x00", " ", "\t", "\r", "\n")):
        return ""

    if raw.startswith("["):
        closing = raw.find("]")
        if closing < 0:
            return ""
        host = raw[1:closing]
        remainder = raw[closing + 1 :]
        if remainder and not (remainder.startswith(":") and remainder[1:].isdigit()):
            return ""
        return host

    if raw.count(":") == 1:
        host, port = raw.rsplit(":", 1)
        if port.isdigit():
            raw = host
    elif raw.count(":") > 1:
        # Unbracketed IPv6 is accepted only as a bare address, not with a port.
        return raw

    return raw


def configured_hosts() -> set[str]:
    """Read optional exact hosts from KAROX_MCP_ALLOWED_HOSTS."""
    values = os.environ.get("KAROX_MCP_ALLOWED_HOSTS", "")
    return {normalize_host(item) for item in values.split(",") if normalize_host(item)}


def is_allowed_mcp_host(value: str | None, extra_hosts: Iterable[str] = ()) -> bool:
    """Allow local endpoints and KaroX-supported authenticated tunnel domains."""
    host = normalize_host(value)
    if not host:
        return False
    if host in _LOCAL_HOSTS:
        return True

    exact = configured_hosts()
    exact.update(normalize_host(item) for item in extra_hosts)
    exact.discard("")
    if host in exact:
        return True

    return any(host.endswith(suffix) and host != suffix[1:] for suffix in _TUNNEL_SUFFIXES)
