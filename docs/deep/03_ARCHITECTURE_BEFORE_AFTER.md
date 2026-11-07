# Architecture before/after (living)

**Baseline:** `066a351` — 164 product modules, 87,689 physical lines, 884 AST
import edges, 4 strongly connected components.
**Current:** 172 modules, 87,658 lines (CLI split; verification trim).

Raw baseline graph: `local-runs/deep-audit/audit.json` / `audit.md`.
Reproduce with `python local-runs/deep-audit/audit.py`.

## Baseline facts

Largest modules (physical lines):

| Module | Lines |
|---|---:|
| `browser.session` | 3,703 |
| `engine_kernel` | 2,905 |
| `bridge_launcher` | 2,392 |
| `cli` | 2,299 |
| `bridge_proxy` | 2,218 |
| `repo_context` | 2,055 |
| `tui.app` | 2,038 |
| `engine_tool_env` | 1,864 |
| `web_check` | 1,831 |
| `core_tools` | 1,802 |
| `providers` | 1,638 |
| `bridge_tailscale` | 1,585 |
| `core` | 1,573 |
| `provider_adapters` | 1,503 |
| `bridge_oauth` | 1,483 |
| `check_report` | 1,293 |
| `completion_gate` | 1,283 |
| `mcp_client` | 1,201 |
| `hosted_core` | 1,162 |
| `bridge_profiles` | 1,144 |

Cycles at baseline (4):

- `command_jobs → core → core_git → core_process → core_tools`
- `agent → engine_kernel → engine_tool_env`
- `credential_secret_service → credential_session → credentials`
- `browser.guards → browser.session`

Fan-in leaders: `paths` (40), `sessions` (30), `providers` (26), `core` (18),
`registry` (16), `web_check` (14). Fan-out leaders: `cli` (98), `engine_kernel`
(39), `hosted_core` (34), `browser.session` (31), `core_tools` (30),
`engine_tool_env` (28).

## After — batch 0 (dead API) and CLI split

### Dead API

`verification.py` 362 → 106 lines: the unused typed-result/autopilot API is
gone; repository-aware discovery remains the single responsibility.

### CLI decomposition (2,299 → one 193-line façade + 8 modules)

| Module | Lines | Responsibility |
|---|---:|---|
| `cli.py` | 193 | public façade, dispatch, `main`, compatibility seams |
| `cli_agent.py` | 451 | `agent run` assembly and report |
| `cli_session.py` | 113 | durable session commands |
| `cli_provider.py` | 554 | `provider`/`model` commands |
| `cli_mcp.py` | 340 | outbound MCP commands |
| `cli_admin.py` | 187 | skills/doctor/status/credential |
| `cli_runtime.py` | 421 | provider route + MCP selection assembly |
| `cli_present.py` | 244 | argv aliases, report/NDJSON presentation |
| `cli_context.py` | 21 | durable registry constructors |
| `cli_bridge.py` (existing) | 328 | hosted bridge commands |

Dependency direction is now:

```text
cli.py facade
  ├── cli_agent ── cli_runtime ── cli_context
  ├── cli_session ── cli_present
  ├── cli_provider ── cli_context, cli_present
  ├── cli_mcp ── cli_context, cli_runtime
  ├── cli_admin ── cli_context, cli_present
  └── cli_bridge
```

No command module imports `cli.py`; the façade re-exports the historical helper
names tests/embedders use, and keeps `_registry` as a patchable seam for the
vision decision. There is no new import cycle.

### Metrics after the split

| Metric | Baseline | Current |
|---|---:|---:|
| Product modules | 164 | 172 |
| Physical lines | 87,689 | 87,658 |
| Largest file | 3,703 | 3,703 (`browser/session.py`, untouched yet) |
| `cli.py` | 2,299 | 193 |
| Import cycles | 4 | to re-measure after the engine/browser/bridge batches |

### Behaviour preservation

- All CLI commands, aliases, `--json`/NDJSON output and `main` dispatch are
  unchanged; targeted CLI/session/MCP/tree tests pass.
