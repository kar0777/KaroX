# ★ KaroX Product Guide

KaroX turns a local Git repository into a guarded workspace for AI agents. Version 3.12 adds a unified administration CLI, a browser-based Control Center, a hardened API runtime, safer diagnostics, and a stable self-update path.

## Install

### Windows

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1 | iex"
```

### macOS / Linux

```bash
curl -fsSL https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.sh | bash
```

The installer keeps configuration and session history in the existing `RepoPilotBridge` compatibility directories, so upgrading does not require reconfiguring KaroX.

## Daily workflow

```text
karox
→ choose a repository
→ choose Observe / Build / Resume / Advanced
→ choose PromptQL / Notion / generic OpenAPI / letaido
→ wait for ● LIVE
→ press V for preflight
→ press C for the connection prompt
→ press K for the protected session key
→ send the real task separately
```

The session name is history metadata. It is never treated as the AI task.

## Product commands

| Command | Purpose |
|---|---|
| `karox` | Open Project Flight Deck |
| `karox version` | Show the installed version |
| `karox status` | Show saved sessions and verify live processes |
| `karox status --json` | Machine-readable session status |
| `karox doctor` | Fast cross-platform product diagnostics |
| `karox doctor --json` | Machine-readable diagnostics |
| `karox doctor --deep` | Run the full endpoint and security harness |
| `karox update --check` | Check the stable release channel |
| `karox update` | Install the newest stable release |
| `karox update --yes` | Update without the confirmation question |
| `karox support` | Create a redacted support ZIP |
| `karox support --output <path>` | Choose the bundle location |
| `karox dashboard` | Open Control Center for the newest live session |
| `karox dashboard --session <id>` | Open a specific session |
| `karox notion` | Start directly with the Notion target |
| `karox notion doctor` | Validate the Notion MCP provider |

`repopilot` remains a compatibility alias for existing installations.

## Control Center

Every session now exposes a static dashboard at:

```text
https://<current-session-host>/control
```

The page itself contains no repository data. Paste the session key copied with `K` into the protected input inside the page. The key:

- stays in the current browser tab through `sessionStorage`;
- is never placed in the URL;
- is never sent to analytics or third-party services;
- can be removed immediately with **Forget key**.

Control Center displays:

- KaroX version and runtime;
- repository root and active branch;
- access mode and capabilities;
- real task state;
- changed-file count;
- Git status;
- live Mission Control context.

Use `karox dashboard` to open the correct live URL automatically.

## Access profiles

### Observe

Read-only analysis. File changes, commands, cleanup and commits are blocked.

### Build

Creates an isolated `promptql/*` branch. The agent may edit files, run approved development commands, review diffs and create a guarded commit.

### Resume

Continues the current workspace branch. KaroX warns when it does not match the usual guarded branch prefix.

### Advanced

Allows a broader command set inside the selected repository. System-level destructive commands, secret access, publishing and push remain blocked.

## Hardened API runtime

All standard and Notion sessions run through the same hardened entrypoint.

### Authentication

- `X-API-Key: <session key>`
- `Authorization: Bearer <session key>`
- constant-time key comparison;
- temporary rate limiting after repeated failures;
- the key is never written to prompts or support bundles.

### Request protection

- repository path confinement;
- sensitive-file filtering;
- configurable request-body limit;
- secure response headers;
- request IDs on every response;
- configurable browser-origin allowlist;
- generic internal errors unless debug mode is explicitly enabled.

### Bounded audit logs

Audit logs rotate instead of growing forever. Sensitive-looking keys and values are redacted before they are written.

Default values:

```text
maximum audit file: 10 MB
rotated backups: 3
failed authentication limit: 30 per minute per client
maximum request body: 30 MB
```

## New system endpoints

These require the session key:

| Endpoint | Description |
|---|---|
| `GET /meta` | Version, platform, repository, branch and runtime identity |
| `GET /capabilities` | Effective read/write/run/commit permissions and limits |
| `GET /security/status` | Active runtime security settings |

The existing API remains compatible. The new endpoints are additive.

## Stable updates

Check without changing anything:

```bash
karox update --check
```

Install the newest release:

```bash
karox update
```

KaroX reads the verified release marker from the public repository and launches the same stable bootstrap used by new installations. The updater does not open another Flight Deck while replacing the installed application.

To disable the lightweight release notice shown before Flight Deck:

```text
KAROX_UPDATE_NOTICE=0
```

## Support bundle

```bash
karox support
```

The resulting ZIP includes:

- product and operating-system information;
- quick doctor results;
- redacted settings;
- redacted session metadata;
- bounded tails of session logs;
- cached release metadata.

It excludes source files and explicitly redacts API keys, authorization headers, tokens, passwords, cookies and common provider-secret formats. A final safety scan aborts bundle creation if an unredacted `apiKey` field remains.

## Configuration directories

### Windows

```text
Configuration: %APPDATA%\RepoPilotBridge
Runtime:       %LOCALAPPDATA%\RepoPilotBridge
```

### macOS

```text
Configuration: ~/Library/Application Support/RepoPilotBridge
Runtime:       ~/.local/share/RepoPilotBridge
```

### Linux

```text
Configuration: ${XDG_CONFIG_HOME:-~/.config}/RepoPilotBridge
Runtime:       ${XDG_DATA_HOME:-~/.local/share}/RepoPilotBridge
```

## Runtime environment variables

| Variable | Default | Purpose |
|---|---:|---|
| `KAROX_LANGUAGE` | saved setting | Force `en` or `ru` |
| `KAROX_NO_ANIMATION` | `0` | Disable the launch animation |
| `KAROX_UPDATE_NOTICE` | `1` | Disable with `0` |
| `REPO_TOOLS_MAX_REQUEST_BYTES` | `30000000` | Maximum HTTP body size |
| `REPO_TOOLS_AUDIT_MAX_BYTES` | `10000000` | Audit rotation threshold |
| `REPO_TOOLS_AUDIT_BACKUPS` | `3` | Rotated audit files, 1–10 |
| `REPO_TOOLS_CORS_ORIGINS` | `*` | Comma-separated browser origins |
| `REPO_TOOLS_DEBUG_ERRORS` | `0` | Include sanitized internal error text with `1` |

For browser clients under your control, replace the wildcard CORS setting with explicit origins.

## Notion Custom Agent

```bash
karox notion
```

KaroX exposes the protected Streamable HTTP MCP endpoint at `/mcp`. Notion receives purpose-built tools for preflight, context, files, commands, Git review, safe commit and final reports. The normal KaroX API remains available beside MCP.

See [NOTION.md](NOTION.md) for the full setup.

## Troubleshooting order

1. Run `karox status`.
2. Run `karox doctor`.
3. Run `karox notion doctor` when the Notion provider is involved.
4. Run `karox doctor --deep` for the full endpoint harness.
5. Create `karox support` and attach the ZIP to a private bug report after reviewing its contents.

Never publish a live session key, tunnel URL plus key, or an unreviewed support artifact in a public issue.
