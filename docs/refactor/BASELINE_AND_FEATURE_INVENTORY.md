# Baseline and product feature inventory

**Mission:** KAROX_V5_STABLE_REFACTOR_GOAL
**Worktree:** `b/karox5-refactor`
**Branch:** `goal/karox5-refactor-20261107`
**Baseline commit:** `c4bc342382149c254f1a1ea08aeeeccd21c4e246`
**Baseline tree:** `1cec72297c941134a0de1300eef6acc68792697f`
**Recorded:** 2026-11-07, Windows, Python 3.13.11

This inventory was created **before any product deletion or refactor**. It is the
freeze record for contract and behavior parity.

## 1. Frozen baseline

| Item | Value |
|---|---|
| Package name | `karox-runtime` |
| Import package | `karox` |
| `__version__` | `6.0.0a3` |
| Console script | `karox = karox.entrypoint:main` |
| Source modules | **162** `*.py` |
| Source physical lines | **87,508** |
| Source non-empty lines | **78,795** |
| Source comment lines | **4,374** |
| Test files | **77** `*.py` |
| Test physical lines | **38,105** |
| Test non-empty lines | **32,424** |
| Collected tests | **1,527** |
| `check_tree` | 162 modules, 87,508 lines, no legacy surface |
| Baseline wheel | `karox_runtime-6.0.0a3-py3-none-any.whl` |
| Wheel bytes | **1,147,638** |
| Wheel SHA-256 | `6a795a2da912935629ea0819e23999d248a1499ca75173fd0b2d49624519dd69` |
| Import cycles (AST, internal) | **0** |
| Environment | isolated config/runtime under `local-runs/karox5-baseline`, no provider key |

Raw metadata is in `local-runs/karox5-baseline/baseline_metadata.json`. Fresh-install smoke used a separate venv under `local-runs/karox5-baseline/venv`; no model was configured.
Raw import graph is in `local-runs/karox5-baseline/import_graph.json`.

### Baseline gate status before refactor

| Gate | Result | Evidence |
|---|---|---|
| `ruff check src tests scripts` | **PASS** | All checks passed |
| `mypy cli.py entrypoint.py tui` | **PASS** | 13 source files, no issues; one unused-override note |
| `scripts/check_tree.py` | **PASS** | 162 modules · 87,508 lines |
| Headless audited subset | **PASS** | 64 passed in 3.65 s |
| Full pytest | **FAIL / HANG recorded** | clean-root run reached 333 passed + 4 skipped, then hung in browser dialog fixture; owner-thread marshalling had no timeout. First failure seen earlier was an environment-override artifact, not product. |
| Wheel build | **PASS after installing setuptools** | wheel above |
| Wheel install in fresh venv | **PASS** | `venv` + wheel install; `karox --version`, `paths`, `agent run --help`, `tui --help`, `doctor --json` all exit 0 |

The full clean baseline was stopped after the reproducible browser hang. `BrowserSession.dispatch` waited forever on a `Future` while its owner thread sat in `Queue.get`; this is recorded as a product defect and fixed fail-closed in candidate commit `09ae58c`.\n\nThe environment initially lacked `setuptools` and `build`; this is an
environment prerequisite, not a product result. After installing them the wheel
built successfully. The earlier full run was restarted because it was launched
from `b/win`, which made pytest use the wrong root; the corrected run uses
`workdir=b/karox5-refactor`.

## 2. Public surfaces

### 2.1 CLI root commands

Declared in `src/karox/cli_parser.py`, dispatched in `src/karox/cli.py`:

```text
tui
agent run
session create|list|show|export|fork|rename|archive|unarchive|revoke|delete|lock
provider presets|local|catalog|catalog-sync|connect|add|edit|remove|list|show|details|test|credential-set|credential-clear|setup
model list|inspect|select|add|discover|map|repair-selection|remove|test
mcp server add|remove|show|authorize|inspect|doctor|list
mcp session select|deselect|permissions
mcp status|call
mcp credential set|show|delete|doctor
bridge connect|serve|saved|status|start|stop|restart|list|show|doctor|oauth|credential
skill list|show|load
credential set|show|delete|doctor|setup
doctor
status
paths
```

