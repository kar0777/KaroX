# KaroX 5 — Intelligence Orchestration and Economy

Status: implementation contract for the opt-in KaroX 5 orchestration surface.

This document describes code that lives inside the existing KaroX local-first
control plane. It does not change the repository, permission, secret, Git, or
verification boundaries defined by `V5_RELEASE_SCOPE.md`.

## Product goal

KaroX should spend expensive model intelligence only where the task and measured
quality require it. A user may bring API providers, already-paid subscription
agents, local models, or explicitly attached external agents. KaroX presents
those sources as one **Intelligence Pool**, shares repository context between
workers, routes roles using local verified evidence, requires independent review
when policy asks for it, and produces measured economy evidence instead of an
unverifiable savings claim.

The orchestration layer is opt-in. It is not the default workflow and it cannot
bypass Core.

## Implemented modules

### `intelligence_pool.py`

Provider-neutral inventory for:

- API models derived live from `ProviderRegistry`;
- subscription agents;
- local models;
- explicitly attached external agents.

The custom pool stores metadata only. Credentials remain in the existing KaroX
credential/provider stores. It accepts no shell command, cookie, API token, or
raw authentication material.

Each endpoint may declare capabilities and user-approved roles. Subscription and
local endpoints may be marked `already_paid`, which means zero *marginal* cost
for routing; KaroX does not claim the subscription itself was free.

### `quota_brain.py`

Provider-neutral, time-stamped quota observations. Quota is runtime telemetry,
not provider configuration. Unknown is different from zero. The router can
reserve scarce subscription capacity instead of spending it on low-value work.

### `orchestration_routing.py`

`VerifiedSmartRouter` scores only eligible endpoints. Automatic quality is based
only on local KaroX observations where a result was both accepted and verified.
Model names, marketing tiers, community rankings, and unversioned benchmark
claims do not become routing evidence.

Before enough verified observations exist, explicit user role assignments and
capabilities are the quality authority. High-risk automatic routing requires
verified evidence or an explicit endpoint/role assignment.

Routing may then prefer:

1. endpoints that meet the capability and risk floor;
2. locally verified success for the same task class;
3. already-paid or local capacity;
4. available quota above the configured reserve;
5. deterministic known marginal price;
6. measured latency.

Cost can never rescue an endpoint that failed an eligibility or high-risk quality
floor.

### `context_bus.py`

Local, redacted, content-addressed shared context. It provides deterministic
role-specific projections and hashes every item. A worker that already knows an
item hash receives only the unchanged ID; changed items are sent as a delta.

Reviewers see diffs/evidence/tests first, implementers see files/symbols/
diagnostics first, planners see architecture/project map first, and so on. This
is a local projection policy, not a model intelligence substitution.

Assignment-style secrets such as `api_key=...` receive an additional
conservative redaction pass before shared storage.

### `prompt_cache_plan.py`

Stable context is rendered in deterministic order before volatile task material.
The stable prefix hash can be used to keep provider prompts cache-friendly.
Actual cache hits and savings still come only from provider-reported usage and
KaroX usage analytics; the layout itself is not reported as a cache hit.

### `agent_protocol.py`

Typed, bounded, evidence-linked worker handoffs:

- request;
- result;
- review pass;
- review fail;
- escalation.

A review can carry file/line/severity findings and opaque evidence references
without replaying the implementer's whole transcript. Independent review
prefers a different provider/source when one is available and otherwise at least
a different endpoint.

### `orchestration_recipes.py`

Built-in recipes:

- `feature`;
- `bug-fix`;
- `security`;
- `large-refactor`.

Recipes are DAGs with roles, capability requirements, dependencies and explicit
independence requirements. The UI verification step requires both vision and a
real browser capability; a vision-only API cannot silently stand in for browser
verification.

### `orchestrator.py`

Role-based plan and execution runtime with presets:

- `maximum_quality`;
- `balanced`;
- `maximum_economy`;
- `custom`.

