# Goal Mode checkpoint

**Updated:** 2026-11-07
**Goal file:** `b/win/docs/goals/KAROX_V5_STABLE_REFACTOR_GOAL.md`
**Product worktree:** `b/karox5-refactor`
**Branch:** `goal/karox5-refactor-20261107`
**Baseline commit:** `c4bc342382149c254f1a1ea08aeeeccd21c4e246`
**Report/audit worktree:** `b/win` (do not use for product tests)\n**First candidate merge:** `1420e6e` (dev fixes merged into refactor)
**Other worktrees:** preserve untouched; do not reset/clean them.

## Current status

Phase A — baseline freeze. No product deletion/refactor yet.

- Baseline inventory: `docs/refactor/BASELINE_AND_FEATURE_INVENTORY.md`.
- Parity matrix: `docs/refactor/PARITY_MATRIX.md`.
- Dead-code audit log: `docs/refactor/DEAD_CODE_EVIDENCE.md` (no deletions).
- Baseline AST graph: `local-runs/karox5-baseline/import_graph.json`.
- Baseline metadata: `local-runs/karox5-baseline/baseline_metadata.json`.

## Proven baseline

- 162 Python product modules, 87,508 physical lines.
- 77 test files, 38,105 physical lines, 1,527 collected tests.
- `ruff` PASS; scoped `mypy` PASS; `check_tree` PASS (162 modules / 87,508 lines).
- Headless audited subset: 64 PASS.
- Wheel: `karox_runtime-6.0.0a3-py3-none-any.whl`, 1,147,638 bytes, SHA-256
  `6a795a2da912935629ea0819e23999d248a1499ca75173fd0b2d49624519dd69`.
- Import graph: 0 internal cycles.
- Baseline test count differs from stale docs (1,527 vs 1,018).
- `README_RU.md` absent; docs contain historical 5/6 contradictions.

## In progress

- Correct-root full pytest first-failure run (background), log:
  `%TEMP%\karox5-baseline-pytest-vv2.log`.
- Fresh-venv wheel install and CLI smoke: **PASS** (version, paths, agent help, TUI help, doctor).
- Independent read-only audits: kernel/verification, CLI/TUI/MCP/bridge,
  browser/skills/package, release/migration.

## Safe continuation

From the product worktree:

```text
$p = 'D:\проекты\KaroX-v5\lab\kernel-duel-20261025\b\karox5-refactor'
$py = 'D:\проекты\KaroX-v5\lab\kernel-duel-20261025\local-runs\venv-m16\Scripts\python.exe'
$env:PYTHONPATH = "$p\src"
$env:KAROX_CONFIG_DIR = "$env:TEMP\karox5-baseline-config"
$env:KAROX_RUNTIME_DIR = "$env:TEMP\karox5-baseline-runtime"
$env:KAROX_DISABLE_UPDATE = '1'
```

Run pytest with `workdir=$p`, not from `b/win`. The previous full run from
`b/win` was discarded because pytest selected the wrong rootdir.

Do not delete a candidate until `DEAD_CODE_EVIDENCE.md` has source/test proof and
the independent auditor has tried to find a live consumer. Do not push, publish,
reset, clean, or alter another worktree.

## Next checkpoint

1. Record full baseline result, including first failure and final summary.
2. Fresh wheel install; `karox --version`, `agent run --help`, `doctor`, `paths`.
3. Merge auditor findings; update inventory/parity.
4. Freeze `PRODUCT_FEATURE_INVENTORY` + `PARITY_MATRIX` and make the first
   reversible deletion batch only if proven dead.
5. Choose and document the free stable 5.x version after PyPI/tag/migration
   evidence (current evidence: PyPI has only 5.0.0rc2/rc3; 5.0.0 and 5.1.0 are
   both free; no decision yet).


