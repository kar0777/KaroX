# KaroX vNext Implementation Status

Last updated: 2026-07-23  
Branch: `codex/vnext-hybrid-runtime`  
Base: `main` at `a5c233a`

## Status summary

| Phase | Status | Evidence |
| --- | --- | --- |
| 0 — audit/design | Complete | Baseline suite passed; required documents reviewed and validated |
| 1 — foundation | Complete | 25 discoverable tests cover Core, policy, sessions, migration, and CLI |
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

## Phase 1 — runnable foundation

Completed:

- Added the installable `karox-runtime` package and `karox` console entry point.
- Established provider-independent models for origin identity, capabilities,
  Core commands/results, and evidence records.
- Added an origin-aware, deny-by-default capability policy. Push, package
  publishing, and authentication remain outside every profile and require a
  future explicit one-shot approval path.
- Implemented repository-bound sessions with checksummed atomic state,
  exclusive mutation leases, fencing tokens, revision checks, revocation, and
  durable idempotency records.
- Implemented the first Core Runtime boundary: UTF-8 file read/write/list,
  bounded checks without a shell, and dedicated read-only Git status/diff.
- Enforced traversal, symlink/reparse, `.git`/`.karox`, repository fingerprint,
  origin capability, lease, and idempotency boundaries.
- Added evidence persistence for file mutations and checks; failed and timed-out
  checks are represented as failed evidence-backed results rather than success.
- Centralized structural/value credential redaction and stopped arbitrary host
  environment variables from reaching child checks.
- Added secret-safe metadata migration that never copies credentials, refuses
  overwrite, reports unsupported data, detects source changes, and rolls back
  partial output.
- Added `karox paths`, `karox session create/list/show`, and dry-run-by-default
  `karox migrate` commands.
- Removed the unused tracked `src/a.txt` placeholder. Legacy bridge and Notion
  implementation paths were not modified.

Changed files in Phase 1:

- `pyproject.toml`
- `src/karox/*.py`
- `tests/*.py`
- `src/a.txt` (removed placeholder)

Verification:

- `python -m compileall -q src tests`
- `python -m unittest discover -s tests -p "test_*.py" -v` — 25 passed
- `python scripts/test_path_migration.py`
- `python scripts/test_app_entry.py`
- `python scripts/test_runtime_rebrand.py`
- `python scripts/test_notion_profile.py`
- `python scripts/test_notion_provider.py`
- `python scripts/test_notion_mcp_transport.py`
- `git diff --check`

Known problems and migration risks:

- `checks.run` is shell-free and policy-gated but is not an operating-system
  sandbox. A granted `process.run` capability remains powerful and must not be
  given to an untrusted origin casually.
- The foundation does not yet include a provider adapter or Agent Kernel, so no
  native model-driven E2E is claimed in this phase.
- Secure OS credential stores, model registry, budgets, provider fallback,
  Skills, MCP client/proxy, Pack SDK, and TUI remain pending.
- Session state is structured and recoverable, but cross-provider handoff has
  not yet been exercised end to end.
- Notion remains the only tested hosted bridge. HyperAgent and PromptQL remain
  experimental because they lack product E2E coverage.

Next actions:

1. Implement the generic OpenAI-compatible provider contract and transport.
2. Implement the bounded Agent Kernel with mandatory verification.
3. Add `karox agent run` and a fake-HTTP-provider E2E covering read, write,
   check, Git diff/status, and an evidence-backed final report.
4. Exercise malformed calls, loop/step/time limits, transport retries, failed
   verification, and idempotent replay before marking Phase 2 complete.

## Commit record

- `4a8278f fix: fail closed when runtime key is missing`
- `b5488a4 docs: define vNext hybrid runtime architecture`

No merge, push, or release has been performed.
