# KaroX 5 implementation status

Last updated: 2026-09-16
Branch under review: `feat/karox-v5-competitive-upgrade`
Runtime version: `5.0.0rc2` (public beta)
Stable shipping line: `4.1.4`

This is the canonical current-status page for KaroX 5. Detailed phase records
under `docs/vNext/` and the working plans under `docs/archive/` are historical
unless the same fact is repeated here.

## Current conclusion

KaroX 5 is a complete hybrid local runtime: Core policy and durable sessions, a
bounded native agent for four provider families, MCP client and proxy, hosted
bridges for ChatGPT Web, Claude Web, HyperAgent and Adapt, remote-MCP OAuth,
universal cross-client memory, a deterministic project fact map, an opt-in
intelligence-orchestration and economy layer, localhost and external-HTTPS
browser automation, a 35-screen bilingual terminal client, Skills, Packs,
structured handoff, migration tooling, and release gates.

`5.0.0rc2` is the **public beta / release candidate**. Stable `5.0.0` is
**not ready**: the remaining work is live third-party conformance, external-beta
feedback, and platform install/upgrade rehearsals — evidence, not another
architecture phase. See `docs/RELEASE_CHECKLIST.md` for the exact open items.

## Implemented release-critical runtime

### Core Runtime

Implemented in source and covered by deterministic tests:

- repository-bound origin/capability policy;
- `read_only`, `browser_control`, `workspace_write`, and `elevated` access
  profiles;
- durable checksum-protected sessions;
- cross-process mutation leases with heartbeat and fencing;
- durable idempotency for every mutating operation;
- repository-confined atomic file operations, unified patches, and transactional
  batches with checkpoints and undo;
- approved shell-free checks and durable long-running check jobs;
- dedicated read-only Git status, diff, and log;
- evidence persistence and typed Evidence Packets with lossless elision;
- credential/value redaction and secret-like content refusal;
- universal RiskEngine (Smart Stop) with four risk levels, single-use
  confirmation tokens, and no configurable auto-approval above medium;
- hard policy boundaries for native and hosted callers: no standing Git-push,
  package-publish, or authentication authority in any shipping profile. An
  elevated saved hosted bridge can expose only the dedicated `karox.git.push`
  action, and every call requires a fresh exact-action MCP user approval.

Policy truth:

- Observe/`read_only`: repository and Git inspection plus non-mutating
  localhost browser observation;
- Browser/`browser_control`: repository/Git read plus a session-isolated browser
  and guarded network policy; no repository write, process run, or local commit;
- Build/`workspace_write`: writes, approved checks/processes, Git status/diff,
  selected MCP calls, localhost browser input; no `git.commit`;
- Advanced/`elevated`: adds guarded local commit and elevated browser,
  desktop-input, and network capabilities.

`scripts/check_access_profiles.py` pins these tiers against the real policy
table. Known boundary: approved process execution is not an operating-system
sandbox.

### Native agent and providers

Implemented:

- bounded provider/tool/result loop with step, action, and wall-time limits;
- streaming transport normalization and malformed/repeated-action protection;
- mandatory write -> successful check -> Git status -> Git diff verification;
- deterministic bounded context compaction with a `compacted` event;
- Context Compiler IR that decides every request item, reasoning-continuity
  ledger across compaction, cache-aware scheduler, stable prompt prefixes,
  tool-schema deduplication, read cache, cost ledger and governor;
- first-class Effort levels (`low` .. `ultra`) mapped to provider reasoning
  effort and agent limits;
- adaptive/concise/standard/detailed output policy at the production call-site;
  failures, warnings and compaction notices survive every mode;
- cross-provider model switch on one durable session;
- usage, cost, budget, pricing-with-provenance, and route-attempt records;
- OpenAI Responses, Anthropic Messages, Gemini GenerateContent, and generic
  OpenAI-compatible adapters; OpenRouter delegation; Puter and Tinfoil presets;
- provider controller with transactional setup, keyring-only credentials, and
  live probe before activation;
- `/model auto` recommendation by explicit capability with an explanation.

Deterministic contract coverage exists. Required live provider records remain
pending (`docs/conformance/`).

### Universal memory and project intelligence

Implemented (2026-08-20 onwards):

- `karox.memory`: USER/PROJECT/WORKSTREAM/SESSION scopes, structured entries
  with provenance, confidence, sensitivity and TTL; credential-shaped content
  refused; keyed upserts; budgeted deterministic recall; source-hash
  revalidation for project facts;
- multilingual retrieval (NFKC + casefold, ё->е, snake_case tokens, RU/EN alias
  map) so Russian, English and mixed queries reach the same memory;
- memory protocol tools `karox.memory.remember/recall/context/list/forget` for
  every client: native agent, ChatGPT Web, Claude Web, Adapt, CLI, TUI;
- cross-client recall proven live: a fact stored through ChatGPT Web was
  recalled through Adapt and through a native provider without shared
  transcripts;
