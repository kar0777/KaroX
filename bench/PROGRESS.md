
## 2026-11-26: dev66 candidate (branch steps/dev66) from Devin SWE-2.1 failure analysis
- Devin SWE-2.1 Max (free) analysed each of the 10 dev-pool tasks Claude Code solved and KaroX failed (dev54-dev61
  runs; condensed traces in karox6-bench-data/work/devin): 8 incomplete fixes (missed writer/copy/branch, step
  order, interaction with another operation on the same state), 3 probe errors dismissed as "separate", 2 broken
  self-made .venv left for the verifier, 1 "minimal change ... finish" stall notice narrowing the fix.
  Gates already tried (finish_gate 0/100 verdicts changed, blind reviewer 1/10) were excluded from proposals.
- Synthesis: Codex gpt-6.2-sol and Devin; Devin's critique of the first draft caught an exemption that excused
  the target bug ("unless the original code fails the same way") -> removed. Prompt cap 600 -> 660 words.
- Timing analysis of 2 slow trials: 1556 s / 1800 s were 600 s waits on suites piped to `tail` ("(no new output)").
  dev66 adds advice-only notes for silent running commands (no caps; caps were not adopted twice).
- Wheel dev66 built, smoke OK, free replay green; screen pre-registered (bench/prereg_dev66_screen.md), not run
  yet: needs the user's approval for ~$3.9.

## 2026-11-26: pre-registered pool7 (bench/prereg_pool7_dev65.md) — dev65 native vs Claude Code, Haiku 5.5
- pool7 = 52 fresh tasks (ids 269-320, window 2026-10-01..10-20). Fairness: two blind Codex auditors (gpt-6.2-sol,
  gpt-6.1-sol; each 10/12 vs Neon on pool5 calibration) -> 27 FAIR / 9 PARTIAL / 14 UNFAIR / 2 DISPUTED, fixed
  before the run. SWE-2.1 Max could not finish an 11-task batch in 585 s (prints only at the end): replaced.
- Checks: free replay dev65 in 3 pool7 images (basics + code) green; paid preflight on id 141 all PASS ($0.012).
- Run 45799935776 (tag p7d65), 52 x 1 attempt, 104/104 trials with a verifier reward, telemetry all PASS.
- PRIMARY FAIR (27 valid): dev65 14 vs CC 15, cost/solve $0.0280 vs $0.0306 (0.91x) -> **FAIL** (needs > and
  <= 0.85x). Paired CI [-14.8, +7.4] pp: a tie within noise. Only 4 discordant tasks in all 52 (FAIR 294, 299
  dev65 only; FAIR 291 and PARTIAL 316 CC only). ALL 52: 17 vs 17, cost $0.740 vs $0.940 (dev65 21% cheaper).
  Wall time per trial: median 112 s vs 110 s, mean 309 s vs 202 s.
- Reading: on fresh tasks dev65 = Claude Code in solves and is ~20% cheaper; the pool6 win (22 vs 20) did not
  reproduce as "more solves". pool7 trajectories stay unread (holdout).
- Spend: run $1.849 (KaroX $0.740, CC $0.940, interview $0.168) + preflight $0.012. Evaluation:
  karox6-bench-data/scripts/eval_pool7.py, output runs/p7d65_eval.txt.

## 2026-11-24 night: H4 "auto-run existing tests of touched modules" — $0 gate fails
- scripts/regress_audit.py (bench-data): for dev61's 40 pool5 failures, graded tests NOT PASSED that are in the
  validated P2P set (passed on base AND after gold) = regressions an existing-test run could catch.
  11/40 failures break >= 1 P2P test; in 6 of them the agent already ran that test file (saw it, still failed).
  Unseen: 5 (iceberg-4317, tox-4452, conan-20530, otel-5579, dbt-1728); only 2 of those fail on P2P alone
  (iceberg 6/6, tox 2/2) — the rest also miss F2P. Ceiling ~2 flips (gate >= 5) -> NO-GO, no paid run.
  (An earlier name-match count "13/40 pre-existing" was wrong: it counted tests the PR modified.)

## 2026-11-24 night: council 19 — stop paid tuning on pool5
- Codex + Neon agree: solve headroom on pool5/Haiku is ~13-15 scattered inferable tasks; ship dev61, no more
  paid pilots on this pool; next solve measurement on a harder pool or another model. Both proposed one last
  $0 cost audit and one solve audit; all fail:
  - cost (scripts/cost_breakdown.py, output_cap_replay.py on dev61 pool5, $1.198): uncached 30% / cache 34% /
    output 37%; exact duplicate tool outputs 0%; capping outputs at 2k/4k/8k chars saves <= 9%/6%/3% (upper
    bound, ignores harm); cache-miss steps ~11%. Gates 8% (Codex, dedup) / 20% (Neon) -> NO-GO.
  - reproduction-before-first-edit: dev61 already reproduced a failure before its first edit in 19/40 failed vs
    7/31 solved trials (inverse correlation). Of the 15 inferable/explicit failures, 9 lacked a repro, but only
    cyclopts-1053 is a wrong-approach case a plain repro could catch (rest are missed siblings/edges)
    -> 1 << gate 5 -> NO-GO.
