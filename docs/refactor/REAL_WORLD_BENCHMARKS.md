# Real-world benchmarks

**Status: 3-task × 3-arm pilot + real browser smoke; no 30–100-task representative matrix. Spend: cost unavailable.**

The goal requires real tasks and comparisons. One real provider smoke has been
run; no benchmark cell, no competing CLI comparison, and no multi-task result
set exists yet. This file records the smoke and the frozen protocol rather than
inventing a benchmark.

## Three-task pilot matrix — 2026-11-07

Machine-readable registry and raw ledger:

- `docs/refactor/benchmarks/registry-20261107-pilot.json`
- `docs/refactor/benchmarks/ledger-20261107-pilot.json`

Three reproducible open tasks (bugfix, feature, refactor), each run in a fresh
repo/commit with a public pytest scorer. Karo arms used
`wandb/deepseek-ai/DeepSeek-V4.1-Flash`; OpenCode used its available
`opencode/deepseek-v4.1-flash-free`. This is **Product-as-shipped**, not a
same-model controlled comparison.

| Task | Candidate | Baseline c4 | OpenCode 2.2.1 |
|---|---|---|---|
| bugfix-add | verified, 29,755 tok, 9.23 s | verified, 29,775 tok, 8.86 s | pytest pass, 10.60 s, no usage |
| feature-slug | verified, 29,755 tok, 9.08 s | verified, 29,746 tok, 9.71 s | pytest pass, 8.89 s, no usage |
| refactor-square | verified, 30,607 tok, 11.12 s | verified, 47,380 tok, 11.75 s | pytest pass, 19.19 s, no usage |

All nine cells produced a patch whose public pytest passed. The only visible
token difference is the refactor cell, where baseline used 47,380 tokens and
candidate 30,607; two cells are within 1% and cannot support a superiority
claim. OpenCode reported no token/verified field, so its harness outcomes are
not directly comparable. DSH is absent because it has no deepseek-official
credential. Cost is still unavailable (`no_pricing`).

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
| Karo baseline c4bc342 | 6.0.0a3 source `b/win` | `karox agent run` | one paired cell |
| KaroX 5.0.0 | `karox_runtime-5.0.0` wheel, SHA `d9867102...` | same | one verified smoke + paired cell |
| OpenCode V2 | installed 2.2.1 (target frozen 2.2.6) | `opencode run` | **one product-as-shipped pass** on `opencode/deepseek-v4.1-flash-free` (15.2 s, fixed file, pytest pass) |
| DSH | 0.2.2-rc.1 + Node 26.13.0 (target 24.22.2) | `dsh headless` | **blocked: `MISSING_CREDENTIAL` for deepseek-official**; no file change |

## Real browser user scenario — 2026-11-07

- Arm: local 5.0.0 source/wheel, `browser_control`, W&B DeepSeek-V4.1-Flash.
- Task: open `http://127.0.0.1:18080/test.html` via `browser.command`, read the
  h1, finish; explicit approved URL-content check.
- Result: **verified: true**, 3 steps, 3 requests, **16,857 tokens**, no file
  edits; `browser.command open` and `get_text` both succeeded; answer:
  `"KaroX Browser Smoke"`. `answer_basis` names the `get_text` call.
- Cost: `no_pricing`; no invoice.

This exercises the real provider→policy→browser→completion path, not only the
fixture browser tests.

## Competitor credential attempts — 2026-11-07

The same trivial `calc.py` task was attempted Product-as-shipped:

- OpenCode 2.2.1 with its default `dash/deepseek-v4.1-flash-1017` exited 1 after
  9.8 s with `Incorrect API key provided`; no file change. A second
  **product-as-shipped** run with the available free model
  `opencode/deepseek-v4.1-flash-free` succeeded: 15.2 s, one correct one-line
  patch, `python -m pytest -q` exit 0. OpenCode did not report tokens or cost in
  the captured output.
- DSH 0.2.2-rc.1 exited 1 after 7.7 s with
  `MISSING_CREDENTIAL: llm-deepseek: no API key for provider route
  "deepseek-official"`; `calc.py` was unchanged and the public test still failed.
- DSH is **not comparable** yet. OpenCode used a different model/provider from
  Karo and baseline; this is a Product-as-shipped datapoint, not a controlled
  same-model comparison. The installed competitor versions also differ from the
  frozen targets (OpenCode 2.2.1 vs 2.2.6, Node 26 vs 24.22.2). No claim of
  general superiority is made from these cells.

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




