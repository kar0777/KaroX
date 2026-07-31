# KaroX 5.0 release scope

Status: **frozen preview scope**  
Runtime version: `5.0.0.dev0`

This is the product and release contract for KaroX 5.0. Code is not part of the
stable product promise merely because it exists. It is part of 5.0 only when it
appears in the shipping scope below and passes the required evidence gates.

## Product promise

KaroX is a local control plane for AI coding agents. It lets ChatGPT, Claude,
API models, and compatible MCP clients work in an explicitly selected Git
repository through one repository-scoped runtime. KaroX owns permissions,
sessions, mutation safety, verification, Git evidence, and secret handling
instead of delegating those boundaries to one model provider or website.

KaroX is not a model, an IDE, or an operating-system sandbox. Its job is to make
agent work portable, permission-bound, retry-safe, and verifiable.

## Primary release scenario

A new user must be able to complete this flow without editing configuration
files or receiving maintainer help:

1. Install KaroX on a clean supported system.
2. Run `karox` inside a Git repository.
3. Choose a language and access profile.
4. Connect ChatGPT Web, Claude Web, or a supported API provider.
5. Ask the agent to make a bounded repository change.
6. Run an explicitly approved verification command.
7. Receive check results, Git status, Git diff, and an evidence-backed report.
8. Restart KaroX and resume without replaying a completed mutation.

A candidate that cannot complete this scenario remains a preview.

## Shipping scope

### Core Runtime

Release-critical capabilities:

- repository-root confinement for every local action;
- origin-aware, deny-by-default capability policy;
- durable repository-bound sessions;
- cross-process mutation leases and fencing tokens;
- idempotency for mutating operations;
- atomic, retry-safe file mutation;
- dedicated read-only Git status and diff;
- explicit verification commands and durable evidence;
- secret filtering and credential redaction;
- hard blocks on Git push, package publishing, and destructive actions;
- structured, secret-free handoff and restart recovery;
- actionable diagnostics for transport, tunnel, and authorization failures.

### Native agent

The stable native path includes OpenAI Responses, Anthropic Messages, Gemini,
and generic OpenAI-compatible endpoints. It must use OS-keyring credentials,
explicit model selection, usage/cost accounting where available, budgets,
narrow fallback rules, and mandatory evidence-backed verification.

Deterministic adapter coverage is required. A provider may be called **live
tested** only when a dated conformance record exists in `docs/conformance/`.

### Hosted bridge

Stable hosted surfaces:

- ChatGPT Web remote MCP bridge;
- Claude Web remote MCP bridge;
- generic authenticated Streamable HTTP MCP;
- generic authenticated OpenAPI for explicitly selected Core tools.

Authentication identifies the caller; it does not grant capabilities. Every
call still crosses Core policy, repository binding, session state, leases, and
idempotency.

### User-facing tools

The release-critical tool set is deliberately small:

- list, read, search, write, and patch repository files;
- run explicitly approved checks without an implicit shell;
- read Git status and diff;
- call explicitly selected external MCP tools;
- inspect session, cost, evidence, and permission state;
- create a guarded local commit only under the Advanced/elevated profile.

Git push and package publishing are not granted by any stable access profile.

### Interfaces

The full-screen terminal client, explicit CLI subcommands, and JSON automation
remain first-class. The TUI is a view over shared services and must not contain
a second policy, provider, bridge, or mutation implementation.

## Preview and legacy scope

The following may be present in the artifact but remain visibly Preview,
Experimental, or Legacy until their own evidence gates pass:

- Skills beyond the strict lazy-load and permission contract;
- Pack SDK and Pack lifecycle;
- Pack-declared MCP execution;
- PromptQL product integration;
- Notion legacy gateway integration;
- external agent targets;
- provider presets without a complete official contract;
- advanced routing and ecosystem integrations.

Preview features cannot be required for the primary release scenario.

## Deferred beyond 5.0

Explicitly out of scope:

- Pack or Skill marketplace;
- automatic execution of arbitrary extension code;
- multi-agent orchestration as the default workflow;
- remote runners, teams, organizations, or a hosted KaroX control plane;
- automatic provider selection based on unverified quality claims;
- arbitrary website automation;
- a claim of operating-system sandboxing;
- compatibility claims for every MCP client or compatible vendor.

