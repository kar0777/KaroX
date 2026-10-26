# Changelog

## 6.0.0a3 — 2026-10-26

### Added — check intelligence

- **A failing check now arrives as a failure map, not as the tail of a log.**
  `check_report.py` parses pytest, unittest, Jest/Vitest, node:test, go test,
  cargo test and cargo build errors, ruff, mypy and tsc into runner, test id,
  file, line, column, code and a one-line message. The map is extracted from
  the unelided streams before the middle of a long log is cut, so a suite with
  many failures still names every file and line the model needs. The same map
  is prepended to the tool result the model reads, and a failed `tests.run`
  gets it too.
- **stderr is part of the check result.** Jest and cargo print their failures
  to stderr; reading only stdout made a red run look silent. The model now
  sees both streams, labelled.
- **The repair turn names the exact place and a copy-ready rerun.** A failure
  summary carries `path:line`, the failing test id and
  `tests.run suite=focused targets=[...]` for the test files that failed,
  instead of a bounded tail of stdout. A second identical failure set is
  labelled as unchanged ("the last change did not alter it"), so a repair turn
  cannot mistake a repeat for progress.
- **Focused tests derive their own targets.** `tests.run` with
  `suite=focused` and no targets used to refuse the call. It now selects the
  test files under `tests/` that conventionally match or import the modules
  this session changed, reports `targets_source: "session_changes"`, and lets
  every path be validated again by the runner. The approved full command
  remains the only evidence; a focused run is fast feedback.
- **A repair edit answers its own question.** While a check is failing, a
  successful edit now reruns exactly the failing test files (or node ids) from
  the map and attaches the verdict to the edit result the model reads — so the
  turn the model used to spend asking "did that fix it?" is gone. The run is
  bounded (4 targets, 20 s), once per changed content, and advisory: it never
  sets `verification_eligible` and is not recorded as a tool entry.

### Added

- `karox agent run --browser-plain-credentials` lets the browser fill
  password/token fields with throwaway test credentials supplied in the task;
  values stay redacted. Without it nothing changes: the default surface still
  refuses secret fills and named-key submits remain available. A
  single-character `press` now passes the same secret-field gate as `fill`
  (it writes text), closing a character-at-a-time bypass; `press` also
  accepts punctuation keys and normalizes common spellings (`ctrl + enter`,
  `return`, `esc`, arrow names). `get_text` without a selector returns the
  whole page's text instead of refusing.
- The default engine run now lets the browser save a download it was asked
  for. `browser.command {action: download_start}` was implemented and tested
  in the adapter but the agent-run policy kept `downloads=False`, so a browser
  task whose deliverable is a file could not produce one. Files land in the
  KaroX artifact store (content-addressed, 25 MiB cap), never the repository,
  are not executed, and the navigation that produces them stays policy-gated;
  the default policy remains loopback-only, headless and non-detaching. The
  size ceiling is now checked on the saved temp file before the bytes are read
  into memory.
- The system prompt now reads the precomputed project map first: when it
  already names the likely implementation files and tests as a small, specific
  set, the model reads those files directly instead of spending a turn on
  `repo.inspect` to discover what the map already states. Measured on the A/B
  fixture: 4 model calls instead of 5, 15-32% fewer input tokens, both runs
  verified.
- The finishing harness no longer re-runs a full approved check the model
  already passed on the current bytes. The fallback that produces a check when
  the floor names an obligation without a command used to fire for any missing
  proof, including only the final `git.diff` review, so a task that verified
  itself paid for the whole suite twice.
- The engine contract allows independent tool calls to be batched in one
  response (the product prompt already asked for this, and the contract read
  last said "prefer exactly one", so the model serialised work and spent
  turns). One unparseable call inside a batch now returns an observation for
  that call id instead of discarding the valid calls beside it; a lone
  unparseable call keeps the format-error recovery.

### Added

- Browser observations and desktop reads now count as answer evidence. A pure
  research or browser task can be reported verified from its own successful
  `snapshot`/`get_text`/`download_start` observations, and a desktop task from
  its `capture`/`inspect_ui`/`list_windows` reads; `answer_basis` names the
  action and the artifact/sha256 when there is one. The finish tool's answer
  is now the model's final message for that check, so a terse run that never
  wrote prose can still verify. Before this, a browser-only or desktop-only
  task always ended `no_changes` however well it went.
- The agent-run surface has end-to-end coverage of both race domains with a
  scripted model: the fixture site login with test credentials, submit, then
  a download whose artifact bytes are checked by sha256; and an owned
  background desktop window driven through list, attach by window id, inspect,
  type and capture, with the EDIT control read back independently.

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

- Every Core tool now passes one argument validator. The built-in validator
  did not know the `integer` type, so the desktop tools' integer arguments
  (window ids, click/scroll coordinates, limits, max nodes, pace) were refused
  before their handlers ran: the computer-use benchmark could not click
  anything. The same code now enforces the constraints the schemas already
  declared (enum, minimum/maximum, string and array bounds, pattern), names
  the accepted arguments on a mismatch, and flattens an unambiguous
  `payload`/`arguments` wrapper that models copied from `browser.command`.
  Desktop tool descriptions now say arguments are top-level and explain that
  `app_id` is the caller's label while `window_id`/`title_contains` choose the
  window.

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