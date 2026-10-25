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
       cli.py --json                                 agent.py (AgentKernel)
                                                              │
                     ┌────────────────────────────────────────┼──────────────────┐
                     ▼                    ▼                   ▼                  ▼
                 core.py             providers.py        sessions.py        mcp_client.py
              (typed tools)        (model transport)   (durable state)     (external tools)
                     │                    │                   │
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
3. **Agent kernel — `agent.py`.** The bounded model loop: request construction,
   tool-call parsing and dispatch, repair turns, context compaction, evidence
   and the final report. It knows about providers, Core, sessions and events.
4. **Core and tools — `core.py`, `core_tools.py`, `action_execution.py`,
   `repo_context.py`, `edit_engine.py`, `workspace_worker.py`,
   `workspace_transaction.py`, `unified_patch.py`.** Core owns the tool table;
   `ExtendedCoreRuntime` adds editing, windowed reads, plans and developer
   commands; `CapabilityCoreRuntime` adds consequence-based authorization. The
   hot worker executes repository commands and tests in a reloadable child
   process without losing the authoritative policy boundary.
5. **Policy — `policy.py`, `risk_engine.py`, `risk_mapping.py`,
   `action_policy.py`, `command_guard.py`.** Capability answers whether an
   origin may ever perform a kind of action; risk answers whether this instance
   is safe now. Both are required.
6. **Providers — `providers.py`, `provider_adapters.py`, `registry.py`,
   `provider_factory.py`, `provider_controller.py`, `provider_pricing.py`,
   `provider_presets.py`, `routing.py`, `model_discovery.py`,
   `credentials.py`.** Transport, model catalog, discovery, fallback routes,
   budgets, capability facts and opaque credentials live here.
7. **Sessions and context — `sessions.py`, `requirements.py`, `event_bus.py`,
   `usage_analytics.py`, `memory.py`, `project_context.py`, `project_map.py`,
   `map_service.py`, `mode_artifacts.py`.** Durable records, leases, history,
   evidence, bounded project orientation and durable Plan/Concept artifacts.
8. **MCP outbound — `mcp_client.py`, `mcp_status.py`, `mcp_oauth.py`.**
   Registry, discovery, schema digests, permissions, transports and OAuth.
9. **MCP inbound bridge — `hosted_core.py`, `bridge_oauth.py`,
   `bridge_proxy.py`, `bridge_serve.py`, `bridge_launcher.py`,
   `bridge_tailscale.py`, `bridge_profiles.py`, `browser_approval.py`.**
   Explicit hosted tool allowlist, OAuth 2.1 authorization server, Streamable
   HTTP MCP, Tailscale Funnel ownership and saved launch profiles.
10. **Platform — `paths.py`, `security.py`, `detached_process.py`,
    `process_launcher.py`, `port_ownership.py`, `process_identity.py`,
    `credential_session.py`, `credential_secret_service.py`.** Paths, redaction,
    child-process identity, port ownership and OS keyring access.
11. **Prompt and skills — `context_compiler.py`, `tool_universe.py`,
    `completion_gate.py`, `skills.py`, `prompts/`, `builtin_skills/`,
    `skill_notes/`.** Stable prompt prefix, tool advertisement, completion
    proof and recipes.

The intended dependency direction is downward: UI → CLI → kernel → Core →
policy/providers/sessions/MCP → platform. A kernel module importing `tui` is a
design error; so is a tool handler importing `cli`.

## One turn

1. The user submits a task. The TUI starts
   `python -m karox.cli agent run --stream-json ...` with a generated session id.
2. The CLI loads or creates the session, resolves the provider, builds the
   capability grants, risk engine, MCP binding and `AgentKernel`.
3. The kernel sends the request through the provider adapter. Tool calls are
   executed by `CapabilityCoreRuntime`; each mutation passes capability and risk
   checks first. Every step is written to the durable session.
4. Typed progress events (`step_started`, `text_delta`, `tool_started`,
   `tool_finished`, `file_edited`, `phase_changed`, `finished`, ...) are emitted
   as one JSON line each. The final line is `{"type": "result", "report": ...}`.
5. The TUI renders deltas, tool cards and a result card. A stopped child leaves
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

Most of layers 3–10 are a curated copy of the v5 kernel, not a rewrite. The map
and the reasons are in [MIGRATION.md](MIGRATION.md). Keeping the code identical
where it already passed its tests is deliberate: the release changes the
surface and dependencies, not the verified behavior of the loop.