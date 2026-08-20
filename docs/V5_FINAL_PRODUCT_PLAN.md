# KaroX v5 — Final Product Plan (scope freeze)

Date: 2026-08-20. Branch: `feat/karox-v5-competitive-upgrade`. Workstream: `release-live-pass3`.
Positioning: **"One local runtime for every AI coding agent."** Models change, clients change;
projects, memory, permissions, and evidence stay in KaroX.

Status inputs: green baseline committed locally 2026-08-20 (34 conventional commits, no push).
Ruff/Mypy/pytest/wheel gates GREEN per dated baseline. ChatGPT + 2 Adapt accounts live on the
shared `chatgpt-dev` bridge. Release decision remains **NOT READY** until P0 closes.

## 1. Competitive audit (bounded, public sources)

Strongest public ideas relevant to a local-first universal MCP runtime:

1. Deferred tool loading / tool search (Anthropic): stable core catalog, load schemas on demand (~85% token cut on large MCP catalogs).
2. AGENTS.md-native onboarding with nested precedence (Codex/Copilot/Junie); CLAUDE.md,`.cursorrules`, GEMINI.md as fallbacks.
3. Progressive disclosure (Claude Skills three-level model): summaries + artifact handles, never raw 50KB dumps.
4. Cache-friendly stable-prefix context layout (Aider/Claude Code).
5. Shadow-repo checkpoints with dual-axis rewind (Gemini CLI/Claude Code).
6. Composable permission rules + read-only plan mode (Claude Code/Codex OS sandbox).
7. Lifecycle hooks with exit-code semantics (Claude Code).
8. Durable, forkable thread/handoff objects (Amp).
9. Aider-style repo map: tree-sitter symbol graph + PageRank under a token budget, zero infra.
10. Auto-memory with user approval (Windsurf/Cursor Memories).

## 2. Scope

### P0 — cannot honestly ship v5 without
- **P0.1 Universal Memory Layer**: one `KaroXMemory` with USER/PROJECT/WORKSTREAM/SESSION scopes;
  structured entries (kind, provenance, confidence, sensitivity, ttl, validation); privacy hard-stops
  (no credentials/secrets ever); budgeted retrieval (scope+relevance+recency, no memory dumps);
  source-hash revalidation for project facts.
- **P0.2 Memory Protocol for external MCP clients**: `karox.memory.context/remember/recall/list/forget`
  tools + preset instructions nudging agents to use them. Cross-client acceptance: fact stored via
  ChatGPT is recalled via Adapt and via native/API provider without shared transcripts.
- **P0.3 `karox init` + Project Map**: local, cheap detection (languages, entrypoints, tests, build,
  package manager, key dirs, git structure); ingest AGENTS.md/CLAUDE.md/README/.cursorrules when present;
  deterministic FACT MAP with incremental rescan + stale-fact invalidation; optional one-shot semantic
  map built on the compressed structure.
- **P0.4 Quality-economy wiring audit**: prove CostLedger/StablePrefixCache/ToolSchemaDeduplicator/
  ReadCache/BatchPlanner/CostGovernor are used by the normal runtime turn, with measurable counters
  (cache hits, repeated bytes avoided, artifact bytes elided, round trips avoided). same_model /
  same_reasoning / same_verification — economy on plumbing only.
- **P0.5 Live acceptance set**: multi-project Ctrl+W UI smoke; one bounded real provider/API turn (Luna);
  cross-client memory demo; bridge restart/reconnect conformance re-smoke after architecture changes.
- **P0.6 Release gates + truthful docs**: Ruff, Mypy, full pytest, coverage, wheel + contents +
  installed-wheel smoke, security checks, CI parity; README (5 pillars), release checklist, changelog,
  demo script (`docs/V5_DEMO_SCRIPT.md`); no NOT READY → READY flips without closed criteria.

### P1 — strong improvements worth landing if bounded
- **P1.1 PhysicalBridge / ClientBinding / ClientPreset contract**: formalize service ≠ physical runtime;
  presets add clients via config, not new runtime implementations. (Foundation already live-proven by
  the shared bridge serving 3 clients.)
- **P1.2 Per-client credentials/grants**: one endpoint, separate revocable logical grants; legacy shared
  bearer migrates safely; revoke one client without breaking others. If risk exceeds budget → minimal
  safe subset (grant issuance + revoke) and the rest POST-V5.
