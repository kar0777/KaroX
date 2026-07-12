param(
    [switch]$NoPause,
    [switch]$Json
)

$ErrorActionPreference = "Stop"
try {
    chcp.com 65001 > $null
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $env:LOCALAPPDATA "RepoPilotBridge"
$PythonExe = Join-Path $RuntimeDir ".venv\Scripts\python.exe"
$Doctor = Join-Path $Root "scripts\product_doctor.py"

if (!(Test-Path -LiteralPath $PythonExe)) {
    Write-Host "[FAIL] Runtime Python не найден: $PythonExe" -ForegroundColor Red
    if (!$NoPause) { Read-Host "Нажмите Enter для выхода" | Out-Null }
    exit 1
}
if (!(Test-Path -LiteralPath $Doctor)) {
    Write-Host "[FAIL] product_doctor.py не найден: $Doctor" -ForegroundColor Red
    if (!$NoPause) { Read-Host "Нажмите Enter для выхода" | Out-Null }
    exit 1
}

$doctorArgs = @($Doctor, "--root", $Root)
if ($Json) { $doctorArgs += "--json" }
& $PythonExe @doctorArgs
$code = $LASTEXITCODE

if (!$NoPause -and !$Json) {
    Write-Host ""
    Read-Host "Нажмите Enter для выхода" | Out-Null
}
exit $code
