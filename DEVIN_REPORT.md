# Devin report (KaroX `devin/big`, bench `devin/bench`)

Status: FINAL for the night of 2026-11-21/22 (UTC). Every run's rule, written before the
run, and its result are in `PROGRESS.md` ("Devin iteration"). Source version 6.0.0.dev37
(code at 573ded0); the wheels the comparison ran are dev35 (disc 1-12) and dev36 (13-20).

## What the comparison says (one model, DeepSeek-V4.1-Flash-1017 on ReformBoss, 1 attempt)

| 17 valid of 18 tasks | solved | ok calls | input M | output k (reasoning) | agent min | cost-eq $ |
|---|---|---|---|---|---|---|
| karox6 (dev35 / dev36) | 10 | 957 | 70.2 | 748 (528) | 139.0 | 0.907 |
| opencode_v2 2.3.2 | 10 | 1080 | 86.8 | 768 (522) | 158.7 | 1.000 |
| deepseek_harness 0.2.2-rc.5 | 12 | 780 | 51.7 | 553 (374) | 111.7 | 0.698 |

By the registered rule (a gap under 4 solved tasks at one attempt is a tie) the three are
not separated. KaroX is not ahead of either: level with OpenCode (9% cheaper, 12% quicker),
two tasks, 30% of cost and 24% of time behind DSH. "KaroX beats both" is not shown.
Codex CLI works through the gateway since bench 6a4201d (devc3: 19 calls, tool use, ended
on its own); it was not compared.

## Part 1: where KaroX spends more; core (dev35) - commits 614438d, d5d3000

Saved runs only (bench `scripts/analysis/`): the "1.6x reasoning" was replies cut while
thinking on Dahl's 8192-token cap; on long tasks the cost is context (compaction at 320k vs
OpenCode's ~171k) and every recorded compaction dropped the prompt cache. dev35: cache-aware
compaction, a notice and effort easing for thinking-only cuts, Retry-After as a floor, daily
allowance = quota, empty reply resent once, stalled stream = timeout (46 tests).

## Part 2: the chat in a real terminal (dev36, dev37) - 9daab3d, 0a616e2, 1326c89, 7096d91, 5d88789

The real `karox6` in a ConPTY/pty, screen read through pyte (`tests/term.py`), scripted
model. Bugs 1-12, fixed in dev36, each with a test: (1) first frame 453 -> 170 ms, the
welcome card popped in a frame late; (2) a streamed paragraph was re-wrapped whole on every
delta, 11 s of CPU for 30,000 characters, now 10 ms; (3) colour lost after the first line of
a multi-line write; (4) text pasted without bracketed paste sent every line as its own
message; (5) provider errors shown as raw JSON, twice; (6) a message sent while a turn runs
was queued invisibly; (7) the status bar lost the approval mode on narrow windows; (8) a
scrollbar drawn when nothing could scroll; (9) `/status` and the status bar disagreed on
context size; (10) a refused command printed the whole refusal in the tool line; (11) tab
stops counted characters, not cells; (12) "[completed]" printed before the turn had ended.
Added: `/undo` `/redo` `/fork` `/init`, pictures, `/mcp`, change preview. Tonight the same
tests ran on Linux for the first time (WSL): 10 of 15 flows failed on one real bug (13).
`docs/perf.md` has Linux figures: first frame 44 ms, ready for input 148 ms (Windows: 170
and 414 ms).

## Part 3: the comparison, providers, hardening - fc34711, ccf8412, 3a6c541, 2d81a0b, 573ded0

Bugs found and fixed tonight (each with tests; none is in the dev36 wheel that was measured):
13. POSIX: keys typed under the start-up frame were echoed over it and Enter arrived as
    Ctrl+J, so a message typed at once was never sent (5d88789).
14. Errors inside a stream: the Responses API's `error` event became "server: None" and was
    retried; rate-limit and quota codes, Gemini's 400s and Anthropic's permission and
    not-found errors were retried as server faults (fc34711).
