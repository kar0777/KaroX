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

## Next

Browser mixin extraction (runtime → navigation → collectors → observation →
interaction → waits/evidence → launch/lifecycle), engine steps 2–7, then the
bridge characterization tests before its split.
