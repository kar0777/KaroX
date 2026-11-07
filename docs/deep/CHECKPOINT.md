# Deep-refactor checkpoint

**Updated:** 2026-11-08 00:48 Europe/Warsaw
**Worktree:** `D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\karox5-deep`
**Branch:** `refactor/karo5-deep-competition-20261107`
**Base:** `release/karox5-5.0.0` @ `066a35134ff5f288413c8ff3d47068706fc8ae8a`
**Goal:** `b/win/docs/goals/KAROX_V5_DEEP_REFACTOR_AND_BENCHMARK_GOAL.md`

## Status

Phase 0 complete. Phase 1A (dead legacy) has two evidenced batches. Phase 1B
continues: CLI, browser session and the whole engine kernel are decomposed;
check_report is split, and the tools/core/MCP layers are decomposed. Full-suite
gates through `d27e2d2` passed; the MCP split (`4c4071d`) has targeted MCP and
engine-tool evidence and needs the next full gate.

| Workstream | State | Evidence |
|---|---|---|
| Baseline freeze | DONE | `docs/deep/01_BASELINE.md` |
| Dead-code batch 0/1 | DONE | verification API + 193 audited helper lines; checkpoint module kept |
| CLI split | DONE | `cli.py` 2,299 → 193 + 8 modules |
| Engine split | DONE | `engine_kernel.py` 2,905 → 753 + 8 owner modules |
| Browser session split | DONE | `browser/session.py` 3,703 → 173 + 11 modules |
| Tools batch | DONE | `engine_tool_env` 1,864 → 1,396 + bodies 544; `core_tools` 1,802 → 780 + 4 mixins |
| Verification batch | DONE (reports/gate) | `check_report` 1,293 → 295 (+parsers/targets); `completion_gate` 1,278 → 565 (+rules/visual/review) |
| Context batch | DONE (repo) | `repo_context` 2,055 → 148 (+support/identity/search/inspect) |
| Core runtime | DONE | `core.py` 1,573 → 453 (+execute/validation/files/support) |
| Core tools | DONE | `core_tools.py` 1,802 → 780 (+skill/job/edit/read mixins) |
| Process tree | DONE | `core_process.py` 1,126 → 836 (+ `process_tree.py` 344) |
| MCP client | DONE | `mcp_client.py` 1,201 → 588 (+ types 539 + registry 196 + transport 95) |
| Disk maintenance | DONE | `disk_maintenance.py` 1,099 → 217 (+ protection 196 + scan 446 + plans 448) |
| Bridge S0 tests | DONE | `tests/test_bridge_characterization.py` — 21 tests, 54 with existing bridge suites |
| Hosted core (first slice) | DONE | `hosted_core.py` 1,162 → 821 (+ contract 129 + health 136 + approval 258) |
| Providers | DONE | `providers.py` 1,638 → 635 (+ types 652 + stream 525) |
| Provider adapters | DONE | `provider_adapters.py` 1,503 → 86 (+ base 496 + OpenAI 305 + Anthropic 499 + Gemini 283) |
| Engine tool env | DONE | `engine_tool_env.py` 1,396 → 582 (+ finish 366 + guidance 267 + attach 256 + support 241) |
| TUI | DONE | `tui/app.py` 2,038 → 569 (+ support/session/commands/run); `tui/widgets.py` 1,141 → 910 (+ render 314) |
| web_check (safe slices) | DONE | `web_check.py` 1,831 → 1,118 (+ scripts 315 + document 513); browser seams/thresholds kept |
| Bridge launcher host | DONE | `bridge_launcher.py` 2,389 → 1,949 (+ `bridge_host.py` 547); `_wait_for_bridge` seam kept |
| Bridge proxy wire | DONE | `bridge_proxy.py` 2,218 → 1,707 (+ `bridge_wire.py` 632); approval/time seam kept |
| Package budget | RAISED (measured) | ceiling 90,000 → 91,000; measured 90,491; rationale in `scripts/check_tree.py` |
| Bridge tailscale/profiles/oauth | NOT STARTED | next batches |
| Benchmark arena | NOT STARTED (correctly blocked) | — |

## Round-12 commits (latest first)

```
9403245 refactor(bridge): extract wire/security helpers from bridge_proxy
95a8fc0 docs(deep): checkpoint the bridge host-primitive split
```

## Gates

| Gate | Result |
|---|---|
| Full suite on `ec32339` (isolated snapshot worktree) | **PASS** — exit 0, 100 % (`local-runs/gate-ec32339-full.log`) |
| ruff / mypy / check_tree on `ec32339` | PASS (check_tree 186 modules / 87,893 lines) |
| Full engine contract suite after the complete mixin split | PASS — kernel, tools, salvage, resilience, report, prompt-cache, probes, tool shapes |
| Tool bodies / render / layout / check / probe tests | PASS |
| check_report / probes / repo-safety / coverage / render tests after split | PASS |
| `ruff` + `check_tree` at current HEAD | PASS — 237 modules / 90,491 lines |
| Full suite at `5bff142` (core split) | **PASS** — exit 0, 100 % (`local-runs/gate-5bff142-full.log`) |
| Full suite at `9e8a367` (disk split + bridge S0 tests) | **PASS** — exit 0, 100 % (`local-runs/gate-9e8a367-full.log`) |
| Full suite at `b8e9789` (hosted-core split) | **PASS** — exit 0, 100 % (`local-runs/gate-b8e9789-full.log`) |
| Full suite at `13ba29d` (providers/engine/budget) | **PASS** — exit 0, 100 % (`local-runs/gate-13ba29d-full.log`) |
| Full suite at `c68e3a2` (TUI split) | **PASS** — exit 0, 100 % (`local-runs/gate-c68e3a2-full.log`) |
| Full suite at `1e968c2` (web_check split) | **PASS** — exit 0, 100 % (`local-runs/gate-1e968c2-full.log`) |
| Full suite at `95a8fc0` (bridge host split) | **PASS** — exit 0, 100 % (`local-runs/gate-95a8fc0-full.log`) |
| Full suite at current HEAD (bridge wire split) | pending next round; bridge suites pass |
| Wheel build | **PASS** — `karox_runtime-5.0.0-py3-none-any.whl`, 256 files, 2 prompts, 10 builtin skills, `tui/theme.tcss`, entry point |
| Fresh-venv wheel install | **PASS** — import/`--version` = 5.0.0, `doctor --json` status ok, full command surface |

