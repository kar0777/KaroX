# GPT Web Autonomous Tool Efficiency Foundation

**Status:** implementation complete for phases A–H. Local Windows acceptance now passes the current managed full suite, branch coverage, release gates, wheel build, and isolated installed-wheel autonomy acceptance. Remote multi-OS CI, the real paired ChatGPT Web benchmark, and any live-profile migration remain separate gates; see `docs/evidence/local-autonomy-acceptance-2026-08-07.md`.

**Priority client:** ChatGPT Web. Existing low-level tools remain backward compatible; high-level tools are additive and versioned.

## 1. Goals and non-goals

The foundation reduces the number of ChatGPT tool calls, repeated reads, inline
context volume, retry ambiguity, cross-chat state loss, and concurrent-write
risk while preserving KaroX's repository-scoped permission model.

It adds deterministic server-side composition, not an internal LLM. KaroX does
not decide product requirements, invent code changes, or bypass a user boundary.
The external coding client remains responsible for reasoning and for supplying
any concrete patch/fix candidate.

The current live `clickup-opus` profile is not migrated by this implementation.
No public low-level tool is renamed or removed.

## 2. Architecture

The implementation composes with existing KaroX subsystems instead of replacing
them:

- `proxy_server.py` is the central hosted MCP wire and attaches best-effort tool
  telemetry plus artifact-backed result compaction.
- `artifacts.py` remains the session-owned artifact store and now supports
  selective bounded reads.
- `sessions.py`, checkpoints, transcript, verification, plan/act, smart-stop,
  session view, cost intelligence, and connection status remain authoritative
  for their existing responsibilities.
- `task_state.py` adds a separate checksum-protected task projection optimized
  for cross-chat recovery, avoiding a risky migration of the strict session
  record schema.
- `autonomy_runtime.py` exposes the versioned high-level tools beside the core
  and hosted-tools runtimes.
- `repo_context.py` performs deterministic repository inspection with ripgrep,
  Python AST, path/test/doc ranking, imports/calls, Git identity, excerpts, and a
  revision-aware cache.
- `plan_executor.py` executes a bounded journaled sequence/DAG by delegating to
  the existing guarded low-level runtimes.
- `affected_checks.py` selects relevant checks and classifies failures only from
  controlled evidence.
- `repository_lease.py` serializes repository mutations across different KaroX
  sessions while allowing concurrent read-only clients.
- `client_capabilities.py` records the client's effective capabilities without
  treating a policy-imposed read-only client as a broken connection.
- `autonomy_benchmark.py` runs the isolated server-side and deterministic
  simulated-client benchmark. It does not claim to measure a model.

## 3. Public high-level tool contracts

All contracts use schema version 1. Existing low-level tools remain available
according to the selected profile.

### `karox.task.bootstrap`

Mutation only because it creates or refreshes authoritative task state. One call
returns a compact recovery snapshot:

- repository, branch, revision, and dirty summary;
- objective and current task revision;
- relevant architecture entry points;
- access profile and hard permission boundaries;
- client capability snapshot;
- verification allowlist;
- running owned services;
- known blockers and pending user gates;
- recommended first safe action.

Observed Git facts are tagged `observed`; session bindings are tagged
`verified`; old agent statements are never promoted automatically.

### `karox.task.checkpoint`

Atomically updates selected task facts with revision preconditions. Agent input
may use `reported_by_agent`, `inferred`, `historical`, `stale`, or `pending`.
The tool rejects attempts by an agent to self-assert `verified` or `observed`.
Retries are idempotent.

### `karox.task.resume`

Loads authoritative state and compares stored branch/revision with the current
repository. A mismatch produces a stale-state warning and recommends a fresh
bootstrap before mutation.

### `karox.task.status`

Read-only provenance-aware task state.

### `karox.repo.inspect`

Input example:

```json
{
  "goal": "Find the saved bridge launch flow and related tests",
  "depth": "focused"
}
```

The tool deterministically returns:

- ranked relevant files and reasons;
- likely change points;
- relevant tests and docs;
- bounded excerpts;
- definitions, intermediate definitions, calls, imports, and types in the full
  artifact;
- cache status, content hash, and artifact metadata.

The cache key includes repository identity, HEAD or `unborn`, every dirty and
untracked file hash, arguments, policy profile, and schema version. A write
invalidates the cache naturally by changing repository identity. Ephemeral
build/cache directories are excluded from identity.

### `karox.task.execute_plan`

Executes a bounded sequence or forward-only DAG. Supported actions are:

- `inspect`, `search`, `read`;
- `patch` through guarded `karox.repo.command`;
- `command` and `checks` through allowlisted check/test tools;
- owned dev-server start/status/logs;
- allowed managed-browser operations;
- task checkpoint.

