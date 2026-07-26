# Notion connection example

Connect Notion Custom Agent to a local KaroX session over authenticated
Streamable HTTP MCP.

## 1. Make sure the OS credential backend is available

KaroX stores the bridge secret in the OS keyring (Windows Credential Manager,
macOS Keychain, or Linux Secret Service). The `keyring` package is a declared
dependency and is installed automatically on first use; verify it once:

```powershell
python -c "import keyring; keyring.set_password('KaroX/probe','a','x'); print(keyring.get_password('KaroX/probe','a')); keyring.delete_password('KaroX/probe','a')"
```

If that prints `x` you are ready. If it errors, install the dependency
(`pip install keyring`) or reinstall KaroX through the provided installer.

## 2. Connect from the interactive shell

Run `karox` in the project folder, then:

1. Type `/connect` (or press `Ctrl+S`).
2. Choose **Website** → **Notion Custom Agent (MCP)**.
3. Tick the KaroX Core tools to expose (read files, list files, write files,
   git status, git diff).
4. Under **Public access**, choose the transport for the public URL that
   the cloud-hosted Notion agent will reach:

   - **Tailscale Funnel** (recommended for Notion) — publishes a stable
     `https://<machine>.ts.net/mcp` URL via your tailnet. Prerequisites:
     install Tailscale, run `tailscale up` to join a tailnet, and enable
     Funnel for the node (`tailscale funnel 443 on`, or in the tailnet
     admin panel). Selecting **Notion Custom Agent (MCP)** above defaults
     this option on.
   - **Cloudflare Tunnel** — a one-off `https://<random>.trycloudflare.com/mcp`
     URL via the `cloudflared` Quick Tunnel (no account needed). Good fallback
     when no tailnet is available.
   - **Local** — no public URL; only `http://127.0.0.1:<port>/mcp` on this
     machine (use only when Notion runs on the same host).

5. Press **F10 — Start bridge**.

KaroX prints two things: the **connector URL** (`https://<machine>.ts.net/mcp`
for Tailscale, or `http://127.0.0.1:8765/mcp` for local) and a **one-time
bearer secret** — copy it now, it is shown only once.

## 3. Configure Notion

```text
name: KaroX
server URL: https://<machine>.ts.net/mcp        # Tailscale Funnel
transport: Streamable HTTP
authorization: Bearer <one-time secret>
```

Paste the secret only into Notion's protected credential field, never into chat.

## 4. Send the task

The Notion agent must call a KaroX Core tool (for example read a file or run
git status) before changing the repository. Then send the actual task as a
separate message, for example:

```text
Add validation for empty project names, run the relevant tests, review the
diff, and commit only if the checks pass. Never push.
```

## Scriptable equivalent

```powershell
karox session create --repository . --task "Notion task" `
  --access-profile workspace_write --id notion-1
$bridge = karox bridge credential set notion-1 --json | ConvertFrom-Json
$bridge.secret   # shown once
karox bridge serve --profile notion --protocol mcp `
  --repository . --session-id notion-1 --credential notion-1 `
  --tool karox.repo.read_file --tool karox.repo.list_files `
  --tool karox.repo.write_file --tool karox.git.status --tool karox.git.diff `
  --port 8765
```

Endpoint: `http://127.0.0.1:8765/mcp`, `Authorization: Bearer <secret>`.
