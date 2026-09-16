# KaroX 5 full-preflight optimization plan

Status: **reviewed design, not applied in the active verification session**.

The current `scripts/run_v5_preflight.py --full` path executes overlapping test
work:

1. focused OAuth bridge tests;
2. focused web bridge launcher tests;
3. the complete unittest suite without coverage;
4. the same complete unittest suite again under coverage.

The focused modules therefore run three times, and every other unittest runs
twice. This lengthens the preflight without adding an independent assertion: a
failed unittest already fails the coverage process, and a successful coverage
process already proves that the complete suite ran successfully.

## Safe change after the active command is no longer protected

1. Keep `STATIC_STEPS` unchanged.
2. Keep `FOCUSED_STEPS` unchanged for the default focused mode.
3. In `_build_steps`, add `FOCUSED_STEPS` only when `args.full` is false.
4. Remove the plain `complete unittest suite` entry from `FULL_STEPS`.
5. Rename `coverage run` to `complete unittest suite under coverage` so the
   report is explicit about what provided the full-suite result.
6. Keep Ruff, Mypy, coverage combine, and coverage report unchanged.
7. Keep `--keep-going` behavior and the structured JSON result unchanged.

The resulting modes are:

- default: static contracts plus fast focused bridge tests;
- `--static-only`: static contracts only;
- `--full`: static contracts, Ruff, Mypy, one complete suite under coverage,
  coverage combine, and coverage report.

## Evidence-preservation requirements

The optimized full mode must still report separate results for:

- repository/static contracts;
- Ruff;
- Mypy;
- complete unittest suite under coverage;
- coverage combine;
- coverage report.

A non-zero coverage-run exit must be reported as a complete-suite failure, not
hidden as a coverage-only failure. The final JSON summary must continue to list
planned, run, failed, and not-run steps.

## Regression tests to add with the change

Without executing subprocesses, test `_build_steps` for all modes:

- default contains focused tests and no full coverage run;
- `--static-only` contains neither focused nor full steps;
- `--full` contains no focused tests, no plain duplicate unittest step, and
  exactly one complete suite invocation under coverage;
- `--full --keep-going` does not alter the selected step set;
- the coverage command still discovers `test_*.py` under `tests`.

## Why this file exists

During the current repair session, `scripts/run_v5_preflight.py` is itself an
active approved verification command. KaroX may block changes to that file to
prevent a running agent from weakening its own verifier. This plan records the
reviewed patch without bypassing that boundary. Apply it only in a later session
where the file is not protected, then run the complete preflight before claiming
the optimization is finished.
