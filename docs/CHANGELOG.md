# Changelog

## 6.0.0a3 — 2026-10-26

### Added

- The default engine run now lets the browser save a download it was asked
  for. `browser.command {action: download_start}` was implemented and tested
  in the adapter but the agent-run policy kept `downloads=False`, so a browser
  task whose deliverable is a file could not produce one. Files land in the
  KaroX artifact store (content-addressed, 25 MiB cap), never the repository,
  are not executed, and the navigation that produces them stays policy-gated;
  the default policy remains loopback-only, headless and non-detaching. The
  size ceiling is now checked on the saved temp file before the bytes are read
  into memory.
- The engine contract allows independent tool calls to be batched in one
  response (the product prompt already asked for this, and the contract read
  last said "prefer exactly one", so the model serialised work and spent
  turns). One unparseable call inside a batch now returns an observation for
  that call id instead of discarding the valid calls beside it; a lone
  unparseable call keeps the format-error recovery.

### Removed

- The v5 `LegacyAgentKernel` loop and the `--legacy-loop` flag. After the
  kernel duel (independent SWE-rebench + live tasks) the vendored karo-core
  engine is the only kernel; `AgentKernel(..., legacy=True)` now raises. The
  seven proof-state readers both loops shared live on in the new
  `session_analysis.py` (evidence chain, answer basis, step count, safe
  arguments, unresolved checks, attempted mutation, last provider message).
- The legacy loop's economy modules: `cache_scheduler.py`,
  `cost_intelligence.py`, `semantic_shadow.py`, `context_compiler.py`,
  `tool_universe.py`, `base_probe.py` (~9.2k lines total with the loop).
- The vendored engine's unused surface: docker/singularity/extra
  environments, the alternative model backends (openrouter, portkey,
  requesty, litellm text/response variants), `agents/interactive.py`,
  `config/` except `default.yaml`, and `run/` (the mini-swe-agent CLI and
  benchmark harnesses). The vendoring is now exactly the product's import
  closure plus `models/test_models.py`; `check_tree.py` freezes that set.
- Dead interface kwargs on `EngineKernel` (`economy_mode`,
  `lossless_context_rewrite`, `render_feedback`, `domain_notes_enabled`,
  `repeat_read_suppression`, `semantic_shadow`, `base_probe_enabled`).
- The detached MCP/subagent block (`engine_mcp.py`, `subagents.py`,
  `core_engine.py`) and its tests: no product entry point reached it, and MCP
  rides the Core runtime's typed tools. The vendored engine model layer
  (`litellm_model.py` and its utils) went with it; the fenced-command salvage
  lives in `engine_salvage.py`. `litellm` and `tenacity` left the runtime
  dependencies.
- Dead seams and data: the checkpoint auto-guard and its factory, the
  `mutation_reviewer` hook, the worker browser path, the unused
  `BrowserApprovalBroker`, the v5 hosted name catalogue, the dead skill
  selection/policy helpers, `skill_notes/`, the v7 prompt assembler and its
  profile files.

### Fixed

- `karox bridge connect/start/restart` spawned a child the CLI parser refused:
  the launcher sent `--protocol`, `--saved-profile-name`, `--project`,
  `--default-project-id` and `--allowed-redirect-hosts`, none of which the
  `bridge serve` subparser defined. Both parsers now share one
  `add_serve_arguments()` definition, and a test parses the launcher's exact
  argv with the CLI parser.
- The live event channel dropped events silently: `AgentEvent` had no
  `reasoning_summary_delta` field, and one constructor failure muted the whole
  observer. The field exists, `COMPACTED` writes `summary`, and a malformed
  event is dropped without detaching the observer.
- `SYSTEM_PROMPT` demanded `more_semantic_changes`, which no tool schema has,
  and told the model to answer without a tool call while the engine contract
  calls that a format error. Both instructions were removed.
- `skill.run` had no risk mapping, so every advertised skill call was
  classified unknown-and-high and refused at CONFIRM. It now maps to
  `process.run_dev` (workspace consequence).
- The TUI passed its own detected checks as `--verification-command`, which
  the CLI treats as explicit user approval. Detection is now display-only;
  only `/verify` sends an approved command.
- `tests.run` results claimed `verification_eligible: True` even though only a
  user-approved `checks.run` may prove a change. The flag is gone and a test
  pins that `tests.run` output is never evidence.
- CLI subcommand handlers ended in implicit default branches; an unhandled
  command name would have executed another command's code. Each group now
  refuses an unknown name.

### Changed

- The TUI palette is the v5 rc4 warm set (gold `#d4b676`, amber `#c6a56b`,
  olive muted `#8a7e6a`, ground `#0e0c08`, panels `#1a1712`), replacing the
  cool slate palette.
- The package is 69.1k lines / 137 modules (from ~88k); the line-count guard
  dropped from 90k to 80k. The CLI parser, tool schemas, agent contract,
  engine provider bridge, browser probes and Core types are separate modules.
- `docs/ARCHITECTURE.md` gained a one-line-per-module map; MIGRATION, PARITY,
  DEVELOPMENT, README, AGENTS and TUI.md describe the current tree.

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