# KaroX vNext Implementation Status

Last updated: 2026-07-25
Branch: `codex/vnext-hybrid-runtime`  
Base: `main` at `a5c233a`

## Status summary

| Phase | Status | Evidence |
| --- | --- | --- |
| 0 — audit/design | Complete | Baseline suite passed; required documents reviewed and validated |
| 1 — foundation | Complete | Core, policy, cross-process sessions, migration, and CLI tests |
| 2 — native vertical slice | Complete | Subprocess CLI E2E covers provider → agent → Core → approved verification |
| 3 — providers/credentials | Complete | Adapter, registry, keyring, routing, fallback, budget, and canonical-usage tests |
| 4 — Skills | Complete | Secure discovery, content-bound grants, permissions, CLI, and Agent integration |
| 5 — MCP client | Complete | Registry, credentials, selection, Core integration, and real stdio/HTTP E2E |
| 6 — unified handoff | Complete | Bounded structured handoff, model switch, locking, and restart recovery |
| 7 — MCP proxy/bridges | Complete | Built-in Core and external MCP allowlists over authenticated MCP/OpenAPI wire E2E |
| 8 — Pack SDK | Complete | Strict manifest, declared-file install, compatibility, integrity doctor, and CLI E2E |
| 9 — TUI | Complete | Full-screen Textual chat/task client, provider onboarding, status view, hosted bridge/tunnel setup, and line-mode fallback |
| 10 — benchmark/readiness | Complete | Current 592-test suite plus KB-HYBRID-01..10 functional run records |
| 11 — outbound target ask | Complete | `target ask` CLI/TUI against the verified PromptQL Natural Language API contract, with mocked-HTTP contract tests |
| 12 — web MCP OAuth | Complete (local contract) | ChatGPT/Claude bridge profiles with OAuth discovery, DCR, PKCE, rotating refresh tokens, replay revocation, and real HTTP/MCP tests; live account runs pending |

## Phase 12 — ChatGPT/Claude web MCP OAuth

Completed:

- Applied the useful part of AgentDock's architecture to KaroX without treating
  AgentDock as a model-inference API. `chatgpt-web` and `claude-web` are hosted
  bridge profiles, not entries in the model selector.
- Added RFC-style protected-resource and authorization-server discovery,
  Dynamic Client Registration for public clients, Authorization Code with PKCE
  S256, exact client/redirect/resource binding, short-lived access tokens,
  rotating refresh tokens, and token-family revocation on refresh replay.
- Added a local KaroX approval page. The existing OS-keyring bridge secret is
  used as its password and is never sent to the web MCP client.
- Added `bridge serve --public-url HTTPS_ORIGIN`; OAuth profiles require it and
  reject OpenAPI mode. The public origin is stable and explicit rather than
  trusted from attacker-controlled forwarding headers.
- Added `bridge connect chatgpt-web|claude-web` as the normal entry point. One
  CLI process creates the session and temporary credential, starts the
  Cloudflare HTTPS tunnel, binds its exact origin into OAuth, starts the local
  MCP bridge, supervises both children, and cleans up on `Ctrl+C`.
- Kept the existing Core/session/origin/allowlist boundary unchanged: OAuth
  authenticates the hosted client but does not grant tools that were not
  selected with `--tool` or `--server`.

Verification:

- `tests/test_oauth_bridge.py` drives discovery, DCR, the approval page, PKCE
  exchange, authenticated MCP initialize, refresh rotation, replay revocation,
  hostile redirect rejection, and failed-binding retry over a real HTTP server.
- `tests/test_bridge.py` covers profile metadata plus CLI fail-closed rules for
  missing public URLs and the wrong protocol.

Limitations:

- Dynamic clients and grants are process-local; restarting the bridge revokes
  them and requires the web connector to authenticate again.
- No live ChatGPT Business/Enterprise/Edu workspace or Claude paid-account run
  is recorded, so both profiles remain honestly `experimental`.
- A Quick Tunnel URL changes after restart. `bridge connect` binds the exact
  generated origin before starting OAuth, but the web connector must be updated
  after a restart. `--tunnel custom --public-url HTTPS_ORIGIN` supports a
  separately provisioned stable reverse proxy while the bridge lifecycle stays
  in the CLI.

## Post-review hardening