`karox --version` reports `karox 6.0.0a3` at baseline.

### 2.2 Agent run contract

`agent run` is the primary product entrypoint. It currently accepts:

- repository, task, session id / continue
- model, base URL, API-key env, routes and route strategy
- privacy limit, total-token and cost ceilings
- verification-command (repeatable, JSON or shell-quoted)
- access profiles `read_only|browser_control|workspace_write|elevated`
- effort `auto|low|medium|high|extra-high|ultra`
- mode `build|plan|ideate`
- project context, memory, probes opt-outs
- max steps/seconds/context/output, vision and images
- stream, NDJSON stream, discovered-check acceptance
- MCP server attachment with read/all permission
- browser plain credentials and JSON output

The terminal client deliberately does **not** import the runtime; it spawns this
command and consumes one JSON event per line plus a final `result` document.

### 2.3 Tool surface

The agent advertises typed tools; the exact names are a public model contract.
Core families seen in `core.py`, `core_tools.py`, `tool_schemas.py`,
`engine_tools.py` and the hosted bridge mapping:

| Family | Examples | Consumer |
|---|---|---|
| repository | `repo.read_file`, `repo.read_lines`, `repo.write_file`, `repo.edit_file`, `repo.list_files`, `repo.search` | engine Core, hosted bridge |
| process/check | `repo.command`, `checks.run`, `tests.run` | engine Core, hosted bridge |
| Git | `git.status`, `git.diff`, `git.log`, `git.commit`, guarded `git.push` | Core, hosted bridge |
| editor/patch | `repo.apply_patch`, windowed reads, plan tools | extended Core |
| language/service | `lsp.diagnostics`, `web.fetch`, `runtime.status` | Core, hosted bridge |
| browser/desktop | `browser.command`, desktop tools | engine tool registration |
| skills | `skill.list`, `skill.load`, `skill.run` | engine/Core |
| hosted aliases | `karox.repo.*`, `karox.checks.*`, `karox.command.*` | hosted bridge wire |

Hosted bridge aliases must not be renamed as part of a local refactor: existing
connectors may persist them.

## 3. Capability inventory

Status vocabulary: **PASS** = executed and evidence captured; **DEFINED** =
source/tests exist but the full baseline gate is still running; **NOT TESTED** =
not executed in this environment; **FAIL** = reproducible failure.