## Access profiles

The product UI may use friendly labels, but persisted and CLI-facing identifiers
remain stable:

- **Observe** → `read_only`: repository and Git inspection without mutation, plus
  `browser.read` — non-mutating observation of a localhost UI (snapshot, text,
  screenshot, console, failed requests; no navigation, and non-localhost URLs are
  refused by the browser session itself).
- **Build** → `workspace_write`: repository changes, approved process/check
  execution, Git status/diff, selected MCP calls, and `browser.input` — driving
  that local UI (open, click, fill, select, press, close). It does **not** grant
  `git.commit`.
- **Advanced** → `elevated`: explicitly adds guarded local commit plus
  `desktop.input` and `network`, the capabilities that reach outside the
  workspace.

Browser capability was previously described as Advanced-only in this document and
in `SECURITY.md`, while the runtime offered browser tools under Build through the
product's own checkbox and then refused them at the session boundary with exit
code 2. `scripts/check_access_profiles.py` now pins the tiers above in both
directions, so the two cannot drift apart again silently.

Even Advanced does not include Git push, package publishing, or authentication
commands. Those capabilities remain one-shot-explicit and are outside the stable
5.0 product promise.

Resume is an action on an existing session, not a permission profile. Legacy UI
may retain a Resume label during migration.

## Status vocabulary

- **Unit tested** — isolated deterministic tests exist.
- **Contract tested** — a real protocol path has local end-to-end coverage
  against a controlled server.
- **Live tested** — a dated third-party product run exists in
  `docs/conformance/` with versions, platform, limitations, and evidence.
- **Preview** — implemented but outside the stable product promise.
- **Experimental** — incomplete live evidence or an unstable contract.
- **Legacy** — retained while a replacement is validated.

Never shorten **contract tested** to **tested** when it could imply live product
compatibility.

## P0 release blockers

All items below must pass before `5.0.0` is published:

- complete suite, lint, dependency, typing, and coverage gates are green;
- wheel build, install, import, and smoke tests pass on Windows, macOS, Linux;
- clean install, 4.x upgrade, interrupted update, rollback, and uninstall are
  verified;
- ChatGPT Web and Claude Web have passed live conformance records;
- OpenAI Responses, Anthropic Messages, Gemini, and one generic compatible
  endpoint have passed minimal live provider records;
- OAuth redirect/resource binding, PKCE, refresh rotation, and replay revocation
  pass;
- temporary tunnel URL changes produce an actionable diagnostic;
- traversal, symlink/reparse escape, secret reflection, duplicate mutation,
  concurrent mutation, and failed-verification tests pass;
- Git push and package publishing remain blocked in every shipping profile;
- README, Russian README, quick start, migration, security, release notes, and
  troubleshooting describe the same product and version;
- five external testers attempt installation without maintainer assistance;
- three external testers complete the primary scenario;
- two external testers voluntarily use KaroX for a second task;
- no known issue can lose repository data or expose a credential.

Static repository facts are checked by `scripts/check_v5_release.py`. Human and
third-party facts remain explicit conformance and beta records.

## P1 release quality

- actionable first-run errors;
- assisted stable-URL setup;
- session browser and evidence export;
- provider/keyring doctor commands;
- short primary-flow demo;
- tested source-free, credential-free support bundle;
- one week of release-candidate use on a non-KaroX repository.

## P2 after stable release

1. 5.0.x reliability, onboarding, compatibility, and security fixes.
2. Bounded context compaction and long-session ergonomics for 5.1.
3. Permission-bound Pack execution for 5.2.
4. Team and remote-runner features only after repeated user demand.

## Feature freeze rule

Until stable 5.0, accept a change only when it fixes a defect, completes the
primary scenario, improves installation/update/rollback, closes a security
boundary, improves onboarding or diagnostics, removes contradictory claims, or
adds a release gate or conformance record. Everything else is deferred.

Release status changes through evidence, not by editing a marketing table. A
blocker may be removed only with an explicit rationale in this document and in
the same commit; it must not be silently bypassed in CI.
