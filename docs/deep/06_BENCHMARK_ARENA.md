# Benchmark arena — epoch e1-2026-11-08

**Status: PREPARATION ONLY. No benchmark runs and no winner claims yet.** The
refactor Phase-2 gate must be green at the frozen commit first; this document
records the registry and the honest readiness gaps.

## Why this exists

The goal requires evidence-based competition after the deep refactor: same
tasks, independent scoring, full logs, dev+holdout split, and no favourable
treatment for Karo or any competitor. This arena is designed to make every
claim reproducible and to make missing competitors visible as `UNAVAILABLE` or
`BLOCKED`, never as an implied win.

## Epoch policy

- One epoch = one frozen set of CLI versions, task registry hash, grader hashes
  and host description.
- Latest public stable npm tag is installed **locally** under
  `local-runs/arena/tools`; competitor source is never patched.
- If a version changes, close the epoch, start a new one and rerun the baseline.
- `karox-before` is the verified 5.0.0 wheel from commit `066a351`
  (SHA-256 `d9867102…396d`); `karox-after` is rebuilt from the frozen refactor
  commit.
- Auth probes and version output are recorded, but no credential value is read
  or printed.

## Registry snapshot (2026-11-08)

| Arm | Latest stable | Installed for arena | Auth readiness | Lane status |
|---|---|---|---|---|
| KaroX 5 AFTER | 5.0.0 refactor candidate | wheel pending freeze | local product | code ready, wheel pending |
| KaroX 5 BEFORE | 5.0.0 @ `066a351` | verified wheel | local product | wheel verified, worktree pending |
| OpenCode | `opencode-ai` 1.19.5 | 1.19.5 local | auth list shows providers; model not probed | `NEEDS_MODEL_PROBE` |
| OpenAI Codex CLI | 0.167.0 | 0.167.0 local | ChatGPT session logged in | `READY_FOR_MODEL_PROBE` |
| Google Gemini CLI | 0.67.0 | 0.67.0 local | no API key/confirmed OAuth | `BLOCKED_BY_CREDENTIAL` |
| Claude Code | 2.1.319 | 2.1.319 local | claude.ai login valid to 2026-11-13 | `READY_FOR_MODEL_PROBE` |
| DeepSeek Harness | 0.2.2-rc.1 | running Web GUI | CLI headless profile not pinned | `NEEDS_HARNESS` |

The machine-readable record, npm integrity hashes, package hashes and exact
paths are in `local-runs/arena/registry.json`.

**Version trap caught:** the globally installed `@opencode/cli` 2.2.1 is a
different package from the official `opencode-ai`; the global one is not used
for this epoch. This is exactly the kind of stale/different binary the goal
warns about.

## Lanes

- **A — product as shipped:** each CLI in its supported configuration, its own
  default tools/permissions/model. Reports provider, model and cost.
- **B — controlled harness:** same model checkpoint/route, prompt, starting
  commit, budgets and tools where achievable. Not mixed with A.
- **C — offline deterministic regression:** contracts and fast regression
  checks; used to prevent quality regressions, not as the headline leaderboard.
- **D — adversarial/security:** separate from coding quality; denied, tampered
  and replayed paths must be graded independently.

Current epoch cannot claim lane B until model routes are pinned for each arm.
Lane A is the first target, with per-arm model/provider disclosure.

## Task set

Target: **100+ task records** across backend, TypeScript/frontend, browser,
Three.js/WebGL/3D, multi-file features, debugging, CI/dependencies, MCP/CLI,
sessions/recovery and security. Every record must include before execution:

`task_id`, source URL/license, pinned repo/commit or fixture hash, prompt,
environment, acceptance rubric/scorer hash, difficulty, category, budgets,
contamination risk and expected observable behaviour.

A task may not be authored and solely graded by Karo; a public test, an
independent behaviour check, or a pre-registered rubric is required. A three
task pilot is a transport check, not the matrix. The task registry and all
scorers are hashed before any arm runs; dev and frozen holdout are separate.

Current state: **0/100 ready** (schema in
`local-runs/arena/tasks/schema.json`; no task is valid until its source,
fixtures and scorer exist). The next milestone is a 10-task DEV pilot with
public-test graders, not a leaderboard.

## Metrics and honesty

Primary: clean solve by independent acceptance test/behaviour. 3D adds
functional requirements plus a pre-registered blind visual rubric. Secondary:
cost per success, wall time per success, all attempts, tokens, calls, retries,
p50/p95, crashes/hangs, manual interventions and repeated-run reliability.
Every unsuccessful class (failed, partial, timed_out, blocked_by_credential,
infra_error, invalid_integrity) is retained.

No winner is declared until a frozen holdout has been run after candidate
freeze. Missing arms stay `UNAVAILABLE`; partially-controlled comparisons are
labelled as such. Historical contaminated grader access (Mission 17) remains
INVALID and is not reused.

## Cost ledger

`local-runs/arena/cost-ledger.jsonl` starts empty. No paid model runs have been
started. Exact provider bills are recorded when available; estimates are marked
`estimated`, unknown costs are `UNKNOWN`, never invented.

## Immediate next steps

1. Freeze the refactor commit after a green full gate, rebuild the AFTER wheel
   and record its SHA-256.
2. Probe one harmless model call per READY arm to pin provider/model; record
   cost separately as readiness, not benchmark evidence.
3. Resolve or honestly park Gemini auth and DSH headless invocation.
4. Build the 10-task DEV pilot with independent public-test graders and a
   pre-registered holdout split; then start lane A.
