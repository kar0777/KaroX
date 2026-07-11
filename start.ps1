$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $env:LOCALAPPDATA "RepoPilotBridge"
$PythonExe = Join-Path $RuntimeDir ".venv\Scripts\python.exe"
$Patcher = Join-Path $Root "scripts\patch_notion_provider.py"
$NotionDoctor = Join-Path $Root "scripts\notion_doctor.py"
$Admin = Join-Path $Root "scripts\karox_admin.py"
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

function Get-TailArguments($items, $startIndex) {
    if ($items.Count -le $startIndex) { return @() }
    return @($items[$startIndex..($items.Count - 1)])
}

$python = Find-KaroXPython
$arguments = @($args)
$forceNotion = $false
$first = if ($arguments.Count -gt 0) { ([string]$arguments[0]).ToLowerInvariant() } else { "" }

if ($first -in @("--version", "-v")) {
    & $python $Admin version
    exit $LASTEXITCODE
}

if ($first -in @("help", "--help", "-h")) {
    & $python $Admin --help
    exit $LASTEXITCODE
}

if ($first -in @("version", "status", "doctor", "update", "support", "dashboard")) {
    $adminArgs = Get-TailArguments $arguments 1
    & $python $Admin $first @adminArgs
    exit $LASTEXITCODE
}

if ($first -eq "notion") {
    $forceNotion = $true
    $subcommand = if ($arguments.Count -gt 1) { ([string]$arguments[1]).ToLowerInvariant() } else { "" }
    if ($subcommand -eq "install" -or $subcommand -eq "update") {
        & $python -m pip install -r (Join-Path $Root "requirements.txt")
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $python $NotionDoctor --root $Root
        exit $LASTEXITCODE
    }
    if ($subcommand -eq "doctor" -or $subcommand -eq "status") {
        & $python $NotionDoctor --root $Root
        exit $LASTEXITCODE
    }
    if ($subcommand -eq "docs") {
        Write-Host (Join-Path $Root "NOTION.md")
        exit 0
    }
}

if (!(Test-Path -LiteralPath $Core)) { throw "start.core.ps1 is missing. Reinstall or update KaroX." }
if (!(Test-Path -LiteralPath $Patcher)) { throw "Notion provider patcher is missing. Reinstall or update KaroX." }
if (!(Test-Path -LiteralPath $Admin)) { throw "KaroX admin CLI is missing. Reinstall or update KaroX." }

if ($env:KAROX_UPDATE_NOTICE -ne "0") {
    try { & $python $Admin notice 2>$null } catch {}
}

New-Item -ItemType Directory -Force -Path $GeneratedDir | Out-Null
& $python $Patcher --platform powershell --source $Core --output $Generated --root $Root | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Could not generate the KaroX launcher. Run: karox doctor" }

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