| # | Capability | Public entrypoint | Owner modules | Tests / evidence | Contract risk | Status |
|---|---|---|---|---|---|---|
| 1 | Local interactive client | `karox tui` | `tui/*` | `test_tui.py` (95), `test_layout_recheck.py` | NDJSON, terminal behavior | DEFINED |
| 2 | Native agent loop | `karox agent run` | `engine_kernel.py`, `engine/`, `agent.py` | `test_engine_kernel.py`, port tests | model protocol, step semantics | DEFINED |
| 3 | No-false-VERIFIED proof | `agent run` result | `completion_gate.py`, `requirement_coverage.py`, `session_analysis.py` | `test_completion_gate.py`, `test_requirement_coverage.py`, engine kernel | final result schema, evidence | DEFINED |
| 4 | Approved check execution | `--verification-command` | `core_process.py`, `verification.py`, `check_report.py` | `test_check_probes.py`, `test_check_report.py`, `test_core_commands.py` | command/evidence schema | DEFINED |
| 5 | File/edit/patch tools | tool calls | `core.py`, `core_tools.py`, `edit_engine.py`, `unified_patch.py` | `test_core_modules_smoke.py`, `test_unified_patch_leniency.py` | tool schemas, atomic writes | PASS (tool-shape/headless) |
| 6 | Git status/diff/log/commit/push guard | tool calls | `core_git.py`, `git_baseline.py` | `test_command_jobs.py`, `test_repo_command_safety.py`, engine tests | baseline semantics, push guard | DEFINED |
| 7 | Policy + risk + approval | all mutating tools | `policy.py`, `risk_engine.py`, `risk_mapping.py`, `action_policy.py`, `command_guard.py` | `test_policy.py`, `test_action_policy.py`, `test_repo_command_safety.py` | capability names, denied paths | DEFINED |
| 8 | Providers: OpenAI Chat/Responses, Anthropic, Gemini | `agent run`, `provider *`, `model *` | `providers.py`, `provider_adapters.py`, `provider_factory.py` | `test_providers.py` (111 KB), `test_engine_provider_resilience.py` | wire formats, streaming, usage | DEFINED |
| 9 | Routing/fallback/budgets | `--route*`, `--max-*` | `routing.py`, `provider_controller.py`, `provider_pricing.py` | `test_provider_catalog.py`, `test_provider_pricing.py`, `test_run_economy.py` | route order, cost semantics | DEFINED |
| 10 | Credential/keyring refs | `credential *`, `provider credential-*` | `credentials.py`, `credential_session.py`, `credential_secret_service.py` | `test_provider_registry.py`, CLI tests | keyring names, redaction | DEFINED |
| 11 | Durable sessions/resume/export | `session *`, `--continue-task` | `sessions.py`, `session_export.py`, `event_bus.py` | `test_sessions.py` (26), `test_session_export.py` (4) | JSON schema, IDs, migration | PASS (export/headless) |
| 12 | Project context/map/memory | agent run defaults; `--no-*` | `project_context.py`, `project_map.py`, `map_service.py`, `memory.py` | `test_engine_port_context.py`, `test_engine_kernel.py` | bounded prompt content | DEFINED |
| 13 | Plan/Ideate artifacts/images | `--mode`, `--image` | `mode_artifacts.py`, `image_attachments.py`, `artifacts.py` | engine/context tests | stored artifact schema | DEFINED |
| 14 | Skills | `skill list/show/load`; `skill.run` | `skills.py`, `skill_runtime.py`, `builtin_skills/` | `test_skill_runtime.py` (24), `test_skill_scope.py` (4) | catalog, capability scope | PASS (scope/headless) |
| 15 | MCP outbound | `mcp server/session/status/call` | `mcp_client.py`, `mcp_status.py`, `mcp_oauth.py` | `test_mcp_client.py`, `test_mcp_cli.py`, `_mcp_*.py` | tool schemas, OAuth, idempotency | DEFINED |
| 16 | Hosted bridge/OAuth/Tailscale | `bridge *` | `bridge_*`, `hosted_core.py`, `port_ownership.py`, `tailscale_routes.py` | `test_bridge_core.py`, `test_bridge_oauth.py`, `test_bridge_launcher.py`, `test_tailscale_routes.py` | saved profiles, wire, route ownership | DEFINED |
| 17 | Browser/Playwright | `browser.command`; browser profile | `browser/*`, `web_check.py`, `visual_check.py` | `test_browser_core.py`, `test_browser_units.py`, `test_browser_live.py`, visual tests | credentials, network policy, screenshots | DEFINED (Windows browser availability pending) |
| 18 | Desktop/Win32 | desktop tool family | `computer_use/*`, `desktop_apps.py` | `test_computer_use.py`, `test_computer_use_live.py` | non-intrusive input, approval | DEFINED (Windows desktop) |
| 19 | TUI protocol/rendering | `karox tui` | `tui/` | `test_tui.py` (95) | events, localization, cards | DEFINED |
| 20 | Doctor/status/paths | `doctor`, `status`, `paths` | `cli.py`, `paths.py`, diagnostics | `test_cli.py`, `test_local_runtimes.py` | JSON shape | PASS (baseline output captured) |
| 21 | Package data/resources | wheel | `prompts/`, `builtin_skills/`, `data/`, `tui/theme.tcss`, engine license | `test_package_data.py` (5), `test_tree.py` | wheel must contain resources | PASS (headless) |
| 22 | Installers/update aliases | `scripts/install.ps1`, `install.sh` | installers, `pyproject.toml` | `test_installers.py` | side-by-side KaroX 5/6, pipx names | DEFINED |
| 23 | Archived/hosted commands | `bridge`, `cli.py` allowlists | `cli.py`, `hosted_core.py`, `bridge_proxy.py` | bridge tests | tool-name aliases, OAuth bindings | DEFINED |
| 24 | Benchmark harness (separate) | `scripts/race_task.py` | `scripts/race_task.py` | `test_race_task.py` | not product surface | DEFINED |

