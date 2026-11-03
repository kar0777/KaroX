# CLI reference

`karox` with no arguments opens the terminal client when both standard streams
are terminals; otherwise it prints help. Every command accepts `--json` where a
machine-readable document is useful.

Short daily forms are accepted: `karox models`, `karox providers`,
`karox sessions`, `karox skills`, and a bare `karox model|provider|session|skill`
lists. They are aliases, not a second command tree.

Exit codes: `0` success or verified run, `1` an agent run that did not verify,
`2` a configuration or command error (the message is on stderr).

## Health and state

```text
karox doctor [--json]      # providers, credential backend, MCP, sessions
karox status [--json]      # version, selected model, latest session, paths
karox paths [--json]       # config_dir, runtime_dir, session_dir
```

`doctor` reports `degraded` in its document but still exits `0`: a missing model
or keyring is data for the caller, not a broken command.

## Model provider

```text
karox provider catalog [QUERY] [--all] [--json]       # models.dev snapshot
karox provider catalog --provider ID [--json]         # one provider + models, prices
karox provider connect ID [--model M] [--stdin-key | --env-key | --credential-ref REF]
    [--provider-id ID] [--base-url URL] [--var NAME=VALUE] [--no-test] [--no-activate]
karox provider catalog-sync [ID ...] [--json]         # fill missing prices/limits
karox provider presets [--json]
karox provider setup <preset> --model <id> [--stdin-key] [--base-url URL]
    [--provider-id ID] [--context-window N] [--max-output-tokens N]
    [--credential-ref REF] [--no-test] [--no-activate]
    [--pricing-version V --currency USD --input-per-million N
     --output-per-million N --pricing-source S]
    [--cache-read-per-million N] [--cache-write-per-million N] [--json]
karox provider add <id> --adapter <kind> --base-url URL
    [--credential-ref REF] [--privacy-class CLASS] [--header K=V] [--query K=V]
karox provider edit <id> [--adapter KIND] [--base-url URL]
    [--credential-ref REF | --clear-credential] [--header K=V] [--query K=V]
    [--privacy-class CLASS] [--timeout-seconds S]
    [--max-transport-retries N]
karox provider remove <id> [--cascade] [--delete-credential]
karox provider list|show|details|test ...
karox provider credential-set <id> [--stdin] [--name NAME]
karox provider credential-clear <id> [--delete-stored]
```

`provider connect` is the key-only path: the built-in catalog (a dated
[models.dev](https://models.dev) snapshot, the one OpenCode uses) supplies the
endpoint, protocol, model list, context windows and published prices. The key
is checked against the provider's `/models` list (a 401/403 fails before
anything is saved), models are narrowed to what the account lists, and the
provider's own metadata wins over the catalog's. Connecting a provider that is
already configured keeps every model's hand-entered values (aliases, context
window, price, provenance) and the provider's headers, timeouts and Bypass
preference; only empty fields are filled. `--env-key` uses
`KAROX_PROVIDER_<ID>_API_KEY` when set, otherwise imports the provider's usual
variable (for example `WANDB_API_KEY`) into the OS keyring once. Providers that
need more than a key (Bedrock, Vertex, Azure, Copilot, ...) are listed with
`--all` and the reason. `catalog-sync` only fills fields that are empty, so it
gives existing providers prices (and makes `--max-cost` usable) without
touching anything entered by hand.

`provider setup` reuses an existing saved credential for the same provider ID.
A loopback endpoint (`localhost`, `127.0.0.1`, `::1`) needs no key. A remote
provider needs `--stdin-key`, `--credential-ref`, or a saved credential even
with `--no-test`. The live test sends one minimal model request before anything
is stored; the key read from stdin reaches the OS keyring only after it passes.
`--no-test` is the explicit opt-out.

```text
karox model list [--provider ID] [--json]
karox model discover <provider> [--save] [--timeout-seconds S] [--json]
karox model add <provider> <model> [--alias ALIAS] [--context-window N]
    [--max-output-tokens N] [--tools true|false|unknown] ...
    [--pricing-version V --currency USD --input-per-million N
     --output-per-million N --pricing-source S]
karox model select <provider> <model>
karox model map <provider> <alias> <model>
karox model repair-selection [--preferred-provider ID]
karox model inspect|test|remove ...
```

