# Pre-registration: dev69 screen vs dev65 on dev pools (2026-11-27)

dev69 = dev68 (silent-command note; bash description: wait with job_output wait: true, `sleep N` always waits N s,
nohup/& is not a job) + bash description: do not pipe output through tail/head merely to shorten it; search or
read a range of the saved log for omitted lines instead of rerunning + shell/job_output inline caps 30k -> 12k chars
(the full log is saved). Sources: dev-pool timing (430 trials: tests 38%, 600 s timeouts 17%, sleep 11% of wall),
Devin pair analyses (KaroX reruns whole suites to recover FAILED names cut by tail), Codex council r3 (option 2),
Devin review r7 (rewording, 12k caps against the cost risk). No system-prompt change.

- Tasks: the 62 scr66 tasks (every FAIR task of pool3+pool4+pool5 except [73, 83, 87, 93, 113, 137, 149, 166, 168]).
  Dev pool, not a holdout. Runs after the pool9 runs (GitHub concurrency).
- Arms side by side in one job: karox6 = dev65, karox6b = dev69; Haiku 5.5, anthropic lane, 2 attempts.
- Checks before the run: free replay of the final dev69 wheel green.
- PRIMARY (speed screen): geometric-mean ratio of per-task mean wall dev69/dev65 over tasks with rewards for both
  arms, paired bootstrap over tasks (10 000, seed 1). GO to a fresh-pool confirmation iff ratio <= 0.90 AND its 95%
  CI upper bound < 1.0 AND solved attempts dev69 >= dev65 - 3 (of 124) AND cost/solve dev69 <= 1.05x dev65.
  Otherwise not adopted.
- SECONDARY: mean/p90 wall, test-command reruns, inline output tokens, cost per trial.
- Hard cap $6. Pairs without a verifier reward are dropped pairwise. If pool9 adopts dev68, the decision on dev69
  is additionally read against dev68's pool9 effect (descriptive only).
