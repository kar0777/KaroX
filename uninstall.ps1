$ErrorActionPreference = "Continue"
try { chcp.com 65001 > $null; [Console]::InputEncoding = [Text.Encoding]::UTF8; [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$Desktop = [Environment]::GetFolderPath("Desktop")
$ConfigDir = Join-Path $env:APPDATA "KaroX"
$RuntimeDir = Join-Path $env:LOCALAPPDATA "KaroX"
$LegacyConfigDir = Join-Path $env:APPDATA "RepoPilotBridge"
$LegacyRuntimeDir = Join-Path $env:LOCALAPPDATA "RepoPilotBridge"
$BinDir = Join-Path $RuntimeDir "bin"

Write-Host "Uninstall KaroX" -ForegroundColor Yellow
$answer = Read-Host "Remove local settings, sessions and runtime files? [y/N]"
if ($answer -notmatch "^[YyДд]") { exit 0 }

foreach ($path in @(
    (Join-Path $Desktop "KaroX.bat"),
    (Join-Path $Desktop "Star For KaroX.bat"),
    $ConfigDir, $RuntimeDir, $LegacyConfigDir, $LegacyRuntimeDir
)) {
    Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue
}
$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath) {
    $items = @($userPath -split ";" | Where-Object { $_ -and ($_ -ne $BinDir) -and ($_ -notlike "*RepoPilotBridge*\bin") })
    [Environment]::SetEnvironmentVariable("Path", ($items -join ";"), "User")
}
Write-Host "KaroX local files were removed. Open a new PowerShell window." -ForegroundColor Green
