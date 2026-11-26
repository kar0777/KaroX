# Pre-registration: pool9 extension — dev65 vs OpenCode on the same model

Written 2026-11-27 after the pool9 main run started (run 45909555070) and before any pool9 result was looked at.
Council (Codex gpt-6.2-sol, night plan) asked for more rivals on the same Haiku than Claude Code alone.

**Rival.** OpenCode V2 CLI 2.3.2 (`opencode_v2` arm, `opencode run --standalone --auto --thinking`), Haiku 5.5
through the same LiteLLM anthropic gateway and cache-injection points as the KaroX arms. Same pool9 ids 370-493,
2 attempts, a separate run after the main one (KaroX results are not re-run).

**Gate.** Run only if a 3-task wiring pilot on dev-pool tasks (tag ocpil, ids 75,76,78) gives a verifier reward and
gateway cost lines for every trial. Otherwise report "OpenCode not wired" and stop.

**Decisions (pool9 alone, FAIR tasks, same bootstrap as prereg_pool9.md: 10 000, seed 1).** Pair = dev65 (karox6 arm
of the main run) vs OpenCode.
- **O1 leading on solves:** lower 95% CI bound of mean(solve dev65 − solve OpenCode) > 0.
- **O2 cheaper:** upper 95% CI bound of the cost/solve ratio dev65/OpenCode < 0.85.
- Reported: wall ratio (geometric, all valid tasks), all/PARTIAL/UNFAIR solves.
Valid tasks = those valid in the main evaluation that also have a reward for every OpenCode attempt; others reported.
Spend cap $8. No re-runs.
