# KaroX 6 — decisions

Each entry: what was decided and why. User decisions (from `KAROX6_BUILD_PROMPT.md`)
are marked **[user]** and are not revisited without asking the user.

## Fixed by the user

- **D-001 [user]** Rebuild from scratch in `D:\проекты\KaroX-6`; KaroX 5 is a source of
  lessons only, none of its code is carried over.
- **D-002 [user]** Python 3.12+, `uv`, minimal dependencies, lazy imports.
- **D-003 [user]** Windows, Linux and macOS from the first release (CI matrix on all three).
- **D-004 [user]** One core, many clients: headless CLI (JSON stream), light TUI, MCP server
  (stdio + Streamable HTTP), MCP client. Providers: OpenAI-compatible Chat Completions and
  Responses, Anthropic Messages, Gemini (with `thought_signature` round-trip).
- **D-005 [user]** No ChatGPT-subscription login, no ACP or other editor protocols.
- **D-006 [user]** Minimal safety at start: only explicitly destructive commands need
  confirmation, using a ready-made, permissively licensed rule set. No word bans
  (e.g. "publish").
- **D-007 [user]** Bench = Harbor + `kar0777/karox-hard12-bench`; new adapter
  `harbor_agents/karox6_agent.py`, the KaroX 5 adapter stays untouched. Model
  `deepseek-v4.1-flash-1017` via ReformBoss, one paid run at a time, parallel <= 3.
  Push only to a new branch (`karox6/main`) of `kar0777/KaroX`; never `main`; publishing
  only with explicit permission.

## Engineering decisions

- **D-008** Resource targets enforced in CI by `scripts/perf_check.py`: `karox --version`
  <= 300 ms (median of 7), idle RSS of the headless stack <= 80 MiB.
- **D-009** Distribution and import package are `karox6` (so it can sit next to an installed
  KaroX 5, whose import package is `karox`); console scripts are `karox` and `karox6`.
  Build backend hatchling (pure-Python wheel, no compilers).
- **D-010** Only runtime dependency in core: `httpx`. Tests use plain `pytest` and
  `asyncio.run` (no pytest-asyncio).
- **D-011** Design reference is DeepSeek Harness (MIT) because DeepSeek-V4.1-Flash scores
  well with it and was very likely trained on harnesses of this shape. Tool names and
  argument names follow the de-facto standard shared by DSH, OpenCode and Claude Code
  (`bash`, `read`, `write`, `edit`, `glob`, `grep`, `todo_write`, `job_*`), so models
  meet in-distribution tools. Source study: DSH 0.2.2-rc.1 local package (2026-11-15).
- **D-012** Dangerous-command guard: port of the rules of
  `kenryu42/claude-code-safety-net` (MIT, 2k stars, also shipped as a DSH/OpenCode
  plugin), restricted to the catastrophic subset the user listed (rm -rf of root/home/
  workspace/.git, PowerShell/cmd equivalents, find -delete on .git, dd/mkfs/disk erase/
  writes to block devices, git push --force). One own rule is added because the catalog
  lacks it: SQL `DROP DATABASE`/`DROP SCHEMA` through database CLIs. Codex CLI's rule set
  (Apache-2.0) was considered and rejected: on POSIX it flags every forced `rm`
  (`rm -rf build`), which would block normal autonomous work. Not blocked at start (a user
  decision if wanted later): `git reset --hard`, `git clean -fd`, `git checkout -- path`.
- **D-013** Every run end copies the changed files to `<session>/submissions/NN/` (files +
  `manifest.json` with sha256, `null` = deleted; 5 MiB per file, 50 MiB per snapshot). It is
  for grading a finish later (bench `snapverify`) and never feeds back into the run;
  `--no-snapshot` turns it off. KaroX's own home is never reported as a changed file.

## Evaluation hygiene

- **D-020** Judge only on held-out tasks. Night tasks are `data/night_tasks.txt` lines 13-73
  of the bench repo. Burned (analysed before KaroX 6, never used for scoring):
  **13, 28, 32, 39, 40, 46, 52, 57, 59, 68, 71**. Every task whose trajectory is analysed
  to change KaroX 6 gets added here and leaves the scoring pool.
- **D-021** No rule may mention a specific task, repository or library. Every prompt/tool
  change goes through an A/B on the same tasks; a rule without measurable gain is removed.
- **D-022** Rival baseline (already paid): run `43027228648`, night tasks 13-73,
  OpenCode 24/61, DSH 24/61, KaroX 5 17/61 (Synthetic upstream, same model).
- **D-023** Development pool (trajectory analysis, A/B of changes): night tasks 1-12 plus the
  burned tasks of D-020. Held-out runs (13-73 minus burned) are for judging only; reading a
  held-out failure trajectory to change KaroX burns that task (add it to D-020).
- **D-024** KaroX 6 bench runs run only the `karox6` arm. The score is computed by one local
  report over the union of the new KaroX 6 trials and the baseline trials of D-022, because
  the report's environment-failure rule (a P2P test failing in every trial of a task) must
  see all arms.
- **D-025** [user, 2026-11-15] Bench runs use the Synthetic upstream (`upstream=synthetic`,
  same model DeepSeek-V4.1-Flash-1017 and the same enforced sampling profile; also the
  upstream of the D-022 baseline). ReformBoss was too slow: it refuses a key seen from more
  than 3 client IPs in 10 minutes, and parallel GitHub runners keep tripping it. The first
  held-out run on ReformBoss (43723372988) was cancelled before any task finished.
- **D-026** [user, 2026-11-15] Free fast dev lane: GLM-5.3-Flash on FreeInference (Harvard
  SEAS; free, $80/day quota, 2 concurrent requests per key, prompts logged for research),
  bench `upstream=freeinference`. A different model from D-022: A/B tests and development
  runs may use it, with every rival re-run on GLM for comparison (never scored against the
  DSV4.1-Flash-1017 baseline). The M2 verdict stays on DeepSeek-V4.1-Flash-1017.
- **D-027** [agent, 2026-11-15] TUI = inline chat on prompt_toolkit alone (extra `tui`,
  imported only by `karox chat`), own ANSI rendering; plain `input()` when the extra is
  missing or stdin is piped. Measured on top of the headless stack (warm import / added
  RSS): prompt_toolkit 170 ms / +6 MB; prompt_toolkit + rich 320 ms / +12 MB; Textual
  350 ms / +13 MB. Textual's full-screen mode also takes the terminal scrollback away; rich
  adds nothing the chat needs.
