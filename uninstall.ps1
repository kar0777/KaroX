$ErrorActionPreference = "Continue"
try {
    chcp.com 65001 > $null
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$DesktopBat = Join-Path ([Environment]::GetFolderPath("Desktop")) "Star For KaroX.bat"
$ConfigDir = Join-Path $env:APPDATA "RepoPilotBridge"
$RuntimeDir = Join-Path $env:LOCALAPPDATA "RepoPilotBridge"
$BinDir = Join-Path $RuntimeDir "bin"

Write-Host "Удаление Star For KaroX" -ForegroundColor Yellow
Write-Host "Будут удалены локальные настройки, runtime-файлы, команды karox/repopilot и ярлык на рабочем столе."
$answer = Read-Host "Продолжить? [д/Н]"
if ($answer -notmatch "^[ДдYy]") { exit }

Remove-Item -LiteralPath $DesktopBat -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $ConfigDir -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $RuntimeDir -Recurse -Force -ErrorAction SilentlyContinue

$userPath = [Environment]::GetEnvironmentVariable("Path", "User")
if ($userPath) {
    $items = @($userPath -split ";" | Where-Object { $_ -and ($_ -ne $BinDir) })
    [Environment]::SetEnvironmentVariable("Path", ($items -join ";"), "User")
}

Write-Host "Локальные файлы Star For KaroX удалены." -ForegroundColor Green
Write-Host "Откройте новое окно PowerShell, чтобы обновился PATH." -ForegroundColor Yellow
