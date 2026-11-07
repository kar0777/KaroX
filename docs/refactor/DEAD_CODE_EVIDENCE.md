# Dead-code evidence — Phase B audit log

## Baseline

- 162 product modules, 87,508 physical lines (`check_tree`), 0 import cycles in
  the package-level AST audit.
- Vulture found only two unused exception variables; the package already has a
  deliberately pruned legacy surface.
- Static reachability and independent audits found several **latent/dead**
  helpers, but also false positives caused by dynamic worker imports, lazy
  package exports and external import compatibility.

**No large file was deleted merely because one AST scan could not reach it.**

## Removed in first candidate batch

| Path | Evidence | Change | Gate |
|---|---|---|---|
| `src/karox/web_check.py::_screenshot_in_browser` | zero references in src/tests; older duplicate screenshot path | removed (~16 lines) | `test_web_check.py`, full browser/resource subset PASS |
| `src/karox/hot_worker.py::HotWorkerHandle.execute_browser` | zero callers; it called `workspace_worker.execute_browser_command`, which does not exist | removed (~12 lines) | `test_browser_core.py`, resource subset PASS |

These are real dead/broken helpers, not a removal of the live browser command
path. The live path is `engine_tools.register_browser_tool` →
`browser.tools.BrowserToolRuntime`.

## Kept and why

| Candidate | Evidence | Decision |
|---|---|---|
| `src/karox/desktop_apps.py` | no production caller; hosted allowlist excludes `app.*`; only `browser.tools.execute_browser_command` (itself unreferenced) and tests exercise it | **QUARANTINE**. Could be an external/v5 import contract; owner confirmation required before deletion. |
| `browser.tools.execute_browser_command` | tests only; docstring claims replacement for a nonexistent workspace-worker function | **QUARANTINE**. Keep until compatibility decision. |
| `src/karox/ellipsis_checkpoint.py` (379 lines) | zero importers; docs/MIGRATION claims it backs hosted Smart Stop, but no runtime path uses it | **DEAD/UNCERTAIN**. The claim and the missing F1 wiring must be resolved together; do not delete blindly. |
| `src/karox/verification.py::VerificationResult/build_result` and `AutopilotPlan/plan_autopilot` | no production/test caller; live verification is `completion_gate.floor` + `session_analysis.evidence_chain` | **DEAD/UNCERTAIN**. Removing would reduce a competing verification API, but first prove no external import. |
| `DesktopAppSecurityError` | defined/exported, never raised | minor dead type; kept for compatibility |
| `src/karox/browser/__init__.py` lazy exports | new `__getattr__` dynamic surface; tested | **MUST NOT REMOVE** |
| `workspace_worker.py` | imported dynamically by `hot_worker` | **MUST NOT REMOVE** |
| `browser/session.py`, `computer_use/`, skills, prompts/data | live typed surfaces | **MUST NOT REMOVE** |

## LOC accounting (candidate, not final)

| Metric | Baseline c4 | Candidate f8b641b |
|---|---:|---:|
| Product modules | 162 | 164 |
| Physical package lines | 87,508 | 87,689 |
| Test files | 77 | 78 |
| Collected tests | 1,527 | 1,540 executed in the isolated gate (70 files) |
| Removed dead helpers | — | 28 lines |
| New compatibility tests | — | lazy facade, multi-project, legacy keyring, JSON export, browser timeout, CLI aliases |
| Net change | — | +124 lines (split/import overhead; no net deletion campaign yet) |

The current 405-line headroom under the 88,000 gate is a real constraint. Future
splits should be net-negative or justified by a policy change recorded in both
`scripts/check_tree.py` and `tests/test_tree.py`.

## Do-not-delete proof rules

1. Try dynamic entrypoints (`hot_worker`, `python -m`, lazy facades, bridge
   subprocess argv, tests that import a module directly) before deletion.
2. Check saved JSON/field names and public import paths, not only source imports.
3. A type/helper with no production caller can still be a compatibility surface;
   uncertainty means keep.
4. After removal run the full affected suite, `ruff`, `mypy`, `check_tree`,
   wheel build and fresh install before the next batch.




