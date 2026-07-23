# KaroX vNext Implementation Status

Last updated: 2026-07-23  
Branch: `codex/vnext-hybrid-runtime`  
Base: `main` at `a5c233a`

## Status summary

| Phase | Status | Evidence |
| --- | --- | --- |
| 0 — audit/design | Complete | Baseline suite passed; required documents reviewed and validated |
| 1 — foundation | Not started | — |
| 2 — native vertical slice | Not started | — |
| 3 — providers/credentials | Not started | — |
| 4 — Skills | Not started | — |
| 5 — MCP client | Not started | — |
| 6 — unified handoff | Not started | — |
| 7 — MCP proxy/bridges | Not started | — |
| 8 — Pack SDK | Not started | — |
| 9 — TUI | Not started | — |
| 10 — benchmark/readiness | Not started | — |

## Phase 0 baseline

Completed:

- Created the isolated feature branch; main is untouched.
- Audited the repository layout, major modules, CI, launchers, and integrations.
- Ran every existing `scripts/test_*.py` executable successfully with a test
  runtime key, including Notion, API hardening, installers, migration, runtime
  rebrand, Tailscale, and session flow.
- Confirmed `python -m compileall -q server scripts` and `git diff --check` pass.
- Fixed import without `REPO_TOOLS_API_KEY` while retaining fail-closed auth,
  with a clean-process regression test.
- Replaced one brittle installer source-adjacency assertion with a semantic
  ordering assertion inside `Promote-StagedApp`.
- Created the ten required vNext design/status documents.
- Validated that all required documents exist, are non-empty, and distinguish
  tested integrations from experimental and planned work.

Baseline observations:

- `server/repo_tools.py` is 1345 lines and combines import-time configuration,
  API, policy, and execution.
- `start.core.ps1` is 1904 lines; `start.core.sh` is 1627 lines.
- `server/karox4_exec.py` is 693 lines; `server/notion_agent_tools.py` is 683.
- Runtime extensions mutate `repo_tools` globals.
- Existing tests are executable scripts. `python -m unittest discover -s
  scripts -p 'test_*.py'` collects zero tests and therefore is not a valid CI
  replacement.
- Notion has meaningful regression coverage and is the only named hosted product
  classified tested. Its MCP tests emit noisy upstream `ClosedResourceError`
  shutdown logs while passing.
- PromptQL is launcher/OpenAPI-oriented and lacks a dedicated E2E. HyperAgent has
  no verified dedicated implementation. Both are experimental.

Changed files in Phase 0:

- `server/repo_tools.py`
- `scripts/test_app_entry.py`
- `scripts/test_runtime_rebrand.py`
- `docs/vNext/*.md`

Tests run for the baseline security commit:

- `python scripts/test_app_entry.py`
- `python scripts/test_runtime_rebrand.py`
- `git diff --check`

Known problems and migration risks:

- No native provider/kernel vertical slice exists yet.
- No origin-aware Core policy, shared session lease, MCP client/proxy, Skills, or
  Pack lifecycle exists yet.
- Legacy globals prevent clean in-process session isolation.
- Launchers duplicate product behavior across shells.
- Secure credential-store behavior has not yet been implemented/tested on the
  three operating systems.

Next actions:

1. Review and commit Phase 0 documents.
2. Add the installable package and normal discoverable vNext test suite.
3. Implement origin-aware Core policy plus durable locked sessions.
4. Build the generic OpenAI-compatible fake-server E2E vertical slice.

## Commit record

- `4a8278f fix: fail closed when runtime key is missing`

No merge, push, or release has been performed.
