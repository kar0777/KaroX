# Pre-registration: pool10 — dev68 (shipping) vs dev69 vs Claude Code

Written 2026-11-27 before pool10 exists (collection of its windows has just started). Follows the dev69 screen GO
(bench/PROGRESS.md 2026-11-27) and the pool9 adoption of dev68.

**Pool.** pool10 = every RED/GREEN-valid candidate of collect_fresh windows 2026-06-10..07-10 and 2026-05-10..06-10
(earlier pools excluded), ids frozen in `pool10_frozen.json` before the run; FAIR labels by the same two blind Codex
auditors and merge rule as pool7-9, fixed in `fair_pools.json` before the run. Trajectories never read except for
infra failures.

**Arms (side by side per task).** karox6 = 6.0.0.dev68 (shipping), karox6b = 6.0.0.dev69 (steps/dev69: dev68 +
"do not pipe output through tail/head merely to shorten it; search the saved log" + shell/job_output inline caps
12k chars), claude_code. Haiku 5.5, anthropic lane, 2 attempts. Cost at audit prices, interview excluded.

**Decisions (pool10 alone; same estimators as prereg_pool9.md: per-task mean over attempts, paired bootstrap over
tasks, 10 000 resamples, seed 1).**
- **C (adopt dev69 over dev68):** ADOPT iff lower 95% CI bound of the FAIR solve difference (dev69 − dev68) > −0.08
  AND geometric wall ratio dev69/dev68 ≤ 1.00 AND cost/solve ratio ≤ 1.00 AND (wall-ratio CI upper < 1.0 OR
  cost/solve-ratio CI upper < 1.0). Otherwise dev68 stays.
- **A1 / A2 (shipping dev68 vs CC):** A1 leading on solves iff the lower CI bound of the FAIR solve difference > 0;
  A2 cheaper iff the upper CI bound of the cost/solve ratio < 0.85.
- Secondary (no decision): the same for dev69 vs CC, all/PARTIAL/UNFAIR solves, p50/p90 wall, pooled pool9+pool10
  dev-vs-CC numbers (descriptive).

**Rival run R2 (amended 2026-11-27 before pool10 exists).** A second run on the same pool10 ids: karox6 = dev68 +
opencode_v2 (OpenCode V2 2.3.2, same gateway), 2 attempts, so both arms share the same per-runner load (4 concurrent
trials). Decisions on R2 alone, FAIR tasks, same estimators: **O1** dev68 leads OpenCode on solves iff the lower CI
bound of the solve difference > 0; **O2** cheaper iff the upper CI bound of the cost/solve ratio < 0.85; **O3**
faster iff the upper CI bound of the geometric wall ratio dev68/OC < 1.0. (pool9 showed per-runner load confounds
wall time across runs.)

**Rules.** Cap $16 for the main run, $8 for R2. No re-runs; a task lacking a verifier reward for any arm is dropped for all arms and reported.
Artifacts that `gh run download` skips are fetched individually before evaluation. Arm versions checked on the first
finished job. Evaluation script written and dry-run before the run.
