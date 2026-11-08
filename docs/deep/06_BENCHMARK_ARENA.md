# Benchmark arena — epoch e1-2026-11-08

**Status: PREPARATION ONLY. No benchmark runs and no winner claims yet.** The
refactor Phase-2 gate must be green at the frozen commit first; this document
records the registry and the honest readiness gaps.

## Frozen candidate (Phase 2)

- Code commit with a full green suite: `51be451` (all 100%).
- Frozen metadata commit: `4919a04` (docs-only delta from `51be451`).
- Wheel: `karox_runtime-5.0.0-py3-none-any.whl`, 1,228,540 bytes,
  SHA-256 `815030a7964fad0c294df568113d0c65c161ddde1e271be81ac662f598ba62f5`,
  264 files / 2 prompts / 10 builtin skills / TUI theme.
- Fresh-venv install: `karox --version` = 5.0.0, `karox doctor --json` =
  `status: ok`, TUI/bridge imports succeed.
- No paid benchmark run has occurred yet; this freeze is the candidate, not a
  leaderboard result.

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

## Runner contract

`local-runs/arena/run_task.py --task <dir> --arm <arm> [--model ...]`:
copies `fixture/` into a fresh git workspace, records the exact command and
task/fixture hashes, runs the CLI in that workspace, captures the full
`changes.patch` and logs, then runs the independent `grader/` with
`TASK_WORKSPACE` pointing at the solution. A run is `solved` only when the
grader exits 0; timeouts and failures are classified, never discarded.
`run_batch.py` runs a task directory for one arm and writes a summary.

Current arm command policy (must be copied verbatim into every result):
Karo `agent run --stream-json` with explicit `--route` when the default route
is unusable; Codex `exec --sandbox workspace-write --skip-git-repo-check
-c approval_policy=never`; Claude `-p --dangerously-skip-permissions
--output-format stream-json` in an isolated fixture; OpenCode `run
--format json` with an explicitly recorded model. Gemini is not run while
credential-blocked; DSH has no pinned headless command yet.

## Readiness probes (2026-11-08, not benchmark results)

One harmless prompt per available arm; actual costs are in
`local-runs/arena/cost-ledger.jsonl`:

| Arm | Probe result | Provider/model | Cost |
|---|---|---|---|
| Codex | `OK`, exit 0, 23,301 input / 5 output tokens | default ChatGPT session | subscription, UNKNOWN USD |
| Claude | `OK`, exit 0 | `claude-sonnet-5-5` | **$0.0919574** list cost |
| KaroX | real call succeeded on a non-default route, but the answer was not verified; default `stepfun-plan/step-5-preview` returns HTTP 400 `request_params_invalid` | `wandb/deepseek-ai/DeepSeek-V4.1-Flash`, 4,841 prompt / 5,034 total tokens | UNKNOWN, no pricing |
| OpenCode | `OK` with an explicit model, exit 0; the default configured provider returns `Unexpected server error` | `opencode/deepseek-v4.1-flash-free`, 13,180 input / 3 output, 1,792 cache-read | **$0** (free model) |
| Gemini | not run | — | blocked by credential |
| DSH | not run | — | headless profile not pinned |

These are readiness observations only. Karo's default provider route and the
OpenCode default config are not claimed as working benchmark configurations;
the explicit Karo `wandb/...` route and OpenCode free model are recorded, and
any run must name them. A free model is not the same capability as a paid
frontier model; the lane record must say so.

## Immediate next steps

1. Freeze the refactor commit after a green full gate, rebuild the AFTER wheel
   and record its SHA-256.
2. Probe one harmless model call per READY arm to pin provider/model; record
   cost separately as readiness, not benchmark evidence.
3. Resolve or honestly park Gemini auth and DSH headless invocation.
4. Build the 10-task DEV pilot with independent public-test graders and a
   pre-registered holdout split; then start lane A.