- Both platform installers install the vNext artifact and expose it through
  the primary `karox` command. With no arguments it opens the interactive
  shell; `karox-vnext` remains a forwarding compatibility alias.
- CI builds/installs that artifact and runs the complete suite on three OSes.
- Streamable HTTP uses stateless JSON responses, eliminating the AnyIO stream
  leak observed in the stateful SSE test server.
- Current local evidence is 592 tests with three platform skips on Windows, run as
  `python -m unittest discover -s tests -p "test_*.py"` — the runner CI uses.
  592 is what `python -m pytest --collect-only -q tests` reports too; a pytest
  *pass* tally is deliberately not quoted anywhere, because pytest adds a subtest
  count whose value moves between runs. A remote matrix result is still pending.
  Every published copy of these figures is verified by
  `scripts/check_test_count.py`, because all three of them had already gone stale
  together at 536.
- The teardown flake previously recorded here for
  `test_core_checks.CheckRunTests.test_a_timeout_caused_by_a_clamp_explains_itself`
  had a real mechanism: these tests kill process trees on purpose, and on Windows
  the directory a killed child was running in stays open until the OS finishes
  tearing the process down. The teardown now retries for up to five seconds
  (`cleanup_temporary_directory` in `tests/_support.py`) instead of failing on a
  handle that is already closing. It deliberately does not ignore cleanup errors:
  a handle that never clears still raises, so a genuine leak stays loud. The
  retry is covered by `TemporaryDirectoryCleanupTests` in both directions.
- The intermittent `test_packs.PackCliTests.test_full_lifecycle` failure -- once
  per full suite run, never in isolation -- was the same Windows mechanism, but on
  the product side rather than the test side, and so was a real defect:
  `karox pack remove` reported "cannot remove installed pack" and exit 2 whenever
  another process held a transient handle on a pack file. Both destructive
  filesystem operations in `packs.py` now retry briefly. Reproduced by holding a
  handle on an installed `skills/SKILL.md`, which yields exit 2 with nothing on
  stdout -- which is why the old assertion could only report a bare `2 != 0`.
- Correction to every earlier phase entry below that calls Notion "the only
  tested hosted bridge": the Notion profile is now `tested_legacy`. Its evidence
  (`scripts/test_notion_mcp_transport.py`) exercises `server/notion_gateway.py`,
  which is a different HTTP server from this runtime's bridge, so it never
  supported a `tested` label here. No bridge profile is `tested` today. The
  historical entries below are left as written; this bullet is the current fact.
- Hosted bridge E2E now covers explicit built-in Core tools over real
  Streamable HTTP MCP and OpenAPI wire servers. The OpenAPI path includes
  `/session` and `/context/brief`, mutation idempotency, bearer denial, and
  dynamic credential rotation. PromptQL remains experimental until a live
  product run is recorded.

## Phase 11 — outbound target ask

Completed:

- Added the outbound half of the bridge contract: the CLI (and the interactive
  shell) can call a hosted agent that exposes a documented invocation API. The
  first and only supported target is PromptQL's Natural Language API.
- Verified the PromptQL contract against the official `hasura/promptql-python-sdk`
  source (`client.py`): `POST {api_base_url}/query`, `Authorization: Bearer
  {api_key}`, default base `https://api.promptql.pro.hasura.io`, v2 body with
  `ddn.build_version` or `ddn.build_id` and `stream:false`, response
  `assistant_actions` / `modified_artifacts`.
- PromptQL is a hosted agent (actions run server-side; it returns
  `assistant_actions`, not tool-calls), so it is exposed as a dedicated
  `target ask` command rather than a provider in the tool-calling routing loop.
  Mixing the two would be a semantic mismatch.
- Credentials stay in the OS keyring (`os-keyring:provider/<name>`), resolved at
  request time via `CredentialStore.accessor`, and the live key is redacted from
  results, errors, and logs. HTTPS-or-loopback is enforced before the credential
  leaves the process.
- v1 (`ddn_url` with explicit LLM config) and SSE streaming are deferred with
  explicit, honest errors — not half-built shims.

Changed files in Phase 11:

- `src/karox/promptql_outbound.py` (new)
- `src/karox/cli.py`
- `src/karox/tui.py`
- `tests/test_promptql_outbound.py` (new)
- `tests/test_ecosystem.py`
- `docs/vNext/CONNECTIVITY.md`
- `examples/promptql-connect.md`

Verification:

