# KaroX release hardening — 2026-09-23

Base commit: `222612e3979b1e270bfbe4b109ca693f072be075` on `main`.
This is a source-change verification record, not a stable-release certificate.
Runtime remains `5.0.0rc4`; stable shipping line remains `4.1.4`.
Pre-existing `tools/` and `KaroX_NEW_CHAT_COMPLETE_PACKET.zip` are preserved.

## Measured changes

- Equivalent reordered JSON read arguments: a deterministic two-result fixture
  shrank from 48,000 to 24,071 characters while retaining the latest complete
  result. Invalid/non-finite values are not canonicalized. These are local
  character measurements, not provider billing or tokenizer measurements.
- Tool discovery: only enabled when omitted schema bytes exceed the added
  discovery note, including its separator and JSON escaping. A measured fixture
  omitted 191 bytes and added 117 serialized bytes of prompt overhead, saving
  74 net bytes in the local payload estimate. This is not a tokenizer or bill.
  Usage distinguishes gross and net values.
- Overlapping repository excerpts: 36 adjacent matches previously spent their
  budget repeating 140 lines but exposed only 26 distinct lines; the new path
  reads the file once and exposes all 42 relevant context lines without repeats.
- Source ranking no longer rewards keywords in snapshot directory names more
  than relevant top-level modules. No snapshot/user files are deleted or hidden.
  Repeating the original inspection against the actual checkout put 11 current
  `src/karox` modules first, replacing the previously dominant copied trees.
- At 46 columns by 14 rows the command menu occupies rows 4–10, the composer
  occupies rows 11–13, and all commands remain reachable using Up/Down and Tab.

## Step 5 Preview and endpoint diagnosis

The user-authorized key was read from an ignored local attachment into memory;
no credential was installed, committed or included in this record.

Observed against the same key and `step-5-preview`:

| Endpoint | Result | Scope |
| --- | --- | --- |
| `https://api.stepfun.ai/v1/models` | HTTP 200 | Model discovery |
| `https://api.stepfun.ai/v1/chat/completions` | HTTP 402, `quota_exceeded` | Standard API allowance |
| `https://api.stepfun.com/v1/models` | HTTP 401 | Wrong account region |
| `https://api.stepfun.ai/step_plan/v1/models` | HTTP 200 | Subscription model discovery |
| `https://api.stepfun.ai/step_plan/v1/chat/completions` | HTTP 200, 33 SSE events, `stop`, answer `OK` | Minimal real model request |

