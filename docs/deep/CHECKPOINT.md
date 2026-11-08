# Deep-refactor checkpoint

**Updated:** 2026-11-08 01:38 Europe/Warsaw
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
| Bridge tailscale | DONE | `bridge_tailscale.py` 1,585 → 225 (+ status 383 + gui 487 + funnel 653) |
| Bridge OAuth types | DONE | `bridge_oauth.py` 1,483 → 936 (+ types 368 + app 351); persistence seam kept |
| Bridge profile types | DONE | `bridge_profiles.py` 1,144 → 864 (+ `bridge_profile_types.py` 338); credential-store seam kept |
| Package budget | RAISED (measured) | ceiling 91,000 → 91,500; measured 90,849; rationale in `scripts/check_tree.py` |
| Bridge route closures | PARTIAL | OAuth pages/factory split; `build_proxy_asgi_app` closures still cohesive |
| Arena preparation | STARTED / FROZEN | `docs/deep/06_BENCHMARK_ARENA.md`; latest CLIs installed locally; registry/hashes/auth/readiness probes recorded; runner + batch harness; tasks in progress |
| Phase 2 freeze | DONE | full suite at `51be451`, frozen docs commit `4919a04`, wheel SHA-256 `815030a7…ba62f5`, fresh install ok |
| Readiness probes | DONE (not benchmarks) | Codex OK; Claude OK ($0.09196); Karo works on `wandb/DeepSeek-V4.1-Flash`; OpenCode works on free model; Gemini blocked; DSH harness pending |
| DEV task set | 80/80 VALIDATED | registry `732d3abd…8c054d`; A8/B8 add async queue, cache invalidation, facets, drag board, skeletal, JSON-RPC, print, CSRF/CORS, CSV→JSON, MCP subscriptions |
| DEV batch e9 (80 tasks) | PRELIMINARY | new ten: Karo **9/10**, Codex 0/10; combined Karo **72/80**, Codex 9/80, OpenCode 2/22 attempted |
| First pilot (n=1) | DONE, NOT A LEADERBOARD | all four arms solved `backend-catalog-pagination`; Karo 14.19 s, OpenCode 44.04 s, Claude 48.97 s, Codex 155.21 s, different models/providers |
| DEV batch e1 | CLOSED — INVALID | raw Karo 7/10 vs others 2/10, but prompts under-specified and browser dispatch invalid; not a comparison |
| DEV prompts e2 | REPAIRED / FROZEN | full acceptance contracts + autonomy line; registry `7d95e2cf…bcdd8`; 10/10 RED→GREEN |
| DEV task set | 20/20 VALIDATED | registry `ef63bcd5…33e461`; new A2/B2 batches RED→GREEN; 2 browser, 2 Three.js, 2 backend, 2 security, 2 migration, 2 MCP etc. |
| Benchmark arena | DEV EVIDENCE | no frozen-holdout winner claim; next: reach 100 tasks, then freeze/open holdout |

## Round-15 commits (latest first)

```
5127dfe refactor(bridge): extract OAuth approval pages and ASGI registration
c4b6999 docs(deep): checkpoint the OAuth and bridge-profile type splits
```

## Gates

| Gate | Result |
|---|---|
| Full suite on `ec32339` (isolated snapshot worktree) | **PASS** — exit 0, 100 % (`local-runs/gate-ec32339-full.log`) |
| ruff / mypy / check_tree on `ec32339` | PASS (check_tree 186 modules / 87,893 lines) |
| Full engine contract suite after the complete mixin split | PASS — kernel, tools, salvage, resilience, report, prompt-cache, probes, tool shapes |
| Tool bodies / render / layout / check / probe tests | PASS |
| check_report / probes / repo-safety / coverage / render tests after split | PASS |
| `ruff` + `check_tree` at current HEAD | PASS — 243 modules / 90,849 lines |
| Full suite at `5bff142` (core split) | **PASS** — exit 0, 100 % (`local-runs/gate-5bff142-full.log`) |
| Full suite at `9e8a367` (disk split + bridge S0 tests) | **PASS** — exit 0, 100 % (`local-runs/gate-9e8a367-full.log`) |
| Full suite at `b8e9789` (hosted-core split) | **PASS** — exit 0, 100 % (`local-runs/gate-b8e9789-full.log`) |
| Full suite at `13ba29d` (providers/engine/budget) | **PASS** — exit 0, 100 % (`local-runs/gate-13ba29d-full.log`) |
| Full suite at `c68e3a2` (TUI split) | **PASS** — exit 0, 100 % (`local-runs/gate-c68e3a2-full.log`) |
| Full suite at `1e968c2` (web_check split) | **PASS** — exit 0, 100 % (`local-runs/gate-1e968c2-full.log`) |
| Full suite at `95a8fc0` (bridge host split) | **PASS** — exit 0, 100 % (`local-runs/gate-95a8fc0-full.log`) |
| Full suite at `d26ace3` (bridge wire split) | **PASS** — exit 0, 100 % (`local-runs/gate-d26ace3-full.log`) |
| Full suite at `02eb453` (tailscale + budget) | **PASS** — exit 0, 100 % (`local-runs/gate-02eb453-full.log`) |
| Full suite at `c4b6999` (OAuth/profile splits) | **PASS** — exit 0, 100 % (`local-runs/gate-c4b6999-full.log`) |
| Full suite at `51be451` (OAuth app split) | **PASS** — exit 0, 100 % (`local-runs/gate-51be451-full.log`) |
| Frozen candidate wheel at `4919a04` | **PASS** — SHA-256 `815030a7964fad0c294df568113d0c65c161ddde1e271be81ac662f598ba62f5`, 264 files |
| Fresh-venv frozen install | **PASS** — version 5.0.0, doctor status ok, TUI/bridge imports |
| Wheel build | **PASS** — `karox_runtime-5.0.0-py3-none-any.whl`, 2 prompts, 10 builtin skills, `tui/theme.tcss`, entry point |
| Fresh-venv wheel install | **PASS** — import/`--version` = 5.0.0, `doctor --json` status ok, full command surface |

## Metrics

| Metric | Baseline | Current |
|---|---:|---:|
| `bridge_profiles.py` | 1,144 | 864 (+ `bridge_profile_types` 338) |
| `bridge_oauth.py` | 1,483 | 936 (+ types 368 + app 351) |
| Product modules | 164 | 243 |
| Physical lines | 87,689 | 90,849 (ceiling 91,500) |
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
| `bridge_tailscale.py` | 1,585 | 225 (+ status 383 + gui 487 + funnel 653) |

Remaining largest files: `bridge_launcher.py` (1,949), `bridge_proxy.py`
(1,707), `web_check.py` (1,118), `bridge_oauth.py` (936), `bridge_profiles.py`
(864), `sessions.py` (966), `engine_provider_bridge.py` (952).

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

1. Fix Karo's two real DEV failures from e3: browser selector bug and
   `feature-inbox-jsonl-import`; add regression coverage, then rerun only the
   affected DEV tasks and the full product gate.
2. Expand the task set from 20 toward 100 with public/issue-derived tasks; keep
   prompts self-contained and freeze a separate holdout with pre-registered
   rubric hashes.
3. Resolve or park Gemini auth, DSH headless and Claude's permission policy;
   keep unavailable lanes visibly labelled.
4. Do not cite e1/e2/e3 as a final leaderboard: different models, partial
   attempts and no frozen holdout.
5. Optionally split the cohesive `build_proxy_asgi_app` only with route-level
   characterization. Ceiling is 91,500 with 651 lines of measured headroom; a
   further raise needs fresh evidence and no behavior growth.

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
