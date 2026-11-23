# Pre-registration: foreground wait cap 30 s (tool-level speed screen, mine split)

Written 2026-11-23 before the run. Council (Codex gpt-6.2-sol, Gemini 4 Neon) ranked the existing
`--command-wait-cap` flag first: a foreground command still running after 30 s becomes a background job, the model
gets its tail and can keep working or wait with job_output. No code change; both arms run the dev56 wheel.
Tasks (mine, fixed by rule before the run): every mine task solved at least once in mine53 except aallan-vera-1760
(3434 s, would break the 1 h budget): night ids 21,28,31,32,39,41,46,71; plus the 4 unsolved mine tasks with the
longest mine53 median wall except those above 1000 s already included: 29,42,16,33. 12 tasks x 2 attempts x 2
arms, Haiku 5.5, anthropic lane, native provider, parallel 3 jobs.
Primary metric: KaroX tool seconds per trial (sum of tool event durations, incl. job_output waits), averaged over
the 2 attempts per task; paired geometric-mean ratio B/A over tasks with a 95% bootstrap CI over tasks.
ADOPT wait cap 30 if: ratio <= 0.80 AND CI upper bound < 1.0 AND paired agent wall ratio <= 0.90 AND solved
attempts B >= A - 1 (of 24) AND $ (audit prices of prereg_final_frozen.md) <= 1.02x. Otherwise not adopted.
No re-runs; infrastructure-lost trials are reported.
