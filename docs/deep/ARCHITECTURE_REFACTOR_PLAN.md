# Deep-refactor architecture plan (living)

**Base:** `066a351` · **Branch:** `refactor/karo5-deep-competition-20261107`.

The goal is not fewer lines for their own sake: it is one owner per
responsibility, no duplicate decision paths, no reverse dependency edges, and no
lost product capability. Every batch below ends with a reversible checkpoint,
targeted tests, the full deterministic gate, and a before/after architecture
note.

## Ownership model

The public module paths stay stable (external/hosted/TUI contracts and tests
import them). A monolith becomes a **facade** that re-exports the public names
from cohesive implementation modules. New modules are added only for a real
responsibility, not as forwarding shells.

Dependency direction is preserved:

```text
UI/TUI -> CLI -> engine/kernel -> Core tools -> policy/risk
      -> providers/sessions/context/MCP/browser/desktop -> platform
```

## Batches

### Batch 0 — dead legacy (first, evidenced)

Candidates with static + dynamic + docs reachability proof before removal:

| Candidate | Size | Current evidence | Action |
|---|---:|---|---|
| `ellipsis_checkpoint.py` | 379 | no source/test/dynamic reference; `docs/MIGRATION.md` lists it as copied, but Smart Stop is `risk_engine.py`; no entrypoint | remove after adversarial check |
| `verification.py` `VerificationResult/build_result/AutopilotPlan/plan_autopilot` | ~200 | dead API; live path is `completion_gate.floor` + `session_analysis`; `discover_verification_commands` stays | remove after adversarial check |
| duplicate helpers | TBD | audit duplicate-body/name report | merge only with a parity test |

Add retired names to the release `LEGACY` set only when removal is deliberate
and pinned by a test.

### Batch A — agent / engine / tools / verification / risk

1. `engine_kernel.py` (2,905) → `engine_agent.py` (EngineAgent + context
   management), `engine_kernel.py` (kernel core + run), `engine_kernel_io.py`
   (durable session writes/state snapshots), `engine_kernel_harness.py` (final
   harness calls, repair prompt, failure summary), `engine_kernel_finish.py`
   (finish/report/terminal reason), `engine_kernel_events.py` (event bridge).
   Compatible class composition, no behavior change.
2. `engine_tool_env.py` (1,864) → environment core, observation/notice helpers,
   result-body renderers.
3. `core_tools.py` (1,802) → extended-runtime core + job tools + edit/patch
   tools + read/fetch tools (mixins or focused collaborators), facade kept.
4. `completion_gate.py` (1,283) → gate core (`floor`), visual proof, change
   review, requirement/test-edit rules.
5. `check_report.py` (1,293) → runner parsers, failure map, target selection.
6. Policy/risk: prove who decides, delete only duplicated decisions; keep
   `policy.py` (capability) + `risk_engine.py` (consequence) as the two gates.

### Batch B — providers

`providers.py` (1,638) → provider wire types/errors + OpenAI-compatible chat
transport. `provider_adapters.py` (1,503) → one module per wire adapter
(OpenAI Responses, Anthropic, Gemini) over a shared streaming base. Keep the
`providers`/`provider_adapters` import surface, retry/fallback/cost semantics
and pricing/usage contracts.

### Batch C — browser / WebGL / visual verification (first-class, not legacy)

`browser/session.py` (3,703) → session/lifecycle core, interaction actions,
observation/snapshot, tabs/history, network/dialog/screenshot collectors.
Keep every action and the owner-thread fail-closed behavior. `web_check.py`
(1,831) → static document proof + browser render/observe + public check API.
Do not "simplify" Three.js/WebGL 3D visibility checks away: they are product
features with dedicated tests.

### Batch D — CLI / TUI

`cli.py` (2,299) → dispatcher/facade plus command modules: agent run, session,
provider/model, MCP, bridge (already separate), skill/doctor/credential/paths.
`tui/app.py` (2,038) → shell/state + command handling + run-stream handling +
presentation helpers, without importing the agent runtime.

### Batch E — hosted bridge

`bridge_launcher` (2,392) → process/watchdog ownership + connect + saved-bridge
lifecycle; `bridge_proxy` (2,218) → host/origin/bearer gates + MCP route
composition + error envelopes; `bridge_oauth` (1,483) → OAuth service store +
ASGI endpoints; `bridge_tailscale` (1,585) → status/doctor + funnel lifecycle;
`bridge_profiles` (1,144) → profile model/store + credential store. Preserve
OAuth bindings, DCR/PKCE, one-shot approval, idempotency and legacy keyring
names.

### Batch F — sessions / context / skills

`repo_context.py` (2,055) → repository identity/cache + search/index +
inspect/excerpts. `sessions`/`task_state`/`repository_lease`/`memory`/
`project_map`/`map_service`/`project_context` → one owner per persisted
contract; remove duplicate serialization only with load-parity tests.

### Batch G — platform / packaging

Paths, processes, keyring, port ownership and package-data stay downward-only.
Verify wheel resources and clean-venv install after every structural batch.

### Batch H — re-audit

Rebuild the AST graph, cycle list, fan-in/out, largest-file distribution and
dead-code ledger; compare with this baseline and with the goal's completion
criteria. Only then move to the competitive arena.

## Definition of done per batch

1. Public symbols/import paths verified.
2. Targeted affected tests green; full `pytest` green for the batch.
3. `ruff`, scoped `mypy`, `check_tree`, wheel build + clean-venv smoke.
4. No new cycle or reverse edge; before/after table updated.
5. Checkpoint commit; independent adversarial review of the risky diffs.