Each operation has an ID, action, inputs, dependencies, preconditions, expected
outcome, failure policy, stable idempotency key, and output policy. A plan with
writes is rejected unless a checks operation appears after the final write.

The executor journals each operation, resumes successful prior steps, rejects an
idempotency key reused for different input, maintains a repository lease,
checks repository identity before and after every step, and rejects unexplained
scope drift. A non-patch operation may not mutate the repository. A patch must
report every actually changed path and may not change repository revision.

Browser authentication/takeover, dev-server stop requiring a Tier 2 grant,
external side effects, destructive operations, publish, and deployment are not
allowed inside the plan.

### `karox.checks.run_affected`

Selects checks from:

- changed files;
- test naming and imports/symbol references;
- bounded Git co-change history;
- project configuration;
- explicit verification allowlist.

It returns selected checks with reasons, compact results, first failure, full
artifact, classification, and recommended next action.

Failure classification is evidence-based:

- no controlled baseline: `unknown`;
- same failure signature from the same check plan in a controlled baseline:
  `pre_existing`;
- failure absent from that baseline: `new`;
- passing current checks: `none`.

An optional fix loop accepts at most three distinct, externally supplied guarded
patch candidates. It rejects duplicate patches, scope growth, unreported file
changes, repository revision drift, and stops after the same failure signature.
KaroX does not use an internal model to generate fixes.

## 4. Safety tiers

Every telemetry event records the effective tier.

### Tier 0 — automatic read-only

Status, search, read, inspect, Git metadata, logs, screenshots, diagnostics,
artifact selection, task resume/status.

### Tier 1 — automatic scoped and reversible

Repository patches/creates inside scope, allowlisted commands, focused/affected
checks, owned dev-server start/status/logs, allowed managed-browser actions,
temporary artifacts, and repository-local reversible configuration.

### Tier 2 — pre-issued session grant

Broad write campaigns, long commands, test-fixture migrations, high browser
action budgets, and stopping owned runtime. The first implementation does not
silently grant Tier 2 inside `execute_plan`.

### Tier 3 — always user

Credentials, password, OAuth consent, CAPTCHA, 2FA, payments, account changes,
external messages/forms, publish, deployment, permission or repository-scope
expansion, credential rotation, destructive operations, personal Chrome,
keyring access, Git push, release, and takeover of a live mutating session.

## 5. Execution budgets and deterministic stops

Default `execute_plan` budgets:

- maximum steps: 20;
- wall time: 300 seconds;
- inline output: 32 KiB per delegated result;
- artifact bytes: 25 MiB per plan;
- writes: 10;
- command/check runs: 10;
- browser actions: 10;
- bounded attempts: 3;
- progress checkpoint every 5 steps.

Hard limits cap user-provided budgets. Execution stops on success, exhausted
budget, failed precondition, repeated identical failure, scope drift, stale
repository revision, permission boundary, user gate, lease conflict, or external
side effect. No blind rollback is performed.

## 6. Telemetry model

`tool_telemetry.py` records metadata only:

- trace, task, operation, session, connection/profile, and client identifiers;
- tool name and schema version;
- start timestamp and duration;
- success and error code;
- input/output/inline/artifact byte counts and result mode;
- cache hit and idempotent replay;
- permission tier/profile and user-gate flag;
- repository revision before and after.

It does not accept or persist tool arguments, file contents, user messages,
headers, tokens, keyring values, clipboard content, or browser form values. Task
text is represented by a stable hash. SQLite is bounded, WAL-backed, explicitly
committed/closed for Windows, and best-effort: telemetry failure never changes a
tool result.

## 7. Artifact-backed result model

Responses below the configurable threshold remain unchanged. Large safe JSON
responses are redacted before persistence and replaced inline by:

- summary and important findings;
- diagnostics and truncation flag;
- total size;
- artifact ID;
- available sections;
- content hash;
- expiry/persistence policy.

Selective reads support line ranges, tail, regex matches, first failure, JSON
path, and section. They are bounded to 1 MiB and cannot return arbitrary binary
artifacts. Images continue through the dedicated image tool.

## 8. Persistent task state

The checksum-protected state includes the requested objective, repository,
branch/revision, connection/access/client capability, decisions, phase progress,
files/operations/checks, known failures, blockers, pending gates, next safe
action, artifacts, and checkpoint timestamp.

Every fact has one provenance type:

`verified`, `observed`, `reported_by_agent`, `inferred`, `historical`, `stale`,
or `pending`.

Writes are atomic and share the session cross-process lock. Revision guards
reject stale updates. Restart and new-chat recovery load the same state without a
large manually copied handoff.

