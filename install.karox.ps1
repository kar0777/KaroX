param([switch]$Start)

$ErrorActionPreference = "Stop"
try {
    chcp.com 65001 > $null
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigDir = Join-Path $env:APPDATA "KaroX"
$RuntimeDir = Join-Path $env:LOCALAPPDATA "KaroX"
$LegacyConfigDir = Join-Path $env:APPDATA "RepoPilotBridge"
$LegacyRuntimeDir = Join-Path $env:LOCALAPPDATA "RepoPilotBridge"
$LegacyBinDir = Join-Path $LegacyRuntimeDir "bin"
$AppDir = Join-Path $RuntimeDir "app"
$BinDir = Join-Path $RuntimeDir "bin"
$VenvDir = Join-Path $RuntimeDir ".venv"
$PythonExe = Join-Path $VenvDir "Scripts\python.exe"
$DesktopBat = Join-Path ([Environment]::GetFolderPath("Desktop")) "KaroX.bat"
$MigrationScript = Join-Path $Root "scripts\karox_paths.py"
$RebrandScript = Join-Path $Root "scripts\rebrand_runtime.py"

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $extra = @(
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links",
        "$env:LOCALAPPDATA\Microsoft\WindowsApps",
        "$env:LOCALAPPDATA\Programs\Python\Python313",
        "$env:LOCALAPPDATA\Programs\Python\Python312",
        "$env:ProgramFiles\Python313",
        "$env:ProgramFiles\Python312"
    ) -join ";"
    $env:Path = "$machine;$user;$extra"
}

function Find-Python {
    Refresh-Path
    foreach ($path in @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:ProgramFiles\Python313\python.exe",
        "$env:ProgramFiles\Python312\python.exe"
    )) {
        if ($path -and (Test-Path -LiteralPath $path)) {
            try { & $path -c "import sys,venv; assert sys.version_info >= (3,10)"; if ($LASTEXITCODE -eq 0) { return $path } } catch {}
        }
    }
    foreach ($name in @("py", "python", "python3")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if (!$cmd) { continue }
        try {
            if ($name -eq "py") { $resolved = (& py -3 -c "import sys; print(sys.executable)").Trim() } else { $resolved = $cmd.Source }
            & $resolved -c "import sys,venv; assert sys.version_info >= (3,10)"
            if ($LASTEXITCODE -eq 0) { return $resolved }
        } catch {}
    }
    return $null
}

