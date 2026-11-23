# Pre-registration: dev57 bounded waits (mine split)

Written 2026-11-23 before the run. dev57 bounds blocking per process: foreground max 120 s (then the command keeps
running as a job), job_output max 90 s per call, a 300 s cumulative blocking budget per process after which
job_output returns at once with status/age/tail; "no output for N s" hint; elapsed time in the exit marker.
Tasks (mine, chosen by rule from the wait30 baseline arm: the 5 mine tasks with the most tool seconds plus the
fastest solved one): night ids 29,32,42,41,33,28. Arms: dev56 vs dev57, native provider, Haiku 5.5, anthropic
lane, 2 attempts, parallel 3.
ADOPT dev57 if: summed tool seconds over all trials B <= 0.75x A, AND solved attempts B >= A, AND $ (audit prices
of prereg_final_frozen.md) <= 1.05x A, AND paired geo-mean agent wall ratio <= 0.95. Otherwise not adopted.
No re-runs; infrastructure-lost trials reported.