- **P1.3 Connections TUI simplification**: one bridge card with client list, not N fake servers.
- **P1.4 Adaptive/Concise output policy**: KaroX-level output modes (Adaptive default if UX confirms);
  MODEL WORK != USER NARRATION; details behind artifacts.
- **P1.5 Tool catalog economy**: deterministic core/browser/devserver/admin groups per profile; no hidden
  magic, availability stays predictable.
- **P1.6 Durable handoff objects**: forkable, referenceable workstream handoffs (goal/done/remaining/
  evidence/map_version/memory_refs/next_safe_action) surviving compaction and client switch.
- **P1.7 `karox insights` (minimal)**: repeated reads, slow tools, cache efficiency, map/memory hit rates
  from existing telemetry; read-only report, no new collection paths.

### POST-V5
- Full insights dashboards/leaderboards; OTel export.
- Lifecycle hooks (pre/post tool, blocking) with exit-code contracts.
- Shadow-repo checkpoints + dual-axis rewind for arbitrary clients.
- Skills packaging; background/cloud agents; DNS-pinning proxy; auto-memory approval UI.
- Anything not listed above. **Scope is frozen as of this document.**

## 3. Maturity matrix

Feature | Priority | Foundation | Wired | Measured | Live | Release blocker
--- | --- | --- | --- | --- | --- | ---
Bypass unified access mode | P0-done | yes | yes | n/a | yes (ELEVATED live) | no
Test isolation (keyring/paths/profiles) | P0-done | yes | yes | yes (guard tests) | yes | no
Multi-project registry/leases/workspace | P0.5 | yes | yes | partial | UI smoke pending | yes
Universal Memory Layer | P0.1 | partial (task_state/checkpoints/repo_context) | no | no | no | yes
Memory Protocol (MCP tools) | P0.2 | no | no | no | no | yes
karox init + Project Map | P0.3 | partial (repo_context) | no | no | no | yes
Quality economy (cache/batch/artifact-first) | P0.4 | yes (classes) | unproven | no | partial (artifact-first observed live) | yes
Handoff / compaction recovery | P0.1 | yes | partial (task.resume/checkpoint live) | no | partial | yes
Provider live (Luna bounded) | P0.5 | yes | yes | n/a | pending | yes
Cross-client memory demo | P0.5 | no | no | no | no | yes
Release gates + docs truthfulness | P0.6 | yes | yes | yes | n/a | yes
PhysicalBridge/ClientBinding contract | P1.1 | partial (durable saved identity) | partial | n/a | yes (3 clients, 1 bridge) | no
Per-client credentials | P1.2 | no | no | no | no | no
Connections TUI simplification | P1.3 | partial | no | n/a | no | no
Adaptive output policy | P1.4 | partial (result_envelope) | no | no | no | no
Tool catalog economy | P1.5 | yes (profile-scoped catalog) | partial | no | yes | no
Durable handoff objects | P1.6 | partial | no | no | no | no
karox insights (minimal) | P1.7 | partial (telemetry/usage_analytics) | no | no | no | no

## 4. Own improvements (impact/complexity/risk/release relevance/uniqueness)

1. Memory Protocol tools for external MCP clients — High/Medium/Low/P0/Unique (no public runtime offers
   cross-client memory to arbitrary MCP hosts).
2. Fact-map-first init (deterministic local scan, semantic pass optional) — High/Medium/Low/P0/Strong
   (cheaper than embedding indexes, honest about evidence).
3. Stable-prefix + read-cache wiring with counters — High/Low/Low/P0/Moderate.
4. Deterministic tool groups per profile (no hidden magic) — Medium/Low/Low/P1/Moderate.
5. Forkable handoff objects with evidence refs — Medium/Medium/Low/P1/Strong.
6. Per-client revocable grants on one endpoint — High/High/Medium/P1-eval/Strong.
7. Lifecycle hooks — Medium/Medium/Medium/POST-V5.
8. Shadow-repo checkpoints — Medium/High/Medium/POST-V5.

## 5. Execution order after freeze

1. P0.4 wiring audit (fast, informs everything).
2. P0.1 memory layer core + privacy + retrieval budget.
3. P0.2 memory protocol tools + preset instructions.
4. P0.3 karox init + fact map + incremental rescan.
5. P0.5 live acceptance battery (Ctrl+W, Luna, cross-client memory, restart conformance).
6. Selected P1 within budget (P1.3, P1.4, P1.5 first; P1.2 minimal-safe or defer).
7. Feature freeze → gates → RC artifacts (no tag/push/publish without approval).

Local commits after each green subsystem. Checkpoint before any compaction.