`model discover` reads the provider's `/models` endpoint, reports context,
output, tool/vision facts and provider-reported prices where available, and
corrects a stored `/v1` base URL when the catalog proves the right one.
`--save` adds the discovered models without selecting one.

## Agent

```text
karox agent run --task TEXT [--repository PATH]
    [--session-id ID] [--continue-task]
    [--model ID --base-url URL [--api-key-env NAME]]
    [--route PROVIDER/MODEL]... [--route-strategy ordered|cheapest]
    [--privacy-limit local|private|public]
    [--max-total-tokens N] [--max-cost N [--currency USD]]
    [--verification-command JSON_OR_SHELL]... [--accept-discovered-checks]
    [--browser-plain-credentials]
    [--access-profile read_only|browser_control|workspace_write|elevated]
    [--effort-level auto|low|medium|high|extra-high|ultra]
    [--mode build|plan|ideate] [--expect auto|change]
    [--image PATH]... [--vision auto|on|off] [--no-project-context] [--no-memory]
    [--max-steps N] [--max-seconds S]
    [--context-window N] [--max-output-tokens N]
    [--mcp-server SERVER[=read|all]]...
    [--stream] [--stream-json] [--json]
```

- `--mcp-server SERVER[=read|all]` selects a configured MCP server for the
  session before the run starts (repeatable). `all` allows every tool the
  server lists; `read` (the default) allows the tools marked read-only and
  denies the rest. The server is inspected first, so an unreachable one fails
  the run up front with the server's own error instead of mid-task. Existing
  per-tool decisions from `karox mcp session select` are replaced for that
  server only.

- The runtime is the vendored karo-core engine (`karox.engine`): one
  bash action per model turn, executed repository-scoped behind the capability
  policy, the command invariants and the consequence engine. The report's
  `engine` field names the loop that ran. The harness runs the approved
  verification commands after the engine submits, so `verified` always means a
  real check passed. The retired v5 loop and its `--legacy-loop` flag are
  gone; the engine is the only kernel.

- The default browser surface is loopback-only and headless. It can save a
  download into KaroX's artifact store (25 MiB cap, never the repository), and
  it refuses password, token and card fields. `--browser-plain-credentials`
  lets it fill password/token fields with throwaway test credentials supplied
  in the task; filled values are redacted from results, snapshots and logs.
  A single-character `press` writes text just like `fill`, so it passes the
  same credential guard (named keys such as Enter do not).

- Routed mode uses the selected registry model. `--route` adds ordered fallback
  routes; the primary is first. Context and output ceilings are the smallest
  known across all routes so a fallback never receives a history it cannot fit.
- `--max-total-tokens` and `--max-cost` are ceilings enforced by the router.
  Token spend is checked before and after each request; a request that crosses
  the cost ceiling is billed before the run stops. A cost ceiling cannot be
  enforced when the gateway omits usage counters or the model has no pricing
  record: the run stops with `budget_exceeded:cost_unknown` instead of
  treating the missing amount as zero, and the report says `costs: unknown`.
  Without `--currency` the budget is in the routes' pricing currency; a route
  with no price is refused before the session starts, naming the route
  (`karox provider catalog-sync` fills catalog prices).
- Direct mode (`--model` plus `--base-url`) is for a one-off endpoint and
  persists no provider record. It cannot be combined with routed options or
  image attachments.
- Verification commands are **user approval**. A command discovered from the
  repository is printed as a suggestion but does not count as evidence unless
  you pass `--accept-discovered-checks`.
- `--image` attaches a repository-confined PNG/JPEG/WebP to the next model turn;
  the selected model must advertise `vision=true`.
- `--vision` decides whether the run may show the model a rendered page through
  `visual.check`. `auto` (the default) asks the registry; a direct
  `--model/--base-url` route stays off because nothing there declares image
  support. `on` opts a known vision-capable direct endpoint in, which is what
  lets a page task finish `verified` instead of `unproducible_requirement`; `off`
  disables the checker even for a vision model.
- Project instructions (`AGENTS.md`, `CLAUDE.md`, `KAROX.md`), the project map
  and relevant local memory are bounded and attributed in the report.
  `--no-project-context` and `--no-memory` turn those sources off.
