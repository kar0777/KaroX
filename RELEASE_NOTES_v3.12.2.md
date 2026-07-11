# ★ KaroX v3.12.2 — Legacy Launcher Migration Hotfix

KaroX 3.12.2 completes the Windows PowerShell encoding fix for users who already generated a broken launcher with v3.12.0.

## Fixed

- Detects cached PowerShell launchers that contain valid UTF-8 text but are missing the UTF-8 BOM.
- Rewrites those legacy files automatically instead of incorrectly treating them as reusable.
- Preserves normal caching once the launcher has the correct encoding.
- Adds a regression test that recreates the exact v3.12.0 cache state, runs the new patcher, and verifies that the BOM is restored.

## Upgrade on Windows

Run the normal installer again:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1 | iex"
```

No manual deletion of `start.notion.generated.ps1` is required. Existing configuration and session history are preserved.
