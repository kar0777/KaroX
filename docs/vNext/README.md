# KaroX vNext — hybrid local runtime for AI agents

This README describes **only what is implemented and tested** on the
`codex/vnext-hybrid-runtime` branch. It is not the shipping product README and
makes no claim about features that are not backed by a passing test on this
branch. Implementation status and per-phase evidence live in
`IMPLEMENTATION_STATUS.md`; the cross-gate benchmark lives in
`RELEASE-READINESS.md`.

No merge, push, or release to `main` has been performed. vNext installs beside
the current runtime and does not remove any working capability.

## What vNext is

KaroX vNext turns a local Git repository into a secure workspace for AI agents
and hosted clients. The runtime is a hybrid: a bounded **native agent** runs
locally under a capability-gated Core, while **hosted clients** (PromptQL,
Notion Custom Agents, generic MCP/OpenAPI clients) reach the same Core through
an authenticated, allowlisted bridge. A **Pack** and **Skill** layer extends the
runtime declaratively without granting itself capabilities.

The single local-action boundary is the **Core Runtime**. Every local action
(repo read/write, process exec, checks, git, external MCP) passes through Core
under an `Origin` identity, a `Capability`, a session, and a mutation lease.
Default is deny; extensions cannot grant themselves capabilities.

## What is implemented and tested

Run the suite:

```bash
python -m unittest discover -s tests -p "test_*.py"
```

That reports `Ran 598 tests` with 3 environment/platform skips on Windows, and it
is the runner CI executes. 598 is also what `python -m pytest --collect-only -q
tests` reports, which is why it is the figure quoted here: collection is stable
run to run, whereas a pytest pass tally is not — pytest adds a subtest count on
top of the test count and that count moves between runs. A bare `python -m pytest
-q` at the repository root collects 603, because it also picks up the five legacy
checks in `scripts/test_karox4_units.py`.

`python scripts/check_test_count.py` recomputes both figures and fails when any
document has drifted from them, so the numbers above cannot quietly go stale
again.

The KB-HYBRID-01..10 benchmark (`tests/test_benchmark.py`) regenerates raw run
records (latency, usage, cost, evidence) from a clean checkout.

| Layer | What works | Evidence |
| --- | --- | --- |
| Core Runtime | Origin/capability/session enforcement, mutation leases, idempotency, audit, evidence | `test_core.py`, `test_sessions.py`, `test_policy.py` |
| Native agent | Bounded loop, durable write→check→git verification, failed-check-can't-verify, step/wall-time limits | `test_agent.py` (subprocess CLI E2E against a real fake OpenAI SSE server) |
| Providers | OpenAI-compatible streaming, Anthropic Messages, Gemini adapters; keyring references; routing, fallback, budgets, usage, cost | `test_providers.py`, `test_provider_adapters.py`, `test_routing.py`, `test_credentials.py` |
| Skills | Multi-directory discovery, lazy content, permission gating, validation, CLI | `test_skills.py`, `test_skill_cli.py` |
| MCP client | Real stdio + Streamable HTTP, registry, credentials, selection, Core integration, idempotent mutating replay | `test_mcp.py` (real bundled stdio + HTTP servers) |
| Unified handoff | Structured handoff doc, mid-task cross-provider model switch, locking, recovery across restart | `test_handoff.py` |
| Hosted bridge | Explicit built-in Core and external-MCP allowlists over Bearer-authenticated Streamable HTTP MCP or OpenAPI; rotation/revocation and idempotency | `test_bridge.py`, `test_hosted_bridge.py` |
| Pack SDK | Strict manifest, template generator, install/remove/list/inspect/doctor/enable/disable, permission approval, path confinement | `test_packs.py` |
| Terminal UI | Full-screen neutral Textual client with persisted Russian/English choice, slash-command menu, natural-language task input, keyboard-only `/connect` API/site/both setup, model and token-limit discovery with editable confirmation, connection probe, status, and hosted MCP/OpenAPI bridge setup; redirected stdin uses line mode | `test_tui.py` |
| Migration | Legacy discovery/import, versioned session schema, dry-run/JSON report | `test_migration_cli.py` |
| Benchmark | KB-HYBRID-01..10 raw run records (latency/usage/cost/evidence/limitations) | `test_benchmark.py` |

## Command surface (implemented)

```text
karox [paths | session | credential | provider | model | skill | mcp | bridge
      | pack | tui | agent | migrate | doctor]
```

- `agent run` — run/resume a bounded native-agent session with explicit
  repeatable `--verification-command` allowlists and JSON evidence.
