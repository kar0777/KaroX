$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$RuntimeDir = Join-Path $env:LOCALAPPDATA "RepoPilotBridge"
$PythonExe = Join-Path $RuntimeDir ".venv\Scripts\python.exe"
$Patcher = Join-Path $Root "scripts\patch_notion_provider.py"
$NotionDoctor = Join-Path $Root "scripts\notion_doctor.py"
$NotionProfile = Join-Path $Root "scripts\notion_profile.py"
$TailscaleReadiness = Join-Path $Root "scripts\tailscale_readiness.py"
$Admin = Join-Path $Root "scripts\karox_cli.py"
$Support = Join-Path $Root "scripts\support_bundle.py"
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

function Find-TailscaleExecutable {
    foreach ($name in @("tailscale.exe", "tailscale")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and (Test-Path -LiteralPath $cmd.Source)) { return $cmd.Source }
    }
    $known = @(
        "$env:ProgramFiles\Tailscale\tailscale.exe",
        "${env:ProgramFiles(x86)}\Tailscale\tailscale.exe",
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\tailscale.exe",
        "$env:LOCALAPPDATA\Microsoft\WindowsApps\tailscale.exe"
    )
    foreach ($path in $known) {
        if ($path -and (Test-Path -LiteralPath $path)) { return $path }
    }
    return $null
}

function Invoke-NotionProfileJson([string[]]$ProfileArgs) {
    if (!(Test-Path -LiteralPath $NotionProfile)) {
        throw "Persistent Notion profile module is missing. Run: karox update"
    }
    $raw = & $python $NotionProfile @ProfileArgs
    if ($LASTEXITCODE -ne 0) { throw "Notion profile command failed: $($ProfileArgs -join ' ')" }
    return (($raw -join [Environment]::NewLine) | ConvertFrom-Json)
}

function Invoke-TailscaleReadinessJson([int]$WaitSeconds = 0) {
    if (!(Test-Path -LiteralPath $TailscaleReadiness)) {
        throw "Tailscale readiness module is missing. Run: karox update"
    }
    $probeArgs = @("--json")
    if ($WaitSeconds -gt 0) {
        $probeArgs = @("--wait", [string]$WaitSeconds, "--interval", "2", "--json")
    }
    $raw = & $python $TailscaleReadiness @probeArgs
    if ($LASTEXITCODE -ne 0) { throw "Tailscale readiness check failed." }
    return (($raw -join [Environment]::NewLine) | ConvertFrom-Json)
}

function Save-TailscaleProfileUrl($probe) {
    if (!$probe.ready -or !$probe.baseUrl) { return $null }
    Invoke-NotionProfileJson @("set-url", "--url", [string]$probe.baseUrl, "--json") | Out-Null
    return (Invoke-NotionProfileJson @("connection", "--json", "--show-token"))
}