15. A context overflow ended the run instead of shrinking the conversation when worded as
    Anthropic's "input length and `max_tokens` exceed context limit", or by Gemini, xAI,
    llama.cpp, Bedrock (fc34711).
16. A reply the provider stopped (Anthropic `refusal`, Responses `content_filter`) was taken
    for the final answer (fc34711).
17. A request the provider answers with HTTP 500 on every retry ended the run (devt2b: 30
    refusals of one request while the key served everything else). Now one more try with
    the big tool results cut (573ded0). Not shown to save that trial: no replay without a key.
18. macOS clipboard fallback left an empty PNG when there was no picture and broke on a
    quote in the path; the Linux and macOS paths have tests with stand-in programs (3a6c541).
19. `tests/term.py` on POSIX forked a threaded interpreter, split UTF-8 characters between
    reads and spun on end of file (ccf8412); two tests assumed Windows or raced (5d88789,
    5e749f2).
20. Bench, codex arm (b9696e8, be12460, 8fe67be, 81f5a37, 6a4201d): Codex's root-only files
    killed artifact collection; the audit callback crashed on bridged calls and lost their
    rows; the bridged request (Responses-only fields, list contents, split replies) was
    refused by the upstream with 400. Found with a local chain: Codex -> gateway -> mock.

## Runs (K = karox6, O = opencode_v2, D = deepseek_harness; usage on all tasks of the run)

| tag, run | lane, KaroX wheel | disc tasks | solved K/O/D (valid tasks) | ok calls | input M | output k (reasoning) | agent min | cost-eq $ |
|---|---|---|---|---|---|---|---|---|
| devd1 44936691031 | dahl2 GLM, dev35, K only | 11, 15, 18 | 0 (1 valid; 87% refused) | 180 | 11.4 | 111 (81) | 172.5 | 1.547 |
| devs1 44949328098 | reformboss, dev35 | 2, 7 | 2/1/2 (2) | 70/73/60 | 3.0/3.7/2.5 | 49/40/35 (36/28/22) | 8.8/8.7/8.3 | .059/.055/.049 |
| devt1 44951155404 | reformboss, dev35 | 1, 3-6, 8-12 | 4/7/6 (10) | 526/430/354 | 33.7/26.9/19.7 | 359/268/230 (249/187/154) | 67.4/55.7/43.1 | .444/.364/.306 |
| devt2 44958999941 | reformboss, dev36 | 17, 20 | 1/1/2 (2) | 148/180/190 | 13.9/16.8/16.0 | 149/127/146 (106/80/102) | 24.0/28.2/31.9 | .171/.167/.171 |
| devt2b 44962894203 | reformboss, dev36 | 13-16 | 3/2/3 (3 valid: 3/1/2) | 215/546/244 | 19.6/51.5/20.0 | 191/417/183 (138/292/126) | 41.5/87.8/37.0 | .236/.537/.240 |
| devc0 44951463333 | freeinference, codex | 2 | no artifact (bug 20) | - | - | - | 44 | - |
| devc1 44955966673 | freeinference, codex | 2 | 30 requests, all 503 (lane paused) | 0 | - | - | 43 | - |
| devc2 44962415993 | reformboss, codex | 2 | 1 request, 400 (bug 20) | 0 | - | - | 0.1 | - |
| devc3 44971059267 | reformboss, codex | 2 | PASS (plumbing; score not read) | 19 | 0.7 | 28 (16) | 4.8 | .028 |
| devab1 44971868428 | reformboss, dev36, K vs Kb `--compact-at 170000` | 18, 19 | void: both arms ended on the provider's daily limit | 70/70 (K/Kb) | 6.6/7.3 | 95/86 (63/60) | 11.7/11.5 | .100/.095 |

Pooled: disc 1-12 (dev35, 12 valid): solved 6/8/8, cost $0.503/0.419/0.356, 76/64/51 min.
Disc 13-17, 20 (dev36, 5 valid): solved 4/2/4, cost $0.405/0.581/0.342, 63/94/60 min.
sentry-python-8185 is void (KaroX's trial ended on the provider's 500, bug 17). Requests
since the 21:00 UTC reset: 3,372 audit rows; the last 24 were refused by a daily limit (below).

