# Real-world benchmarks

**Status: NOT RUN. Spend: $0.00.**

The goal requires real tasks and comparisons. No benchmark cell has been run in
this pass, so this file records the frozen protocol and the exact blocker rather
than inventing results.

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
