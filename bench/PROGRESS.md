
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
