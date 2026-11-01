# Parity audit: v5 → 6.0.0a1

This record answers one question: does the clean release still do what the v5
laboratory did, and where it does not, was that a deliberate burial or a
regression? The audit compared both trees at file/function level and then fixed
the real gaps before the release gates were run.

## Method

- OLD: `D:\проекты\KaroX-v5` — 280 Python modules, ~165,800 lines.
- NEW: the race winner tree (`lab/kernel-duel-20261025/b/integrate`) —
  137 modules, ~69,100 lines at 6.0.0a3.
- Eight parallel auditors covered providers/API, MCP client, hosted bridge,
  kernel features, CLI/TUI, policy/security, packaging/dependencies and tests.
- Every claim below was checked against source or a runnable test; the first
  audit pass found 10 release test failures and 8 product gaps, all resolved
  before this record was written.

## Verdict

| Area | Status | Evidence |
| --- | --- | --- |
| Model API transports | **Parity** | `providers.py` and `provider_adapters.py` are byte-identical to v5: OpenAI Chat Completions/Responses, Anthropic Messages, Gemini GenerateContent, SSE, tool calls, reasoning, retries, usage. |
| Provider registry, presets, credentials | **Parity + improved** | Registry, controller, presets, pricing and keyring modules are byte-identical. New: `model discover`, pricing flags, stored-credential reuse, loopback key exemption, `credential setup`. |
| Routing, fallback, budgets | **Restored** | `routing.py` ported; `agent run` now accepts repeated `--route`, `--privacy-limit`, `--max-total-tokens`, `--max-cost`, `--currency`. |
| MCP outbound client | **Parity** | `mcp_client.py` differs from v5 by one deliberate hosted-catalogue branch. stdio/HTTP, schema digests, per-tool allow/ask/deny, OAuth, credential redaction and reconnect behavior remain. |
| MCP CLI mutations | **Fixed regression** | v5 took a mutation lease for `mcp call`; the first release extraction did not, so every mutating call failed. The lease and idempotent replay are now wired and covered by `tests/test_mcp_cli.py`. |
| ChatGPT/Claude/HyperAgent web bridge | **Restored** | Compact port of the v5 server core and lifecycle: OAuth 2.1, DCR, PKCE S256, approval page, rotating refresh tokens, Streamable HTTP MCP, explicit allowlist, Tailscale Funnel, saved profiles. |
| Existing connector compatibility | **Verified** | The real v5 `chatgpt-dev` profile, session id `web-saved-18870c825f9eba831276059d` and keyring approval-password entry are read by the new CLI without migration. |
| Agent kernel | **Parity + restored** | `agent.py`, `core.py`, `core_tools.py`, policy/risk are byte-identical. Hot-worker closure restored (`workspace_worker`, `workspace_transaction`, `unified_patch`), so `tests.run`, `repo.command`, `repo.apply_patch` no longer crash on import. |
| Project context, memory, artifacts, images | **Restored** | `project_context`, `project_map`, `map_service`, `memory`, `mode_artifacts`, `image_attachments` are wired into `agent run`; visual checker is injected when vision and Chromium are available. |
| Effort auto | **Fixed regression** | `--effort-level auto` now uses `effort_signals` and `recommend_effort`; previously it always resolved to medium. |
| CLI daily surface | **Improved** | `doctor`, `status`, `model discover/map/repair-selection`, `credential setup`, `paths --json`, short forms (`karox models`, bare `karox model`) and the full `bridge` group. |
| TUI | **Parity + improved** | The rebuilt `tui/` package (app, widgets, modals, backend, prefs, i18n, protocol) plus `/doctor`, `/bridge`, safer MCP defaults and authenticated remote-MCP fields. |
| Security boundary | **Stronger** | System folders and drive roots are refused as workspaces; instruction symlinks outside the repository are refused; discovered checks are not evidence without approval; Bypass requires an elevated profile. Core policy/risk/lease code is unchanged. |
| Packaging | **Improved** | Declared deps now include `starlette`, `uvicorn` and `psutil`; mypy has an explicit scope; `check_tree` counts 98 modules and no legacy surface. |
| Tests | **Restored + extended** | 22 → 1020 tests (1018 outside Windows): provider transports/registry/pricing, engine kernel and its ports, MCP client+CLI, policy/risk/action, sessions, OAuth, hosted bridge, launcher, Tailscale routes, browser/desktop units (including idle-page parking and process priority), model discovery, core modules, visual checks, requirement coverage, TUI, tree gates, and a guard that keeps the shipped 3D skill benchmark-neutral. |

## Restored or improved after the audit

- `karox bridge connect/serve/saved/status/start/stop/restart/oauth/credential`
  and the `hosted_core`/`bridge_*` modules.
- Hosted write path: `repo.write_file` through the bridge writes once; an
  identical retry returns `idempotent_replay: true` without a second write.
