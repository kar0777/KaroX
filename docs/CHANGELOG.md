# Changelog

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