function Normalize-PathEntry($value) {
    if ($null -eq $value) { return "" }
    return ([string]$value).Trim().Trim('"').TrimEnd('\')
}

function Set-KaroXPath {
    $newNormalized = Normalize-PathEntry $BinDir
    $legacyNormalized = Normalize-PathEntry $LegacyBinDir

    $userItems = @()
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    foreach ($entry in @(([string]$userPath) -split ";")) {
        $normalized = Normalize-PathEntry $entry
        if (!$normalized) { continue }
        if ($normalized -ieq $newNormalized -or $normalized -ieq $legacyNormalized) { continue }
        $userItems += ([string]$entry).Trim()
    }
    [Environment]::SetEnvironmentVariable("Path", ((@($BinDir) + $userItems) -join ";"), "User")

    $currentItems = @()
    foreach ($entry in @(([string]$env:Path) -split ";")) {
        $normalized = Normalize-PathEntry $entry
        if (!$normalized) { continue }
        if ($normalized -ieq $newNormalized -or $normalized -ieq $legacyNormalized) { continue }
        $currentItems += ([string]$entry).Trim()
    }
    $env:Path = ((@($BinDir) + $currentItems) -join ";")
}

function Write-LegacyForwarder {
    New-Item -ItemType Directory -Force -Path $LegacyBinDir | Out-Null
    @'
$ErrorActionPreference = "Stop"
$target = Join-Path $env:LOCALAPPDATA "KaroX\bin\karox.ps1"
if (!(Test-Path -LiteralPath $target)) { throw "KaroX compatibility launcher could not find the new installation." }
& $target @args
exit $LASTEXITCODE
'@ | Set-Content -LiteralPath (Join-Path $LegacyBinDir "karox.ps1") -Encoding UTF8
    @'
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\KaroX\bin\karox.ps1" %*
'@ | Set-Content -LiteralPath (Join-Path $LegacyBinDir "karox.cmd") -Encoding ASCII
}

function Move-OutOf-AppDirectory {
    try {
        $current = [IO.Path]::GetFullPath((Get-Location).Path).TrimEnd('\')
        $installedApp = [IO.Path]::GetFullPath($AppDir).TrimEnd('\')
        if ($current -ieq $installedApp -or $current.StartsWith($installedApp + "\", [StringComparison]::OrdinalIgnoreCase)) {
            Set-Location -LiteralPath $Root
        }
    } catch {
        Set-Location -LiteralPath $Root
    }
}

function Copy-AppFiles {
    Move-OutOf-AppDirectory
    if (Test-Path -LiteralPath $AppDir) { Remove-Item -LiteralPath $AppDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    Get-ChildItem -LiteralPath $Root -Force | Where-Object { $_.Name -notin @(".git", ".venv", "__pycache__") } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $AppDir $_.Name) -Recurse -Force
    }
}

function Repair-RequiredFiles {
    foreach ($relative in @(
        "scripts\tailscale_readiness.py",
        "scripts\karox_paths.py",
        "scripts\karox_admin_entry.py",
        "scripts\support_bundle_entry.py",
        "scripts\rebrand_runtime.py"
    )) {
        $source = Join-Path $Root $relative
        $target = Join-Path $AppDir $relative
        if (!(Test-Path -LiteralPath $target) -and (Test-Path -LiteralPath $source)) {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
            Copy-Item -LiteralPath $source -Destination $target -Force
        }
    }
}

function Assert-InstallationComplete {
    $required = @(
        "start.ps1", "start.core.ps1", "requirements.txt",
        "scripts\karox_paths.py", "scripts\karox_admin_entry.py", "scripts\support_bundle_entry.py",
        "scripts\rebrand_runtime.py", "scripts\notion_profile.py", "scripts\tailscale_readiness.py",
        "scripts\notion_setup_wizard.py",
        "server\repo_tools.py", "server\notion_gateway.py"
    )
    $missing = @()
    foreach ($relative in $required) {
        if (!(Test-Path -LiteralPath (Join-Path $AppDir $relative))) { $missing += $relative }
    }
    if ($missing.Count -gt 0) { throw "Incomplete KaroX installation. Missing: $($missing -join ', ')" }
    $startText = Get-Content -Raw -LiteralPath (Join-Path $AppDir "start.ps1")
    if ($startText -match "RepoPilotBridge") { throw "Installed start.ps1 still contains legacy RepoPilotBridge paths." }
}

function Schedule-LegacyCleanup {
    if (!(Test-Path -LiteralPath $LegacyConfigDir) -and !(Test-Path -LiteralPath $LegacyRuntimeDir)) { return }
    $cleanup = Join-Path $env:TEMP ("karox-cleanup-" + [guid]::NewGuid().ToString("N") + ".ps1")
    $legacyConfigEsc = $LegacyConfigDir.Replace("'", "''")
    $legacyRuntimeEsc = $LegacyRuntimeDir.Replace("'", "''")
    @"
`$ErrorActionPreference = 'SilentlyContinue'
for (`$i = 0; `$i -lt 30; `$i++) {
    Start-Sleep -Seconds 2
    `$busy = @(Get-CimInstance Win32_Process | Where-Object { `$_.ProcessId -ne `$PID -and `$_.CommandLine -like '*RepoPilotBridge*' })
    if (`$busy.Count -eq 0) {
        Remove-Item -LiteralPath '$legacyConfigEsc' -Recurse -Force -ErrorAction SilentlyContinue
        if (Test-Path -LiteralPath '$legacyRuntimeEsc') {
            Get-ChildItem -LiteralPath '$legacyRuntimeEsc' -Force | Where-Object { `$_.Name -ne 'bin' } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
            `$legacyBin = Join-Path '$legacyRuntimeEsc' 'bin'
            if (Test-Path -LiteralPath `$legacyBin) {
                Get-ChildItem -LiteralPath `$legacyBin -Force | Where-Object { `$_.Name -notin @('karox.cmd','karox.ps1') } | Remove-Item -Recurse -Force -ErrorAction SilentlyContinue
            }
        }
        break
    }
}
Remove-Item -LiteralPath `$MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue
"@ | Set-Content -LiteralPath $cleanup -Encoding UTF8
    Start-Process powershell -WindowStyle Hidden -ArgumentList @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $cleanup) | Out-Null
}

Write-Host ""
Write-Host "KaroX installer" -ForegroundColor Cyan
Write-Host "----------------------------------------" -ForegroundColor DarkCyan
New-Item -ItemType Directory -Force -Path $ConfigDir, $RuntimeDir, $BinDir | Out-Null

$BasePython = Find-Python
if (!$BasePython) {
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if (!$winget) { throw "Python 3.10+ is required. Install Python and retry." }
    winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
    Refresh-Path
    $BasePython = Find-Python
}
if (!$BasePython) { throw "Python 3.10+ was not found after installation." }
Write-Host "Python: $BasePython" -ForegroundColor Green

$env:KAROX_CONFIG_DIR = $ConfigDir
$env:KAROX_RUNTIME_DIR = $RuntimeDir
if (Test-Path -LiteralPath $MigrationScript) {
    & $BasePython $MigrationScript migrate --json | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Could not migrate legacy KaroX data." }
}

if (!(Test-Path -LiteralPath $PythonExe)) {
    Write-Host "Creating virtual environment: $VenvDir" -ForegroundColor Yellow
    & $BasePython -m venv $VenvDir
}
if (!(Test-Path -LiteralPath $PythonExe)) { throw "Could not create virtual environment." }
& $PythonExe -m pip install --upgrade pip
& $PythonExe -m pip install -r (Join-Path $Root "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Python dependency installation failed." }

Copy-AppFiles
Repair-RequiredFiles
& $PythonExe (Join-Path $AppDir "scripts\rebrand_runtime.py") --root $AppDir
if ($LASTEXITCODE -ne 0) { throw "Could not rewrite installed files to KaroX paths." }
Assert-InstallationComplete

$legacyCloudflared = Join-Path $LegacyRuntimeDir "bin\cloudflared.exe"
$newCloudflared = Join-Path $BinDir "cloudflared.exe"
if (!(Test-Path -LiteralPath $newCloudflared) -and (Test-Path -LiteralPath $legacyCloudflared)) {
    Copy-Item -LiteralPath $legacyCloudflared -Destination $newCloudflared -Force
}

$KaroXPs1 = Join-Path $BinDir "karox.ps1"
$KaroXCmd = Join-Path $BinDir "karox.cmd"
@'
$ErrorActionPreference = "Stop"
try { chcp.com 65001 > $null; [Console]::InputEncoding = [Text.Encoding]::UTF8; [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}
$AppRoot = Join-Path $env:LOCALAPPDATA "KaroX"
$env:KAROX_CONFIG_DIR = Join-Path $env:APPDATA "KaroX"
$env:KAROX_RUNTIME_DIR = $AppRoot
$Bin = Join-Path $AppRoot "bin"
$Root = Join-Path $AppRoot "app"
$LegacyRoot = Join-Path $env:LOCALAPPDATA "RepoPilotBridge"
$LegacyBin = Join-Path $LegacyRoot "bin"
$legacyNormalized = $LegacyBin.TrimEnd('\')
$legacyInCurrentPath = @((([string]$env:Path) -split ';') | Where-Object { ([string]$_).Trim().Trim('"').TrimEnd('\') -ieq $legacyNormalized }).Count -gt 0
if (!$legacyInCurrentPath -and (Test-Path -LiteralPath $LegacyRoot)) {
    try { Remove-Item -LiteralPath $LegacyRoot -Recurse -Force -ErrorAction Stop } catch {}
}
$env:Path = "$Bin;" + $env:Path
& (Join-Path $Root "start.ps1") @args
exit $LASTEXITCODE
'@ | Set-Content -LiteralPath $KaroXPs1 -Encoding UTF8
@'
@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\KaroX\bin\karox.ps1" %*
'@ | Set-Content -LiteralPath $KaroXCmd -Encoding ASCII
@'
@echo off
title KaroX
color 0B
powershell -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\KaroX\bin\karox.ps1"
pause
'@ | Set-Content -LiteralPath $DesktopBat -Encoding ASCII

Write-LegacyForwarder
Set-KaroXPath
Write-Host ""
Write-Host "Installation complete." -ForegroundColor Green
Write-Host "Application : $AppDir"
Write-Host "Runtime     : $RuntimeDir"
Write-Host "Config      : $ConfigDir"
Write-Host "Command     : karox"
Write-Host ""
Schedule-LegacyCleanup

if ($Start) {
    powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $AppDir "start.ps1")
    exit $LASTEXITCODE
}
& $PythonExe (Join-Path $AppDir "scripts\product_doctor.py") --root $AppDir
exit $LASTEXITCODE
