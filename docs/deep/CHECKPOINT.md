# Deep-refactor checkpoint

**Updated:** 2026-11-07 21:55 Europe/Warsaw
**Worktree:** `D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\karox5-deep`
**Branch:** `refactor/karo5-deep-competition-20261107`
**Base:** `release/karox5-5.0.0` @ `066a35134ff5f288413c8ff3d47068706fc8ae8a`
**Goal:** `b/win/docs/goals/KAROX_V5_DEEP_REFACTOR_AND_BENCHMARK_GOAL.md`

## Status

Phase 0 (baseline/inventory/plan) complete. Phase 1A started: one dead second
verification API removed; the checkpoint module was restored after adversarial
review (latent capability, not dead). Phase 1B started with the CLI split.

| Workstream | State | Evidence |
|---|---|---|
| Baseline freeze | DONE | `docs/deep/01_BASELINE.md`; wheel SHA verified |
| Dead-code batch 0 | DONE (verification API only; checkpoint restored) | `docs/deep/02_DEAD_LEGACY_EVIDENCE.md` |
| CLI split | IN PROGRESS — files split; targeted CLI/MCP/tree tests PASS; engine integration tests running | this file |
| Engine split | PLANNED — implementation-ready mixin plan received; identity constraints recorded | `docs/deep/ARCHITECTURE_REFACTOR_PLAN.md` |
| Browser split | PLANNED — mixin plan received; first step = `browser/contract.py` (breaks guards→session cycle) | plan in session notes |
| Bridge split | PLANNED — 31-module plan + characterization-test prerequisites received | plan in session notes |
| Benchmark arena | NOT STARTED (correctly blocked on Phase 1/2) | — |

## Gates run in this worktree

| Gate | Result |
|---|---|
| `python scripts/check_tree.py` (base) | PASS — 164 modules / 87,689 lines |
| `python scripts/check_tree.py` (current) | PASS — 172 modules / 87,658 lines |
| `ruff check src tests scripts` | PASS |
| `mypy cli entrypoint tui` (base) | PASS — 13 files |
| Full `pytest` on base with batch-0 edit in flight | exit 0 (1542 collected); recorded as a smoke, not a clean pre-change baseline |
| Targeted CLI/MCP/tree tests after split | PASS (`test_cli`, `test_local_runtimes`, `test_session_export`, `test_agent_report_surface`, `test_tree`, `test_mcp_cli`) |

## Decisions that must not be lost

1. `ellipsis_checkpoint.py` is **kept**: it was the implementation of the
   documented hosted rollback hook, and Git history shows the hook existed.
   Wiring it back (or formally retiring the feature) is tracked separately.
   Do not delete it as "dead".
2. `route_health` trackers/probes, `requirements.delegated_source_context` and
   the effort/mode display helpers are **lost wiring**, not dead code. Keep.
3. `command_jobs.run_job`, `workspace_worker`, `browser.tools`, `tui.app`,
   `desktop_apps`, `engine/*` and `computer_use.__main__` are dynamic
   entrypoints. Never judge them by static imports.
4. `EngineAgent` must remain a concrete class in `engine_kernel.py`
   (`__class__.__module__` is written to the trajectory and asserted);
   extract a mixin, not the class. Keep `_any_language_server` in the facade
   because tests monkeypatch it there.
5. Browser: use thin mixins; keep the `BrowserSession` façade, the owner-thread
   fail-closed behavior, and every action wrapper. First split is
   `browser/contract.py` (breaks `guards → session`).
6. CLI: tests patch `karox.cli._registry` (kept as a facade seam),
   `karox.cli_runtime.OpenAIChatCompletionsProvider` and
   `karox.cli_session.session_dir` (patch targets updated with the move).

## Package budget

- `MAX_PACKAGE_LINES = 88_000`; current 87,658 → 342 lines headroom.
- The CLI split added ~225 net lines of cohesive modules (8 new files; one
  2,299-line file became one 193-line façade + focused modules).
- The engine/browser splits will need a recorded budget decision (raise both
  `scripts/check_tree.py` and `tests/test_tree.py` together, with the measured
  reason) unless the concurrent simplification removes enough duplication.

## Cost ledger

No paid model/benchmark runs have been started in this goal yet (Phase 1).
Subagent work is internal. Benchmark spend starts only after Phase 1B/2.

## Next automatic actions

1. Finish the CLI-split integration test run; commit batch 0 and CLI split as
   separate reversible commits.
2. Run the full suite for the CLI split.
3. Apply engine split step 1 (`engine_agent.py` mixin; concrete shell stays).
4. Add bridge characterization tests before moving any modern-wire/approval
   code (S0 in the bridge plan).
5. Continue batches A→H; update this checkpoint after every commit.

## Safe continuation

```powershell
$py = 'D:\проекты\KaroX-v5\lab\kernel-duel-20261025\local-runs\venv-m16\Scripts\python.exe'
$wt = 'D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\karox5-deep'
$env:PYTHONPATH = "$wt\src"
$env:KAROX_CONFIG_DIR = "$env:TEMP\karox5-deep-config"
$env:KAROX_RUNTIME_DIR = "$env:TEMP\karox5-deep-runtime"
$env:KAROX_DISABLE_UPDATE = '1'
Set-Location $wt
```

Do not push, publish, reset/clean foreign worktrees, alter the user's installed
KaroX, or start a public tunnel. See `docs/deep/ARCHITECTURE_REFACTOR_PLAN.md`
for the batch order.
