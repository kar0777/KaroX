# Devin report (branch `devin/big`, bench branch `devin/bench`)

Status: IN PROGRESS. Stages are added as they finish; details and every run's
pre-registration and result are in `PROGRESS.md` ("Devin iteration").

## Stage 1 (2026-11-21 18:10-18:40 UTC): setup, analysis of saved runs, dev35

- Worktrees `D:\проекты\karox6-wt-devin` (devin/big) and `D:\проекты\bench-wt-devin`
  (devin/bench); own venv with pyte, pywinpty, build (commit e03012f).
- Analysis of saved runs, no new runs (PROGRESS.md 18:30; scripts in the bench repo
  `scripts/analysis/`): the "1.6x reasoning" is replies cut while thinking on Dahl's
  8192-token cap; on long tasks KaroX's cost is context (compaction at 320k vs OpenCode's
  ~171k), and every recorded compaction dropped the provider's prompt cache.
- 6.0.0.dev35 (commit d5d3000): cache-aware compaction, thinking-only cut notice and
  effort easing, Retry-After as a floor, daily allowance = quota, empty reply resent once,
  stalled stream = timeout. 46 new tests (tests/test_retry.py, tests/test_session_limits.py).
- Bench (60fd856): dev35 wheel, `trio_report.py` with agent time and cost-equivalent, a
  scoring fix (the "waited and timed out" clause never fired for KaroX).
- Runs pre-registered: devd1 (Dahl, mechanism), devs1 (ReformBoss smoke), devt1/devt2.