## Metrics

| Metric | Baseline | Current |
|---|---:|---:|
| Product modules | 164 | 237 |
| Physical lines | 87,689 | 90,491 (ceiling 91,000) |
| `cli.py` | 2,299 | 193 |
| `browser/session.py` | 3,703 | 173 |
| `engine_kernel.py` | 2,905 | 753 |
| `engine_tool_env.py` | 1,864 | 582 (+ finish 366 + guidance 267 + attach 256 + support 241; `engine_tool_bodies` 544) |
| `check_report.py` | 1,293 | 295 (+ parsers 829 + targets 271) |
| `completion_gate.py` | 1,278 | 565 (+ rules 360 + visual 317 + review 199) |
| `repo_context.py` | 2,055 | 148 (+ support 407 + identity 628 + search 736 + inspect 394) |
| `core.py` | 1,573 | 453 (+ execute 336 + validation 508 + files 425 + support 99) |
| `core_tools.py` | 1,802 | 780 (+ skill 202 + job 267 + edit 513 + read 228) |
| `core_process.py` | 1,126 | 836 (+ process_tree 344) |
| `mcp_client.py` | 1,201 | 588 (+ types 539 + registry 196 + transport 95) |
| `disk_maintenance.py` | 1,099 | 217 (+ protection 196 + scan 446 + plans 448) |
| `hosted_core.py` | 1,162 | 821 (+ contract 129 + health 136 + approval 258) |
| `providers.py` | 1,638 | 635 (+ types 652 + stream 525) |
| `provider_adapters.py` | 1,503 | 86 (+ base 496 + OpenAI 305 + Anthropic 499 + Gemini 283) |
| `tui/app.py` | 2,038 | 569 (+ support 175 + session 258 + commands 825 + run 636) |
| `tui/widgets.py` | 1,141 | 910 (+ render 314) |
| `web_check.py` | 1,831 | 1,118 (+ scripts 315 + document 513) |
| `bridge_launcher.py` | 2,389 | 1,949 (+ `bridge_host` 547) |
| `bridge_proxy.py` | 2,218 | 1,707 (+ `bridge_wire` 632) |

Remaining largest files: `bridge_launcher.py` (1,949), `bridge_proxy.py`
(1,707), `bridge_tailscale.py` (1,585), `bridge_oauth.py` (1,483),
`bridge_profiles.py` (1,144), `web_check.py` (1,118), `sessions.py` (966),
`engine_provider_bridge.py` (952).

## Decisions that must not be lost

1. `ellipsis_checkpoint.py` stays (latent hosted rollback capability); do not
   delete it as dead.
2. `route_health` trackers/probes, `requirements.delegated_source_context` and
   effort/mode display helpers are lost wiring, not dead code.
3. Dynamic entrypoints: `command_jobs.run_job`, `workspace_worker`,
   `browser.tools`, `tui.app`, `desktop_apps`, `engine/*`,
   `computer_use.__main__`.
4. `EngineAgent`/`EngineKernel` concrete classes stay in the façade;
   `_any_language_server` stays in the façade; `_ENGINE_IDENTITY` is
   re-exported from `engine_identity.py`.
5. Browser owner-thread/fail-closed/action-wrapping/result shapes and all
   WebGL/Three.js/3D code outside `browser/` are unchanged.
6. Patch seams preserved or retargeted with moves: `karox.cli._registry`,
   `karox.cli_runtime.{OpenAIChatCompletionsProvider,_mcp_registry}`,
   `karox.cli_session.session_dir`,
   `karox.browser.session.{demote_headless_browsers,is_local_browser_url,MAX_ARTIFACT_BYTES}`.
7. `Failure` and moved helper classes keep their decorators; moving a class
   must include `decorator_list` lines (two near-misses already fixed).

## Cost ledger

No paid model/benchmark runs started (correctly Phase 1). Internal subagents
only.

## Next automatic actions

1. Run the full gate at current HEAD; fix any regression.
2. `bridge_tailscale`, `bridge_profiles`, `bridge_oauth` splits; keep their
   existing patch seams.
3. Add route-level characterization before splitting
   `build_proxy_asgi_app`; keep the core-vs-wire one-shot boundary documented.
4. Phase 2 full gate + fresh wheel install; only then benchmark arena. Ceiling
   is 91,000 with 509 lines of measured headroom; a further raise needs fresh
   evidence and no behavior growth.

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

Do not push/publish, reset foreign worktrees, alter the installed KaroX, or
start a public tunnel.
