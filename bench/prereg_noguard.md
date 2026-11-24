# Pre-registration: progress guard A/B on pool5 (2026-11-24)

Council 12 (Codex gpt-6.2-sol + Gemini 4 Neon): option A.
- Arms (same job per task, side by side): karox6 = wheel 6.0.0.dev56 (control); karox6b = same wheel with
  `--no-progress-guard`. Upstream anthropic, claude-haiku-5-5. All 72 pool5 tasks (night ids 141-212), 1 attempt.
- Expected $1.80-2.00; hard cap +$2.50. Fewer than 72 completed pairs because of the cap = inconclusive.
- Net gain = solves only without guard - solves only with guard, over all completed pairs.
  * net >= +3 and cost(no-guard) <= 1.25x control  -> no-guard goes to the single final comparison vs Claude Code
    on pool3+pool4 (67).
  * net >= +3 and cost > 1.25x -> build the guard fix (dev61: bash commands count as progress) and test it on pool5.
  * net < +3 -> keep dev56.
- Cost from gateway audit at the stated prices, interview excluded. Infra failures (no verifier reward) excluded
  pairwise, not re-run.