- deterministic project fact map (`project_map.py`, `repo_context.py`):
  languages, entrypoints, build/test commands, package manager, key
  directories; ingests AGENTS.md / CLAUDE.md / KAROX.md / .cursorrules;
  incremental rescan with stale-fact invalidation; `karox.task.bootstrap`
  returns the compact digest;
- provenance-aware cross-chat task state and durable structured handoff.

### MCP and hosted bridges

Implemented:

- stdio and Streamable HTTP MCP client; server and credential registry;
- selected-tool Core integration with schema/identity drift detection;
- authenticated Streamable HTTP and OpenAPI bridges with allowlists;
- OAuth discovery, DCR, PKCE, exact redirect/resource binding, access/refresh
  token lifecycle, refresh-replay revocation; RU/EN approval pages;
- ChatGPT Web, Claude Web, HyperAgent, and Adapt profiles; Adapt shares a
  compatible running ChatGPT bridge instead of launching a second process;
- durable saved bridge identity: deterministic session ID, keyring secret that
  survives ordinary restarts, orphan reaping that preserves auth state;
- managed Cloudflare Quick Tunnel and Tailscale Funnel lifecycle with stable
  `.ts.net` hostnames; custom MCP clients launched as managed bridges sharing
  the Tailscale listener;
- multi-project registry: one bridge serves several approved repositories with
  per-project leases, workstreams, and no global lock;
- deterministic tool-catalog groups (core/task/memory/browser/devserver/admin)
  and honest stale-catalog handling on the stateless wire;
- managed dev servers per approved project with process identity
  re-verification before every stop;
- strong process identity (creation-time evidence, executable/argv/owner
  digests) so PID reuse can never be signalled;
- Dev/Dogfood/Stable channel state isolation.

The local protocol path is contract tested and the shared `chatgpt-dev` bridge
has served three real clients during development. Dated live conformance
records for the named products remain pending, so the product profiles stay
Experimental in the release contract.

### Intelligence orchestration and economy (opt-in)

Implemented, documented in `docs/KAROX_5_ORCHESTRATION_ECONOMY.md`:

- Intelligence Pool over API models, subscription agents, local models, and
  explicitly attached external agents; metadata only, no credentials;
- `VerifiedSmartRouter`: automatic quality routing only from local outcomes that
  were both accepted and verified; explicit role assignments remain
  authoritative before enough evidence exists;
- quota brain, content-addressed shared context bus with role projections and
  deltas, cache-friendly stable prompt envelopes, typed evidence-linked
  handoffs, independent review requirements;
- built-in recipes (`feature`, `bug-fix`, `security`, `large-refactor`), presets
  (`maximum_quality`, `balanced`, `maximum_economy`), per-worker Effort;
- bounded DAG scheduler with parallel read/review waves and serialized or
  worktree-isolated implementers; detached worktrees are never auto-merged;
- guarded built-in adapters for already-paid Codex (worktree-confined
  implementation) and Claude Code (read/review only); Gemini CLI and OpenCode
  discovered but not auto-executed;
- crash-safe orchestration journal with `reconcile_required` for interrupted
  workers;
- `SavingsReceipt`, controlled OFF-vs-ON evaluation with a parity gate, shadow
  routing and Replay Lab labelled as projections;
- Mission Control (CLI, TUI, loopback/Tailscale mobile server with short-lived
  pairing; `approve_request` never mints a confirmation token).

### Browser and desktop

Implemented:

- localhost browser observation and input through Playwright with a lazily
  provisioned Chromium;
- external HTTPS browser for hosted clients under `browser_control`: domain
  allow/deny lists, private-IP and metadata blocking, redirect revalidation,
  headed takeover through a dedicated Chrome profile plus the local Manifest V3
  extension, headless verification through an isolated pinned-proxy context;
- locator grammar, navigation, screenshots, network inspection with bounded
  redaction; deterministic local fixture suite;
- browser credential store with injection that never exposes the secret to the
  model;
- Windows desktop application capture without global input primitives, with
  blank-frame rejection.

### Terminal client and CLI

Implemented:

- full-screen Textual client: 35 screens inventoried in
  `docs/V5_TOTAL_UI_ACCEPTANCE.md`, all exercised from 40x12 to 160x45 in RU and
  EN by `tests/test_tui_total_size_acceptance.py`;
- native character-level selection and copy, Ctrl+C copies while Esc stops;
  13 of 15 recorded terminal defects closed (`docs/UX_BUG_INVENTORY.md`);
- typed session views over the event bus, session browser and detail, grouped
  activity chronology;
- Connections hub, provider wizard, model picker, workspace manager (Ctrl+W),
  usage/cost, saved web profile settings, browser credential manager;
- compact eight-command slash menu; advanced commands stay routable when typed;
- explicit CLI groups: `paths session credential provider model intelligence
  skill mcp connections bridge connect orchestrate mission-control economy pack
  target tool integration agent migrate doctor`, every one with `--json`;
- human-facing argument normalization (`models`, `agents`, task-first
  `orchestrate` verbs) in front of the canonical argparse tree.

## Ellipsis Opus 5 local-workspace integration