- `python -m compileall -q src tests`
- `python -m unittest discover -s tests -p "test_*.py"`
- `git diff --check`

End-to-end evidence:

- `PromptQLTargetConfigTests` (5) assert the fail-closed configuration rules:
  a target without `build_version`/`build_id`, a v1-only `ddn_url` target, a
  target with both, a missing credential reference, and base URL normalization.
- `PromptQLClientContractTests` (10) drive the client against a fake
  `httpx.Client`: the exact `/query` endpoint, `Authorization: Bearer` header,
  v2 body shape (`build_version` and `build_id` alternatives, prior interactions
  appended, `stream:false`), 401→access denied, 500→invocation error with the key
  redacted, response-level key redaction, non-loopback HTTP rejection before any
  network, loopback allowance, empty-message rejection, and a failing
  credential accessor.
- `EcosystemTests.test_target_ask_invokes_promptql_natural_language_api` drives
  the real CLI path end to end: `credential set` → `target add` → `target
  configure` → `target ask`, asserts the request URL/header/body and that the
  API key never reaches stdout or stderr.

Security boundaries, known problems, and migration risks:

- No live PromptQL run has been recorded. The contract is exercised only against
  a mocked HTTP transport; the PromptQL target status remains experimental, not
  `tested`.
- SSE streaming and v1 `ddn_url` mode are deferred with explicit errors. A user
  who needs them receives a clear message instead of a half-built path.
- The architectural blockers from the prior technical review (proxy fail-open,
  lease fencing, verification no-op acceptance, process-execution confinement,
  MCP secret reflection, etc.) are out of scope for this phase. This phase
  closes the outbound-direction product gap and the false documentation claim
  that PromptQL had no public outbound API; it does not re-open the readiness
  gate.

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

Next actions for Phase 9:

1. Build a fast, optional TUI over the already-working backend; the backend
   must remain usable without the TUI (line-mode and non-interactive CLI).
2. Keep business logic out of the TUI; the TUI only renders state and forwards
   commands to the existing CLI handlers.
3. Support the slash-command surface (help, provider, model, status, plan,
   diff, tests, checkpoint, cost, context, handoff, bridge, permissions,
   doctor, exit).
4. Add JSON output for automation, stdin support, and CI mode alongside the TUI.
5. Add tests that exercise the TUI shell without coupling to a specific
   terminal library's rendering internals.

## Phase 8 — Pack SDK

Completed:

- Defined a strict `karox-pack.toml` manifest with manifest version, name,
  semantic version, description, authors, license, KaroX version range,
  supported platforms, tools, skills, MCP declarations, detectors, commands,
  templates, tests, health checks, and permissions. Unknown fields are rejected
  for the current major version; unsupported permissions and platforms are
  rejected; `network` requires `process.run`.
- Added a template generator (`karox pack create`) that emits a minimal,
  testable sample pack (`project-inspector`) with one read-only `repo.read`
  tool, a Skill, a detector, a test harness marker, and no network or external
  key. The manifest keeps `[[tools]]` last so all top-level keys parse
  correctly.
- Implemented the install/remove/list/inspect/doctor/enable/disable lifecycle:
  manifest and content-hash validation in staging before atomic activation,
  namespace collision detection, immutable installed copies, and doctor that
  verifies installed state without mutating the machine.
- Enforced permission approval: every elevated `permissions` entry must be
  explicitly user-approved at install; there is no auto-grant. Tool
  capabilities are re-authorized by Core policy on every call, not at install.
- Enforced path confinement: every referenced file must stay inside the Pack
  directory; traversal references are rejected. Installed Pack copies are
  immutable; an enabled Pack cannot be removed until disabled.
- Added `karox pack create/install/remove/list/inspect/doctor/enable/disable`
  CLI commands.

Changed files in Phase 8:

- `src/karox/packs.py`
- `src/karox/cli.py`
- `tests/test_packs.py`

Verification:

- `python -m compileall -q src tests`
- `python -m unittest discover -s tests -p "test_*.py"` — 207 passed (14 new, 1 skipped)
- `python scripts/test_path_migration.py`
- `python scripts/test_app_entry.py`
- `python scripts/test_runtime_rebrand.py`
- `python scripts/test_notion_profile.py`
- `python scripts/test_notion_provider.py`
- `python scripts/test_notion_mcp_transport.py`
- `git diff --check`

