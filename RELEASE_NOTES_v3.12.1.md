# ★ KaroX v3.12.1 — Windows PowerShell Encoding Hotfix

KaroX 3.12.1 fixes a Windows-only startup failure introduced in v3.12.0.

## Fixed

- Generated `start.notion.generated.ps1` files are now written as UTF-8 with BOM, which Windows PowerShell 5.1 requires for scripts containing Cyrillic and Unicode interface symbols.
- Russian UI text no longer turns into mojibake such as `Р”/РЅ` or breaks the PowerShell parser.
- Cached generated launchers are read back with the same encoding used to write them.
- Provider tests now execute the real generator and verify the BOM, Cyrillic round-trip, shell encoding and absence of mojibake.
- CI now generates and parses the final launcher using Windows PowerShell 5.1, matching the environment used by the public Windows installer.
- Release-contract checks now derive archive and notes names from `VERSION` instead of hardcoding one release number.

## Upgrade on Windows

Run the normal installer again:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1 | iex"
```

The installer replaces the broken generated launcher automatically. Existing configuration and session history are preserved.
