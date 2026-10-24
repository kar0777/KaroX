# MCP

KaroX is an MCP client. Servers are configured once, selected per session, and
called as typed tools through the same capability and risk policy as the
built-in tools.

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

A mutating tool requires `--idempotency-key`; a retry with the same key is
deduplicated rather than executed twice. The call goes through `CoreRuntime`,
so session validation, capability policy and the MCP binding all apply.

## Security rules

- Server IDs, namespaces, tool names, URLs, headers and environment names are
  validated at the registry boundary.
- Raw secrets are rejected in persisted configuration.
- A URL with credentials, a query or a fragment is refused.
- Tool output is bounded by `--max-result-bytes` and per-call timeouts.
- The release never executes a tool that is not explicitly `allow` in the
  session, even if a model asks for it by name.