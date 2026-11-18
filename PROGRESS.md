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
- Code mode replay in real hard12 images (run 43769569759, scenario `karox6_code`, no
  model): a program called bash, write and read and caught a ToolError; a refused nested
  `rm -rf /` failed the program (exit 1, nothing after it ran); an empty program reported
  so. Passed on all three images. C1 is queued to start when k6h3s finishes.
- Correction: the miss-normalized prices above ("KaroX $2.91 vs DSH $2.41 vs OpenCode
  $4.57, 1.21x") compared KaroX's 50 held-out tasks with the rivals' whole baseline run
  (all 61 tasks). On the same 50 held-out tasks (`uncached.py --heldout`, flag added) the
  k6h1s numbers are KaroX $2.91, DSH $2.04, OpenCode $4.10: KaroX 1.43x DSH, not 1.21x.
- k6h3s partial (49 of 50 trials, task 70 still running at 02:35 UTC): KaroX 15/49, DSH
  17/50, OpenCode 15/50; raw usage $3.11 vs $2.30 vs $4.80; 10.9 vs 4.7 vs 7.9 min per
  trial. Against k6h1s the only change in solved tasks is 35 (3 of 4 F2P tests, model
  variance). Agent time split on the 48 finished runs: 472 min total, 189 min inside tools
  (mostly broad test runs and long background checks), 284 min in model calls.
- V1 decision rule (proportional verification), fixed before the run: dev6 with
  `--append-system-prompt "Keep verification proportional to the change: run the tests
  that cover the code you changed and a short reproduction, and a wider test run at most
  once. Do not write extra test tooling or wait on long background runs. Once the relevant
  tests pass, stop and report."` on the 11 dev tasks, 1 attempt, Synthetic, after C1.
  Same reference as B1/C1 (9 solved; 55 LLM calls per task on E1's 9 clean tasks). V1
  becomes the candidate for the next held-out run if it solves >= 9 with <= 47 calls per
  task (-15%) on those 9; otherwise it is rejected. BASE_PROMPT does not change tonight
  either way. Provenance: the wording is generic; the motivation is the dev-pool phase
  analysis (E1/B1: 2-20 steps after the last edit) plus the aggregate held-out time split
  above, looked at after the held-out run was scored (no task content used).
- Held-out verdict k6h3s (run 43766318734, wheel 6.0.0.dev5, Synthetic, 50 tasks, 1 attempt;
  report `karox6-bench-data/reports/heldout-k6h3s`): KaroX 6 16/50, DSH 17/50, OpenCode
  15/50. Raw usage $3.20 / $2.30 / $4.80; miss-normalized (`uncached.py --heldout`, same 50
  tasks) $2.41 / $2.04 / $4.10, i.e. KaroX 1.18x DSH (k6h1s: 1.43x). LLM calls 2835 / 2256
  / 4351 (k6h1s KaroX: 3670, so dev5 makes 23% fewer calls). Min per trial 11.9 / 4.7 / 7.9.
  Against k6h1s only task 35 changed (3 of 4 F2P tests). M2 bar still not met: one task
  behind DSH and 18% dearer; ahead of OpenCode on both. KaroX alone solves 48 again and
  breaks one P2P test on 60 again. C1 (run 43794614606) started 02:51 UTC.
- MCP client hardening (commit e6bf3d4 + review fixes): server pings answered (other server
  requests get -32601 instead of silence), timed-out/interrupted calls cancelled on the
  server, crashed stdio servers restarted on the next call (max 3), per-server `timeout`,
  HTTP session renewal after a 404 (once, under a lock, concurrent callers share it),
  stderr tail quoted when a server fails. Independent review (agy Gemini 4 Argon, high,
  768 s, ~$1.25 API-equivalent): 7 findings, all confirmed by hand and fixed (cancelled
  stderr task leaking CancelledError, broken pipe without the stderr tail, close() leaking
  the process when cancelled, 404 renewal race, SSE server request taken for the reply,
  cancel notice for an unsent request, httpx timeout below the connect timeout). Live check
  against `karox6 mcp` over stdio: 7 tools in 0.23 s; killed server restarted in 0.2 s.
  624 tests.
- C1 verdict (run 43794614606, tag k6c1, dev6 + `--code-mode`; report
  `karox6-bench-data/reports/dev-k6c1`): 6/11 solved (DSH 7, OpenCode 9), 4 of E1's 9 clean
  tasks, 40 LLM calls per task on them. Rejected by the rule (needs >= 9): code mode stays
  opt-in. But the model never called `run_code` (0 calls in 11 trials, although the tool
  was offered: 11 tools instead of 10), so C1 measured dev6 with one unused tool, not code
  mode. What it does show is the noise of one attempt per task: against E1 the same base
  prompt lost 13, 39, 46, 59 and won 52. Code mode needs a prompt nudge to be used at all;
  that is a separate A/B (not tonight).
- k6n1 control, fixed before its run: dev6 without flags on the 11 dev tasks, 1 attempt,
  Synthetic, right after V1. Purpose: a same-wheel, same-night reference for V1 and a second
  noise sample; no adoption decision rides on it. V1 is still judged by its rule above;
  V1 vs k6n1 is reported alongside.
- V1 (run 43798764208, tag k6v1) did not run: from ~03:35 UTC every request to Synthetic
  fails with HTTP 402 "Insufficient credits and no active subscription"; all 11 trials
  stopped at the first model call (0 tokens). C1 had finished at 03:32 with no 402 in any
  trial. The k6n1 control (run 43800330980) was cancelled at once for the same reason. The
  V1 rule stays as fixed above and waits for a working upstream; ReformBoss is paid and
  limited to one runner at a time (2.5 h for 6 jobs last time), GLM is a different model.
  Bench runs are blocked until the Synthetic subscription is renewed (the user's call).
- Code mode prompt: with `--code-mode` the system prompt now gets one paragraph saying when
  to use `run_code` (several tool calls whose inputs are already known), because C1 showed
  the tool alone is never picked. Off without the flag; BASE_PROMPT unchanged. A live check
  on a free model (CrowLLM grok-4.7-fast) was inconclusive: it batched four reads into one
  step without `run_code`, then claimed a fix it never made (a model failure). Code mode
  waits for its C2 A/B on DeepSeek when an upstream is available.
- 6.0.0.dev7 = dev6 + MCP client hardening + the code-mode paragraph (flag only). For
  bench runs without MCP servers or flags it behaves as dev6, so the V1 rule can be run on
  it unchanged. 624 tests; clean-venv wheel smoke OK.
- dev7 wheel in the bench repo (next to dev6; branch karox6-adapter). Free replays in the
  real hard12 images, no model: `karox6_basics` (run 43800916266) and `karox6_code` (run
  43800923218) green on all three images.
- Next queue, rules fixed before any of these runs (`karox6-bench-data/queue_next.sh`, one
  command, ~4 h, stops itself if a run makes no successful LLM call). C1 showed that one
  attempt per task swings 3-4 of 11 tasks, so every arm gets 2 attempts and a same-wheel
  control:
  - k6n2: dev7, no flags, 11 dev tasks x 2 attempts (22 trials), Synthetic, parallel 3.
  - k6v2 (V1 prompt, rule revised before any V1 trial ran; the first V1 run died on 402):
    candidate for the next held-out run if its solved trials >= k6n2's and its LLM calls
    per trial <= 85% of k6n2's; otherwise rejected.
  - k6c2 (`--code-mode` with the new paragraph): code mode on by default if solved trials
    >= k6n2's, LLM calls per trial <= 85% of k6n2's, and `run_code` is called in at least
    11 of the 22 trials; otherwise it stays opt-in.
  - A winner becomes the default only after a held-out run.
- Second review of the MCP client fixes (opencode DeepSeek-V4.1-Flash free, high, 449 s,
  ~$0.06 API-equivalent): 5 of 7 fixes confirmed, 2 partial, 2 new small defects; fixed:
  a stdio request now has one time limit covering the send as well (a stuck pipe times
  out), session renewal waits on its lock within the call's limit and initializes within
  the connect limit, a stalled cancel notice is cancelled when the HTTP client closes.
  Kept on purpose: when a server dies, pending calls fail up to 0.5 s later so that the
  error can quote its stderr. 624 tests; live self-check unchanged.
- Synthetic probe 05:53 UTC (run 43813130259, task 28): still HTTP 402. Benches stay blocked.
- Dev-pool finding (E1/B1/C1/k6d1 trajectories only): the model calls failing tests
  "pre-existing" or "unrelated" in 13 of 42 trials and never checks it (0 `git stash`
  calls). In 2 of them it had broken the test itself (B1 32, C1 59: it re-ran the suite
  with `--deselect` on the test it broke); in C1 40 it kept 3 broken tests as "expectations
  of the old behaviour". Dev-pool P2P regressions by KaroX: 0, 0, 1, 2 per run.
- V2 rule, fixed before its run (queue arm k6r2, 11 dev tasks x 2 attempts, after k6v2):
  `--append-system-prompt "Tests that passed before your change must still pass. If a test
  fails after your change, check whether it also fails without it (git stash, run the test,
  git stash pop) before calling it pre-existing; if your change broke it, fix your change
  rather than the test, unless the task asks for that behaviour to change."` Candidate for
  the next held-out run if its solved trials >= k6n2's and its LLM calls per trial <= 110%
  of k6n2's (it may add a check step); otherwise rejected.
- Live end-to-end check of `karox6 mcp --http` driven like a remote client (bearer token by
  environment, KaroX's own HTTP client): initialize + tools/list 0.05 s; with GLM-5.3-Flash
  (FreeInference, free) `karox_run` with wait_seconds=20 came back after 16.5 s with the
  task done (bug fixed, test passing), `karox_diff` returned the exact diff. With CrowLLM
  (upstream down: Cloudflare 521/525, then 404) the run ended as `error` with the retries
  and the reason in the brief log. No server-side issue found. The model's own bad first
  `edit` (old_string ending mid-line) is a model error; on the dev pool DeepSeek never left
  a .py file unparsable (0 of 225 edits), so a post-edit syntax check is not worth adding.
- k6h3s vs k6h1s context: mean input per LLM call 91k (DSH 85k) after dev5, down from 116k
  (DSH 70k); the cost gap to DSH is now mostly the number of calls (1.26x).
- Windows fix found by a live headless run in a Cyrillic path (GLM-5.3-Flash, free): with
  stdout redirected, Python used the ANSI code page (cp1251), so `--json` events were not
  UTF-8 (a Cyrillic workspace path broke every JSON reader), and one emoji in the model's
  reply raised `UnicodeEncodeError` and ended the run with exit code 2. `karox` now writes
  piped stdout/stderr as UTF-8 and never raises on an unencodable character (terminals
  keep their encoding, errors replaced). Live rerun: exit 0, valid UTF-8, task solved, the
  model saw correct Cyrillic in tool output. Linux bench unaffected. 626 tests.
- Same family: a task piped on stdin (`cat task.md | karox run -`) was decoded with the
  locale code page (cp1251 on Windows), so UTF-8 text from Git Bash or PowerShell 7 became
  mojibake. It is now read as UTF-8 (BOM allowed), falling back to the code page only for
  bytes that are not UTF-8. Live: a Cyrillic task piped from Git Bash reached the model
  intact. Child processes already get PYTHONIOENCODING=utf-8. 628 tests.
- MCP stdio on Windows: `karox_diff` answered "not a git repository" for a real repo, and
  the system prompt's git line said the same. Cause: helper processes (`git`, the python
  probe, `taskkill`) inherited KaroX's stdin, which under `karox6 mcp` is the JSON-RPC
  pipe a reader thread blocks on; on Windows such a child hangs in process creation until
  its timeout (repro: 8.0 s timeout vs 0.03 s with stdin closed). Every child process now
  gets an explicit stdin, and a test scans the source so a new spawn cannot forget it.
  Live: stdio run in a Cyrillic repo, `"git": true`, diff with Cyrillic paths intact, 14 s.
- Cancelling a run (Ctrl+C, `karox_cancel`) left the foreground shell command running: the
  shell tool did not kill the process tree when its wait was cancelled (`run_code` did).
  Found through an "unclosed transport" warning in the MCP tests, which a `sleep(0.2)` in
  the test had been hiding; the sleep is gone and the warning with it. 631 tests.
- Windows `bash` (Git Bash) lost backslashes: its runtime parses the Windows command line
  itself and halves every run of backslashes inside quotes, so a model's `grep -E '\.'`,
  `'C:\Users'` or `printf 'a\nb'` reached bash broken (probe: 2 sent, 1 received; 4 -> 2),
  and commands over 32767 characters could not start at all. Git Bash now gets the command
  as a script file next to the command log (`cmd-NNNN.sh`); POSIX keeps `bash -c`. Tests:
  1/2/4 backslashes in single quotes, and a 40 000-character command.
- `karox6 mcp` shutdown, checked as a real process with a fake model running a long command:
  (1) stdin closed while a `karox_status` long-poll was in flight: the server waited for the
  long-poll (up to 50 s) before stopping runs, while clients kill it ~2 s after closing
  stdin, so the command was left running; in-flight requests now get 1 s, then are
  cancelled (exit < 3 s, command gone). (2) SIGTERM, how Claude Code/Codex stop servers on
  Linux/macOS: Python died without running `finally`, the command (own session) kept
  running; `karox run/chat/mcp` now treat SIGTERM like Ctrl+C and exit 143 after cleanup
  (WSL: old CLI -> "the command outlived the server", new -> pass). stdin is read by a
  daemon thread with raw reads, so a blocked read no longer holds up the exit. Child
  process transports are closed explicitly (the "unclosed transport" warning is gone from
  the suite, checked with warnings as errors). 636 tests (Linux 626 + 11 Windows-only).
- Windows: a KaroX killed outright (an MCP client's TerminateProcess, the console window
  closed, Task Manager) runs no cleanup, so its commands, background jobs and MCP servers
  kept running. Every process KaroX starts now goes into a job object with
  KILL_ON_JOB_CLOSE (and BREAKAWAY_OK, so a program that asks to detach, such as an editor
  launched through libuv, still can; libuv does the same for Node's children). Test: hard
  kill of a KaroX process -> its command is gone; a breakaway child survives; without the
  binding the command outlived KaroX. 639 tests (Linux 628 + 13 Windows-only), startup
  115 ms.
- MCP server honours `notifications/cancelled`: the client's request with that id (per
  client: the HTTP session, or the stdio peer) stops and gets no answer, as the spec asks.
  A cancelled karox_run/karox_status only stops waiting; the run goes on (karox_cancel ends
  runs). Before, a long-poll the user had cancelled kept its slot for up to 50 s and then
  answered nobody. 640 tests (Linux 629 + 13 Windows-only).
- Linux/macOS (and Windows): a foreground `bash` call that leaves a child running
  (`python -m http.server &`, `server & sleep 1 && curl ...`) returned only after its full
  timeout (120 s by default) as "still running ... moved to background job", although the
  shell had exited at once: since Python 3.12 asyncio's `Process.wait()` also waits for the
  output pipe to close, which the child holds. Exit is now taken from the exit status (WSL
  probe: 20.0 s -> immediate). Such a child is registered as a background job (the model is
  told its id), so `job_output` reads it and the session stops it at the end; on POSIX
  `kill()` now also reaches a process group whose leader already exited.
- Independent review (codex gpt-6.1-terra, 58 s, ~$0.09) of today's lifecycle changes:
  fixed `KAROX_SHELL` pointing at Git Bash/MSYS2 (it bypassed the script-file launch), `rg`
  not bound to the Windows job and not killed on timeout/cancel, and the left-behind child
  above. Not taken: the microsecond race between process start and job assignment (bash and
  node take tens of ms to spawn children; atomic assignment needs CREATE_SUSPENDED, which
  Python's subprocess cannot resume) and `$0` being the script path (checked: exit codes,
  heredocs, set -e, pipefail, Cyrillic, syntax errors behave the same). 648 tests (Linux
  638 + 13 Windows-only).
- Second independent review (opencode DeepSeek free, 677 s) of the same changes, fixed:
  the stdin reader thread now always ends the stream (a missing stdin is a clear error,
  not a hang); a second SIGTERM during cleanup exits at once (143); KaroX joins its Windows
  kill-on-exit job before its first child, so a child that spawns and exits at once cannot
  leave a grandchild outside the job (test "spawner"); a client's cancellation of
  `karox_cancel` was swallowed by `suppress(CancelledError)` around the worker wait (now
  `asyncio.wait`; test fails on the old code). Found while testing: a second cancel during
  a command's kill (shutdown after karox_cancel, a second Ctrl+C) abandoned the kill; the
  kill is now seen through and the cancel raised after. Not taken: bounded stdin queue
  (local, trusted client), `_release` for unpolled background jobs (bounded by the
  session). 651 tests on Windows, Linux green, warnings-as-errors clean twice.
- 2026-11-16 07:30 UTC, user: benches on GLM only where needed (about 4 h left), Tailscale
  preferred over ngrok for the ChatGPT tunnel, KaroX 5 and its bridge may be read freely,
  the OpenRouter $3 may be used (no OpenRouter key is available to the agent; the exact
  DeepSeek model costs $0.30/M input there, about 20 trials for $3).
- dev8 = dev7 + today's fixes (no prompt or tool-schema change; one new marker text when a
  command leaves a child running). Replays karox6_basics / karox6_code started on dev8.
- GLM dev-pool three-arm run g53d1 (run 43824581914, freeinference, parallel 3, 1 attempt,
  11 dev tasks, karox6 dev8 / opencode_v2 / deepseek_harness). Purpose: the first rival
  numbers on GLM (D-026 needs rivals re-run on GLM) and KaroX's standing there. Reading
  fixed before results: one attempt on 11 tasks swings 3-4 tasks, so only a gap of 4+
  solved tasks counts as a difference in score; calls, tokens and minutes per trial are
  compared on tasks every arm finished. Dev-pool trajectories may be read for causes.
- 07:40 UTC, user: Tailscale is on; the connector must work for every client, not only
  ChatGPT; Claude Code subagents may be used too. OpenRouter: the keyring key
  (openrouter@KaroX/provider) and the `.env` keys found on the PC all have a key limit of
  $0-0.01, already used up (the account itself has $15.49 left); a key with a limit is
  needed before any OpenRouter run. Replays on dev8 failed only on tb41
  `risk-scorer-replay`, whose image runs as a non-root user that cannot write /app (the
  replay defaults, not a KaroX change; the other two tasks passed); re-run on hard12.
- `karox mcp --http PORT --tunnel tailscale|tailnet` (new `mcp/tunnel.py`): checks that
  Tailscale runs and has a ts.net name, takes the first of 443/8443/10000 that is free
  (or still routes to this port after a killed KaroX), runs `tailscale funnel|serve --bg
  --yes --https=P http://127.0.0.1:PORT`, prints the connector URL and removes only that
  route on exit. Failures say what to do (sign in, MagicDNS/HTTPS, the Funnel enable link,
  ports taken). Live: Funnel came up in 0.2 s on 443 next to KaroX 5's routes on 8443 and
  10000 (left intact); initialize + tools/list answered 200 over the tailnet and through
  two public Funnel relays (curl --resolve), a wrong token 401, route removed after.
- Connector for every client: a request that carries the token is now accepted from any
  Origin (Claude, Le Chat, web IDEs were refused with 403 before unless listed by
  `--allow-origin`), and CORS preflights are answered for any origin (they carry no
  credentials). The Origin check still refuses token-less requests from unknown pages
  (DNS rebinding). Docs: chatgpt.md/.ru.md cover Tailscale first, ngrok/cloudflared as
  alternatives, and ChatGPT, Claude and other clients. 663 tests on Windows.
- Independent review of the tunnel (Claude subagent, read-only) found real ways to touch
  another program's route, all fixed with tests: a `serve status` that failed or did not
  parse was read as "all ports free" (now KaroX refuses and touches nothing); ports of a
  foreground `tailscale funnel` (under "Foreground") were not seen; stop removed the port
  even if someone replaced the route meanwhile (now it re-reads and removes only its own
  exact "/" route, with --set-path=/); a publish that timed out after being applied was
  left; Ctrl+C while the tunnel was opening could leave a public route (now waited for and
  removed); the target followed 127.0.0.1 even when bound elsewhere. Origin/CORS logic:
  no hole found. Live again: two public relays 200, route removed, KaroX 5's untouched.
- 08:00 UTC, user gave an OpenRouter key with a $4 limit (stored as the bench secret
  OPENROUTER_API_KEY, never printed). Bench: new `openrouter` upstream, the same model
  DeepSeek-V4.1-Flash-1017 pinned to fp8 providers with caching (deepinfra, stepfun,
  novita; no fallbacks), enforced in the gateway for every arm; probe: tools,
  reasoning_effort=max and caching work.
- Held-out run or1 (runs 43828489989 karox6 dev8 + deepseek_harness, 43829985688
  opencode_v2; 1 attempt, parallel 16). Tasks fixed before results: the 30 held-out tasks
  any arm solved or passed at least half of in h3s/baseline (18-24, 31, 34-36, 41, 44, 45,
  47-51, 54, 56, 58, 60-62, 65, 66, 69, 70, 73); the 20 never-reached tasks are left out
  to save money. Reading: solved counts (1 attempt: a gap under 4 is a tie), cost from
  tokens at one price list, minutes per trial on tasks all arms finished. GLM run g53d1
  cancelled at 5/34 trials (a 2-concurrent key at parallel 3 made it ~25 min per trial;
  the same-model lane is now cheap); its finished trials stay as artifacts.
- Connector survives restarts: without `KAROX_MCP_TOKEN` the HTTP token is kept in
  `<home>/mcp/http-token` (0o600 on POSIX, written beside and renamed over) and reused, so
  the pasted URL stays valid; `--new-token` replaces it; an env token is still neither
  saved nor printed. An authorized request with an Mcp-Session-Id this process never
  issued is carried on as a live session (version LATEST) instead of 404, as hosted
  clients may never initialize again; ids ended with DELETE (up to 1000 remembered) and
  malformed ids still get 404. An unknown run_id now says the server was probably
  restarted, the work is on disk (karox_run there + karox_diff, or git) and session_id
  continues the conversation. Tool replies are capped at 100,000 characters:
  karox_status pages events by size (`next` + a `more` note), final_text is cut at 50,000,
  karox_diff cuts to fit JSON escapes and says how much was left out, and `fit_reply`
  guards the rest (largest field cut, oldest runs first, `cut` says what). Not done: an
  offset/path parameter for karox_diff (tool schema unchanged). 668 tests on Windows.
- `karox mcp --direct` (new `mcp/direct.py`), for the user who drives the PC from ChatGPT
  on the web: the chat's own model does the work through KaroX's tools, like the KaroX 5
  bridge did; no `--model`, provider or key (the key variable still leaves the
  environment). Served instead of the karox_* tools: read, write, edit, grep, glob,
  bash/pwsh, job_output/job_list/job_kill, with their agent implementations and output
  caps; one ToolContext per server, so read-before-write state and background jobs carry
  over between calls (jobs are killed on exit). Paths outside `--root` (also through a
  symlink) are refused unless `--any-cwd`; KaroX's own log/spill directory stays readable.
  Annotations: readOnlyHint for read/grep/glob/job_output/job_list, destructiveHint for
  write/edit/shell/job_kill. Destructive commands: deny refuses, allow runs, ask refuses
  with "ask the user in the chat, then repeat with confirmed: true" (the field is in the
  shell schema only under ask). A shell call waits at most 50 s, then the command goes on
  as a background job: hosted clients drop longer calls. Found on the way: parallel shell
  calls counted the same log number, so a Git Bash call could run another call's script
  (test fails on the old code); the log is now claimed before the first await. Known
  gaps: `confirmed` is only as good as the client model asking; shell commands themselves
  are not confined to the root (only the destructive-command rules apply). Live: stdio and
  HTTP smoke (initialize, tools/list, bash, a background job killed with the server).
  675 passed, 5 skipped on Windows (13 new).
- 08:35 UTC: the queued dev-pool A/Bs (rules above) move to the OpenRouter upstream
  (Synthetic is still 402); wheel dev8 for every arm (dev7 + process fixes, no prompt or
  schema change). Budget ($4 key, ~$2.8 going to or1 + its OpenCode arm) covers about two
  of the four arms now, so k6n2 (control) and k6r2 (V2, regression check) are dispatched
  together, 11 dev tasks x 2 attempts each, so both see the same upstream at the same
  time; k6v2 (V1) and k6c2 (code mode) wait for more budget. Rules unchanged.
- Staying up unattended (what the KaroX 5 bridge had): `karox mcp --http ... --supervise`
  (new `mcp/supervise.py`) runs the same command minus `--supervise` as a child (`python
  -m karox6`; `--new-token` only on the first start; environment unchanged, so the child
  reads or pops the token itself) and restarts it when it exits on its own: 1 s, doubling,
  capped at 60 s, reset after 5 minutes of running; exit code 2 is not retried; one stderr
  line per restart. The child has its own process group (Windows: CREATE_NEW_PROCESS_GROUP
  and the kill-on-exit job, so a killed supervisor takes it along); Ctrl+C (exit 0) or
  SIGTERM/CTRL_BREAK (143) on the supervisor sends SIGTERM/CTRL_BREAK, waits 10 s (a second
  Ctrl+C: at once), then kills. Found on the way: on Windows CTRL_BREAK ended `karox mcp`
  without running `finally`, so the tunnel route stayed; run_async now handles SIGBREAK like
  SIGTERM (cancel, exit 143; the existing SIGTERM test now also runs on Windows with
  CTRL_BREAK and fails on the old code). Tunnel health: `Tunnel.keep` reads `tailscale serve
  status --json` and re-applies a vanished route with the same port and target only if the
  port is free or still ours (`usable`, shared with free_port); `karox mcp --tunnel` calls it
  every 30 s in the background, printing a line when it re-applies or cannot (once per
  persisting problem); a lock keeps a re-apply in flight from outliving `stop`. Live smoke
  (Windows, --direct): the server killed with TerminateProcess came back after 1 s on the
  same URL (initialize 200), CTRL_BREAK on the supervisor exited 143 in 0.2 s with the
  server gone, and killing the supervisor outright took the server along. Not done: a
  tunnel failure at start (Tailscale not up yet) is exit 2 and so ends the supervisor; no
  login autostart (docs name Task Scheduler / systemd user units). 713 passed, 3 skipped on
  Windows (22 new, 2 newly run).
- 08:50 UTC: k6n2/k6r2 cancelled before any trial ran: at ~$0.03 per trial the $4 key
  cannot cover or1 + its OpenCode arm (~$3) and 44 more trials; they go again together
  with a third arm once the budget allows, so all arms share the upstream and the hour.
- Dev-pool analysis (Claude subagent, dev tasks only, 41 KaroX + 11 OpenCode trials):
  of 14 genuine KaroX failures, 7 are "fix too narrow" (a sibling implementation, another
  entry path or input form of the same bug left unfixed, usually after seeing it), 2 own
  regressions dismissed as pre-existing (V2's target), 1 deleted behaviour, 3 a hidden
  edge case OpenCode also misses; none wrong diagnosis or out of steps. OpenCode's process
  numbers match KaroX's (first edit at call 33 both, 9.7 vs 9.6 files read, 7.1 vs 6.8 test
  runs); the difference is the scope of the fix. KaroX quotes BASE_PROMPT's "Make the
  smallest change that fully solves the task" in 26 of 42 trials (checked by hand: e.g.
  "Should we fix confluent too? 'Make smallest change fully solves task' -> no."), OpenCode
  and DSH never. Candidate S1 for the next A/B: replace that line with a root-cause-wide
  scope rule (fix the faulty logic in sibling implementations and other entry points,
  preferably at the shared point; keep the diff focused).
- `screenshot` tool for `karox mcp --direct` (new `tools/screen.py`), so the chat model can
  see the user's screen: the whole virtual desktop, or one window by a case-insensitive
  part of its title (exact title wins, else the topmost match), `list_windows: true` for
  titles and sizes as text. Windows: ctypes GDI only, per-monitor DPI awareness on the
  capturing thread (restored after), PrintWindow(PW_RENDERFULLCONTENT, then 0) so a
  covered window is captured without being raised (a hung window or a refusal falls back
  to a screen copy of its frame, said in the reply), cropped to the DWM frame; GDI scales
  with StretchBlt/HALFTONE into a 32-bit DIB, so Python touches only the scaled pixels;
  own PNG encoder (filter 0: compressed UI shots best against Sub/Up); the width drops
  until the PNG is under 1.5 MB. Minimized windows are refused (never restored). macOS:
  `screencapture` (+ `sips` to shrink), Linux: grim or ImageMagick `import` (+ `magick` to
  shrink); window by title only on Windows. Reply: image + text content; annotations
  read-only, not destructive, closed-world; `--no-screenshot` leaves it out; not in agent
  mode. Live (this machine's desktop is 640x480, one monitor): desktop 31 KB PNG in 0.03 s
  over stdio, a covered BitBrowser window at max_width 500 -> 500x339, 73 KB; 2560x1920
  encode ~0.35 s. Gaps: no live multi-monitor or high-DPI check here; PrintWindow can
  still return a black frame for some GPU/protected windows; macOS/Linux paths are tested
  with a fake runner only. 700 passed, 5 skipped on Windows (11 new).
- 09:00 UTC, key limit raised to $6. Dev-pool A/B, all three arms dispatched together on
  OpenRouter (same model, fp8 providers), 11 dev tasks x 2 attempts, wheel named
  explicitly per arm (new bench input `karox6_wheel`):
  - k6n2 control: dev8, no flags.
  - k6r2 V2: dev8 + the V2 sentence (rule fixed above: candidate if solved trials >=
    k6n2's and LLM calls per trial <= 110% of k6n2's).
  - k6s2 S1: wheel 6.0.0.dev8+s1 = dev8 with one BASE_PROMPT line replaced ("Make the
    smallest change that fully solves the task, in the style of the surrounding code. Do
    not reformat or rewrite unrelated code." -> "Fix the root cause everywhere it applies:
    once you find the faulty logic, look for the same logic in sibling implementations
    (other backends, subclasses, duplicated helpers) and in other entry points that reach
    it, and fix those too, preferably at the shared point they all go through. Keep the
    diff focused and in the style of the surrounding code; do not reformat or rewrite
    unrelated code."). Rule fixed now: candidate for the next held-out run if its solved
    trials >= k6n2's + 2, its P2P-regression trials <= k6n2's + 1, and its LLM calls per
    trial <= 115% of k6n2's; otherwise rejected. Secondary: share of trials whose
    reasoning cites "smallest" (expected near 0), non-test source files per diff.
  Trials an upstream error stopped (no successful call, or HTTP 402/429/5xx ending the
  run) are excluded from all arms' counts for that task-attempt.
- 09:05 UTC, grep anchor fix (found by CI on f602eed: `test_mcp_direct` grep `^one$` red on
  every OS). The Python fallback (no `rg` on PATH, or `KAROX_NO_RG=1`) prefiltered each
  whole file without MULTILINE, so `^`/`$` matched only at the file's start/end and an
  anchored pattern missed every later line; `karox run` was affected wherever rg is
  missing (rg is present in the bench images, so bench numbers are unaffected). CRLF files
  also broke `$` in both backends: rg now gets `--crlf`, the prefilter reads `\r\n` as
  `\n`. Tool-contract bug fix, no prompt or behaviour choice involved: no A/B. New test
  fails on the old code in both backends, passes now; full suite green on Windows.
- Secret masking for `karox mcp` (new `mcp/redact.py`): everything sent back to the
  client goes through it, in both modes; KaroX's own model and the disk see real values.
  Rules: private key blocks (also cut short, END-only tails, line-numbered reads and
  JSON-escaped keys), token formats (sk-/sk-ant-/sk-or-, gh*_/github_pat_, glpat-, xox*,
  AKIA/ASIA, AIza, JWT, sk_/rk_live/test, hf_, npm_, pypi-), Authorization headers and
  bare Bearer/Basic, URL passwords, `KEY=value` / `key: value` / `"key": "value"` under a
  secret-ish name, and exact values of secret-looking environment variables (taken before
  the key and token variables leave the environment) plus the HTTP access token. Agent
  replies are masked as one JSON text (a key beside its value), then fitted; the rules
  keep JSON valid, with a string-by-string fallback. Deliberately not masked: random
  strings with no known prefix or secret-ish name (git SHAs, UUIDs, lockfile hashes,
  base64), code references (`os.environ[...]`, `settings.X`, `f(token=token)`), templates
  (`${X}`, `<...>`, `xxxx`). Direct mode refuses a write/edit/shell call that carries a
  marker, so a masked secret is never written back over the real one. `--no-redact`
  turns it off; the initialize instructions say secrets are masked. 100k characters take
  about 10-30 ms (adversarial inputs included). Known gaps: lowercase keys with
  letters-only values in code style (`password=letmeinplease`), numeric-only secrets, secrets
  with quotes/backslashes for exact matching, XML `<password>`, `.netrc`, tokens glued to
  ANSI colour codes. 784 passed, 5 skipped on Windows (95 new).
- 09:08 UTC, merged wt/secrets. Integration fix: the masking loop read `item["text"]` on
  every content item, and the screenshot reply (merged in parallel) carries an image item:
  KeyError -> internal error on every screenshot; now only text items are masked (the
  existing screenshot reply test catches it).
- 09:35 UTC, or1 speed analysis (23 paired tasks, report in
  karox6-bench-data/reports/or1-speed.md): K6 163 min vs DSH 123 / OC 140 with 11 / 8 / 10
  solved. Extra time: tests (+19.6 min vs DSH: broad re-runs after passing narrower ones,
  600 s timeouts on 42% of test commands, waiting on promoted jobs), reasoning per call
  (+20%), and two runaway replies of 32,768 output tokens that OpenRouter reported as
  `tool_calls` (4.3 min). Bug fix: openai_chat now treats a reply whose output tokens reach
  max_tokens as a cut (`length`) whatever finish reason the server sends, so the
  truncation notice fires and the step is logged as cut. Test scoping, promotion hint and
  the economy bullet are candidates for a pre-registered A/B on held-out tasks.
- 09:40 UTC, dev-pool A/B verdict (runs 43835356411 k6n2, 43835368408 k6r2, 43835380463
  k6s2; 11 dev tasks x 2 attempts, no upstream failures, versions checked: dev8, dev8,
  dev8+s1; V2 prompt present only in k6r2):

  | arm | solved / 22 | P2P-regression trials | LLM calls / trial | cost | agent wall |
  |---|---|---|---|---|---|
  | k6n2 control | 15 | 0 | 50.4 | $0.618 | 151 min |
  | k6r2 V2 | 18 | 0 | 48.7 (97%) | $0.588 | 169 min |
  | k6s2 S1 | 17 | 2 (montepy, both attempts) | 50.7 (101%) | $0.632 | 156 min |

  V2 meets its rule (solved >= control, calls <= 110%): candidate for the next held-out
  run. S1 fails its P2P rule (2 > control + 1): rejected; its "fix it everywhere" wording
  widened montepy changes into breaking existing tests. V2 costs 12% more agent wall time
  (stash-and-rerun checks); the speed candidates target that. Evaluator:
  scratchpad ab.py (F2P/P2P from data/val_new.jsonl, env failures = P2P failing in every
  attempt of the task across arms).
- 09:50 UTC, speed bundle T, pre-registered before any run (branch wt/speed, wheel
  6.0.0.dev9+t = main + three changes):
  1. BASE_PROMPT "Verify your work..." bullet -> "Verify your work with the narrowest tests
     that exercise your change (test ids, -k, the test file of the module you changed) or a
     short reproduction script, and check the [exit code: N] marker of every command.
     After a fix, re-run what failed before widening. Run a broader selection (a test
     directory or the whole suite) at most once, at the end, and only if it should finish
     within about two minutes; start a longer one in the background and keep working. Do
     not re-run a slow selection, or a wider one that contains it, unless code it covers
     changed since. Fix failures your change caused before you finish."
  2. bash foreground cap 600 s -> 300 s (the command keeps running as a background job).
  3. The promotion marker adds "If it runs more than you need (a whole suite or
     directory), job_kill it and run a narrower command; otherwise ...".
  Rule for the next held-out OpenRouter run (the 20 held-out tasks or1 did not use: 14,
  15, 16, 17, 25, 26, 27, 29, 30, 33, 37, 38, 42, 43, 53, 55, 63, 64, 67, 72; 1 attempt;
  arm V2 = dev9 + the V2 sentence, arm VT = dev9+t + the V2 sentence, plus a dev9 control
  if the budget allows): VT is adopted if its solved trials >= V2's - 1, its
  P2P-regression trials <= V2's + 1 and its summed agent wall time <= 90% of V2's;
  otherwise rejected. With a control arm, V2 is adopted if solved >= control's and LLM
  calls <= 110% of control's. Upstream-failed trials are excluded for all arms of that
  task. A FreeInference GLM smoke (tasks 19 and 20, 1 attempt, VT only) comes first and is
  not a verdict: it only checks that the prompt and the marker are live and counts test
  commands and their time.
- 10:00 UTC, or1 final (30 held-out tasks, 1 attempt, same model and fp8 providers for all
  arms, prices of deepinfra/fp8 for all): solved KaroX 6 (dev8) 16, OpenCode 15, DSH 12
  (DSH hit the 60 min limit on task 70); cost $0.84 / $0.76 / $0.76; agent wall 304 /
  256 / 226 min (LLM 156 / 132 / 130 min). By the pre-registered reading (a gap under 4
  is a tie at 1 attempt) KaroX ties OpenCode and leads DSH by 4; it is the slowest and
  ~10% the dearest. The six tasks finished last (47, 54, 58, 62, 70, 73) show the same
  time sinks as the speed report: whole test directories re-run (4-5 min each) and
  300 s job_output waits. OpenRouter key: $4.22 of $6 used.
  Caveat on the rule above: the 20 held-out tasks or1 left out are the never-reached ones
  (no arm solved or half-passed them before), so solved counts there will barely move and
  trials there are long and dear (~$0.05-0.10 each); two arms likely exceed the $1.78
  left. The held-out VT run waits for more budget or a different task choice, fixed here
  before it starts.
- 10:16 UTC, user: OpenRouter is off limits now; work with the free/subscription models for
  the remaining ~1.5 h. FreeInference GLM-5.3-Flash mini A/B dispatched (runs 43847138848
  gv2 = dev9 + V2, 43847166118 gvt = dev9+t + V2; tasks 58 and 62, 1 attempt, parallel 1
  each): directional only, not a verdict (different model, 2 trials per arm, tasks already
  inspected); read: test-command minutes, agent wall, solved. Smoke 43841655368 (dev9+t +
  V2, GLM) task 20: version and V2 flag live; it started the long directory run in the
  background as told, then still waited 253 s on job_output (wait cap unchanged in T).
  Reviews of dev8..dev9 delegated: codex gpt-6.1-terra and opencode deepseek-v4.1-flash-free
  (secret masking, grep, cut detection), agy Gemini 4 Hydrogen (screenshot, direct mode,
  supervisor).
- 10:20 UTC, review fixes (codex gpt-6.1-terra on masking/grep/cut; agy Gemini 4 Hydrogen
  on screenshot/direct/supervisor; each finding checked before acting):
  - direct mode: `confirmed: true` now runs only a call that was refused before (same name
    and arguments), once, within 15 minutes; a model can no longer skip the refusal that
    tells it to ask, or confirm one command and run another (Gemini).
  - direct mode: client timeouts given as numeric strings or out of range are coerced to
    1..50 s (a non-numeric one already got a clean argument error; Gemini's TypeError
    claim did not reproduce).
  - screenshot: a window closed between listing and capture is an error instead of a copy
    of the screen at its old place; png_size rejects a PNG shorter than its header.
  - supervisor: a closed stderr no longer stops the restart loop.
  - masking: secret-named environment values that are numbers (>= 8 digits) are masked
    exactly (codex). Documented, not changed: screenshots are not masked; code literals
    under secret-looking names are masked (edit around them; the write guard refuses the
    marker). Not changed: values with escaped quotes inside JSON, the exact-cap case of
    cut detection (a stop at exactly max_tokens is treated as a cut; harmless notice).
  - Open (not done, logged): direct-mode jobs, file-tracker sets and spill files grow for
    the server's lifetime (Gemini); the agent-mode marker guard (codex).
- 10:24 UTC, jobs: finished jobs whose end was already reported are forgotten beyond the
  newest 50, so a long-lived `karox mcp --direct` server does not keep every job (Gemini's
  growth finding; agent runs never reach 50 finished jobs, so bench behaviour is unchanged).
  File-tracker sets and spill files still grow with use (small; logged).
- 15:40 UTC, OpenCode (deepseek-v4.1-flash-free, effort high) review fixes, each probed:
  masking now covers bare *_KEY env-style names (APP_KEY), *_PWD, a registry's "auth",
  AccountKey, and digit-only values under secret names; the JSON fallback (used when
  pattern masking would break JSON) masks values under secret-named keys whole instead
  of failing open; cut detection compares with the max_tokens actually sent (extra_body
  may override it) and needs output == cap (a provider ignoring the cap is not a cut).
  Logged, not changed: the marker guard also refuses edits to KaroX's own masking code.
- GLM-5.3-Flash mini A/B (directional, not a verdict), tasks 58 and 62, all four solved:
  gv2 (dev9 + V2) 13.6 + 11.4 = 25.0 min, 92 calls, $0.070; gvt (dev9+t + V2) 13.0 + 7.9
  = 20.9 min (-16%), 80 calls, $0.050. Smoke (dev9+t + V2): task 19 solved in 59 min on
  GLM (shared 2-concurrency key with the A/B runs), task 20 unsolved in 20 min.
  Consistent with T's aim; the held-out verdict still needs the same-model run.
- 16:58 UTC, user: run the held-out V2 vs VT comparison on free FreeInference GLM-5.3-Flash
  instead of OpenRouter (slow, free, no hurry). Pre-registered before dispatch: the 20
  held-out tasks of the 09:50 entry (14, 15, 16, 17, 25, 26, 27, 29, 30, 33, 37, 38, 42,
  43, 53, 55, 63, 64, 67, 72), 1 attempt, arm hv2 = wheel dev9 + the V2 sentence, arm hvt =
  wheel dev9+t + the V2 sentence, parallel 1 per arm (key allows 2 concurrent). Same rule
  as 09:50: VT adopted if solved >= V2 - 1, P2P-regression trials <= V2 + 1 and summed
  agent wall <= 90% of V2's; upstream-failed trials excluded for both arms of that task.
  The verdict holds for GLM; it is the decision basis for merging T into main (DeepSeek
  confirmation only if budget returns). No control arm (concurrency).
- 19:09 UTC, user stopped the held-out GLM run (43913446007 hv2, 43913476747 hvt) after 2 of
  20 tasks per arm (14: 25 vs 26 min, 15: 60 vs 57 min): no verdict under the 16:58 rule;
  T is not adopted and stays on wt/speed. Main keeps dev9 behaviour (600 s cap, old verify
  bullet). FreeInference concurrency (2) is per account; a second key shares it.
- 19:58 UTC, new held-out source (user: only verifiably solvable tasks, from public benches):
  DeepSWE v1.1 (datacurve-ai/deep-swe @ 0b9fabb, 113 never-merged tasks, public-API
  verifiers). Per-task data from deepswe.datacurve.ai/artifacts/v1.1/trials.json: kept the
  45 tasks GLM-5.3-Flash (mini-swe-agent, max effort, 4 runs) solved 1-3 times, minus the 23
  Epoch flagged. Bench repo set `deepswe` (scripts/import_deepswe.py): gateway-only agent
  network, agent timeout 3600 s for every arm, collect = committed + uncommitted work vs
  base through a temporary index. Oracle arm: 45/45 solved on GitHub runners (run
  43941849886; the first oracle run died on CRLF from a Windows clone). The old hard12
  held-out tasks: 3 of 73 need private names absent from the task text (14, 15, 17).
- 19:58 UTC, pre-registered DeepSWE run on FreeInference GLM-5.3-Flash (user approved, night):
  18 tasks (seed 20261116, 6 each Python / TypeScript / Go; set indices
  3,8,12,15,16,17,20,22,25,27,28,29,30,33,37,40,41,45), 1 attempt, arms karox6 (wheel
  6.0.0.dev10 = main, no extra flags), opencode_v2, deepseek_harness, parallel 4. Reading:
  solved trials per arm; a gap under 4 is a tie at 1 attempt; upstream-failed trials are
  excluded for all arms of that task; then minutes and LLM calls on tasks every arm
  finished. Not a tuning set afterwards: trajectories may be read for causes, any change
  is judged on other DeepSWE tasks.
- 22:40 UTC, dsg1 interim (16 of 54 trials): Harbor's ApiRateLimitError label also lands on
  trials that recovered and solved the task (karox6 dynamodb, reward 1), so the label alone
  does not mark an upstream failure. Clarification written before the remaining 38 trials:
  "upstream-failed" = the agent's run ended on a provider error (its last model reply was a
  429/5xx or an empty reply it took as the end); the final report gives the score both ways
  (this definition and Harbor's label).
- 22:40 UTC, pre-registered A/B "spec audit" (from reading dsg1 karox6 misses cattrs and
  fastapi-implicit-head: in both the agent wrote its own tests around a misreading of one
  task sentence and finished on them green). Arm A = dev10 as is; arm B = dev10 +
  `--append-system-prompt` "Before you finish, re-read the task statement sentence by
  sentence and check each stated requirement against your code: exact names, return types
  and shapes, what must be preserved, excluded or matched. Your own tests only prove what
  you wrote them to check; if the code and the statement disagree, fix the code, not the
  statement or docstring." Tasks: the 16 other DeepSWE-set tasks picked by seed 20261117
  (4 per language where available; set indices 1,2,5,6,7,14,18,19,23,24,26,31,35,36,42,44),
  1 attempt, FreeInference GLM, both runs at parallel 2, dispatched after dsg1 ends.
  Reading: B adopted into the base prompt only if solved(B) >= solved(A) + 3 with
  upstream-failed tasks (definition above) dropped for both arms; otherwise not adopted
  (1-attempt noise is 3-4 tasks). Wall minutes reported, not a gate.
- 05:20 UTC, dsg1 result (run 43944899185, 18 DeepSWE tasks, GLM-5.3-Flash, 1 attempt):
  all tasks: karox6 12, deepseek_harness 10, opencode_v2 7. Pre-registered reading, with
  upstream-failed tasks (22:40 definition; checked by scratch dsw_upstream.py) dropped for
  every arm: 9 tasks dropped, all for deepseek_harness ending on a 429 or an empty reply
  (karox6 and opencode_v2: none); on the other 9: karox6 6, deepseek_harness 8, opencode_v2 4.
  Both readings are ties under the "gap under 4" rule: KaroX 6 is not shown better than
  DSH on DeepSWE. KaroX's lead in the raw count comes from surviving the 2-slot 429s that
  stopped DSH. KaroX misses in the kept set: cattrs (2 of 69, spec sentence misread),
  kysely (type-level tests: its types accepted calls the tests expect to be type errors),
  opa (Go build with the hidden tests failed). Four KaroX trials hit the 3600 s cap and
  still solved 3 of them (collect hook).
- 09:40 UTC, spec-audit A/B decided early (runs 44021663883 A, 44021705895 B; 11 of 16
  trials per arm done): on the 9 tasks both arms finished (bandit-nosec dropped: B's image
  pull hit an ECR 429 "data limit"), A solved 8, B 4. With 5 tasks left B cannot reach
  A + 3, so B is not adopted under the 22:40 rule; the base prompt stays as is. B's losses
  are mostly one-test misses (abs 5/6, expr 78/79, happy-dom-io 13/14) plus boa 0/17 and
  they ran no shorter: the audit sentence did not fix spec misreadings and cost steps.
  Lesson: the three dsg1 misreadings were not cured by a generic "re-read the statement"
  instruction at the end; any further fix must target how the agent forms its own tests.
- 11:30 UTC, user asked for the best possible system prompt from the prompts of other top
  agents. Corpus (public repos, shallow clones in D:\проекты\prompt-corpus, ~40 coding-agent
  prompts selected into sel/g1-g3): Claude Code (Opus 5.5, headless and code-mode Fable 5.2),
  Codex (GPT-6.1-Sol, GPT-5.x-codex, models-manager), OpenCode (anthropic/gpt/kimi/meta/
  trinity/runner), Qwen Code, Kilo, MiMo, Cursor 2.0 + CLI, Devin, Windsurf, Amp, Augment,
  Junie, Warp, VSCode Copilot, Gemini CLI, Z.ai/ZCode, Qoder, Trae, Hermes, mini-swe-agent.
  Surveys by agy (Gemini 4 Hydrogen), Codex (gpt-6.1-terra), OpenCode (DeepSeek free);
  draft reviewed by Codex (gpt-6.1-sol), OpenCode and agy (Gemini 4 Argon), all three
  against the failed spec-audit A/B. Result: prompts/base_v2_draft.md (522 words; not yet
  in code). Dropped after review: per-requirement test lists, an up-front requirement
  taxonomy, "stop exploring once you can name the files", the end-of-run last-message
  audit. Adoption only through a pre-registered A/B on unseen DeepSWE tasks (user's call).
- 12:00 UTC, user adopted base prompt v2 directly, without an A/B (commit 230d85e, wheel
  6.0.0.dev11; ruff, mypy, pytest green). Data run v2f (run 44060598494, FreeInference GLM,
  1 attempt, karox6 only) on the three dsg1 KaroX misses whose causes shaped v2: cattrs
  solved (dev10: 67/69), fastapi-implicit-head-options solved (dev10: 38/43), kysely pending.
  These tasks informed the prompt, so this is a sanity check, not evidence of a gain.
- 12:00 UTC, pre-registered comparison v2c (user asked for 4 tasks that KaroX or another CLI
  failed in dsg1): koota-pair-relation-tracking (all three arms failed), textual-kitty-key-
  phases and opa-rego-rule-profiling (KaroX failed, both rivals solved), scriggo-method-
  declarations (KaroX solved, both rivals failed); set indices 28,33,37,40. Arms karox6
  (dev11), opencode_v2, deepseek_harness; 1 attempt; FreeInference GLM; parallel 2 (the
  account's concurrency, so 429s do not decide the result as in dsg1); dispatched after v2f
  ends. Reading: solved per arm, upstream-failed trials (22:40 definition) dropped for every
  arm of that task. Four tasks at one attempt cannot prove KaroX better (noise is 3-4 tasks
  on 11); the result is data. opa's miss (test name collision) shaped one v2 sentence.
- 16:00 UTC, v2f final: kysely solved too (55 min), so dev11 solved all 3 dsg1 KaroX misses
  (tasks that shaped v2; sanity data only).
- 16:00 UTC, v2c result (run 44070073090, dev11, 1 attempt, parallel 2): all 4 tasks:
  opencode_v2 3, karox6 2, deepseek_harness 1. Pre-registered reading: koota-pair dropped
  (deepseek_harness ended on an empty reply after 1.6 min; dsw_upstream.py missed it, found
  by hand in dsh.jsonl); on opa, scriggo, textual: karox6 2, opencode_v2 2,
  deepseek_harness 1. A tie. KaroX solved opa and textual (dsg1 misses) and lost scriggo
  (dsg1 solved): 48/48 F2P but 3 P2P TestIssue967 cases broken, ran to the 3550 s limit;
  koota-pair 37/38 F2P. Both losses are near misses where the final broad test run was not
  done. Tokens on the 4 tasks: input karox6 93M, opencode_v2 55M, deepseek_harness 42M;
  output 0.49M / 0.11M / 0.32M. On 20 shared tasks (dsg1 + v2c) karox6 uses ~1.6x the
  input of the rivals overall, about equal on tasks both arms solved.
- 2026-11-17 evening, user direction: no 3-4 h comparison runs for small changes, verify
  without the model where possible, no benchmark gaming (no remaining-time hints), user
  context limit with a 400k default, data runs only on tasks that are solvable but were
  not solved on our model, no rival CLIs in those runs.
  Design review as an argument: Codex (gpt-6.1-sol) and agy (Gemini 4 Argon) each answered
  a candidate list, then attacked each other's answer and the lead's position; the lead
  accepted points only on code/data evidence (rejected: remaining-time hints, "reasoning
  high everywhere", per-edit tsc/go build, read de-duplication, a windowed loop guard with a
  hard stop, refusing complete calls of a cut-off reply, dropping prune-before-summary).
  Implemented, tested with scripted providers only (dev12, commits 8e88ad8..4fdce88):
  context_limit 400k (model window unknown by default, learned per session from a context
  error), overflow recovery (prune, bounded summary, model-free drop keeping every user
  message), run deadline bounds compaction and shell/job waits, grep context/literal/
  ignore_case/files/count, edit returns the changed region with line numbers, finished-job
  notice carries unread output, read file_paths (20 files / 50 KB), environment lists
  declared project facts, cancellation keeps finished tool results, three accounting bugs
  (double-counted replies, non-consecutive truncation limit, tool-call reply to the
  summary request). Diff reviewed by Codex (terra) and OpenCode (DeepSeek free): 11 real
  defects, all fixed with tests.
- Pre-registered data run dsz1: DeepSWE set deepswe0 = 4 tasks GLM-5.3-Flash failed 0/4 on
  the public leaderboard (mini-swe-agent, max effort) but 19-25 other models solve:
  claude-code-by-agents-recursive-delegation, httpx-streaming-json-iteration,
  koota-deferred-mutation-buffer, csstree-shorthand-expansion-compression; oracle 4/4 in
  our plumbing (run 44134776910). karox6 dev12 only, FreeInference GLM, 1 attempt,
  parallel 2. Reading: solved count against the leaderboard's 0/16 for this model; any
  solve is data, not proof (one attempt). Trajectories of failures are read for causes;
  these 4 tasks then join the tuning pool and stop being evidence.
- dsz1 result (run 44138163101, dev12, 1 attempt): 0/4. Near misses: httpx 106/108 (stream
  not closed on invalid JSON), csstree 74/79 (every failing test is the background
  shorthand), koota 70/71 (hit the 3550 s limit); claude-code-by-agents 2/7 (most contract
  points missed). Three of four ended by the agent's own choice at 29-37 min, old tests all
  pass, no compaction, no upstream failure. New tools used: grep options 4x per trial,
  shell file browsing nearly gone (bash ran tests); read file_paths never used. The
  leaderboard has this model at 0/16 on these tasks, so 0/4 is no regression signal; the
  dominant cause is still finishing before every stated requirement is exercised.
  FreeInference runs paused from here at the user's request (lane needed elsewhere).
- dsz1 causes, argued (gpt-6.2-sol diagnosis, Gemini 4 Argon critique, lead checked the
  logs): csstree read `background-size => "auto auto"` at step 5 and wrote `auto` in code
  and tests; httpx leaves the stream open only on a decode error before the body is
  exhausted (flush-time errors close it); koota filtered the base-trait event in the add
  branch only, and its own test hid it by adding and removing in one batch;
  claude-code-by-agents covered every stated point in its tests, exact mismatch unknown.
  Rejected: prompt rules (contract table, "no hand-copied constants", early stop; the
  prompt already says this and was not followed, and early stop would have cut httpx's real
  base64 fix); an echo-literal detector and an AST linter for generator cleanup / branch
  symmetry (each one is a rule lifted from one of these four tasks, false positives on
  ordinary constants and intended asymmetry); a best-test-state snapshot (every final
  tree here was already the best one). Deferred: a test-failure diff between runs. In
  these logs 15-21 runs per trial use different test subsets (targeted file, full suite,
  /tmp scripts), so a diff between runs of the same command would rarely apply; revisit
  only with an A/B lane. No code change from dsz1.
- Quick check k6q1 (run 44164362591, dev12, ReformBoss DSV4.1F-1017, 1 attempt, parallel 1;
  artifacts in `karox6-bench-data/runs/44164362591`): 1/3. 52 (sqllineage) solved, 11/11,
  but 18.9 min and 79 calls (rivals 4-5 min); 13 and 39 lost exactly as in k6d1: 13 fixed
  the kafka batch nack and left the same defect in the confluent broker
  (`test_batch_nack_rewinds_every_partition`), 39 split snake_case names but not camelCase
  (`getWeather`). First dev12 run on the target model: no regression on these three, the
  sibling-implementation miss (H1) is unchanged; the H1 prompt rule was already rejected
  on cost, so a fix must not be a prompt line.
- `--copy-notes` (experimental, off; dev13, commits 0b58d6c, 144cfc5): after an edit, other
  places in files of the same type that still hold 3 consecutive meaningful lines of the
  replaced code (one of them changed, imports excluded) are appended to the edit result,
  up to 5, once per run, 1.5 s budget. Source: k6q1 task 13 saw `confluent/message.py`'s
  `nack` at step 2 and never opened it; that file held three of the replaced lines.
  Model-free check on git history (`karox6-bench-data/eval_copies.py`, each commit hunk
  replayed as an edit against the parent tree): KaroX-6, last 150 commits: fires on 15 of
  315 qualifying edits, 12 of 21 places were changed by the same commit; KaroX-v5 (many
  snapshot copies under tools/ and lab/): fires on 172 of 965, 622 of 1440 places changed
  by the same commit. Reviewed by codex gpt-6.2-sol (61 s): deadline not enforced,
  FIFO/symlink reads, truncation before sorting, dedup and path-escaping issues, all
  fixed with tests; not taken: an occurrence-aware diff for `ABCABC -> ABC` (only a miss).
  Bench fix: "newest karox6 wheel" sorted by name picked dev9 over dev10..13 in replay.yml
  and the hard12 default; earlier replays of dev10-12 therefore ran dev9 (paid runs passed
  the wheel explicitly and were right).
- Mechanism check k6cn1 (run 44211067172, dev13 `--copy-notes`, ReformBoss, task 13 only;
  this task is where the idea came from, so it is not evidence): the note fired once, at
  the right place ("also appear at faststream/confluent/message.py:75"), and the agent did
  not open that file. The issue names the aiokafka backend; the agent kept to it and
  lost the same hidden confluent test, 6.7 min, 58 calls. The note stays informational
  (no "must" wording: making the model obey a heuristic would trade one failure for
  scope creep elsewhere); it stays off until a run on tasks it was not built from.
  Free replay of dev13 (run 44213099117, karox6_basics, hard12 images, task_dir /testbed):
  3/3 green. Earlier replay attempts failed on my inputs (no scenario; Git Bash rewrote
  /testbed), not on the wheel.
- 2026-11-18, user: ReformBoss runs may be launched without asking. Copy-notes A/B fixed
  before results (tasks the idea was not built from: dev pool 39, 40, 52, 68, 71; dev13,
  ReformBoss, 1 attempt, parallel 1 per run; k6cnA = `--copy-notes`, k6cnB = control):
  `--copy-notes` becomes default-on only if A solves >= B, A's LLM calls <= 110% of B's,
  and at least one note in A points at a place the reference fix changed; otherwise it
  stays off. 5 tasks at 1 attempt is a weak signal: a tie with no useful note keeps it off.
- 2026-11-18, code review round (codex gpt-6.2-astra core+providers, gpt-6.2-sol tools and
  then the fix diff, agy Gemini 4 Argon loop+cli, OpenCode DeepSeek tools; each finding
  traced in code by the lead before fixing; dev14-dev16, commits e04deed..): fixed a Python
  grep hang (no rg in the image + `.*X` on a long minified line is quadratic: 100 KB took
  4 s), compaction replacing history with a tool-call preamble or a cut summary, a partial
  reply after a proxy's [DONE] taken as the final answer, a finished reply discarded and
  re-paid after a late reset, string SSE error codes (rate limit retried as "server"),
  context overflow inside a 5xx, atomic writes, edit keeping non-UTF-8 bytes and mixed line
  endings, job_kill for a `server &` child, read of FIFOs/huge files, summary usage
  overwriting the context estimate, subagent deadline. Not fixed (not on the bench path):
  resume after a crash mid-batch, API key visible to MCP subprocesses, apply_patch first
  match (tool off by default). Replays of dev14 and dev15 green.
- Near-miss trajectory study (agy Gemini 4 Hydrogen, 16 trials, lead checked fastapi):
  the "no final broad test run" hypothesis is wrong: all 7 near misses that ended on their
  own ran the broad suite after the last edit and had 6-44 min left. 7 of 9 failures
  misread or skipped one stated requirement and wrote their own tests around it (fastapi:
  the task says `operations` matches OpenAPI, i.e. a dict; the agent returned a list).
  The prompt-level "re-read the statement" audit already lost its A/B (8 vs 4).
- Pre-registered spec-reviewer probe (model only, no agent; workflow spec-probe.yml,
  cases probes/spec1: 12 failed and 18 solved DeepSWE KaroX trials, statement + final
  model.patch, 2 samples each, DSV4.1F via ReformBoss, prompt fixed in
  scripts/spec_probe.py before results). A hit = a sample names the requirement behind
  the failing hidden tests (judged from verifier test-stdout). Building an in-loop
  reviewer is worth an agent A/B only if hits >= 5 of 12 failed cases (either sample) and
  "NO GAPS" on >= 50% of samples of the solved cases; otherwise the idea is dropped.
- Copy-notes A/B result (k6cnA run 44242941939 vs k6cnB control 44242946895, dev13,
  ReformBoss): 3/5 vs 3/5 (both solve 52, 68, 71; both lose 39 haystack and 40 armi).
  The note fired 0 times in A's five sessions, so the A/B measured noise only (assistant
  turns 311 vs 373; B's armi hit a provider rate limit). Pre-registered rule: tie with no
  useful note -> `--copy-notes` stays off. Kept as an experimental flag; not worth more paid
  runs until a model-free signal shows it firing on bench-like edits.
