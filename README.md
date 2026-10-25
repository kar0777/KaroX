# KaroX

A local coding agent with a compact terminal client, typed tools, durable
sessions and MCP support.

This is the clean KaroX 6 line. It keeps what proved itself in v5 — the agent
kernel, capability and risk policy, session evidence, MCP in both directions,
and the hosted bridge that lets ChatGPT Web, Claude Web and HyperAgent call
KaroX tools over OAuth. Browser profiles, orchestration experiments and the
legacy shell stay behind.

## Status

Alpha (`6.0.0a1`). The CLI, terminal client, providers, sessions, outbound MCP,
the inbound OAuth/MCP bridge and skills work end to end. The migration record in
[docs/MIGRATION.md](docs/MIGRATION.md) and the parity audit in
[docs/PARITY.md](docs/PARITY.md) list what was restored, improved or deliberately
left behind.

## Install

```text
cd D:\проекты\KaroX-v5\lab\kernel-duel-20261025\integrate
python -m venv .venv
. .venv/Scripts/activate        # Windows
python -m pip install -e ".[dev]"
```

## First run

```text
karox provider presets
karox provider setup openai --model gpt-5 --stdin-key
karox model discover openai --save
karox doctor
karox
```

For a ChatGPT/Claude/HyperAgent connector:

```text
karox bridge saved create my-dev --target-profile chatgpt-web --repository . --tunnel tailscale
karox bridge connect --saved my-dev
```

`karox` with no arguments opens the terminal client. Inside it, `/provider`
connects a provider, `/model` picks a model, `/mcp` manages outbound servers,
`/bridge` lists saved inbound bridges, and
`/help` lists every command and key.

Without a terminal (CI, pipes), the scriptable surface is:

```text
karox agent run --repository . --task "fix the failing test" --stream-json
karox session list --json
karox mcp status --json
```

## What it does

- **Typed tools, not a shell.** The agent reads, edits, searches, runs approved
  checks and inspects Git through named tools. Every mutation passes a
  capability profile and a risk decision before execution.
- **Durable sessions.** Each step is persisted; a stopped run resumes. Sessions
  can be forked, archived, renamed and revoked.
- **Verified completion.** A run reports `verified` only when its evidence and
  checks support it; the terminal shows the reason and the changed files.
- **MCP.** Add stdio or Streamable HTTP servers, discover tools, choose them per
  session, and call them with schema-digest checks and explicit permissions.
- **One compact client.** Streaming answers, tool observations, a model picker,
  sessions, provider setup and MCP in one Textual screen built from a small
  widget set and a single stylesheet.

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [CLI reference](docs/CLI.md)
- [Terminal client](docs/TUI.md)
- [Sessions](docs/SESSIONS.md)
- [MCP](docs/MCP.md)
- [Migration from v5](docs/MIGRATION.md)
- [Development and release gates](docs/DEVELOPMENT.md)
- [Changelog](docs/CHANGELOG.md)

## License

MIT. See [LICENSE](LICENSE).