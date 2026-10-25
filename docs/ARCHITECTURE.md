# Architecture

KaroX is a local agent runtime. One process owns the model loop, tool
execution, policy and durable state; the terminal client is a separate viewer
that speaks a line protocol to it.

```text
┌──────────────────────────┐        NDJSON         ┌────────────────────────┐
│ tui/ (Textual client)    │ ◄──────────────────── │ cli.py (agent run)     │
│ no runtime imports       │  one event per line   │ session + provider     │
└────────────┬─────────────┘                       └───────────┬────────────┘
             │ other surfaces: model/session list,           │
             │ mcp status, provider presets, bridge saved    │
             ▼                                               ▼
       cli.py --json                          engine_kernel.py (EngineKernel)
                                                │
                                    engine/ (vendored karo-core)
                                      DefaultAgent + guard
                                                │
                     ┌────────────────────────┼───────────────┐
                     ▼                    ▼                   ▼
                 core.py             providers.py        sessions.py
              (typed tools)        (model transport)   (durable state)  mcp_client.py
                     │                    │                   │      (external tools)
                     ▼                    ▼                   ▼
             policy.py + risk_engine  provider_adapters  runtime_dir()

Hosted client (ChatGPT / Claude / HyperAgent)
        │ HTTPS + OAuth
        ▼
bridge_launcher → bridge_tailscale → localhost uvicorn
        │
        ▼
bridge_oauth (discovery, DCR, PKCE, refresh) → bridge_proxy (Host/Origin, bearer)
        │
        ▼
hosted_core (explicit allowlist) → CapabilityCoreRuntime → core.py
```

## Layers and dependency direction

1. **UI — `karox/tui/`.** Widgets, modals, styles, subprocess bridge. It imports
   only `paths`, `detached_process` and its own modules. It must never import
   `agent`, `core`, `providers` or `sessions`; those changes belong behind the
   CLI.
2. **Command surface — `cli.py`, `entrypoint.py`.** Parses arguments, resolves
   a provider route/model, creates or resumes a session, constructs the kernel
   and renders the report. It also owns the `bridge` and `connections`-style
   lifecycle commands; all handlers are thin and return an exit code.
3. **Agent runtime — `engine_kernel.py`, `agent.py`, `session_analysis.py`.**
   `engine_kernel.py` runs the vendored karo-core engine
   (`engine/agents/default.py`, the loop measured ahead of stock mini) as the
   native agent: it speaks the engine's Model protocol on top of
   `provider_adapters`, gates every shell action through policy, the command
   invariants, repository confinement and the consequence engine, and runs the
   approved verification commands itself after the engine submits. The
   session's `allow`-selected MCP tools, the audited `skill.run` tool and the
   browser/desktop tools registered by `engine_tools.py` are advertised from
   `core.tools()` and execute through the same gate. `agent.py` now holds only
   the public `AgentKernel` facade, the event/report vocabulary and the system
   prompt; `session_analysis.py` holds the seven proof-state readers the
   harness uses to decide what a session already proved. The v5 loop and its
   `--legacy-loop` flag are deleted.
4. **Core and tools — `core.py`, `core_tools.py`, `action_execution.py`,
   `repo_context.py`, `edit_engine.py`, `workspace_worker.py`,
   `workspace_transaction.py`, `unified_patch.py`, `engine_tools.py`.** Core
   owns the tool table; `ExtendedCoreRuntime` adds editing, windowed reads,
   plans and developer commands; `CapabilityCoreRuntime` adds
   consequence-based authorization; `engine_tools.py` registers the
   `browser.command` and `desktop.*` surface tools on the runtime the engine
   run uses. The hot worker executes repository commands and tests in a
   reloadable child process without losing the authoritative policy boundary,
   and its browser.command entry point runs the real browser engine behind
   the runtime's capability policy.
5. **Browser and desktop surfaces — `browser/`, `computer_use/`,
   `desktop_apps.py`.** A real Playwright engine with per-session access
   policy, credentials, redaction and a typed tool adapter; non-intrusive
   Win32 computer use (message-based input, on-demand HWND capture, one-shot
   approvals) with the hosted `app.*` facade over the same adapter. Both
   register behind the Core capability/risk gate; the agent-path browser
   policy is loopback-only and headless by default.
6. **Policy — `policy.py`, `risk_engine.py`, `risk_mapping.py`,
   `action_policy.py`, `command_guard.py`.** Capability answers whether an
   origin may ever perform a kind of action; risk answers whether this instance
   is safe now. Both are required.
7. **Providers — `providers.py`, `provider_adapters.py`, `registry.py`,
   `provider_factory.py`, `provider_controller.py`, `provider_pricing.py`,
   `provider_presets.py`, `routing.py`, `model_discovery.py`,
   `credentials.py`.** Transport, model catalog, discovery, fallback routes,
   budgets, capability facts and opaque credentials live here.
