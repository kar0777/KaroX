# KaroX v4.1.0 — Out-of-the-box launch + console redesign

Two upgrades in one release: KaroX now sets itself up on first start, and the whole CLI got a unified, prettier look.

## 1. Launch and work — no manual setup

- **Auto-deps on start.** The launcher runs a one-time "launch autopilot" (`scripts/karox_autodeps.py`) that installs the optional packages for browser screenshots and desktop capture (playwright, pillow, mss) plus headless Chromium. The result is cached per version, so later starts are instant. Opt out: `KAROX_AUTO_DEPS=0`.
- **Watchdog by default.** The local API server always runs under the supervisor (`scripts/karox_supervisor.py`): heartbeat every 5 s, full process-tree restart in under 3 s after two failed pings. No extra commands needed. Opt out: `KAROX_NO_WATCHDOG=1`.
- **Clean shutdown.** The supervisor exports the real server PID (`server.pid` in the session folder); stopping a session kills the whole tree — no orphaned uvicorn processes.
- **No secrets on the command line.** The supervisor takes the API key from the environment (`REPO_TOOLS_API_KEY` / `KAROX_API_KEY`).

## 2. Console redesign

- New shared UI kit `scripts/karox_ui.py`: star banner, sections, key-value rows, ✓/!/× status marks, automatic color detection (honors `NO_COLOR` / `KAROX_NO_COLOR`, safe when output is piped to files).
- `karox status`, `karox doctor`, `karox update --check`, and the update notice now share the same design.
- The PowerShell launcher intro and header show the installed version.

## Upgrade

```
karox update
```

That's it — on the next start the autopilot installs everything optional by itself.

## Notes

- The Windows launcher is fully wired; on Linux/macOS run `python scripts/karox_autodeps.py` and the supervisor manually for now.
- Security unchanged: `git push` and publish commands remain hard-blocked; all endpoints still require the API key; the audit log covers 100% of operations.
