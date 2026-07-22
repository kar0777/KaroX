<div align="center">

# KaroX

### Controlled local tooling for AI coding agents

**KaroX turns a selected local Git repository into a guarded workspace where compatible AI clients can inspect context, perform approved development actions, run checks, review changes, and produce evidence-backed completion reports.**

[![Release](https://img.shields.io/badge/release-v4.1.4-7C3AED)](https://github.com/kar0777/KaroX/releases/latest)
[![Notion](https://img.shields.io/badge/Notion-Custom%20Agent-000000?logo=notion)](NOTION.md)
[![Windows](https://img.shields.io/badge/Windows-PowerShell-0078D4?logo=windows)](#install-in-one-command)
[![macOS](https://img.shields.io/badge/macOS-Bash-000000?logo=apple)](#install-in-one-command)
[![Linux](https://img.shields.io/badge/Linux-Bash-FCC624?logo=linux&logoColor=black)](#install-in-one-command)
[![Quality](https://github.com/kar0777/KaroX/actions/workflows/quality.yml/badge.svg)](https://github.com/kar0777/KaroX/actions/workflows/quality.yml)
[![CodeQL](https://github.com/kar0777/KaroX/actions/workflows/codeql.yml/badge.svg)](https://github.com/kar0777/KaroX/actions/workflows/codeql.yml)

[Quick start](QUICKSTART.md) · [Changelog](CHANGELOG.md) · [Research](RESEARCH.md) · [Private inference use case](docs/private-inference-use-case.md) · [Benchmark plan](benchmarks/README.md) · [Русская версия](README_RU.md)

</div>

## What KaroX does today

KaroX is an engineering project, not a mockup. The current release provides:

- repository-scoped API and MCP sessions;
- explicit Observe, Build, Resume, and Advanced access profiles;
- persistent sessions with supervised process recovery;
- protected endpoints using a session-specific key;
- repository path confinement and sensitive-file filtering;
- guarded file operations, patches, checkpoints, commands, checks, and local Git workflows;
- idempotency protection for mutating requests so retries do not repeat completed changes;
- a hard no-push and no-publish policy;
- structured build/test/lint results with retry support for flaky checks;
- managed development servers, localhost-only HTTP fetches, and a headless browser;
- opt-in target-window screenshots and input controls;
- preflight checks, diagnostics, request IDs, audit logs, and a live Control Center;
- source-free redacted support bundles;
- Windows, macOS, and Linux support with CI and CodeQL coverage;
- atomic updates with staging validation and rollback-safe replacement.

These controls govern the local bridge. They do not automatically guarantee that a connected model provider offers private or confidential inference.

## Research track: confidential inference

KaroX may send approved source fragments, repository structure, build errors, test output, diffs, and tool results to a model. For private or unreleased code, the provider-side processing boundary matters as much as local access control.

The confidential-inference research track evaluates whether KaroX can preserve its coding-agent workflow while using a provider that offers hardware-backed protected execution or comparable controls. The work will measure API compatibility, structured output, tool use, failure recovery, verification, latency, cost, retention boundaries, and attestation claims.

This is a **research and integration track**, not a claim that every current KaroX connection is confidential. Provider claims will be separated from properties that can be independently verified.

- [Concrete private-inference use case and threat model](docs/private-inference-use-case.md)
- [Ten-task coding-agent safety benchmark](benchmarks/README.md)
- [Per-run result template](benchmarks/results-template.csv)
- [Research overview and integrity rules](RESEARCH.md)

A video is not required to inspect the proposed demonstration. The benchmark documentation includes a text-only reproducible scenario based on diffs, test output, redacted logs, permission events, and Git state.

## Install in one command

The command installs the latest stable KaroX release, its dependencies, the `karox` command, and the platform launcher. Run the same command again to reinstall or upgrade.

### Windows

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1 | iex"
```

### macOS / Linux

```bash
curl -fsSL https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.sh | bash
```

Launch Project Flight Deck:

```bash
karox
```

Or start directly with Notion selected:

```bash
karox notion
```

## New in KaroX 4.1

- **Safe atomic updates:** a complete staging application is built and validated before the installed app is replaced; interrupted swaps can be restored.
- **Supervised sessions:** the watchdog can restart failed or hung local API processes while persistent session state preserves the active workflow.
- **Retry-safe mutations:** idempotency keys prevent a dropped connection from duplicating writes, patches, commands, file operations, or commit actions.
- **Stronger agent tools:** structured check matrices, async jobs, file checkpoints, binary and image reads, project mapping, persistent memos, and broader local Git review.
- **Browser and desktop workflows:** managed dev servers, a headless browser, screenshots, and opt-in target-window input for visual verification.
- **Security improvements:** entropy-aware secret scanning, protected endpoints, bounded queues and timeouts, redacted audit logs, and hard blocks on push, publish, and authentication commands.
- **Reliable connectivity:** per-session Cloudflare or Tailscale choices and safer handling of parallel sessions.

See the [changelog](CHANGELOG.md) for exact version-by-version details.

## Product commands

| Command | Purpose |
|---|---|
| `karox` | Open Project Flight Deck |
| `karox version` | Show the installed version |
| `karox status` | Inspect saved and live sessions |
| `karox stop` | Stop a live KaroX session safely |
| `karox doctor` | Run fast product diagnostics |
| `karox doctor --deep` | Run the full endpoint/security harness |
| `karox update --check` | Check the stable release channel |
| `karox update` | Install the newest stable release |
| `karox support` | Create a redacted support ZIP |
| `karox dashboard` | Open Control Center for a live session |
| `karox notion` | Start directly with the Notion target |

JSON output is available for `version`, `status`, and `doctor`.

## Control Center

Every live session exposes:

```text
https://<current-session-host>/control
```

Run `karox dashboard` to open the correct session automatically. Paste the session key copied with `K` into the page. It stays in the current browser tab through `sessionStorage`, is never placed in the URL, and can be removed with **Forget key**.

Control Center shows the repository, branch, access mode, task state, changed files, Git status, capabilities, and current Mission Control brief.

## Notion Custom Agent in three steps

> Requires a Notion workspace where Custom Agents can add a custom Streamable HTTP MCP server and store a protected Bearer credential.

1. Run `karox notion`, choose the repository and access profile, then wait for `● LIVE`.
2. Press `C` and paste the generated connection prompt into the Notion Custom Agent. Press `K` and place the key only in Notion's protected Bearer-token field.
3. Let the agent run `karox_preflight`, then send the real coding task as a separate message.

The Notion agent receives purpose-built tools for repository context, file operations, development commands, Git review, safe commits, and completion reports. It does not bypass KaroX safeguards. See [the full Notion guide](NOTION.md).

## Why KaroX

- **Local-first control:** each server is scoped to the repository selected by the user, and context leaves the machine only through an explicitly connected client or endpoint.
- **Explicit access:** Observe, Build, Resume, and Advanced profiles expose the real permission level.
- **Context before action:** Mission Control gives the agent a fresh, secret-filtered operating brief.
- **Retry-safe execution:** idempotency and persistent state reduce duplicate side effects after transport failures.
- **Safe Git workflow:** branch validation, checkpoints, reviewed commits, local merge/rebase support, and a hard no-push policy.
- **Provider-ready:** native handoffs for PromptQL and Notion, plus generic OpenAPI and letaido compatibility.
- **Operational visibility:** session status, Control Center, request IDs, diagnostics, update checks, events, jobs, and support bundles.
- **Researchable behavior:** permission events, tool results, diffs, tests, screenshots, and Git state can support reproducible agent evaluations.

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
   - **Build** — isolated branch, edits, checks, checkpoints, and safe commit;
   - **Resume** — continue a persisted workspace session;
   - **Advanced** — extended commands inside the selected repository.
4. Give the session a history label. It is never treated as an AI task.
5. Choose Cloudflare Tunnel or Tailscale Funnel for the session.
6. Wait for `● LIVE`.

KaroX starts the repository-scoped API and opens it through the selected tunnel. The generated handoff contains the URL, branch, mode, preflight, and safety rules.

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

## Safety contract

- every protected endpoint requires the session-specific key;
- repository access cannot leave the selected `repoRoot`;
- secret paths, high-entropy credentials, and traversal are blocked;
- Observe remains read-only;
- dangerous commands, authentication commands, push, and publishing operations are blocked;
- mutating retries use idempotency protection;
- large output can be captured to a generated file;
- commits are created only through guarded KaroX tools;
- audit logs rotate and sensitive values are redacted;
- support bundles contain no source code and scan for known session secrets;
- KaroX never runs `git push`.

HTTP endpoints remain backward compatible. `repopilot` remains available as a compatibility alias.

## Documentation

- [Changelog](CHANGELOG.md)
- [Quick start / Быстрый старт](QUICKSTART.md)
- [Product guide](PRODUCT_GUIDE.md)
- [Research overview](RESEARCH.md)
- [Private inference use case](docs/private-inference-use-case.md)
- [Coding-agent benchmark](benchmarks/README.md)
- [Notion Custom Agent provider](NOTION.md)
- [PromptQL connection](examples/promptql-connect.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Security](SECURITY.md)
