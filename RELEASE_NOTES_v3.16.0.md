# KaroX v3.16.0 — Native Notion Provider

## New native workflow

Notion is now a first-class provider in the normal KaroX interface.

1. Run `karox`.
2. Choose **Notion** as the AI target.
3. Create a repository session as usual.
4. KaroX automatically prepares Tailscale, waits for `Connected`, forces the stable Funnel transport, reuses the persistent Bearer credential, and starts the MCP server.
5. Once the workspace is LIVE, KaroX shows a built-in Notion connection card with separate actions to copy the MCP URL and token.

The regular workflow no longer requires `karox notion setup`, `karox notion connection`, or a separate command sequence.

## Included

- native Notion provider choice in KaroX;
- automatic Tailscale startup/readiness flow;
- automatic stable Funnel selection for Notion;
- persistent MCP URL and Bearer credential;
- Russian and English provider UI;
- LIVE Notion connection card;
- reusable `N = Notion` action in the session menu;
- Windows PowerShell 5.1, macOS, Linux, Python 3.10 and 3.12 coverage.

## Compatibility

Advanced `karox notion ...` commands remain available for diagnostics and credential maintenance, but they are not required for ordinary use.

Existing settings, repository history, sessions, and the persistent Notion credential are preserved during the update.