Compaction A/B (devab1; rule in PROGRESS.md before the run): no result. ReformBoss ended
both trials at 01:45 UTC, 14 minutes in, with "Исчерпан дневной лимит 1500 запросов твоего
ранга": koota-pair stopped at step 70 in both arms with the context at 169k and 162k, below
either threshold, so neither arm compacted; fastapi-implicit-head-options never started.
0 valid tasks, nothing to read, the default stays. The limit it names is 1,500 a day, not
the 5,000 this plan assumed, although about 3,350 requests had gone through since the
reset: the account's rank or limit changed during the night. The lane is closed until
21:00 UTC (00:00 MSK).

## Where KaroX is worse, and why

1. Solved tasks: no lead. All four tasks KaroX lost on disc 1-12 while another CLI solved
   them (haystack-13633, sqllineage-872, linopy-1083, linopy-1088) are answers that pass
   the agent's own tests and miss neighbouring cases of the hidden ones (name forms the
   issue called "not checked", `COPY INTO <location>`, the shared constructor). The dev-pool
   analysis of 2026-11-17 found the same in 7 of 14 failures. It is the scope of the fix,
   not a tool, loop or provider fault: the prompt asks for the smallest change and an
   exact contract, and the one earlier attempt to widen it (S1) broke existing tests.
2. Cost and time on short tasks (disc 1-12): +20% cost and +18% time against OpenCode, +41%
   and +49% against DSH. It is output tokens (408k / 308k / 265k), most in the first third
   of a run: reasoning +49% and visible output +80% against OpenCode there. On the longer
   tasks the surplus is gone (30% cheaper than OpenCode, level with DSH).
3. One trial lost to the provider (bug 17) where the other two CLIs went on.
4. Chat: ready for input on Windows is still 414 ms (interpreter plus imports).

## What to do next

1. Scope of the fix, as an A/B on the dev pool (night tasks 13, 28, 32, 39, 40, 46, 52,
   57, 59, 68, 71; 2 attempts; `karox6+karox6b`, one wheel, the variant appended with
   `--append-system-prompt`): "The examples in a task are samples of a class of inputs:
   fix the cause where all forms of that class pass through, and try two or three
   variants beyond the given examples before you finish. Behaviour the task does not
   concern stays as it is." Rule as for S1: candidate if solved >= control + 2, trials
   that break existing tests <= control + 1, calls <= 115%. About 2,200 requests: two
   days of the lane if its daily limit is 1,500 now (ask the provider which it is).
2. The comparison again with 3 attempts per task: one attempt cannot separate 10 from 12.
3. A lighter start on short tasks (less reading before the first edit), as its own A/B;
   and the compaction A/B again (devab1 gave nothing), on tasks that pass 200k.
4. dev37 (wheel in the bench, 01872b1; built at 768b4d1, smoke-tested in a fresh Linux
   venv) has not run in the bench images: one run there before merging. A codex arm can
   join a registered comparison now.
5. Unverified: providers against real APIs (scripted servers only, no keys on this PC);
   macOS (never run); real clipboards; bug 17 against a real refusal.

## Checks at the head of `devin/big` (last lines)

```
ruff check .                     All checks passed!
ruff format --check .            133 files already formatted
mypy src/karox6                  Success: no issues found in 68 source files
pytest, Windows (Python 3.12.10) 1166 passed, 7 skipped in 178.44s (0:02:58)
pytest, Linux (WSL, 3.12.14)     1168 passed, 6 skipped in 71.33s (0:01:11)
git diff --check                 no output, exit 0
python scripts/perf_check.py     start 136.5 ms (<= 300.0), idle RSS 31.3 MiB (<= 80.0): OK
dev37 wheel, fresh Linux venv    karox run --json, scripted model: completed, SMOKE PASS
```
