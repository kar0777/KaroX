# KaroX vNext Implementation Status

Last updated: 2026-07-24
Branch: `codex/vnext-hybrid-runtime`  
Base: `main` at `a5c233a`

## Status summary

| Phase | Status | Evidence |
| --- | --- | --- |
| 0 — audit/design | Complete | Baseline suite passed; required documents reviewed and validated |
| 1 — foundation | Complete | 25 discoverable tests cover Core, policy, sessions, migration, and CLI |
| 2 — native vertical slice | Complete | 52 tests plus a subprocess CLI E2E cover provider → agent → Core → verification |
| 3 — providers/credentials | Complete | 99 tests cover adapters, registry, keyring references, routing, fallback, and budgets |
| 4 — Skills | Complete | 119 tests cover secure discovery, lazy loading, permissions, CLI, and Agent integration |
| 5 — MCP client | Complete | 163 tests cover registry, credentials, selection, Core integration, and real stdio + Streamable HTTP E2E |
| 6 — unified handoff | Complete | 170 tests cover structured handoff, mid-task model switch, locking, and recovery |
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

## Phase 3 — providers and credentials

Completed:

- Added normalized streaming adapters for OpenAI Responses, Anthropic Messages,
  and Gemini GenerateContent while retaining the generic OpenAI-compatible Chat
  Completions path for local and custom endpoints.
- Hardened SSE parsing with bounded event, line, and stream sizes; strict
  lifecycle, usage, tool-call identity, and fragment validation; and
  secret-safe classified transport errors.
- Added an atomic provider/model registry with validated endpoint metadata,
  aliases, explicit capability declarations, privacy classes, pricing
  provenance, and default-model selection. Credential values are forbidden in
  registry headers, query values, and persisted configuration.
- Added opaque `os-keyring:provider/...` credential references backed by the
  operating-system keyring. Plaintext fallback is disabled, values are resolved
  only when constructing a request, and CLI output never returns a secret.
- Added provider construction, explicit ordered routing, capability/privacy
  preflight, narrowly classified fallback, cumulative token/cost accounting,
  pricing-version attribution, and secret-free route-attempt audit data.
- Enforced already-exhausted budgets before any provider call and withheld tool
  execution when a just-completed response crosses a token or cost budget.
- Added provider, model, and credential management/test commands and integrated
  routed/default-model operation into `karox agent run`; conflicting direct and
  routed options fail before session mutation.
- Kept deterministic fake transports as the CI contract. No paid provider was
  contacted and no external credential was required.

Changed files in Phase 3:

- `pyproject.toml`
- `src/karox/agent.py`
- `src/karox/cli.py`
- `src/karox/credentials.py`
- `src/karox/provider_adapters.py`
- `src/karox/provider_factory.py`
- `src/karox/providers.py`
- `src/karox/registry.py`
- `src/karox/routing.py`
- `src/karox/security.py`
- `tests/test_agent.py`
- `tests/test_credentials.py`
- `tests/test_provider_adapters.py`
- `tests/test_provider_cli.py`
- `tests/test_providers.py`
- `tests/test_registry.py`
- `tests/test_routing.py`

Verification:

- `python -m compileall -q src tests`
- `python -m unittest discover -s tests` — 99 passed
- `git diff --check`

Security boundaries, known problems, and migration risks:

- Real OpenAI, Anthropic, Gemini, and third-party compatible services remain
  unverified because the phase deliberately used no external keys or paid API
  calls. `provider test` and `model test` are explicit opt-in live checks.
- Secure credential availability depends on a working OS keyring backend.
  `credential doctor` reports absence and KaroX fails closed instead of writing
  plaintext credentials.
- Pricing and capability metadata are operator-supplied and provenance-labelled;
  KaroX does not silently claim that stale or unknown metadata is authoritative.
- A single provider response can cross a remaining budget because authoritative
  usage is known only after the response. The response is recorded and its tool
  calls are not executed; later provider calls are blocked.
- Context compaction, long-term provider health scoring, MCP client/proxy,
  unified cross-provider handoff, Packs, and TUI remain pending.
- Existing Notion behavior and legacy bridge paths were not modified. HyperAgent
  and PromptQL remain experimental.

Next actions for Phase 4:

1. Discover Skill metadata from project, compatible-agent, global KaroX, and
   explicitly configured directories without eagerly reading instructions.
2. Validate metadata, identities, source precedence, file confinement, and
   duplicate handling.
3. Lazily load selected instructions and referenced files without executing
   setup scripts or arbitrary Skill content.
