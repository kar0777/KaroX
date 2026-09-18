# 🚀 KaroX 5.0.0rc2 — Public Beta / Публичная бета

<div align="center">

**One local runtime for every AI coding agent. Your models can change. KaroX remembers.**

![Channel](https://img.shields.io/badge/channel-public_beta-f59e0b)
![Runtime](https://img.shields.io/badge/runtime-5.0.0rc2-2563eb)
![Platforms](https://img.shields.io/badge/platform-Windows%20%7C%20macOS%20%7C%20Linux-64748b)
![Supporters](https://img.shields.io/badge/supporters-33-ec4899)

**KaroX 5 is now the primary `main` line.** Stable 4.1.4 remains available as
the previous stable fallback while this release candidate completes live beta evidence.

</div>

`5.0.0rc2` is the second public **KaroX 5 beta release candidate** and the first intended to complete the publication pipeline end to end: the local
runtime, packaging, deterministic release gates, Windows/macOS/Linux CI path,
guarded Git workflow, durable sessions, evidence model, and multi-client
architecture are ready for real beta use. Stable `5.0.0` still waits for the
remaining live conformance, external-beta, and install/upgrade evidence recorded
in `docs/RELEASE_CHECKLIST.md`.

`5.0.0rc2` — второй публичный **release candidate KaroX 5**. Основная ветка
`main` теперь содержит KaroX 5 beta. Стабильный `5.0.0` появится после
оставшихся live-conformance, внешней беты и подтверждённых install/upgrade
прогонов из `docs/RELEASE_CHECKLIST.md`.

## Why rc2 / Почему rc2

The `v5.0.0rc1` tag reached GitHub but its prerelease workflow stopped inside the
quality gate **before PyPI or a GitHub Release was published**. That run exposed
release-only CI assumptions that the normal shard pipeline did not exercise:
source-vs-installed import precedence, coverage-job installation, Windows console
encoding, a stale installer contract assertion, and a fake-PID process-group test.
`rc2` contains those release-gate fixes and is the first candidate intended to
complete the public publication pipeline end to end.

Тег `v5.0.0rc1` дошёл до GitHub, но prerelease workflow остановился на quality
gate **до публикации в PyPI и до создания GitHub Release**. `rc2` исправляет
найденные именно release-pipeline проблемы и является первым кандидатом, который
должен пройти публичную публикацию полностью от тега до устанавливаемого пакета.

## Verification pipeline improvements / Проверки и совместимость

- Python 3.10 test fixtures use portable context cleanup, with regression tests
  for successful entry, failed entry, cleanup ordering, and cleanup failures.
- Isolated Windows test processes relay both output streams and preserve real
  failure exit codes, including when the parent console uses a legacy encoding.
- CI distributes complete test files across workers. The benchmark aggregation
  assertion runs in parallel builds instead of being skipped.
- Coverage runs the full pytest suite against checkout source, including CLI
  subprocesses. The required branch-coverage threshold remains **70%**.
- Browser probes always release the Playwright driver on failure; support-bundle
  fixtures restore HOME, runtime directories, import paths, and their module state.
  Python 3.12 matrix jobs require real Chromium acceptance on all three OSes.
- Pip dependency caches are enabled for the ordinary CI jobs. The platform and
  Python-version matrices and release/security gates remain in place.

These changes do not replace the pending live-provider and external-beta
conformance records needed for stable 5.0.0.

## ✨ What makes this release different

- **One runtime, many agents:** ChatGPT Web, Claude Web, API models, MCP clients,
  Codex/Claude Code orchestration and local workers use one repository-scoped
  permission and evidence boundary.
- **Durable autonomy:** sessions, retries, mutation leases, checkpoints and
  idempotency survive reconnects without replaying completed work.
- **Evidence before claims:** checks, Git status/diff and typed evidence packets
  back completion reports instead of trusting narration.
- **Meaningful approvals only:** routine development remains low-friction;
  remote push and release publication keep exact one-shot user confirmation.
- **A real public beta:** installable wheel, cross-platform CI, release notes,
  migration path, security model, benchmarks and public supporter acknowledgements.

## 🤝 Supported by

KaroX 5 ships with public thanks to **33 unique supporters**. This release expands
the acknowledgements with StepFun and the additional confirmed support restored
from the project outreach/Gmail record, while collapsing duplicate records such
as Sentry/Sentry for Good and Verda/DataCrunch.

The full list now also includes BlockRun, UnifyLLM, Novita AI, BazaarLink,
CostRouter, Ellipsis, Advanced Installer, Bump.sh, Sentry, Socket, RouterPlex,
LangWatch, and LLMTR / Knowhy alongside the existing KaroX supporters.

See [SUPPORTERS.md](SUPPORTERS.md) for the full logo wall, links, support
categories and acknowledgements.

## Install / Установка

```bash
pipx install "karox-runtime==5.0.0rc2"
# or / или
uv tool install --prerelease=allow karox-runtime
karox
```

To test the exact source tree directly, install the immutable tag / Для проверки именно исходников установи фиксированный тег:

```bash
pipx install --force "git+https://github.com/kar0777/KaroX.git@v5.0.0rc2"
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

| Surface | Status in rc2 |
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

The beta gate covers the full pytest suite, Ruff, Mypy, artifact installation,
and the unchanged **70% statement-and-branch coverage floor**. CI retains Ubuntu,
Windows, and macOS with Python 3.10 / 3.12 / 3.13 / 3.14 in the artifact matrix.
Real Chromium acceptance is required in the three Python 3.12 matrix jobs.

The deterministic unittest count is tracked separately by
`scripts/check_test_count.py`; it excludes pytest-only and parameterized cases.
A local pass is not a substitute for green GitHub Actions on the exact tagged
commit, successful publication, and a fresh install of the published package.

```bash
python -m pytest tests -n 6 --dist=loadfile
python scripts/check_v5_release.py --json
```

`check_v5_release.py --strict` intentionally fails on this candidate until the
live conformance records and the external beta are `passed`.
