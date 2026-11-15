# KaroX 6 — progress

Plan: `D:\проекты\KAROX6_BUILD_PROMPT.md`. Decisions: `DECISIONS.md`. A new chat resumes
from the first stage that is not **done** below.

| Stage | Status | Commit/tag | Notes |
|---|---|---|---|
| M0 Skeleton | done | `m0` | repo, MIT, uv, ruff, mypy, pytest, CI Win/Linux/macOS, perf gate |
| M1 Core + headless CLI | not started | | |
| M2 Bench, first win | not started | | |
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
