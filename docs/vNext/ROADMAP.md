# KaroX vNext Roadmap

Progress is evidence-based. “Complete” means implementation plus the tests named
for the phase; documents and interfaces alone do not complete a phase.

## Phase 0 — baseline and design

- Audit current code, CI, integrations, and security boundaries.
- Keep all legacy executable tests green.
- Publish product, architecture, security, migration, provider, MCP, session,
  Pack, and status documents.

Gate: clean baseline commit, honest integration statuses, reviewed boundaries.

## Phase 1 — runnable foundation

- Installable `karox` package and versioned configuration layout.
- Core command/policy boundary with origin identity.
- Durable sessions, locking, idempotency, and legacy discovery/migration report.
- Provider and Agent Kernel contracts used by runnable code.

Gate: unit/integration tests plus session restart and lock E2E.

## Phase 2 — native vertical slice

- Generic OpenAI-compatible streaming provider.
- Agent loop, validated tool calls, file mutation, checks, Git diff, verification,
  and evidence report through CLI/JSON mode.
- Deterministic fake OpenAI server E2E with no external key.

Gate: KB-HYBRID-01 repeatably passes and a failed check cannot report success.

## Phase 3 — providers and credentials

- OpenAI Responses, Anthropic Messages, and Gemini adapters.
- Local/custom compatible profiles, model registry, secure credentials, usage,
  budgets, health, fallback, and provider contract suite.

Gate: deterministic adapter conformance; live services remain opt-in and are
labelled untested without recorded E2E.

## Phase 4 — Skills

- Multi-directory metadata discovery, lazy content, permissions, validation,
  and CLI.

Gate: lazy-load and attempted permission-bypass E2E.

## Phase 5 — MCP client

- stdio and Streamable HTTP, supervision, namespace/schema validation, grants,
  cancellation, and health.

Gate: native agent uses a deterministic external stdio MCP (KB-HYBRID-05).

## Phase 6 — unified handoff

- Provider switching, hosted/native attachments, structured handoff, recovery,
  locks, and reconciliation.

Gate: KB-HYBRID-03/04/07/09.

## Phase 7 — MCP proxy and bridges

- Explicit selected-tool exposure, double policy evaluation, identity, secret
  isolation, bridge profiles, rotation/revoke, and Notion regression.

Gate: KB-HYBRID-02/06/08 and verified Notion E2E; other profiles stay honest.

## Phase 8 — Pack SDK

- Manifest, generator, atomic lifecycle, compatibility, grants, test harness,
  docs, and `project-inspector` sample.

Gate: sample lifecycle and bypass tests pass cross-platform.

## Phase 9 — terminal UX

- Complete line/JSON/CI commands first; add a fast TUI over the same services.
- Provider/model/session/MCP/Skill/Pack/bridge management and slash commands.

Gate: non-interactive paths remain fully functional and TUI smoke tests pass.

## Phase 10 — benchmark and release readiness

- KB-HYBRID-01 through 10 with raw run records, failures, latency, usage, cost,
  evidence, and limitations.
- Migration rehearsal, cross-platform matrix, security review, truthful README.

Gate: release-readiness report recommends merge or lists blockers. No merge,
push, or release is part of this branch’s implementation work.
