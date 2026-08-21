# KaroX 5 Preview

**Run AI coding agents on local Git repositories without giving one provider
control of your permissions, sessions, or evidence.**

KaroX is a local control plane for ChatGPT, Claude, API models, and compatible
MCP clients. Every local action goes through one repository-scoped runtime with
explicit permissions, retry-safe mutations, durable sessions, approved checks,
Git evidence, and secret filtering.

KaroX is not a model, an IDE, or an operating-system sandbox. An explicitly
approved process still runs with the operating-system rights of the KaroX user.
The runtime reduces authority through repository confinement, capabilities,
allowlists, leases, idempotency, and hard blocks; it does not virtualize the
machine.

> **Release status:** the packaged runtime is `5.0.0.dev0`. Deterministic local
> contracts are extensively tested, but live ChatGPT Web, Claude Web, and paid
> provider conformance records are still pending. See the
> [5.0 release scope](docs/V5_RELEASE_SCOPE.md),
> [4.x migration contract](docs/MIGRATION_V4_TO_V5.md), and
> [live conformance records](docs/conformance/README.md).

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

## Install the preview from a checkout

The public bootstrap commands on `main` install the latest stable 4.x release.
To test the KaroX 5 preview, clone or check out the preview branch and run its
local installer.

Windows:

```powershell
.\install.karox.ps1
```

macOS or Linux:

```bash
./install.karox.sh
```

Open a new terminal inside the repository you want to work with and run:

```bash
karox
```

On Windows, a terminal opened before installation keeps its old `PATH`. Open a
new terminal, use the Desktop shortcut, or refresh the environment before
reporting that the launcher is missing.

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

Omit `--write` for the read-only tool set. KaroX prints the MCP URL, approval
password, and connection instructions. Keep the process open while using the
connector. For HyperAgent, leave "Bring my own OAuth app" off — KaroX advertises
OAuth discovery and Dynamic Client Registration, so no Client ID or Client Secret
is entered by hand. See `docs/CONNECTIVITY.md` for the full HyperAgent steps.

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

No stable profile grants Git push, package publishing, or authentication
commands. Resuming work is an action on an existing durable session, not a new
permission level. Some legacy UI may still display a Resume profile during
migration.

## Automation CLI

Scripts and CI use explicit subcommands:

```text
karox paths | session | credential | provider | model | skill | mcp | bridge
      | pack | target | tool | integration | agent | migrate | doctor
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
allowlisted hosted tools, dedicated credential namespaces, and hard blocks on
Git push and package publishing. A failed check cannot be converted into a
verified success by model text.

Read [SECURITY.md](SECURITY.md). Security reports should use the private process
described there, not a public issue containing a credential or private
repository data.

## Migration

KaroX 5 migration is incremental and reversible. The preview discovers legacy
state in dry-run mode by default, preserves the old installation, imports only
validated non-secret metadata, places credentials in the OS keyring, and reports
unsupported data instead of dropping it.

See [Migrating from KaroX 4.x to KaroX 5](docs/MIGRATION_V4_TO_V5.md).

## Verification

The suite is 2782 tests. CI runs:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

A clean run reports `Ran 2782 tests`.

2420 is the number collected under `tests` by the documented runner. The
repository root collects 2787 because a bare pytest collection also finds five
legacy KaroX 4 checks under `scripts/`.

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
