# KaroX vNext Implementation Status

Last updated: 2026-07-23  
Branch: `codex/vnext-hybrid-runtime`  
Base: `main` at `a5c233a`

## Status summary

| Phase | Status | Evidence |
| --- | --- | --- |
| 0 — audit/design | Complete | Baseline suite passed; required documents reviewed and validated |
| 1 — foundation | Complete | 25 discoverable tests cover Core, policy, sessions, migration, and CLI |
| 2 — native vertical slice | Complete | 52 tests plus a subprocess CLI E2E cover provider → agent → Core → verification |
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

## Phase 2 — native vertical slice

Completed:

- Implemented an OpenAI-compatible Chat Completions provider with real HTTP
  Server-Sent Events transport, normalized streamed content and tool-call
  fragments, multiple tool calls per response, usage reporting, request
  deadlines, and classified transport/HTTP/protocol failures.
- Limited retries to retryable failures before a usable streamed response has
  begun; interrupted or malformed responses after that boundary fail closed.
- Added an Agent Kernel that drives the bounded provider → tool → result loop,
  recovers pending mutations through Core idempotency, rejects malformed calls,
  detects repeated identical actions, and enforces step, action, and wall-time
  limits.
- Required durable verification before reporting success: at least one real
  file change, a successful check, Git status evidence, and Git diff evidence.
  Model text alone is never treated as proof of completion.
- Added `karox agent run` as the first native model-driven CLI path while keeping
  the legacy bridge and tested Notion behavior unchanged.
- Made file hashes byte-accurate, classified byte-identical writes as no-ops,
  bounded process output by UTF-8 bytes, redacted credentials, and made Git
  status/diff failures evidence-backed failures.
- Kept HyperAgent and PromptQL explicitly experimental; this phase does not
  claim product-level support for either integration.

Changed files in Phase 2:

- `src/karox/agent.py`
- `src/karox/providers.py`
- `src/karox/cli.py`
- `src/karox/core.py`
- `tests/test_agent.py`
- `tests/test_providers.py`
- `tests/test_core.py`

Verification:

- `python -m compileall -q src tests`
- `python -m unittest discover -s tests -p "test_*.py" -v` — 52 passed
- `python scripts/test_path_migration.py`
- `python scripts/test_app_entry.py`
- `python scripts/test_runtime_rebrand.py`
- `python scripts/test_notion_profile.py`
- `python scripts/test_notion_provider.py`
- `python scripts/test_notion_mcp_transport.py`
- `git diff --check`

End-to-end evidence:

- `AgentCliEndToEndTests` starts a real loopback HTTP server that emits
  OpenAI-compatible SSE, then launches `python -m karox.cli agent run` in a
  subprocess rather than calling the kernel directly.
- Across five provider requests, the agent reads and changes a file, runs a
  check, collects dedicated Git status and diff results, and finishes only
  after verification succeeds.
- The test validates the streamed tool-call/result ordering and four persisted
  evidence records: file mutation, successful check, Git status, and Git diff.
- The server is a deterministic local fake provider. This proves the actual
  HTTP/SSE and CLI integration without requiring a paid external API, but it is
  not evidence of interoperability with every OpenAI-compatible vendor.

Security boundaries, known problems, and migration risks:

- Provider credentials are accepted only over HTTPS or HTTP loopback
  endpoints. Remote cleartext HTTP with credentials is rejected.
- `checks.run` is policy-gated, repository-bound, environment-restricted, and
  shell-free, but it is not an operating-system sandbox. Test and build commands
  can execute arbitrary code with the KaroX process's operating-system rights.
- A timed-out check terminates the direct child process, but descendant-process
  termination is not yet guaranteed consistently across operating systems.
- Tool calls are executed sequentially. Safe parallel scheduling, cancellation,
  pause, and resume remain future kernel work.
- Only the generic Chat Completions protocol is implemented. OpenAI Responses,
  Anthropic, Gemini, secure credential storage, a model registry, budgets,
  fallback, and context compaction remain pending.
- MCP client/proxy support, Skills, unified cross-provider handoff, Packs, and
  TUI remain pending. Existing bridge paths remain the migration route in the
  meantime.
- Notion remains the only tested hosted bridge. HyperAgent and PromptQL remain
  experimental because they still lack dedicated product E2E coverage.

Next actions for Phase 3:

1. Define provider and model registry configuration without exposing secrets in
   session state, logs, or command history.
2. Add secure credential storage with explicit cross-platform behavior and
   migration tests.
3. Add OpenAI Responses, Anthropic, Gemini, and local/custom provider adapters
   behind shared contract tests.
4. Add usage accounting, token/currency budgets, model selection, and explicit
   provider fallback policy.
5. Exercise representative real providers only when credentials and spending
   authorization are available; keep deterministic fake transports in CI.

## Commit record

- `4a8278f fix: fail closed when runtime key is missing`
- `b5488a4 docs: define vNext hybrid runtime architecture`
- `353158d feat: add policy-gated core runtime foundation`
- `3ca96cf docs: record Phase 1 runtime foundation`
- `6f05b61 feat: add native agent vertical slice`

No merge, push, or release has been performed.
