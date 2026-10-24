# Changelog

## 6.0.0a1 — 2026-10-23

First clean release line. The agent kernel, policy, providers, sessions, MCP and
skills are a curated copy of v5; the command surface and terminal client are
new.

### Added

- Compact terminal client (`tui/`): streaming markdown answers, one-line tool
  cards, reason-only-on-request, model and session pickers, provider setup, MCP
  panel, confirmation dialogs, RU/EN catalogue and a single stylesheet.
- `karox agent run --stream-json`: typed progress events as one JSON document
  per line, then one final result document. This is the client's only runtime
  boundary.
- Compact CLI for `agent`, `session`, `provider`, `model`, `mcp`, `skill`,
  `credential` and `paths`.
- Project instruction files (`AGENTS.md`, `CLAUDE.md`, `KAROX.md`) are read
  directly, bounded and attributed in the report.
- The skill catalogue is advertised to the agent; `skill_load` is available.
- `scripts/check_tree.py` release-hygiene gate.
- Release tests: TUI pilot, CLI, stream protocol and tree contracts.

### Changed

- `provider setup` verifies the endpoint with a minimal request before storing a
  key; `--no-test` is the explicit opt-out.
- `agent run` discovers a repository-appropriate verification command when none
  is supplied, instead of requiring a flag.
- Dependencies are narrowed to what the release imports; the vendored
  karo-core engine, litellm and jinja2 are not part of this wheel.
- `mcp_client` no longer imports the hosted bridge's tool-name catalogue.

### Removed

- The legacy terminal shell (`tui.py`, `tui_connections.py`,
  `tui_dashboard.py`, `tui_workspace.py`, `tui_onboarding.py`).
- Hosted bridge, web bridge launcher, autonomy runtime, orchestration and
  mission-control surfaces.
- Browser automation/profile surfaces; `web_check --render` remains as a
  verification command.
- Tailscale, Ellipsis, ClickUp/Notion setup and ecosystem registries.

### Known gaps

See [MIGRATION.md](MIGRATION.md#not-ported-in-600a1). The largest are the
karo-core engine adapter, project-map/research context, visual-checker
injection, per-session skill selection and automatic Git checkpoints.