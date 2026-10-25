# MCP

KaroX is both an MCP client and a hosted MCP server.

- **Outbound:** KaroX configures external servers, selects their tools per
  session, and calls them as typed tools through the same capability and risk
  policy as the built-in tools.
- **Inbound:** ChatGPT Web, Claude Web and HyperAgent connect to a local KaroX
  bridge over OAuth 2.1 and Streamable HTTP. The hosted client sees only the
  explicit tool allowlist selected at launch.

## Add a server

```text
# stdio
karox mcp server add filesystem --namespace filesystem --transport stdio \
    --command npx --arg -y --arg @modelcontextprotocol/server-filesystem --arg .

# Streamable HTTP
karox mcp server add notion --namespace notion --transport streamable_http \
    --url https://mcp.notion.com/mcp --oauth
```

A header or environment value must not contain a secret. Use a credential
reference instead:

```text
karox mcp credential set notion --stdin
karox mcp server add notion --namespace notion --transport streamable_http \
    --url https://mcp.notion.com/mcp \
    --credential-ref os-keyring:mcp/notion --credential-target Authorization
```

OAuth servers use `karox mcp server authorize <id>` and cannot also carry a
static credential.

## Discover before selecting

```text
karox mcp server inspect <id> --repository .
karox mcp server doctor <id> --repository .
```

`inspect` returns the server record, status and every discovered tool with its
schema digest. `doctor` returns a compact connectivity result.

## Select tools per session

```text
karox mcp session select filesystem --session-id S \
    --permission read_file=allow --permission list_directory=allow
```

Permissions are `allow`, `ask` or `deny`. Only `allow` tools are described to
the model. `ask` is **not** an interactive prompt in this release: a call is
refused until the selection is edited. `deny` is refused as well. This is
deliberate — a permission state that silently means "maybe" is worse than an
explicit refusal.

A selection stores the tool's name, schema digest and read-only classification.
At call time the binding re-reads the server record and re-checks the digest.
A changed tool schema is refused; re-select the server to approve it.

## Status

```text
karox mcp status [--session-id S] [--probe]
```

The status view joins three independent facts:

- configuration from the registry;
- authorization from the session selection;
- liveness only when `--probe` asked a server.

Without a probe, liveness is `not_probed`, never `ok`. Authorization and
reachability are separate fields: an allowed tool is still allowed when the
server is down, and a reachable server does not imply an allowed tool. Missing
or unreadable credentials are reported without inventing an expiry date.

## Call

```text
karox mcp call mcp.filesystem.read_file --session-id S \
    --arguments '{"path":"README.md"}'
```

A mutating tool requires `--idempotency-key`; the CLI takes a session mutation
lease, and a retry with the same key returns the recorded result instead of
executing twice. The call goes through `CoreRuntime`, so session validation,
capability policy and the MCP binding all apply.

## MCP in the engine-backed agent

MCP tools reach the engine through the same typed Core surface as every other
tool: the session's `--tool` selection becomes a `McpRuntimeBinding`, the
binding is passed to `CoreRuntime`/`CapabilityCoreRuntime`, and the engine's
tool environment calls those tools exactly like `repo.read_file`. Capability
policy (`mcp.call`), session binding and revocation, stored selection identity
(registry digest, schema digest, mutation classification, permission), the
mutation lease and idempotent replay all apply in the Core runtime, so the
engine gets no separate MCP path and no shell-text grammar.

A mutating tool still requires an idempotency key; a repeated key with the
same input replays the recorded outcome, a key reused for different input
conflicts, and a call that fails after reserving its intent stays pending
until the outcome is reconciled.

## Hosted bridge for ChatGPT Web / Claude Web / HyperAgent

```text
karox bridge connect chatgpt-web --repository . --tunnel tailscale
karox bridge connect chatgpt-web --repository . --write --tunnel tailscale
karox bridge saved create chatgpt-dev --target-profile chatgpt-web \
    --repository . --tunnel tailscale --language ru
karox bridge connect --saved chatgpt-dev
karox bridge status --saved chatgpt-dev --json
karox bridge stop --saved chatgpt-dev
```

Without `--write`, the bridge exposes a read-only repository/Git/runtime
catalogue. `--write` adds repository mutation, commands, checks/tests and
`git.commit`. `git.push`, publish, deploy and auth mutations are not exposed by
any profile.

The launch flow:

1. validates the repository and selected tools;
2. creates or reuses the repository-bound session;
3. generates an approval password and stores it in the OS keyring service
   `KaroX/bridge` under the session id;
4. starts a loopback-only child `karox bridge serve`;
5. establishes an owned Tailscale Funnel route, or uses an explicit custom
   HTTPS origin;
6. prints the MCP URL and only the credential fingerprint.

The endpoint implements OAuth discovery aliases, Dynamic Client Registration,
authorization code with PKCE S256, exact redirect/resource/client binding, a
password approval page, short-lived access tokens, rotating refresh tokens and
refresh-replay family revocation. Process restart keeps the same saved profile,
session id, keyring entry and OAuth state.

Mutations from a hosted client are idempotent: an explicit
`_meta.karoxIdempotencyKey` is honoured; without one, the proxy derives a stable
key from the tool name and arguments. A retry returns the recorded result with
`idempotent_replay: true`. Core still requires a mutation lease, capability and
risk approval for every call. OAuth authenticates the client; it grants no tool.

Saved profiles live in `web-bridge-profiles.json` and contain no secret. The
release reads v5 profiles unchanged, so an existing `chatgpt-dev` connector keeps
its URL, keyring entry and approval password. A profile that uses the removed
Cloudflare Quick Tunnel is refused with an instruction to switch to
`--tunnel tailscale` or a custom HTTPS origin.

### Bridge security rules

- The listener binds `127.0.0.1` by default. A non-loopback bind requires both
  `--allow-network-bind` and TLS certificate/key files.
- Host, Origin and forwarded-host checks reject rebinding attempts with `421`.
- An authorization code is one-time and bound to its client, redirect URI,
  PKCE challenge and resource.
- A replayed refresh token revokes the whole token family across restarts.
- The approval password is never printed; `--copy` uses the clipboard, and the
  OAuth state file stores digests only.
- Foreign Tailscale routes are never reset. `stop` only terminates a proven
  KaroX-owned process and route.
- The bridge never exposes browser control, desktop control, autonomy,
  orchestration or publish/push surfaces.

## Security rules

- Server IDs, namespaces, tool names, URLs, headers and environment names are
  validated at the registry boundary.
- Raw secrets are rejected in persisted configuration.
- A URL with credentials, a query or a fragment is refused.
- Tool output is bounded by `--max-result-bytes` and per-call timeouts.
- The release never executes a tool that is not explicitly `allow` in the
  session, even if a model asks for it by name.