- Provider discovery and price metadata; route fallback and hard budgets.
- Project map, environment facts, bounded memory and durable Plan/Concept
  artifacts in the prompt/report.
- Image attachments and the independent visual checker for web tasks.
- Hot-worker repository/test tools; mutating MCP lease.
- Health/diagnostics (`doctor`, `status`) and the short daily aliases.
- Provider setup for local endpoints and repeated setups that reuse a saved key.

## Deliberately buried

These stay in v5 and are not accidental omissions:

- The vendored karo-core `engine/` and `karox core run`. The product kernel is
  the typed agent; a bash/completion engine needs an adapter before it can
  replace it.
- Browser control, desktop control, Chrome-extension bridges, hosted
  browser/dev-server tools, autonomy, mission control, orchestration, worktree
  pools and model-grant delegation.
- The legacy terminal shell, dashboards, session view and activity stream.
- ClickUp/Notion setup helpers, Ellipsis remote runtimes, ecosystem registries,
  mobile mission control and benchmark harnesses.
- OpenAPI bridge and interactive hosted approval. The MCP bridge is the supported
  hosted path.
- `git.push`, publish, deploy and auth mutations from a hosted profile.

The release keeps the verified loop and the surfaces a local coding agent needs;
it does not carry laboratory experiments merely because they existed.

## Remaining gaps

- Research subagents are not exposed.
- Per-session skill selection and skill-level capability grants are not exposed;
  the agent can still discover and load a skill.
- Automatic local Git rollback checkpoints are not created; the hosted Smart Stop
  checkpoint store is present.
- MCP `ask` is a refusal, not an interactive prompt. The TUI marks mutating
  tools `ask` and points to `karox mcp session select`.
- A live ChatGPT workspace write/restart run still requires the owner's account
  and a Funnel cutover. The OAuth/MCP/write/idempotency path is verified over
  real loopback HTTP, not through ChatGPT's servers.
- Cloudflare Quick Tunnel is not supported; use Tailscale Funnel or a custom
  HTTPS origin.

## Verification evidence

Commands run from the winner tree at `d210f86`, on a Linux sandbox (Python
3.13) rather than the Windows machine of the earlier evidence:

```text
python -m pytest -q                 # 1018 collected: 1004 passed, 5 skipped, 9 failed, 1 collection error (see below)
python -m ruff check src tests scripts
python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui
python scripts/check_tree.py        # ok · 145 modules · 75255 lines
```

`tests/test_computer_use_live.py` does not import on Linux (it needs Windows
`ctypes`). The nine failures fail the same way on `27f6122`, the commit before
the browser parking and the later gate fixes, in the same sandbox: the two `test_check_report.py`
path tests, the two open-terminal tests of `test_computer_use.py` (no desktop),
`test_cli.py::test_local_provider_setup_needs_no_key_and_doctor_is_healthy`,
`test_browser_live.py::test_detach_and_reconnect_with_ownership_proof`, and
three `test_engine_kernel.py` failure-map and advisory-rerun tests
(`test_failed_check_hands_the_model_a_parsed_failure_map`,
`test_focused_test_failure_is_mapped_for_the_model`,
`test_one_advisory_rerun_per_batched_response`). Three more fail now and then
on both commits: `test_browser_live.py::test_console_redaction_and_network_metadata`,
`test_browser_live.py::test_iframe_read` and
`test_engine_kernel.py::test_compaction_briefing_keeps_requirements_files_and_failure`.

The previous evidence, at `48195aa` on Windows, was 959 collected: 953 passed,
5 skipped and one failure under load,
`test_browser_live.py::test_console_redaction_and_network_metadata`: the suite
ran at idle priority beside three benchmark runs, and the page's fetch did not
reach `network_requests` within the test's 10 s window. Run alone it passed
three times out of three, and the whole file passed.

The suite above ran with `KAROX_BRIDGE_DIAGNOSTICS_JSON` unset. Under a live
bridge, which exports that variable to the processes it starts,
`test_bridge_core.py::BridgeWireSecurityTests::test_catalog_advertises_wire_names_and_accepts_the_dotted_alias`
also fails, because the catalog then lists `karox_bridge_diagnostics`.

Bridge wire evidence (local loopback, isolated config/runtime, no public
tunnel):

1. `bridge serve` started on `127.0.0.1`.
2. `/.well-known/oauth-protected-resource/mcp` and
   `/.well-known/oauth-authorization-server/mcp` returned `200`.
3. `/mcp` without a bearer returned `401`.
4. Dynamic client registration returned a client id; the approval page accepted
   the keyring password; PKCE token exchange returned an access token.
5. `initialize` and `tools/list` returned the selected tools.
6. `tools/call` read a real repository file.
7. A write call created the file once; an identical retry returned
   `idempotent_replay: true` and did not write again.
8. The real v5 `chatgpt-dev` profile and keyring entry were read successfully.

This is protocol and product-path evidence. It does not claim a live ChatGPT
workspace pass; that remains an external conformance step.