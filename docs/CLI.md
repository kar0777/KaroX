# CLI reference

`karox` with no arguments opens the terminal client when both standard streams
are terminals; otherwise it prints help. Every command accepts `--json` where a
machine-readable document is useful.

Exit codes: `0` success or verified run, `1` an agent run that did not verify,
`2` a configuration or command error (the message is on stderr).

## Model provider

```text
karox provider presets [--json]
karox provider setup <preset> --model <id> --stdin-key [--base-url URL]
    [--provider-id ID] [--context-window N] [--max-output-tokens N]
    [--credential-ref REF] [--no-test] [--no-activate] [--json]
karox provider add <id> --adapter <kind> --base-url URL
    [--credential-ref REF] [--privacy-class CLASS] [--header K=V] [--query K=V]
karox provider edit <id> [...]
karox provider remove <id> [--cascade] [--delete-credential]
karox provider list|show|details|test ...
karox provider credential-set <id> [--stdin] [--name NAME]
karox provider credential-clear <id> [--delete-stored]
```

`provider setup` sends one minimal model request before it stores anything. A
key read from stdin is written to the OS keyring only after that request
succeeds. `--no-test` is the explicit opt-out.

```text
karox model list [--provider ID] [--json]
karox model add <provider> <model> [--alias ALIAS] [--context-window N]
    [--max-output-tokens N] [--tools true|false|unknown] ...
karox model select <provider> <model>
karox model inspect|test|remove ...
```

## Agent

```text
karox agent run --task TEXT [--repository PATH]
    [--session-id ID] [--continue-task]
    [--model ID --base-url URL [--api-key-env NAME]]
    [--verification-command JSON_OR_SHELL]...
    [--access-profile read_only|browser_control|workspace_write|elevated]
    [--effort-level auto|low|medium|high|extra-high|ultra]
    [--mode build|plan|ideate] [--expect auto|change]
    [--max-steps N] [--max-seconds S]
    [--context-window N] [--max-output-tokens N]
    [--stream] [--stream-json] [--json]
```

- Routed mode uses the selected registry model. Direct mode (`--model` plus
  `--base-url`) is for a one-off endpoint and persists no provider record.
- Without `--verification-command`, the run discovers a bounded check for the
  repository (`npm test`, `pytest`, `cargo test`, `go test`, or a safe Git
  check). An explicit command always wins.
- `--continue-task` appends a new user turn to an existing session. Without it,
  a changed task is refused rather than silently overwriting the old objective.
- `--stream` narrates progress on stderr; `--stream-json` emits typed events on
  stdout and finishes with one result document. `--json` prints only the final
  report.

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

## MCP

```text
karox mcp server add <id> --namespace NS --transport stdio --command CMD [--arg A]...
karox mcp server add <id> --namespace NS --transport streamable_http --url URL
    [--header K=V] [--oauth] [--credential-ref REF --credential-target T]
karox mcp server list|show|inspect|doctor|authorize|remove ...
karox mcp session select <server> --session-id ID --permission TOOL=allow|ask|deny
karox mcp session deselect|permissions ...
karox mcp status [--session-id ID] [--probe]
karox mcp call <tool> --session-id ID --arguments JSON [--idempotency-key KEY]
karox mcp credential set|show|delete|doctor ...
```

See [MCP.md](MCP.md) for the permission model.

## Skills

```text
karox skill list [--repository PATH] [--skill-dir PATH]...
karox skill show <name> [...]
karox skill load <name> [...]
```

## Credentials and paths

```text
karox credential set|show|delete|doctor ...
karox paths
```

`credential show` prints an opaque reference and a fingerprint, never the
secret.