Every planned worker also carries the existing first-class KaroX **Effort** level
(`low`, `medium`, `high`, `extra-high`, or `ultra`). Presets allocate less effort
to rediscovery/summarization and more to orchestration, implementation, security,
and independent review. `--worker-effort ROLE_OR_STEP=LEVEL` overrides that
policy explicitly. This is the same Effort system used by the single-agent
runtime, not a second prompt-only knob.

The orchestrator chooses endpoints through `VerifiedSmartRouter`; it does not
construct arbitrary commands. Execution is possible only through a registered
`WorkerExecutor` adapter. The selected orchestrator is a real participant, not a
label: planner work defaults to that endpoint, and a final `orchestrator-judge`
step receives the independent review/test evidence before the run can finish.

High-risk feature/bug recipes are automatically augmented with an independent
security review when the recipe does not already contain one.

Every worker receives a bounded context delta and a durable idempotency key.
Mission Control state, routing evidence, handoffs, budget state and recovery
state are updated by the runtime.

Execution is a real bounded DAG scheduler, not a sequential multi-agent facade.
Dependency-ready read/review workers run in parallel waves up to
`max_parallel_workers`. Repository writers are serialized unless implementer
worktree isolation is enabled; isolated implementers may then run concurrently
because each write path is bound to a separate detached KaroX worktree. Durable
journal/context/handoff/telemetry state is still applied by the coordinator in
plan order, so completion order cannot make persisted orchestration state
nondeterministic.

### `worker_adapters.py` and `orchestration_native.py`

`NativeAgentExecutor` runs API workers through the existing `AgentKernel`,
`ExtendedCoreRuntime`, `CapabilityPolicy`, `RiskEngine`, `SessionStore`, provider
factory and verification allowlist. Per-worker Effort sets the worker's real
`AgentLimits` and provider `reasoning_effort`, bounded by the orchestration hard
caps. External guarded adapters receive the same Effort as orchestration metadata.

Role grants narrow the normal workspace-write profile:

- planner/scout/summarizer: read and Git read only;
- reviewer/security/tester: read plus approved checks, no repository write;
- implementer: repository write plus approved checks and MCP;
- UI: no pretend browser capability in the native Core path.

`GuardedPromptExecutor` supports an application-registered subscription/MCP/
desktop adapter, but accepts no argv, shell command, browser cookie or
credential. External/subscription automation is therefore explicit rather than
a generic arbitrary-process escape hatch.

### `subscription_cli.py`

KaroX can discover installed subscription CLIs without reading their credentials.
The built-in execution contract is deliberately narrower than discovery:

- **Codex Exec** is available for read/review roles with a read-only sandbox and
  for implementation only inside a detached KaroX worktree with the
  workspace-write sandbox. KaroX uses non-interactive JSONL, ephemeral sessions,
  ignores user exec rules/config for the worker invocation, passes KaroX Effort
  through the validated `model_reasoning_effort` config, and never enables
  dangerous bypass flags.
- **Claude Code** is available only for read/review roles through non-persistent
  print mode, safe mode, `dontAsk`, the `Read,Glob,Grep` tool set, and the native
  `--effort` value mapped from KaroX Effort. The built-in adapter grants no
  Edit/Write/Bash path.
- **Gemini CLI** and **OpenCode** may be discovered and displayed, but built-in
  automatic execution remains disabled until KaroX can prove an equally strong
  write-confinement contract. A hosting application may still register its own
  separately guarded adapter.

All built-in subscription children receive KaroX's minimal secret-filtered child
environment, so KaroX does not copy API-key/token environment variables into a
subscription agent. KaroX independently checks Git state before/after a worker
and runs the user-approved verification commands after relevant roles. A
read/review worker that changes repository state is rejected.

### `orchestration_recovery.py`

Crash-safe worker journal. A passed step is never replayed after restart. A step
that was `running` when KaroX restarted becomes `reconcile_required`; its adapter
must establish whether the external work completed or had no side effect before
a new attempt receives a new idempotency key.

This is the orchestration-level counterpart to Core mutation idempotency.

### `worktree_pool.py`

Detached worker worktrees live only under the KaroX runtime directory. The pool
never pushes, merges, rebases or rewrites history. It reports changed-file
overlap between worker worktrees and refuses to remove a dirty worktree.

