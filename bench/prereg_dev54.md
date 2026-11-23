# Pre-registration: dev54 (progress guard only, finish gate off, redirection fix) — cost lever

Written 2026-11-23 before any dev54 run. Council consensus (Codex 6.2-sol, Fusion, Neon): stop chasing solve rate on
this pool; make cost/speed the edge. Lane: Claude Haiku 5.5, anthropic, parallel 4 (avoid org concurrency limits).

Run: dev54, 1 attempt, on 8 mine tasks: 16, 33, 35, 53 (largest token sinks in hold49) + 28, 31, 32, 71
(solved 4/4 by dev42 in mine53). Baseline: the existing dev42 x4 attempts on the same tasks in mine53 (no new dev42 run).
Adopt dev54 as the new base if: none of 28, 31, 32, 71 fails, AND dev54 total output tokens on the 8 tasks
<= 0.75 x dev42's mean-per-attempt total on them. Known limits: n=1 only catches large regressions; a later
fresh pool will re-check.
