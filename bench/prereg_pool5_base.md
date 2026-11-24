# Pre-registration: pool5 baseline, KaroX dev56 vs Claude Code (2026-11-24)

Council 11 (Codex gpt-6.2-sol + Gemini 4 Neon): approved option 1.
- Tasks: all 72 pool5 tasks (night ids 141-212, bench daffa77, bench/pool5_frozen.json). 1 attempt per arm.
- Arms: karox6 = wheel 6.0.0.dev56 (main), claude_code (same image/harness as pool3a). Upstream anthropic,
  model claude-haiku-5-5. Expected $1.95, hard cap $2.10, no paid repeats of any trial.
- Report (no pass/fail; measurement run): solved per arm, difference of solve rates with paired bootstrap 95% CI,
  discordant counts (KaroX-only / CC-only), $ from gateway audit (uncached in $0.10/M, cache read $0.01/M,
  out $0.50/M, interview excluded), median agent wall, tool-seconds ratio.
- Infrastructure failures (trial without a verifier reward, harness exception before the agent's first model
  call): reported separately and excluded pairwise from the solve comparison; not re-run.
- Minimal practically relevant gap: 5 percentage points.
- After this report is committed, pool5 logs may be read for diagnosis: pool5 becomes a dev pool.
  pool3+pool4 keep exactly one comparison, reserved for one version chosen before that run.
