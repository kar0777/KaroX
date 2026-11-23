# Pre-registration: dev53 (finish gate + progress guard) vs dev42

Written 2026-11-23, before any dev53 run. Lane: Claude Haiku 5.5 (anthropic or tokenharbor, same arms on the same lane).

Stage 1, mine (25 tasks from bench/night_holdout_split.json), dev42 x4 vs dev53 x4 attempts per task:
- Per-task solve rate (k/4). Statistic: sum over tasks of (dev53 - dev42) solve rate, i.e. a delta in expected solved tasks.
- Paired sign-flip permutation test over tasks (one-sided).
- GO to frozen only if all hold: delta >= +1.0 task, permutation p < 0.10, no task dev42 solved >= 3/4 falls below 3/4 for dev53, total output tokens <= 1.10x dev42.
- The dev42 replicates also give an A/A noise estimate.

Stage 2, frozen (24 tasks), run once, dev42 x5 vs dev53 x5 (+ Claude Code x5 for the headline):
- Ship dev53 if delta > 0 and one-sided permutation p < 0.10 and output tokens not worse than 1.10x.
- At most 2 candidates may ever be evaluated on frozen. Frozen trajectories are never read for tuning.

Flags may be ablated (finish_gate only / progress_guard only) on mine; the frozen run uses one fixed build.
Known caveat: the gate text was designed after reading mine failures (hindsight); it stays generic (no task examples).
