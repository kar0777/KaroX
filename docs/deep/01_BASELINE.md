# Frozen baseline (deep-refactor start)

**Recorded:** 2026-11-07
**Worktree:** `b/karox5-deep` (new isolated branch)
**Base commit:** `066a35134ff5f288413c8ff3d47068706fc8ae8a`
**Tree:** `72fe7e2e71b30d08dc710a90f8181e95e0b4aae4`
**Source before this commit:** `release/karox5-5.0.0` in `b/karox5-refactor`
**Verified wheel (pre-existing):** `karox_runtime-5.0.0-py3-none-any.whl`,
1,151,184 bytes, SHA-256
`d9867102f76d4a7f30fb2b3ad585ace9bcc76f10ee06ef02e3e0d197ae23396d`
(verified again at the start of this mission).

## Package facts at baseline

| Metric | Value |
|---|---|
| Product Python modules | 164 |
| Physical package lines (`check_tree`, `splitlines`) | 87,689 |
| Test files | 79 |
| Collected tests | see gate log below |
| `MAX_PACKAGE_LINES` | 88,000 (311 lines headroom) |
| Import cycles | 4 (see audit) |
| Console scripts | `karox`, `karox-vnext` |
| Package/version | `karox-runtime` 5.0.0 |

## Baseline gates (run from this worktree, isolated config/runtime)

| Gate | Result | Evidence |
|---|---|---|
| `python scripts/check_tree.py` | PASS — 164 modules · 87,689 lines | local-runs/deep-audit |
| `ruff check src tests scripts` | PASS | local log |
| `mypy cli.py entrypoint.py tui` | PASS — 13 files | local log |
| Full `pytest` | recorded in `docs/deep/CHECKPOINT.md` | `local-runs/baseline-full-pytest.log` |
| Wheel build/install | inherited verified 5.0.0 wheel | SHA above |

## Why this base

`release/karox5-5.0.0` is the newest commit that is a complete product tree and
has a green isolated gate plus a verified wheel (5.0.0, SHA-256 above). The newer
`b/win` commits are the goal documents themselves; the experimental
`b/autonomy-16-5a`, `b/mission17*` branches are laboratory experiments, not the
product baseline. The older `b/karox5-refactor-dev` HEAD is an ancestor of the
base. `b/integrate` is the separate v6 line and is not this product.
