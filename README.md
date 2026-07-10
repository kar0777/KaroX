<div align="center">

# ★ Star For KaroX

### Your local code. Your rules. AI that arrives with the right context.

**KaroX turns any local Git repository into a secure, observable workspace for AI agents.**

[![Windows](https://img.shields.io/badge/Windows-PowerShell-0078D4?logo=windows)](#install-in-one-command)
[![macOS](https://img.shields.io/badge/macOS-Bash-000000?logo=apple)](#install-in-one-command)
[![Linux](https://img.shields.io/badge/Linux-Bash-FCC624?logo=linux&logoColor=black)](#install-in-one-command)
[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?logo=python&logoColor=white)](requirements.txt)
[![License](https://img.shields.io/badge/license-see%20LICENSE-7C3AED)](LICENSE)

[Quick start](QUICKSTART.md) · [Русская версия](README_RU.md) · [Security](SECURITY.md) · [Troubleshooting](TROUBLESHOOTING.md)

</div>

KaroX starts a repository-scoped OpenAPI server, exposes it through a secure tunnel, and produces a ready-to-use AI handoff. You choose the repository and access profile; the AI receives the exact branch, permissions, live preflight, and safety rules.

**Why KaroX**

- **Local-first:** source code stays on your machine; the server is scoped to the repository you select.
- **Explicit control:** Observe, Build, Resume, and Advanced profiles make permissions visible.
- **Context before action:** Mission Control gives AI a fresh, secret-free operating brief.
- **Safe Git workflow:** branch checks, generated-file cleanup, reviewed commits, and push blocked by policy.
- **Built for daily use:** bilingual adaptive Flight Deck, session history, diagnostics, and one-command setup.

## First launch

```bash
karox
```

On the first launch KaroX asks:

1. **Language** — English or Русский. The choice is saved and can be changed later with `G → L`.
2. **AI target** — PromptQL is recommended; generic OpenAPI and letaido compatibility modes remain available.

For CI or redirected-input launches, preselect the language with `KAROX_LANGUAGE=en` or `KAROX_LANGUAGE=ru`. Interactive first launch still asks explicitly.

The Star For KaroX landing animation then opens **KAROX / PROJECT FLIGHT DECK** automatically. Set `KAROX_NO_ANIMATION=1` to disable animation; it is also disabled for redirected output.

## Create a workspace

1. Press `N`.
2. Select or paste a Git repository path.
3. Choose an access profile:
   - **Observe** — read-only analysis;
   - **Build** — isolated `promptql/*` branch, edits, checks, and safe commit;
   - **Resume** — continue the current workspace branch;
   - **Advanced** — extended repository commands.
4. Give the session a history label. The label is never treated as an AI task.
5. Wait for `● LIVE`.

KaroX starts the local API and Cloudflare Tunnel or Tailscale Funnel. You never need to assemble the URL, branch, or permission context manually.

## AI handoff

Open the session card:

| Key | Action |
|---|---|
| `V` | Verify AI readiness locally |
| `M` | Open Mission Control context |
| `C` | Copy the localized connection prompt |
| `T` | Copy the localized real-task template |
| `K` | Copy `X-API-Key` separately |
| `P` | Copy provider ID |
| `A` | Copy the complete handoff |
| `L` | View logs |
| `S` | Stop the session |

**Run `V` before handoff.** KaroX calls `/session`, `/health`, `/git/status`, and `/context/brief` locally and verifies that `repoRoot` and `branch` match the session card.

Press **`M`** for **Mission Control**: a live, secret-free brief of the current task, permissions, Git cleanliness, detected project context, warnings, and recommended next action. The AI can call `GET /context/brief` again at any time instead of relying on stale handoff text. A warning does not modify the repository; review existing diffs before continuing.

Paste the connection prompt into your AI client. Enter the key only in its protected credential form—never in chat. After preflight succeeds, send a separate message containing the real task.

## Install in one command

### Windows

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1 | iex"
```

### macOS / Linux

```bash
curl -fsSL https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.sh | bash
```

The primary command is `karox`. `repopilot` remains a compatibility alias for existing installations.

## Terminal experience

KaroX uses one adaptive terminal design system across PowerShell and POSIX shells:

- a 52–88-column layout that remains readable in narrow terminals;
- workspace cards that expose repository, status, branch, access, tunnel, and AI target at a glance;
- consistent notices for progress, success, warnings, errors, and empty states;
- a keyboard-first command bar and explicit AI launch sequence;
- human-readable Mission Control recommendations instead of internal action codes;
- structural symbols and labels that remain meaningful in monochrome terminals or whenever color is unavailable.

## Flight Deck navigation

| Key | Action |
|---|---|
| `N` | New session |
| number | Open a workspace |
| `G` | Settings: language, AI target, tunnel |
| `D` | Diagnostics |
| `R` | Refresh |
| `U` | Clear stopped history |
| `X` | Stop all sessions |
| `Q` | Close the manager without stopping LIVE sessions |

## Safety contract

- every endpoint requires the session-specific `X-API-Key`;
- repository access is restricted to the selected `repoRoot`;
- secret paths and traversal outside the repository are blocked;
- writable profiles use an explicit branch and permission context;
- large output should use `capture=file`;
- generated files are cleaned through `/git/cleanup-generated`;
- commits are created only through `/git/commit`;
- `git push` is never performed by the KaroX workflow.

HTTP endpoints and the security contract remain backward compatible.

More:
- [Quick start / Быстрый старт](QUICKSTART.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Security](SECURITY.md)
- [PromptQL connection](examples/promptql-connect.md)
