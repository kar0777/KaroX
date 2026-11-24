# Pre-registration: FINAL blind comparison on pool3+pool4 — KaroX dev61 vs Claude Code (2026-11-24)

Council 15 (Codex gpt-6.2-sol + Gemini 4 Neon), both chose (b). Stated openly: dev61 FAILED its own pre-registered
cost cap on pool5 (1.48x > 1.20x vs dev56); it is sent to the final under the single objective adopted by the strategy
council BEFORE that result was read: more solves at cost/solve <= 0.85x Claude Code. This uses the last allowed
comparison on pool3+pool4 (pool3 comparison 1 = pool3a).

- Arms: karox6 = wheel 6.0.0.dev61 with `--provider anthropic --base-url http://127.0.0.1:14000` (shipping config);
  claude_code (same harness as pool3a). Upstream anthropic, claude-haiku-5-5. Tasks: night ids 74-140 (67). 1 attempt.
- Preflight first: 1 task outside the final set (pool5 id 141) per arm; assert run_started provider=anthropic,
  version 6.0.0.dev61, >=1 successful gateway call per arm. Any mismatch -> stop.
- KaroX dev61 PASSES iff, on valid pairs: solves(dev61) > solves(CC) (strictly) AND cost/solve(dev61) <=
  0.85 x cost/solve(CC), costs from the gateway audit (uncached in $0.10/M, cache read $0.01/M, out $0.50/M,
  interview excluded). Infra failure (no verifier reward) removes the pair; >= 65 valid pairs required, else no verdict.
- Hard cap $3.00 including preflight; stopped by the cap = no verdict. Expected ~$2.5.
- Strictest of the two council answers taken (Codex: strict >, $3 cap; Neon: >=, $6 cap).
