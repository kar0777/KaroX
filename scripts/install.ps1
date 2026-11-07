# KaroX 5.0.0 installer for Windows (PowerShell 5.1 or 7).
#
#   $env:KAROX_SPEC = "C:\path\to\karox_runtime-5.0.0-py3-none-any.whl"
#   irm https://raw.githubusercontent.com/kar0777/KaroX/v5.0.0/scripts/install.ps1 | iex
#
# What it does
#   * finds Python >= 3.10 and pipx (installs pipx for the user if missing);
#   * fresh machine: installs KaroX 5.0.0 as the "karox" command;
#   * existing KaroX 5.x/6.x: does not overwrite silently. With
#     KAROX_UPGRADE=1 it first copies the KaroX data directories to timestamped
#     backups and then runs `pipx install --force`;
#   * an old side-by-side `karox-runtime6` venv is left untouched and is not
#     used by this installer.
#
# Settings (environment variables, so they also work with "irm | iex")
#   KAROX_SPEC          required: pip requirement, URL or local wheel of 5.0.0
#   KAROX_UPGRADE       1 to replace an existing main install after backup
#   KAROX_PIPX          pipx executable instead of discovering one
#   KAROX_DRY_RUN       1 to print commands/backups instead of running them

$ErrorActionPreference = "Stop"

function Get-Setting([string]$Name, [string]$Default) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value.Trim()
}

$Spec = Get-Setting "KAROX_SPEC" ""
$Upgrade = (Get-Setting "KAROX_UPGRADE" "0") -eq "1"
$DryRun = (Get-Setting "KAROX_DRY_RUN" "0") -eq "1"
if (-not $Spec) {
    throw "karox-install: error: KAROX_SPEC is required; point it at karox_runtime-5.0.0-py3-none-any.whl (or a pinned 5.0.0 requirement)"
}

function Say([string]$Text) { Write-Host "karox-install: $Text" }
function Fail([string]$Text) {
    # throw, not exit: under "irm | iex" exit would close the user's window.
    throw "karox-install: error: $Text"
}

function Invoke-Checked([string[]]$Command) {
    if ($DryRun) {
        Say ("would run: " + ($Command -join " "))
        return
    }
    $rest = @()
    if ($Command.Length -gt 1) { $rest = $Command[1..($Command.Length - 1)] }
    & $Command[0] @rest
    if ($LASTEXITCODE -ne 0) { Fail ("command failed (" + $LASTEXITCODE + "): " + ($Command -join " ")) }
}

