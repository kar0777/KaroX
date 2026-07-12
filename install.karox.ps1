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
$AppDir = Join-Path $RuntimeDir "app"
$BinDir = Join-Path $RuntimeDir "bin"
$VenvDir = Join-Path $RuntimeDir ".venv"
$PythonExe = Join-Path $VenvDir "Scripts\python.exe"
$DesktopBat = Join-Path ([Environment]::GetFolderPath("Desktop")) "KaroX.bat"
$MigrationScript = Join-Path $Root "scripts\karox_paths.py"

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
            if ($name -eq "py") {
                $resolved = (& py -3 -c "import sys; print(sys.executable)").Trim()
            } else { $resolved = $cmd.Source }
            & $resolved -c "import sys,venv; assert sys.version_info >= (3,10)"
            if ($LASTEXITCODE -eq 0) { return $resolved }
        } catch {}
    }
    return $null
}

function Ensure-UserPath($path) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (($userPath -split ";") -notcontains $path) {
        [Environment]::SetEnvironmentVariable("Path", (($userPath.TrimEnd(";")) + ";" + $path).TrimStart(";"), "User")
    }
    if (($env:Path -split ";") -notcontains $path) { $env:Path = "$path;" + $env:Path }
}

function Copy-AppFiles {
    if (Test-Path -LiteralPath $AppDir) { Remove-Item -LiteralPath $AppDir -Recurse -Force }
    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    Get-ChildItem -LiteralPath $Root -Force | Where-Object { $_.Name -notin @(".git", ".venv", "__pycache__") } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $AppDir $_.Name) -Recurse -Force
    }
}

function Assert-InstallationComplete {
    $required = @(
        "start.ps1",
        "start.core.ps1",
        "requirements.txt",
        "scripts\karox_paths.py",
        "scripts\karox_admin_entry.py",
        "scripts\support_bundle_entry.py",
        "scripts\notion_profile.py",
        "scripts\tailscale_readiness.py",
        "server\repo_tools.py",
        "server\notion_gateway.py"
    )
    $missing = @()
    foreach ($relative in $required) {
        if (!(Test-Path -LiteralPath (Join-Path $AppDir $relative))) { $missing += $relative }
    }
    if ($missing.Count -gt 0) { throw "Incomplete KaroX installation. Missing: $($missing -join ', ')" }
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
        Remove-Item -LiteralPath '$legacyRuntimeEsc' -Recurse -Force -ErrorAction SilentlyContinue
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
$env:Path = "$Bin;" + $env:Path
Set-Location -LiteralPath $Root
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

Ensure-UserPath $BinDir
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
