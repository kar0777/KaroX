# ★ KaroX v3.11.0 — Notion Custom Agent provider

KaroX can now expose a selected local Git repository to a **Notion Custom Agent** through a protected Streamable HTTP MCP connection.

## Install or update

### Windows

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1 | iex"
```

### macOS / Linux

```bash
curl -fsSL https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.sh | bash
```

The public bootstrap scripts are pinned to this stable release. Running the same command again updates an existing installation.

## Start with Notion selected

```bash
karox notion
```

Then:

1. choose a repository and access profile;
2. wait for `● LIVE`;
3. press `C` and paste the connection prompt into the Notion Custom Agent;
4. press `K` and store the key only in Notion's protected Bearer-token field;
5. let the agent call `karox_preflight`;
6. send the real task as a separate message.

## What is new

- first-class **NOTION** target in the KaroX Flight Deck;
- protected `/mcp` endpoint using the current KaroX session key;
- purpose-built tools for preflight, Mission Control, files, commands, Git review, safe commit, and completion reports;
- `karox notion`, `karox notion install`, `karox notion doctor`, `karox notion status`, and `karox notion docs`;
- Windows, macOS, and Linux support;
- stable one-command installer pinned to `v3.11.0`;
- automated source-contract, dependency, launcher, and packaging checks;
- polished English and Russian documentation.

## Security

The Notion provider reuses the existing KaroX repository API in-process. It does not bypass KaroX safeguards:

- access remains restricted to the selected `repoRoot`;
- Observe remains read-only;
- secret paths and traversal are blocked;
- dangerous commands and publishing operations are blocked;
- commits use the guarded KaroX endpoint;
- `git push` remains prohibited;
- the session key is not inserted into generated prompts.

## Notion requirement

A live connection requires a Notion workspace where Custom Agents can add a custom Streamable HTTP MCP server and store a protected Bearer credential. Availability may depend on the Notion plan and workspace rollout.

## Documentation

- [Notion provider guide](NOTION.md)
- [Quick start](QUICKSTART.md)
- [Security](SECURITY.md)
- [Troubleshooting](TROUBLESHOOTING.md)