- Test-visible seams were preserved or retargeted with the move:
  `karox.cli._registry` (facade), `karox.cli_runtime.OpenAIChatCompletionsProvider`,
  `karox.cli_session.session_dir`.
- No product capability was removed in this batch.

## After — engine split step 1

`engine_kernel.py` 2,905 → 2,300 lines; `EngineAgent`'s methods (594 lines) now
live in `engine_agent.py` as `_EngineAgentMixin` (646 lines with the moved
error types, stale-read constants and briefing key).

The concrete class deliberately stays in the façade:

```python
class EngineAgent(_EngineAgentMixin, DefaultAgent):
    ...
```

The vendored engine writes `self.__class__.__module__` into its trajectory and
the report identity is `karox.engine_kernel.EngineAgent`; moving the concrete
class would silently change a durable artifact. `tests/test_engine_split_identity.py`
pins the module, the MRO and the report identity. The kernel still imports
`_EngineBudgetExceeded`/`_EngineProviderFailure` from the mixin module because
`run()` and the finish path catch them.

`engine_agent.py` sets `MSWEA_SILENT_STARTUP` before its first `.engine` import,
so importing the mixin directly stays silent and does not load another tool's
dotenv.

### Metrics

| Metric | Baseline | Current |
|---|---:|---:|
| Product modules | 164 | 173 |
| Physical lines | 87,689 | 87,486 |
| `engine_kernel.py` | 2,905 | 2,300 |
| `engine_agent.py` | — | 646 |
| Largest file | 3,703 | 3,703 (`browser/session.py`) |

### Behavior preservation

- Full `tests/test_engine_kernel.py`, `test_engine_tools.py`,
  `test_engine_port_salvage.py`, `test_engine_provider_resilience.py`,
  `test_agent_report_surface.py`, `test_prompt_cache.py`, `test_tool_shape_fixes.py`
  and the neutrality/tree/identity tests pass.
- `_any_language_server` stays in the façade (monkeypatched by tests).
- No public import changed; no logic edited in the move.

## After — browser split step 1 (`contract.py`)

`browser/session.py` 3,703 → 3,501 lines. The leaf contract moved to
`browser/contract.py` (286 lines): `CancelToken`, the four typed records, the
session caps, and the `READ_ACTIONS`/`INPUT_ACTIONS`/`ACTION_CAPABILITIES`
vocabulary. `session.py` imports and re-exports them, so
`browser.BrowserSession is session.BrowserSession`,
`browser.CancelToken is session.CancelToken` and the existing tests keep
working.

The only dependency edge that changed is the historical
`browser.guards → browser.session` edge: guards now take `CancelToken` from the
contract leaf. The action-wrapping loop still runs exactly once in
`session.py`; no Playwright code moved.

Tests: `test_browser_units`, `test_browser_resources`,
`test_browser_code_sources`, `test_public_package_facades` PASS.

## After — browser split step 2 (guards)

The four session guard methods (`_assert_agent_input_allowed`,
`_guard_fill_target`, `_assert_press_text_allowed`, `_assert_action_safe_page`)
and the credential/payment hint now live in `browser/guards.py` on the existing
`SessionGuardsMixin`; `BrowserSession(SessionGuardsMixin)` inherits them with the
same names. `guards.py` 151 → 267 lines; `session.py` 3,501 → 3,398.
`test_browser_core`, unit/resource/code-source and public-facade tests PASS.

## After — browser split step 3 (runtime)

`browser/session.py` 3,398 → 3,141 lines; the owner-thread runtime moved to
`browser/runtime.py` (`BrowserRuntimeMixin`, 312 lines): owner loop and queue
marshalling, `dispatch`, the fail-closed `_guarded` action wrapper, parking and
waking, `_stop_owner`, `_active_page` and the stateless selector/text helpers.

The concrete `BrowserSession` composes the mixin first
(`BrowserRuntimeMixin, SessionGuardsMixin`); methods keep their names,
decorators, signatures and the exact fail-closed timeout. Tests that patch
`karox.browser.session.demote_headless_browsers` keep working through the
`_demote_browser_processes()` seam on the session; the runtime never imports the
session at runtime (TYPE_CHECKING only).

