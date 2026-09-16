# KaroX 5.0.0rc1 — release candidate / кандидат в релиз

**One local runtime for every AI coding agent. Your models can change. KaroX remembers.**

This is the first public release candidate of KaroX 5. It is a **pre-release**:
the runtime, its guarantees and its documentation are complete, and the
remaining distance to stable `5.0.0` is recorded evidence — live conformance
runs against ChatGPT Web, Claude Web and the four provider families, an
external beta, and install/upgrade rehearsals on all three OS families. See
`docs/RELEASE_CHECKLIST.md`.

Это первый публичный кандидат в релиз KaroX 5. Это **pre-release**: runtime,
его гарантии и документация завершены; до стабильного `5.0.0` остаются
записанные доказательства — live-прогоны ChatGPT Web, Claude Web и четырёх
семейств провайдеров, внешняя бета и репетиции установки/обновления на трёх
ОС.

## Install / Установка

```bash
pipx install --pre karox-runtime
# or / или
uv tool install --prerelease=allow karox-runtime
karox
```

Stable 4.1.4 users keep their installation: the bootstrap scripts on `main`
still install 4.x. KaroX 5 uses a separate configuration layout and imports 4.x
metadata only through the dry-run-by-default `karox migrate`.

## What KaroX 5 is / Что такое KaroX 5

A local control plane that lets ChatGPT, Claude, Codex, Gemini, any MCP client
and any API model work in an explicitly selected Git repository through **one**
repository-scoped runtime. KaroX owns permissions, sessions, mutation safety,
verification, Git evidence, memory and secret handling; the model is a
replaceable worker.

## Highlights vs 4.1.4

### Every agent, one runtime
- ChatGPT Web, Claude Web, HyperAgent and Adapt connect to the same local MCP
  bridge with OAuth/DCR/PKCE; one durable saved profile keeps its Tailscale
  `.ts.net` URL and credential across restarts.
- OpenAI Responses, Anthropic Messages, Gemini and OpenAI-compatible endpoints
  run through the native agent with OS-keyring credentials and a live probe
  before activation.
- One bridge serves several approved repositories in parallel with per-project
  leases and workstreams — no global lock, no shared cwd.

### Memory that belongs to you
- `karox.memory.remember/recall/context/list/forget` for every client. A fact
  stored through ChatGPT is recalled through Claude, Adapt or a native provider
  without shared transcripts. Russian and English queries hit the same entries.
- Deterministic project fact map: languages, entrypoints, test/build commands,
  package manager; ingests AGENTS.md / CLAUDE.md / KAROX.md / .cursorrules.

### Evidence, not narration
- A change counts only after write → approved check → Git status → Git diff.
  A failed check cannot be turned into "done" by model text.
- Typed Evidence Packets, artifact-first result envelopes, adaptive/concise
  output that never hides failures.

### Permissions as architecture
- Observe / Build / Advanced access profiles; no profile grants Git push,
  package publishing or authentication commands.
- Universal Smart Stop (RiskEngine): four risk levels, single-use confirmation
  tokens, and no way to configure auto-approval above medium.
- Cross-process mutation leases with heartbeat, idempotency for every mutating
  operation, transactional workspace changes with checkpoints and undo.

### Your paid subscriptions work for you
- Opt-in orchestration: an Intelligence Pool over API models, local models and
  already-paid Codex / Claude Code subscriptions; role-based recipes; parallel
  read/review waves; worktree-isolated implementers that are never auto-merged;
  independent review; crash-safe recovery.
- Economy with a parity gate: stable prompt prefixes, tool-schema dedup, read
  cache, shared content-addressed context, and a Savings Receipt that reports a
  dollar figure **only** when a measured baseline exists.

### Browser and desktop, bounded
- Localhost browser observation and input; opt-in external HTTPS browsing under
  `browser_control` with domain allowlists, private-IP blocking and headed
  takeover for login/CAPTCHA/2FA through a dedicated Chrome profile.
- Windows desktop application capture without global input primitives.

### Terminal client
- 35 screens, Russian and English on equal footing, native text selection,
  size-tested from 40x12 to 160x45, typed session views, session browser,
  Connections hub, Ctrl+W workspace manager, Mission Control.

## Status of surfaces

| Surface | Status in rc1 |
| --- | --- |
| Core runtime, native agent, provider adapters | Contract tested; live provider records pending |
| ChatGPT Web / Claude Web bridges | Experimental; live conformance pending |
| Universal memory, project map | Implemented; cross-client recall proven live |
| Orchestration and economy | Opt-in; guarded Codex/Claude Code adapters |
| Skills, Packs, PromptQL, Notion gateway | Preview / Legacy |
| Ellipsis Opus 5 local workspace | Experimental; paid live run gated |

## Breaking changes from 4.x

- New package name and entry point: `karox-runtime` wheel, `karox` console
  script. `karox-vnext` remains a compatibility alias.
- New configuration and session layout; 4.x state is imported only through
  `karox migrate --apply` after a dry run.
- Access profiles replace the 4.x permission model. `workspace_write` (Build)
  does not grant `git.commit`; only `elevated` (Advanced) does.
- Browser tools under Build are localhost-only; public HTTPS requires an
  explicit `browser_control` session.
- Provider secrets live in the OS keyring only; plaintext keys in configuration
  are rejected.

## Known limitations

- KaroX is not an operating-system sandbox; an approved process runs with the
  OS rights of the KaroX user.
- Cloudflare Quick Tunnel URLs change on restart; use a saved Tailscale profile
  for a stable endpoint.
- One open P1 terminal item: handing the mouse back to the terminal (UX-006).
- No public task-success benchmark figure is published with this candidate;
  the benchmark harness ships, the numbers come with a reproducible manifest.

## Verification

3325 tests under `tests/`; Ruff, Mypy and a 70% branch-coverage gate in CI;
wheel built, installed and smoke-tested outside the source tree on Windows,
macOS and Linux for Python 3.10–3.14.

```bash
python -m unittest discover -s tests -p "test_*.py"
python scripts/check_v5_release.py --json
```

`check_v5_release.py --strict` intentionally fails on this candidate until the
live conformance records and the external beta are `passed`.