End-to-end evidence:

- `PackCliTests.test_full_lifecycle` drives `karox pack` in a subprocess
  through create/install/list/doctor/enable/disable/remove, asserting each
  step's JSON output and that the installed pack disappears after removal.
- `PackLifecycleTests` cover the roundtrip, duplicate-install rejection,
  enabled-pack removal rejection, permission approval with explicit grants,
  path traversal rejection, and doctor detection of a broken pack.
- `PackManifestTests` cover the strict schema: unknown fields, unsupported
  version, bad name/version, network-without-process.run, unsupported
  permission, and unsupported platform.

Security boundaries, known problems, and migration risks:

- A Pack is a declarative extension, not a plugin installer. KaroX does not
  run Pack-provided setup scripts or arbitrary code at install; only the
  declared tool capabilities and elevated permissions are gated, and tool
  calls are re-authorized by Core policy at execution time.
- The Pack SDK gates permissions and path confinement but does not yet wire
  Pack-declared MCP servers into the session; MCP declarations remain metadata
  until a Pack integration binds them through the Phase 5 MCP client with an
  explicit per-session approval.
- Pack tool execution through Core is not yet wired in this phase; the SDK
  proves the manifest, lifecycle, permissions, and path-confinement contract.
  Actual tool registration for a Pack remains pending.
- No network or external key was required. Legacy bridge paths and the tested
  Notion integration were not modified. HyperAgent and PromptQL remain
  experimental.
- The hybrid runtime benchmark remains pending.

## Phase 9 — terminal client

Completed:

- `karox` now opens a full-screen Textual application with a conversation view,
  composer, repository/model/session/bridge status, keyboard shortcuts, and a
  command palette. `karox SUBCOMMAND` remains the automation interface.
- Ordinary input is always treated as a natural-language agent task. It is
  never forwarded to `argparse`; malformed CLI diagnostics therefore cannot
  leak into the chat experience.
- First run opens a provider setup screen for OpenAI Responses,
  OpenAI-compatible endpoints, Anthropic, or Gemini. Secrets go directly to the
  OS keyring and the chosen model becomes the active route.
- First-run choice supports API, hosted site, or both. The complete flow is
  keyboard-operable: numeric choices, Tab/Shift+Tab, arrows, Space/Enter, F5
  discovery, Alt+Up/Down model switching, F10 validation, and Escape.
- Model discovery reads provider IDs and advertised context/output limits. All
  values remain editable for providers that omit metadata. A candidate route is
  activated only after a real minimal model request succeeds.
- `Ctrl+B` opens hosted-client setup for PromptQL/OpenAPI, Notion/MCP, generic
  MCP, or HyperAgent. It creates a repository-bound session and credential,
  starts the allowlisted bridge, and optionally starts Cloudflare Tunnel to
  produce the public connector URL.
- Redirected stdin and non-interactive terminals retain a bounded line-mode
  fallback; JSON and explicit CLI commands remain usable in CI.

Changed files in Phase 9:

- `src/karox/tui.py`
- `src/karox/cli.py`
- `tests/test_tui.py`

Verification:

- `python -m compileall -q src tests`
- `python -m unittest discover -s tests -p "test_*.py"` — 221 passed (14 new, 1 skipped)
- `printf '/help\n/quit\n' | python -m karox.cli` — renders line-mode help and
  exits cleanly without attempting full-screen terminal control.
- `git diff --check`

End-to-end evidence:

- `InputRoutingTests` prove normal text, including `-`, is never parsed as CLI
  syntax and that task execution carries an explicit verification allowlist.
- `LineModeTests` cover task routing, friendly setup guidance, help, EOF, and
  unknown-command behavior for redirected streams.
- `FullScreenAppTests` run the Textual application headlessly and verify the
  composer/status view plus natural-language delegation to the agent backend.

Security boundaries, known problems, and migration risks:

- The UI adds no authorization bypass: agent tasks and hosted calls still cross
  the CLI/service layer, Core policy, repository binding, leases, and audit.
- Provider secrets and bridge credentials are written to their dedicated OS
  keyring scopes. Generated bridge credentials are displayed once for connector
  setup and are not persisted in UI configuration.
- Textual is an installed runtime dependency. Redirected input uses line mode;
  a broken minimal installation receives a reinstall diagnostic.
- The hybrid runtime benchmark remains pending.

