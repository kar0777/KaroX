# Holdout protocol (pre-registration)

The DEV sets are for engineering. A holdout is the only set that may support a
competitive claim, and only after a candidate is frozen. This document is the
pre-registration contract; it is intentionally short and strict.

## Construction

- At least 30 tasks, covering backend (small and large context), frontend,
  debugging, multi-file features, browser workflows, Three.js/WebGL/3D,
  TypeScript/Node, security, migration and MCP.
- Authored separately from the DEV tasks and never used for prompt tuning. The
  task author does not run any arm on the holdout before freeze.
- Each task has `task.json`, `fixture/`, an independently authored `grader/`,
  `reference/` and `validate.py`; every task must be RED→GREEN before freeze.
- Prompts are self-contained, include the exact observable contract and end
  with the standard autonomy line. No hidden acceptance phrases.
- Fixtures contain no caches, `.git`, credentials or network dependencies.

## Freeze

- Record `task_json_sha256`, fixture/grader digests and `rubric_hash` for every
  task in `REGISTRY.json`, then record the registry SHA-256 in
  `docs/deep/CHECKPOINT.md` and the holdout report.
- The frozen registry and task files are read-only for the rest of the
  comparison. No prompt, grader or fixture edits after the first arm runs.
- Candidate Karo commit, wheel hash, model route and budgets are frozen in the
  same record before any holdout arm starts.

## Opening

- One opening per candidate. All available arms run the same tasks with the
  same budgets and isolated workspaces.
- Missing, timed-out, invalid-harness and refused rows stay visible; they are
  never converted to passes. A harness fix made after opening invalidates that
  arm's opening and must be declared.
- If a grader or task is found to be objectively wrong (not merely
  inconvenient), the holdout is invalidated and re-frozen; the old results stay
  in the record and cannot be cited.
- No cherry-picking of tasks, retries of failed tasks with new prompts, or
  post-hoc rubric changes.

## Reporting

- Report solved/total, timeout count, median wall time, cost where the arm
  reports it, and the exact model/provider per arm.
- Distinguish model quality from environment/harness failures. Do not claim
  "Karo leads" on a DEV set, a partial matrix or a single model pairing.
- A claim of category leadership requires the frozen holdout, all intended
  arms, and a clear margin that is not explained by timeouts or missing rows.

## Current status

No holdout has been opened. All results so far are DEV-only.