function Invoke-Quiet([string[]]$Command) {
    # Continue, not Stop: Windows PowerShell 5.1 turns any stderr line of a
    # native command into a terminating error under Stop, which would make a
    # warning from pipx look like "nothing installed".
    $ErrorActionPreference = "Continue"
    $rest = @()
    if ($Command.Length -gt 1) { $rest = $Command[1..($Command.Length - 1)] }
    # Python writes to a pipe in the ANSI code page and PowerShell decodes it
    # with the console code page, so a path such as C:\Users\<Cyrillic name>
    # would come back garbled. UTF-8 on both ends, restored afterwards.
    $savedIo = $env:PYTHONIOENCODING
    $savedEncoding = $null
    try {
        $savedEncoding = [Console]::OutputEncoding
        [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
    } catch {
        $savedEncoding = $null
    }
    $env:PYTHONIOENCODING = "utf-8"
    try {
        $output = & $Command[0] @rest 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return ($output | Out-String).Trim()
    } catch {
        return $null
    } finally {
        $env:PYTHONIOENCODING = $savedIo
        if ($null -ne $savedEncoding) {
            try { [Console]::OutputEncoding = $savedEncoding } catch { }
        }
    }
}

function Find-VenvPython([string[]]$Pipx, [string]$Venv) {
    $roots = @()
    $reported = Invoke-Quiet ($Pipx + @("environment", "--value", "PIPX_LOCAL_VENVS"))
    if ($reported) { $roots += $reported }
    # Fallbacks: an older pipx without "environment", or a reply that did not
    # survive the console code page.
    if ($env:PIPX_HOME) { $roots += (Join-Path $env:PIPX_HOME "venvs") }
    $roots += @((Join-Path $HOME "pipx\venvs"), (Join-Path $HOME ".local\pipx\venvs"))
    if ($env:LOCALAPPDATA) { $roots += (Join-Path $env:LOCALAPPDATA "pipx\pipx\venvs") }
    foreach ($root in $roots) {
        try {
            $candidate = Join-Path (Join-Path $root $Venv) "Scripts\python.exe"
            if ([System.IO.File]::Exists($candidate)) { return $candidate }
        } catch {
            continue
        }
    }
    return ""
}

function Install-KaroX {
    # --- Python -------------------------------------------------------------
    $Python = $null
    foreach ($candidate in @(@("py", "-3"), @("python"), @("python3"))) {
        if (-not (Get-Command $candidate[0] -ErrorAction SilentlyContinue)) { continue }
        $probe = Invoke-Quiet ($candidate + @("-c", "import sys; print(int(sys.version_info >= (3, 10)))"))
        if ($probe -eq "1") { $Python = $candidate; break }
    }
    if ($null -eq $Python) { Fail "Python 3.10 or newer was not found; install it from https://www.python.org/downloads/ (tick 'Add python.exe to PATH')" }

    # --- pipx ---------------------------------------------------------------
    $Pipx = $null
    $explicit = Get-Setting "KAROX_PIPX" ""
    if ($explicit) {
        $Pipx = @($explicit)
    } elseif (Get-Command "pipx" -ErrorAction SilentlyContinue) {
        $Pipx = @("pipx")
    } elseif ($null -ne (Invoke-Quiet ($Python + @("-m", "pipx", "--version")))) {
        $Pipx = $Python + @("-m", "pipx")
    } else {
        Say "pipx not found; installing it for this user"
        Invoke-Checked ($Python + @("-m", "pip", "install", "--user", "--quiet", "pipx"))
        $Pipx = $Python + @("-m", "pipx")
    }

    # --- What is installed ----------------------------------------------------
    $listing = Invoke-Quiet ($Pipx + @("list", "--json"))
    $mainVersion = ""
    $sideVersion = ""
    if ($listing) {
        try {
            $venvs = (ConvertFrom-Json $listing).venvs
            if ($venvs.'karox-runtime') { $mainVersion = [string]$venvs.'karox-runtime'.metadata.main_package.package_version }
            if ($venvs.'karox-runtime6') { $sideVersion = [string]$venvs.'karox-runtime6'.metadata.main_package.package_version }
        } catch {
            $mainVersion = ""
            $sideVersion = ""
        }
    }

    $external = $false
    if (-not $mainVersion -and (Get-Command "karox" -ErrorAction SilentlyContinue)) {
        $reported = Invoke-Quiet @("karox", "--version")
        if ($reported -match '(^|\s)[456]\.') { $external = $true }
    }
    if ($sideVersion) {
        Say "an old side-by-side karox6 install ($sideVersion) is present; leaving it untouched"
    }

    $stamp = ""
    if ($mainVersion -or $external) {
        $existing = if ($mainVersion) { $mainVersion } else { "outside pipx" }
        if (-not $Upgrade) {
            Say "KaroX $existing is already installed; nothing changed."
            Say "Back up the data and set KAROX_UPGRADE=1 to install $Spec over it."
            return
        }
        Say "KAROX_UPGRADE=1: backing up KaroX data before replacing $existing"
        $stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        $seen = @{}
        $candidates = @()
        if ($env:KAROX_CONFIG_DIR) { $candidates += $env:KAROX_CONFIG_DIR }
        if ($env:APPDATA) { $candidates += (Join-Path $env:APPDATA "KaroX") }
        if ($env:KAROX_RUNTIME_DIR) { $candidates += $env:KAROX_RUNTIME_DIR }
        if ($env:LOCALAPPDATA) { $candidates += (Join-Path $env:LOCALAPPDATA "KaroX\vnext") }
        foreach ($source in $candidates) {
            if (-not (Test-Path -LiteralPath $source)) { continue }
            $resolved = (Resolve-Path -LiteralPath $source).Path
            if ($seen.ContainsKey($resolved)) { continue }
            $seen[$resolved] = $true
            $destination = "$resolved.backup-$stamp"
            if ($DryRun) {
                Say "would copy $resolved -> $destination"
            } else {
                Copy-Item -LiteralPath $resolved -Destination $destination -Recurse -Force
                Say "backed up $resolved -> $destination"
            }
        }
    }

    # --- Install -------------------------------------------------------------
    $install = $Pipx + @("install")
    if ($mainVersion -or $external) { $install += "--force" }
    Invoke-Checked ($install + @($Spec))
    Say "done. Run: karox --version   (if the command is not found: pipx ensurepath, then open a new terminal)"
    if ($stamp) {
        Say "rollback: restore the .backup-$stamp directories before reinstalling the previous wheel"
    }
}

Install-KaroX
