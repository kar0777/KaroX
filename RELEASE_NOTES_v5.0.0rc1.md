# KaroX 5.0.0rc1 — public beta / публичная бета

**One local runtime for every AI coding agent. Your models can change. KaroX remembers.**

`5.0.0rc1` is the public **KaroX 5 beta channel**. It is intentionally a
pre-release: the local runtime and deterministic release gates are ready for
real use, while stable `5.0.0` still waits for the remaining live conformance,
external-beta, and install/upgrade evidence recorded in
`docs/RELEASE_CHECKLIST.md`.

`5.0.0rc1` — публичный **beta-канал KaroX 5**. Это намеренно pre-release:
локальный runtime и детерминированные release-gates уже предназначены для
реального тестирования, а стабильный `5.0.0` появится только после оставшихся
live-conformance, внешней беты и подтверждённых install/upgrade прогонов из
`docs/RELEASE_CHECKLIST.md`.

## Install / Установка

```bash
pipx install --pre karox-runtime
# or / или
uv tool install --prerelease=allow karox-runtime
karox
```

To test the exact source tree directly, install the immutable tag / Для проверки именно исходников установи фиксированный тег:

```bash
pipx install --force "git+https://github.com/kar0777/KaroX.git@v5.0.0rc1"
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
- Observe / Build / Advanced capabilities plus an explicit **Bypass** autonomy
  switch. Advanced capability alone does not authorize destructive deletion.
- Normal reads, edits, tests, checks, dev commands and local commits do not stop
  for nuisance confirmations. Protected mode gates destructive source deletion;
  Bypass may automate deletion **inside the selected repository only**.
- Unknown or destructive local gates are deferred so the agent can keep doing
  independent work and ask the user only when the pending action becomes necessary.
- Real external commit points such as `git push`, publish and deploy keep an
  exact-action one-shot human boundary; force-push never inherits normal push authority.
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

The beta gate covers the canonical test suite, Ruff, Mypy and the configured
70% branch-coverage floor. On the 2026-09-17 release-preparation tree, the
published suite count is 3362 tests (`3367` from repository-root collection with
five legacy script checks), the final six-worker xdist run completed successfully,
and the focused guarded-release/risk tests passed. Earlier in the same release
mission, canonical `unittest` completed `OK (skipped=6)` and KB-HYBRID-01..10
passed 10/10. The release wheel is built, its contents are checked, and it is
installed and smoke-tested outside the source tree. CI exercises Ubuntu, Windows
and macOS; the broader Python 3.10/3.12/3.14 installation matrix remains an
explicit prerequisite for stable `5.0.0` until the exact release tree is green
in GitHub Actions.

```bash
python -m unittest discover -s tests -p "test_*.py"
python scripts/check_v5_release.py --json
```

`check_v5_release.py --strict` intentionally fails on this candidate until the
live conformance records and the external beta are `passed`.
