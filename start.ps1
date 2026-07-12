$ErrorActionPreference = "Stop"
try {
    chcp.com 65001 > $null
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = if ($env:KAROX_RUNTIME_DIR) { $env:KAROX_RUNTIME_DIR } else { Join-Path $env:LOCALAPPDATA "RepoPilotBridge" }
$PythonExe = Join-Path $RuntimeDir ".venv\Scripts\python.exe"
$Patcher = Join-Path $Root "scripts\patch_notion_provider.py"
$NotionDoctor = Join-Path $Root "scripts\notion_doctor.py"
$NotionWizard = Join-Path $Root "scripts\notion_setup_wizard.py"
$Admin = Join-Path $Root "scripts\karox_admin_entry.py"
$Support = Join-Path $Root "scripts\support_bundle_entry.py"
$Core = Join-Path $Root "start.core.ps1"
$GeneratedDir = Join-Path $RuntimeDir "generated"
$Generated = Join-Path $GeneratedDir "start.notion.generated.ps1"

function Find-KaroXPython {
    if (Test-Path -LiteralPath $PythonExe) { return $PythonExe }
    foreach ($name in @("py", "python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd) { return $cmd.Source }
    }
    throw "Python was not found. Run the KaroX installer first."
}

function Get-TailArguments($items, $startIndex) {
    if ($items.Count -le $startIndex) { return @() }
    return @($items[$startIndex..($items.Count - 1)])
}

function Clear-StaleReleaseCache {
    $cache = Join-Path $RuntimeDir "cache\release-status.json"
    $versionFile = Join-Path $Root "VERSION"
    if (!(Test-Path -LiteralPath $cache) -or !(Test-Path -LiteralPath $versionFile)) { return }
    try {
        $installedText = (Get-Content -Raw -LiteralPath $versionFile).Trim()
        $cached = Get-Content -Raw -LiteralPath $cache | ConvertFrom-Json
        if (!$cached.version) { return }
        $installed = [version]$installedText
        $cachedVersion = [version]([string]$cached.version)
        if ($cachedVersion -lt $installed) {
            Remove-Item -LiteralPath $cache -Force -ErrorAction SilentlyContinue
        }
    } catch {}
}

function Invoke-NotionWizard([string[]]$WizardArgs) {
    if (!(Test-Path -LiteralPath $NotionWizard)) {
        throw "Localized Notion setup wizard is missing. Run: karox update"
    }
    & $python $NotionWizard @WizardArgs | Out-Host
    return [int]$LASTEXITCODE
}

$python = Find-KaroXPython
Clear-StaleReleaseCache
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

if ($first -eq "support") {
    $supportArgs = Get-TailArguments $arguments 1
    & $python $Support @supportArgs
    exit $LASTEXITCODE
}

if ($first -in @("version", "status", "doctor", "update", "dashboard")) {
    $adminArgs = Get-TailArguments $arguments 1
    & $python $Admin $first @adminArgs
    exit $LASTEXITCODE
}

if ($first -eq "notion") {
    $forceNotion = $true
    $subcommand = if ($arguments.Count -gt 1) { ([string]$arguments[1]).ToLowerInvariant() } else { "" }

    if ($subcommand -in @("install", "update")) {
        & $python -m pip install -r (Join-Path $Root "requirements.txt")
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $python $NotionDoctor --root $Root
        exit $LASTEXITCODE
    }
    if ($subcommand -eq "setup") {
        $code = Invoke-NotionWizard @("setup")
        exit $code
    }
    if ($subcommand -eq "connection") {
        $code = Invoke-NotionWizard @("connection", "--show-token")
        exit $code
    }
    if ($subcommand -eq "rotate-key") {
        $code = Invoke-NotionWizard @("rotate")
        exit $code
    }
    if ($subcommand -eq "reset-connection") {
        $code = Invoke-NotionWizard @("reset")
        exit $code
    }
    if ($subcommand -eq "doctor") {
        & $python $NotionDoctor --root $Root
        exit $LASTEXITCODE
    }
    if ($subcommand -eq "status") {
        $wizardCode = Invoke-NotionWizard @("status")
        & $python $NotionDoctor --root $Root
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        exit $wizardCode
    }
    if ($subcommand -eq "docs") {
        Write-Host (Join-Path $Root "NOTION.md")
        exit 0
    }
    if ($subcommand) {
        throw "Unknown notion command: $subcommand"
    }

    $code = Invoke-NotionWizard @("ensure")
    if ($code -ne 0) { exit $code }
}

foreach ($required in @($Core, $Patcher, $Admin, $Support, $NotionWizard)) {
    if (!(Test-Path -LiteralPath $required)) {
        throw "Required KaroX component is missing: $required. Run: karox update"
    }
}

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
