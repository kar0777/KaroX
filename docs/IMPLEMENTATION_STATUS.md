# KaroX 5 implementation status

Last updated: 2026-07-30  
Branch under review: `feat/karox-v5-competitive-upgrade`  
Runtime version: `5.0.0.dev0`  
Stable shipping line: `4.1.4`

This is the canonical current-status page for KaroX 5. Detailed phase records
under `docs/vNext/` are historical unless the same fact is repeated here.

## Current conclusion

KaroX 5 already has a substantial hybrid local runtime: Core policy and durable
sessions, a bounded native agent, provider adapters, MCP client and proxy,
hosted bridges, remote-MCP OAuth, Skills, Packs, structured handoff, TUI,
migration tooling, and functional benchmark gates.

Stable `5.0.0` is **not ready**. The remaining work is dominated by verification,
installation/update rehearsal, live third-party conformance, onboarding, and
external beta rather than another large architecture phase.

## Implemented release-critical runtime

### Core Runtime

Implemented in source and covered by existing deterministic tests:

- repository-bound origin/capability policy;
- `read_only`, `browser_control`, `workspace_write`, and `elevated` access profiles;
- durable checksum-protected sessions;
- cross-process mutation leases and fencing;
- durable idempotency;
- repository-confined file operations;
- approved shell-free checks;
- dedicated Git status and diff;
- evidence persistence;
- credential/value redaction;
- hard policy boundaries for native and hosted callers.

Policy truth:

- Observe/read_only: repository and Git inspection;
- Browser/browser_control: repository/Git read plus a session-isolated browser
  and guarded network policy; no repository write, process run, or local commit;
- Build/workspace_write: writes, approved checks/processes, Git status/diff, and
  selected MCP calls; no `git.commit`;
- Advanced/elevated: adds guarded local commit and elevated browser,
  desktop-input, and network capabilities;
- Git push, package publishing, and authentication commands are not in any
  stable profile.

Known boundary: approved process execution is not an operating-system sandbox.

### Native agent and providers

Implemented:

- bounded provider/tool/result loop;
- streaming transport normalization;
- malformed and repeated-action protection;
- step, action, and wall-time limits;
- recovery of pending mutation state;
- mandatory write → successful check → Git status → Git diff verification;
- cross-provider model switch on one durable session;
- usage, cost, budget, and route-attempt records;
- OpenAI Responses, Anthropic Messages, Gemini GenerateContent, and generic
  OpenAI-compatible adapters;
- OS-keyring credential references.

Deterministic contract coverage exists. Required live provider records remain
pending.

### MCP and hosted bridges

Implemented:

- stdio and Streamable HTTP MCP client;
- server and credential registry;
- selected-tool Core integration;
- authenticated Streamable HTTP and OpenAPI bridges;
- built-in Core and external MCP allowlists;
- separate bridge credential namespace;
- credential rotation and revocation;
- OAuth discovery, DCR, PKCE, exact redirect/resource binding, access/refresh
  token lifecycle, and refresh-replay revocation;
- ChatGPT Web and Claude Web profiles;
- managed Cloudflare Quick Tunnel lifecycle;
- custom stable public URL support.

The local protocol path is contract tested. Real ChatGPT and Claude account runs
remain pending, so both named product profiles remain Experimental.

### External HTTPS browser for hosted clients

Implemented as an explicit addition to the original localhost browser mode:

- `browser_control` grants browser read/input and guarded network access without
  repository write, process execution, or local commit;
- public HTTPS is opt-in per session, with domain allow/deny lists, private-IP and
  metadata blocking, unsafe-scheme blocking, redirect revalidation, and a block
  on external pages reaching localhost;
- headed user takeover uses installed Google Chrome with a dedicated persistent
  KaroX profile and local Manifest V3 extension; every ordinary tab in that
  profile is agent-controllable while the user's normal Chrome profile is absent;
- headless verification retains the separate Playwright context and authenticated
  random-port pinned-IP proxy, with service workers/downloads disabled;
- extension commands use an authenticated loopback WebSocket that is never
  exposed through the public MCP tunnel;
- takeover pauses agent input for login, CAPTCHA, passwords, 2FA, consent, or
  ambiguous payment review, then resumes the same tab/profile;
- network inspection exposes bounded redacted metadata and selected model, usage,
  credits, plan, trial, and subscription values without headers, cookies, tokens,
  session identifiers, card data, or arbitrary message/file bodies;
- payment and subscription confirmation remain a separate capability that is
  absent by default.

Deterministic URL, proxy, isolation, takeover, payment, network-redaction, CLI,
profile, and compatibility tests are present. A Windows smoke opened
`https://example.com/`, created a snapshot and PNG, exercised tab lifecycle, and
inspected the GitLab signup page only to its first form without entering or
submitting data. This is local contract/smoke evidence, not a completed live
ChatGPT account conformance run. Full details are in
[`docs/EXTERNAL_BROWSER.md`](EXTERNAL_BROWSER.md).

