# Using KaroX from ChatGPT, Claude and other web clients

`karox mcp --http` serves KaroX as a remote MCP server (Streamable HTTP). ChatGPT, Claude
and any other client that takes a remote MCP URL can use it as a connector: you describe
the task in the chat, the assistant starts a KaroX run on your machine, follows its
progress and shows the diff. To let the chat's own model do the work instead, with no
second model or API key, see [Direct mode](#direct-mode-no-second-model).

## 1. Start the server

```bash
karox mcp --http 8765 --root ~/projects --model MODEL [provider flags]
```

The server listens on `127.0.0.1:8765`; its endpoint is
`http://127.0.0.1:8765/mcp/<token>`. The token in the path is the only credential: keep
the URL private. On the first start the server makes up a random token, keeps it in
`~/.karox6/mcp/http-token` (under `--home`; readable by you only) and prints the full URL.
Later starts reuse it, so a connector you added once keeps working after a restart: the
client's session carries on, and runs started before the restart are gone from the
server's memory but their changes are still on disk. `--new-token` makes a new token (the
old URL stops working; do it if the URL leaked). To choose the token yourself, set
`KAROX_MCP_TOKEN`: that token is neither saved nor printed. The server removes the token
and the API key variable from its environment, so the agent's commands cannot read them.
Everything the server sends back (command output, file contents, the diff, the final
answer) is checked for secrets first: private keys, API keys and tokens of common formats,
`Authorization` headers, passwords in URLs and in `password=` / `token:` style settings,
and the values of secret-looking environment variables show as `[secret removed: <kind>]`.
The files keep the real values, direct mode refuses to write such a marker back, and
`--no-redact` turns the masking off. Runs may only work inside `--root` (add `--any-cwd`
to lift that).

Dangerous commands (deletions, force pushes, ...) wait for approval by default: ChatGPT
sees a pending approval in `karox_status` and answers with `karox_approve`. Approve only
what you have read yourself: text in the repository can try to talk the model into
approving. Use `--approve-dangerous deny` to refuse them outright.

## 2. Make it reachable over HTTPS

Hosted assistants connect from the internet, so the local port needs a public HTTPS
address. With [Tailscale](https://tailscale.com/download) (signed in, MagicDNS and HTTPS
certificates on) KaroX sets it up itself:

```bash
karox mcp --http 8765 --root ~/projects --model MODEL --tunnel tailscale
```

It publishes the port with Tailscale Funnel and prints the connector URL,
`https://<machine>.<tailnet>.ts.net/mcp/<token>` (or with `:8443` / `:10000` when another
program already uses 443). The address stays the same across restarts, the certificate is
real, and the route is removed when KaroX stops; routes other programs configured are left
alone. If Tailscale says Funnel is not enabled, it prints the admin-console link that turns
it on. `--tunnel tailnet` uses Tailscale Serve instead: only devices in your tailnet reach
the URL (a client on your phone or laptop, not hosted assistants).

Any other HTTPS tunnel works too:

```bash
ngrok http 8765            # or: cloudflared tunnel --url http://127.0.0.1:8765
```

The connector URL is then `https://<tunnel-host>/mcp/<token>`. Anyone who learns the full
URL can start runs on your machine; stop the server or tunnel when you are done.

## 3. Add the connector

The token in the URL is the secret, so choose no authentication wherever the client asks.

- **ChatGPT**: in Settings > Apps & Connectors enable developer mode, create a connector
  with the URL from step 2, then enable it in a chat.
- **Claude** (claude.ai, desktop and mobile apps): Settings > Connectors > Add custom
  connector, paste the URL.
- **Other clients** (Le Chat, Cursor, VS Code, MCP Inspector, ...): add a remote /
  Streamable HTTP MCP server with that URL, or send `Authorization: Bearer <token>` to
  `https://<host>/mcp` if the client prefers a header.

Then ask, for example: "Use KaroX to fix the failing test in ~/projects/app and show me
the diff".

## Direct mode: no second model

With `--direct` the assistant in the chat (ChatGPT's own model, Claude, ...) does the
work itself through KaroX's tools: no `--model`, no provider, no API key.

```bash
karox mcp --direct --http 8765 --root ~/projects/app --tunnel tailscale
```

The connector then offers the agent's own tools instead of `karox_*`: `read`, `grep`,
`glob`, `job_output`, `job_list` (marked read-only) and `write`, `edit`, `bash` (`pwsh` on
Windows without Git Bash), `job_kill` (marked destructive, so ChatGPT asks before it uses
them). Relative paths start at `--root`; paths outside it are refused unless `--any-cwd`.
As for KaroX's agent, an existing file must be read before `write` replaces it. A call
returns within 50 s: a longer command goes on as a background job (read it with
`job_output`); jobs live until the server stops, which stops them.

Destructive commands (the same rules as for the agent) follow `--approve-dangerous`: `ask`
(default) refuses the call and tells the model to ask you in the chat; once you agree, it
repeats the call with `confirmed: true`. That relies on the model asking honestly, so use
`deny` to refuse them outright (or `allow` to run them). The URL, token and tunnel work as
above; without `--http` the same tools are served over stdio to local clients.

## Tools (agent mode)

| Tool | What it does |
|---|---|
| `karox_run` | start a task in a directory (or continue a session); idempotent per `request_id` |
| `karox_status` | a short log (one line per action), final answer, usage and pending approvals; the long-poll gathers lines and wakes early only when the run ends or needs approval (`detail: full` gives raw events) |
| `karox_approve` | approve or deny a pending dangerous command |
| `karox_cancel` | stop a run |
| `karox_diff` / `karox_files` | the workspace diff / files the run changed |
| `karox_runs` | runs of this server process |

One reply holds at most 100,000 characters: `karox_status` sends longer event lists in
pages (ask again with `since` = `next`), and a longer diff or final answer is cut with a
note saying how much was left out.
