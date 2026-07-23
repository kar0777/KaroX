# KaroX vNext Architecture

Status: accepted target architecture. Implementation status is tracked in
`IMPLEMENTATION_STATUS.md`.

## Dependency rule

Dependencies point inward toward Core contracts:

```text
CLI / TUI       Hosted bridges       Automation API
     \                |                 /
      Agent Kernel / Bridge Orchestrator
                    |
          Capability Policy + Sessions
                    |
              Core Runtime ports
                    |
 files  processes  git  checks  browser  desktop  external MCP

Provider adapters -> Agent Kernel only
Pack/Skill adapters -> capability-scoped Core ports only
```

Only Core Runtime implementations may perform local actions. The kernel,
providers, bridge profiles, Skills, Packs, MCP proxy, CLI, and TUI must request
actions through typed Core commands carrying session, origin, capability, and
idempotency identity.

## Packages

The new installable package lives under `src/karox/`. Legacy modules in
`server/` and launch scripts remain behind adapters until their behavior has
equivalent vNext regression coverage.

### Core Runtime

- Validates repository-relative paths and symlinks.
- Executes file, patch, process, check, Git, browser, and desktop operations.
- Applies capability policy before side effects.
- Enforces output limits, timeouts, idempotency, and cancellation.
- Emits correlated audit events and evidence records.
- Does not import provider, bridge, CLI, or TUI code.

### Agent Kernel

Runs model → tool calls → Core → tool results until a bounded terminal state.
It owns malformed-call handling, retries, streaming recovery, loop detection,
budgets, compaction, verification, and repair. A provider response may propose
completion; only verification evidence can confirm it.

Independent tool calls may run concurrently only when declared read-only and
free of overlapping resources. Mutation is serialized per session and guarded
by idempotency keys.

### Provider Layer

Adapters translate a common request/event contract to vendor APIs. They own
transport, authentication injection, usage normalization, and provider error
classification. They cannot import Core implementations or execute tools.

The generic OpenAI-compatible adapter is the first vertical slice. Dedicated
adapters are used only where semantics differ: OpenAI Responses, Anthropic
Messages, and Gemini. Ollama, LM Studio, and vLLM are profiles of the generic
adapter unless their verified behavior requires a specialization.

### Policy and identity

Every invocation has an immutable origin:

- `user`
- `native_agent`
- `hosted_client`
- `skill`
- `proxied_mcp`
- `pack`

An origin also carries a concrete identity (provider/model, bridge client,
skill name/version, MCP server/tool, or pack name/version). Effective access is
the intersection of session profile, origin policy, tool requirement, and an
optional short-lived capability token. No component may increase its own scope.

### Unified Sessions

Sessions store structured state, not an assumed canonical chat transcript.
They use an atomic on-disk record plus an exclusive mutation lease. Provider
history, model usage, evidence, jobs, decisions, changed files, and pending work
survive restarts. See `SESSION-MODEL.md`.

### MCP

KaroX remains an MCP server and also becomes an MCP client. Both paths share a
normalized tool descriptor and policy engine. The proxy exposes an explicit
allowlist of normalized tools; it never forwards credentials or the raw local
environment. See `MCP-ARCHITECTURE.md`.

### Skills and Packs

Skill discovery reads metadata only. Full instructions are loaded after a user
or routing decision and cannot execute setup code. Packs are versioned bundles
with a manifest, lifecycle, health checks, and declared permissions. Built-in
tools supplied by Packs are registered through Core rather than called around
it.

## Command and event contracts

A Core command contains:

- command type and version;
- session ID and repository fingerprint;
- origin identity;
- required capability and resource scope;
- correlation and idempotency IDs;
- bounded input and output declarations;
- optional approval reference and deadline.

Core returns a typed result with status, redacted output, timings, mutation
metadata, and evidence references. Audit events form an append-only diagnostic
stream; session snapshots remain the recovery source of truth.

## Configuration boundaries

Configuration is split rather than stored in one universal object:

- public provider/model metadata;
- opaque credential references;
- session state;
- bridge profiles;
- MCP registrations and grants;
- Skill/Pack manifests;
- user defaults.

Secrets are never serialized into these files. Repository configuration cannot
grant permissions above user/session policy.

## Recovery and failure semantics

- State is persisted before acknowledging a mutation.
- Retried mutations reuse an idempotency key and return the prior result.
- Provider/MCP transport retry is allowed only before an acknowledged mutation
  or with a matching idempotency record.
- Interrupted jobs are marked unknown until reconciled; they are not silently
  restarted.
- Corrupt state is quarantined and reported rather than replaced with defaults.
- Fallback providers require capability compatibility and budget/privacy policy.

## Legacy containment

`server/repo_tools.py` currently combines configuration, policy, execution, API,
and audit in one import-time module; launch scripts also contain product logic.
During migration, a legacy bridge adapter invokes the existing application, but
new native features target vNext interfaces. Notion code remains unchanged until
the vNext path has equivalent integration coverage.

## Testing architecture

- Unit: policy, models, normalization, budget, compaction, manifests.
- Contract: each provider and MCP transport against deterministic fakes.
- Integration: Core commands against temporary Git repositories.
- E2E: complete native/hosted workflows with evidence.
- Regression: existing script-style suites, especially Notion and installers.

The baseline `scripts/test_*.py` files are executable test programs rather than
discoverable `unittest` cases; `unittest discover` currently collects zero. The
vNext suite must use a normal test runner while CI continues executing legacy
programs until migration is complete.
