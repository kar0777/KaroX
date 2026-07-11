<div align="center">

# ★ Star For KaroX

### Your local code. Your rules. AI that arrives with the right context.

**KaroX turns a local Git repository into a secure workspace for PromptQL, Notion Custom Agents, and other AI clients.**

[![Release](https://img.shields.io/badge/release-v3.11.0-7C3AED)](https://github.com/kar0777/KaroX/releases/latest)
[![Notion](https://img.shields.io/badge/Notion-Custom%20Agent-000000?logo=notion)](NOTION.md)
[![Windows](https://img.shields.io/badge/Windows-PowerShell-0078D4?logo=windows)](#install-in-one-command)
[![macOS](https://img.shields.io/badge/macOS-Bash-000000?logo=apple)](#install-in-one-command)
[![Linux](https://img.shields.io/badge/Linux-Bash-FCC624?logo=linux&logoColor=black)](#install-in-one-command)
[![CI](https://github.com/kar0777/KaroX/actions/workflows/notion-provider.yml/badge.svg)](https://github.com/kar0777/KaroX/actions/workflows/notion-provider.yml)

[Quick start](QUICKSTART.md) · [Notion setup](NOTION.md) · [Русская версия](README_RU.md) · [Security](SECURITY.md) · [Troubleshooting](TROUBLESHOOTING.md)

</div>

## Install in one command

The command installs the latest stable KaroX release, all Python dependencies, the `karox` command, and the platform launcher. Run the same command again to update.

### Windows

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1 | iex"
```

### macOS / Linux

```bash
curl -fsSL https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.sh | bash
```

Then launch normally:

```bash
karox
```

Or launch directly with the Notion provider selected:

```bash
karox notion
```

## Notion Custom Agent in three steps

> Requires a Notion workspace where Custom Agents can add a custom Streamable HTTP MCP server and store a protected Bearer credential.

1. Run `karox notion`, choose the repository and access profile, then wait for `● LIVE`.
2. Press `C` and paste the generated connection prompt into the Notion Custom Agent. Press `K` and place the key only in Notion's protected Bearer-token field.
3. Let the agent run `karox_preflight`, then send the real coding task as a separate message.

The Notion agent receives purpose-built tools for repository context, file operations, development commands, Git review, safe commits, and completion reports. It does not bypass KaroX safeguards. See [the full Notion guide](NOTION.md).

## Why KaroX

- **Local-first:** your source stays on your machine; each server is scoped to the repository you selected.
- **Explicit access:** Observe, Build, Resume, and Advanced profiles expose the real permission level.
- **Context before action:** Mission Control gives the agent a fresh, secret-free operating brief.
- **Safe Git workflow:** branch validation, generated-file cleanup, reviewed commits, and a hard no-push policy.
- **Provider-ready:** native handoffs for PromptQL and Notion, plus generic OpenAPI and letaido compatibility.
- **Daily-use UX:** bilingual adaptive Flight Deck, session history, diagnostics, and one-command updates.

## Supported AI targets

| Target | Connection | Best for |
|---|---|---|
| **PromptQL** | Custom OpenAPI integration | Shared coding workspace |
| **Notion Custom Agent** | Protected Streamable HTTP MCP | Coding and project work from Notion |
| **Other client** | Generic OpenAPI + `X-API-Key` | Any compatible AI tool |
| **letaido.com** | Direct protected-header compatibility | Existing letaido workflows |

Choose the target on first launch or change it later through `G → A`. `karox notion` temporarily forces Notion for that launch.

## Create a workspace

1. Press `N`.
2. Select or paste a local Git repository path.
3. Choose an access profile:
   - **Observe** — read-only analysis;
   - **Build** — isolated `promptql/*` branch, edits, checks, and safe commit;
   - **Resume** — continue the current workspace branch;
   - **Advanced** — extended commands inside the selected repository.
4. Give the session a history label. It is never treated as an AI task.
5. Wait for `● LIVE`.

KaroX starts the repository-scoped API and opens it through Cloudflare Tunnel or Tailscale Funnel. The generated handoff already contains the URL, branch, mode, preflight, and safety rules.

## Session controls

| Key | Action |
|---|---|
| `V` | Verify AI readiness locally |
| `M` | Open live Mission Control context |
| `C` | Copy the localized connection prompt |
| `T` | Copy the real-task template |
| `K` | Copy the session key separately |
| `P` | Copy provider ID |
| `A` | Copy the complete handoff |
| `L` | View logs |
| `S` | Stop the session |

Run `V` before handoff. KaroX checks `/session`, `/health`, `/git/status`, and `/context/brief`, then verifies the exact repository and branch.

## Notion diagnostics

```bash
karox notion doctor
```

Other provider helpers:

```bash
karox notion install
karox notion status
karox notion docs
```

## Safety contract

- every endpoint requires the session-specific key;
- repository access cannot leave the selected `repoRoot`;
- secret paths and traversal are blocked;
- Observe remains read-only;
- dangerous commands and publishing operations are blocked;
- large output can be captured to a generated file;
- commits are created only through the guarded KaroX endpoint;
- KaroX never runs `git push`.

HTTP endpoints remain backward compatible. `repopilot` remains available as a compatibility alias.

## Documentation

- [Quick start / Быстрый старт](QUICKSTART.md)
- [Notion Custom Agent provider](NOTION.md)
- [PromptQL connection](examples/promptql-connect.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Security](SECURITY.md)
- [Changelog](CHANGELOG.md)
