# Deep-refactor checkpoint

**Updated:** 2026-11-07 22:20 Europe/Warsaw
**Worktree:** `D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\karox5-deep`
**Branch:** `refactor/karo5-deep-competition-20261107`
**Base:** `release/karox5-5.0.0` @ `066a35134ff5f288413c8ff3d47068706fc8ae8a`
**Goal:** `b/win/docs/goals/KAROX_V5_DEEP_REFACTOR_AND_BENCHMARK_GOAL.md`

## Status

Phase 0 (baseline/inventory/plan) complete. Phase 1A (dead legacy) has two
evidenced batches. Phase 1B is well underway: CLI and engine-agent seams split;
the browser session monolith is fully decomposed. All landed changes have
targeted tests green; a full-suite gate on `a8424d7` is running in an isolated
snapshot worktree (`b/karox5-deep-gate`).

| Workstream | State | Evidence |
|---|---|---|
| Baseline freeze | DONE | `docs/deep/01_BASELINE.md`; wheel SHA verified |
| Dead-code batch 0 | DONE | verification second API removed; checkpoint module restored after adversarial review |
| Dead-code batch 1 | DONE | 193 audited helper lines removed |
| CLI split | DONE | `cli.py` 2,299 → 193 + 8 modules; targeted tests pass |
| Engine split | PARTIAL | EngineAgent mixin, prompts, tool catalog done; control/IO/finish/harness pending |
| Browser split | DONE (session) | `browser/session.py` 3,703 → 173 + 11 owner modules; browser tests pass |
| Bridge split | NOT STARTED | plan + characterization-test prerequisites ready |
| Browser 3D/web_check split | NOT STARTED | deliberate; 3D checks are product features |
| Benchmark arena | NOT STARTED (correctly blocked) | — |

## Landed commits (latest first)

```
02d7a8b refactor(engine): extract the tool catalog/gating module
e29d45b refactor(engine): extract model-facing prompts module
a8424d7 refactor(browser): extract close/profile lifecycle mixin and finish the split
3fce4ce refactor(browser): extract browser launch mixin
120d779 refactor(browser): extract screenshot/upload/download evidence mixin
537d722 refactor(browser): extract waits and user-takeover mixin
987f4c6 refactor(browser): extract interaction mixin
afc12e5 build: raise the package line budget to 90k for the measured split overhead
dc3cb54 refactor(browser): extract observation and snapshot mixin
b57abf3 refactor(browser): extract event collectors mixin
7c3095b refactor(browser): extract navigation and tab management mixin
92b0b12 refactor(browser): extract the owner-thread runtime mixin
8dbaf2a refactor(browser): move input and payment guards into SessionGuardsMixin
c46aed6 refactor(browser): extract the session contract leaf
3121d04 refactor(engine): extract the EngineAgent mixin from engine_kernel
4b93bd8 refactor: remove 193 lines of independently audited dead helpers
1bc4702 refactor(cli): split the command surface into focused owner modules
54adfcc refactor(verification): remove the unused second verification API
```

## Gates

| Gate | Result |
|---|---|
| `ruff check src tests scripts` | PASS (latest) |
| `check_tree.py` | PASS — 185 modules · 87,862 lines (ceiling 90,000) |
| Browser targeted suite after each mixin | PASS (resources/units/core/code-source/facades) |
| CLI/session/MCP/tree targeted suite | PASS |
| Engine kernels/tools + prompt-cache + neutrality after step 1 | PASS |
| Full suite on `c46aed6` | **FAIL** — one test patched the old CLI facade for `_mcp_registry`; fixed at `120d779` and retested (`test_mcp_client` PASS) |
| Full suite on `a8424d7` (isolated snapshot worktree) | **PASS** — exit 0, 100% progress (log `local-runs/gate-a8424d7-full.log`) |
| Full suite on `HEAD` (engine prompts/catalog/control) | pending next round; those moves have targeted engine/neutrality/tree tests green |
| Wheel build/fresh install | pending after the next full green gate |

## Decisions that must not be lost

1. `ellipsis_checkpoint.py` is **kept**: it was the implementation of a
   documented hosted rollback hook; the history and the tracked wiring gap are
   in `docs/deep/02_DEAD_LEGACY_EVIDENCE.md`. Do not delete it as "dead".
2. `route_health` trackers/probes, `requirements.delegated_source_context` and
   the effort/mode display helpers are **lost wiring**, not dead code.
3. `command_jobs.run_job`, `workspace_worker`, `browser.tools`, `tui.app`,
   `desktop_apps`, `engine/*`, `computer_use.__main__` are dynamic entrypoints.
4. `EngineAgent` must remain concrete in `engine_kernel.py`; the mixin keeps
   `__module__`/trajectory identity. `_any_language_server` stays patchable in
   the facade.
5. Browser owner-thread rules, fail-closed timeout, the single action-wrapping
   loop, result shapes and all WebGL/Three.js/3D code outside `browser/` are
   unchanged. `web_check.py`/`visual_check.py` were deliberately untouched.
6. Test seams preserved or retargeted with the move:
   `karox.cli._registry` (facade), `karox.cli_runtime.OpenAIChatCompletionsProvider`,
   `karox.cli_runtime._mcp_registry`, `karox.cli_session.session_dir`,
   `karox.browser.session.{demote_headless_browsers,is_local_browser_url,MAX_ARTIFACT_BYTES}`
   (session hooks).

## Package budget

`MAX_PACKAGE_LINES = 90_000` (raised from 88,000 at `afc12e5` with a measured
justification in `scripts/check_tree.py`). Current 87,862 → 2,138 headroom for
the remaining decompositions. No duplicated/generated code was admitted.

## Cost ledger

No paid model/benchmark runs have been started (correctly still Phase 1).
Subagent work is internal.

## Next automatic actions

1. Read the `a8424d7` full-gate result; fix any regression before continuing.
2. Finish engine split: control/events → IO/durable state → finish/report →
   harness finishing; run full `tests/test_engine_kernel.py` per group.
3. Add bridge S0 characterization tests (modern MCP wire, sealed approval
   state, `execute_approved`); only then move bridge code.
4. Split `repo_context`, `tui/app`, `web_check`, providers, `engine_tool_env`,
   `core_tools`; delete the now-unwired v5 visual ledger only after a product
   decision.
5. Then Phase 2 full gate + wheel/fresh install, and only then benchmarks.

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

Do not push, publish, reset/clean foreign worktrees, alter the installed KaroX,
or start a public tunnel.
