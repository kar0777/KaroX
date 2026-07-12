# KaroX provider for Notion Custom Agents

KaroX exposes the selected local repository to a Notion Custom Agent through a protected Streamable HTTP MCP connection. Starting with **v3.13.0**, Notion uses a persistent connection profile: one stable Tailscale Funnel URL and one Bearer credential are reused across KaroX restarts and repository sessions.

## Requirements

- A Notion workspace with **Custom Agents** and support for custom MCP servers.
- Python and the normal KaroX dependencies.
- Tailscale installed and signed in. KaroX can install it through `winget` on Windows during setup.
- Never paste the KaroX Bearer credential into a normal chat message. Store it only in Notion's protected token field.

## One-time persistent setup

After updating KaroX, run:

```powershell
karox notion setup
```

On Windows this command:

1. creates a persistent Notion credential;
2. protects it with Windows DPAPI when available;
3. installs Tailscale through `winget` when necessary;
4. opens Tailscale login if the device is not connected;
5. waits up to 120 seconds for Tailscale and the stable device `*.ts.net` hostname;
6. records the stable HTTPS origin;
7. prints the MCP URL and Bearer token needed for the one-time Notion connection.

KaroX accepts both the direct `Self.DNSName` value and the equivalent `HostName + MagicDNSSuffix` form reported by Tailscale while login is still settling.

On macOS or Linux, install/sign in to Tailscale first and run the same command:

```bash
karox notion setup
```

Then start a repository session:

```powershell
karox notion
```

Wait for `LIVE`, then display the same persistent connection values at any time:

```powershell
karox notion connection
```

In the Notion Custom Agent add one **Custom MCP server**:

- **Name:** `KaroX`
- **MCP server URL:** the displayed stable URL ending in `/mcp`
- **Authentication:** `Bearer token`
- **Prefix:** `Bearer`
- **Token:** the displayed persistent credential, entered only in the protected token field

Save the agent. This URL and token remain the same after restarting KaroX or starting a different repository session on the same computer. The KaroX session still needs to be running while Notion is actively using the tools.

## Daily flow

1. Run `karox notion`.
2. Choose the repository and access profile.
3. Wait for `LIVE`.
4. Open the already configured KaroX Developer agent in Notion.
5. Ask it to call `karox_preflight`.
6. Send the real task.

Do **not** recreate the MCP connection and do not replace the token during normal restarts.

## Connection commands

```text
karox notion setup             One-time Tailscale/profile setup
karox notion                   Start a persistent Notion repository session
karox notion connection        Show the current stable URL and token
karox notion status            Check Tailscale, profile, and provider files
karox notion doctor            Run provider diagnostics
karox notion rotate-key        Deliberately replace the persistent key
karox notion reset-connection  Delete the persistent profile
```

`rotate-key` is an explicit security action. After rotating, replace the token once in Notion. Normal updates and restarts do not rotate it.

## MCP tools

The provider exposes:

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

### `Tailscale setup did not finish`

Update to v3.13.2 or newer. KaroX now waits for login and reports the exact Tailscale backend state. Complete sign-in in the browser or Tailscale app, wait until it says **Connected**, then run:

```powershell
karox notion setup
```

When Tailscale reports `Running` but `Self.DNSName` is temporarily empty, KaroX derives the same stable hostname from `HostName + MagicDNSSuffix`.

### Doctor reports `server\repo_tools.py was not found`

Update to v3.13.0 or newer. Older Windows installations could place the server package in `app\server\server`; the new doctor resolves both layouts instead of reporting a false failure.

### `421 Misdirected Request`

Update to v3.12.3 or newer. Older builds inherited a localhost-only Host allowlist from the MCP SDK and rejected tunnel hostnames before authentication.

### `401 Unauthorized`

Run `karox notion connection` and compare the protected token in Notion. Do not include the word `Bearer` inside the token field when Notion already shows the Bearer prefix.

### Connection timeout or `502/503`

The persistent URL remains valid, but the local KaroX session must be running. Start `karox notion`, wait for `LIVE`, and retry in Notion.

### Tailscale Funnel requires approval

KaroX prints and opens the Tailscale approval URL when the tailnet has not enabled Funnel yet. Approve it once, then restart `karox notion`.

## Architecture

The large platform managers remain `start.core.ps1` and `start.core.sh`. Thin entrypoints generate reviewed provider-enabled launchers. A persistent local profile supplies the same Notion key on every launch, while Tailscale supplies the same public device hostname. Each new KaroX repository session updates the Funnel route to its current local port without changing the Notion-side connection.
