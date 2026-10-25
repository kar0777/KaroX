# Migration from v5

The release is a curated extraction, not a rewrite of the agent. The kernel
that passed the v5 tests is copied unchanged where possible; the command
surface and terminal client are new because those were the parts carrying the
most legacy weight.

## Source

- Source tree: `D:\проекты\KaroX-v5` (v5 working tree, HEAD `ee6a0ab` plus the
  local changes present on 2026-10-23).
- Copy rule: follow intra-package imports from the agent kernel, tools, policy,
  providers, sessions, MCP in both directions, skills and the new client; stop
  at browser-control, autonomy, orchestration and legacy-shell concerns.
- The hosted MCP bridge was initially excluded, then restored after the parity
  audit because ChatGPT Web/Claude Web/HyperAgent connectivity is a product
  requirement, not a laboratory feature. Its security modules were ported with
  new names and without browser/autonomy/hosted-tools runtimes.
- One edge remains cut: `mcp_client._normalize_discovered_tools` no longer
  imports `hosted_bridge.KNOWN_HOSTED_TOOL_NAMES`. The hosted bridge's own wire
  mapper handles underscore/dotted names; external MCP server names are
  preserved byte-for-byte.

## Copied unchanged

| Area | Modules |
| --- | --- |
| Agent kernel | `agent.py`, `core.py`, `completion_gate.py`, `requirements.py`, `session_analysis.py` |
| Tools | `core_tools.py`, `repo_context.py`, `edit_engine.py`, `action_execution.py`, `auto_verify.py`, `verification.py`, `test_output.py`, `lsp_diagnostics.py`, `hot_worker.py`, `workspace_worker.py`, `workspace_transaction.py`, `unified_patch.py`, `artifacts.py`, `web_check.py`, `visual_check.py`, `browser_bootstrap.py` |
| Policy | `policy.py`, `risk_engine.py`, `risk_mapping.py`, `action_policy.py`, `command_guard.py`, `agent_modes.py`, `effort.py`, `effort_signals.py`, `mode_artifacts.py` |
| Providers | `providers.py`, `provider_adapters.py`, `provider_factory.py`, `provider_controller.py`, `provider_presets.py`, `provider_pricing.py`, `routing.py`, `model_discovery.py`, `registry.py`, `models.py`, `credentials.py`, `credential_session.py`, `credential_secret_service.py` |
| Sessions and context | `sessions.py`, `event_bus.py`, `usage_analytics.py`, `memory.py`, `project_context.py`, `project_map.py`, `map_service.py`, `image_attachments.py` |
| MCP outbound | `mcp_client.py`, `mcp_status.py`, `mcp_oauth.py` |
| MCP inbound bridge | `hosted_core.py` (from `hosted_bridge.py`), `bridge_oauth.py`, `bridge_proxy.py`, `bridge_serve.py`, `bridge_launcher.py` (from `web_bridge_launcher.py`), `bridge_profiles.py`, `bridge_tailscale.py`, `tailscale_routes.py`, `port_ownership.py`, `process_identity.py`, `route_health.py`, `browser_approval.py`, `result_envelope.py`, `tool_telemetry.py`, `project_registry.py`, `task_state.py`, `repository_lease.py`, `ellipsis_checkpoint.py` |
| Skills | `skills.py`, `builtin_skills/`, `prompts/` |
| Platform | `paths.py`, `security.py`, `detached_process.py`, `process_launcher.py` |

`disk_maintenance.py` remains for the protected-workspace check and read-only
disk tools; the release does not expose a maintenance mode. `browser_bootstrap`
is used only to decide whether a rendered-page check can run, not to control a
browser. `desktop_apps.py` is an explicit refusal shim required by the
hot-worker module group; desktop control is not a release surface.

## New in the release

- `cli.py` — a compact command surface: agent run, sessions, providers, models,
  MCP outbound, hosted bridge, skills, credentials, doctor/status and paths. It
  constructs `CapabilityCoreRuntime` with the same capability grants and risk
  engine as v5.
- `tui/` — the rebuilt client. 8 files, one stylesheet, no import of the agent
  runtime. It talks to `agent run --stream-json` and the CLI `--json` surfaces.
