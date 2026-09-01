# KaroX 5.0 release scope

- Status: **frozen preview scope, amended once — see Scope amendment 1**
- Runtime version: `5.0.0.dev0`

This is the product and release contract for KaroX 5.0. Code is not part of the
stable product promise merely because it exists. It is part of 5.0 only when it
appears in the shipping scope below and passes the required evidence gates.

## Scope amendment 1 (2026-07-31)

The feature freeze rule at the end of this document permits a change to the scope
only with an explicit rationale recorded here, in the same commit. This is that
record.

**What changed.** The Skill marketplace moves out of *Deferred beyond 5.0* into
the shipping scope, and a set of interface capabilities is added to it: an
explicit plan/act split, per-turn checkpoints with undo on the native agent path,
inline diff review before a write reaches disk, file mentions and image
attachments, language-server diagnostics as a gated Core tool, parallel sessions
with a session browser, themes, user-defined commands, budget-bounded subagents,
and a benchmark harness with published methodology.

**Why.** The freeze rule exists to stop scope from growing while the primary
scenario is unfinished. It is not the constraint that was actually binding here.
Two things forced this instead:

1. *The competitive floor moved.* An agent terminal without a plan/act split,
   without undo, without diagnostics from a language server, and without a way to
   review a diff before it lands is now behind what a user already has elsewhere.
   Shipping 5.0 without them means shipping something a user compares
   unfavourably on the first day, and the control-plane guarantees that make
   KaroX different never get evaluated because the surface loses first.
2. *The deferral was partly fictional.* Bounded context compaction was listed
   below as a 5.1 item; it has been implemented, deterministically, in
   `karox.agent` for some time, with a `compacted` event and per-run metadata. A
   contract that defers what the code already does is not a constraint, it is an
   inaccuracy.

**What did not change.** Nothing in this amendment relaxes a boundary. Every
addition lands inside the existing capability policy rather than beside it: plan
mode only *narrows* an access profile to read-only; checkpoints restore paths
KaroX itself recorded as written; language-server diagnostics arrive as a gated
capability; subagents cannot exceed a parent's budget or tool set; marketplace
cards install as data, pinned by commit and verified by content hash, through the
Pack machinery that already requires explicit approval. Git push, package
publishing, arbitrary website automation, automatic execution of extension code,
and multi-agent orchestration as a default all remain deferred, and no P0 blocker
is removed.

**What this costs.** The 5.0 surface is larger, so there is more to keep working.
The mitigation is ordering, not optimism: the UX core (text selection, copy,
decomposition of the terminal client) is done before any of these features is
added, because each new screen built on the current transcript widget would
reproduce the same class of defect.

## Scope amendment 2 (2026-08-23)

**What changed.** KaroX 5 adds an opt-in intelligence-orchestration and economy
surface. It unifies registered API models with explicitly configured
subscription/local/external agent metadata, adds role-based orchestration
recipes, locally verified routing evidence, quota-aware routing, shared
content-addressed context with deltas, cache-friendly stable prompt envelopes,
typed evidence-linked worker handoffs, independent review requirements,
crash-safe orchestration recovery, detached worktree isolation helpers, measured
savings receipts, shadow/replay analysis, and local/mobile Mission Control.

The implementation contract is documented in
`docs/KAROX_5_ORCHESTRATION_ECONOMY.md`.

**Why.** The new layer is not a second policy engine or an attempt to replace the
Core. KaroX already owns providers, model metadata, budgets, context compilation,
usage/cost accounting, risk, workstreams, evidence, checkpoints and durable
recovery. Coordinating those existing services allows KaroX to reduce duplicated
context and marginal model spend without achieving economy by silently
substituting a weaker model or removing verification.

**Routing evidence rule.** Automatic quality selection may use only local KaroX
outcomes that were both accepted and verified for the same task class. Model
names, marketing tiers, community rankings and unversioned benchmark claims do
not become routing evidence. Before enough local evidence exists, explicit user
role assignments and capabilities remain authoritative. High-risk automatic
routes require verified evidence or an explicit route.

