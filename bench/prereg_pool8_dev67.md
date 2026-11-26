# Pre-registration: pool8 — dev67 vs dev65 vs Claude Code (2026-11-26)

Written before any pool8 run. pool8 = 49 fresh tasks (night ids 321-369, merged 2026-09-10..10-01, frozen in
bench/pool8_frozen.json); labels fixed before the run (bench/fair_pools.json "pool8": 24 FAIR / 12 PARTIAL /
12 UNFAIR / 1 DISPUTED; two blind Codex auditors, same rule as pool7). No pool8 trajectory was read by anyone.

dev67 = dev66 (screen: +5 solved attempts on 62 dev FAIR tasks, 1.12x total cost, NO-GO by its total-cost cap)
+ Devin SWE-2.1 review fixes (silent-command note only for end-only filters, no kill advice; state tracing only
for stateful/ordered changes; an unverifiable case is reported, not looped).

- Arms, side by side in one job per task: karox6 = wheel dev65 (shipping), karox6b = wheel dev67, claude_code.
  claude-haiku-5-5, anthropic lane, native flags, 2 attempts each, all 49 tasks.
- Checks before the run: free replay of dev67 (karox6_basics + karox6_code) green; the 3-arm config was
  exercised by the scr66 run (dev65/dev66 karox arms) and the p7d65 run (claude_code arm) today.
- PRIMARY (24 FAIR tasks; per task solved attempts 0-2; costs from the gateway audit at uncached $0.10/M,
  cache read $0.01/M, out $0.50/M, interview excluded; tasks lacking any verifier reward dropped for all arms):
  ADOPT dev67 as the new main iff solves(dev67) - solves(dev65) >= +3 AND cost/solve(dev67) <= 1.05 x
  cost/solve(dev65). Otherwise dev65 stays.
- SECONDARY "number one" check (reported, decides nothing): dev67 vs Claude Code with the pool6/pool7 rule
  (solves strictly greater AND cost/solve <= 0.85x). Also all 49 tasks, per class, wall time.
- Hard cap $8 including interview; expected ~$6.