function Show-NotionConnection($profile, $showToken = $false) {
    Write-Host ""
    Write-Host "KaroX <-> Notion persistent connection" -ForegroundColor Cyan
    Write-Host "----------------------------------------" -ForegroundColor DarkCyan
    Write-Host ("MCP URL : " + [string]$profile.mcpUrl)
    Write-Host "Auth    : Bearer token"
    if ($showToken -and $profile.apiKey) {
        Write-Host ("Token   : " + [string]$profile.apiKey) -ForegroundColor Yellow
    } else {
        Write-Host ("Token   : " + [string]$profile.tokenHint)
    }
    Write-Host ""
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

function Format-TailscaleFailure($probe, $upExitCode) {
    $parts = @()
    if ($probe.backendState) { $parts += ("state=" + [string]$probe.backendState) }
    if ($probe.error) { $parts += [string]$probe.error }
    if ($upExitCode -ne 0) { $parts += ("tailscale up exit=" + [string]$upExitCode) }
    if ($probe.authUrl) { $parts += ("login=" + [string]$probe.authUrl) }
    if ($parts.Count -eq 0) { return "Tailscale did not become ready." }
    return ($parts -join " | ")
}

function Setup-PersistentNotionConnection {
    Invoke-NotionProfileJson @("ensure", "--json") | Out-Null
    $tailscale = Find-TailscaleExecutable
    if (!$tailscale) {
        $winget = Get-Command winget -ErrorAction SilentlyContinue
        if (!$winget) {
            throw "Tailscale is required for a permanent Notion URL. Install Tailscale and run: karox notion setup"
        }
        Write-Host "Installing Tailscale for the permanent Notion URL..." -ForegroundColor Yellow
        & winget install -e --id Tailscale.Tailscale --accept-source-agreements --accept-package-agreements
        if ($LASTEXITCODE -ne 0) { throw "Tailscale installation failed." }
        $tailscale = Find-TailscaleExecutable
        if (!$tailscale) { throw "Tailscale was installed but is not visible yet. Restart PowerShell and run: karox notion setup" }
    }

    $probe = Invoke-TailscaleReadinessJson 3
    $upExitCode = 0
    if (!$probe.ready) {
        Write-Host "Opening Tailscale login..." -ForegroundColor Cyan
        Write-Host "Finish sign-in in the browser or Tailscale app. KaroX will wait up to 120 seconds." -ForegroundColor Yellow
        & $tailscale up
        $upExitCode = $LASTEXITCODE
        Write-Host "Waiting for Tailscale and the stable ts.net hostname..." -ForegroundColor Cyan
        $probe = Invoke-TailscaleReadinessJson 120
    }

    if (!$probe.ready -or !$probe.baseUrl) {
        if ($probe.authUrl) {
            try { Start-Process ([string]$probe.authUrl) | Out-Null } catch {}
        }
        $detail = Format-TailscaleFailure $probe $upExitCode
        throw ("Tailscale setup did not finish. " + $detail + "`nComplete Tailscale sign-in, wait until the app says Connected, then run: karox notion setup")
    }

    $state = Save-TailscaleProfileUrl $probe
    if (!$state -or !$state.mcpUrl) {
        throw "Tailscale connected, but KaroX could not save the stable .ts.net URL."
    }

    Show-NotionConnection $state $true
    Write-Host "Add this Custom MCP server to Notion once. Future KaroX sessions reuse the same URL and token." -ForegroundColor Green
    Write-Host "After connecting, run: karox notion" -ForegroundColor Green
}

function Ensure-PersistentNotionReady {
    $probe = Invoke-TailscaleReadinessJson 5
    if (!$probe.ready -or !$probe.baseUrl) {
        Setup-PersistentNotionConnection
        $probe = Invoke-TailscaleReadinessJson 10
    }
    if (!$probe.ready -or !$probe.baseUrl) {
        throw "Tailscale is not ready. Run: karox notion setup"
    }
    Save-TailscaleProfileUrl $probe | Out-Null
    return $probe
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
    if ($subcommand -eq "install" -or $subcommand -eq "update") {
        & $python -m pip install -r (Join-Path $Root "requirements.txt")
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
        & $python $NotionDoctor --root $Root
        exit $LASTEXITCODE
    }
    if ($subcommand -eq "setup") {
        Setup-PersistentNotionConnection
        exit 0
    }
    if ($subcommand -eq "connection") {
        $profile = Invoke-NotionProfileJson @("connection", "--json", "--show-token")
        Show-NotionConnection $profile $true
        exit 0
    }
    if ($subcommand -eq "rotate-key") {
        Invoke-NotionProfileJson @("rotate", "--json") | Out-Null
        $profile = Invoke-NotionProfileJson @("connection", "--json", "--show-token")
        Show-NotionConnection $profile $true
        Write-Host "The key changed. Replace it once in Notion before reconnecting." -ForegroundColor Yellow
        exit 0
    }
    if ($subcommand -eq "reset-connection") {
        & $python $NotionProfile reset | Out-Null
        Write-Host "Persistent Notion connection profile removed." -ForegroundColor Yellow
        exit $LASTEXITCODE
    }
    if ($subcommand -eq "doctor") {
        & $python $NotionDoctor --root $Root
        exit $LASTEXITCODE
    }
    if ($subcommand -eq "status") {
        $probe = Invoke-TailscaleReadinessJson 3
        if ($probe.ready -and $probe.baseUrl) { Save-TailscaleProfileUrl $probe | Out-Null }
        $profile = Invoke-NotionProfileJson @("connection", "--json")
        Show-NotionConnection $profile $false
        Write-Host ("Tailscale: " + [string]$probe.backendState + " | " + [string]$probe.dnsName)
        if (!$probe.ready -and $probe.error) { Write-Host ([string]$probe.error) -ForegroundColor Yellow }
        & $python $NotionDoctor --root $Root
        exit $LASTEXITCODE
    }
    if ($subcommand -eq "docs") {
        Write-Host (Join-Path $Root "NOTION.md")
        exit 0
    }
    Ensure-PersistentNotionReady | Out-Null
}

if (!(Test-Path -LiteralPath $Core)) { throw "start.core.ps1 is missing. Reinstall or update KaroX." }
if (!(Test-Path -LiteralPath $Patcher)) { throw "Notion provider patcher is missing. Reinstall or update KaroX." }
if (!(Test-Path -LiteralPath $Admin)) { throw "KaroX admin CLI is missing. Reinstall or update KaroX." }
if (!(Test-Path -LiteralPath $Support)) { throw "KaroX support bundle module is missing. Reinstall or update KaroX." }
if (!(Test-Path -LiteralPath $TailscaleReadiness)) { throw "Tailscale readiness module is missing. Reinstall or update KaroX." }

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