With implementer isolation enabled, each implementer receives its own worktree;
a downstream tester/reviewer on one lineage inherits that same worktree. If a
later step depends on more than one dirty lineage, KaroX stops with an explicit
integration-required error instead of silently auto-merging them. An isolated
parallel implementation is not evidence that integration is safe.

### `economy_engine.py`

`SavingsReceipt` separates:

- actual measured cost;
- measured OFF-vs-ON baseline cost when supplied;
- measured cache savings;
- local context characters reused;
- tool-schema bytes/round trips/premium calls avoided when measured by the
  caller;
- accepted and verified quality gates.

No baseline means no dollar-savings claim. `ShadowRouteReport` and `ReplayLab`
label counterfactual route cost as a projection and explicitly do not claim the
counterfactual worker would have succeeded.

### `mission_control.py` and `mission_control_server.py`

Mission Control stores a compact agent/status/cost/context view and a bounded
remote command queue.

The optional mobile server is designed for loopback or a private Tailscale
address. Pairing uses a short-lived code submitted by POST; successful pairing
creates an HttpOnly, SameSite=Strict cookie. KaroX bridge credentials are not put
in URLs.

Mobile commands are consumed only at safe orchestration boundaries. An
`approve_request` command does **not** mint a RiskEngine confirmation token; it
only asks the normal Smart Stop flow to present/handle approval.

## CLI

The main KaroX parser now exposes:

```text
karox intelligence ...
karox orchestrate ...
karox mission-control ...
karox economy ...
```

Examples:

```text
karox intelligence list --json

karox intelligence add sub:codex \
  --name "Codex subscription" \
  --source subscription \
  --target-id codex \
  --role implementer \
  --capability code \
  --capability tools

karox intelligence quota sub:codex \
  --remaining-fraction 0.35 \
  --source codex-adapter

karox orchestrate recipes --json

karox orchestrate plan \
  --objective "Implement bounded auth retry" \
  --recipe feature \
  --preset maximum_economy \
  --risk high \
  --assign orchestrator=api:openai:planner \
  --assign implementer=api:openai:coder \
  --assign reviewer=api:anthropic:reviewer
```

`orchestrate run` can execute API endpoints immediately through
`NativeAgentExecutor`. It can also use the guarded built-in Codex/Claude
subscription adapters described above; other subscription/external endpoints
still require an application-registered adapter. This is an intentional safety
boundary, not a shell fallback.

```text
karox intelligence discover-agents --json
karox intelligence discover-agents --apply

karox orchestrate run \
  --repository . \
  --objective "Fix retry semantics" \
  --recipe bug-fix \
  --isolate-implementers \
  --verification-command '["python","-m","pytest","-q"]'
```

The Textual client exposes the same service rather than a second implementation:
`/orchestrate plan feature :: TASK`, `/orchestrate run feature :: TASK`,
`/agents [apply]`, and `/mission [RUN_ID]`. The slash menu itself is intentionally
kept to eight high-frequency commands; advanced commands remain routable when
typed.

Measured routing observations:

```text
karox orchestrate observe api:provider:model \
  --task-class implementation \
  --accepted --verified \
  --cost-usd 0.42 --tokens 120000
```

Shadow routing does not execute anything:

```text
karox orchestrate shadow-route api:provider:expensive \
  --task-class implementation \
  --role implementer \
  --capability code \
  --input-tokens 1000000 \
  --output-tokens 100000
```

Mission Control:

```text
karox orchestrate status RUN_ID
karox mission-control show RUN_ID
karox mission-control command RUN_ID steer --text "check the race condition"
karox mission-control serve RUN_ID --host 127.0.0.1 --port 8766
```

For remote phone access, bind only to an interface the user intentionally made
private/reachable (for example a private Tailscale address) and use the displayed
short-lived pairing code.

## Release claims

The code above makes these features testable; it does not by itself justify a
marketing savings percentage. Release notes may claim a savings figure only from
an attached reproducible economy benchmark or an actual measured task baseline.

Likewise, a subscription agent is supported only when its dated adapter contract
and live conformance evidence exist. Merely registering `source=subscription` in
the Intelligence Pool is not a compatibility claim.
