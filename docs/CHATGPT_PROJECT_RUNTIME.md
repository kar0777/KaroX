# ChatGPT Project Runtime for KaroX

## Goal

Make ChatGPT Web the primary user interface while KaroX provides durable local engineering state, compact continuation, guarded tools, parallel workstreams, artifacts, verification and optional external workers.

## Product contract

```text
ChatGPT Project
    |
    | custom MCP app / Secure MCP Tunnel
    v
KaroX hosted runtime
    |
    +-- ChatGPT Project binding capsule
    +-- durable KaroX session
    +-- compact resume + checkpoints
    +-- task workstreams
    +-- artifact-backed large outputs
    +-- guarded repository / Git / checks / browser
    +-- Intelligence Pool / orchestration workers
```

ChatGPT does not currently provide a trusted ChatGPT Project identifier to a third-party MCP server. Therefore KaroX must not claim that it can authenticate a request by ChatGPT Project name. Instead, a non-secret binding capsule is stored in one ChatGPT Project's instructions and verified by KaroX to prevent accidental cross-project continuation. OAuth and KaroX session permissions remain the actual security boundary.

## Implemented now

1. `karox.chatgpt_project.bind`
   - binds one durable KaroX session to one human-readable ChatGPT Project name;
   - returns a stable instruction capsule;
   - changing the bound project requires explicit rotation;
   - the binding is explicitly labelled non-authentication.

2. `karox.chatgpt_project.resume`
   - verifies the project capsule;
   - returns a compact durable session snapshot;
   - includes objective/session state, plan, decisions, changed files, checks, failures and unfinished actions;
   - includes compact summaries of up to 16 named parallel workstreams while reporting the true total/returned/truncated counts;
   - enforces a hard 24 KiB transport budget with a minimal continuation fallback instead of returning an oversized capsule;
   - avoids replaying large raw tool outputs.

3. Hosted orchestration
   - `karox.intelligence.list`, `karox.orchestrate.recipes`, `karox.orchestrate.plan` and `karox.orchestrate.status` ship in the normal ChatGPT Web surface;
   - write-capable profiles additionally expose `karox.orchestrate.start` and `karox.orchestrate.control`;
   - ChatGPT Web remains the supervising planner: hosted start uses deterministic KaroX planning (`--no-delegate-workers`) and pins the exact preflight-selected endpoints before the detached process starts;
   - implementers are always launched with worktree isolation; the hosted client cannot disable it;
   - approved verification commands are forwarded to the detached run;
   - Mission Control ownership is bound to the durable KaroX session + repository fingerprint before execution;
   - hosted run IDs are stable across retries of the same idempotency key, and launch claims fail closed across the spawn -> durable-record crash window;
   - control commands use retry-stable command IDs and refuse dead or unproven/reused process identities before queueing;
   - short mutation-lease collisions use bounded waiting instead of being misreported as lost permission;
   - pause/resume/stop occur only at orchestration safe boundaries; no arbitrary PID kill is exposed;
   - hosted runs use `HostedProjectOrchestrationRuntime`, where `steer` may target a role or step without leaking that instruction to unrelated workers.

4. Default `chatgpt-web` bundle
   - includes bind/resume and read-only orchestration discovery/status automatically;
   - durable saved ChatGPT profiles self-heal legacy explicit tool snapshots at connect and child restart, so new continuity, memory and orchestration tools do not remain silently disabled after a KaroX upgrade;
   - the compatibility bundle is target-scoped to saved `chatgpt-web` profiles and never adds repository write, browser input, network, publish, push or credential capabilities;
   - bind works in read-only KaroX sessions because binding metadata is session-local rather than repository mutation;
   - process-changing orchestration calls remain write-tier only and are backfilled only for profiles that already grant workspace-write/elevated process execution.

5. Existing KaroX components reused rather than duplicated
   - `TaskStateStore`: bootstrap/checkpoint/resume/workstreams;
   - `ArtifactStore`: large outputs by reference;
   - `RepositoryContextEngine`: bounded repository context;
   - `PlanExecutor`: bounded multi-step operations;
   - `AffectedChecksEngine`: compact verification;
   - `IntelligencePool`: optional API/subscription/local workers;
   - `BackgroundOrchestrationRegistry`: detached worker survival across ChatGPT bridge reconnects;
   - `MissionControlStore`: safe control + session/repository ownership;
   - `OrchestrationJournal`: fail-closed crash recovery and no blind replay of in-flight mutations.

## Remaining hardening gates

1. Secure transport: OpenAI now officially documents Secure MCP Tunnel for local/private MCP servers. Prefer it when the target ChatGPT plan/product supports the required MCP actions; retain stable Tailscale/custom HTTPS fallback and do not depend on rotating Quick Tunnel URLs for durable projects. Current OpenAI Help documentation says full MCP write/modify support is a Business/Enterprise/Edu beta, while Pro custom MCP access is read/fetch-limited, so KaroX must feature-detect rather than promise write support from the tunnel alone.
2. Automatic continuation policy: project instructions should call `karox.chatgpt_project.resume` at the start of a new project chat or after bridge reconnect before substantial work.
3. Compaction policy: continue moving raw logs, large diffs and browser traces into artifacts; inline only structured summaries plus artifact IDs.
4. Recovery UX: hosted status surfaces journal recovery state. A dead run with an in-flight journaled mutation must be reconciled before retry; never auto-replay it.
5. Conformance: add a live ChatGPT Project record for bind -> resume -> start parallel workers -> targeted steer -> guarded write -> checks -> bridge reconnect -> status/resume, plus wrong-binding and wrong-session ownership tests.

## UX target

The user opens one ChatGPT Project and works normally. KaroX is selected as the project app. New chats recover the compact KaroX state instead of replaying previous chats. Parallel work is represented as KaroX workstreams/optional workers, while ChatGPT remains the main planner and final judge.
