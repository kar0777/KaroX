# KaroX v3.13.2

Tailscale readiness and persistent Notion setup reliability hotfix.

## Fixed

- `karox notion setup` now waits up to 120 seconds after Tailscale login instead of checking only once;
- transitional states such as `NeedsLogin`, `Starting`, and `Running` are reported clearly;
- KaroX can derive the stable device hostname from `HostName + MagicDNSSuffix` when `Self.DNSName` has not appeared yet;
- the Tailscale authentication URL and backend state are included in actionable errors;
- Windows, macOS, and Linux entrypoints use the same readiness probe;
- stale release metadata caches older than the installed KaroX version are removed automatically on Windows;
- the persistent Notion Bearer key is preserved during upgrade.

## Upgrade

Run the normal stable installer again:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1 | iex"
```

Then run:

```powershell
karox notion setup
```

Finish Tailscale sign-in if prompted. KaroX will wait for the stable `*.ts.net` hostname and print the one-time Notion MCP URL and token.
