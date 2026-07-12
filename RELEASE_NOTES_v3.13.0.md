# ★ KaroX v3.13.0 — Persistent Notion Connection

KaroX v3.13.0 removes the daily Notion reconnection workflow and repairs Windows doctor checks on installations with the historical nested server layout.

## Persistent Notion MCP

- Adds `karox notion setup` for one-time persistent configuration.
- Uses a stable Tailscale Funnel `*.ts.net` URL instead of a new Quick Tunnel URL for each launch.
- Reuses one persistent Bearer credential across repository sessions and KaroX restarts.
- Protects the credential with Windows DPAPI when available; other platforms use a current-user-only profile file.
- Automatically forces Tailscale for Notion while leaving other KaroX providers unchanged.
- Stops an older Notion session before routing the stable endpoint to a new repository session.
- Adds `connection`, `status`, `rotate-key`, and `reset-connection` management commands.

One final Notion-side migration is required from the old `trycloudflare.com` connection to the new stable `*.ts.net` URL and persistent token. After that migration normal restarts require no MCP edits.

## Doctor repair

- Replaces the brittle legacy Windows doctor path check.
- Resolves both `server/repo_tools.py` and the historical installed `server/server/repo_tools.py` layout.
- Makes the Notion provider doctor use the same resolver.
- Adds regression coverage for normal and nested layouts.

## Validation

The release is tested across Windows, macOS, and Linux with Python 3.10 and 3.12, including:

- persistent key lifecycle and explicit rotation;
- stable URL preservation;
- Windows PowerShell 5.1 generated-launcher parsing;
- POSIX generated-launcher syntax;
- nested Windows installation layout detection;
- MCP Host validation and Bearer authentication;
- release archive generation and CodeQL.
