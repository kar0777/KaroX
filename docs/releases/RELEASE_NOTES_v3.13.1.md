# KaroX v3.13.1

Windows PowerShell 5.1 compatibility hotfix for the persistent Notion connection release.

## Fixed

- `start.ps1` no longer contains a non-ASCII arrow in a BOM-less UTF-8 file;
- Windows PowerShell 5.1 no longer misreads that byte sequence as a smart quote and reports a missing string terminator;
- KaroX now starts normally after installing v3.13.x on Windows;
- the CI matrix parses every PowerShell entrypoint with the actual Windows PowerShell 5.1 engine;
- CI rejects a BOM-less `start.ps1` when it contains non-ASCII bytes, preventing this regression from returning.

## Upgrade

Run the normal stable installer again:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1 | iex"
```

The installer preserves the persistent Notion profile and encrypted Bearer key.
