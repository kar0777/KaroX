# Devin report (KaroX `devin/big`, bench `devin/bench`)

Status: short shift of 2026-11-22, started 04:20:33 UTC (`date -u`), report closed 06:25 UTC.
Source version 6.0.0.dev39; ReformBoss was not used. Every run's rule, written before it,
and its result are in `PROGRESS.md`; the night's full report is this file at d4e2d5c.

## Where the comparison stands (night of 2026-11-21/22: one model, ReformBoss, 1 attempt)

| 17 valid of 18 tasks | solved | ok calls | input M | output k (reasoning) | agent min | cost-eq $ |
|---|---|---|---|---|---|---|
| karox6 (dev35 / dev36) | 10 | 957 | 70.2 | 748 (528) | 139.0 | 0.907 |
| opencode_v2 2.3.2 | 10 | 1080 | 86.8 | 768 (522) | 158.7 | 1.000 |
| deepseek_harness 0.2.2-rc.5 | 12 | 780 | 51.7 | 553 (374) | 111.7 | 0.698 |

One attempt does not separate the three (registered rule: a gap under 4 tasks is a tie).
KaroX is not ahead: level with OpenCode, two tasks, 30% of cost and 24% of time behind DSH.

## 1. Why DSH solves more and costs less (saved runs only, no model call)

Bench `scripts/analysis/dshcmp.py`, `scopewords.py` over devs1, devt1, devt2, devt2b. A run
is cut at its first and last edit of a source file: explore / work / tail. Tokens are the
gateway audit's (DSH's log cuts tool results at 8k characters). Mean per task, 17 tasks:

| | KaroX | DSH | ratio |
|---|---|---|---|
| model calls | 56.3 | 45.9 | 1.23 |
| output tokens (of them reasoning) | 44.0k (31.1k) | 32.5k (22.0k) | 1.35 (1.41) |
| first request, tokens | 3.9k | 6.9k | 0.56 |
| explore: steps, output tokens, request at the first edit | 21.6, 17.1k, 61k | 20.6, 15.3k, 56k | 1.05, 1.12, 1.10 |
| work: steps, output tokens | 21.4, 17.3k | 15.3, 11.4k | 1.40, 1.52 |
| tail: steps, output tokens | 13.3, 9.5k | 9.1, 5.8k | 1.47, 1.64 |
| after the first edit: test commands, inline probes | 8.7, 9.0 | 4.7, 3.9 | 1.9, 2.3 |
| trials that edit a test file; test lines per diff | 13 of 17; 52 | 6 of 17; 15 | 3.5 |
| reasoning quotes "smallest change"; "broaden" (trials) | 17 of 17; 9 of 17 | 0 of 18; 1 of 18 | |
| reasoning steps that argue about the scope of the fix | 11.5% (104 of 904) | 3.0% (24 of 792) | |
| the 4 tasks only DSH solved: source files edited; source lines in the diff | 1.2; 16 | 2.2; 29 | |

General causes (none is about a task):
1. Two prompt sentences set the scope of the fix, and the model says so: "Make the
   smallest general change" and "Do not broaden a contract because nearby code supports
   more" are quoted in 17 and 9 of 17 KaroX trials; DSH has no such rule (OpenCode argues
   about scope in 3.1% of steps, like DSH). In two of the four tasks only DSH solved,
   KaroX names the case the hidden tests check and drops it with the rule ("The task
   instruction says don't broaden contract. So only ..."); DSH names the same case and
   takes it ("adding ... is safe if not breaking other tests"). All four are near misses:
   1, 2, 3 and 5 hidden tests.
2. KaroX then checks its own narrow reading twice as hard. The start is the same (first
   edit at step 22.6 / 21.6, same request size); the extra calls come later: +6.1 steps
   in work, +4.2 in tail. Output +12% in explore, +52% in work, +64% in tail. Reasoning
   is 1.41x (1.23x the calls, 547 / 487 tokens per call): the scope argument comes back.
3. KaroX writes test files the task told it to leave alone ("Do not modify benchmark
   tests unless the task explicitly requires test changes"). BASE_PROMPT said "Add a few
   focused tests"; the model sees the conflict and settles it by the prompt ("adding tests
   is allowed/encouraged by instructions"), DSH's the other way ("not necessary; we can
   avoid"). On the ten short tasks this is most of the gap: work 12.9 / 8.3 steps.
4. DSH gives the model no method: two identity sentences and twelve paragraphs on tool
   use; its loop adds one notice (the same tool call 3, 5, 8 times). What differs is what
   KaroX's 520 words of method make the same model do, not prompt size (DSH's first
   request is the larger), context, tool limits (the same) or the ending.

## 2. What changed (BASE_PROMPT only: 6.0.0.dev38 = bc7fb40, dev39 = f328136)

- Scope (dev38): "Treat the task statement as an exact contract ... Do not broaden a
  contract ..." -> what the task states stays exact (names, signatures, shapes, accepted
  and rejected types) and "Its examples are samples: other forms of the same input and
  other paths to the same behaviour belong to the task unless it rules them out." "Make
  the smallest general change ..." -> "Fix the cause, not only the reported example, where
  every affected input passes through ... Leave behaviour the task does not concern".
