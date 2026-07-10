$ErrorActionPreference = "Stop"
try {
    chcp.com 65001 > $null
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$Doctor = Join-Path $Root "doctor.ps1"

Write-Host "scripts\Test-RepoToolsDoctor.ps1 оставлен для совместимости." -ForegroundColor Yellow
Write-Host "Запускаю актуальную проверку RepoPilot Bridge doctor..." -ForegroundColor Cyan

powershell -ExecutionPolicy Bypass -File $Doctor -NoPause
exit $LASTEXITCODE
