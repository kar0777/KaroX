# Using KaroX from ChatGPT on the web

`karox mcp --http` serves KaroX as a remote MCP server (Streamable HTTP). ChatGPT can
use it as a custom connector: you describe the task in the chat, ChatGPT starts a KaroX
run on your machine, follows its progress and shows the diff.

## 1. Start the server

```bash
export KAROX_MCP_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
karox mcp --http 8765 --root ~/projects --model MODEL [provider flags]
```

The server listens on `127.0.0.1:8765` and prints its endpoint,
`http://127.0.0.1:8765/mcp/<token>`. The token in the path is the only credential: keep
the URL private. Runs may only work inside `--root` (add `--any-cwd` to lift that).

Dangerous commands (deletions, force pushes, ...) wait for approval by default: ChatGPT
sees a pending approval in `karox_status` and answers with `karox_approve`. Use
`--approve-dangerous deny` to refuse them outright.

## 2. Make it reachable over HTTPS

ChatGPT connects from the internet, so the local port needs a public HTTPS address, for
example with a tunnel:

```bash
ngrok http 8765            # or: cloudflared tunnel --url http://127.0.0.1:8765
```

The connector URL is then `https://<tunnel-host>/mcp/<token>`. Anyone who learns the full
URL can start runs on your machine; stop the tunnel when you are done.

## 3. Add the connector in ChatGPT

In ChatGPT settings, enable developer mode for connectors, create a connector with the
URL from step 2 and choose no authentication (the token in the URL is the secret). Then
enable the connector in a chat and ask, for example: "Use KaroX to fix the failing test in
~/projects/app and show me the diff".

## Tools

| Tool | What it does |
|---|---|
| `karox_run` | start a task in a directory (or continue a session); idempotent per `request_id` |
| `karox_status` | progress events, final answer, usage and pending approvals (long-poll) |
| `karox_approve` | approve or deny a pending dangerous command |
| `karox_cancel` | stop a run |
| `karox_diff` / `karox_files` | the workspace diff / files the run changed |
| `karox_runs` | runs of this server process |