## Ellipsis Opus 5 local-workspace integration

Implemented in this working tree as an Experimental integration:

- `karox agent` starts an Ellipsis interactive session through a configurable,
  versioned REST contract without sending repository metadata;
- the selected local Git root remains the only working tree;
- `sandbox.repositories` is empty or omitted, never populated from Git origin;
- Ellipsis receives only the strict local-workspace instruction, a thin
  dependency-free `karox-remote` adapter, and three write-only connection
  variables;
- short-lived credentials are bound to the local Karo session, repository
  digest, access profile, and Ellipsis session ID and are automatically revoked;
- repository reads/writes, approved commands and checks, managed processes,
  localhost browser verification, screenshots, Git evidence, checkpoints and
  rollback remain local KaroX operations;
- shell, remote Git, push, publish, deploy, release, and authentication commands
  remain blocked;
- detach/attach, follow-up messages, event cursor recovery, status, cost, diff,
  tests, report, stop, and explicit rollback are represented in the Karo CLI;
- a no-secret watchdog closes local authority on bridge death or TTL expiry;
- Claude Code/OpenCode can manage the remote session through five local MCP
  control tools rather than a fake Anthropic/OpenAI endpoint;
- deterministic contract, bridge E2E, credential, security, CLI, checkpoint,
  and ordering tests are present;
- a paid live E2E exists but is gated by `ELLIPSIS_API_TOKEN`,
  `--allow-live-ellipsis`, exact confirmation, and a budget at or below
  `0.10 USD`.

Remaining evidence:

- account-specific Ellipsis interactive REST paths/capabilities must be supplied
  by an organization with API access;
- the separate `karox-remote` artifact must be version-pinned and made available
  to the Ellipsis image or setup path;
- the paid live acceptance test has not been run in this session;
- browser automation requires the optional Playwright dependency and a local
  Chromium installation.

## Current branch hardening

The pre-existing working diff on this branch adds:

- Russian and English OAuth approval pages selected from `ui_locales`;
- explicit approval-password destination guidance;
- `same-origin` Referrer Policy for the local approval form to avoid Chromium
  `Origin: null` rejection while preserving cross-origin privacy;
- shared `cloudflared` discovery between TUI and CLI;
- Windows WinGet and standard-install discovery fallbacks;
- localized temporary-URL warning;
- clearer instruction to keep KaroX open;
- focused regression tests.

A documentation review on 2026-07-29 found one remaining P0 copy defect in the
runtime source: ChatGPT instructions said `Settings → Plugins`, while the current
official product uses Apps with developer mode. **That defect is fixed.**
`src/karox/web_bridge_launcher.py` now names `Settings → Apps` and
`Apps → Create` in English and `Настройки → Приложения` and
`Приложения → Создать` in Russian; `scripts/check_v5_release.py` fails the
release if either stale path reappears, and
`tests/test_web_bridge_launcher.py::WebBridgeInstructionTests` asserts the
current wording in both languages. The one-time helper that carried the change
has been applied and deleted, as the release-hygiene gate requires.

This paragraph claimed the opposite for several commits after the fix had landed,
which is the failure mode these documents exist to prevent. It is corrected here
rather than in a later pass.

## Release infrastructure implemented in this update

Added or rebuilt:

- frozen `docs/V5_RELEASE_SCOPE.md`;
- canonical English/Russian README and bilingual quick start;
- current security, troubleshooting, connectivity, implementation, migration,
  beta, live-test, and release-checklist documents;
- required live conformance record templates;
- structured external-beta issue template and PR template;
- `scripts/check_v5_release.py` for final product/evidence gates;
- `scripts/check_access_profiles.py` backed by the actual policy table;
- `scripts/check_release_workflow.py` for publication-order invariants;
- central `scripts/check_versions.py` integration of all repository contracts;
- release workflow ordering that builds and smoke-tests artifacts before tag
  creation and requires strict KaroX 5 evidence gates;
- explicit historical status for `docs/vNext/README.md`.

These files exist in the working tree. They have not yet been executed by local
Python, Ruff, Mypy, coverage, or GitHub Actions in this session.

## Preview features

Implemented but outside the stable primary-flow promise:

- strict lazy Skills;
- Pack manifest/generator/lifecycle/integrity checks;
- PromptQL target contract;
- legacy Notion gateway;
- advanced routing and ecosystem integrations;
- Ellipsis Opus 5 as a repository-free remote reasoning backend.

Pack-declared runtime tool execution remains outside stable 5.0 until its full
registration and security path has complete evidence.

## Migration and update foundations

Implemented or retained:

- legacy discovery and dry-run-by-default migration reporting;
- `karox migrate --json` for dry-run and `--apply --json` for mutation;
- secret-safe metadata migration;
- staged/atomic update and rollback foundations;
- installer and launcher compatibility paths;
- legacy server and Notion gateway during validation.