## 9. Concurrency model

Read-only sessions may run concurrently. A repository-scoped mutation lease
allows one mutating session/task/connection at a time.

A lease records owner identities, PID, process creation marker, created/heartbeat
and expiry times, and current operation. PID creation time defeats PID reuse on
Windows and `/proc` systems. A live owner is never automatically taken over,
even after heartbeat expiry. Strict stale recovery requires evidence that the
original process is gone or that the PID belongs to a different process.

A conflict returns owner identity, lease age, current operation, permission to
continue read-only, and safe options: wait, continue read-only, request user
takeover, or strictly recover a stale lease.

## 10. Client capability negotiation

The durable capability snapshot records:

- client kind and tool-schema snapshot version;
- read/write/browser/browser-input/image-artifact support;
- approval behavior and practical output limit;
- reconnect behavior;
- effective capability and reason;
- tool-count and tool-set digest.

If a client policy blocks writes, KaroX reports
`effective_capability=read_only` and `effective_reason=client_policy`; the
connection is not reported as broken. Diagnostics and task bootstrap include the
snapshot.

## 11. Benchmark methodology and measured results

Runner: `scripts/run_gpt_web_autonomy_benchmark.py`

Measured artifact: `scratch/gpt_web_autonomy_benchmark.json`

Paired real-client protocol:
`benchmarks/gpt_web_autonomy/PAIRED_CHATGPT_WEB_PROTOCOL.md`

The deterministic benchmark creates a separate temporary Git repository for
each baseline/optimized task variant. It runs ten required tasks and does not
mutate the KaroX working tree. It uses real high-level KaroX components with a
deterministic local fixture runtime. It uses no network and no external model.
Managed-browser and dev-server tasks measure their guarded contracts, not a real
Chromium/network server.

Measured simulated-client result:

- baseline task success: 100%;
- optimized task success: 100%;
- baseline median tool calls: 4.0;
- optimized median tool calls: 1.0;
- median tool-call reduction: 75.0%;
- median inline context reduction: approximately 99.1%;
- optimized recovery success: 100%;
- unauthorized writes/safety violations: 0;
- confidentiality violations after a credential sentinel was injected into raw
  check logs: 0 in optimized inline/artifact outputs;
- duplicate side effects during replay: 0;
- incorrect pre-existing classification: 0.

The baseline deliberately exposes the raw credential sentinel in low-level test
logs; optimized result/artifact redaction removes it. This is a deterministic
confidentiality regression test, not a claim about arbitrary future secrets.

These results are not ChatGPT model performance. The real paired ChatGPT Web
benchmark remains `not_run_user_gate` until two fresh chats are connected to
separate disposable baseline/optimized profiles.

## 12. Testing evidence

Focused tests cover telemetry redaction and Windows SQLite lifecycle, artifact
truncation/selection, task-state checksum/revision/provenance, repository cache
invalidation and path traversal, tracked Git porcelain parsing, plan replay and
scope drift, mutation lease/PID reuse/concurrency, affected-check baseline
classification and bounded fixes, client capability fallback, and the ten-task
benchmark.

Current local Windows evidence is recorded in
`docs/evidence/local-autonomy-acceptance-2026-08-07.md`: the managed full pytest
suite passed with 2197 passed, 5 skipped, and 1544 subtests; canonical branch
coverage passed at 72.4195% against the 70% gate; release gates, Ruff, Mypy,
wheel build, clean non-editable installed-wheel autonomy, restart recovery, and
isolated test-bridge acceptance are green. This is not a substitute for the
remote multi-OS matrix or named-product live evidence.

The known historical flaky `test_streamable_http` must be reported separately
and must not be relabeled pre-existing without controlled evidence.

## 13. Migration plan

1. Keep the current live `clickup-opus` bridge unchanged.
2. Complete full suite, packaging, wheel-content, and release gates.
3. Install the wheel non-editably in an isolated environment.
4. Start a separate test profile/session/port and, only if needed, tunnel route.
5. Verify existing low-level tools and all high-level tools with a simulated MCP
   client.
6. Verify checkpoint/resume after restart, repository-lease conflict,
   idempotent replay, and confidentiality invariants.
7. Prepare the paired ChatGPT Web benchmark.
8. Stop at the user gate and request permission before any controlled migration
   of the live profile.

No automatic migration, credential rotation, public URL change, commit, push,
publish, release, or deployment is part of this foundation.

## 14. Remaining blockers

- remote GitHub Actions for the exact accepted working tree are not proven without a push;
- Linux/macOS execution for this exact tree remains a remote matrix gate;
- real paired ChatGPT Web benchmark remains a user gate;
- controlled migration of `clickup-opus` requires explicit user permission.