4. Enforce `allow`, `ask`, `deny`, and session-only permission decisions through
   the origin-aware Core boundary.
5. Add CLI, unit coverage, and a permission-bypass integration/E2E test.

## Phase 4 — Skills

Completed:

- Added deterministic Skill discovery with this precedence: repository
  `.karox/skills`, `.agents/skills`, `.claude/skills`, explicitly configured
  directories in declaration order, then the global KaroX directory. Shadowed
  and rejected candidates produce diagnostics instead of silently replacing a
  higher-precedence Skill.
- Kept discovery metadata-only. `skill list` and `skill show` do not read the
  instruction body or referenced files; `skill load` and an explicitly selected
  Agent Skill are the only paths that load content.
- Added strict YAML/frontmatter, semantic-version, known-tool, capability, and
  reference validation. YAML aliases and duplicate keys are rejected, and
  falsey values with the wrong type cannot masquerade as empty lists.
- Confined every Skill to its selected source and rejected linked/reparse-point
  source directories, Skill directories, manifests, and references. Content is
  identity-checked before, during, and after reads to detect replacement races.
- Bound session selections to the Skill source, resolved directory, file
  identity, version, and metadata SHA-256. Changed metadata invalidates a prior
  selection and resets permission decisions instead of inheriting stale grants.
- Added session-scoped `allow`, `ask`, and `deny` decisions for declared
  capabilities. Only `allow` becomes an origin grant, `deny` remains explicit,
  and `ask` fails closed until the user records a new decision. Every grant is
  still intersected with the active Core access profile.
- Added `karox skill list/show/load/select/deselect` and integrated one selected
  Skill into `karox agent run`. Selection changes use session mutation leases.
- Injected untrusted Skill instructions only into provider-facing requests, not
  the durable base system history. Assistant/tool records retain Skill origin,
  and pending calls with missing or mismatched origin are never replayed.
- Added explicit resource ceilings: 64 KiB metadata, 1 MiB manifest or reference,
  2 MiB aggregate loaded content, and at most 64 references per Skill.
- Added unit, CLI integration, and Agent permission-bypass coverage without
  contacting a paid API or executing Skill-provided setup code.

Changed files in Phase 4:

- `pyproject.toml`
- `src/karox/agent.py`
- `src/karox/cli.py`
- `src/karox/skills.py`
- `tests/test_agent.py`
- `tests/test_skill_cli.py`
- `tests/test_skills.py`

Verification:

- `python -m compileall -q src tests`
- `python -m unittest discover -s tests -p "test_*.py" -v` — 119 passed
- `git diff --check`

Security boundaries, known problems, and migration risks:

- A Skill is an untrusted declarative instruction/reference bundle, not a plugin
  installer. KaroX does not execute Skill setup scripts, hooks, or arbitrary
  metadata, so Skills that depend on those behaviors require a future Pack or a
  documented manual migration.
- `ask` currently means fail closed until the user updates the session selection;
  there is no interactive approval prompt or expiring per-call approval token.
- A granted `process.run` capability remains powerful. Core keeps it
  repository-bound, shell-free, and environment-restricted, but it is not an
  operating-system sandbox.
- Compatible-agent directories are accepted only through the strict KaroX
  metadata and confinement model. Broader Claude/Codex ecosystem conventions
  are not implicitly trusted or claimed compatible.
- Selections are intentionally invalidated when identity or metadata changes.
  This can require re-selection after legitimate Skill updates, but prevents
  old grants from silently applying to new content.
- MCP declarations remain inert metadata in this phase. No Skill can acquire an
  MCP transport or bypass Core policy before the Phase 5 client exists.

Next actions for Phase 7:

1. Define explicit per-MCP exposure policy so a hosted client receives only
   user-selected external MCP servers, never all installed servers.
2. Add bridge profiles with transport, authentication, tunnel, doctor,
   handshake test, and honest tested/experimental status without per-site code.
3. Enforce identity, per-tool permissions, and provider-key/MCP-secret isolation
   so external clients cannot reach credentials outside their capability set.
4. Add a KaroX-as-MCP-server namespace/capability boundary so clients see only
   allowed `karox.*` tools.
5. Add deterministic fake-bridge and proxy E2E tests before any opt-in real
   hosted-client interoperability verification.

## Phase 6 — unified session handoff

Completed:

- Added a secret-free, strict-JSON structured handoff document derived from a
  `SessionRecord`. It carries goal, constraints, summary, decisions,
  checkpoints, changed files, commands, check results, errors, remaining plan
  steps, Git state, active processes, a summarized model history, usage,
  unfinished actions, and evidence -- without copying full chat history,
  credential references, or secret values.