## Phase 10 — benchmark and release readiness

Completed:

- Formalized the gates the roadmap names for Phases 2/5/6/7 plus the
  release-readiness benchmark as **KB-HYBRID-01..10**, each exercising the
  real runtime (real `AgentKernel` loop, real stdio MCP server, real session
  store, real proxy boundaries, real bridge credentials) through deterministic
  scripted providers.
- Captured structured raw run records (pass/fail, latency, usage, cost,
  evidence kinds, limitations, failure reason) per gate. Raw run records are
  regenerated by the suite and are intentionally not committed as frozen
  numbers, because latency/usage vary by machine and run; reproducibility is
  the contract.
- Added the new harness this phase required: **KB-HYBRID-05** drives
  `AgentKernel` with a scripted provider tool-call to an MCP alias so the
  kernel routes the call through `CoreRuntime` to the real external stdio MCP
  server — the path no earlier phase test exercised.
- Added an aggregation gate that asserts every benchmark produced a
  well-formed record and that all gates passed, so a silent skip or a missing
  gate fails the suite. The suite prints a summary table of raw run records.
- Wrote a truthful implemented-only overview (`docs/vNext/README.md`, distinct
  from the shipping product README), a release-readiness report
  (`docs/vNext/RELEASE-READINESS.md`) that recommends an action and lists
  blockers, and refreshed `MIGRATION.md` with implementation status.

KB-HYBRID gates and what each proves:

- **01** — a failing check cannot report success (durable verification chain
  resets on failure).
- **02** — proxy enforces both the hosted-client policy (`MCP_CALL`) and the
  session MCP selection; descriptors are secret-free.
- **03** — model A stops at the step limit on a leased session; model B
  resumes the same session and verifies without re-mutating.
- **04** — fresh `SessionStore`/`CoreRuntime` handles on the same on-disk
  paths recover the session and verify.
- **05** — the native agent routes a scripted tool-call to an MCP alias through
  Core to the real external stdio echo server.
- **06** — bridge credentials live in the `KaroX/bridge` namespace; a
  provider-namespace reference cannot resolve; profile statuses are honest.
- **07** — a second lease on a held session is blocked by `SessionBusy`.
- **08** — the bundled Notion transport regression script exits 0.
- **09** — the structured handoff document is strict-JSON, content-digested,
  secret-free, and carries both models' history.
- **10** — a capstone verified run accumulates routed cost end-to-end
  (`0.40 USD`) in the session store and the agent report.

Changed files in Phase 10:

- `tests/_bench_support.py`
- `tests/test_benchmark.py`
- `docs/vNext/README.md`
- `docs/vNext/RELEASE-READINESS.md`
- `docs/vNext/MIGRATION.md`
- `docs/vNext/IMPLEMENTATION_STATUS.md`

Verification:

- `python -m compileall -q src tests`
- `python -m unittest discover -s tests -p "test_*.py"` — 292 passed (2 skipped on Windows)
- `python -m unittest discover -s tests -p "test_benchmark.py" -v` — 10/10 KB-HYBRID gates pass
- `python scripts/test_notion_mcp_transport.py` (KB-HYBRID-08 in-env)
- `git diff --check`

Security boundaries, known problems, and migration risks:

- The benchmark adds no new trust boundary: every gate exercises the real
  runtime through its existing authorization paths. No secret value is written
  to records, logs, or evidence; credentials are keyring references.
- No live service, paid account, or external key was required for
  KB-HYBRID-01..07, 09, and 10. KB-HYBRID-08 runs the bundled Notion transport
  regression and fails honestly if that environment is unavailable.
- Live OpenAI/Anthropic/Gemini conformance and a macOS/Linux cross-platform
  matrix are **not** recorded on this branch; they need a human decision and
  are listed as blockers in `RELEASE-READINESS.md`.
- Context compaction remains pending. Pack-declared MCP remains metadata only
  this phase. No legacy code was removed; vNext installs beside the current
  runtime.

## Phase 7 — MCP proxy and bridge layer

Completed:

- Added a declarative bridge profile registry with honest statuses. Notion is
  `tested` (covered by existing regression scripts), the generic Streamable HTTP
  client is `protocol_compatible`, PromptQL and HyperAgent are `experimental`
  because they lack a dedicated product end-to-end test. A `tested` profile
  requires verified versions; a `planned` profile cannot use stdio.
