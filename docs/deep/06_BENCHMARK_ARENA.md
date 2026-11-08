# Benchmark arena — epoch e2-2026-11-08

**Status: DEV evidence only. No frozen-holdout winner claim.** Epoch e1 was
invalidated and is retained as a harness-failure record; epoch e2 has a
repaired, frozen task registry and a preliminary four-arm result. The refactor
Phase-2 gate was green at `51be451`; the wheel/build evidence is below.

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

Current state: **10/10 DEV tasks validated RED→GREEN** (registry
`489f0495cfb42cdf586334ecde7bdd2691be9c6dca46258d005b82a17558e08e`);
the 100-task target and frozen holdout remain open. The optional Three.js
grader uses a vendored Three.js r160 + Node scene-graph/control checks, with an
optional Playwright mount marker; WebGL rasterization is not graded, which is
an explicit limitation, not a hidden pass.

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

## Epoch e3 — 20-task DEV expansion (preliminary)

Ten new DEV tasks were authored and independently validated RED→GREEN, giving
20 tasks total (registry `ef63bcd5…33e461`, all prompts self-contained with the
autonomy line, all rubric hashes match). New categories include a second
backend-large-context task, a second browser workflow, a second Three.js
scene, SSRF/HMAC security, multi-source migration and a framed MCP protocol.

New-task results (e2 old-task rows retained from the previous run):

| Metric | Karo | OpenCode | Codex |
|---|---:|---:|---:|
| Solved (of 20 attempted rows) | **18/20** | 2/16 attempted (4 missing) | 4/20 |
| Median time | 45.6 s | 113.8 s | 119.6 s |
| Cost | UNKNOWN | $0 (free model) | UNKNOWN |

Karo's two failures: `browser-orbit-shipping-workflow` (selector bug:
`querySelector("promo-status")` instead of the `data-testid` selector) and
`feature-inbox-jsonl-import` (real grader failure). OpenCode's free model hit
the 300 s timeout on six new tasks; four rows were not run after the batch was
stopped to avoid wasteful repeats, and are shown as missing, not as passes or
losses. Codex produced several zero-patch/question turns.

This remains **partially controlled DEV evidence**, not a frozen-holdout
leaderboard. It is sufficient to justify the next engineering loop on Karo's
two failures and to expand toward 100 tasks; it is not sufficient to claim
category leadership.

## Epoch e2 result — preliminary, not a holdout

All four arms were rerun on the repaired registry. Claude's lane is **excluded**
because its noninteractive sandbox still denied writes even with
`bypassPermissions` + both bypass flags; its runs made zero-byte patches and are
not evidence about coding ability.

| Task | Karo | OpenCode | Codex |
|---|---|---|---|
| backend-catalog-pagination | ✅ | ✅ | ✅ |
| browser-orbit-shipping-workflow | ❌ | ❌ | ❌ |
| debug-catalog-stale-cache | ✅ | ✅ | ✅ |
| feature-contact-csv-import | ✅ | ❌ | ❌ |
| frontend-pricing-responsive | ✅ | ❌ | ❌ |
| mcp-record-lookup-tool | ✅ | ❌ | ❌ |
| migration-subscription-records | ✅ | ❌ | ❌ |
| security-vault-document-access | ✅ | ❌ | ❌ |
| threejs-orbit-gallery | ✅ | ❌ | ❌ |
| ts-invoice-quote-core | ✅ | ❌ | ❌ |

Raw aggregate: **Karo 9/10**, OpenCode 2/10, Codex 2/10. Karo's browser failure
is real: its `workflow.py` timed out waiting for the confirmation page (3/5
grader checks passed); the other arms made no workflow patch on that task.
Competitor browser lanes may also lack the task's Python Playwright runtime, so
that row is environment-sensitive and is not used to claim a 3D/browser win.

This is **preliminary DEV evidence**, not a frozen-holdout leaderboard: each arm
uses a different model/provider, n=10, and several competitor failures are
zero-patch/question turns rather than demonstrated wrong code. It is useful for
the engineering loop (Karo solved backend, debugging, multi-file, frontend,
MCP, migration, security, Three.js and TypeScript on this set) but cannot be
cited as final superiority.

