# Pre-registration: FINAL blind comparison on pool6 — KaroX dev63 vs Claude Code (2026-11-25)

The user approved runs only after full checks ("сначала всё проверь"). pool6 (night ids 213-268, 56 tasks, frozen
2026-11-24) has never been run by any arm. The FAIR/PARTIAL/UNFAIR labels (bench/fair_pools.json, blind Neon audit,
calibrated on pools 3-5: UNFAIR solved 0/28 by both arms on pool3+4) were fixed BEFORE this run.

- Arms: karox6 = wheel 6.0.0.dev63 with `--provider anthropic --base-url http://127.0.0.1:14000` (shipping config,
  auto-prepended by the renderer); claude_code (same harness as the pool3+4 final). Upstream anthropic,
  claude-haiku-5-5, 1 attempt, all 56 tasks.
- Checks before spending: (1) free replay of the dev63 wheel in pool6 images (scenarios karox6_basics,
  karox6_code) green; (2) local render shows dev63 + native flags + CC arm; (3) paid preflight on 1 task outside
  pool6 (pool5 id 141) with both arms, checked by scripts/preflight_check.py: run_started provider=anthropic,
  version 6.0.0.dev63, run_finished completed, >=1 successful gateway call per arm, cost recorded per harness,
  karox6.jsonl usage events present. Any mismatch -> stop and fix, no final.
- PRIMARY (26 FAIR tasks): dev63 PASSES iff solves(dev63) > solves(CC) strictly AND cost/solve(dev63) <=
  0.85 x cost/solve(CC) (costs over the FAIR tasks from the gateway audit: uncached in $0.10/M, cache read
  $0.01/M, out $0.50/M, interview excluded). Pairs without a verifier reward are removed; >= 24 valid FAIR pairs
  required, else no verdict.
- SECONDARY (reported, not decisive): all 56 tasks, PARTIAL and UNFAIR separately; paired bootstrap CI.
- Hard cap $3.50 including preflight; stopped by the cap = no verdict. Expected ~$2.5.
- The result does not tune anything on pool6; pool6 trajectories stay unread except for infra failures.
