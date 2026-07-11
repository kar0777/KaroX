# KaroX provider for Notion Custom Agents

KaroX can expose the selected local repository to a Notion Custom Agent through a protected Streamable HTTP MCP connection. The existing KaroX OpenAPI interface remains available, while Notion receives a smaller set of purpose-built tools with the same repository, mode, branch, command, secret, and Git guardrails.

## Requirements

- A Notion workspace with **Custom Agents** and support for custom MCP servers. Availability depends on the Notion plan and workspace rollout.
- Python and the normal KaroX dependencies.
- Cloudflare Tunnel or Tailscale Funnel configured in KaroX.
- Never paste the KaroX session key into a normal Notion chat message. Store it only in the protected connector credential field.

## First setup

After updating KaroX, run once:

```powershell
karox notion install
```

On macOS or Linux:

```bash
karox notion install
```

This installs the optional MCP dependencies and runs the provider doctor.

## Daily flow

1. Run `karox` and choose **NOTION**, or run `karox notion` to force the Notion target for this launch.
2. Create the repository session and wait for `LIVE`.
3. Press `C` to copy the Notion connection prompt.
4. In the Notion Custom Agent, add the MCP server described by the prompt. Its URL ends in `/mcp`.
5. Press `K` in KaroX and paste that session key into Notion's protected Bearer-token field.
6. Send the copied connection prompt.
7. After `karox_preflight` succeeds, send the real coding task as a separate message.

Every KaroX session has a different URL and key. Do not reuse an old connection for another active session.

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

- `/mcp` accepts the current session key as `Authorization: Bearer <key>` or `X-API-Key`.
- MCP tools call the existing KaroX API in-process; they do not bypass its mode or path checks.
- Observe sessions stay read-only.
- File access stays inside the selected `repoRoot` and sensitive files remain blocked.
- Dangerous commands, secret-related paths, publishing commands, and `git push` remain blocked.
- The generic request tool accepts only relative KaroX paths and cannot call arbitrary external hosts.

## Diagnostics

```powershell
karox notion doctor
```

or:

```bash
karox notion doctor
```

The doctor verifies dependencies, preserved core launchers, source patch anchors, and the Notion gateway.

## Architecture

The large platform managers are preserved as `start.core.ps1` and `start.core.sh`. Thin `start.ps1` and `start.sh` entrypoints generate a reviewed Notion-enabled manager in the KaroX runtime directory. Normal PromptQL, generic OpenAPI, and letaido flows continue to use the same core logic. Only a Notion session switches the Uvicorn target from `repo_tools:app` to `notion_gateway:app`.