- Checking (dev38): "Passing the task's example proves only that example. Then try a few
  inputs the task did not spell out (another form of the same input, a boundary, an input
  that must be rejected, another path to the same code) in one throwaway command, and fix
  what fails for the same cause."
- Tests (dev39): "Add a few focused tests ..." -> "Whether to add tests is the task's and
  the project's call; if you do, give new test functions distinctive names ...". dev38
  had "Unless the task says to leave tests alone, add ..."; the live trial below edited a
  test file all the same, so the order itself went.
General: no task, repository, library or tool is named (a test holds the prompt to that);
the wording comes from counts over 17 trials and the rival's prompt; one rule for every
task; two rules that never had an A/B go (D-021). Not S1 again (rejected: it sent the
agent looking for sibling code) and not the end-of-run audit or finish check (rejected):
this acts where the scope is chosen. 600 words; `tests/test_prompt.py`, 9 tests, 6 fail on
dev37's prompt. Unproven until the A/B; if its rule rejects it, revert both commits.

## 3. Replay and live check

- Replay (no model): dev38 (run 44986591414) and dev39 (run 44988919342) each PASS in 5 of
  5 bench images (Python 3.11, 3.12, 3.13): install in 3-7 s, 16 of 16 scripted turns ok,
  "completed" with its snapshot, and the prompt the model received is the wheel's (the
  check now prints its digest: dev37 dad10eb48ba6, dev38 e73cceec60d8, dev39 dc5dea2fdcb3).
- Live, Dahl GLM (`devl1d`, run 44986701735, dev38, plopp-614): PASS. 48 calls of 48
  answered, no retry, ended by its own choice after 48 steps and 16.5 min (and solved the
  task). No "smallest" or "broaden" in its reasoning; it edited a test file (see above).
- Live, FreeInference (`devl1f`, run 44986695557, dev38): the lane refused all 238 requests
  (503, "paused due to a security incident"); recorded, not retried. No live run of dev39.

## 4. The A/B and the 3-attempt comparison (ReformBoss, after 21:00 UTC; nothing run here)

