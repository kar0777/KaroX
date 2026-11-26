# Pre-registration: pool9 — dev65 vs dev68 vs Claude Code (leadership confirmation + waiting-tool screen)

Written 2026-11-26 before any pool9 agent run. Council: Codex gpt-6.2-sol (night plan + dev68 review),
Devin SWE-2.1 (loss/pair analyses on dev pools only).

**Pool.** pool9 = every RED/GREEN-valid candidate of collect_fresh windows 2026-08-10..09-10 (part a) and
2026-07-10..08-10 (part b); ids 370-493 (124 tasks) in `pool9_frozen.json` (bench commit 0dffb1b), frozen before the run. FAIR/PARTIAL/UNFAIR labels come from the
same two blind Codex auditors (gpt-6.2-sol, gpt-6.1-sol) and the same merge rule as pool7/pool8, and are fixed in
`fair_pools.json` before the run: 55 FAIR / 29 PARTIAL / 39 UNFAIR / 1 DISPUTED. pool9 trajectories are never read except for infra failures.

**Arms (side by side per task, one job).** karox6 = 6.0.0.dev65 (shipping, unchanged), karox6b = 6.0.0.dev68
(dev65 + waiting-tool changes only: silent-command note, bash description on job_output wait / sleep / nohup;
branch steps/dev68 55dfa5f), claude_code. Haiku 5.5, anthropic lane, 2 attempts, parallel as in pool8.
Interview calls excluded from cost. Cost at audit prices of prereg_final_frozen.md.

**Decisions — on pool9 alone; historical pools are descriptive only.** Unit = task; per task the solve rate over
its 2 attempts. Paired bootstrap over FAIR tasks (10 000 resamples, seed 1).
- **A1 (leading on solves, dev65 vs CC):** lower bound of the 95% CI of mean(solve dev65 − solve CC) > 0.
- **A2 (cheaper, dev65 vs CC):** upper bound of the 95% CI of the cost/solve ratio dev65/CC < 0.85.
- **B (adopt dev68 over dev65):** geometric-mean wall ratio dev68/dev65 (per-task mean wall, all valid tasks)
  ≤ 0.90 AND its 95% CI upper bound < 1.0 AND lower bound of the FAIR solve-difference CI (dev68 − dev65) > −0.08
  AND cost/solve dev68 ≤ 1.05× dev65. Otherwise dev65 stays.
- Secondary (reported, no decision): all/PARTIAL/UNFAIR solves, p50/p90 wall, dev68 vs CC, total spend incl.
  interview, how often the silence note fired.

**Rules.** Spend cap $16 (estimate ~$12). No re-runs of finished trials; a task lacking a verifier reward for any arm
(infra) is dropped for all arms and reported. Evaluation script: `karox6-bench-data/scripts/eval_pool9.py` (written and dry-run on pool8 data before the
run). Arm versions are checked on the first finished job.
