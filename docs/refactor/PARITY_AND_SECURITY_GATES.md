# Parity and security gates

**Status:** candidate, not release-ready. This file replaces the earlier
aspirational matrix with executed evidence and explicit NOT TESTED rows.

## Contract gates

| Gate | Result | Evidence |
|---|---|---|
| CLI parser/help/version | PASS | `test_cli.py`, targeted parser tests, `karox --version` |
| Agent run event contract | PASS | `test_engine_kernel.py::test_engine_run_emits_the_stream_event_contract`; new per-step usage assertion |
| Hosted multi-project runtime | PASS (new regression) | `tests/test_bridge_core.py::test_a_non_anchor_project_builds_its_own_runtime` |
| Saved bridge `--tunnel auto` | PASS (rejects at parser) | `test_bridge_saved_create_rejects_auto_tunnel_before_writing` |
| Plural aliases with `--json` | PASS | `test_plural_aliases_accept_json` |
| Legacy saved credential lookup | PASS (mock keyring) | `test_cli_saved_bridge_resolves_a_legacy_approval_credential` |
| JSON session export >500 entries | PASS | `test_json_export_keeps_the_whole_trajectory_beyond_500_entries` |
| Lazy public package imports | PASS | `tests/test_public_package_facades.py` |
| Browser owner fail-closed timeout | PASS | `test_owner_marshalling_times_out_instead_of_hanging` |
| Browser teardown stops owner thread | PASS | `test_force_close_wakes_parked_tabs_first` |
| `ruff` | PASS | 164 modules/scoped tests |
| scoped `mypy` | PASS | cli/entrypoint/tui, 13 source files |
| `check_tree` | PASS | 164 modules, 87,632 lines < 88,000 |
| Full pytest | **NOT GREEN** | repeated browser/MCP/git test hangs on this host; a mixed-revision run reached 1510 passed/17 skipped, not a valid one-revision baseline; see CHECKPOINT |

## Behavior gates

| Scenario | Status | Evidence |
|---|---|---|
| Simple agent task with fake model/tool loop | PASS (offline) | engine tests |
| VERIFIED only with evidence | PASS (offline) | completion/engine tests |
| Session create/export/resume | PASS (offline) | sessions/export tests |
| Provider wire formats (fake transports) | PASS (offline) | provider/engine tests |
| MCP outbound replay/idempotency | PASS isolated; full-suite hang encountered | `test_mcp_cli` isolated pass |
| Hosted bridge local core | PASS (offline) | bridge core tests |
| Browser core/live local page | PASS file-isolated; full-suite order hang | `test_browser_core.py` 10/10, `test_browser_live.py` seen pass in earlier runs |
| TUI synthetic streaming | PASS | `test_tui.py` 95 tests |
| Desktop live | **NOT TESTED** | Windows fake tests only |
| Real W&B/model | **ONE SMOKE PASS** | W&B DeepSeek-V4.1-Flash, verified, 4 steps, 27,131 tokens; cost `no_pricing` |
| OpenCode/DSH comparison | **NOT RUN** | blocked by release readiness |
| Fresh wheel install | PASS on pre-split wheel; final pending | baseline wheel smoke |
| Upgrade/rollback from real 5.x | **NOT TESTED** | requires backup and a migration decision |

## Security gates

| Gate | Status | Evidence / limitation |
|---|---|---|
| No false VERIFIED | PASS (offline) | completion/engine tests |
| Capability + risk on mutating Core tools | PASS for Core path | policy/risk tests |
| MCP CLI call risk gate | **FIXED, HIGH-RISK REFUSAL NOT TESTED** | direct `mcp call` now passes `RiskEngine`; existing MCP CLI suite 11 PASS |
| checks/probes inline remote-action guard | **NOT TESTED / potential gap** | probes admit `python -c` text; command guard not called |
| Credentials redaction | PASS scoped | redaction/export tests |
| Credential storage outside repo | PASS static | keyring refs |
| No real key in benchmark guest | **N/A** | no benchmark run |
| Bridge OAuth/loopback | PASS in-process; live NOT TESTED | bridge tests |
| Browser late callbacks | FIXED | ignore teardown callbacks |
| Full OS sandbox / zero-egress | **NOT CLAIMED** | Mission 17.2 remains blocked for native AppContainer |

## Required before a READY claim

1. Full one-revision pytest PASS with deterministic browser/MCP/git behavior.
2. Rebuild wheel from final HEAD and fresh-install smoke.
3. Version/upgrade/rollback tests against a copy of real 5.x state.
4. Real model/browser/MCP/TUI smoke with honest cost.
5. Real benchmark matrix and results.
6. Resolve or explicitly quarantine the MCP risk gap, probe guard, dead
   desktop/verification APIs, and the TUI import-boundary documentation.