- `provider`/`model` — add/list/show/test/select adapters and models.
- `credential` — OS-keyring credential lifecycle (provider/mcp/bridge scopes).
- `skill` — discover/inspect/validate skills.
- `mcp` — register/list/inspect/test/credential/credential-doctor external MCP.
- `bridge serve` exposes explicit built-in Core and/or selected external MCP
  tools over authenticated Streamable HTTP MCP or OpenAPI. MCP mutations use
  `_meta.karoxIdempotencyKey`; OpenAPI mutations use
  `X-KaroX-Idempotency-Key`.
- `bridge credential` — create, rotate, inspect, and revoke bridge credentials.
- `pack` — create/install/remove/list/inspect/doctor/enable/disable packs.
- `session` — list/show/revoke, structured handoff, lock state.
- `tui` — full-screen human client; the first launch asks only for a language,
  `/` opens the command menu, ordinary input runs an agent task, and `/connect`
  configures API models and hosted-client bridges.
- `migrate` — legacy discovery/import with dry-run and JSON report.
- `doctor` — diagnostics.

The installers expose vNext through the primary `karox` launcher. Running
`karox` with no arguments opens the full-screen terminal client; `karox SUBCOMMAND`
provides the scriptable command surface. `karox-vnext` is a compatibility alias
that forwards to the same entry point.

## Security posture (implemented)

- Credentials are OS-keyring references in dedicated namespaces
  (`KaroX/provider`, `KaroX/mcp`, `KaroX/bridge`); never plaintext in config,
  logs, headers, support bundles, session snapshots, or MCP results.
- Hosted exposure is explicit: built-in Core tools require `--tool`; external
  MCP servers require `--server` plus stored per-tool `allow` decisions. Both
  paths execute through Core and return secret-free descriptors.
- Session mutations require a lease plus idempotency key; concurrent mutation
  is blocked by `SessionBusy`; stale leases are fenced across processes and
  emergency revoke prevents new leases.
- Structured handoff documents are secret-free, strict-JSON, and
  content-digested (volatile fields excluded so the digest is stable).
- A failed check cannot report success: the durable verification chain (write
  `changed=True` → check `ok=True` → git.status → git.diff) resets on any
  failed step (KB-HYBRID-01).

## Honest limitations

- **Live services are opt-in and unlabelled-tested.** Provider adapters are
  proven against deterministic in-process/HTTP fakes; live OpenAI/Anthropic/
  Gemini E2E requires a user-supplied key and is not recorded on this branch.
- **Notion** is `tested_legacy`, not `tested`. Its only evidence,
  `scripts/test_notion_mcp_transport.py` (KB-HYBRID-08), drives
  `server/notion_gateway.py` — the legacy HTTP server, a different program from
  this runtime's bridge — so it proves nothing about `src/karox`. The profile is
  still usable, on the same footing as `generic-streamable-http`: the
  authenticated Streamable HTTP wire itself has vNext end-to-end coverage.
- **PromptQL** remains product-level `EXPERIMENTAL`: its OpenAPI wire path to
  built-in Core tools has local E2E coverage, but no live PromptQL run is
  recorded. **HyperAgent** has no dedicated product E2E.
- PromptQL's public docs do not document a remote web-agent invocation API.
  KaroX does not automate its logged-in website; CLI-to-service use requires a
  documented provider API.
- **Pack-declared MCP servers are metadata only** this phase; tool execution
  registration for a Pack remains pending.
- **Context compaction** is not implemented; the unified handoff carries full
  structured state but does not yet compress long histories.
- **Cross-platform matrix**: CI now builds and installs the wheel and runs the
  complete vNext suite on Windows, macOS, and Linux. That workflow is committed
  but no successful remote run is recorded in this document yet.

## Documents

- `ARCHITECTURE.md` — dependency rule and layering
- `SECURITY.md` — trust model, enforcement, credentials, threat-driven tests
- `SESSION-MODEL.md` — durable sessions, leases, idempotency, handoff
- `MCP-ARCHITECTURE.md` — MCP client/proxy and bridge
- `CONNECTIVITY.md` — provider APIs, PromptQL/OpenAPI, MCP, and CLI workflows
- `PROVIDER-SDK.md` — provider adapter contract
- `PACK-SDK.md` — pack manifest and lifecycle
- `MIGRATION.md` — legacy import strategy and compatibility phases
- `PRODUCT.md` — product framing
- `ROADMAP.md` — phase gates
- `IMPLEMENTATION_STATUS.md` — per-phase evidence and commit record
- `RELEASE-READINESS.md` — release readiness report and benchmark summary
