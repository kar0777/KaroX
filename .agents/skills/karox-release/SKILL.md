---
name: karox-release
description: Release-hardening workflow for KaroX 5: preserve dirty work, use durable verification jobs, repair every deterministic failure, protect exact-action approval boundaries, and finish with reproducible release evidence.
version: 1.0.0
tags:
  - karox
  - release
  - verification
  - safety
compatible_project_types:
  - python
required_tools:
  - repo_read_file
  - repo_write_file
  - repo_list_files
  - checks_run
  - git_status
  - git_diff
validation_rules:
  - Never declare release-ready while a deterministic required gate is failing.
  - Never weaken push, publish, deploy, auth, payment, or user-data boundaries to make a test pass.
  - Preserve unrelated dirty work; never reset, clean, or blanket-checkout the repository.
---
# KaroX release hardening

Use this Skill when finishing a KaroX release candidate, repairing release gates, or validating ChatGPT Web integration.

## Operating rule

Treat every red deterministic gate as work to fix, not as an acceptable warning. Reproduce it narrowly, fix the root cause, run the narrow test again, then re-run the containing gate. Only classify a failure as external/pre-existing when there is concrete evidence and the required release contract explicitly allows it.

## Fast workflow

1. Confirm repository/workstream identity and inspect the dirty tree. Do not reset, clean, stash, or overwrite unrelated changes.
2. Read `AGENTS.md`, the current Project Map digest, and the smallest relevant code/tests. Prefer deterministic repository inspection over broad raw reads.
3. Make local/reversible engineering changes autonomously. Use exact/hash-checked edits or atomic patches.
4. For tests/builds that can exceed roughly 20 seconds, use durable detached jobs and poll status/logs. Do not keep the MCP request open for the lifetime of a subprocess.
5. Fix targeted failures immediately. After targeted tests are green, run static release gates and the full suite (parallel split jobs are preferred on Windows when safe).
6. Build the wheel, verify wheel contents, run `git diff --check`, inspect final status/diff, and checkpoint the workstream.

## ChatGPT Web approval contract

Local repository work and ordinary local commits should not create nuisance prompts when the selected profile grants them. A remote or irreversible effect is different:

- `git.commit` is local and reversible; it never implies push permission.
- Git push must use the dedicated guarded push tool and a real one-shot user approval for the exact action.
- Protocol approval state must be integrity-protected, short-lived, exact-argument-bound, and single-use. The internal confirmation/capability token must never be returned to the model.
- Decline, state tampering, argument changes, expiration, and replay after consumption must all fail closed without executing the side effect.
- Force-push, package/release publication, deploy, authentication/account changes, payments, and similar external effects remain behind their own explicit guarded boundaries. Do not emulate them with developer commands, shell wrappers, eval code, or another tool.

## Canonical gates

Use the repository's own release tooling rather than inventing substitute checks:

```text
python -m ruff check src tests scripts
python -m mypy src/karox
python scripts/check_dependencies.py
python scripts/check_versions.py
python scripts/check_test_count.py
python scripts/check_access_profiles.py
python scripts/check_documented_commands.py
python scripts/check_release_hygiene.py --json
python scripts/check_release_workflow.py
python scripts/run_v5_preflight.py --static-only --json
python -m build --wheel
python scripts/check_wheel_contents.py
```

Run the complete test suite after targeted checks. If test-count claims changed, use `python scripts/check_test_count.py --write`, then review the resulting documentation diff.

## Release evidence discipline

Do not mark live conformance or external beta records passed from code inspection. Real ChatGPT/Claude/provider/browser evidence must come from an actual external client run. A prerelease may keep those records pending if the prerelease workflow allows it, but the final stable release gate must remain honest.

When finished, report what is green, what exact live/external approval remains (if any), and the evidence/job/artifact identifiers needed to reproduce the result.