A/B `devp`, registered before any run: karox6 = dev37 wheel, karox6b = dev39 wheel, side
by side in one job, 1 attempt, the 19 held-out night tasks whose trajectories were never
read. Rule: dev39 stays if solved >= dev37 + 2, P2P-regression trials <= dev37 + 1 and
calls per task <= 110%; or if solved is equal or +1 and cost-equivalent <= 90%; otherwise
revert. Requests: 56 per task and arm (the night's mean) -> ~2,130 (2,550 with a margin).
The daily allowance is 1,500 or 5,000: devp0 (~225) shows the lane answers, devp0 + devp1
stay inside 1,500 (~1,235), devp2 (~900) crosses it: run it the same evening only if no
refusal has named a daily limit (a trial cut by the limit is void and is run again).
Optional first step, 48 requests: probe `scope1` samples the model's next step at 12
recorded scope decisions under both prompts; if both decide alike, skip the A/B.

```bash
B="-R kar0777/karox-hard12-bench --ref devin/bench"; R="D:/проекты/karox6-bench-data/runs"
W="-f karox6_wheel=karox6-6.0.0.dev37-py3-none-any.whl -f karox6b_wheel=karox6-6.0.0.dev39-py3-none-any.whl"
AB="-f arms=karox6+karox6b -f attempts=1 -f upstream=reformboss -f parallel=1"
MSYS_NO_PATHCONV=1 gh workflow run diagnose.yml $B -f spec_probe=scope1 -f samples=2 -f probe_workers=2
python scripts/analysis/scope_probe_read.py probes/scope1 $R/scope1/spec-probe --show 200
MSYS_NO_PATHCONV=1 gh workflow run hard12.yml $B $AB $W -f tag=devp0 -f chunk=2 -f tasks=18,19
MSYS_NO_PATHCONV=1 gh workflow run hard12.yml $B $AB $W -f tag=devp1 -f chunk=5 -f tasks=20,22,23,24,31,34,36,41,44
MSYS_NO_PATHCONV=1 gh workflow run hard12.yml $B $AB $W -f tag=devp2 -f chunk=4 -f tasks=45,49,50,51,56,61,66,69
# after each: MSYS_NO_PATHCONV=1 gh run download <run id> -R kar0777/karox-hard12-bench -D $R/<tag>
python scripts/trio_report.py $R/devp0 $R/devp1 $R/devp2 --md ab.md       # solved, validity, calls, cost
python scripts/analysis/scopewords.py $R/devp0 $R/devp1 $R/devp2          # "smallest" / "broaden" per arm
python scripts/analysis/dshcmp.py $R/devp0 $R/devp1 $R/devp2 --arms karox6,karox6b   # test edits, phases
```

One run at a time on the lane (4 requests in flight per key). P2P regressions: each run's
`HARD12_RESULTS`. Tasks 51 and 61 ship an AGENTS.md of their own with "Make the smallest
change" (not the prompt's). dev38 against dev39 would isolate the order to add tests.

3-attempt comparison (trio; `<wheel>` = the A/B's winner). Never `attempts=3` here: one job
would hold 9 trials in flight on a key that allows 4. One attempt per dispatch, the next
(devx2, devx3) after it ends; read each with `trio_report.py` and add up:

```bash
MSYS_NO_PATHCONV=1 gh workflow run hard12.yml $B -f arms=trio -f attempts=1 -f upstream=reformboss -f parallel=1 \
  -f chunk=5 -f karox6_wheel=<wheel> -f tag=devx1 -f tasks=18,19,20,22,23,24,31,34,36,41,44,45,49,50,51,56,61,66,69
```

Requests: a trio needed 162 per h12 task last night: ~3,080 per attempt, ~9,240 for three
(two days at 5,000 a day; at 1,500 a day 8 tasks an evening, about seven evenings). The
held-out 19 keep it clean for dev39, whose prompt was shaped on the disc set. Cheaper,
valid for dev37 only: `-f taskset=disc -f tasks=1,2,...,12 -f chunk=4`, 1,575 per attempt.

## 5. Merge status

`origin/karox6/main` (ee24034, fetched 06:10 UTC) is an ancestor of `devin/big`: a
fast-forward of 46 commits, no conflict (`git merge-tree --write-tree origin/karox6/main
devin/big` exits 0 with the tree of `devin/big`). Nothing was merged or pushed there.
dev39 changes the default prompt before its A/B: merge after it, or revert both commits.

## 6. Not done, not verified

- Whether dev39 solves more or costs less on the bench model: no such run was possible.
- Found, not fixed (last night's chat start-up, Linux): Ctrl+C in the first 70 ms under
  the start-up frame, 1,260 starts: 25 exit -2, 5 lose the key, 1 traceback (cause and fix
  in PROGRESS.md, 06:30); its test failed in one of three full Linux runs (~1 in 30).
- An outage costs a bench trial 34 min, not KaroX's 600 s budget: the gateway retries too.
- The night's open items stand (real provider APIs, macOS, bug 17, the compaction A/B).

## Checks at the head of `devin/big` (code at f328136; last lines)

```
ruff check src tests             All checks passed!
ruff format --check src tests    119 files already formatted
mypy src/karox6                  Success: no issues found in 68 source files
pytest, Windows (Python 3.12.10) 1175 passed, 7 skipped in 191.88s (0:03:11)
pytest, Linux (WSL, 3.12.14)     1177 passed, 6 skipped in 71.83s (0:01:11)   (one of three runs: 1 failed, see 6)
git diff --check                 no output, exit 0
python -m build --wheel          Successfully built karox6-6.0.0.dev39-py3-none-any.whl
python scripts/perf_check.py     start 141.4 ms (<= 300.0), idle RSS 31.4 MiB (<= 80.0): OK
replay.yml, dev39, 5 images      REPLAY PASSED / KAROX6 PASSED in each
```