The inventory is intentionally behavior-first. A module is not a capability just
because it exists, and a capability is not dead because one test import list
does not mention it.

## 4. Storage, wire and security contracts

| Contract | Location | Preservation requirement |
|---|---|---|
| Config root | `paths.config_dir()/vnext/` | existing providers/MCP/UI/bridge profiles readable |
| Runtime root | `paths.runtime_dir()/vnext/` | sessions, audit, session exports, resume |
| OAuth root | `paths.oauth_state_dir()` | bridge registration/grant continuity |
| Credentials | OS keyring + opaque `os-keyring:`/`env:` refs | never migrate a raw key into a registry |
| Sessions | session JSON under runtime | old version 1/2 records must still load or fail closed with a migration |
| CLI wire | one JSON object per line; final `result` | event names and fields |
| Hosted wire | MCP Streamable HTTP + OAuth 2.1 + DCR/PKCE | existing ChatGPT/Claude/HyperAgent connectors |
| Tool names | `repo.*`, `git.*`, `checks.*`, hosted `karox.*` | schemas and aliases |
| Package data | prompts, skills, catalog, tcss, engine license | present in wheel |
| Installer aliases | `karox` vs side-by-side command | no silent replacement of a user install |

## 5. Known documentation drift at baseline

Real code and deployed contracts win over prose. Observed at `c4bc342`:

- `AGENTS.md` calls the tree “KaroX 6”; the goal is a stable 5.x line.
- `docs/DEVELOPMENT.md` says the suite collects 1,018 tests; the actual
  collection is **1,527**.
- `docs/PARITY.md` says the vendored engine, browser and desktop were
  deliberately buried; `docs/ARCHITECTURE.md` and the source show the engine,
  browser and desktop paths present at 6.0.0a3. The two documents describe
  different historical releases.
- `docs/INSTALL.md` documents KaroX 6 side-by-side installation, not a stable
  5.x migration.
- `README_RU.md` is absent in this baseline even though the release checklist
  expects localized release documentation.
- `docs/DEVELOPMENT.md` names `integrate` as the working tree; this goal uses
  the isolated `b/karox5-refactor` worktree instead.

These are release/documentation gaps to fix, not proof that source is dead.

## 6. Refactor constraints from baseline

1. No deletion before the full inventory and reachability evidence.
2. No change to tool names, CLI flags, session storage, keyring names, hosted
   aliases or event names without a compatibility shim and regression test.
3. No silent package-name/version migration; installed KaroX 5/6 state must
   remain readable.
4. The TUI must remain a subprocess client with no runtime import.
5. Every mutating tool must continue through Core capability + risk; no new
   shortcut.
6. The vendored `engine/` remains vendored; behavior changes belong around it.
7. The full gate must be run in this worktree, not in `b/win` or `integrate`.

## 7. Next baseline actions

1. Finish the full pytest baseline, record first failure and final summary.
2. Fresh-venv install the baseline wheel and run CLI/TUI smoke.
3. Ask the independent read-only auditors to challenge this inventory.
4. Complete `DEAD_CODE_EVIDENCE.md` before the first deletion.
5. Only then start reversible removals and checkpoint commits.


