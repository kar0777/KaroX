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
    [--max-total-tokens N] [--max-cost N --currency USD]
    [--verification-command JSON_OR_SHELL]... [--accept-discovered-checks]
    [--access-profile read_only|browser_control|workspace_write|elevated]
    [--effort-level auto|low|medium|high|extra-high|ultra]
    [--mode build|plan|ideate] [--expect auto|change]
    [--image PATH]... [--no-project-context] [--no-memory]
    [--max-steps N] [--max-seconds S]
    [--context-window N] [--max-output-tokens N]
    [--stream] [--stream-json] [--legacy-loop] [--json]
```

- The default runtime is the vendored karo-core engine (`karox.engine`): one
  bash action per model turn, executed repository-scoped behind the capability
  policy, the command invariants and the consequence engine. The report's
  `engine` field names the loop that ran. The harness runs the approved
  verification commands after the engine submits, so `verified` always means a
  real check passed.
- `--legacy-loop` selects the older v5 kernel with its typed tool calls
  (including `skill_load`, MCP tools mid-task, the visual checker and image
  attachments). It is a diagnostic escape hatch, not the product path; image
  attachments are refused on the engine loop rather than silently dropped.

- Routed mode uses the selected registry model. `--route` adds ordered fallback
  routes; the primary is first. Context and output ceilings are the smallest
  known across all routes so a fallback never receives a history it cannot fit.
- `--max-total-tokens` and `--max-cost` are hard ceilings enforced by the router.
- Direct mode (`--model` plus `--base-url`) is for a one-off endpoint and
  persists no provider record. It cannot be combined with routed options or
  image attachments.
- Verification commands are **user approval**. A command discovered from the
  repository is printed as a suggestion but does not count as evidence unless
  you pass `--accept-discovered-checks`.
- `--image` attaches a repository-confined PNG/JPEG/WebP to the next model turn;
  the selected model must advertise `vision=true`.
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