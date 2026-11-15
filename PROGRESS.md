# KaroX 6 — progress

Plan: `D:\проекты\KAROX6_BUILD_PROMPT.md`. Decisions: `DECISIONS.md`. A new chat resumes
from the first stage that is not **done** below.

| Stage | Status | Commit/tag | Notes |
|---|---|---|---|
| M0 Skeleton | done | `m0` | repo, MIT, uv, ruff, mypy, pytest, CI Win/Linux/macOS, perf gate |
| M1 Core + headless CLI | done | `m1` | agent loop, 4 providers, 10 tools, guard, spill, compaction, snapshots; 528 tests |
| M2 Bench, first win | in progress | | adapter + replay green; smoke run 43717195476 |
| M3 TUI | not started | | |
| M4 MCP server + client | not started | | |
| M5 Subagents, options | not started | | |
| M6 Packaging, release | not started | | publishing needs the user's explicit permission |

## How to work in this repo

```bash
uv sync                      # dev environment (.venv)
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
uv run python scripts/perf_check.py   # start <= 300 ms, idle RSS <= 80 MiB
git diff --check
```

## Log

### M0 — 2026-11-15
- Repository `D:\проекты\KaroX-6`, branch `karox6/main`, MIT license, hatchling + uv.
- Package `karox6` (CLI `karox`, `karox6`), `karox --version`, `karox debug rss`.
- CI `.github/workflows/ci.yml`: Ubuntu/Windows/macOS x Python 3.12/3.13 — ruff, format,
  mypy (strict), pytest, perf gate; wheel smoke install in a clean venv on all three OSes.
- Studied DSH sources (helper agy/Gemini) and command-guard candidates; see D-011, D-012.

### M1 — 2026-11-15
- Core (`core/session.py`): asyncio loop as an event stream (JSON lines with `--json`);
  model decides when it is done (a turn without tool calls ends the run); limits on steps,
  seconds, cost; loop guard 3/5/8 + hard stop 20; output-truncation notice; parallel
  read-only calls; background-job finish notes; session store with resume.
- Providers: OpenAI Chat Completions (SSE, reasoning_content replay per turn), Anthropic
  Messages (signed thinking round-trip, prompt caching), Gemini native (thought_signature
  round-trip), OpenAI Responses (encrypted reasoning items); retry with Retry-After.
- Tools (DSH-compatible names): bash/pwsh (Git Bash on Windows, tree kill, timeout ->
  background job), read, write, edit (fallback cascade), glob, grep (ripgrep or Python),
  todo_write, job_output/job_list/job_kill, optional apply_patch (Codex format).
- Destructive-command guard (claude-code-safety-net port, D-012); spill of big outputs to a
  readable file; compaction (prune old tool output, then summarize with task/decisions/
  open hypotheses/changed files); changed files = git status diff + tool writes (one
  source); finish snapshots `submissions/NN` (D-013).
- Tests: 528 (pytest) on Windows/Linux/macOS x 3.12/3.13; scenario tests use a scripted
  provider and `httpx.MockTransport` (no network). Perf: start 115 ms, idle RSS 29 MiB.
- Helpers (ledger): codex gpt-6.1-terra x5 (patch, guard, providers, session tests, tool
  tests; ~$0.75 API-equivalent in total); opencode free DeepSeek stopped (opened the wrong
  directory via $PWD; delegate.py fixed).

### M2 — started 2026-11-15
- Bench repo branch `karox6-adapter`: `harbor_agents/karox6_agent.py`, alias
  `hard12-karox6` in every gateway config/profile, arm `karox6` in plan/render/report,
  `karox6_flags` workflow input for A/B variants, snapverify reads KaroX 6 snapshots.
- Free replay (replay.yml, agent=karox6, hard12 images py3.13/3.10/3.9): run 43716952886
  green, 16/16 scripted steps.
- Paid smoke on dev tasks 13, 28, 39 (run 43717195476, tag k6s1, reformboss): 28 solved,
  39 failed one F2P (camelCase case the issue left open; DSH solved it), 13 not run (runner
  Docker daemon down). Usage-priced $0.019 per trial vs DSH $0.024 / OpenCode $0.023.
- Scoring: `D:\проекты\karox6-bench-data\compare.py` (union with baseline run, D-024).
- Dev run k6d1 (run 43718359189, 9 burned tasks) + k6s1: KaroX 6 7/11, DSH 7/11, OpenCode
  9/11 on the dev pool; usage-priced ~$0.042 / $0.040 / $0.047 per trial. KaroX 6 solved
  32, 57, 59 where DSH failed; lost 13 and 39 by fixing only the reported instance of a
  defect (hypothesis H1, to be A/B-tested), 40 on an unstated API expectation, 52 to the
  upstream key limit (429: more than 3 client IPs in 10 minutes; parallel GitHub runners).
- 6.0.0.dev1: rate limits are waited out on a 15-minute budget (commit 6fccc09).
- Held-out judging run k6h1 started (run 43723372988): all 50 held-out tasks, 1 attempt.
  Bar: DSH 17/50 at $0.046 per trial, OpenCode 15/50 at $0.096 (same report, same prices).
