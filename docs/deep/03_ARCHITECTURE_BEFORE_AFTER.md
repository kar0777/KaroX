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

## Next

Engine split step 1 (`_EngineAgentMixin`; concrete `EngineAgent` shell stays in
`engine_kernel.py`), then browser `contract.py`, then bridge characterization
tests. The before/after table is updated after each landed batch.
