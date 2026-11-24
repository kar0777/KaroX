# Pre-registration: dev61 vs dev56 on pool5, NATIVE Anthropic provider (2026-11-24)

Council 14 (Codex + Neon): option 3. Re-check of prereg_dev61 in the shipped configuration.
- Arms: karox6 = 6.0.0.dev56, karox6b = 6.0.0.dev61; both with `--provider anthropic` (checked in trial.log before
  analysis). claude-haiku-5-5, 72 pool5 tasks (141-212), 1 attempt. Hard cap $2.00 for both arms; fewer than 72
  completed pairs = keep dev56.
- dev61 goes to the single final pool3+pool4 comparison vs Claude Code iff solves(dev61) > solves(dev56) AND
  total cost(dev61) <= 1.20x cost(dev56). Otherwise keep dev56.
- $0 pre-check done: in the openai-chat run, 5 tasks carried 51% of dev61's extra output (long explorations).
