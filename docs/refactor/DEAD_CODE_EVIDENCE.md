# Dead-code evidence — Phase B audit log

**No product deletion has been performed at this checkpoint.**

## Method

1. AST import graph over all 162 product modules (including relative imports).
2. Dynamic-entrypoint search: `importlib`, `__import__`, subprocess `-m`,
   CLI/parser actions, bridge subprocess argv, tests and docs.
3. Resource reachability: prompts, builtin skills, provider catalog, TUI theme,
   engine license, package-data declarations.
4. External contracts: console scripts, public `karox.*` imports, hosted tool
   aliases, persisted JSON keys, keyring names and installers.
5. Test-reference map and black-box test replay.
6. Independent auditor review before any high-risk deletion.

## Status vocabulary

- `PROVEN_DEAD`: no supported entry, loader, package contract or external
  consumer requires it; evidence recorded.
- `DUPLICATE_WITH_PARITY`: another source of truth exists and a regression test
  proves replacement.
- `DYNAMIC_OR_EXTERNAL`: reachable through reflection, subprocess, wire, saved
  state or public import; preserve.
- `UNCERTAIN`: evidence insufficient; preserve and investigate.
- `SUPPORTED`: real feature; preserve.

## Initial candidates to investigate (not deletion decisions)

| Candidate | Why it is suspicious | Current status | Required evidence |
|---|---|---|---|
| `disk_maintenance.py` | maintenance mode is documented as not exposed; file may still back protected-workspace/size tools | UNCERTAIN | search core/tools/CLI and tests; black-box tool list |
| `desktop_apps.py` | migration note calls it a refusal shim, architecture says desktop is present | UNCERTAIN | import closure, engine tool registration, Windows tests |
| `browser_bootstrap.py` | may only gate rendered-page checks | UNCERTAIN | engine/web_check imports |
| `engine_salvage.py` | prose-to-command recovery may be used only by engine | UNCERTAIN | engine import closure and tests |
| `mode_artifacts.py` | Plan/Ideate may be active through CLI | UNCERTAIN | CLI run path and session JSON |
| `task_state.py`, `ellipsis_checkpoint.py` | hosted Smart Stop / old plan state overlap | UNCERTAIN | bridge imports, persisted keys |
| `project_registry.py`, `repository_lease.py` | bridge lifecycle helpers may be unreachable | UNCERTAIN | bridge launcher/proxy imports |
| `browser_*` modules | several old names are banned but new package exists | SUPPORTED | import graph and browser tests |
| legacy tests/docs | tests are release contracts, not package code | SUPPORTED | never delete to reduce count |
| `scripts/race_task.py` | benchmark harness, separate from product | DYNAMIC_OR_EXTERNAL | installer/wheel/package exclusion |

No candidate above is removed by this document.

## Reachability evidence to collect

- Full internal import SCCs and roots from `karox.entrypoint`, CLI parser,
  TUI, bridge `-m`, computer-use `-m`, tests and resource loaders.
- Every string containing a `karox.` module name or tool name.
- Every persisted key that a loader reads (`providers.json`, `mcp-servers.json`,
  `ui.json`, `web-bridge-profiles.json`, session fields, OAuth).
- Every wheel resource and every file opened by import-time code.
- Import closure of the installed wheel, not only the source checkout.

## Deletion record template

```text
path:
before/after LOC:
fan-in / fan-out:
contracts touched:
evidence of deadness:
replacement/parity test:
affected tests:
full gate:
commit:
restore command:
```

Deletion starts only after the baseline full gate and the independent audit are
recorded, and proceeds in reversible functional batches.
