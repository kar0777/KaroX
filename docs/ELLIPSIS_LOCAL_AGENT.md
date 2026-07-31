# Ellipsis Opus 5 as a remote reasoning backend

This integration keeps the selected local Git repository as the only working
tree. Ellipsis receives no repository metadata and no project files. It receives
only Claude Opus 5, the minimal `karox-remote` adapter, a strict instruction, and
three write-only connection variables.

## Architecture

```text
Karo CLI on Windows
  -> local repository-scoped Karo session
  -> authenticated OpenAPI bridge
  -> Tailscale Funnel, Cloudflare Tunnel, or an approved HTTPS transport
  -> Ellipsis interactive session with claude-opus-5
  -> karox-remote
  -> local KaroX tools
  -> the selected local Git working tree
```

The Ellipsis session is created directly through the configured REST contract.
KaroX never starts the Ellipsis CLI from the project repository, never supplies a
Git origin, never uses `agent session handoff`, and never asks Ellipsis to clone,
execute, or synchronize the project.

## What runs locally

KaroX remains responsible for:

- repository-confined file reads, searches, writes and patches;
- user-approved commands, tests, lint, type checks and builds;
- managed local processes and dev servers;
- localhost-only browser verification and screenshots;
- local Git status, diff, log and optional guarded commits;
- retry-safe mutations, audit evidence, secret filtering and request IDs;
- checkpoints, explicit rollback, session recovery and credential revocation.

Shell commands, remote Git, push, publish, deploy, release and authentication
commands are not exposed. Browser requests and subresources are limited to
`localhost`, `127.0.0.1`, or `::1`.

## Ellipsis API contract

Ellipsis does not publish a universal public interactive-session endpoint. Set
the account-specific base URL and token provided for the organization:

```powershell
$env:ELLIPSIS_API_BASE_URL = "https://<organization-endpoint>"
$secureToken = Read-Host "Ellipsis API token" -AsSecureString
$pointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
  $env:ELLIPSIS_API_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($pointer)
} finally {
  [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($pointer)
}
```

The default versioned contract is isolated in `karox.ellipsis_api`. An account
with different paths can provide `ELLIPSIS_API_CONTRACT_JSON`. The JSON may
change only endpoint paths and whether `sandbox.repositories` is an empty array
or omitted. KaroX rejects any non-empty repository field before sending a
request.

Example contract override:

```powershell
$env:ELLIPSIS_API_CONTRACT_JSON = @'
{
  "version": "organization-interactive-v1",
  "capabilities_path": "/api/capabilities",
  "create_path": "/api/interactive-sessions",
  "variables_path": "/api/interactive-sessions/{session_id}/sandbox-variables",
  "start_path": "/api/interactive-sessions/{session_id}/start",
  "message_path": "/api/interactive-sessions/{session_id}/messages",
  "status_path": "/api/interactive-sessions/{session_id}",
  "events_path": "/api/interactive-sessions/{session_id}/events",
  "stop_path": "/api/interactive-sessions/{session_id}/stop",
  "repositories_mode": "empty"
}
'@
```

Do not put the Ellipsis token, KaroX bridge URL, remote credential, or session
ID in this JSON.

## Installing the thin client in the sandbox

`remote/` is a separate, dependency-free Python package. It contains no KaroX
runtime and no user project code. Build it independently:

```powershell
python -m build .\remote
```

For a live session, use one of these explicit, version-pinned strategies:

1. Host a standalone `karox-remote` artifact and set both
   `KAROX_REMOTE_INSTALL_URL` and `KAROX_REMOTE_INSTALL_SHA256`.
2. Publish the separate package and set an exact
   `KAROX_REMOTE_PACKAGE_VERSION`.
3. Use an Ellipsis image where the matching client is already installed.

A standalone URL must use HTTPS and must include a 64-character SHA-256 digest.
An unpinned branch, Git clone of the user project, or floating package version
is rejected. This repository does not publish or deploy an artifact as part of
the integration change.

## Starting from PowerShell

The normal path is:

```powershell
cd "D:\path\to\local-project"
karox agent `
  --repository "D:\path\to\local-project" `
  --access-profile workspace_write `
  --model claude-opus-5 `
  --budget 0.10 `
  --tunnel tailscale `
  --task "Fix the failing tests and verify the result locally."
```

KaroX creates a local session and checkpoint before remote preflight. It then
opens the transport, creates a repository-free Ellipsis draft, mints a
short-lived credential bound to the local session, repository digest, access
profile, and Ellipsis session ID, writes the three connection values as
write-only sandbox variables, and starts the task.

Project-aware test/build/dev command prefixes are inferred conservatively. Add
or replace an approval explicitly when needed:

```powershell
karox agent `
  --repository $PWD `
  --task "Run the project test suite and fix the failure." `
  --allow-command '["python","-m","pytest","*"]' `
  --verification-command '["python","-m","pytest","*"]'
```

PowerShell is accepted only with `-File` and an existing repository-scoped
`.ps1` script. Shell command strings are not accepted.

## Interactive commands

The same terminal supports:

```text
/status   current local path, branch, profile, transport, session and actions
/cost     cost reported by Ellipsis
/diff     local Git diff
/tests    locally recorded checks
/continue ask the agent to continue after re-reading local state
/stop     stop remote work, revoke access, keep local changes
/detach   leave the terminal while the session and watchdog remain active
/attach   show that the current terminal is already attached
/rollback explain the separate explicit rollback flow
/report   local KaroX evidence report
```

Reattach after restarting KaroX:

```powershell
karox agent attach <local-session-id>
```

Stop does not delete local changes. Rollback is separate and explicit:

```powershell
karox agent stop <local-session-id>
karox agent rollback <local-session-id>
```

A no-secret watchdog revokes the credential and closes the local bridge and
tunnel when the lease expires or the bridge dies.

## Claude Code and OpenCode

KaroX also exposes a local stdio MCP server:

```powershell
karox-agent-mcp
```

It provides:

- `karox_agent_start`
- `karox_agent_status`
- `karox_agent_send`
- `karox_agent_stop`
- `karox_agent_result`

These tools manage the Ellipsis session. They do not emulate an Anthropic or
OpenAI completion endpoint, and `ANTHROPIC_BASE_URL` is not used.

## Browser support

Install the optional browser dependency and Chromium locally:

```powershell
python -m pip install -e ".[browser]"
python -m playwright install chromium
```

Browser automation remains local and blocks non-localhost subrequests.
Screenshots are written into the selected repository through guarded KaroX file
boundaries and appear in the session evidence.

## Tests

The normal suite contains contract tests, local bridge E2E coverage, checkpoint
rollback coverage, CLI routing tests, remote-client secrecy tests, and a fake
control-plane ordering test.

The real paid acceptance test is deliberately excluded from the normal suite:

```powershell
python .\scripts\run_ellipsis_live_e2e.py `
  --allow-live-ellipsis `
  --budget 0.10
```

It additionally requires `ELLIPSIS_API_TOKEN`, `ELLIPSIS_API_BASE_URL`, and the
exact interactive confirmation `LIVE ELLIPSIS`. A budget above `0.10 USD` is
rejected. The test creates a temporary Git repository with no remote, asks Opus
5 to read and modify it only through KaroX, runs a local test, verifies the local
diff, confirms that no Git remote appeared, stops the session, and verifies
credential revocation.
