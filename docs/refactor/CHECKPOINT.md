# Goal Mode checkpoint

**Updated:** 2026-11-07
**Goal file:** `b/win/docs/goals/KAROX_V5_STABLE_REFACTOR_GOAL.md`
**Product worktree:** `b/karox5-refactor`
**Branch:** `goal/karox5-refactor-20261107`
**Baseline commit:** `c4bc342382149c254f1a1ea08aeeeccd21c4e246`
**Candidate HEAD:** `b30d22c` (docs); code HEAD `f3bceb0`
**Dev branch:** `goal/karox5-refactor-dev-20261107` (latest isolated test fix)
**Report/audit worktree:** `b/win` (do not use for product tests)
**Other worktrees:** preserve untouched; do not reset/clean them.

## Status

Phase A baseline is frozen; Phase B/C first candidate is implemented. This is
**not** a release candidate: the deterministic full gate and the version/migration
work remain open.

## Baseline evidence

- 162 Python product modules, 87,508 physical lines, 77 test files, 38,105 test
  lines, 1,527 collected tests.
- `ruff` PASS; scoped `mypy` PASS; `check_tree` PASS.
- Headless audited subset: 64 PASS.
- Baseline full single-process pytest **did not complete**: clean-state run
  reached 333 passed + 4 skipped, then hung in browser teardown due to an
  unbounded owner `Future`; first failure was an environment override artifact,
  not product.
- Baseline wheel: `karox_runtime-6.0.0a3-py3-none-any.whl`, 1,147,638 bytes,
  SHA-256 `6a795a2da912935629ea0819e23999d248a1499ca75173fd0b2d49624519dd69`.
- Import cycles: 6 before refactor; 164 modules/4 cycles after first candidate.

## Candidate evidence

- `ruff` PASS; scoped `mypy` PASS; `check_tree` PASS: 164 modules / 87,666 lines
  (334-line headroom under the 88,000 gate).
- New wheel: `karox_runtime-6.0.0a3-py3-none-any.whl`, 1,150,836 bytes, SHA-256
  `2eed6bfb0a9ef9f815420b88284256c0a39ad8c9981e0b23bebe46946d6849f3`.
- Full **file-isolated** gate PASS: 70 test files, 1,525 passed / 15 skipped /
  0 failed / 0 hangs / 0 errors, each file with its own process and config/runtime.
- The earlier apparent hangs were prematurely killed slow browser/MCP sections; with a full time budget the single-process suite completed normally.
- Fresh install of the final wheel in `venv-candidate-final` PASS: version,
  isolated paths JSON and `agent run --help` all exit 0.
- Public package-facade, CLI alias, hosted multi-project, legacy keyring, JSON
  export, browser timeout, TUI usage and MCP risk tests are passing.
- One real provider smoke passed: W&B `DeepSeek-V4.1-Flash`, verified, 4
  steps, 27,131 tokens; cost unavailable/`no_pricing`, no invoice invented.
- One paired cell: baseline c4 **stopped on token budget** (34,113 tokens,
  unverified) on the same task where candidate was verified in 27,131 tokens.
- No representative benchmark matrix or OpenCode/DSH comparison has been run.

## Landed fixes

- Hosted non-anchor project runtime no longer calls nonexistent
  `_checkpoint_factory_for` or passes unsupported `checkpoint_factory`.
- Plural aliases (`providers/models/sessions/skills --json`) work.
- Saved-create parser rejects `--tunnel auto` before writing.
- Legacy bridge keyring identity is searched for saved OAuth credentials.
- JSON session export preserves more than 500 history entries.
- Per-step TUI usage is emitted as a delta; final usage replaces the counter.
- Direct MCP CLI mutations now use the consequence/RiskEngine path.
- Browser owner calls have a fail-closed timeout; force-close stops its owner
  thread; late navigation callbacks after teardown are ignored.
- Dead `web_check._screenshot_in_browser` and
  `hot_worker.execute_browser` removed.
- `repo-selfcheck` helper is explicitly declared in package data.
- Lazy package facades cut import time for browser/TUI/computer_use while
  preserving public imports.

## Still blocking READY

1. Version/scope/migration. PyPI shows only 5.0.0rc2/rc3; 5.0.0 and 5.1.0 are
   free. The target still reports 6.0.0a3 and its scope differs from old v5.
2. `git_baseline` is under schema 1; rc4 rejects the new field, so rollback
   needs a restore/migration plan.
3. Installers still default to `v6/plan-impl-20261104` and side-by-side
   `karox6`/dogfood, not a 5.x in-place upgrade.
4. Real browser/MCP/TUI smoke and the 30–100-task benchmark matrix are not run;
   only one real provider smoke exists.
5. Checks/probes command-guard coverage and the TUI pure-helper import boundary
   still need explicit decisions.

## Safe continuation

From `b/karox5-refactor` (never from `b/win` for product tests):

```text
$p = 'D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\karox5-refactor'
$py = 'D:\проекты\KaroX-v5\lab\kernel-duel-20261025\local-runs\venv-m16\Scripts\python.exe'
$env:PYTHONPATH = "$p\src"
$env:KAROX_CONFIG_DIR = "$env:TEMP\karox5-baseline-config"
$env:KAROX_RUNTIME_DIR = "$env:TEMP\karox5-baseline-runtime"
$env:KAROX_DISABLE_UPDATE = '1'
```

Do not push, publish, reset, clean, delete personal data, alter firewall/services,
or repair a bridge route without owner approval. Do not call the current wheel a
release candidate.

## Next checkpoint

1. Rebuild wheel at final HEAD, install in a fresh venv, run CLI/TUI/doctor/paths.
2. Resolve 5.x version, scope, installer upgrade, and session rollback.
3. Run real browser/MCP/TUI smoke with cost ledger.
4. Run the benchmark tasks and then decide READY/NOT_READY.



