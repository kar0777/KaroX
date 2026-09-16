# ★ KaroX v3.12.0 — Control Center & Reliability

KaroX 3.12 is a full product-quality pass focused on daily usability, security, diagnostics and cross-platform reliability.

## Highlights

- Added a browser-based **KaroX Control Center** for live repository, task, Git and Mission Control visibility.
- Added the unified commands `karox version`, `status`, `doctor`, `update`, `support` and `dashboard` on Windows, macOS and Linux.
- Added stable-channel self-update with `karox update` and a lightweight update notice.
- Added redacted support bundles that exclude source code and actively scan for leaked session keys.
- Routed ordinary OpenAPI sessions and Notion MCP sessions through the same hardened runtime.
- Added constant-time key comparison and temporary throttling after repeated authentication failures.
- Added request IDs, request-body limits, secure response headers and safer internal-error responses.
- Added bounded, rotating and redacted audit logs.
- Added authenticated `/meta`, `/capabilities` and `/security/status` endpoints.
- Added cached launcher generation to reduce repeated startup work.
- Expanded CI to validate the product on Windows, macOS and Linux.

## Control Center

Start a session, then run:

```bash
karox dashboard
```

Or open the current tunnel URL with `/control`. Paste the key copied with `K` into the page. It remains in browser `sessionStorage` and is never placed in the URL.

## New commands

```bash
karox version
karox status
karox doctor
karox doctor --deep
karox update --check
karox update
karox support
karox dashboard
```

Machine-readable output is available through:

```bash
karox version --json
karox status --json
karox doctor --json
```

## Security changes

The API now provides:

- constant-time credential comparison;
- 30-failure-per-minute temporary client throttling;
- a configurable 30 MB request-body limit;
- `no-store`, `nosniff`, frame, referrer and permissions headers;
- a unique `X-KaroX-Request-ID` on responses;
- sanitized internal errors unless `REPO_TOOLS_DEBUG_ERRORS=1`;
- 10 MB audit rotation with three backups by default;
- recursive redaction for token-, password-, secret-, cookie- and authorization-like fields.

Existing repository confinement, sensitive path filtering, guarded commit workflow and permanent push prohibition remain unchanged.

## Install

### Windows

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1 | iex"
```

### macOS / Linux

```bash
curl -fsSL https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.sh | bash
```

## Upgrade

Existing installations can use:

```bash
karox update
```

Configuration and session history remain in the existing `RepoPilotBridge` compatibility directories.

## Compatibility

- Existing OpenAPI endpoints remain available.
- Existing `X-API-Key` and Bearer authorization continue to work.
- PromptQL, Notion, generic OpenAPI and letaido targets remain available.
- `repopilot` remains a compatibility command alias.
- The public installer remains pinned to a stable release tag.

Full documentation: [PRODUCT_GUIDE.md](PRODUCT_GUIDE.md)
