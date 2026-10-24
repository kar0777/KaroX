# Migration from v5

The release is a curated extraction, not a rewrite of the agent. The kernel
that passed the v5 tests is copied unchanged where possible; the command
surface and terminal client are new because those were the parts carrying the
most legacy weight.

## Source

- Source tree: `D:\проекты\KaroX-v5` (v5 working tree, HEAD `ee6a0ab` plus the
  local changes present on 2026-10-23).
- Copy rule: follow intra-package imports from the agent kernel, tools, policy,
  providers, sessions, MCP, skills and the new client; stop at any module that
  is a hosted, browser-bridge, orchestration or legacy-shell concern.
- One edge was cut on purpose: `mcp_client._normalize_discovered_tools` no
  longer imports `hosted_bridge.KNOWN_HOSTED_TOOL_NAMES`. That branch only
  restores dotted names for a KaroX-hosted MCP server, which this release does
  not ship.

## Copied unchanged

| Area | Modules |
| --- | --- |
| Agent kernel | `agent.py`, `core.py`, `context_compiler.py`, `cache_scheduler.py`, `semantic_shadow.py`, `base_probe.py`, `completion_gate.py`, `requirements.py`, `tool_universe.py` |
| Tools | `core_tools.py`, `repo_context.py`, `edit_engine.py`, `action_execution.py`, `auto_verify.py`, `affected_checks` behavior inside the kernel, `verification.py`, `test_output.py`, `lsp_diagnostics.py`, `hot_worker.py`, `artifacts.py`, `web_check.py`, `visual_check.py`, `browser_bootstrap.py` |
| Policy | `policy.py`, `risk_engine.py`, `risk_mapping.py`, `action_policy.py`, `command_guard.py`, `agent_modes.py`, `effort.py`, `effort` budget tables |
| Providers | `providers.py`, `provider_adapters.py`, `provider_factory.py`, `provider_controller.py`, `provider_presets.py`, `provider_pricing.py`, `registry.py`, `models.py`, `credentials.py`, `credential_session.py`, `credential_secret_service.py` |
| Sessions | `sessions.py`, `event_bus.py`, `usage_analytics.py`, `cost_intelligence.py` |
| MCP | `mcp_client.py`, `mcp_status.py`, `mcp_oauth.py` |
| Skills | `skills.py`, `builtin_skills/`, `skill_notes/`, `prompts/` |
| Platform | `paths.py`, `security.py`, `detached_process.py`, `process_launcher.py` |

`disk_maintenance.py`, `hot_worker.py` and `browser_bootstrap.py` remain only
because a copied tool imports them; the release does not expose a maintenance
mode or a browser-control command.

## New in the release

- `cli.py` — a compact command surface: agent run, sessions, providers, models,
  MCP, skills, credentials, paths. It constructs `CapabilityCoreRuntime` with
  the same capability grants and risk engine as v5, but without the hosted
  options.
- `tui/` — the rebuilt client. 8 files, one stylesheet, no import of the agent
  runtime. It talks to `agent run --stream-json`.
- `agent run --stream-json` — one typed event per stdout line and one final
  result document. v5 had human progress on stderr and a pretty `--json`
  document at the end; neither could drive a live UI.
- Project instruction files (`AGENTS.md`, `CLAUDE.md`, `KAROX.md`) are read
  directly and appended to the system prompt; the report names the files that
  shaped the run.
- The skill catalogue is advertised to the agent and `skill_load` is available
  through Core.
- `provider setup` verifies the endpoint with a minimal request before storing
  the key.

## Not ported in 6.0.0a1

These remain in v5 and are deliberate gaps, not accidental omissions:

- **karo-core engine.** `karox core run` and the vendored `engine/` are not in
  this release. The product kernel is the agent here; the bash-oriented engine
  needs a typed-tool adapter before it can replace it.
- **Project map and research subagents.** Only the repository instruction files
  are composed into the prompt. Dependency-aware project mapping, file
  mentions, memory blocks and research branches are not wired.
- **Visual checker injection.** `web_check --render` is still available as a
  verification command, but the kernel does not receive a `VisualChecker`.
- **Per-session skill selection.** `skill list/show/load` works and the agent
  may load a skill, but `skill select/deselect` and skill-level capability
  grants are not exposed.
- **Auto-checkpoint, offline/online checkpoint stores, handoff documents.**
  Sessions are durable and resumable; Git rollback checkpoints are not created
  automatically.
- **Image attachments, maintenance mode, `--route`, cost ceilings, research
  budgets, hosted approval.** None of the hosted surfaces exist here.
- **MCP `ask` as an interactive prompt.** A tool marked `ask` is refused until
  the session selection is edited. The release does not pretend otherwise.
- **Old terminal shell.** `tui.py`, `tui_connections.py`, `tui_dashboard.py`,
  `tui_workspace.py`, `tui_onboarding.py` (~26k lines) are gone.

## How to extend the copy

When a v5 module becomes necessary:

1. Decide which layer it belongs to in [ARCHITECTURE.md](ARCHITECTURE.md).
2. Copy the module and its intra-package imports; run
   `python scripts/check_tree.py` to find a missing edge.
3. Remove any hosted/bridge/orchestration branch it carries rather than copying
   that dependency back in.
4. Add a test for the behavior the release now relies on. Do not add a module
   just because it was next to a copied one.

## Validation of this extraction

- 22 release tests pass (`pytest`).
- `ruff check src tests` passes.
- `mypy` passes on the new CLI and client.
- Live agent, routed through an isolated gateway configuration:
  task created `hello.txt`, `python -m pytest -q` passed, report
  `verified: true`, one step.
- Live terminal client, same task: `verified`, 6 steps, 2/2 checks, ~44k tokens,
  file content correct.
- Wheel built, inspected for data files, installed into a fresh virtual
  environment and used for the same task: `verified`, one step.
- Release TUI tests replay recorded events and never touch a model, network or
  keyring.