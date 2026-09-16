# Archived planning documents

These documents were working plans, execution logs, and hand-off prompts written
while KaroX 5 was being built. They are kept because they record decisions and
the reasons behind them; they are **not** current guidance.

Every file below is superseded. The two documents that describe the product and
its release state today are:

- `docs/V5_RELEASE_SCOPE.md` -- the frozen product and release contract;
- `docs/RELEASE_CHECKLIST.md` -- the execution record and release decision.

| Archived file | Written | Superseded by |
| --- | --- | --- |
| `V5_FINAL_PRODUCT_PLAN.md` | 2026-08-20 | `V5_RELEASE_SCOPE.md` (scope amendment 2), `RELEASE_CHECKLIST.md` |
| `V5_PLUS_PLAN.md` | 2026-08 | Deferred beyond 5.0 by `V5_RELEASE_SCOPE.md`; marketplace/skills remain post-5.0 |
| `KAROX_V5_RECOVERY_PLAN_2026-08-01.md` | 2026-08-01 | Recovery completed; see `RELEASE_CHECKLIST.md` |
| `PREFLIGHT_OPTIMIZATION_PLAN.md` | 2026-08 | `scripts/run_v5_preflight.py` |
| `COMMIT_PLAN_2026-08-20.md` | 2026-08-20 | Commits landed on `feat/karox-v5-competitive-upgrade` |
| `V5_COMMIT_PLAN.md` | 2026-08 | Same |
| `V5_HANDOFF_PROMPT.md` | 2026-08-02 | `CONTRIBUTING.md`, `RELEASE_CHECKLIST.md` |
| `V5_MASTER_EXECUTION_STATE.md` | 2026-07 -- 2026-08-21 | `RELEASE_CHECKLIST.md`, `IMPLEMENTATION_STATUS.md`, `docs/evidence/` |

Older phase-by-phase records for the vNext design live in `docs/vNext/`, which
is likewise marked historical.

Nothing in this directory is read by a gate, a test, or the runtime. Do not
update these files; write a new record instead.
