# Pre-registration: blind comparison on pool7 — KaroX dev65 vs Claude Code (2026-11-26)

User approval 2026-11-26 ("Да делай"): fresh tasks, two-auditor fairness audit, ~$1 preflight, ~$4 main run.
pool7 (night ids 269-320, 52 tasks, bench commit 6494cf6, frozen in bench/pool7_frozen.json) has never been run by
any arm. Its FAIR/PARTIAL/UNFAIR labels (bench/fair_pools.json "pool7": 27/9/14 + 2 DISPUTED, two blind Codex
auditors gpt-6.2-sol and gpt-6.1-sol, FAIR only if both say FAIR) were fixed BEFORE this run.

- Arms: karox6 = wheel 6.0.0.dev65 with the shipping native flags (auto-prepended by the renderer); claude_code
  (same harness as the pool6 final). Upstream anthropic, claude-haiku-5-5, 1 attempt, all 52 tasks, paired arms.
- Checks before spending: (1) free replay of the dev65 wheel in 3 pool7 images (scenarios karox6_basics,
  karox6_code) green; (2) paid preflight on 1 task outside pool7 (pool5 id 141) with both arms, checked by
  karox6-bench-data/preflight_check.py (provider=anthropic, version 6.0.0.dev65, completed run, >=1 successful
  gateway call per arm, cost per harness, usage events). Any mismatch -> stop and fix, no main run.
- PRIMARY (27 FAIR tasks): dev65 PASSES iff solves(dev65) > solves(CC) strictly AND cost/solve(dev65) <=
  0.85 x cost/solve(CC) (gateway audit costs over FAIR tasks: uncached $0.10/M, cache read $0.01/M, out $0.50/M,
  interview excluded). Pairs without a verifier reward are removed; >= 25 valid FAIR pairs required, else no verdict.
- SECONDARY (reported, not decisive): all 52 tasks; PARTIAL, UNFAIR, DISPUTED separately; paired bootstrap CI;
  wall time per trial.
- Hard cap $4.00 including preflight; stopped by the cap = no verdict. Expected ~$2.1 (pool6: 56 tasks $2.11).
- Nothing is tuned on pool7; pool7 trajectories stay unread except for infra failures.
