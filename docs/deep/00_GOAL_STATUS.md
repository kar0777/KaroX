# Deep refactor — goal status

**Goal:** `b/win/docs/goals/KAROX_V5_DEEP_REFACTOR_AND_BENCHMARK_GOAL.md`
**Product worktree:** `b/karox5-deep`
**Branch:** `refactor/karo5-deep-competition-20261107`
**Base:** `release/karox5-5.0.0` @ `066a35134ff5f288413c8ff3d47068706fc8ae8a`
**Updated:** 2026-11-08

## Phase

- [x] Phase 0 — frozen baseline, feature inventory, architecture map (this file set)
- [x] Phase 1A — evidence-driven dead-legacy removal (two batches; adversarial review)
- [x] Phase 1B — deep refactor of every major monolith (CLI, browser, engine,
      tools/core/process, providers/adapters, MCP, hosted/bridge launcher/proxy/
      tailscale/oauth/profiles, repo context, verification, TUI, web_check).
      One cohesive route factory remains intentionally unsplit; all seams are
      covered by bridge characterization tests.
- [x] Phase 2 — full parity/security/build/release gate (full suite at `51be451`;
      frozen wheel SHA-256 `815030a7…ba62f5`; fresh install `doctor status ok`)
- [ ] Phase 3 — honest latest-CLI benchmark arena (50/50 DEV tasks validated;
      preliminary: Karo 46/50, Codex 7/50, OpenCode 2/22 attempted, Claude
      blocked; no holdout/winner claim; expand to 100 + frozen holdout)
- [ ] Phase 4 — evidence-driven optimization loop
- [ ] Phase 5 — local 5.x release candidate

## Progress at a glance

| Subsystem | Original | Current |
|---|---:|---:|
| `cli.py` | 2,299 | 193 (+8 owner modules) |
| `browser/session.py` | 3,703 | 173 (+11 owner modules) |
| `engine_kernel.py` | 2,905 | 753 (+8 owner modules) |
| `check_report.py` | 1,293 | 295 (+2 owner modules) |
| `completion_gate.py` | 1,278 | 565 (+3 owner modules) |
| `repo_context.py` | 2,055 | 148 (+4 owner modules) |
| `core.py` | 1,573 | 453 (+4 owner modules) |
| `core_tools.py` | 1,802 | 780 (+4 owner modules) |
| `core_process.py` | 1,126 | 836 (+ `process_tree` 344) |
| `mcp_client.py` | 1,201 | 588 (+ types/registry/transport) |
| `disk_maintenance.py` | 1,099 | 217 (+ protection/scan/plans) |
| `hosted_core.py` | 1,162 | 821 (+ contract/health/approval) |
| `providers.py` | 1,638 | 635 (+ types/stream) |
| `provider_adapters.py` | 1,503 | 86 (+ base/OpenAI/Anthropic/Gemini) |
| `engine_tool_env.py` | 1,864 | 582 (+ finish/guidance/attach/support; bodies 544) |
| `tui/app.py` | 2,038 | 569 (+ support/session/commands/run) |
| `tui/widgets.py` | 1,141 | 910 (+ render 314) |
| `web_check.py` | 1,831 | 1,118 (+ scripts/document) |
| `bridge_launcher.py` | 2,389 | 1,949 (+ `bridge_host` 547) |
| `bridge_proxy.py` | 2,218 | 1,707 (+ `bridge_wire` 632) |
| `bridge_tailscale.py` | 1,585 | 225 (+ status/gui/funnel) |
| `bridge_profiles.py` | 1,144 | 864 (+ `bridge_profile_types`) |
| `bridge_oauth.py` | 1,483 | 936 (+ `bridge_oauth_types`, `bridge_oauth_app`) |
| Package modules | 164 | 243 |
| Package lines | 87,689 | 90,849 (ceiling 91,500) |

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