8. **Sessions and context — `sessions.py`, `requirements.py`, `event_bus.py`,
   `usage_analytics.py`, `memory.py`, `project_context.py`, `project_map.py`,
   `map_service.py`, `mode_artifacts.py`.** Durable records, leases, history,
   evidence, bounded project orientation and durable Plan/Concept artifacts.
9. **MCP outbound — `mcp_client.py`, `mcp_status.py`, `mcp_oauth.py`.**
   Registry, discovery, schema digests, permissions, transports and OAuth.
10. **MCP inbound bridge — `hosted_core.py`, `bridge_oauth.py`,
   `bridge_proxy.py`, `bridge_serve.py`, `bridge_launcher.py`,
   `bridge_tailscale.py`, `bridge_profiles.py`, `browser_approval.py`.**
   Explicit hosted tool allowlist, OAuth 2.1 authorization server, Streamable
   HTTP MCP, Tailscale Funnel ownership and saved launch profiles.
11. **Platform — `paths.py`, `security.py`, `detached_process.py`,
    `process_launcher.py`, `port_ownership.py`, `process_identity.py`,
    `credential_session.py`, `credential_secret_service.py`.** Paths, redaction,
    child-process identity, port ownership and OS keyring access.
12. **Prompt and skills — `completion_gate.py`, `skills.py`, `prompts/`,
    `builtin_skills/`.** Completion proof, skill sessions, recipes and the
    visual-checker prompt.

The intended dependency direction is downward: UI → CLI → kernel → Core →
policy/providers/sessions/MCP → platform. A kernel module importing `tui` is a
design error; so is a tool handler importing `cli`.

## One turn

1. The user submits a task. The TUI starts
   `python -m karox.cli agent run --stream-json ...` with a generated session id.
2. The CLI loads or creates the session, resolves the provider, builds the
   capability grants, risk engine, MCP binding and the kernel —
   `EngineKernel` through the `AgentKernel` facade.
3. The engine queries the model through the provider adapter, parses one bash
   action per response and executes it in the guarded environment: capability
   policy first, then the string-level command invariants (no push, publish,
   remote transport), repository confinement for redirects and `cd`, and the
   consequence engine — anything needing a one-shot approval fails closed as
   an observation the model can act on. Every turn lands in the durable
   session; the trajectory is written under the session directory.
4. When the engine submits, the harness runs the user-approved verification
   commands through `CapabilityCoreRuntime` and reads git status/diff the same
   way. Only a passing approved check makes the run verified; the engine's own
   submission never does.
5. Typed progress events (`step_started`, `text_delta`, `tool_started`,
   `tool_finished`, `finished`, ...) are emitted as one JSON line each, and the
   final line is `{"type": "result", "report": ...}`. The engine run's report
   carries an `engine` identity block (`name` = `karo-core`, `version` = the
   vendored engine's `__version__`, `implementation` =
   `karox.engine_kernel.EngineAgent`) so a reader can prove which loop ran,
   and the engine's trajectory artifact under the session directory is the
   durable second proof.
6. The TUI renders deltas, tool cards and a result card. A stopped child leaves
   the session resumable; the next task continues it.

## State and secrets

- Configuration: `paths.config_dir()/vnext/providers.json`,
  `mcp-servers.json`, `ui.json`, `web-bridge-profiles.json`.
- Sessions and audit: `paths.runtime_dir()/vnext/sessions/`,
  `audit.jsonl`. Bridge OAuth registrations/grants live under
  `paths.oauth_state_dir()`; the approval password lives only in the keyring.
- Credentials: OS keyring only; the registry stores an opaque reference such as
  `os-keyring:provider/openai`, `os-keyring:mcp/<name>` or
  `os-keyring:bridge/<session-id>`, or an environment reference named
  `KAROX_PROVIDER_<NAME>_API_KEY`.
- A raw API key never appears in the registry, a session record, an event or a
  log line.

## Invariants

- One tool name, one implementation, one capability and one risk mapping.
- The agent runtime is the vendored karo-core engine; the retired v5 loop
  must not return. A report says which loop executed (`engine.module`), and a
  run is never called verified without a passing user-approved check.
- A tool call either has an idempotency key or is refused when it mutates an
  external system.
- Evidence is recorded before it is narrated. A report may not claim a change
  the session did not observe.
- Compaction is deterministic and derived from recorded content, never from a
  model's summary of work it might not have done.
- Approval cannot be widened by editing prompt text; it is a policy decision.
- The UI adds no authority. Every action it triggers goes through the same CLI
  path a script would use.