StepFun documents the separate allowances and subscription endpoint in its
[Step Plan documentation](https://platform.stepfun.ai/docs/en/step-plan/overview).
The successful minimal request does **not** establish Core tool-loop conformance.
Automatic tool approval review refused the complete runner, including the
restricted read-only version, with `blocked by policy` and no detailed reason.
That action was not retried through another transport.

The opt-in runner is `scripts/run_stepfun_acceptance.py`. It requires explicit
endpoint selection, uses `step-5-preview` for both read-only workers, and asserts
actual file reads plus deterministic answer correctness. It runs only against
disposable fixtures. Source hashes before/after bind its output to the exact
working tree; a changing source tree cannot pass. Offline tests are not live proof.

Example after the execution gate is resolved:

```powershell
python scripts/run_stepfun_acceptance.py --credential-file "<local key file>" --endpoint https://api.stepfun.ai/step_plan/v1 --output .tmp/stepfun-acceptance.json
```

## Verification

- Independent adversarial review: PASS after fixing its confirmed findings.
- Static preflight, Ruff, full-package Mypy, wheel build, wheel contents,
  `git diff --check`: PASS (`release.json`).
- Fresh isolated virtual environment, declared dependency installation, package
  import, CLI version/help, quickstart and doctor: PASS (`release-6.log`).
- Built wheel: `karox_runtime-5.0.0rc4-py3-none-any.whl`, SHA-256
  `a413083ef21a11d19ce775adf89f9621506f7eac6262b164d9a9d2c00ccd78ad`.
- Complete pytest run with the runtime source frozen: PASS, 4,383 passed,
  57 skipped, plus 2,156 passing subtests; zero errors/failures, 545.273 seconds
  (`full-final.xml`, `full-final.json`). Interrupted/pre-freeze runs are not
  counted as completed verification. The two subsequent synchronization-only
  test fixes are covered by the separate runs below.
- Baseline GitHub [Product quality run 35769348913](https://github.com/kar0777/KaroX/actions/runs/35769348913)
  failed only on Windows Python 3.12 with two UI synchronization errors (4,364
  passed, 63 skipped). Those tests now wait for screen/save completion while
  retaining their assertions. The corrected local UI group passed five tests.
  In a fresh Python 3.12.10 environment with declared dependencies, both exact
  failing scenarios passed in each of three separate runs (6/6 executions,
  no skips; `ci312-0.xml`, `ci312-1.xml`, `ci312-2.xml`). This does not claim the
  old remote run became green; GitHub must verify the new commit separately.

Durable local artifacts are under `.tmp/release-20260923/` and are intentionally
not shipped. No supplied credential value occurs in the changed files.

## Stable-release prerequisites

`python scripts/check_v5_release.py --strict --json` currently rejects promotion.
Required live records remain incomplete for ChatGPT Web, Claude Web, OpenAI
Responses, Anthropic Messages, Gemini and generic OpenAI-compatible Core flow.
External beta still lacks the required 5 unaided installations, 3 primary
scenarios, 2 completions without a maintainer and 2 returning users. Local
automation cannot manufacture this evidence. Cross-platform install/upgrade
rehearsals also remain recorded as open in the release checklist.

No stable version bump, package publication, release tag or installed-runtime
replacement is claimed by this record.

## Second-pass verification — independent review of the same tree

Three separate reviews ran against this change set, each with its own scope and
none sharing the author's assumptions: an adversarial read of the `src/karox`
diff, an independent diagnosis of the two GitHub failures on the base commit,
and an audit of the strict release contract. Their confirmed findings were fixed
at cause and each fix carries a test that was shown to fail without it.

Corrections to the first pass, all reproduced locally:

- The two Windows Python 3.12 failures were not test-side timing. The first was
  a product defect: `KaroXApp.input_submitted` analysed Enter against the
  raw-paste burst before consulting the command palette, so a fast-typed
  `/connect` became a paste marker and the command never ran. Measured with the
  first-pass test changes in place: 3 failures in 12 runs on Python 3.12.10, the
  same interpreter family CI used. After the fix, 15 of 15 runs passed, and the
  new regression test fails deterministically (3 of 3) when the old condition is
  restored.
- The second was a Windows file-sharing race in `ProviderRegistry`, not a test
  ordering problem: `os.replace` swaps the file while readers open it, and Win32
  refuses both sides of that window. Measured raw: 396 of 400 swaps collided
  against continuously re-reading threads, and every read failure arrived as
  `errno.EACCES` with `winerror` unset - a spelling the first classifier
  revision would have missed. Reads and swaps now retry on bounded budgets that
  differ by where they run (event loop vs save worker).
- The new command-menu window was computed in content rows while its widget is
  `max-height: 14` laid out border-box, so it could ask for 14 paintable rows
  and place the highlighted command in the 2 that are clipped. Measured before
  the fix: `region.height=14`, content height 12, 14 rendered rows, the selection
  marker painted nowhere at 29 commands. Fixed and pinned at three window sizes.
- An HTTP 402 was newly classified as `BUDGET_EXCEEDED` on the status path only,
  which removed route fallback (a healthy second route was never tried) and
  disagreed with the streamed path's `PERMISSION`. Both paths now agree and an
  endpoint-reported 402 falls back; the local budget stop is unaffected because
  it is raised before any route is selected.
- The rewritten excerpt path could truncate the final excerpt below the match
  line that created it, spending context on a reason the reader never saw. Now
  such a window is dropped instead of emitted partially.
- `AgentKernel._action_signature` let a model-authored deeply nested document
  raise `RecursionError` out of `run()`; it now degrades to the raw spelling
  like the context compiler already did.

Findings reviewed and deliberately not changed: the duplicate-key JSON
equivalence introduced by canonicalization (defensible per RFC 8259), a
fractional path-token-hits value in a ranking reason string, a bracket group cut
by the discovery-note ceiling, and the canonicalizer logic duplicated between
`agent._action_signature` and `context_compiler` (both correct; unifying them is
a separate refactor).

The release-contract audit restated, with the checker's own messages, that the
strict gate needs six live conformance records and an external beta before a
final `5.0.0` may be published, and added two facts this record did not have: the
stable `release.yml` has never completed a single 5.x publication in its current
shape, and it publishes only to GitHub Releases plus `RELEASE.json` - there is no
automated path that puts a final version on PyPI.
