# Pre-registration: dev61 (progress guard counts new shell commands) vs dev56 on pool5 (2026-11-24)

Council 13 (Codex gpt-6.2-sol + Gemini 4 Neon). Fusion review of dev61 before the run.
- Arms: karox6 = 6.0.0.dev56, karox6b = 6.0.0.dev61. Upstream anthropic, claude-haiku-5-5. 72 pool5 tasks
  (night ids 141-212), 1 attempt. Expected $2.0-2.2, hard cap $2.60; fewer than 72 pairs = no selection.
- dev61 becomes the candidate for the single final pool3+pool4 comparison vs Claude Code iff
  net (dev61-only minus dev56-only solves) >= +3 AND total cost(dev61) <= 1.20x cost(dev56). Otherwise keep dev56.
- Report: paired outcomes, 95% bootstrap CI, cost per solve, wall (diagnostic only).