## Epoch e2 — repaired prompts, rerun complete (see result above)

Epoch e1 was invalidated above. Both task authors audited all ten prompts
against their graders and added every externally observable requirement plus
the standard autonomy line. Fixtures/graders/references are unchanged; all ten
`validate.py` runs are still RED→GREEN, all rubric hashes match, and the new
registry is frozen at:

`registry_sha256 = 7d95e2cf8561b514fa0f9e4d775f169cf1a55b75add04a5df2b3d5862f7bcdd8`

The four arms are being rerun into `local-runs/arena/runs-e2` with the same
runner, models, budgets and graders. No e2 result is reported until the batch
is complete; e1 files are retained but must not be cited.

## DEV batch (epoch e1) — INVALID FOR A LEADERBOARD

All four arms completed the frozen 10-task DEV registry. Raw solved counts:

| Arm | Solved | Median time | Cost | Status |
|---|---:|---:|---:|---|
| KaroX | 7/10 | 28.9 s | UNKNOWN | browser rerun solved; 3 real failures |
| OpenCode | 2/10 | 88.6 s | $0 | free model |
| Claude | 2/10 | 63.0 s | $0.034572 | mostly zero-patch/question turns |
| Codex | 2/10 | 139.8 s | UNKNOWN | several zero-patch/question turns |

**This comparison is not valid for any leaderboard claim.** Causes established
from the raw logs, not guesses:

- Several task prompts do not state the full acceptance contract that the
  independent grader enforces (for example the MCP projection/scope details).
  Codex and Claude responded with a design question and a zero-byte patch; Karo
  guessed a projection and passed. That is prompt ambiguity, not a clean coding
  comparison.
- Browser graders for three arms were invoked through the old pytest-only
  dispatch and returned exit 5; Karo's post-fix rerun passed with real headless
  Chromium. Those browser rows are harness-invalid for the other arms.
- The arms use different models/providers (DeepSeek V4.1 Flash, free
  DeepSeek, Claude Haiku, ChatGPT default), so this is at best partially
  controlled.
- Karo's failed frontend/multi-file/Three.js tasks are **real grader failures**
  worth engineering analysis after the task contract is made explicit.

The raw logs, diffs and costs are retained in `local-runs/arena/runs`; the
aggregate is `DEV_SUMMARY.md/json`. Epoch e1 is closed as an invalid/incomplete
comparison and must not be cited as evidence of superiority. The next epoch must
first make every prompt self-contained (including the autonomy requirement) and
rerun all arms on the same frozen registry.

## First DEV pilot — one task, not a leaderboard

Task `backend-catalog-pagination` (synthetic DEV fixture, independent grader
validated RED 11/12 → GREEN 12/12 before any arm ran). Same prompt, fixture and
grader; different providers/models, so this is **partially controlled**, n=1.

| Arm | Model | Solved | Wall time | Cost | Notes |
|---|---|---|---:|---:|---|
| KaroX | `wandb/deepseek-ai/DeepSeek-V4.1-Flash` | yes | **14.19 s** | UNKNOWN (no pricing) | 38,508 total tokens, 4 steps, verified true; patch 9,968 B |
| OpenCode | `opencode/deepseek-v4.1-flash-free` | yes | 44.04 s | $0 | free model, 18,984 final-step tokens |
| Claude | `claude-haiku-5-5` (default) | yes | 48.97 s | $0.01312508 | added a boundary test as well as the fix |
| Codex | default ChatGPT | yes | 155.21 s | subscription, UNKNOWN USD | 542,100 input / 9,200 output tokens; first 372 s run was `invalid_infra` due a `__pycache__` cleanup loop and is excluded |

A first Karo attempt is **excluded as invalid_integrity**: the harness had not
passed the required explicit verification command, so the agent exited before a
model call. A first Codex attempt is **excluded as invalid_infra**: the fixture
run produced `__pycache__`, and Codex spent the budget trying to remove it
through a blocked policy. Both are harness defects, not capability results.
The clean reruns above are the valid records.

This pilot proves the isolation/grader pipeline works. It does **not** prove
category leadership, cost parity or holdout quality. The task set is still being
completed (9/10 DEV records present; the Three.js record is pending validation)
and no holdout exists yet.

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
