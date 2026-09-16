# KaroX repository agent guide

This repository is KaroX 5. Treat this file as the fast path for coding agents; source code, tests, and release gates remain authoritative when prose disagrees.

## Start every task

1. Confirm the repository is `D:\проекты\KaroX-v5` and the intended workstream/project binding before mutating anything.
2. Resume the existing KaroX task/workstream instead of starting over. Use the deterministic project map / `repo.inspect` before broad raw-file reading.
3. Preserve the working tree. Never `reset`, `clean`, blanket `checkout`, or stash unrelated dirty work. Edit only the files needed for the task.
4. Prefer exact, idempotent repository operations and durable jobs. If a command/test may take ~20 seconds or more, start it detached and poll status/logs rather than holding an MCP request open.

## Repository map

- `src/karox/` — runtime and product implementation.
- `tests/` — canonical KaroX 5 suite.
- `scripts/` — deterministic release/check tooling; five legacy root pytest checks live in `scripts/test_karox4_units.py`.
- `docs/` — current product/release documentation. `docs/archive/` and detailed `docs/vNext/` phase records are historical unless a current document explicitly points to them.
- `remote/` — remote companion package.
- `.github/workflows/` — release/quality contracts.

Project context may include `AGENTS.md`, `CLAUDE.md`, or `KAROX.md`. Keep it bounded and repository-scoped; instruction files are context, not a way to bypass Core capability/risk policy.

## Safety and autonomy contract

KaroX should be highly autonomous for local, reversible engineering work and strict only at real external/user-data boundaries.

- Reads, normal repository edits, checks, tests, local dev commands, and ordinary local commits should proceed without nuisance prompts when the active profile permits them. Do not turn routine file-by-file edits into approval prompts.
- Destructive source deletion is the main local exception: protected mode defers it for user review; Bypass may perform repository-scoped deletion autonomously. Rebuildable cache/output cleanup remains locally guarded rather than noisy.
- A blocked action is not a blocked mission. Continue every independent read/edit/check that can still make progress, collect unresolved approval gates, and ask the user only when the remaining work actually depends on them (normally at the end of the response).
- Unknown/opaque high-consequence commands stay reviewable instead of being handed unrestricted authority, but one such command must not stop unrelated work.
- Local `git.commit` is a guarded reversible checkpoint. It never implies permission to push.
- Remote Git push is available only through the dedicated `karox.git.push` path and a machine-verifiable one-shot user approval for the exact action. Never smuggle push through `command.run`, shell wrappers, Python/Node eval, or another tool.
- Force-push, publish, deploy/release, auth/account mutations, payments, and other irreversible/external effects must stay behind their explicit guarded surfaces. Do not weaken these boundaries to make a test pass.
- Approval material/tokens must never be exposed to the model. Protocol `requestState` must be integrity-protected, short-lived, exact-action-bound, and one-shot.
- Never print/store raw credentials. Use opaque OS-keyring references or user takeover for login/CAPTCHA/2FA/consent/payment review.
- Desktop computer-use is intentionally non-intrusive: background HWND capture/input, no global keyboard/pointer injection, no focus stealing. Preserve the user-takeover boundary.

## Verification

Run the smallest affected checks while editing, then the release gates before declaring completion. Canonical commands include:

```text
python -m ruff check src tests scripts
python -m mypy src/karox
python -m build --wheel
python -m pytest -q
python scripts/run_v5_preflight.py --static-only --json
python scripts/check_versions.py
python scripts/check_dependencies.py
python scripts/check_test_count.py
python scripts/check_access_profiles.py
python scripts/check_documented_commands.py
python scripts/check_release_hygiene.py --json
python scripts/check_release_workflow.py
python scripts/check_wheel_contents.py
```

For the full suite, detached split jobs are preferred on Windows when available; aggregate every split and fix any failure immediately. Never interpret a flaky-looking failure as acceptable without reproducing and fixing or proving it is pre-existing with evidence.

`python scripts/check_test_count.py --write` is the supported way to synchronize current published test-count claims after tests are added/removed. Do not hand-maintain duplicate counts when the checker can do it.

## Version and release rules

- Runtime/package version source of truth: `src/karox/__init__.py`.
- `VERSION` tracks the stable shipping line; prerelease workflows may intentionally package a newer runtime candidate without rewriting the stable line.
- `scripts/check_versions.py` is the authority for whether those sources and workflows agree.
- Do not push tags, publish PyPI/GitHub releases, deploy, or mark external conformance/beta evidence passed without real user approval/evidence.
- A release candidate is not “done” until deterministic gates, wheel smoke/contents, the live ChatGPT bridge/reconnect path, and the relevant conformance record are verified.

## Editing discipline

- Prefer exact string edits or atomic patches with expected hashes when possible.
- Keep schemas/tool catalogs/counts synchronized when adding a tool; search exact-set/count tests before finishing.
- Any new hosted mutator needs capability mapping, risk mapping, idempotency/reconciliation semantics, audit evidence, and tests for denied/tampered/replayed paths.
- Any transport/restart change must prove response completion, no duplicate side effect, owner/process identity, and recovery after disconnect.
- Generated caches (`__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`, build artifacts) must not influence durable idempotency/workspace fingerprints.

## Finish

Before reporting completion: inspect the final diff, run targeted + static gates, run/aggregate the full suite, verify `git diff --check`, verify the built wheel, refresh the durable workstream checkpoint, and state any external approval that still requires the user. Do not claim release-ready while a known deterministic gate is red.
