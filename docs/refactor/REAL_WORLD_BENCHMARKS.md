# Real-world benchmarks

**Status: one real smoke, no comparison matrix. Spend: cost unavailable.**

The goal requires real tasks and comparisons. One real provider smoke has been
run; no benchmark cell, no competing CLI comparison, and no multi-task result
set exists yet. This file records the smoke and the frozen protocol rather than
inventing a benchmark.

## Paired baseline/candidate cell — 2026-11-07

Same model (`wandb/deepseek-ai/DeepSeek-V4.1-Flash`), same prompt, same
`workspace_write` profile, same approved `python -m pytest -q`, same
`--max-total-tokens 30000`, separate temp repos/configs/runtimes.

| Arm | Status | Steps | Requests | Tokens | Approved check | Changed |
|---|---|---|---:|---:|---|---|
| Baseline c4 source (`b/win`, 6.0.0a3) | **stopped: budget_exceeded:token_budget** | 4 | 5 | 34,113 | exit 0 | `calc.py` |
| Candidate `f3bceb0` | **verified: true** | 4 | 4 | 27,131 | exit 0 | `calc.py` |

The baseline produced the correct one-line patch and its check passed, but the
run stopped before a current change review and never became `verified=true`.
The candidate reached VERIFIED with one fewer request and 6,982 fewer tokens.
This is **one trivial paired cell**, not a benchmark result; it is not evidence
of general superiority.

## Real provider smoke — 2026-11-07

- Arm: KaroX candidate, `wandb/deepseek-ai/DeepSeek-V4.1-Flash`.
- Task: fix `calc.py` (`a - b` → `a + b`) so the public `test_calc.py` passes.
- Entry point: `karox agent run`, `workspace_write`, approved check
  `["python","-m","pytest","-q"]`, `--max-total-tokens 30000`.
- Result: **verified: true**, 4 steps, 4 model requests, 1 changed file,
  approved check exit 0.
- Usage: **27,131 total tokens** (prompt 26,810, completion 321; cache-read
  6,400); cost status **unknown — no pricing** (`cost_unknown_reason:
  no_pricing`). No invoice or dollar estimate is invented.
- Isolated config/runtime under `%TEMP%\karox5-paid-smoke`; no real key printed
  or written outside the OS keyring.
- This is a transport/tool/verification smoke. It is **not** a comparison and
  not a statistically meaningful benchmark.

## Candidate configurations

| Arm | Version under test | Entry point | Status |
|---|---|---|---|
| Karo baseline c4bc342 | 6.0.0a3 at `b/karox5-refactor` before refactor | `karox agent run` | offline tests only |
| KaroX 5.x refactor | candidate `f8b641b`, version still to be chosen | same | offline tests only |
| OpenCode V2 | 2.2.6 frozen | `opencode run` | not run |
| DSH | 0.2.2-rc.1 + Node 24.22.2 | `dsh` | not run |

## Planned task families (30–100, not yet frozen)

- Python/TS/JS/Go/Rust bugfix with failing public tests
- feature addition with acceptance test
- refactor/CI/build dependency repair
- documentation/migration
- browser/frontend local page and visual evidence
- large-repo exploration, Unicode/symlink paths
- interrupted session/resume and tool recovery

For every task the registry must store exact prompt, source commit, environment,
observable acceptance test/scorer, model and limits **before** seeing results.

## Cell ledger schema

```text
task_id, source_commit, task_hash, scorer_hash, arm, arm_version_sha,
model/route, permissions, timeout, start/end, exit, tokens(in/out/reasoning/cached),
estimated_usd, actual_invoice_if_known, tool_calls, failed_tools, retries,
clean_solve, artifact/patch path, notes
```

No result may be called a protected holdout. Open acceptance tests are not a
security proof. A hidden grader is not exposed to an agent; Mission 17's
contaminated candidate remains INVALID.

## Prerequisites before first paid cell

1. One deterministic full test run on the candidate.
2. Wheel build and fresh install.
3. A provider/keyring setup that does not put a raw key in a workspace or log.
4. Task registry and scorer hashes frozen.
5. Per-cell timeout/retry/cost cap and a shared ledger.
6. Browser/desktop availability explicitly marked, not assumed.

## Next step

After the candidate is deterministically green and versioned, start with a small
development set (6–10 tasks, paired arm order) and do not call it a release
benchmark until the full 30–100-task registry has been run.