Tests: browser resources/units/core/code-source/public-facade PASS.

## After — browser split step 4 (navigation)

`browser/session.py` 3,141 → 2,820 lines; navigation, tab management, history
and page registration moved to `browser/navigation.py`
(`BrowserNavigationMixin`, 458 lines). The session keeps the owner-thread
runtime and composes navigation first. Module-qualified `code_sources.fetch`
calls stay patchable. Browser resource/unit/core/code-source/facade tests PASS.

## Browser series complete

`browser/session.py` 3,703 → **173 lines**: a composition façade with
`__init__`, three patchable seams and the single action-wrapping loop. Every
responsibility has an owner module:

| Module | Lines | Responsibility |
|---|---:|---|
| `contract.py` | 294 | cancellation, typed records, caps, action vocabulary, windowless flags |
| `guards.py` | 267 | locator/input/payment guards |
| `runtime.py` | 312 | owner thread/queue, dispatch, guarded wrapper, parking |
| `navigation.py` | 368 | routes, pages, tabs, history, recovery |
| `collectors.py` | 429 | console/network/dialog/cookie events |
| `observation.py` | 497 | snapshots, text/evaluate, forms, iframes, timing |
| `interaction.py` | 620 | click/drag/fill/type/key/press/check/scroll |
| `waits.py` | 204 | waits, user takeover, recovery |
| `evidence.py` | 316 | screenshots, uploads, downloads |
| `launch.py` | 437 | managed/detached launch, reconnect |
| `lifecycle.py` | 255 | close, force-close, profile persistence, process terminate |

The owner-thread serialization, fail-closed timeouts, action wrapping, result
shapes and all WebGL/Three.js/3D-related behaviour outside `browser/` are
unchanged. `web_check.py`, `visual_check.py`, `render_backend.py` and
`code_sources.py` were deliberately not touched: their 3D visibility checks are
product features with dedicated tests.

## After — engine split complete

`engine_kernel.py` 2,905 → **753 lines**: the façade keeps the two concrete
classes (trajectory identity), `EngineKernel.__init__`, the single `run()`,
tool advertising and context-budget math. Every other responsibility has an
owner module:

| Module | Lines | Responsibility |
|---|---:|---|
| `engine_agent.py` | 646 | EngineAgent methods, resume/context, stale reads, loop-control errors |
| `engine_prompts.py` | 89 | engine execution contract + closing-turn prompts (neutrality-scanned) |
| `engine_tool_catalog.py` | 165 | domain-keyword schema gating, provider-tool shape helpers |
| `engine_kernel_control.py` | 170 | cancellation, grace windows, remaining time, event bridge |
| `engine_kernel_io.py` | 670 | transcript seeding, durable message/usage/tool persistence, state briefing, verified snapshot/restore |
| `engine_kernel_finish.py` | 252 | completion predicates, termination reason, provider-failure record, finish/report |
| `engine_kernel_harness.py` | 417 | harness final calls, closing answer turn, repair prompt, failure summary |
| `engine_identity.py` | 23 | the report identity constant shared by façade and finish mixin |

The façade composes `EngineHarnessMixin, EngineFinishMixin, EngineIoMixin,
EngineControlMixin`; private methods stay reachable for the unbound test calls.
`_any_language_server` remains in the façade because tests monkeypatch it there.
The full engine contract suite (kernel, tools, salvage, resilience, report,
prompt-cache, probes, tool shapes) passes after the split.

### Totals after CLI + engine + browser

| Metric | Baseline | Current |
|---|---:|---:|
| Product modules | 164 | 190 |
| Physical lines | 87,689 | 88,043 |
| Largest file | 3,703 (`browser/session.py`) | 2,392 (`bridge_launcher.py`) |
| `browser/session.py` | 3,703 | 173 |
| `engine_kernel.py` | 2,905 | 753 |
| `cli.py` | 2,299 | 193 |
| Package budget | 88,000 | 90,000 (measured split headroom) |

