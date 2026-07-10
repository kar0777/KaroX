param(
    [switch]$Start
)

$ErrorActionPreference = "Stop"
try {
    chcp.com 65001 > $null
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigDir = Join-Path $env:APPDATA "RepoPilotBridge"
$RuntimeDir = Join-Path $env:LOCALAPPDATA "RepoPilotBridge"
$AppDir = Join-Path $RuntimeDir "app"
$BinDir = Join-Path $RuntimeDir "bin"
$Venv = Join-Path $RuntimeDir ".venv"
$PythonExe = Join-Path $Venv "Scripts\python.exe"
$DesktopBat = Join-Path ([Environment]::GetFolderPath("Desktop")) "Star For KaroX.bat"

function Has-Command($name) {
    return [bool](Get-Command $name -ErrorAction SilentlyContinue)
}

function Ask-Yes($text, $defaultYes = $true) {
    $suffix = if ($defaultYes) { "Д/н" } else { "д/Н" }
    $answer = Read-Host "$text [$suffix]"
    if (!$answer) { return $defaultYes }
    return $answer -match "^[ДдYy]"
}

function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $extra = @(
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links",
        "$env:LOCALAPPDATA\Microsoft\WindowsApps",
        "$env:LOCALAPPDATA\Programs\Python\Python312",
        "$env:LOCALAPPDATA\Programs\Python\Python312\Scripts",
        "$env:LOCALAPPDATA\Programs\Python\Python313",
        "$env:LOCALAPPDATA\Programs\Python\Python313\Scripts",
        "$env:ProgramFiles\Python312",
        "$env:ProgramFiles\Python312\Scripts",
        "$env:ProgramFiles\Python313",
        "$env:ProgramFiles\Python313\Scripts"
    ) -join ";"
    $env:Path = "$machine;$user;$extra"
}

function Install-WingetPackage($id, $name) {
    Refresh-Path
    if (!(Has-Command "winget")) {
        Write-Host "winget не найден. Установите App Installer из Microsoft Store и запустите установку снова." -ForegroundColor Red
        return
    }

    if (Ask-Yes "Установить $name через winget?") {
        winget install -e --id $id --accept-source-agreements --accept-package-agreements
        Refresh-Path
    }
}

function Test-Python($path) {
    if (!(Test-Path $path)) { return $false }
    try {
        & $path -c "import sys, venv; print(sys.executable)" *> $null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

function Find-Python {
    Refresh-Path
    $candidates = New-Object System.Collections.Generic.List[string]

    $known = @(
        "$env:LOCALAPPDATA\Programs\Python\Python313\python.exe",
        "$env:LOCALAPPDATA\Programs\Python\Python312\python.exe",
        "$env:ProgramFiles\Python313\python.exe",
        "$env:ProgramFiles\Python312\python.exe"
    )
    foreach ($p in $known) {
        if ($p -and !$candidates.Contains($p)) { $candidates.Add($p) }
    }

    try {
        $where = where.exe python 2>$null
        foreach ($p in $where) {
            if ($p -and !$candidates.Contains($p)) { $candidates.Add($p) }
        }
    } catch {}

    foreach ($p in $candidates) {
        if (Test-Python $p) { return $p }
    }

    foreach ($version in @("3.13", "3.12", "3")) {
        try {
            $pyOut = & py -$version -c "import sys; print(sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and $pyOut -and (Test-Python $pyOut.Trim())) {
                return $pyOut.Trim()
            }
        } catch {}
    }

    return $null
}

function Find-Cloudflared {
    Refresh-Path
    foreach ($name in @("cloudflared.exe", "cloudflared")) {
        $cmd = Get-Command $name -ErrorAction SilentlyContinue
        if ($cmd -and (Test-Path $cmd.Source)) { return $cmd.Source }
    }

    $known = @(
        "$env:LOCALAPPDATA\Microsoft\WinGet\Links\cloudflared.exe",
        "$env:LOCALAPPDATA\Microsoft\WindowsApps\cloudflared.exe",
        "$env:ProgramFiles\Cloudflare\cloudflared.exe"
    )
    foreach ($p in $known) {
        if (Test-Path $p) { return $p }
    }

    try {
        $pkg = Get-ChildItem "$env:LOCALAPPDATA\Microsoft\WinGet\Packages" -Recurse -Filter "cloudflared.exe" -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($pkg) { return $pkg.FullName }
    } catch {}

    return $null
}

function Sync-AppFiles {
    New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
    New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

    $resolvedRoot = (Resolve-Path -LiteralPath $Root).Path
    $resolvedApp = $null
    if (Test-Path -LiteralPath $AppDir) {
        $resolvedApp = (Resolve-Path -LiteralPath $AppDir).Path
    }

    if ($resolvedRoot -eq $resolvedApp) {
        return $resolvedRoot
    }

    if (Test-Path -LiteralPath $AppDir) {
        Remove-Item -LiteralPath $AppDir -Recurse -Force -ErrorAction SilentlyContinue
    }

    New-Item -ItemType Directory -Force -Path $AppDir | Out-Null
    Get-ChildItem -LiteralPath $Root -Force | Where-Object {
        $_.Name -notin @(".git", ".venv", "__pycache__")
    } | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $AppDir $_.Name) -Recurse -Force
    }

    return (Resolve-Path -LiteralPath $AppDir).Path
}

function Resolve-InstalledServerDir($appDir) {
    $candidates = @(
        (Join-Path $appDir "server"),
        (Join-Path (Join-Path $appDir "server") "server"),
        $appDir
    )
    foreach ($candidate in $candidates) {
        if (Test-Path -LiteralPath (Join-Path $candidate "repo_tools.py")) {
            return (Resolve-Path -LiteralPath $candidate).Path
        }
    }
    throw "repo_tools.py не найден после установки. Проверенные папки: $($candidates -join ', ')"
}

