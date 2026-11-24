# Pre-registration: dev62 (related-files notice) — pilot + waves on pool5 vs stored dev61 control (2026-11-24)

Council 17 (Codex + Neon), merged (stricter where they differed, except caps/ratios explained).
Control: stored dev61 native arm of run 45407450472 (bench/baselines.json). One arm per paid run: dev62, native
provider (bench default), claude-haiku-5-5, 1 attempt. Task lists fixed in bench/plan_dev62.json (seed 20261124).
- Preflight: night id 28 (mine pool, outside pool5), preflight_check.py must PASS (provider anthropic, 6.0.0.dev62).
- Pilot (ids 153,156,174,177,192,206 = 4 dev61 failures whose missed gold file is in the list + first 2 dev61
  solves with a non-empty list). PASS iff notice fires in >= 5/6; in >= 2 of the 4 failures the agent edits a
  listed gold file after the notice; both prior solves stay solved; pilot cost <= 1.6x the same 6 tasks in the
  control. Cap $0.20 incl. preflight. FAIL -> stop.
- Waves (pilot tasks excluded): wave 1 = 24 tasks stratified by control outcome (11 solved / 3 missed-gold-hit /
  10 other failures), wave 2 = remaining 41. Stop after wave 1 iff solves(dev62) <= solves(control) - 2 on those
  24, or cost/solve > $0.052. Final at 65: dev62 accepted iff solves > control's 29 and cost/solve <= $0.048
  (0.85 x Claude Code $0.056). Waves cap $1.55 (Neon: $1.30 would leave a passing run inconclusive).