Experimental. `karox agent` starts an Ellipsis interactive session through a
versioned REST contract without sending repository metadata; the local Git root
stays the only working tree; short-lived credentials are bound to session,
repository digest, access profile and Ellipsis session ID and revoked on stop.
Deterministic contract, bridge E2E, credential, security, CLI and checkpoint
tests exist. The paid live acceptance is gated by `ELLIPSIS_API_TOKEN`,
`--allow-live-ellipsis`, exact confirmation, and a budget at or below
`0.10 USD`, and has not been run.

## Preview features

Implemented but outside the stable primary-flow promise:

- strict lazy Skills;
- Pack manifest/generator/lifecycle/integrity checks;
- PromptQL target contract;
- legacy Notion gateway;
- advanced routing and ecosystem integrations.

Pack-declared runtime tool execution remains outside stable 5.0.

## Migration and update foundations

- legacy discovery and dry-run-by-default migration (`karox migrate --json`;
  only `--apply` writes);
- secret-safe metadata migration into the OS keyring;
- staged/atomic update and rollback foundations;
- installer and launcher compatibility paths.

Clean-machine 4.x -> 5.0 migration, interrupted update, rollback and uninstall
still require recorded runs on Windows, macOS and Linux.

## Evidence status

The canonical documented suite is now 3384 tests under:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

Repository contracts run in CI and locally:

```bash
python scripts/check_dependencies.py
python scripts/check_versions.py
python scripts/check_test_count.py
python scripts/check_v5_release.py --json
python -m ruff check src tests scripts
python -m mypy src/karox
```

Dated records live in [`docs/evidence/`](evidence/README.md). The most recent
complete local acceptance is
[`docs/evidence/local-autonomy-acceptance-2026-08-07.md`](evidence/local-autonomy-acceptance-2026-08-07.md)
(Windows, Python 3.13: full suite green, coverage 72.4% against a gate of 70,
wheel built and smoke-tested). Later durable full-suite runs through the hosted
bridge on 2026-08-20/21 were green as well (2826 passed, 5 skipped); they are
recorded in `docs/archive/V5_MASTER_EXECUTION_STATE.md`.

## Required external evidence

| Integration | Record | Status |
| --- | --- | --- |
| ChatGPT Web | `docs/conformance/chatgpt-web.md` | Pending |
| Claude Web | `docs/conformance/claude-web.md` | Pending |
| OpenAI Responses | `docs/conformance/openai-responses.md` | Pending |
| Anthropic Messages | `docs/conformance/anthropic-messages.md` | Pending |
| Gemini | `docs/conformance/gemini.md` | Pending |
| Generic OpenAI-compatible | `docs/conformance/openai-compatible.md` | Pending |
| Ellipsis Opus 5 local workspace | `docs/ELLIPSIS_LOCAL_AGENT.md` | Contract tests; live pending |
| External beta | `docs/conformance/beta-summary.md` | Not started |

A record becomes Passed only after a dated sanitized real run.

## Current P0 blockers

### Terminal client

One open P1 item remains in `docs/UX_BUG_INVENTORY.md`: UX-006, handing the mouse
back to the terminal. It is not a P0.

### Source and automated verification

- run the complete 3384-test suite, lint, typing, coverage and wheel smoke on
  the exact release-candidate commit and record it under `docs/evidence/`;
- validate the release workflow in GitHub Actions and record a green
  Windows/macOS/Linux matrix;
- build/install/smoke-test the wheel and the separate remote client outside the
  source tree;
- verify every public command against the installed `--help` output.

### Installation and lifecycle

- clean install on Windows, macOS and Linux;
- migration from latest stable 4.x;
- interrupted update and rollback;
- uninstall without repository deletion.

### Live and human evidence

- six live conformance records;
- Ellipsis live acceptance under the strict budget gate;
- five unaided beta installations, three completed primary scenarios, two
  unaided completions, two voluntary second uses;
- final security and release review.

## Next execution order

1. Publish `5.0.0rc2` as a pre-release wheel so the beta can install without a
   source checkout.
2. Record the four provider live runs (OpenAI Responses, Anthropic Messages,
   Gemini, one generic compatible endpoint).
3. Record ChatGPT Web and Claude Web live runs through a saved bridge profile.
4. Run the install/migrate/uninstall rehearsal on all three OS families.
5. Run the external beta and fix only P0/P1 blockers.
6. Require `python scripts/check_v5_release.py --strict` to pass before the
   final version transition to `5.0.0`.

## Canonical documents

- `docs/V5_RELEASE_SCOPE.md`
- `docs/RELEASE_CHECKLIST.md`
- `docs/BETA_TEST_PLAN.md`
- `docs/LIVE_TEST_RUNBOOK.md`
- `docs/MIGRATION_V4_TO_V5.md`
- `docs/CONNECTIVITY.md`
- `docs/KAROX_5_ORCHESTRATION_ECONOMY.md`
- `docs/EXTERNAL_BROWSER.md`
- `docs/ELLIPSIS_LOCAL_AGENT.md`
- `docs/conformance/README.md`
- `SECURITY.md`
- `TROUBLESHOOTING.md`
