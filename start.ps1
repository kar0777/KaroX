$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $env:LOCALAPPDATA "RepoPilotBridge"
$PythonExe = Join-Path $RuntimeDir ".venv\Scripts\python.exe"
$Patcher = Join-Path $Root "scripts\patch_notion_provider.py"
$Doctor = Join-Path $Root "scripts\notion_doctor.py"
$Core = Join-Path $Root "start.core.ps1"
$GeneratedDir = Join-Path $RuntimeDir "generated"
$Generated = Join-Path $GeneratedDir "start.notion.generated.ps1"

function Find-KaroXPython {
    if (Test-Path -LiteralPath $PythonExe) { return $PythonExe }
    foreach ($name in @("py", "python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    throw "Python was not found. Run install.ps1 first."
}

$python = Find-KaroXPython
$arguments = @($args)
$forceNotion = $false

if ($arguments.Count -gt 0 -and ([string]$arguments[0]).ToLowerInvariant() -eq "notion") {
    $forceNotion = $true
    $subcommand = if ($arguments.Count -gt 1) { ([string]$arguments[1]).ToLowerInvariant() } else { "" }
    if ($subcommand -eq "install" -or $subcommand -eq "update") {
        & $python -m pip install -r (Join-Path $Root "requirements.txt")
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $python $Doctor --root $Root
        exit $LASTEXITCODE
    }
    if ($subcommand -eq "doctor" -or $subcommand -eq "status") {
        & $python $Doctor --root $Root
        exit $LASTEXITCODE
    }
    if ($subcommand -eq "docs") {
        Write-Host (Join-Path $Root "NOTION.md")
        exit 0
    }
}

if (!(Test-Path -LiteralPath $Core)) { throw "start.core.ps1 is missing. Reinstall or update KaroX." }
if (!(Test-Path -LiteralPath $Patcher)) { throw "Notion provider patcher is missing. Reinstall or update KaroX." }

New-Item -ItemType Directory -Force -Path $GeneratedDir | Out-Null
& $python $Patcher --platform powershell --source $Core --output $Generated --root $Root | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not generate the Notion-enabled KaroX launcher. Run: karox notion doctor" }

$previousRoot = $env:KAROX_SOURCE_ROOT
$previousClient = $env:KAROX_FORCE_AI_CLIENT
try {
    $env:KAROX_SOURCE_ROOT = $Root
    if ($forceNotion) { $env:KAROX_FORCE_AI_CLIENT = "notion" }
    & powershell -NoProfile -ExecutionPolicy Bypass -File $Generated
    exit $LASTEXITCODE
} finally {
    $env:KAROX_SOURCE_ROOT = $previousRoot
    $env:KAROX_FORCE_AI_CLIENT = $previousClient
}
