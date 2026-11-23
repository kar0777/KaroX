# Pre-registration: pool3 comparison 1 of 2 (fresh blind pool)

Written 2026-11-23 before the run. Pool: bench/pool3_frozen.json (43 tasks, night ids 74-116, never run by any
agent before). Arms: KaroX dev56 (karox6/main; native Anthropic provider through the gateway) vs Claude Code
2.1.329, Claude Haiku 5.5, anthropic lane, 1 attempt each, parallel 6.
$ from the gateway audit: uncached input $0.10/M, cache read $0.01/M, output $0.50/M, interview calls excluded.
Same rule as bench/prereg_final_frozen.md: KaroX wins if solved >= Claude Code's AND $ <= 0.85x AND median agent
wall per task <= 1.0x; Claude Code wins if it solves more or KaroX $ >= 1.0x; otherwise inconclusive. Also report
the paired solve table and a one-sided sign test on discordant tasks. Trajectories of pool3 are never read.
No re-runs; infrastructure-lost tasks reported.
