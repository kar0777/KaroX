# Pre-registration: final comparison on frozen (candidate 1 of max 2)

Written 2026-11-23 before the run. Frozen = 24 tasks from bench/night_holdout_split.json (never read for tuning).
Arms: KaroX dev54 with the native Anthropic provider (`--provider anthropic`, through the same LiteLLM gateway) vs
Claude Code 2.1.329. Model Claude Haiku 5.5, anthropic lane, 1 attempt each, parallel 6.
Cost in $ from gateway audit: uncached input $0.10/M, cache read $0.01/M, output $0.50/M (cache writes counted as
uncached input; prompts above 100k tokens are rare and ignored), interview calls excluded.
KaroX wins on this pool if: solved tasks >= Claude Code's, AND total $ <= 0.85 x Claude Code's, AND median
agent wall time per task <= 1.0 x Claude Code's. Claude Code wins if it solves more tasks or KaroX $ >= 1.0x.
Otherwise inconclusive. No re-runs after seeing results; tasks lost to infrastructure errors are reported, not rerun.
