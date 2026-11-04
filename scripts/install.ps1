# KaroX 6 installer for Windows (PowerShell 5.1 or 7).
#
#   irm https://raw.githubusercontent.com/kar0777/KaroX/v6/plan-impl-20261104/scripts/install.ps1 | iex
#
# What it does
#   * finds Python >= 3.10 (the py launcher or python) and pipx (installs pipx
#     for the user if it is missing);
#   * KaroX 5 already installed (pipx venv "karox-runtime" with a 5.x version):
#     installs KaroX 6 next to it as "karox6" (pipx --suffix 6) with its own
#     data channel (KAROX_CHANNEL=dogfood), so v5, its bridge and its data in
#     %APPDATA%\KaroX stay untouched;
#   * KaroX 6 already installed: leaves it alone unless KAROX_REINSTALL=1;
#   * otherwise installs KaroX 6 as "karox".
#
# Settings (environment variables, so they also work with "irm | iex")
#   KAROX_SPEC          what to install (pip requirement or URL); default: the
#                       source archive of KAROX_REF, until 6.0 is on PyPI
#   KAROX_REF           git branch for the default KAROX_SPEC
#   KAROX_SIDE_BY_SIDE  auto (default) | 1 (always karox6) | 0 (never)
#   KAROX_REINSTALL     1 to replace an existing KaroX 6
#   KAROX_PIPX          pipx executable to use instead of discovering one
#   KAROX_DRY_RUN       1 to print the commands instead of running them

$ErrorActionPreference = "Stop"

function Get-Setting([string]$Name, [string]$Default) {
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { return $Default }
    return $value.Trim()
}

$Ref = Get-Setting "KAROX_REF" "v6/plan-impl-20261104"
$Spec = Get-Setting "KAROX_SPEC" "https://github.com/kar0777/KaroX/archive/refs/heads/$Ref.zip"
$SideBySide = Get-Setting "KAROX_SIDE_BY_SIDE" "auto"
$Reinstall = (Get-Setting "KAROX_REINSTALL" "0") -eq "1"
$DryRun = (Get-Setting "KAROX_DRY_RUN" "0") -eq "1"
$ChannelPth = "karox6_channel.pth"

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
    try {
        $output = & $Command[0] @rest 2>$null
        if ($LASTEXITCODE -ne 0) { return $null }
        return ($output | Out-String).Trim()
    } catch {
        return $null
    }
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
    $hasV5 = $mainVersion -match '^[45]\.'
    if (-not $hasV5 -and -not $mainVersion -and (Get-Command "karox" -ErrorAction SilentlyContinue)) {
        # KaroX 5 installed some other way (pip --user, an editable checkout).
        $reported = Invoke-Quiet @("karox", "--version")
        if ($reported -match '(^|\s)5\.') { $hasV5 = $true }
    }

    switch -Regex ($SideBySide.ToLowerInvariant()) {
        '^(1|yes|true)$' { $suffixed = $true }
        '^(0|no|false)$' {
            if ($hasV5) { Fail "KaroX 5 is installed; installing KaroX 6 without a suffix would replace it. Set KAROX_SIDE_BY_SIDE=1 or uninstall KaroX 5 first" }
            $suffixed = $false
        }
        default { $suffixed = $hasV5 }
    }

    if ($suffixed) { $venv = "karox-runtime6"; $command = "karox6"; $existing = $sideVersion }
    else { $venv = "karox-runtime"; $command = "karox"; $existing = $mainVersion }

    if ($existing -and -not $Reinstall) {
        Say "KaroX $existing is already installed as '$command' (pipx venv $venv); nothing changed."
        Say "Set KAROX_REINSTALL=1 to replace it with $Spec"
        return
    }

    # --- Install -------------------------------------------------------------
    $install = $Pipx + @("install")
    if ($existing) { $install += "--force" }
    if ($suffixed) {
        Say "KaroX 5 stays as 'karox'; installing KaroX 6 next to it as 'karox6'"
        Invoke-Checked ($install + @("--suffix", "6", $Spec))
        if ($DryRun) {
            Say "would write $ChannelPth (KAROX_CHANNEL=dogfood) into $venv"
        } else {
            $venvs = Invoke-Quiet ($Pipx + @("environment", "--value", "PIPX_LOCAL_VENVS"))
            $venvPython = if ($venvs) { Join-Path (Join-Path $venvs $venv) "Scripts\python.exe" } else { "" }
            if (-not $venvPython -or -not (Test-Path $venvPython)) {
                Fail "installed, but the karox6 venv was not found to set its data channel; set KAROX_CHANNEL=dogfood before running karox6"
            }
            $site = Invoke-Quiet @($venvPython, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])")
            if (-not $site) { Fail "could not locate site-packages of $venv" }
            $line = 'import os; os.environ.setdefault("KAROX_CHANNEL", "dogfood")'
            [System.IO.File]::WriteAllText((Join-Path $site $ChannelPth), $line + "`n", (New-Object System.Text.UTF8Encoding($false)))
            Say "karox6 keeps its data in the separate 'dogfood' channel"
        }
    } else {
        Invoke-Checked ($install + @($Spec))
    }

    Say "done. Run: $command --version   (if the command is not found: pipx ensurepath, then open a new terminal)"
}

Install-KaroX
