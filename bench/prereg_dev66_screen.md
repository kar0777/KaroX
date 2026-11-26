# Pre-registration: dev66 screen vs dev65 (2026-11-26)

dev66 = dev65 + system-prompt edits from 9 dev-pool losses to Claude Code (trace producers/copies/consumers of the
changed state and the step order of equivalent code; unexpected probe errors leave a case unverified; the fixed
operation combined with another on the same state; remove incomplete artifacts after a failed setup) + stall
notice without "minimal change" + advisory notes for silent running commands (no caps). Root causes: Devin
SWE-2.1 per-case reports + Codex/Devin synthesis (karox6-bench-data/work/devin).

- Tasks: every FAIR task of pool3+pool4+pool5 except the 9 analysed losses ([73, 83, 87, 93, 113, 137, 149, 166, 168]): 62 tasks.
  A dev pool (run by dev56-dev61, not a holdout); the dev66 edits come only from the 9 excluded tasks.
- Arms, side by side in one job: karox6 = wheel dev65, karox6b = wheel dev66; both native Anthropic flags,
  claude-haiku-5-5, anthropic lane, 2 attempts.
- Checks before the run: free replay of dev66 (karox6_basics + karox6_code) green - done 2026-11-26.
- PRIMARY: per task solve score = solved attempts (0-2). Net = sum(dev66) - sum(dev65).
  GO to a confirmation run (fresh pool vs Claude Code, separately pre-registered) iff net >= +4 AND
  cost(dev66) <= 1.10 x cost(dev65) (gateway audit prices, interview excluded). Otherwise dev66 is not adopted
  (the silent-command notes may be screened alone later).
- SECONDARY: wall time per trial (mean, p90), paired sign test on discordant tasks.
- Hard cap $4.5; stopped by the cap = no verdict. Pairs without a verifier reward are dropped pairwise.
