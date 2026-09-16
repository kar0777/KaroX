# KaroX v3.16.1 — Reliable Notion MCP transport

## Fixed

- fixes Notion errors such as `SSE error: MCP fetch request failed` and `Failed to connect to MCP server`;
- replaces Starlette `BaseHTTPMiddleware`, which could close FastMCP Streamable HTTP channels, with a raw ASGI authentication and Host-validation gateway;
- switches the provider to stateless Streamable HTTP with JSON responses;
- accepts both `/mcp` and `/mcp/` without authentication-losing redirects;
- returns explicit transport errors instead of opaque connection failures;
- converts local KaroX API failures into structured MCP tool results;
- adds `karox_ping` for immediate transport and health diagnosis;
- bounds excessive `karox_run` timeout and output-tail values.

## Reliability and diagnostics

- `karox notion doctor` now runs a real MCP `initialize` → `tools/list` → `tools/call` handshake;
- CI runs the same handshake on Windows, macOS, and Linux with Python 3.10 and 3.12;
- the product doctor verifies the raw ASGI gateway, stateless JSON mode, required files, and dependency versions;
- the MCP SDK requirement is raised to the maintained v1 line used by the tested transport.

Existing settings, sessions, repository history, and the persistent Notion URL/token are preserved.
