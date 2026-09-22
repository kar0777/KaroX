# KaroX 5

<div align="center">

**A local control plane for autonomous AI coding — one runtime, many agents, your repository.**

![Status](https://img.shields.io/badge/status-beta-f59e0b)
![CI](https://github.com/kar0777/KaroX/actions/workflows/ci.yml/badge.svg?branch=main)
![Product quality](https://github.com/kar0777/KaroX/actions/workflows/quality.yml/badge.svg?branch=main)
![Release](https://img.shields.io/github/v/release/kar0777/KaroX?include_prereleases&label=release)
![Runtime](https://img.shields.io/badge/runtime-5.0.0rc4-2563eb)
![Python](https://img.shields.io/badge/python-%3E%3D3.10-3776ab)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-64748b)
![Protocol](https://img.shields.io/badge/protocol-MCP-7c3aed)
[![Supporters](https://img.shields.io/badge/supporters-33-ec4899)](SUPPORTERS.md)

Run AI coding agents on local Git repositories without giving one provider
control of your permissions, sessions, or evidence.

**KaroX 5 beta is now the primary `main` line · `5.0.0rc4`**

### 🤝 Supported by

<table>
<tr>
<td align="center"><a href="https://routing.run"><img src="https://www.google.com/s2/favicons?domain=routing.run&sz=128" width="38" height="38" alt="routing.run"><br><sub><b>routing.run</b></sub></a></td>
<td align="center"><a href="https://vivgrid.com"><img src="https://www.google.com/s2/favicons?domain=vivgrid.com&sz=128" width="38" height="38" alt="Vivgrid"><br><sub><b>Vivgrid</b></sub></a></td>
<td align="center"><a href="https://puter.com"><img src="https://www.google.com/s2/favicons?domain=puter.com&sz=128" width="38" height="38" alt="Puter"><br><sub><b>Puter</b></sub></a></td>
<td align="center"><a href="https://www.verda.com"><img src="https://www.google.com/s2/favicons?domain=verda.com&sz=128" width="38" height="38" alt="Verda"><br><sub><b>Verda</b></sub></a></td>
<td align="center"><a href="https://tinfoil.sh"><img src="https://www.google.com/s2/favicons?domain=tinfoil.sh&sz=128" width="38" height="38" alt="Tinfoil"><br><sub><b>Tinfoil</b></sub></a></td>
<td align="center"><a href="https://www.stepfun.com"><img src="https://www.google.com/s2/favicons?domain=stepfun.com&sz=128" width="38" height="38" alt="StepFun"><br><sub><b>StepFun</b></sub></a></td>
</tr>
<tr>
<td align="center"><a href="https://browser-use.com"><img src="https://www.google.com/s2/favicons?domain=browser-use.com&sz=128" width="38" height="38" alt="Browser Use"><br><sub><b>Browser Use</b></sub></a></td>
<td align="center"><a href="https://tavily.com"><img src="https://www.google.com/s2/favicons?domain=tavily.com&sz=128" width="38" height="38" alt="Tavily"><br><sub><b>Tavily</b></sub></a></td>
<td align="center"><a href="https://sentry.io"><img src="https://www.google.com/s2/favicons?domain=sentry.io&sz=128" width="38" height="38" alt="Sentry"><br><sub><b>Sentry</b></sub></a></td>
<td align="center"><a href="https://wandb.ai"><img src="https://www.google.com/s2/favicons?domain=wandb.ai&sz=128" width="38" height="38" alt="W&B"><br><sub><b>W&amp;B</b></sub></a></td>
<td align="center"><a href="https://openrouter.ai"><img src="https://www.google.com/s2/favicons?domain=openrouter.ai&sz=128" width="38" height="38" alt="OpenRouter"><br><sub><b>OpenRouter</b></sub></a></td>
<td align="center"><a href="https://www.blockrun.ai"><img src="https://www.google.com/s2/favicons?domain=blockrun.ai&sz=128" width="38" height="38" alt="BlockRun"><br><sub><b>BlockRun</b></sub></a></td>
</tr>
</table>

**All 33 acknowledged supporters:** [routing.run](https://routing.run) · [Vivgrid](https://vivgrid.com) · [Puter](https://puter.com) · [OmniaKey](https://omniakey.com) · [Browser Use](https://browser-use.com) · [Verda](https://www.verda.com) · [Tinfoil](https://tinfoil.sh) · [fal](https://fal.ai) · [Tavily](https://tavily.com) · [Cohere](https://cohere.com) · [Chutes](https://chutes.ai) · [EmpirioLabs](https://empiriolabs.ai) · [Langfuse](https://langfuse.com) · [AIReiter](https://aireiter.com) · [Scout APM](https://scoutapm.com) · [APIMaster](https://apimaster.ai) · [Merge Gateway](https://gateway.merge.dev) · [OpenRouter](https://openrouter.ai) · [Weights & Biases (W&B)](https://wandb.ai) · [BlockRun](https://www.blockrun.ai) · [UnifyLLM](https://www.unifyllm.com) · [Novita AI](https://novita.ai) · [BazaarLink](https://bazaarlink.ai) · [CostRouter](https://www.costrouter.ai) · [Ellipsis](https://www.ellipsis.dev) · [Advanced Installer](https://www.advancedinstaller.com) · [Bump.sh](https://bump.sh) · [Sentry](https://sentry.io) · [Socket](https://socket.dev) · [RouterPlex](https://routerplex.com) · [LangWatch](https://langwatch.ai) · [LLMTR / Knowhy](https://www.knowhy.ai) · [StepFun](https://www.stepfun.com)

**[Meet all 33 supporters — full logo wall & acknowledgements →](SUPPORTERS.md)**

</div>

KaroX is a local control plane for ChatGPT, Claude, API models, and compatible
MCP clients. Every local action goes through one repository-scoped runtime with
explicit permissions, retry-safe mutations, durable sessions, approved checks,
Git evidence, and secret filtering.

KaroX is not a model, an IDE, or an operating-system sandbox. An explicitly
approved process still runs with the operating-system rights of the KaroX user.
The runtime reduces authority through repository confinement, capabilities,
allowlists, leases, idempotency, and hard blocks; it does not virtualize the
machine.

> **Release status:** the packaged runtime is `5.0.0rc4`. Deterministic local
> contracts are extensively tested, but live ChatGPT Web, Claude Web, and paid
> provider conformance records are still pending. See the
> [5.0 release scope](docs/V5_RELEASE_SCOPE.md),
> [4.x migration contract](docs/MIGRATION_V4_TO_V5.md), and
> [live conformance records](docs/conformance/README.md).

## Why KaroX

| What you want | What KaroX does |
| --- | --- |
| **Autonomous coding without prompt spam** | Normal reads, edits, tests, checks, dev commands, and local commits run without nuisance approval loops. |
| **One safety boundary for every agent** | ChatGPT, Claude, API models, MCP clients, and local workers reach the machine through the same repository-scoped Core policy. |
| **Rare, meaningful approvals** | Protected mode stops destructive source deletion and real external commit points; Bypass can opt into autonomous deletion *inside the repository* without weakening credential/system boundaries. |
| **Work continues while a gate is pending** | A blocked dangerous action is deferred so the agent can finish independent work and ask only when the gate actually becomes necessary. |
| **Portable release evidence** | Windows, macOS, and Linux CI exercise the same wheel, tests, release checks, Git contracts, and secret-scan rules. |

## The primary flow

A stable KaroX 5 release is defined by one complete workflow:

1. Install KaroX and run `karox` inside a Git repository.
2. Choose Russian or English and an access profile.
3. Connect ChatGPT Web, Claude Web, or a supported API provider.
4. Ask the agent to make a bounded repository change.
5. Let KaroX run an explicitly approved verification command.
6. Inspect check results, Git status, Git diff, and the evidence report.
7. Restart and resume the durable session without replaying a completed change.

Features that do not support this path remain Preview, Experimental, or Legacy
until their own evidence gates pass.

## Install

The beta candidate is **`v5.0.0rc4`**. The recommended first install is the
**preview portable bootstrap**: no preinstalled Python, pipx, uv, or administrator
rights are needed. It verifies the release bundle's SHA-256, installs it in your
user directory, and uses bundled `uv` to obtain managed Python and start KaroX.
Internet access is needed for the bundle, Python, and Python packages.

**Windows — paste into PowerShell:**

```powershell
& ([scriptblock]::Create((Invoke-RestMethod -Uri 'https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1' -TimeoutSec 30))) -Channel preview
```

**macOS / Linux — paste into a terminal with Bash and curl:**

```bash
curl --proto '=https' -fsSL --max-time 30 https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.sh | bash -s -- --channel preview
```

Portable bundles target Windows, macOS, and Linux x64/ARM64. Installation starts
KaroX by default; set `KAROX_NO_START=1` to install without starting it. The
bootstrap keeps the previous portable installation until the verified replacement
is ready. A TLS, timeout, checksum, or extraction error **stops installation**;
it does not silently execute an unchecked source fallback. Retry after fixing the
reported error. Release assets must have been published for the selected channel.
The default channel follows `RELEASE.json`; `preview` follows `PREVIEW.json`.

**Windows PATH — usable immediately:** the bootstrap updates PATH in its own
PowerShell process and persists the user PATH. If you launched it from another
process, paste this into the **original** window (default install location):

```powershell
$env:Path = "$env:LOCALAPPDATA\KaroX;$env:Path"
karox
# Direct launch works even before PATH is refreshed:
& "$env:LOCALAPPDATA\KaroX\KaroX.cmd"
```

With `KAROX_INSTALL_ROOT`, use the exact PATH command printed by the installer.
On macOS/Linux, if `karox` is not found, run
`export PATH="$HOME/.local/bin:$PATH"` or `~/.local/bin/karox` directly.
Then run `karox` inside the Git repository you want to work with;
`karox quickstart` prints the next setup step.

When a web bridge requests automatic tunnel selection, KaroX uses an installed
Tailscale, otherwise Cloudflare. A Tailscale login/Funnel error is reported, not
silently replaced with Cloudflare. Explicit `--tunnel tailscale`,
`--tunnel cloudflare`, `--tunnel custom`, and `--cloudflared PATH` remain explicit.
Cloudflare setup reuses an installed cloudflared or lazily downloads a pinned,
checksum-verified executable to the user-local runtime cache. Automatic downloads
support Windows x64 and macOS/Linux x64/ARM64; the pinned upstream release has no
Windows ARM64 executable. Unsupported platforms/offline machines must supply
`--cloudflared PATH` or use Tailscale. Managed cloudflared downloads are limited
to 96 MiB, with a 120-second transfer budget and 15-second individual I/O timeout;
macOS archives are verified and only the single regular executable is copied.
No tunnel OS service is installed. Credentials still require the OS keyring;
there is no plaintext fallback.

**Alternative: existing Python tool environment.** After this version is
published to PyPI:

```bash
pipx install "karox-runtime==5.0.0rc4"
# or
uv tool install "karox-runtime==5.0.0rc4" --prerelease=allow
```

**Source/developer installs:** use a reviewed checkout of the exact tag and run
`./install.karox.sh` or `.\install.karox.ps1`, or:

```bash
pipx install --force "git+https://github.com/kar0777/KaroX.git@v5.0.0rc4"
```

A missing portable asset (HTTP 404) or a non-release `KAROX_BOOTSTRAP_REF` can
use the bootstrap's source fallback only with an independently trusted
`KAROX_SOURCE_SHA256` for that source archive. The POSIX source fallback requires
Python 3. Do not set a checksum from an untrusted download just to bypass a
verification error.

## Terminal client

`karox` opens the full-screen client. Ordinary text is an agent task; it is not
parsed as command-line syntax.

- The first launch asks for Russian or English.
- `/connect` configures an API provider, a hosted client, or both.
- Provider credentials are stored in the OS keyring.
- `F5` discovers models where the provider exposes discovery.
- `F10` performs a minimal live connection test before activating a route.
- `Ctrl+B` opens hosted bridge setup.
- `/language`, `/help`, and `/quit` provide basic UI control.

The header shows repository, model, session, and bridge state. The TUI does not
bypass Core policy; it is a view over the same services used by the CLI.

## Connect hosted clients

The bridge command creates a repository-bound session, temporary approval
credential, local MCP endpoint, and managed HTTPS tunnel:

```bash
karox bridge connect chatgpt-web --repository . --write
karox bridge connect claude-web --repository . --write
karox bridge connect hyperagent-web --repository . --write
```

Omit `--write` for the read-only tool set. KaroX prints the MCP URL and connection
instructions; approval credentials remain in the OS keyring instead of being
printed into logs or chat. When a client needs an OAuth approval password, copy
the current value locally with `karox bridge oauth approval-password --saved NAME --copy`.
For HyperAgent, leave "Bring my own OAuth app" off — KaroX advertises OAuth
discovery and Dynamic Client Registration, so no Client ID or Client Secret is
entered by hand. See `docs/CONNECTIVITY.md` for the full HyperAgent steps.

A Cloudflare Quick Tunnel URL changes after restart. For repeatable setup, save
only the non-secret launch policy and reconnect with a short command:

```bash
karox bridge saved create full-dev --target-profile chatgpt-web --repository . --write --tunnel tailscale --deadline-preset full-suite
karox bridge connect --saved full-dev
```

Add explicit `--tool` and `--verification-command` allowlists before exposing
checks. Tailscale mode uses a stable `.ts.net` hostname when the tailnet permits
Funnel, refuses to replace existing routes, and stops only its foreground child.
Use `karox bridge saved validate full-dev --json` or
`karox bridge connect --saved full-dev --diagnostics-only` to inspect effective
tools, disabled-tool reasons, verification commands, deadlines, tunnel type, URL
stability, and session lifetime before publication. Real Tailscale account
conformance remains pending under `docs/TAILSCALE_LIVE_RUNBOOK.md`.

For legal inspection of public HTTPS services, create a separate
`browser_control` connection with `--browser-external-https`, a domain allowlist,
headed takeover, and optional redacted network inspection. Headed takeover uses
a dedicated persistent Chrome profile plus the local KaroX Manifest V3 extension;
headless verification retains the isolated Playwright and pinned-proxy backend.
Neither mode exposes cookies or credentials or grants repository write. See
[External HTTPS browser](docs/EXTERNAL_BROWSER.md).

## Connect an API model

KaroX supports adapter contracts for:

- OpenAI Responses;
- Anthropic Messages;
- Gemini GenerateContent;
- generic OpenAI-compatible streaming endpoints.

Use `/connect` in the TUI or the explicit provider, model, and credential CLI
commands. Credentials are opaque keyring references and must not appear in
configuration, session state, logs, support bundles, or MCP descriptors.

KaroX never silently changes provider, privacy boundary, or mutation behavior.
Fallback is restricted to classified failures and explicit configured routes.

## Intelligence orchestration and economy

KaroX 5 now has an opt-in orchestration layer over the same guarded Core. Add API
models, already-paid subscription agents, local models, and explicitly attached
external agents to one **Intelligence Pool**, then assign one orchestrator and
role-specific workers. Automatic quality routing learns only from local KaroX
outcomes that were both accepted and verified; it does not rank models from their
names or unversioned marketing benchmarks.

The economy path reduces duplicated work before it reduces model quality:
content-addressed shared context, role-specific context projections, delta
transfer, deterministic stable prompt prefixes, already-paid-capacity preference,
quota reserve, measured pricing, and independent review. A Savings Receipt shows
dollar/percentage savings only when a measured baseline exists; shadow/replay
counterfactuals are labelled projections.

```bash
karox intelligence list --json
karox intelligence discover-agents --apply
karox orchestrate recipes --json
karox orchestrate plan --objective "Fix retry semantics" --recipe bug-fix --preset maximum_economy
karox orchestrate run --objective "Fix retry semantics" --recipe bug-fix --isolate-implementers --verification-command '["python","-m","pytest","-q"]'
karox orchestrate status RUN_ID
karox mission-control serve RUN_ID
```

`orchestrate run` executes registered API endpoints through the existing
`AgentKernel` and Core policy. It also has guarded built-in adapters for already-
paid Codex and Claude Code subscriptions: Codex implementation is confined to a
KaroX worktree with its workspace-write sandbox, while the Claude built-in path
is read/review-only in safe mode with Read/Glob/Grep. Gemini CLI and OpenCode can
be discovered, but automatic built-in execution stays off until KaroX can prove
an equivalent write boundary. Other local/external workers still require a
separately registered guarded adapter; KaroX never falls back to arbitrary shell
commands, scraped cookies, or raw credentials.

The selected orchestrator owns planner work by default and runs the final
`orchestrator-judge` after independent review/test evidence. High-risk plans add
an independent security review, interrupted running workers require reconciliation
before retry, and detached worker worktrees are never auto-merged. In the TUI,
`/orchestrate`, `/agents`, and `/mission` use the same CLI/runtime services; the
slash menu stays deliberately compact at eight high-frequency commands.

See [Intelligence Orchestration and Economy](docs/KAROX_5_ORCHESTRATION_ECONOMY.md)
for the exact contracts and commands.

## Access profiles

Friendly UI labels map to stable policy identifiers:

- **Observe** (`read_only`) — repository and Git inspection without mutation.
- **Browser** (`browser_control`) — repository/Git read plus an explicitly
  configured, session-isolated browser and network policy; no repository writes,
  process execution, or local commit.
- **Build** (`workspace_write`) — file changes, explicitly approved process and
  check execution, Git status/diff evidence, and selected MCP calls. Build does
  not grant `git.commit`.
- **Advanced** (`elevated`) — explicitly adds guarded local commit and the
  browser, desktop-input, and network capabilities present in elevated policy.

No stable profile grants standing Git push, package publishing, or authentication
authority. An Advanced durable ChatGPT Web bridge may advertise the dedicated
`karox.git.push` surface, but every push requires a machine-verifiable one-shot
user approval bound to the exact remote/branch action; `command.run` cannot
bypass that gate. Resuming work is an action on an existing durable session, not
a new permission level. Some legacy UI may still display a Resume profile during
migration.

## Automation CLI

Scripts and CI use explicit subcommands:

```text
karox paths | session | credential | provider | model | intelligence | skill | mcp | bridge
      | orchestrate | mission-control | economy | pack | target | tool | integration
      | agent | migrate | doctor
```

Examples:

```bash
karox --version
karox doctor
karox provider list
karox model list
karox session list
karox bridge list
karox migrate --json
karox migrate --apply --json
```

`karox migrate` is dry-run by default; `--apply` is the only mode that writes the
sanitized migration destination.

`karox-vnext` remains a compatibility alias during the preview. New docs and
scripts should use `karox`.

## What is stable, preview, and pending

| Surface | Current evidence | Product status |
| --- | --- | --- |
| Core Runtime | deterministic unit, integration, and benchmark coverage | Preview release-critical |
| Native agent loop | real local HTTP/SSE contract E2E with evidence verification | Contract tested |
| OpenAI/Anthropic/Gemini adapters | deterministic adapter and transport coverage | Contract tested; live pending |
| ChatGPT Web bridge | OAuth/DCR/PKCE and real local MCP wire coverage | Experimental; live pending |
| Claude Web bridge | OAuth/DCR/PKCE and real local MCP wire coverage | Experimental; live pending |
| Generic Streamable HTTP MCP | authenticated local wire E2E | Protocol compatible |
| Notion gateway | legacy transport regression | Legacy / Preview |
| PromptQL | local contract and mocked outbound coverage | Experimental |
| Skills and Packs | strict validation and lifecycle coverage | Preview |

A surface is called **live tested** only when a dated record exists under
[`docs/conformance/`](docs/conformance/README.md). Local fake servers prove the
KaroX contract, not the current behavior of a third-party product.

## Security model

Every shipping local action is evaluated against:

- an origin identity;
- a capability;
- the selected repository;
- the active session;
- a mutation lease and fencing token where required;
- an idempotency key for mutating operations.

KaroX additionally enforces secret redaction, path and link confinement,
allowlisted hosted tools, dedicated credential namespaces, a hard block on
arbitrary developer-command Git push/package publication, and an exact-action
one-shot user gate on the dedicated push surface. A failed check cannot be
converted into a verified success by model text.

Read [SECURITY.md](SECURITY.md). Security reports should use the private process
described there, not a public issue containing a credential or private
repository data.

## Migration

KaroX 5 migration is incremental and reversible. The preview discovers legacy
state in dry-run mode by default, preserves the old installation, imports only
validated non-secret metadata, places credentials in the OS keyring, and reports
unsupported data instead of dropping it.

See [Migrating from KaroX 4.x to KaroX 5](docs/MIGRATION_V4_TO_V5.md).

## Sponsors & supporters

KaroX now publicly thanks **33 unique supporters** for concrete development help across model/API access, compute, browser capacity, search, observability, security tooling, developer infrastructure, licenses and research tooling. The expanded list includes StepFun plus the additional confirmed support restored from the project outreach/Gmail record, with duplicate records collapsed.

The same 33 names are carried by the KaroX 5 sponsor registry used by `/sponsors`. See the [full supporter wall](SUPPORTERS.md) for logos, links, support categories, and acknowledgements. A listing is a thank-you, not an endorsement of a provider's security, privacy, pricing, or model claims.

## Verification

CI runs the complete pytest collection, including standalone and parameterized
tests. For a parallel local run after installing the test dependency group:

```bash
python -m pip install --group test
python -m pytest tests -n 6 --dist=loadfile
```

Whole-file scheduling keeps benchmark aggregation and class fixtures together;
no benchmark assertion is skipped to enable parallelism.

For comparison with historical records, the deterministic **unittest-only**
count is maintained separately. The suite is 3525 tests.

```bash
python -m unittest discover -s tests -p "test_*.py"
```

A clean run reports `Ran 3525 tests`. Class-level environment skips can reduce
the executed count. The unittest-plus-legacy subtotal is 3530 (the baseline
plus five KaroX 4 script checks); it is **not** the full pytest collection size.

`python scripts/check_test_count.py` verifies the published counts. Other
release checks include:

```bash
python scripts/check_dependencies.py
python scripts/check_versions.py
python scripts/check_test_count.py
python scripts/check_v5_release.py
python scripts/check_release_workflow.py
python -m ruff check src tests scripts
python -m mypy src/karox
```

Use `python scripts/check_v5_release.py --strict` for release-candidate
rehearsal. It intentionally fails while required live conformance records and
external beta gates are not passed.

## Documentation

- [Quick start](QUICKSTART.md)
- [KaroX 5 release scope](docs/V5_RELEASE_SCOPE.md)
- [Release checklist](docs/RELEASE_CHECKLIST.md)
- [External beta plan](docs/BETA_TEST_PLAN.md)
- [Live test runbook](docs/LIVE_TEST_RUNBOOK.md)
- [Migration from 4.x](docs/MIGRATION_V4_TO_V5.md)
- [Live conformance records](docs/conformance/README.md)
- [Connectivity](docs/CONNECTIVITY.md)
- [External HTTPS browser](docs/EXTERNAL_BROWSER.md)
- [Implementation status](docs/IMPLEMENTATION_STATUS.md)
- [Troubleshooting](TROUBLESHOOTING.md)
- [Security](SECURITY.md)
- [Contributing](CONTRIBUTING.md)
- [Russian README](README_RU.md)
