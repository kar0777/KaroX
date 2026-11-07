# Deep refactor — goal status

**Goal:** `b/win/docs/goals/KAROX_V5_DEEP_REFACTOR_AND_BENCHMARK_GOAL.md`
**Product worktree:** `b/karox5-deep`
**Branch:** `refactor/karo5-deep-competition-20261107`
**Base:** `release/karox5-5.0.0` @ `066a35134ff5f288413c8ff3d47068706fc8ae8a`
**Updated:** 2026-11-07

## Phase

- [x] Phase 0 — frozen baseline, feature inventory, architecture map (this file set)
- [ ] Phase 1A — evidence-driven dead-legacy removal
- [ ] Phase 1B — deep refactor of every monolith (batches A–H)
- [ ] Phase 2 — full parity/security/build/release gate
- [ ] Phase 3 — honest latest-CLI benchmark arena
- [ ] Phase 4 — evidence-driven optimization loop
- [ ] Phase 5 — local 5.x release candidate

## Work ledger

See `CHECKPOINT.md` for HEADs, gates and the next automatic action. See
`02_DEAD_LEGACY_EVIDENCE.md` for every removed path and its proof, and
`03_ARCHITECTURE_BEFORE_AFTER.md` for the architecture deltas.

## Non-negotiables carried into every batch

- No feature loss: browser/Playwright, Three.js/WebGL/3D visual checks, MCP
  outbound + hosted inbound, providers/routing/pricing, TUI, sessions/memory,
  skills, desktop, security gates, wheel resources.
- No new reverse dependency edge and no new import cycle.
- Every mutating tool still passes capability policy and the consequence engine.
- The TUI still talks to `agent run --stream-json` as a subprocess.
- Tests may be added, never weakened to hide a regression.
