# Pre-registration: dev55 speed screen (mine split)

Written 2026-11-23 before the run. Change: Verify/Efficiency prompt wording only (targeted checks, a full suite
once at the end, wait for jobs with job_output instead of sleep, baseline via tests/git stash instead of repo
copies). Motivation: mine-split wall-time breakdown (hold49 mine tasks, native54): KaroX spends 63-70% of wall in
tools, mostly repeated broad test runs, vs Claude Code.
Tasks: the 8 mine tasks of native54 (trials 16,28,31,32,33,35,53,71). Arms: dev54 vs dev55, both
`--provider anthropic` via the same gateway, Haiku 5.5, anthropic lane, 1 attempt, parallel 4.
ADOPT dev55 if: solved >= dev54's in this run, AND median agent wall per task <= 0.75x dev54, AND $ (audit prices
as in prereg_final_frozen.md) <= 1.0x dev54. Otherwise not adopted. No re-runs after seeing results.
