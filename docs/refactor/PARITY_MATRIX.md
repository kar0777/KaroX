# Parity matrix — frozen baseline vs refactor

Status is deliberately not green-coded until the gate has actually run. At the
initial freeze the full suite and fresh install are still in progress, so the
matrix records the test **contract** and the evidence path, not a victory claim.

Legend: **PASS** run and captured; **DEFINED** contract/test exists, full gate
pending; **NOT TESTED** not runnable/pending; **FAIL** reproducible; **N/A**
platform not present.

| Capability | Contract test | Behavior scenario | Baseline | Target evidence |
|---|---|---|---|---|
| CLI/help/version | `test_cli.py`, `test_argument_validation.py` | root help, version, every parser default | DEFINED | before/after help diff + parser tests |
| Agent run flags | `test_engine_kernel.py`, `test_cli.py` | flags accepted, no-model failure is clean | DEFINED | same command and result schema |
| Agent loop | `test_engine_kernel.py` | simple synthetic task, tool call, final | DEFINED | frozen transcript replay |
| Verification proof | `test_completion_gate.py`, `test_check_probes.py` | approved check passes/fails; no false VERIFIED | DEFINED | same report fields and negative tests |
| Tools/file/Git | `test_core_modules_smoke.py`, `test_command_jobs.py` | read/write/edit/patch/status/diff | DEFINED | tool schema and path tests |
| Policy/risk | `test_policy.py`, `test_action_policy.py`, `test_repo_command_safety.py` | denied target, approval required, safe command | DEFINED | denied/approved parity matrix |
| Providers | `test_providers.py`, `test_engine_provider_resilience.py` | OpenAI/Anthropic/Gemini streaming, tools, usage, errors | DEFINED | recorded fake transports + live limited smoke |
| Routing/budget | `test_run_economy.py`, `test_provider_catalog.py` | fallback, cost/token cap, privacy | DEFINED | same route decisions |
| Credentials | `test_provider_registry.py`, CLI tests | keyring ref, redaction, no raw key | DEFINED | redaction and refs unchanged |
| Sessions/resume/export | `test_sessions.py`, `test_session_export.py` | create, resume, fork, export, old record | PASS (export) | old/new records load |
| Context/memory/map | `test_engine_port_context.py` | bounded context, opt-out, no stale memory | DEFINED | prompt budget diff |
| MCP outbound | `test_mcp_client.py`, `test_mcp_cli.py` | stdio/HTTP, allow/ask/deny, replay | DEFINED | saved config and wire |
| Hosted bridge/OAuth | `test_bridge_*.py`, `test_tailscale_routes.py` | discovery, DCR, PKCE, refresh, tools/call, profile | DEFINED | local loopback wire + saved profile |
| Browser | `test_browser_*.py` | local page, snapshot, download, policy | DEFINED | real local page only |
| Desktop | `test_computer_use*.py` | non-intrusive input, refusal | DEFINED | Windows-only if available |
| Skills | `test_skill_runtime.py`, `test_skill_scope.py` | catalog, load, scope, run | PASS (scope) | same catalog names |
| TUI | `test_tui.py`, `test_layout_recheck.py` | stream replay, cards, errors | DEFINED | recorded event replay |
| Package data | `test_package_data.py`, `test_tree.py` | wheel contains prompts/skills/theme/license | PASS | wheel listing diff |
| Install/migration | `test_installers.py` | side-by-side alias, dry run, no overwrite | DEFINED | fresh install + rollback |
| Doctor/status/paths | `test_cli.py`, `test_local_runtimes.py` | JSON shape, no secrets | PASS (baseline outputs) | key-set diff |
| Benchmark harness | `test_race_task.py` | separate, not product | DEFINED | no product imports |

## Contract invariants

- CLI flag spellings and defaults.
- One JSON event per line and final `{"type":"result","report":...}`.
- Tool schemas and names (including hosted `karox.*` aliases).
- Session/config/keyring/OAuth names.
- Wheel resource set.
- `verified: true` only with real evidence.

## Behavior invariants

- No hidden test/grader access.
- No raw provider key in any durable or guest-visible artifact.
- No mutation bypasses capability + risk.
- Failed checks never become verified.
- Session resume and export survive a process restart.

## Baseline gate state

- Headless audited subset: **64 passed**.
- `ruff`, scoped `mypy`, `check_tree`, wheel build: **PASS**.
- Full suite: **running**, 1,527 collected.
- Fresh wheel install: **pending**.
- All other statuses stay DEFINED until those runs complete.
