# ★ KaroX v3.12.3 — Notion MCP 421 Connection Hotfix

KaroX 3.12.3 fixes the `421 Misdirected Request` error shown by Notion when connecting a Custom Agent through Cloudflare Tunnel or Tailscale Funnel.

## Root cause

The MCP Python SDK automatically enables localhost-only DNS-rebinding protection when FastMCP uses `127.0.0.1`. KaroX correctly binds its local server to `127.0.0.1`, but Notion reaches that server through a public tunnel hostname. The SDK therefore rejected the tunnel `Host` header before the Bearer token could be checked.

## Fixed

- Replaced the SDK's localhost-only Host allowlist with a KaroX tunnel-aware validator.
- Allowed only localhost, `*.trycloudflare.com`, `*.ts.net`, and exact hosts explicitly listed in `KAROX_MCP_ALLOWED_HOSTS`.
- Kept the per-session Bearer token mandatory for every MCP request.
- Switched MCP token comparison to constant-time comparison.
- Added cross-platform regression tests for allowed and rejected Host headers.
- Added a provider contract that verifies the SDK localhost-only protection is not accidentally re-enabled.

## Upgrade

Run the normal installer again, restart the KaroX Notion session, and reconnect the Custom MCP server with the new tunnel URL and session key.

### Windows

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1 | iex"
```

### macOS / Linux

```bash
curl -fsSL https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.sh | bash
```
