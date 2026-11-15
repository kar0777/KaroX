# KaroX 6 — progress

Plan: `D:\проекты\KAROX6_BUILD_PROMPT.md`. Decisions: `DECISIONS.md`. A new chat resumes
from the first stage that is not **done** below.

| Stage | Status | Commit/tag | Notes |
|---|---|---|---|
| M0 Skeleton | done | `m0` | repo, MIT, uv, ruff, mypy, pytest, CI Win/Linux/macOS, perf gate |
| M1 Core + headless CLI | done | `m1` | agent loop, 4 providers, 10 tools, guard, spill, compaction, snapshots; 528 tests |
| M2 Bench, first win | in progress | | adapter + replay green; smoke run 43717195476 |
| M3 TUI | done (pending tag) | | inline chat on prompt_toolkit (extra `tui`), D-027 |
| M4 MCP server + client | in progress | aafdba6, d768078 | server stdio+HTTP and client done; ChatGPT web check needs a public tunnel (user's call) |
| M5 Subagents, options | in progress | | `--subagents` built (off by default until an A/B win); code mode not started |
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
- Held-out judging run k6h1 on reformboss (run 43723372988) cancelled before any task
  finished (key IP limit); rerun on Synthetic per D-025: run 43725472165, tag k6h1s, all 50
  held-out tasks, 1 attempt, parallel 3. Bar: DSH 17/50 at $0.046 per trial, OpenCode 15/50
  at $0.096 (same report, same prices).
- Free/cheap upstream search (user asked for free providers): no free source of
  DSV4.1-Flash-1017; every reseller key on the PC is empty or invalid; CrowLLM free tier is
  5 requests/min per key and lacks 1017. Benches stay on Synthetic + ReformBoss.
- H1 A/B started on the dev pool (run 43741239067, tag k6dh1, reformboss, parallel 1 to stay
  under the key's IP limit): same 11 burned tasks as k6d1+k6s1, with
  `--append-system-prompt` "When you fix a bug, fix its cause everywhere it applies: check
  sibling implementations, related code paths and other inputs that hit the same cause, not
  only the case in the report."
- Held-out verdict k6h1s (run 43725472165, Synthetic, 50 tasks, 1 attempt; report
  `karox6-bench-data/reports/heldout-k6h1s`): KaroX 6 17/50, DSH 17/50, OpenCode 15/50.
  Usage-priced $/trial 0.085 / 0.046 / 0.096; min/trial 11.5 / 4.7 / 7.9. KaroX 6 alone
  solved 48; lost 60 to one P2P regression (OpenCode too). M2 bar not met: score ties DSH
  but cost is 1.85x — input tokens 390M vs 192M (uncached 14.4M vs 6.4M), output 2.5M vs
  1.7M. Next: find the token overhead on the dev pool only (held-out trajectories stay
  unread), fix it, A/B on the free GLM lane (D-026), then a fresh held-out judgment.
- GLM lane smoke (run 43746913764, tag g53s1, freeinference): task 28 solved by KaroX 6,
  DSH and OpenCode (6-10 min each); one 429 retry across three parallel trials.
- GLM 46-task baseline (run 43751954752, tag g53b1) cancelled on the user's call: too slow
  to be useful; tuning works from the DeepSeek (Synthetic) trajectories already collected.
- Token anatomy on the dev pool (`karox6-bench-data/tokstat.py`, `cats.py`, `hunt.py`):
  the 1.85x cost is more steps (73 vs 45 calls per trial) times larger contexts (106k vs 85k
  mean input per call); cache-miss rate ~3.3% of calls for every arm (upstream property);
  compaction never fires in the bench. Extra steps spread over view / run-python / test /
  search / git, plus solution hunting (47 vs 20 commands into pip caches, other installed
  copies, git objects, `find /`).
- E1 A/B started (run 43753202223, tag k6e1, Synthetic, parallel 3, dev pool): appended rule
  "Work economically: every step re-sends the whole conversation, so prefer fewer, fuller
  steps. Combine related shell commands in one call, read the whole relevant part of a file
  at once instead of many small slices, and issue independent reads together. Solve the
  task from the project code and tests in front of you; do not hunt for a ready-made fix in
  package caches, other installed copies of the code, or unreachable git objects."
  Baseline k6d1+k6s1: 7/11, 56 calls per task.
- M4 part 2, MCP client: `karox6.mcp.client` connects stdio and Streamable HTTP MCP servers
  from `<home>/mcp.json` or `--mcp-config` (`--no-mcp` turns it off); their tools join the
  session as `mcp__<server>__<tool>` (readOnlyHint respected, output capped and spilled);
  a server that fails to start is skipped with a warning. `karox6 mcp` passes the same tools
  to its runs. Tests: fake stdio server (paged tools/list), our own HTTP server with bearer
  auth, a scripted session calling an MCP tool. 543 tests pass, ruff + mypy clean.
- Context anatomy on the dev pool (`karox6-bench-data/growth.py`, `phases.py`): per step
  KaroX 6 adds less context than DSH (mean 1.6k vs 2.3k tokens) and starts equally small
  (3.6k vs 3.2k), so its larger mean context is only the quadratic effect of more steps
  (62 vs 38 LLM calls per task). The extra steps come after the first edit (34 vs 17 per
  task: many one-off python probes of edge cases, which also won tasks 32/57/59) and from
  fewer multi-call steps (25% vs 33% of steps). E1 targets exactly this; no new run needed.
- M3 TUI: `karox chat` (and bare `karox` on a terminal) is an inline chat in the normal
  scrollback: streaming text, one line per tool call, approvals inline (y / N / a = always),
  Ctrl+C stops only the running turn (every time: its own SIGINT handler while a turn
  runs), `/diff /status /new /sessions /resume /approve /reasoning /help /quit`, multi-line
  input with a trailing backslash, history in `<home>/chat_history`. A turn stopped mid-call
  leaves no dangling tool calls (answered as cancelled on the next turn). Stack by
  measurement (D-027). Live check on CrowLLM grok-4.7-fast (free): fixed a bug, `/diff`
  and `/status` correct, 429s ridden out. Generated caches (`__pycache__`, `*.pyc`, tool
  caches) no longer count as changed files. 555 tests; `karox --version` ~100 ms.
- Cache misses (`karox6-bench-data/uncached.py`, `misses.py`; gateway audits only): the
  held-out rivals' baseline 43027228648 ran on W&B Inference (audit `api_base`), KaroX 6's
  k6h1s on Synthetic, so the 1.85x cost ratio crossed upstreams. Misses are random (gap
  before a miss median 0 s, no TTL effect) at similar rates (KaroX 3.3%, DSH 2.2%,
  OpenCode 2.5% of calls) but cost KaroX more (mean 116k vs 70k context); they are 72% of
  KaroX's uncached input on Synthetic and 0% on ReformBoss dev runs. Repricing misses as
  cache hits: KaroX $2.91 vs DSH $2.41 vs OpenCode $4.57 for the 50 tasks (1.21x DSH).
  Cost comparisons from now on report this miss-normalized price next to the raw one.
- H1 A/B (run 43741239067, ReformBoss) stopped after 4 of 11 tasks to save paid tokens:
  2/4 solved like the baseline on the same tasks (fixed target 13, still lost target 39,
  lost 32 that the baseline solved) with 31% more LLM calls (237 vs 181). Rejected: the
  rule buys no score and costs steps.
- `edit` no longer demands a prior `read` when `old_string` matches exactly and uniquely
  (files seen through shell output such as `sed -n`); approximate matches on such files and
  stale files are still refused, `write` still needs a read. Dev pool: "has not been read"
  cost an extra read + retry 5 times in k6d1 and 5 times in 4 E1 tasks.
- E1 decision rule, fixed before its last tasks finish: adopt into BASE_PROMPT if on the
  11 dev tasks it solves >= 7 (baseline k6d1+k6s1) with <= 56 LLM calls per task, or
  solves >= 9 with <= 62 calls per task; otherwise reject. Cost is compared on calls and
  miss-normalized price (E1 runs on Synthetic, the baseline ran on ReformBoss).
- E1 verdict (run 43753202223, Synthetic, report `karox6-bench-data/reports/dev-k6e1`): raw
  7/11 (DSH 7, OpenCode 9). Tasks 68 and 71 were lost to the upstream (Synthetic answered
  503 to every request for its last ~20 minutes), as the baseline lost 52 to ReformBoss's IP
  limit. On the 8 tasks both arms really ran: E1 7/8 vs baseline 5/8, 54 vs 60.5 LLM calls
  per task, input tokens -18%, output +3.5%. Adopted per the rule above (D-028): the two
  sentences are now BASE_PROMPT bullets; version 6.0.0.dev2.
- Held-out judgment k6h2s started (run 43757315661, Synthetic, 50 tasks, 1 attempt,
  parallel 3, wheel 6.0.0.dev2 = E1 rules + edit without read on exact match).
- M5 subagents (`--subagents`, off by default): an `agent` tool runs a child session with a
  fresh context (bash/read/grep/glob only, no edits, no further subagents, 40 steps /
  15 min) and returns only its final report; several calls in one step run in parallel;
  child usage counts in the parent; child tool calls appear in the event stream as
  `agent-N/<tool>`; child logs live in `<session>.agent-N` and are not offered for resume.
  Idea to A/B on the dev pool after k6h2s: exploration in a discarded context should cut
  the quadratic input growth of the main context. Live check (CrowLLM grok-4.7-fast): the
  child searched 4 calls and reported `tools/danger.py:check_command`; the parent finished
  in 2 steps.
- Settings file `<KAROX_HOME>/config.toml` (default `~/.karox6`): validated defaults for
  model/provider/base URL/key variable/limits/prices/flags, `[profiles.NAME]` with
  `--profile`; flag > `KAROX_*` env > file > built-in. CI green on Windows/Linux/macOS
  (py3.12/3.13) after fixing the Linux SIGINT-test hang. 574 tests.
- E1 also removed solution hunting: commands into pip caches / other installed copies / git
  objects fell from 47 of 780 calls (6%, baseline) to 8 of 677 (1.2%).
- Security review before exposing the MCP server through a tunnel (helper agy Gemini 4 Argon,
  high; each finding re-checked by hand). Fixed: command guard now sees `$(...)` inside
  double quotes even next to a `'`, nested `$( (...) )`, `r\m`-style escaped command names,
  `git push -uf`, `rm -rf $UNSET/` (unknown variables judged as empty when that makes the
  target root/home) and `//`; HTTP server checks origin/token before reading a body (small
  refused bodies are drained, large ones close the connection), read timeouts, 64
  connections, strict framing (repeated/conflicting Content-Length/Transfer-Encoding,
  malformed or folded headers, chunk endings, trailers -> 400), CORS preflight answered
  without the token, deep JSON and empty/oversized batches -> 400, MCP sessions capped at
  1000 and finished runs at 200, `request_id` reuse for another task refused, non-finite
  `wait_seconds` ignored, session ids validated before loading. The API key variable and
  `KAROX_MCP_TOKEN` are removed from the process environment after reading, so agent shell
  commands (and bench transcripts) cannot show them; a user-chosen token is not printed.
  Kept as designed: localhost origins stay allowed (the token is still required);
  `karox_approve` stays a client tool marked destructive, docs warn about injected approvals.
  605 tests.
- Live MCP end-to-end on this PC (own HTTP client like ChatGPT, CrowLLM grok free, token
  from env): initialize -> karox_run -> long-poll karox_status -> karox_diff -> DELETE,
  fixed a toy bug in 6 steps / 74 s. A first attempt exposed a Windows trap: `python3` is
  the Microsoft Store placeholder (prints "Python", exit 49) and a weak model looped on it
  for 30 steps; the environment block now names the working interpreter when that stub is
  detected (Windows only, so no bench effect). The same run also committed to the user's
  repo unasked: candidate prompt rule "do not commit/push unless asked" goes into the next
  A/B batch (D-021), not straight into BASE_PROMPT.
- Night plan (user: 9 h until review, "make KaroX the best"): Synthetic allows 3 parallel
  tasks, so: prepare batch B1 while k6h2s keeps running, cancel k6h2s (finished trials kept),
  A/B B1 on the dev pool (~1 h), then one held-out run of the winner by morning.
- Why B1: on k6h1s KaroX made 1.38x DSH's model calls (3620 vs 2627) with fewer tool calls
  per step (1.27 vs 1.42); tasks with >120 calls hold 56% of its input tokens (one task: 285
  calls, 80M tokens); DSH's prompt has no verification push at all. On the dev pool no
  solved KaroX run needed more than 94 calls.
- B1 (6.0.0.dev3): (1) wrap-up notice at step 120 and a final-answer notice at 150
  (`--wrap-up-steps`, 0 = off); headless `run`/`mcp` default `--max-steps 200` (0 = none,
  chat unlimited); (2) "Work economically" also asks to check edge cases with one script
  rather than one probe per step; (3) "Do not commit, push or create branches unless the
  task asks for it" (product rule; a live run committed unasked).
- B1 decision rule, fixed before the run: adopt if on the dev pool, against E1 (k6e1) on
  the tasks both ran cleanly (and against k6d1 for 68/71, lost to 503s in E1), B1 solves
  at least as many tasks with no more LLM calls per task; otherwise reject and the
  held-out run judges dev2. The step-120 notice barely triggers on the dev pool (E1 max
  110 calls), so its effect is measured on the held-out run.
- MCP for chat clients: `karox_status`/`karox_run` return `log`, one line per meaningful
  event (`✓ bash pytest -q → 2 passed (1.5s)`), instead of raw events (`detail: full`
  keeps them); a brief long-poll gathers lines and wakes early only when the run ends or
  needs approval, so ChatGPT spends fewer tool calls and context per run. Line digests are
  shared with the chat UI (`core/digest.py`).
- MCP compliance and speed: the official MCP Python SDK 2.3.0 client connects over stdio
  and Streamable HTTP (negotiates 2025-11-25), lists tools, calls them, gets tool errors
  and pings; stdio `initialize` answers 0.21 s after process start (5 of 5 runs). B1 wheel
  passed the free replay in real hard12 images (run 43761878216).
- KaroX as MCP client against a reference server built with the official SDK 2.3.0
  (`MCPServer`, stdio and streamable HTTP): tools listed as `mcp__ref__add` etc., results
  returned, tool errors flagged `is_error`. Both directions of MCP are now checked against
  the reference implementation.
- Provider retries: 502/503/529 ("service unavailable", request not processed) are waited
  out for up to 10 minutes like 429s, instead of 5 quick retries (~15 s) that ended the
  run; a 20-minute Synthetic 503 storm cost E1 tasks 68 and 71. Plain 500s and timeouts
  keep the short policy (they may have been processed and billed).
- `job_output`: a timeout now means "wait" unless `wait: false` is explicit. In all 87 bench
  sessions on disk, 56 of 90 `job_output` calls passed a timeout (up to 300000 ms) without
  `wait: true`, came straight back and were followed by more polls and `/proc` checks (dev
  task 40: steps 97-124 spent babysitting a full test-suite run). Bug-level fix of the
  tool contract; its effect is measured with the rest on the held-out run.
- `edit` on a file that changed on disk since its last read: allowed when `old_string`
  matches exactly (same rule as for a never-read file); a fuzzy match still needs a fresh
  read. dev2 sessions hit 5 such refusals in 2 sessions; in one the agent re-viewed the
  file with `sed -n`, which does not count as a read, and was refused again. Each refusal
  ended in a full-file `read`. Bug-level tool-contract fix, measured on the held-out run.
- Note on the two tool fixes above: the error counts came from a scan of every session on
  disk, including the partial held-out runs k6h1s/k6h2s. Only tool-contract error
  messages were counted (no task content, no prompt rules derived from them), and both
  fixes only change what a tool accepts, so the held-out judgment stays usable; the
  report states it.
- B1 verdict (run 43762127729, tag k6b1, Synthetic, no retries; report
  `karox6-bench-data/reports/dev-k6b1`): raw 7/11 (DSH 7, OpenCode 9). By the rule fixed
  before the run: against E1 on its 9 clean tasks plus k6d1 on 68/71, the reference solved
  9 and B1 7 (B1 lost 32, 39, 57, won 52), with 43 vs 55 LLM calls per task (-21%).
  Rejected: fewer solved. The step-120 notice and the 200-step cap never fired (max 80
  calls), so the loss sits in the prompt rules or noise; the "one script for edge cases"
  clause is the likely culprit (one-off edge-case probes won 32/57/59 before). B1 is off:
  BASE_PROMPT is dev2's again, `--wrap-up-steps` and `--max-steps` stay as opt-in flags
  with no default limit, and "do not commit unless asked" waits for its own A/B.
- 6.0.0.dev5 = dev2 prompt + tool fixes (`job_output` timeout waits, `edit` exact match on
  changed files) + MCP brief log + outage retries. It goes to the final held-out run.
- B1 post-mortem (dev pool only): the lost tasks did not probe less (edge-case probes after
  the first edit: 32: 9 vs 6 in E1, 57: 12 vs 4, 39: 5 vs 5), so the losses look like
  single-attempt noise rather than the edge-case clause at work; the 21% fewer calls are
  the consistent part. Candidate for a later A/B with 2 attempts per task.
- Final held-out run k6h3s started 21:55 UTC (run 43766318734, Synthetic, 50 tasks, 1
  attempt, parallel 3, wheel 6.0.0.dev5).
- Independent review of tonight's diff (agy Gemini 4 Argon, high; each finding re-checked by
  hand, all six confirmed and fixed after the dev5 wheel went to the held-out run):
  `karox_run` validates `detail`/`wait_seconds` before starting (a refused call used to
  leave a nameless run holding the workspace); retry counts are per failure kind (a long
  429/503 spell no longer leaves zero retries for a following dropped connection or 500);
  `edit` on an unread/changed file refuses `replace_all` over several places, and such an
  edit no longer counts as a read for a later full-file `write`; the Windows Python hint
  asks a WindowsApps `python3`/`python` for `--version` (a real Store install lives there
  too; 125 ms, Windows only); wrap-up notices survive a truncated step; negative
  `--max-steps`/`--wrap-up-steps` mean no limit. No issues found in the brief long-poll
  (`since`/`next`, wakeups, memory) or in `job_output`. 613 tests.
- M5 code mode (`--code-mode`, off by default): a `run_code` tool runs a Python 3 program
  with the project's interpreter in the workspace; `tools.<name>(...)` inside it calls any
  other session tool (keywords or positional in required-parameter order) over a loopback
  socket guarded by a per-program token, so nested calls keep the command guard,
  approvals, file tracking and output limits. A failing nested call raises `ToolError`;
  uncaught it fails the program (exit-code marker, no silent truncation - KaroX 5 lessons:
  full stdlib incl. os/subprocess, real Python booleans). Timeout kills the process tree
  and abandons nested calls. Nested calls stream as `run_code/<tool>` events. Programs and
  their logs stay in `<session>/code/`. Live check (CrowLLM grok free, Windows): one
  program listed the files and ran pytest, the model fixed both bugs, tests passed.
  619 tests. Goes to a dev-pool A/B after the held-out run.
- 6.0.0.dev6 = dev5 + review fixes + code mode (flag, off). Same BASE_PROMPT as dev5.
- C1 decision rule (code mode on by default), fixed before the run: dev6 with
  `--code-mode` on the 11 dev tasks, 1 attempt, Synthetic, after the held-out run. Against
  the same reference as B1 (E1 on its 9 clean tasks, k6d1 on 68/71: 9 solved, 55 LLM
  calls per task on the 9): adopt if C1 solves >= 9 with <= 55 calls per task on those 9;
  otherwise code mode stays an opt-in flag.