- No paid run tonight after the dev62 pilot. Spend today stays $10.83.

## 2026-11-25: fair pool + fresh-context reviewer ($0)
- Fair-pool audit (Neon, blind to outcomes; scripts/fair_bundles.py, work/fair/*.jsonl): each task FAIR /
  PARTIAL / UNFAIR by whether the F2P tests' specifics are obtainable from the issue + repository.
  pool5: 38 FAIR / 8 PARTIAL / 26 UNFAIR. Calibration on pool5 outcomes: FAIR solved dev61 28/38, dev56 22,
  CC 24; PARTIAL 1/8 each; UNFAIR dev61 2/26, CC 1/26 -> classifier is predictive. pool6: 26/12/18.
  Decision: comparisons from now on are scored on FAIR tasks (the rest measured luck/noise).
- H4 revisited on FAIR failures: most "broken P2P" tests were ADDED by the PR (pass on base, absent for the
  agent); truly pre-existing broken tests only in iceberg-4317 and a2a-1420 (agent saw and rewrote the test)
  -> still NO-GO. (The 11/40 figure above over-counted for the same reason.)
- PRE-REGISTERED (before results): blind fresh-context review of dev61's 38 FAIR final diffs (Neon proxy with
  repo access at base, issue + diff only). GO for a Haiku-reviewer replay only if it names the actual missed
  behaviour in >= 5/10 FAIR failures AND flags <= 4/28 solved diffs as MISSED.
- RESULT (Neon ran out of credits mid-run; same blind prompt given to Codex gpt-6.2-sol with repo access):
  names the missed behaviour in 1/10 FAIR failures (a2a-1476), flags 8/28 solved diffs as MISSED. Gate
  (>= 5/10, <= 4/28) FAILS -> NO-GO for a reviewer/critic pass. Even a strong fresh reviewer does not see the
  edge cases the maintainers' tests probe. On FAIR tasks dev61 is 28/38 (74%) vs CC 24/38 (63%).

## 2026-11-25: 6.0.0.dev63 — CLI bugs found by SWE-2.1 (free Devin model) review + dogfood
- Free Dahl GLM dogfood (run 45612552433, dev61, tasks 141/158/159): iceberg solved but 2262 s of 3550 s
  went to a silent full test suite (`| tail`) and 3 x 600 s job_output waits showing only "(no new output)".
- SWE-2.1 Max code reviews (core/tools, UI/CLI) -> verified and fixed with regression tests:
  Git Bash /d/x paths on Windows resolved to D:/d/x (writes into a parallel tree); PowerShell output
  decoded as UTF-8 while written in OEM (Cyrillic -> U+FFFD; now BOM-less UTF-8, param/using/#Requires
  untouched); background job reads split UTF-8 characters; run_code ignored the run deadline; MCP stdio
  `npx` failed on Windows (no PATHEXT; now resolved with the server's PATH); project-instructions budget
  went negative and kept ~whole second file (up to 1 MB per request). job_output on a running job now
  says "running for N s; M s left in this run". Two SWE-2.1 review rounds on the branch; remaining
  known limits: pwsh prologue after comments/[CmdletBinding()], foreground-timeout handoff can split a
  UTF-8 char once.
- Gates: full suite green on Windows (main venv), ruff, ruff format, mypy clean; wheel built and
  smoke_installed OK (karox 6.0.0.dev63, chat CLI, MCP stdio 7 tools). Bench behaviour change on Linux:
  only the job_output timing text and run_code deadline. Not pushed.
- SWE-2.1 harness audit of all 71 dev61 pool5 sessions (work/swe21/traj_a.out, traj_b.out), measured at $0:
  calls hitting a missing dev tool (ruff/uv/mypy/...) 67 of 1323 steps in 41/71 sessions (~5% steps);
  baseline re-runs (git stash / worktree / git show HEAD) 80 calls in 38 sessions, 10% of wall time.
  Both below the 20% cost gate -> not pursued as bench changes. Most other findings are bench-image
  gaps (missing dev deps, offline hatch/uv), not CLI bugs.

## 2026-11-25: pre-registered pool6 final (bench/prereg_final_pool6.md) — dev63 native vs Claude Code, Haiku 5.5
- Run 45669297097 (tag final63), 56 tasks x 1 attempt, all 112 trials with a verifier reward; telemetry
  checks 395 PASS (provider, version, per-step usage, cache hits on every KaroX trial). Two KaroX trials
  (227, 251) hit the 3430 s time limit (exit 3, status "limit"); counted as normal outcomes (251 solved).
- PRIMARY (26 FAIR, all 26 valid pairs): KaroX 22/26 vs CC 20/26; cost over FAIR $0.313 vs $0.410;
  cost/solve $0.0142 vs $0.0205 (ratio 0.69 <= 0.85) -> PASS. Paired diff 95% CI [0.0, +19.2] pp
  (lower bound touches 0: a win on the pre-registered rule, not a statistically separated one).
- SECONDARY: all 56 KaroX 24 vs CC 21 (CI [0.0, +12.5] pp), cost $0.836 vs $1.104; PARTIAL 1 vs 0;
  UNFAIR 1 vs 1. Mean agent time 380 s vs 281 s (the two 3430 s limits alone add ~122 s to KaroX's mean).
- Spend: run $2.11 (KaroX $0.836, CC $1.104, interview $0.173) + preflight $0.012. Evaluation:
  karox6-bench-data/scripts/eval_final63.py, output runs/final63_eval.txt.

## 2026-11-26 (night): 6.0.0.dev64 — CLI bugs from review + free dogfood
- Codex gpt-6.2-sol review of providers/compaction/review (work/night/rev_prov.out.md): 10 claims, 4 verified
  and fixed with tests: an empty reply stopped by a content filter was retried as "empty response" and then
  misreported (all 4 providers; the empty turn is now dropped and Anthropic never gets an empty message);
  pruning could cut results of the newest tool batch before the model read them; SSE keep-alive lines reset
  the stall timer (stuck answer behind pings hung forever); native transport kept reading after [DONE].
  Not fixed: retry deltas shown twice in the TUI (a retry line separates them), uncounted usage of empty
  replies (2 retries in 5761 bench calls), --review guard races (opt-in, NO-GO feature).
- Free Dahl GLM dogfood dog63 (run 45685383324, pool4 FAIR 122/124/128/130/135/137): 4/6 solved.
  a2a-1456 died on "invalid JSON from server" at the same char on every retry: httpx aiter_lines (and
  splitlines in the MCP client) also split at U+2028/U+2029/U+0085/VT/FF, which JSON may carry unescaped.
  SSE lines now end only at CR/LF/CRLF (tests fail on the old code). Other waste: model-chosen full
  pydicom suites > 20 min (not a harness bug).
- Speed check (dev61 pool5): calls >= 300 s are ~45% of agent time but in ~10% of trials, which solve
  as often as the rest (~50% vs 36%); on pool5 KaroX is faster than CC (279 s vs 406 s). Not pursued.
- Fairness audit of tasks 13-73 (pool0): SWE-2.1 Max is lenient (8/12 vs Neon on blind calibration,
  all misses toward FAIR, 27 empty details); Codex gpt-6.2-sol 11/12. FAIR only when both agree: 28 FAIR,
  15 PARTIAL, 17 UNFAIR, 1 disputed. Total FAIR ids now 124 (pool0 is an old dev pool, not a holdout).
- Gates: full suite green (Windows), ruff, ruff format, mypy clean; wheel dev64 built, smoke_installed OK.
- Targeted Codex reviews after dev64 (work/night/rev_unicode.out.md, rev_parallel.out.md). Fixed (each with a
  test failing on the old code): rg grep dropped matches on lines with U+2028/U+0085; run_code nested calls
  over 64 KiB (asyncio line limit) killed tools.write of a 70 KB file; profile names like "работа" saved as
  invalid TOML; apply_patch could not match the first line of a UTF-8 BOM file; karox_diff returned C-quoted
  non-ASCII untracked names; @file cap cut a UTF-8 character. Known, not fixed (rare or no outcome effect):
  edit on mixed-EOL files when old_string spans a CRLF line normalizes the file; UTF-16 grep context;
  /undo vs a concurrent editor save; apply_patch marking a stale file as observed; POSIX orphan of a
  detached background child on Ctrl+C; bash/run_code edits to files already dirty before the session
  missing from "files changed"; a write in the same batch after the time limit.
- Free Dahl GLM dogfood dog64 (run 45738740180, dev64, tasks 128 + pool0 FAIR 13/40/62): 1/4 solved (GLM
  noise); every run ended normally (no "invalid JSON" crash on a2a-1456 this time), 40 hit the time limit
  amid 429 retries. Dahl reports no prompt-cache hits (provider side). dev65 replay 6/6 green.

## 2026-11-26: aquarium-3d (user's visual task), dev65 vs Claude Code, Haiku 5.5, 2 attempts each
- Run 45759284569 (aqua65), $0.22 total (KaroX $0.077, CC $0.135, interview $0.007); telemetry PASS.
- KaroX 2/2 pages render (canvas, no console/page errors), ~204-280 s agent time, 4 steps: one wasted
  32k-token first reply (all thinking, cut at max_tokens), then one 40 KB write, one edit. Look: tank,
  glass, frame, lamp, gravel, stones, plants, 3 banded clownfish (trial 2 fish smaller and clustered).
- CC 0/2: both ended after ~620 s with "response exceeded the 128000 output token maximum", no file.
- Files for the user: D:/проекты/aquarium-dev65/ (html + screenshots per trial).
