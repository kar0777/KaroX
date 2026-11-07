# Parity audit: v5 laboratory → KaroX 5.0.0

This record answers one question: what does the stable 5.0.0 line provide, what
was deliberately left out of the v5 laboratory, and what is still not proven?

## Method

- OLD: `D:\проекты\KaroX-v5` — 280 Python modules, ~165,800 lines.
- NEW: `b/karox5-refactor` at the 5.0.0 release head —
  **164 modules, 87,667 physical lines**.
- Independent read-only audits covered agent kernel/verification, CLI/TUI/MCP,
  browser/skills/packaging, policy/security, and release/migration.
- Claims below are checked against source or executed tests. Dynamic entrypoints
  (`hot_worker`, `python -m`, lazy facades, bridge argv, saved profiles) were
  checked before declaring code dead.

## Verdict

| Area | Status | Evidence |
| --- | --- | --- |
| Agent engine/kernel | **Parity + restored** | The vendored karo-core engine is the kernel; `engine_kernel.py` adapts it to typed Core tools. Completion evidence, failure maps, requirement coverage and run economy are tested. |
| Provider transports | **Parity** | OpenAI Chat Completions/Responses, Anthropic Messages, Gemini GenerateContent, SSE, tool calls, reasoning, retries, usage and pricing metadata. |
| Provider registry, discovery, credentials | **Parity + improved** | `.env`/keyring references, model discovery, presets, route fallback, hard token/cost ceilings, credential redaction. |
| MCP outbound | **Parity** | stdio/HTTP, schema digests, per-tool allow/ask/deny, OAuth, reconnect and credential handling. |
| MCP CLI mutations | **Fixed regression** | Mutating `mcp call` takes a lease, replays idempotently, preserves `--json` contracts, and now uses the consequence/RiskEngine path. |
| Hosted OAuth/MCP bridge | **Restored** | OAuth 2.1, DCR, PKCE S256, approval page, rotating refresh tokens, Streamable HTTP MCP, explicit allowlist, Tailscale Funnel, saved profiles. |
| Existing v5 bridge/profile compatibility | **Verified offline** | The real `chatgpt-dev` profile/session/keyring reference is read without migration; loopback OAuth/write/idempotency is covered. A live ChatGPT workspace run remains external. |
| Sessions, evidence, export | **Parity + fixed** | Schema 1, checksums, leases, resume/fork and proof readers. JSON export no longer truncates at 500 entries. |
| Browser control | **Present, tested offline/live-local** | 48 browser actions; live local page suite passes; owner queue has fail-closed timeout, teardown stops its thread, and late callbacks are ignored. |
| Desktop control | **Present, Windows offline** | Typed identity/approval tests; real Windows fixture tests are **NOT TESTED** in this pass. |
| CLI/TUI | **Present** | Full parser/JSON contract tests; TUI synthetic streaming/usage tests; `karox-vnext` alias. |
| Skills/prompts/package data | **Present** | Eight packaged skills, prompts/theme/catalog/NOTICE/license; `repo-selfcheck` helper explicitly declared. |
| Packaging | **Verified** | 164 modules; wheel `karox_runtime-5.0.0-py3-none-any.whl`; fresh venv install and CLI smoke. |
| Full test suite | **PASS** | 1,542 collected; single-process `pytest` exit 0. File-isolated gate: 1,525 passed / 15 skipped / 0 failed / 0 hangs. |
| Security | **Stronger, not an OS sandbox** | Capability + consequence policy, leases, hosted allowlist, credential redaction and negative tests. Direct MCP risk path fixed. This is not a claimed host sandbox. |

## Deliberate gaps in stable 5.0.0

These are v5 laboratory surfaces, not accidental omissions:

- Research subagents, orchestration/mission control and autonomy runtimes.
- Automatic local Git rollback checkpoints and offline/online checkpoint stores;
  the hosted Smart Stop checkpoint module remains present.
- Per-session skill selection and skill-level capability grants; the agent can
  still discover and load skills.
- `karox-agent-mcp` / the Ellipsis remote-control experiment. The
  `karox-vnext` alias is retained; no stub pretends the old feature exists.
- OpenAPI bridge and interactive hosted approval; MCP is the supported hosted
  path.
- Hosted `git.push`, publish, deploy and auth mutations.
- The legacy terminal shell/dashboard (~26k lines).

## Verification evidence

From `b/karox5-refactor` on Windows, Python 3.13.11:

```text
python -m pytest -q -p no:cacheprovider     # exit 0; 1,542 collected
python -m ruff check src tests scripts       # PASS
python -m mypy src/karox/cli.py src/karox/entrypoint.py src/karox/tui  # PASS
python scripts/check_tree.py                 # PASS, 164 modules / 87,667 lines
python -m build --wheel --no-isolation       # karox_runtime-5.0.0
```

A file-isolated gate also ran every executable test file in a fresh process and
fresh config/runtime: 70 files, 1,525 passed, 15 skipped, 0 failed, 0 hangs.

Fresh venv wheel smoke: `karox --version` → `karox 5.0.0`, `karox-vnext
--version` → `karox 5.0.0`, `paths --json` and `agent run --help` exit 0.

## NOT TESTED / remaining release blockers

- Published installers still target the old side-by-side 6.x dogfood flow; a
  pinned 5.0.0 installer and a real in-place upgrade/rollback rehearsal are
  **not done**.
- `git_baseline` and the newer session fields are not readable by rc4;
  rollback requires restoring the pre-upgrade backup. Rehearsal: rc4 creates a
  session, 5.0.0 reads it; 5.0.0 creates a session, rc4 rejects it with
  `unknown session fields: ['git_baseline']`; restoring the backup makes the
  old session readable by rc4 again. This is a restore path, not in-place
  downgrade.
- Real provider/browser/MCP/TUI user smoke beyond one provider task and the
  local protocol suites is incomplete.
- No representative 30–100-task benchmark matrix or OpenCode/DSH comparison.
- Linux real install and macOS are **NOT TESTED**.
- Checks/probes inline command-guard coverage is fixed for the documented
  textual patterns; obfuscated payloads remain outside the claimed model (no OS
  sandbox).

The 5.0.0 wheel is a local candidate for owner review; it is not published and
does not replace the installed `karox` command.
