# Dead-legacy evidence (deep refactor)

**Worktree:** `b/karox5-deep` · **Base:** `066a351` · **Recorded:** 2026-11-07

Status vocabulary is the goal's: `PROVEN_DEAD`, `REDUNDANT_WITH_PARITY`,
`PUBLIC_OR_DYNAMIC`, `UNCERTAIN`, `NECESSARY`. A symbol is only deleted with
reachability evidence across static imports, dynamic loaders, tests, docs,
CLI/hosted schemas, package data and Git history.

## Removed — batch 0

| Path / symbol | Lines | Evidence | Effect |
|---|---:|---|---|
| `verification.VerificationResult`, `build_result`, `_digest`, `_redact` | ~190 | zero source references outside their own definitions; zero test references; no docs/CLI/wire mention. The live verification decision is `completion_gate.floor` + `session_analysis.evidence_chain`; `verification.py` is imported only for `discover_verification_commands`. | dead second verification API removed |
| `verification.AutopilotPlan`, `plan_autopilot`, `LAYERS` | ~80 | same evidence; no caller, no test, no external documented contract. | removed |
| `verification.py` imports/machinery used only by the above (`dataclasses`, `hashlib`, `re`, `time`, typing extras) | ~10 | ruff/pyflakes verify no remaining use. | removed |

`discover_verification_commands` and its npm/python/page discovery rules are
kept and still covered by `tests/test_python_tests.py` and CLI/hosted tests.

## Reversed after adversarial review — `ellipsis_checkpoint.py` stays

The first pass deleted `src/karox/ellipsis_checkpoint.py` (379 lines) because it
had no static importer, no test, no dynamic loader and no CLI entrypoint.
An independent adversarial audit then found:

- Git history: `3aa154b` added a function-local dynamic import in
  `hosted_core._checkpoint_factory_for` that used `WorkspaceCheckpointStore`;
  `04bf7bb` removed that hook. The module is the only implementation of the
  documented on-demand rollback/Smart Stop capability.
- `docs/MIGRATION.md`, `docs/PARITY.md`, `core.py` and `hosted_core.py` still
  describe the rollback checkpoint, while `action_execution.py` now forces
  `reversible_by_checkpoint=False`.

Verdict: **`UNCERTAIN`/latent safety capability**, not `PROVEN_DEAD`. Deletion
was reverted; the module is restored byte-identical and stays unmodified. The
wiring gap is a separate tracked task (restore the hosted checkpoint hook or
formally retire the documented feature with owner-visible docs/CHANGELOG).

## Reversed after adversarial review — other candidates stay

| Candidate | Why it stays |
|---|---|
| `route_health.RouteHealthTracker`, `LocalListenerBusyTracker`, `public_mcp_route_failure_class` | **Lost wiring, not dead code.** The v5 supervisor used them; the current `hosted_core._bridge_status` still reads `public_route_healthy`, `public_route_failures`, `local_bridge_state`, `local_bridge_busy_seconds`, `child_recycle_history`, `tunnel_recoveries` from the watchdog record while `bridge_launcher` writes only `readiness`/`pid`/`heartbeat`. The bridge batch must decide one owner and either wire the health loop or remove the read-side claims with proof. |
| `requirements.delegated_source_context` | `SYSTEM_PROMPT` still tells the model that a delegated spec is prefetched, but no current path calls it. Lost wiring/prompt drift; keep and fix in the agent batch. |
| `effort.effort_summary/effort_user_summary/effort_display_name`, `agent_modes.mode_summary/mode_display_name` | Likely lost TUI presentation wiring (the v5 picker used descriptions); keep for parity until the TUI batch decides. |
| `command_jobs.run_job` | **Dynamic entrypoint**: `_RUNNER_SHIM` launches `from karox.command_jobs import run_job` in a detached `-c` child. Zero static refs is expected. |
| `workspace_worker`, `browser.tools`, `tui.app`, `desktop_apps`, `engine/*`, `computer_use.__main__` | Dynamic/lazy/package entrypoints; pinned by their loaders and tests. |
| `paths.legacy_config_dir` | rc4→5.0.0 migration path; cheap documented compatibility. |
| `desktop_apps.DesktopAppSecurityError` | Exported type; compatibility surface. |

## Method / reproduction

- Static graph and symbol-reference audit:
  `local-runs/deep-audit/audit.py` → `audit.json` / `audit.md`
  (AST imports incl. function-level, SCC cycles, fan-in/out, same-body hashes,
  dynamic `karox.*` string literals).
- Manual checks: `Select-String` over `src`, `tests`, `docs`, `scripts`,
  `pyproject.toml` for every removed name; Git history `git log -S`.
- Independent adversarial subagent reviewed both removals and found the hidden
  `_checkpoint_factory_for` history and lost-wiring candidates.
- Gate after the batch: see `docs/deep/CHECKPOINT.md`.

## Accounting

| Metric | Before batch 0 | After batch 0 |
|---|---:|---:|
| Product modules | 164 | 164 (module restored) |
| Physical package lines | 87,689 | 87,433 |
| Removed lines | — | 256 (`verification.py` 362 → 106) |
| `MAX_PACKAGE_LINES` headroom | 311 | 567 |

The ellipsis module is restored; the only net removal is the unused second
verification API. No product behavior is removed.