function Ensure-UserPath($path) {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if (($userPath -split ";") -notcontains $path) {
        $newPath = (($userPath.TrimEnd(";")) + ";" + $path).TrimStart(";")
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
    }

    if (($env:Path -split ";") -notcontains $path) {
        $env:Path = "$path;" + $env:Path
    }
}

Write-Host ""
Write-Host "Установка Star For KaroX" -ForegroundColor Cyan
Write-Host "----------------------------------------" -ForegroundColor DarkCyan
Write-Host ""

New-Item -ItemType Directory -Force -Path $ConfigDir | Out-Null
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
Refresh-Path

if (!(Has-Command "git")) { Install-WingetPackage "Git.Git" "Git" }
if (!(Find-Cloudflared)) { Install-WingetPackage "Cloudflare.cloudflared" "cloudflared" }
if (!((Has-Command "node") -and (Has-Command "npm"))) { Install-WingetPackage "OpenJS.NodeJS.LTS" "Node.js LTS" }

$BasePython = Find-Python
if (!$BasePython) {
    Install-WingetPackage "Python.Python.3.12" "Python 3.12"
    $BasePython = Find-Python
}

if (!$BasePython) {
    throw "Python не найден после установки. Перезапустите PowerShell и выполните install.ps1 ещё раз."
}

Write-Host "Python: $BasePython" -ForegroundColor Green

if (!(Test-Path $PythonExe)) {
    Write-Host "Создаю Python virtualenv: $Venv" -ForegroundColor Yellow
    if (Test-Path $Venv) { Remove-Item -LiteralPath $Venv -Recurse -Force -ErrorAction SilentlyContinue }
    & $BasePython -m venv $Venv
    if (!(Test-Path $PythonExe)) { throw "Не удалось создать virtualenv: $PythonExe" }
}

Write-Host "Устанавливаю Python-зависимости..." -ForegroundColor Yellow
& $PythonExe -m pip install --upgrade pip
& $PythonExe -m pip install -r (Join-Path $Root "requirements.txt")

$InstalledAppDir = Sync-AppFiles
$InstalledServerDir = Resolve-InstalledServerDir $InstalledAppDir

$CfExe = Find-Cloudflared
if ($CfExe) {
    $CfDest = Join-Path $BinDir "cloudflared.exe"
    $ResolvedCfExe = (Resolve-Path -LiteralPath $CfExe).Path
    $ResolvedCfDest = $null
    if (Test-Path -LiteralPath $CfDest) {
        $ResolvedCfDest = (Resolve-Path -LiteralPath $CfDest).Path
    }
    if ($ResolvedCfExe -ne $ResolvedCfDest) {
        Copy-Item -LiteralPath $CfExe -Destination $CfDest -Force
    }
}

$KaroXPs1 = Join-Path $BinDir "karox.ps1"
$KaroXCmd = Join-Path $BinDir "karox.cmd"
$RepopilotPs1 = Join-Path $BinDir "repopilot.ps1"
$RepopilotCmd = Join-Path $BinDir "repopilot.cmd"

Set-Content $KaroXPs1 -Encoding UTF8 -Value @(
    '$ErrorActionPreference = "Stop"',
    'try {',
    '    chcp.com 65001 > $null',
    '    [Console]::InputEncoding = [System.Text.Encoding]::UTF8',
    '    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8',
    '    $OutputEncoding = [System.Text.Encoding]::UTF8',
    '} catch {}',
    '$AppRoot = Join-Path $env:LOCALAPPDATA "RepoPilotBridge"',
    '$Bin = Join-Path $AppRoot "bin"',
    '$Root = Join-Path $AppRoot "app"',
    '$env:Path = "$Bin;" + $env:Path',
    'Set-Location -LiteralPath $Root',
    '& (Join-Path $Root "start.ps1") @args',
    'exit $LASTEXITCODE'
)

Set-Content $KaroXCmd -Encoding ASCII -Value @(
    '@echo off',
    'powershell -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\RepoPilotBridge\bin\karox.ps1" %*'
)
Copy-Item -LiteralPath $KaroXPs1 -Destination $RepopilotPs1 -Force
Set-Content $RepopilotCmd -Encoding ASCII -Value @(
    '@echo off',
    'powershell -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\RepoPilotBridge\bin\karox.ps1" %*'
)

Set-Content $DesktopBat -Encoding ASCII -Value @(
    '@echo off',
    'title Star For KaroX',
    'color 0B',
    'powershell -NoProfile -ExecutionPolicy Bypass -File "%LOCALAPPDATA%\RepoPilotBridge\bin\karox.ps1"',
    'pause'
)

Ensure-UserPath $BinDir

Write-Host ""
Write-Host "Установка завершена." -ForegroundColor Green
Write-Host "Приложение       : $InstalledAppDir"
Write-Host "Server module    : $InstalledServerDir"
Write-Host "Runtime          : $RuntimeDir"
Write-Host "Команда          : karox"
Write-Host "Ярлык            : $DesktopBat"
Write-Host ""
Write-Host "Если команда karox не находится в старом окне PowerShell, откройте новое окно." -ForegroundColor Yellow
Write-Host ""

if ($Start) {
    Write-Host "Запускаю KaroX..." -ForegroundColor Green
    powershell -ExecutionPolicy Bypass -File (Join-Path $InstalledAppDir "start.ps1")
    exit $LASTEXITCODE
}

if (Ask-Yes "Запустить doctor-проверку сейчас?" $true) {
    powershell -ExecutionPolicy Bypass -File (Join-Path $InstalledAppDir "doctor.ps1")
}