- Made the handoff content digest exclude volatile fields (`generated_at`) so
  two snapshots of identical state compare equal, while any change to a stable
  field invalidates the digest. The document rejects `Bearer` tokens and
  `os-keyring:` references before emission.
- Summarized model history to one-line content previews and tool-call names so
  the receiving side sees what each model did without replaying full text or
  tool argument bodies.
- Added `karox session handoff` and `karox session lock` CLI commands. The
  handoff command re-validates the session repository binding; the lock command
  reports the durable mutation lease state (owner, expiry, expired flag).
- Proved cross-provider handoff end to end: model A changes a file, runs a
  check, and stops at the step limit; model B (a different model id) resumes the
  same session, observes the preserved `changed_files`, checks, and evidence,
  and completes verification without re-mutating. Both models are recorded in
  the durable provider history and the handoff document.
- Proved session locking: a second model attempting to run while the first
  holds the mutation lease is rejected with `SessionBusy`, preventing concurrent
  mutation. Recovery of pending tool calls already existed from Phase 2 and is
  exercised by the existing suite.

Changed files in Phase 6:

- `src/karox/cli.py`
- `src/karox/handoff.py`
- `tests/test_handoff.py`

Verification:

- `python -m compileall -q src tests`
- `python -m unittest discover -s tests -p "test_*.py"` — 170 passed (7 new)
- `python scripts/test_path_migration.py`
- `python scripts/test_app_entry.py`
- `python scripts/test_runtime_rebrand.py`
- `python scripts/test_notion_profile.py`
- `python scripts/test_notion_provider.py`
- `python scripts/test_notion_mcp_transport.py`
- `git diff --check`

End-to-end evidence:

- `ModelSwitchEndToEndTests.test_model_b_resumes_preserved_state_and_completes_verification`
  runs two distinct `AgentKernel` instances (model A then model B) against one
  leased session through the real `CoreRuntime`. Model B resumes, observes the
  real file change and check persisted by model A, completes git status/diff
  verification, and the session reports `verified`.
- `SessionLockCliTests.test_lock_reports_unlocked_then_acquired` drives
  `karox session lock` in a subprocess and confirms the lease visibility.
- `HandoffDocumentTests` cover the structured fields, secret-free strict-JSON
  output, stable digest, and compact model-history summarization.

Security boundaries, known problems, and migration risks:

- The handoff document is a read-only snapshot. It is generated from session
  state and never carries full chat history, credential references, or secret
  values; the generator rejects `Bearer` and `os-keyring:` markers before
  emission.
- `native ↔ hosted` and `hosted A ↔ hosted B` transfer require the Phase 7
  bridge/proxy layer for hosted clients to consume a handoff document over the
  wire. This phase proves the in-process session survives a model switch and
  records locking; hosted wire transfer remains pending.
- No remote provider was contacted and no external credential was required.
  Legacy bridge paths and the tested Notion integration were not modified.
  HyperAgent and PromptQL remain experimental.
- Context compaction, MCP proxy to hosted clients, Packs, and TUI remain
  pending.

## Phase 5 — MCP client

Completed:

- Added a secret-free, atomic MCP server registry with validated server identity,
  transport, endpoint metadata, per-tool read-only classification, and bounded
  size/timeout/retry limits. Credential values are forbidden in registry
  configuration; secret-like environment and header values require an opaque
  `os-keyring:mcp/<name>` reference.
- Added an MCP-only credential store backed by the operating-system keyring that
  resolves secrets only when constructing a request, rejects control characters
  and oversized values, and never returns a secret value. CLI output exposes only
  an opaque reference and a masked fingerprint.
- Implemented bounded stdio and Streamable HTTP clients over the installed MCP
  SDK: real JSON-RPC initialize / tools list / tool call, strict JSON-Schema
  validation, schema-digest binding, message and result size limits, timeouts,
  classified transport/protocol/access/timeout errors, and secret-safe redaction
  of remote errors and tool descriptions.
- Limited transport retries to retryable read-only failures before a usable
  streamed response begins; mutating calls and post-boundary failures are not
  retried. A mutating call whose outcome is unknown after a transport fault
  surfaces as `McpUnknownOutcome` rather than a silent success.
- Bound selected MCP tools onto dynamic Core tools behind the `MCP_CALL`
  capability. The binding re-reads the registry record at the authorization
  boundary so an approved tool cannot run against stale command, URL, credential,
  or transport configuration, and rejects identity, schema, and permission
  changes before execution.