- A hosted client gets only the tools explicitly selected at launch. OAuth
  authenticates the client; it does not grant a capability. Every call still
  passes Core policy and risk, and mutations still require a lease, an
  idempotency key and replay-safe evidence.
- The bridge listener binds loopback; public reachability is a separately owned
  tunnel (Tailscale Funnel) and a proven route is never replaced or reset.

## Copied kernel

Layers 3–10 are a curated copy of the v5 kernel, not a rewrite. The map
and the reasons are in [MIGRATION.md](MIGRATION.md). Keeping the code identical
where it already passed its tests is deliberate: the release changes the
surface and dependencies, not the verified behavior of the loop.

The exception is the loop itself: `engine/` is the vendored karo-core
fork (mini-swe-agent lineage) that was measured ahead of stock mini on the
same model and grader, and `engine_kernel.py` is new integration code written
for this release, not a v5 copy. The engine's own sources stay vendored and
unmodified; every product boundary (providers, policy, risk, sessions,
verification) is applied around it by `engine_kernel.py`. The v5
`LegacyAgentKernel` and its economy modules (`cache_scheduler`,
`cost_intelligence`, `semantic_shadow`, `context_compiler`, `tool_universe`,
`base_probe`) were deleted after the engine won the kernel duel: the seven
proof-state readers both loops shared live on in `session_analysis.py`.

## Module map

Read this instead of the tree. One line per module that matters; anything
not listed is internal to its layer.

| Module | What it is |
| --- | --- |
| `cli.py` | command handlers; `agent run` resolves the session, provider and kernel |
| `cli_parser.py` | the declarative command surface and its defaults |
| `entrypoint.py` | console-script shim |
| `agent.py` | `AgentKernel` facade, module helpers and system prompt |
| `agent_contract.py` | `AgentEvent`/`AgentEventKind`/`AgentReport`, limits and context budget |
| `engine_kernel.py` | the engine kernel: typed-tool environment, checkpoints, verification |
| `engine_provider_bridge.py` | `EngineProviderModel`: the engine's Model protocol over `providers.py` |
| `session_analysis.py` | seven proof-state readers over a `SessionRecord` |
| `engine_salvage.py` | recovers one unambiguous fenced shell command from a prose response |
| `core.py`, `core_tools.py` | typed tool table, handlers and the runtimes (`Capability`, `SkillSession`) |
| `tool_schemas.py` | the extended tool names, descriptions and JSON schemas |
| `engine_tools.py` | registers `browser.command` / `desktop.*` on the engine run's runtime |
| `action_execution.py`, `workspace_worker.py`, `workspace_transaction.py` | hot worker: repo commands in a reloadable child process |
| `edit_engine.py`, `unified_patch.py`, `repo_context.py` | edits, patches, windowed reads |
| `policy.py`, `risk_engine.py`, `risk_mapping.py`, `action_policy.py`, `command_guard.py` | capability + risk gates |
| `providers.py`, `provider_adapters.py`, `provider_factory.py` | model transport; the only path to a model |
| `registry.py`, `model_discovery.py`, `routing.py`, `provider_pricing.py`, `provider_presets.py`, `provider_controller.py`, `credentials.py` | catalog, routes, budgets, keyring |
| `sessions.py`, `requirements.py`, `usage_analytics.py`, `memory.py` | durable state, leases, evidence, usage |
| `project_context.py`, `project_map.py`, `map_service.py`, `mode_artifacts.py` | bounded orientation, durable Plan/Concept |
| `effort.py`, `agent_modes.py`, `auto_verify.py`, `completion_gate.py` | effort budgets, modes, quick checks, verified floor |
| `mcp_client.py`, `mcp_status.py`, `mcp_oauth.py` | outbound MCP |
| `hosted_core.py`, `bridge_oauth.py`, `bridge_proxy.py`, `bridge_serve.py`, `bridge_launcher.py`, `bridge_tailscale.py`, `bridge_profiles.py`, `browser_approval.py` | hosted MCP inbound bridge |
| `browser/`, `computer_use/`, `desktop_apps.py` | Playwright engine; non-intrusive Win32 desktop use |
| `visual_check.py`, `web_check.py` | render grading and page verification |
| `skills.py` | skill sessions (`skill.run` is a leased Core tool) |
| `paths.py`, `security.py`, `detached_process.py`, `process_launcher.py`, `port_ownership.py`, `process_identity.py`, `credential_session.py`, `credential_secret_service.py`, `test_output.py` | platform: paths, redaction, child processes, keyring |
| `tui/` | Textual client (app, widgets, modals, backend, prefs, i18n, protocol) |
| `engine/` | vendored karo-core: `agents/default.py` (the loop), `environments/local.py`, `exceptions.py`, `utils/` — pruned to the product's import closure |