The remaining biggest files are `bridge_launcher.py` (2,392),
`bridge_proxy.py` (2,218), `repo_context.py` (2,055), `tui/app.py` (2,038),
`engine_tool_env.py` (1,864), `core_tools.py` (1,802) and `web_check.py`
(1,831); they are the next batches.

## After — tools batch: engine tool bodies

`engine_tool_env.py` 1,864 → 1,413 lines; the pure result/observation body
renderers (fence salvage, layout/refusal notices, compact diffs, mutation and
check bodies, edit/command normalization) move to `engine_tool_bodies.py`
(544 lines) and stay re-exported through the environment module for the tests
that import them there. The new module is on the model-facing neutrality scan.
Engine tool/body/render/layout/check/probe tests pass.

## After — verification: check_report split

`check_report.py` 1,293 → 295 lines. Runner parsers (pytest/unittest/Jest/
Vitest/node/go/cargo/ruff/mypy/tsc), normalisation and the `Failure` record
move to `check_parsers.py` (829 lines); focused test-target selection and the
import scan move to `check_targets.py` (271 lines). `check_report.py` keeps the
public `analyze`, `fingerprints`, `render_failure_map`, `is_test_path`,
`render_targets`/`rerun_targets` surface and re-exports the historical names.
The Failure dataclass decorator and every regex constant moved with their
owners. Check/probe/repo-safety/coverage/render tests pass.

## After — verification: completion gate split

`completion_gate.py` 1,278 → 565 lines. The shared predicates (requirements,
docs, test-edit rules, diagnostics, run identity) move to
`completion_rules.py` (360); visual/web intent and proof move to
`completion_visual.py` (317); whole-change review and run identity to
`completion_review.py` (199). `floor`, coverage, setup/runtime classification
and the attestation/unproducible predicates stay in the gate. All historical
public names are re-exported; the new modules join the model-facing neutrality
scan. Completion/coverage/probe/neutrality tests pass.

## After — repository context split

`repo_context.py` 2,055 → 148 lines: `RepositoryContextEngine` is a facade over
`RepoIdentityMixin` (revision identity/cache, 628 lines), `RepoSearchMixin`
(files/grep/ripgrep/symbols/imports, 736) and `RepoInspectMixin` (ranking,
excerpts, edit context, inspect, 394); shared constants/budgets/match selection
live in `repo_support.py` (407). The public constructor and method names are
unchanged; core-smoke and engine context/inspect tests pass.

## After — core runtime split

`core.py` 1,573 → 453 lines. `CoreRuntime` composes `CoreExecuteMixin`
(execute/risk/replay/session load, 336), `CoreValidationMixin`
(normalization + JSON-schema validation, 508) and `CoreFilesMixin`
(safe paths, read/write/list, mutation evidence, audit, 425); support types
(`VerificationRule`, write modes, ripgrep lookup) live in `core_support.py` (99).
The facade re-exports the historical names other modules/tests import
(`CoreCommand`, `InvalidCommand`, `InvalidPath`, `CoreError`, `ToolDefinition`,
`SessionError`, `_path_label`, `VerificationRule`). Core command/validation/
safety/smoke and engine-tools tests pass.

## Next

Providers, `core_tools`/`core_process`, TUI, `web_check`, then the bridge with
its S0 characterization tests.

| Metric | Baseline | Current |
|---|---:|---:|
| Product modules | 164 | 183 |
| Physical lines | 87,689 | 87,804 |
| Largest file | 3,703 (`browser/session.py`) | 2,392 (`bridge_launcher.py`) |
| `browser/session.py` | 3,703 | 173 |
| `engine_kernel.py` | 2,905 | 2,300 |
| `cli.py` | 2,299 | 193 |
| Package budget | 88,000 | 90,000 (measured split headroom) |

The remaining biggest files are now `engine_kernel.py` (2,300),
`bridge_launcher.py` (2,392), `bridge_proxy.py` (2,218), `repo_context.py`
(2,055), `tui/app.py` (2,038) and `engine_tool_env.py` (1,864); they are the
next batches.

## Next

Engine steps 2–7, then the bridge characterization tests before its split,
then providers, repo_context/TUI and web_check.