- Added `mcp server add/remove/list/show/inspect/doctor`,
  `mcp session select/deselect/permissions`, `mcp call`, and
  `mcp credential set/show/delete/doctor` CLI commands, plus MCP selection
  integration into `karox agent run`. Only explicitly `allow`ed tools are
  described to the provider; `ask` fails closed until the user updates the
  session selection.
- Added an end-to-end test harness using a *real* MCP server built on the
  installed SDK for both transports: a stdio echo server and a Streamable HTTP
  echo server with bearer auth, each driven through `McpClient` and
  `CoreRuntime` rather than a mock.

Changed files in Phase 5:

- `pyproject.toml`
- `src/karox/agent.py`
- `src/karox/cli.py`
- `src/karox/core.py`
- `src/karox/models.py`
- `src/karox/policy.py`
- `src/karox/mcp_client.py`
- `tests/_mcp_echo_server.py`
- `tests/_mcp_http_server.py`
- `tests/test_mcp.py`

Verification:

- `python -m compileall -q src tests`
- `python -m unittest discover -s tests -p "test_*.py"` — 163 passed (44 new)
- `python scripts/test_path_migration.py`
- `python scripts/test_app_entry.py`
- `python scripts/test_runtime_rebrand.py`
- `python scripts/test_notion_profile.py`
- `python scripts/test_notion_provider.py`
- `python scripts/test_notion_mcp_transport.py`
- `git diff --check`

End-to-end evidence:

- `StdioMcpEndToEndTests` launches the deterministic stdio echo server as a
  subprocess; `McpClient` performs a real JSON-RPC handshake, discovers both
  tools, and the read-only `echo` call returns its text through `CoreRuntime`.
  The mutating `write_note` call requires a lease and idempotency key and is
  idempotently replayed on a second identical call.
- `HttpMcpEndToEndTests` starts a real Uvicorn Streamable HTTP server built on
  the MCP SDK's `StreamableHTTPSessionManager`; the bearer token is injected
  from an opaque `McpCredentialStore` reference, never persisted in the
  registry, and never written to the audit log. A missing credential fails
  closed at discovery.
- The servers are deterministic local fakes. This proves the genuine transport
  and CLI/Core integration without requiring a paid or remote MCP server, but is
  not evidence of interoperability with every MCP vendor.

Security boundaries, known problems, and migration risks:

- The OS keyring is required for MCP credential storage. `credential doctor`
  reports absence and KaroX fails closed instead of writing plaintext
  credentials; the in-memory fake backend is used only by tests where the
  keyring is unavailable.
- MCP stdio transport spawns a child process with `child_process_environment()`
  and the configured, credential-injected environment. It is not an
  operating-system sandbox; a granted `MCP_CALL` capability lets a configured
  server run arbitrary code within its process rights.
- The MCP SDK's stdio transport emits benign `ResourceWarning` messages from
  unclosed memory streams during subprocess teardown on some platforms. The
  warnings do not affect test outcomes or leaked file handles after process
  exit; the suite is run with `-W ignore::ResourceWarning` for deterministic
  output.
- External JSON-Schema validation enforces the safe subset understood by Core
  (types, properties, required, items, additionalProperties). Unknown draft
  keywords are ignored so a valid remote schema is not rejected merely because
  Core does not implement every feature, but unknown declared types never become
  an allow-all rule.
- No remote, third-party, or paid MCP server was contacted. Legacy bridge paths
  and the tested Notion integration were not modified. HyperAgent and PromptQL
  remain experimental.
- Context compaction, unified cross-provider handoff, MCP proxy to hosted
  clients, Packs, and TUI remain pending.

## Commit record

- `4a8278f fix: fail closed when runtime key is missing`
- `b5488a4 docs: define vNext hybrid runtime architecture`
- `353158d feat: add policy-gated core runtime foundation`
- `3ca96cf docs: record Phase 1 runtime foundation`
- `6f05b61 feat: add native agent vertical slice`
- `d0dea44 docs: record Phase 2 native vertical slice`
- `6a3e521 feat: add provider routing and credential management`
- `ceaee0b docs: record Phase 3 provider routing`
- `5f497bb feat: add secure lazy skill runtime`
- `743ef9f docs: record Phase 4 skill runtime`
- `555d4ee feat: add bounded MCP client with stdio and streamable HTTP`
- `dd3318b docs: record Phase 5 MCP client`

No merge, push, or release has been performed.