- `agent run --stream-json` — one typed event per stdout line and one final
  result document. v5 had human progress on stderr and a pretty `--json`
  document at the end; neither could drive a live UI.
- Automatic provider model discovery (`model discover`) with context/output,
  tool/vision and provider-reported price metadata.
- Multi-route provider fallback, privacy preflight and hard token/cost ceilings
  (`--route`, `--max-total-tokens`, `--max-cost`).
- Bounded project context (instructions, environment, dependency-aware map) and
  relevant local memory, with `--no-project-context`/`--no-memory` opt-outs.
- Plan/Ideate durable artifacts, image attachments and visual-checker injection
  for web tasks when the selected model advertises vision and Chromium is
  available.
- The skill catalogue is advertised to the agent and `skill_load` is available
  through Core.
- `provider setup` reuses an existing credential, exempts loopback endpoints
  from a key requirement, and verifies the endpoint with a minimal request
  before storing a new key.
- The hosted bridge command group: `bridge connect/serve/saved/status/stop/
  restart/oauth/credential`. It reads existing v5 saved profiles and keyring
  entries unchanged.

## Not ported (historical 6.0.0a1 list, superseded)

The 6.0.0a2 release made the vendored karo-core engine the only kernel, so
the engine item below no longer applies; 6.0.0a3 deleted the unreachable
research-subagent block instead of shipping a second driver.

These remain in v5 and are deliberate gaps, not accidental omissions:

- ~~**karo-core engine.**~~ Superseded: `engine/` is the product kernel.
- ~~**Research subagents.**~~ Removed: the detached `subagents.py` /
  `core_engine.py` block was deleted in 6.0.0a3; orchestration stays out.
- **Per-session skill selection.** `skill list/show/load` works and the agent
  may load a skill, but `skill select/deselect` and skill-level capability
  grants are not exposed.
- **Auto-checkpoint, offline/online checkpoint stores, handoff documents.**
  Sessions are durable and resumable; `ellipsis_checkpoint.py` backs hosted
  Smart Stop, but local Git rollback checkpoints are not created automatically.
- **Maintenance mode.** Read-only disk maintenance tools exist, but there is no
  autonomous cleanup mode.
- **Autonomy, orchestration, mission control.** These remain v5 laboratory
  surfaces. Browser and desktop control were ported in 6.0.0a2 and are
  registered as typed tools around the engine.
- **Hosted extras.** The old v5 compatibility catalogue (browser/dev-server/
  task/orchestrate/memory tool names) was removed in 6.0.0a3: it passed launch
  validation but no runtime served it. An OpenAPI bridge and an external MCP
  proxy inside the hosted bridge are still not ported. `git.push`, publish,
  deploy and auth mutations are never exposed.
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

- 745 release tests pass (`pytest`), including provider transports, registry
  and credentials, MCP client and CLI lease/replay, policy and risk, session
  idempotency, OAuth/DCR/PKCE/refresh replay, hosted allowlist/idempotency and
  Tailscale route ownership.
- `ruff check src tests scripts` passes.
- `mypy` passes on the declared new surface (CLI, entrypoint, TUI); the copied
  kernel is explicitly outside that gate.
- Live agent, routed through an isolated gateway configuration:
  task created `hello.txt`, `python -m pytest -q` passed, report
  `verified: true`, one step.
- Live terminal client, same task: `verified`, 6 steps, 2/2 checks, ~44k tokens,
  file content correct.
- Live loopback bridge, HTTP wire (not just in-process):
  discovery metadata returned 200, `/mcp` without a token returned 401,
  DCR returned a client, the approval page accepted the keyring password,
  PKCE token exchange succeeded, MCP `initialize` and `tools/list` returned the
  selected read-only tools, and `tools/call` read a real file. A mutating
  `tools/call` wrote once, and an identical retry returned
  `idempotent_replay: true` without a second write.
- The existing v5 `chatgpt-dev` saved profile, session id
  `web-saved-18870c825f9eba831276059d` and its keyring approval-password entry
  were read successfully; the connector does not need to be recreated.
- Wheel built, inspected for data files, installed into a fresh virtual
  environment and used for the same task: `verified`, one step.
- Release TUI tests replay recorded events and never touch a model, public
  network or real keyring.