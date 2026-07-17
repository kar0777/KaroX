# KaroX provider for Notion Custom Agents

KaroX exposes the selected local repository to a Notion Custom Agent through a protected Streamable HTTP MCP connection. The Notion provider uses one persistent Tailscale Funnel URL and one persistent Bearer credential, so the connection in Notion does not need to be recreated after ordinary KaroX restarts.

## Requirements

- A Notion workspace with **Custom Agents** and support for custom MCP servers.
- Python and the normal KaroX dependencies.
- Tailscale installed and signed in. On Windows, KaroX can install it through `winget`.
- Never paste the KaroX Bearer credential into a normal chat message. Store it only in Notion's protected token field.

## Normal setup

1. Run:

   ```powershell
   karox notion
   ```

2. Choose the repository and access profile.
3. KaroX prepares Tailscale, waits for `Connected`, starts the local MCP server, and points the stable Funnel URL to the current session.
4. Wait for `LIVE`.
5. In the built-in Notion connection card, copy the URL and token separately.
6. In the Notion Custom Agent add one **Custom MCP server**:

   - **Name:** `KaroX`
   - **MCP server URL:** the displayed stable URL ending in `/mcp`
   - **Authentication:** `Bearer token`
   - **Prefix:** `Bearer`
   - **Token:** the displayed credential, entered only in the protected token field

The URL and token remain the same after restarting KaroX or starting a different repository session on the same computer. A KaroX session must still be running while Notion is using the tools.

## Daily flow

1. Run `karox notion`.
2. Choose the repository and access profile.
3. Wait for `LIVE`.
4. Open the already configured KaroX agent in Notion.
5. Ask it to call `karox_ping`, then `karox_preflight`.
6. Send the real task.

Do not recreate the MCP connection and do not replace the token during normal restarts.

## Advanced connection commands

```text
karox notion setup             Rebuild the Tailscale/profile setup manually
karox notion                   Start a persistent Notion repository session
karox notion connection        Show the current stable URL and token
karox notion status            Check Tailscale, profile, and provider files
karox notion doctor            Run source checks and a real MCP initialize/list/call handshake
karox notion rotate-key        Deliberately replace the persistent key
karox notion reset-connection  Delete the persistent profile
```

`rotate-key` is an explicit security action. After rotating, replace the token once in Notion. Normal updates and restarts do not rotate it.

## MCP tools

The provider exposes:

- `karox_ping` — fast transport and local API health check;
- `karox_preflight` — session, health, Git status, and Mission Control validation;
- `karox_start_task` — records the real task separately from the history label;
- `karox_context` — refreshes Mission Control;
- `karox_tree`, `karox_read_file` — repository discovery and reading;
- `karox_write_file`, `karox_batch_write`, `karox_delete_file` — controlled changes;
- `karox_run` — builds, tests, and development commands through KaroX guardrails;
- `karox_git_status`, `karox_git_diff`, `karox_cleanup_generated`;
- `karox_commit` — explicit-file safe commit, never push;
- `karox_request` — access to less common KaroX API endpoints without external URLs;
- `karox_finish_task` — completion marker plus final report.

## Transport reliability

KaroX uses stateless Streamable HTTP with JSON responses. This avoids fragile long-lived SSE sessions while remaining an MCP Streamable HTTP endpoint.

The `/mcp` gateway also:

- uses raw ASGI middleware, so request and response streams are not wrapped or consumed;
- accepts both `/mcp` and `/mcp/` without a redirect;
- returns an explicit `405` for unsupported GET/SSE probes so clients can continue with POST;
- disables proxy buffering hints and response caching;
- converts local API failures into structured tool errors instead of crashing the MCP connection.

## Security model

- `/mcp` accepts the persistent key as `Authorization: Bearer <key>` or `X-API-Key`.
- Notion sessions force Tailscale Funnel even when another provider is selected for ordinary KaroX sessions.
- MCP requests must use localhost, a `*.trycloudflare.com` host, a `*.ts.net` host, or an exact host listed in `KAROX_MCP_ALLOWED_HOSTS`.
- The credential is compared in constant time and remains mandatory even for an allowed host.
- On Windows the persistent key is protected with the current user's DPAPI key when pywin32 is available. Other platforms use a profile file restricted to the current user.
- Starting a new Notion session stops an older active Notion session first so one stable Funnel route cannot point at two repositories.
- MCP tools call the existing KaroX API in-process and retain all mode, path, command, secret, and Git guardrails.
- Observe sessions stay read-only and `git push` remains blocked.

## Troubleshooting

### `SSE error`, `MCP fetch request failed`, or `Failed to connect to MCP server`

Run:

```powershell
karox update
karox notion doctor
karox notion
```

Wait for `LIVE`, then reconnect using the URL ending exactly in `/mcp`. The doctor now performs a real MCP initialize, tool listing, and tool call locally. If that handshake fails, its final lines identify the failing transport stage.

Do not add a second `Bearer` word inside the token field. Notion already applies the selected prefix.

### `karox_run` says that it failed to connect to the MCP server

First call `karox_ping`. If it fails, the problem is the transport or the local session, not the development command. Keep the KaroX window open, restart `karox notion`, wait for `LIVE`, then retry.

For commands with large output, use `capture_to_file=true`. KaroX limits excessive timeout and tail values to safe supported ranges.

### `Tailscale setup did not finish`

Complete sign-in in the browser or Tailscale app and wait until it says **Connected**, then run `karox notion` again. When Tailscale reports `Running` but `Self.DNSName` is temporarily empty, KaroX derives the same stable hostname from `HostName + MagicDNSSuffix`.

### Doctor reports `server\repo_tools.py was not found`

Reinstall or update KaroX. The doctor supports both the current flattened server directory and the older nested Windows layout.

### `421 Misdirected Request`

The request reached KaroX with an unsupported `Host` header. Use the exact URL shown by the running KaroX session. For a custom reverse proxy, add only its exact hostname to `KAROX_MCP_ALLOWED_HOSTS`.

### `401 Unauthorized`

Run `karox notion connection` and compare the protected token in Notion. Do not include the word `Bearer` inside the token field when Notion already shows the Bearer prefix.

### Connection timeout or `502/503`

The persistent URL remains valid, but the local KaroX session must be running. Start `karox notion`, wait for `LIVE`, and retry in Notion.

### Tailscale Funnel requires approval

KaroX prints and opens the Tailscale approval URL when the tailnet has not enabled Funnel yet. Approve it once, then restart `karox notion`.

## Architecture

The platform managers remain `start.core.ps1` and `start.core.sh`. Thin entrypoints generate reviewed provider-enabled launchers. A persistent local profile supplies the same Notion key on every launch, while Tailscale supplies the same public device hostname. Each new KaroX repository session updates the Funnel route to its current local port without changing the Notion-side connection.