- `--continue-task` appends a new user turn to an existing session. Without it,
  a changed task is refused rather than silently overwriting the old objective.
- `--stream` narrates progress on stderr; `--stream-json` emits typed events on
  stdout and finishes with one result document. `--json` prints only the final
  report.
- The agent refuses a drive root and Windows/system folders as a workspace.

Exit code is `0` only when the report says `verified: true`.

## Sessions

```text
karox session create --task TEXT [--repository PATH] [--id ID] [--name NAME]
karox session list [--all]
karox session show <id>
karox session fork <id> [--new-session-id ID] [--task TEXT] [--name NAME]
karox session rename <id> --name NAME
karox session archive|unarchive|revoke <id>
karox session delete <id> --confirm <id>
karox session lock <id>
```

## MCP outbound

```text
karox mcp server add <id> --namespace NS --transport stdio --command CMD [--arg A]...
karox mcp server add <id> --namespace NS --transport streamable_http --url URL
    [--header K=V] [--env K=V] [--oauth] [--credential-ref REF --credential-target T]
    [--read-only-tool NAME]... [--timeout-seconds S]
karox mcp server list|show|inspect|doctor|authorize|remove ...
karox mcp session select <server> --session-id ID --permission TOOL=allow|ask|deny
karox mcp session deselect|permissions ...
karox mcp status [--session-id ID] [--probe]
karox mcp call <tool> --session-id ID --arguments JSON [--idempotency-key KEY]
karox mcp credential set|show|delete|doctor ...
```

A mutating `mcp call` requires `--idempotency-key`, takes a session mutation
lease, and a retry with the same key returns the recorded result instead of
executing twice. See [MCP.md](MCP.md) for the permission model.

## Hosted bridge (ChatGPT Web, Claude Web, HyperAgent)

```text
karox bridge list|show|doctor [--json]
karox bridge connect <profile> [--repository PATH] [--port N]
    [--tool NAME]... [--write] [--access-profile PROFILE]
    [--tunnel auto|tailscale|custom] [--public-url HTTPS_ORIGIN]
    [--tailscale PATH] [--tunnel-timeout-seconds S]
    [--deadline-seconds S] [--verification-command ARGV]...
    [--language en|ru] [--diagnostics-only] [--json]
karox bridge connect --saved NAME [--tailscale PATH] [--session-id ID]
karox bridge serve ...                      # advanced: one loopback child
karox bridge saved create NAME --target-profile PROFILE --repository PATH
    [--tool NAME]... [--write] [--verification-command ARGV]...
    [--tunnel tailscale|custom] [--public-url URL] [--port N]
    [--language en|ru] [--replace] [--json]
karox bridge saved list|show|validate|delete ...
karox bridge status|start|stop|restart --saved NAME [--json]
karox bridge oauth approval-password --saved NAME [--copy] [--quiet] [--json]
karox bridge credential set|show|copy|rotate|revoke <session-id> [--json]
```

`connect` without `--saved` creates an ad-hoc session and keeps the owner in the
foreground. `--write` adds repository mutation and check tools; the default
profile is read-only and cannot write. `--diagnostics-only` prints the exact
effective contract without creating a session, starting a listener or touching a
tunnel.

Saved profiles store only non-secret launch policy in
`web-bridge-profiles.json`. The approval password and bearer credential live in
the OS keyring service `KaroX/bridge`; the command prints only a fingerprint.
`--copy` places the secret on the clipboard instead of stdout.

A profile currently used by a live ChatGPT connector can be read unchanged: the
profile JSON, keyring namespace and session-id derivation are compatible with
v5. The bridge listener is loopback-only; the public HTTPS origin is Tailscale
Funnel or an explicitly supplied custom origin. Foreign Funnel routes are never
reset.

## Credentials and paths

```text
karox credential set|show|delete|doctor ...
karox credential setup [--activate|--unlock|--verify --consent] [--json]
karox paths [--json]
```

`credential show` prints an opaque reference and a fingerprint, never the
secret. `credential setup` is informational on Windows/macOS and the explicit
Linux Secret Service recovery path on Linux.