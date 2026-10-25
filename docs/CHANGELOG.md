# Changelog

## 6.0.0a2 — 2026-10-25

### Changed

- `karox agent run` (and therefore the TUI backend, which spawns it) now
  executes the vendored karo-core engine (`src/karox/engine`) as the default
  runtime. The new `engine_kernel.py` wraps it: models are reached only
  through the KaroX provider adapters, every shell action passes the
  capability policy, the no-push/no-publish/no-remote-transport invariants,
  repository-confinement checks and the consequence engine (confirmation-class
  actions fail closed as observations), and the harness runs the approved
  verification commands itself after the engine submits. The report carries an
  `engine` identity block (name, vendored version, implementation) so the
  executing loop is provable in-band — the race harness reads exactly this
  block — and the engine run also writes its own trajectory artifact under
  the session's runtime dir (`vnext/sessions/<id>/engine/trajectory.json`,
  tagged with the `karox.engine_kernel.EngineAgent` implementation), which
  the `--legacy-loop` path never writes. The v5 `AgentKernel` remains
  available behind `--legacy-loop` and is unchanged.
- The line-count guard rose from 70k to 80k: the engine vendoring seeded at
  4d8d5d9 already put the tree at ~74.8k against the old bound. The merged
  browser (~6.5k) and computer-use (~6.2k) surfaces then moved it to 90k; the
  merged tree is ~87.9k.
- The default engine agent run (and the TUI backend, which spawns it) now
  registers the browser and desktop surface tools next to the built-in ones
  through the new `karox.engine_tools` wiring: `browser.command` (loopback-only,
  headless, no downloads by default; external navigation still needs the
  network capability) is advertised to write-profile sessions, and the 15
  `desktop.*` tools are advertised to elevated sessions only. Every call flows
  through the same capability policy, Smart-Stop risk gate and audit trail as
  the built-in tools; `CoreRuntime.register_tool` is the construction-time
  registration seam. The risk vocabulary gained explicit `desktop.read` /
  `desktop.input` / `desktop.launch` kinds and explicit grades for the 13
  previously fallback-graded browser actions.
- `workspace_worker.execute_browser_command` (the hosted path) now runs the
  real browser engine behind the runtime's capability policy through a lazily
  attached loopback `BrowserToolRuntime`, instead of refusing every non-`app.*`
  action; `app.*` still forwards to the desktop facade.

### Added

- `--legacy-loop` flag on `agent run`.
- `scripts/race_task.py`: identical-task race runner with isolated
  config/runtime, secret redaction, independent-check verdicts and a
  fairness-aware `compare` subcommand.
- `karox.engine_mcp`: MCP tools for the karo-core engine loop. The
  `EngineMcpAdapter` binds a session's `allow`-selected MCP tools (stdio and
  Streamable HTTP) and exposes them both as a typed Python API (`call_tool`
  with `EngineMcpCallResult` results, `McpProgressEvent` progress, mutation
  lease and idempotent replay) and as an in-band `mcp list` / `mcp call`
  shell command composed into the engine's `command_runner` via
  `build_engine_command_runner`.
- MCP client calls now forward remote progress notifications to an optional
  progress sink.

## 6.0.0a1 — 2026-10-25

First clean release line. The agent kernel, policy, providers, sessions, MCP and
skills are a curated copy of v5; the command surface, terminal client and compact
hosted bridge are new.

### Added

- Compact terminal client (`tui/`): streaming markdown answers, one-line tool
  cards, reason-only-on-request, model and session pickers, provider setup, MCP
  panel, `/doctor`, `/bridge`, confirmation dialogs, RU/EN catalogue and a
  single stylesheet.
- `karox agent run --stream-json`: typed progress events as one JSON document
  per line, then one final result document. This is the client's only runtime
  boundary.
- Compact CLI for `agent`, `session`, `provider`, `model`, `mcp`, `bridge`,
  `skill`, `credential`, `doctor`, `status` and `paths`.
- Inbound hosted bridge for ChatGPT Web, Claude Web and HyperAgent:
  OAuth discovery/DCR/PKCE/refresh, Streamable HTTP MCP, explicit tool allowlist,
  loopback listener, Tailscale Funnel ownership, saved profiles and credential
  management. Existing v5 saved profiles and keyring entries are read unchanged.
- Provider model discovery (`model discover`), multi-route fallback, privacy
  preflight and hard token/cost ceilings.
- Bounded project context (instructions, environment, dependency-aware map) and
  relevant local memory, with opt-outs.
- Plan/Ideate durable artifacts, image attachments and visual-checker injection
  where the model and Chromium support it.
- `doctor`/`status` health surfaces and the short daily command forms
  (`models`, `providers`, `sessions`, `skills`, bare groups).
- Release tests: provider transports, registry and pricing, MCP client and CLI
  lease/replay, policy/risk, session idempotency, OAuth security, hosted
  allowlist/idempotency, Tailscale route ownership, TUI pilot and tree
  contracts.

### Changed

- `provider setup` reuses an existing saved credential, exempts loopback
  endpoints from the key requirement, and verifies a new endpoint with a
  minimal request before storing a key.
- Verification commands discovered from a repository are suggestions. They are
  not accepted as evidence unless the user passes
  `--accept-discovered-checks`; explicit `--verification-command` remains the
  authority.
- `effort-level auto` now resolves from task, project-map and mode signals
  instead of a fixed medium budget.
- A user-approved `checks.run` may execute safe read-only Git subcommands such
  as `git diff --check`; `git push` and other remote mutations remain blocked.
  Root-level `test_*.py` files are detected as pytest verification even without
  a `pyproject.toml`.
- Pricing, route budgets and evidence details are surfaced in the human report.
- The agent refuses drive roots and protected system folders as a workspace,
  reads instruction files without following symlinks outside the repository,
  and ties Bypass to an elevated session profile.
- Mypy is scoped to the new surface (`follow_imports = "skip"`); the copied
  kernel remains outside the declared gate.
- Dependencies now include `starlette`, `uvicorn` and `psutil` for the bridge.

### Removed

- The legacy terminal shell (`tui.py`, `tui_connections.py`,
  `tui_dashboard.py`, `tui_workspace.py`, `tui_onboarding.py`).
- Browser automation/profile surfaces, autonomy runtime, orchestration,
  mission-control, hosted browser/dev-server tools and the OpenAPI bridge.
- Tailscale legacy autostart, Ellipsis, ClickUp/Notion setup and ecosystem
  registries.
- `git.push`, publish, deploy and auth mutations are not reachable from any
  bridge profile.

### Known gaps

See [MIGRATION.md](MIGRATION.md#not-ported-in-600a1). The largest are the
karo-core engine adapter, research subagents, per-session skill selection and
automatic local Git checkpoints.