Clean-machine 4.x → 5.0 migration, interrupted update, rollback, and uninstall
still require recorded runs on Windows, macOS, and Linux.

## Evidence status

The canonical documented suite is now 1117 tests under:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

The ten KB-HYBRID gates are implemented. CI declares dependency, version,
product-contract, lint, typing, coverage, wheel-install, legacy regression,
PowerShell, POSIX, and multi-OS checks.

This page does not itself claim that a working tree passes them; a claim like that
belongs to a dated record of an actual run. Those live in
[`docs/evidence/`](evidence/README.md), each naming its commit, its platform, and
the command behind every number.

The starting point for the 5.0 work is
[`docs/evidence/baseline-2026-07-31.md`](evidence/baseline-2026-07-31.md), taken
on commit `dee041c`: 796 tests pass with 7 Playwright skips, all 12 static gates
pass, Ruff and Mypy are clean, coverage is 71.26% against a gate of 70, and
`check_v5_release.py --strict` fails on nothing except the live evidence listed
below. Windows only — the macOS and Linux matrix has no record yet.

## Required external evidence

| Integration | Record | Status |
| --- | --- | --- |
| ChatGPT Web | `docs/conformance/chatgpt-web.md` | Pending |
| Claude Web | `docs/conformance/claude-web.md` | Pending |
| OpenAI Responses | `docs/conformance/openai-responses.md` | Pending |
| Anthropic Messages | `docs/conformance/anthropic-messages.md` | Pending |
| Gemini | `docs/conformance/gemini.md` | Pending |
| Generic OpenAI-compatible | `docs/conformance/openai-compatible.md` | Pending |
| Ellipsis Opus 5 local workspace | `docs/ELLIPSIS_LOCAL_AGENT.md` | Contract tests added; live pending |
| External beta | `docs/conformance/beta-summary.md` | Not started |

A record becomes Passed only after a dated sanitized real run. Local controlled
servers cannot substitute for named-product live evidence.

## Current P0 blockers

### Terminal-client defects

Fifteen terminal-client defects are recorded in
[`docs/UX_BUG_INVENTORY.md`](UX_BUG_INVENTORY.md), each with executable evidence.
Thirteen are closed. The old RichLog selection design and all four former P0
defects have been removed; the two remaining open items are P1: handing mouse
selection back to the terminal (UX-006) and preserving a usable conversation area
in a 14-row window (UX-010).

These remain release-relevant usability work, but this page must not describe
closed P0 defects as current blockers.

### Source and automated verification

- run focused OAuth/web-bridge and Ellipsis local-agent tests;
- run the complete 1117-test suite;
- run dependency, version, product, profile, workflow, Ruff, Mypy, and coverage
  gates;
- validate the edited release workflow in GitHub Actions;
- record a green Windows/macOS/Linux matrix on the exact release candidate;
- build/install/smoke-test both the KaroX wheel and the separate remote client
  artifact outside the source tree;
- verify every public command against the installed `--help` output;
- remove remaining stale user-facing `vNext` wording while preserving storage
  and compatibility paths that intentionally retain the name.

### Installation and lifecycle

- clean install on Windows, macOS, and Linux;
- migration from latest stable 4.x;
- interrupted update and rollback;
- uninstall without repository deletion;
- separate session/credential cleanup behavior.

### Live and human evidence

- ChatGPT Web live conformance;
- Claude Web live conformance;
- four required provider live records;
- Ellipsis repository-free live acceptance run with the strict `0.10 USD` gate;
- five unaided beta attempts;
- three completed primary scenarios;
- two unaided completions and two voluntary second uses;
- final security and release review.

## Next execution order

1. Apply the reviewed ChatGPT Apps copy fix and inspect its diff.
2. Run focused bridge and Ellipsis tests and all static repository contracts.
3. Run the complete suite, lint, typing, coverage, and wheel smoke tests.
4. Push the branch only after local evidence is clean, then obtain the remote
   matrix result.
5. Complete ChatGPT and Claude live runbooks.
6. Complete minimal provider and Ellipsis live runs under explicit budgets.
7. Rehearse install, migration, rollback, and uninstall on all three OS families.
8. Run external beta and fix only P0/P1 blockers.
9. Prepare a release candidate and require `check_v5_release.py --strict` before
   the final version transition.

## Canonical documents

- `docs/V5_RELEASE_SCOPE.md`
- `docs/RELEASE_CHECKLIST.md`
- `docs/BETA_TEST_PLAN.md`
- `docs/LIVE_TEST_RUNBOOK.md`
- `docs/MIGRATION_V4_TO_V5.md`
- `docs/CONNECTIVITY.md`
- `docs/ELLIPSIS_LOCAL_AGENT.md`
- `docs/conformance/README.md`
- `SECURITY.md`
- `TROUBLESHOOTING.md`
