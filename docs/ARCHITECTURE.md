# Architecture

KaroX is a local agent runtime. One process owns the model loop, tool
execution, policy and durable state; the terminal client is a separate viewer
that speaks a line protocol to it.

```text
┌──────────────────────────┐        NDJSON         ┌────────────────────────┐
│ tui/ (Textual client)    │ ◄──────────────────── │ cli.py (agent run)     │
│ no runtime imports       │  one event per line   │ session + provider     │
└────────────┬─────────────┘                       └───────────┬────────────┘
             │ other surfaces: model/model list,             │
             │ session list, mcp status, provider presets    │
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
```

## Layers and dependency direction

1. **UI — `karox/tui/`.** Widgets, modals, styles, subprocess bridge. It imports
   only `paths`, `detached_process` and its own modules. It must never import
   `agent`, `core`, `providers` or `sessions`; those changes belong behind the
   CLI.
2. **Command surface — `cli.py`, `entrypoint.py`.** Parses arguments, resolves
   a provider/model, creates or resumes a session, constructs the kernel and
   renders the report. All handlers are thin and return an exit code.
3. **Agent kernel — `agent.py`.** The bounded model loop: request construction,
   tool-call parsing and dispatch, repair turns, context compaction, evidence
   and the final report. It knows about providers, Core, sessions and events.
4. **Core and tools — `core.py`, `core_tools.py`, `action_execution.py`,
   `repo_context.py`, `edit_engine.py`, `affected_checks.py`, `check_jobs`-free
   checks.** Core owns the tool table; `ExtendedCoreRuntime` adds editing,
   windowed reads, plans and developer commands; `CapabilityCoreRuntime` adds
   consequence-based authorization.
5. **Policy — `policy.py`, `risk_engine.py`, `risk_mapping.py`,
   `action_policy.py`, `command_guard.py`.** Capability answers whether an
   origin may ever perform a kind of action; risk answers whether this instance
   is safe now. Both are required.
6. **Providers — `providers.py`, `provider_adapters.py`, `registry.py`,
   `provider_factory.py`, `provider_controller.py`, `provider_pricing.py`,
   `provider_presets.py`, `credentials.py`.** Transport, model catalog,
   capability facts and opaque credentials live here.
7. **Sessions — `sessions.py`, `requirements.py`, `event_bus.py`,
   `usage_analytics.py`.** Durable records, leases, history and evidence.
8. **MCP — `mcp_client.py`, `mcp_status.py`, `mcp_oauth.py`.** Registry,
   discovery, schema digests, permissions, transports and OAuth.
9. **Platform — `paths.py`, `security.py`, `detached_process.py`,
   `process_launcher.py`, `credential_session.py`, `credential_secret_service.py`.**
   Paths, redaction, child-process identity and OS keyring access.
10. **Prompt and skills — `context_compiler.py`, `tool_universe.py`,
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
  `mcp-servers.json`, `ui.json`.
- Sessions and audit: `paths.runtime_dir()/vnext/sessions/`,
  `audit.jsonl`.
- Credentials: OS keyring only; the registry stores an opaque reference such as
  `os-keyring:provider/openai` or an environment reference named
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

## Copied kernel

Most of layers 3–10 are a curated copy of the v5 kernel, not a rewrite. The map
and the reasons are in [MIGRATION.md](MIGRATION.md). Keeping the code identical
where it already passed its tests is deliberate: the release changes the
surface and dependencies, not the verified behavior of the loop.