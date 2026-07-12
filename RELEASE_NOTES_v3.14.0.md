# KaroX v3.14.0 — Native paths and reliable migration

This release completes the RepoPilotBridge → KaroX rebrand at the filesystem level.

## Fixed

- Windows installations now use `%LOCALAPPDATA%\KaroX` and `%APPDATA%\KaroX`.
- macOS/Linux installations now use `KaroX` configuration and runtime directories.
- Existing settings, repository history, session metadata, logs, and the DPAPI-protected persistent Notion key are migrated automatically.
- Stored absolute paths are rewritten to the new directories.
- Legacy directories are cleaned after the old processes have exited.
- The installer explicitly repairs and verifies `scripts/tailscale_readiness.py` instead of reporting success with an incomplete application.
- Installed PowerShell, Bash, and Python runtime files are rewritten to KaroX paths before the doctor runs.
- Admin, support bundle, Notion profile, generated launchers, shortcuts, and command shims all resolve the same canonical directories.

## Upgrade

Run the normal one-command installer. Do not use `-Clean`; automatic migration preserves the existing Notion credential and settings.

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1 | iex"
```

After upgrading:

```powershell
karox version
karox doctor
karox notion setup
```

The active paths should begin with `%LOCALAPPDATA%\KaroX` and `%APPDATA%\KaroX`.