**Subscription/external rule.** Registering a subscription or external endpoint
is metadata, not a compatibility claim and not permission to launch arbitrary
software. Execution requires a separately registered guarded adapter. The
orchestration layer accepts no generic shell command, scraped browser cookie or
raw provider credential as an execution fallback. Native API workers continue to
run through `AgentKernel`, Core policy, repository confinement, leases,
idempotency, RiskEngine and the verification allowlist.

**Safety rule.** Multi-agent orchestration remains opt-in, not the default
workflow. Git push, package publishing, authentication commands and automatic
merge remain outside this surface. A mobile `approve_request` is only a request
to the normal Smart Stop flow; it is not a confirmation token. A worker that was
running during a restart becomes `reconcile_required` and is not blindly
replayed. Detached worker worktrees are never auto-merged.

**Economy-claim rule.** KaroX may publish a dollar or percentage savings claim
only when the corresponding measured baseline or reproducible benchmark is
attached. Shadow routing and Replay Lab counterfactuals are labelled projections
and never claim that the alternative worker would have succeeded.

This amendment does not make orchestration a requirement of the primary release
scenario. The original single-agent/hosted-client scenario must remain green on
its own.

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

Release-critical interface behaviour, added by Scope amendment 1:

- character-level text selection and copy in the transcript, working after a
  resize and over a remote terminal;
- an explicit plan/act split, where plan narrows the session to read-only
  regardless of the access profile and act requires confirmation;
- a checkpoint before each mutating series, with undo limited to the paths KaroX
  recorded as written;
- inline diff review with per-hunk accept and reject, before a write reaches
  disk;
- file mentions bounded by the repository root, and image attachments where the
  provider supports them;
- language-server diagnostics as a separately gated Core capability, so the agent
  acts on a compiler's verdict rather than its own guess;
- parallel sessions, a session browser, and evidence export;
- themes including a high-contrast one, and full keyboard-only operation;
- user-defined commands from repository files;
- subagents bounded by an explicit budget, tool set, and step limit.

### Skill marketplace

Moved into scope by Scope amendment 1. A card is a pointer -- repository,
subdirectory, and a ref pinned to a commit -- installed as data through the Pack
machinery, verified against a content hash, and cached locally. Installation
requires explicit approval of the capabilities the skill asks for. Nothing in a
card executes on install.

### Benchmarks

Moved into scope by Scope amendment 1. Runtime latency baselines and
control-plane integrity measurements ship with the release and gate regressions
in CI. Task-success numbers against public suites require a provider key and are
published only with a reproducible run manifest; no figure is published without
one.

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

Explicitly out of scope. The Skill marketplace was removed from this list by
Scope amendment 1; a Pack marketplace stays deferred, because a Pack can declare
executable behaviour and a Skill card cannot.

- Pack marketplace;
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
2. Long-session ergonomics for 5.1. Bounded context compaction itself is **not**
   deferred and never was in practice: `karox.agent` implements it
   deterministically, replacing dropped turns with a summary computed from
   recorded tool results rather than from the model's own account of them, and
   reports each compaction as an event with the token counts either side. Scope
   amendment 1 corrects the claim.
3. Permission-bound Pack execution for 5.2.
4. Team and remote-runner features only after repeated user demand.

## Feature freeze rule

Until stable 5.0, accept a change only when it fixes a defect, completes the
primary scenario, improves installation/update/rollback, closes a security
boundary, improves onboarding or diagnostics, removes contradictory claims, or
adds a release gate or conformance record. Everything else is deferred, unless it
is admitted by a numbered scope amendment at the top of this document.

Release status changes through evidence, not by editing a marketing table. A
blocker may be removed only with an explicit rationale in this document and in
the same commit; it must not be silently bypassed in CI.

An amendment is not a way around this rule. It has to name what changed, why the
rule did not hold, what boundary was *not* relaxed, and what the change costs.
Scope amendment 1 is the worked example; anything less than that is a bypass with
extra steps.