- Added a dedicated bridge credential store in a separate `KaroX/bridge`
  OS-keyring namespace, distinct from provider and MCP secrets. Credentials are
  high-entropy random tokens, never persisted in plaintext configuration, and
  support generation, rotation (which revokes the prior value), and revocation.
  CLI output exposes only an opaque reference and a masked fingerprint.
- Added the `McpProxy`: a hosted client receives only the explicitly allowlisted
  session MCP servers, and only the tools with an explicit `allow` permission.
  The proxy never auto-exposes every installed MCP, and proxy descriptors carry
  no server command, URL, environment, or credential -- only name, description,
  input schema, and read-only classification.
- Enforced a second authorization boundary: the hosted-client origin must be
  `HOSTED_CLIENT`, and both the hosted-client policy (`MCP_CALL`) and the
  external-MCP server selection must permit each call. Schema and read-only
  classification changes are detected at the boundary and fail closed.
- Added `karox bridge list/show/doctor` and
  `karox bridge credential set/show/rotate-key/revoke` CLI commands. The
  existing legacy Notion gateway in `server/` was not modified; this layer is a
  generalization that reuses the Phase 5 MCP client and the Phase 6 session
  lease for hosted-client access.

Changed files in Phase 7:

- `src/karox/bridge.py`
- `src/karox/proxy.py`
- `src/karox/cli.py`
- `tests/test_bridge.py`

Verification:

- `python -m compileall -q src tests`
- `python -m unittest discover -s tests -p "test_*.py"` — 193 passed (23 new, 1 skipped)
- `python scripts/test_path_migration.py`
- `python scripts/test_app_entry.py`
- `python scripts/test_runtime_rebrand.py`
- `python scripts/test_notion_profile.py`
- `python scripts/test_notion_provider.py`
- `python scripts/test_notion_mcp_transport.py`
- `git diff --check`

End-to-end evidence:

- `McpProxyTests.test_e2e_proxy_executes_read_only_call_through_two_boundaries`
  drives a real stdio MCP echo server through `McpProxy`: a hosted-client origin
  with an explicit `MCP_CALL` policy grant calls `echo` and receives its text,
  with the proxy allowlist limiting exposure to the selected `echo` server only.
- `McpProxyTests.test_proxy_only_exposes_allowed_servers_and_tools` confirms a
  `deny`-permitted tool is invisible to the hosted client, and descriptors are
  secret-free.
- `McpProxyTests.test_proxy_rejects_server_not_in_allowlist` confirms a second
  selected server kept out of the proxy allowlist is invisible.
- `BridgeCliTests` cover `bridge list/show/doctor` with honest status reporting.

Security boundaries, known problems, and migration risks:

- The proxy is an authorization boundary, not a transport server. It exposes
  descriptors and routes calls through the existing Phase 5 MCP client; the
  hosted-client wire transport (Streamable HTTP MCP server) remains the
  legacy Notion gateway until a generalized server is added. This phase proves
  the proxy authorization and secrets isolation, not hosted wire transport.
- The OS keyring is required for bridge credential storage. `bridge doctor`
  reports absence and KaroX fails closed; the bridge-credential tests use an
  in-memory backend when the keyring is unavailable (the CLI doctor test skips).
- No remote hosted client was contacted. The legacy Notion gateway and its
  regression scripts were not modified. HyperAgent and PromptQL remain
  experimental -- their profiles describe connection expectations but do not
  claim a working product integration.
- A KaroX-as-MCP-server namespace/capability boundary (`karox.*` tools) and
  hosted wire transport remain pending. Context compaction, Packs, and TUI
  remain pending.

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
- Context compaction remains pending.

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
- `3c8f070 feat: add unified session handoff and mid-task model switch`
- `11b4e0b docs: record Phase 6 unified handoff`
- `57592ff feat: add MCP proxy and bridge layer for hosted clients`
- `2d07dda docs: record Phase 7 MCP proxy and bridge layer`
- `6bdf7e3 feat: add KaroX Pack SDK with manifest and lifecycle`
- `a640ee2 docs: record Phase 8 Pack SDK`
- `319e53d feat: add optional TUI shell over the CLI backend`
- `533aa6b docs: record Phase 9 TUI`
- `10495f7 feat: add hybrid runtime benchmark (KB-HYBRID-01..10)`

No merge, push, or release has been performed.
