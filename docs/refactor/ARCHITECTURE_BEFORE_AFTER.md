# Architecture before/after

**Baseline:** `c4bc342` (162 modules, 87,508 physical lines).
**After:** not yet available; this file is updated after each split.

## Baseline dependency facts

The AST graph over all 162 product modules (relative imports included, imports
inside functions counted as edges because they are still dependency edges)
contains:

- **432 internal import edges**
- **6 strongly connected components**
- **0 layer violations** under the intended direction
  (UI → CLI → kernel/tools → policy/providers/sessions/MCP → platform)
- top fan-out: `cli` (52), `engine_kernel` (19), `hosted_core` (17),
  `core_tools` (15), `engine_tool_env` (14), `core` (13),
  `bridge_launcher` (13), `bridge_proxy` (11), `browser.session` (10)

Raw graph: `local-runs/karox5-baseline/architecture_graph.json`.

### Cycles at baseline

| Cycle | Kind | Initial assessment |
|---|---|---|
| `command_jobs → core_git → core_process → core → core_tools` | function-level imports for job stores and launcher helpers | lazy by design; still a maintainability cycle |
| `engine_tool_env → engine_kernel → agent` | engine integration back-reference | likely breakable with a narrow protocol/facade |
| `credential_secret_service → credential_session → credentials` | Linux Secret Service backend | platform-specific cycle; needs a protocol boundary |
| `browser.guards → browser.session → browser.tools → browser` | package `__init__` eager exports | break with lazy package exports |
| `tui.app → tui` | package `__init__` eager `run_tui` | break with lazy package exports |
| `computer_use.adapter → computer_use` | package `__init__` eager exports | break with lazy package exports |

The last three are package initializer cycles, not domain cycles. The first
three are lazy function-level back-edges. Neither is automatically a bug, but
the goal requires reducing hidden coupling without changing behavior.

## Intended dependency direction

```text
UI / TUI
  ↓
CLI command surface
  ↓
agent kernel / engine integration
  ↓
Core tools + policy/risk/verification
  ↓
providers / sessions / MCP / context
  ↓
platform (paths, security, processes, keyring)
```

The bridge is a second entry point into Core and must not bypass policy. The
browser/desktop integrations register behind the same Core gate.

**Correction after source audit:** the TUI does not import `karox.agent` or the
engine, but it does import the pure helpers `karox.python_tests` and, lazily,
`karox.check_report` for verification suggestions and failure presentation.
The earlier statement that it imports *nothing* from the kernel was too strong.
The boundary is narrowed to: no agent loop, provider, Core runtime or session
store import. Either those helpers move behind a CLI/JSON surface or the rule
must explicitly allow a neutral presentation utility; the final architecture
will state which one was chosen.

## Baseline module shape

| Size band | Files |
|---|---|
| >2,000 physical lines | browser/session.py, engine_kernel.py, cli.py, bridge_launcher.py, bridge_proxy.py, repo_context.py, tui/app.py |
| 1,000–2,000 | engine_tool_env.py, web_check.py, core_tools.py, providers.py, core.py, bridge_tailscale.py, provider_adapters.py, bridge_oauth.py, completion_gate.py, check_report.py, hosted_core.py, mcp_client.py, core_process.py, bridge_profiles.py |
| 500–999 | disk_maintenance.py, tui/widgets.py, engine_provider_bridge.py, skills.py, tui/i18n.py, sessions.py, requirement_coverage.py, routing.py, computer_use/adapter.py, map_service.py, registry.py, model_discovery.py, tool_schemas.py, core_git.py, core_search.py, skill_runtime.py, provider_controller.py, memory.py, command_jobs.py, port_ownership.py, risk_mapping.py, action_policy.py, visual_check.py |

Physical counts are the release gate count; non-empty counts differ.

## Planned split order

1. **Lazy package facades** for `browser`, `tui`, `computer_use` — removes
   package-init cycles, preserves public imports, no behavior change.
2. **CLI command groups** — extract bridge/help/output into focused modules with
   a `cli` compatibility facade. Keep `main` and parser stable.
3. **Engine integration** — extract pure verification/event helpers from
   `engine_kernel.py` only where a behavioral test can be written first.
4. **Browser session** — extract action/observation policy only after a
   transcript test proves the exact contract; do not split Playwright I/O
   mechanically.
5. **Bridge lifecycle** — separate saved-profile/config validation from
   process/tunnel ownership after local loopback tests.

Each step must add no new reverse edge and must reduce or hold the cycle count.
The after graph and before/after table are filled in only after gates pass.

## After the first candidate

At `f3bceb0` the AST graph has:

- **164 modules** (two CLI modules added), **424 internal edges**;
- **4 cycles**, down from 6:
  - `command_jobs → core_git → core_process → core → core_tools`
  - `agent → engine_kernel → engine_tool_env`
  - `credential_secret_service → credential_session → credentials`
  - `browser.guards → browser.session`
- the `browser`/`tui`/`computer_use` package-initializer cycles are gone;
- `cli` fan-out is 50 (from 52) because the bridge group was extracted; the
  remaining fan-out is the command dispatcher itself.
- `check_tree`: 164 modules, 87,671 lines.

This is a real reduction in package-initializer coupling, not a claim that the
four remaining lazy/domain cycles are harmless. The next split should target the
browser guard/session cycle or the engine integration boundary, with a
behavioral test first.

### Import-time effect

Measured with `python -X importtime -c "import <module>"` on the same host:

| Import | Baseline c4 | Candidate |
|---|---:|---:|
| `karox.browser` | 163,047 µs | **15,118 µs** |
| `karox.tui` | 543,488 µs | **25,320 µs** |
| `karox.computer_use` | 91,360 µs | **15,798 µs** |

Public names and submodule imports are pinned by
`tests/test_public_package_facades.py`; the improvement is not achieved by
removing the actual browser/TUI/desktop modules.

## First candidate changes

- Lazy public facades in `browser/__init__.py`, `tui/__init__.py` and
  `computer_use/__init__.py`. Public imports still resolve to the same
  submodule objects; new `tests/test_public_package_facades.py` pins identity
  and submodule paths.
- `BrowserSession` owner-thread marshalling is now bounded. A lost owner queue
  raises `BrowserSessionError` after the action deadline instead of blocking
  the process forever. This is a fail-closed shutdown fix, not a root-cause
  proof of the queue loss.
- CLI plural aliases expand for flags (`providers --json` works), saved-create
  rejects `--tunnel auto` before writing, and saved OAuth lookup checks the v5
  legacy keyring candidates.
- Hosted multi-project runtime no longer passes the nonexistent
  `checkpoint_factory`/`_checkpoint_factory_for`; a two-project regression test
  covers the previously broken non-anchor path.
- JSON session export no longer truncates lists at 500 entries; display
  redaction still caps by default.

These need the complete gate before they count as a stable split.



