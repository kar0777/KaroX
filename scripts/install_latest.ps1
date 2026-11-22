<#
.SYNOPSIS
Install or update KaroX 6 from its git branch into a venv, without a working KaroX.

.DESCRIPTION
Idempotent: creates the venv when it is missing, does nothing when the installed build
is already the branch's newest commit, and otherwise builds the branch in a temporary
clone, checks the wheel and installs it. The same steps as `karox6 update`.

  powershell -ExecutionPolicy Bypass -File scripts\install_latest.ps1
  powershell -File scripts\install_latest.ps1 -Venv D:\karox6-venv -Branch karox6/main
#>
param(
    [string]$Venv = $(if ($env:KAROX_VENV) { $env:KAROX_VENV } else { Join-Path $HOME "karox6-venv" }),
    [string]$Repo = $(if ($env:KAROX_UPDATE_REPO) { $env:KAROX_UPDATE_REPO } else { "https://github.com/kar0777/KaroX.git" }),
    [string]$Branch = $(if ($env:KAROX_UPDATE_BRANCH) { $env:KAROX_UPDATE_BRANCH } else { "karox6/main" }),
    [string]$Python = "",
    [switch]$Force
)
$ErrorActionPreference = "Continue"  # native tools report through exit codes

function Fail([string]$message) {
    Write-Host "install_latest: $message" -ForegroundColor Red
    exit 1
}

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Fail "git is not installed or not on PATH (https://git-scm.com)"
}
$py = Join-Path $Venv "Scripts\python.exe"
if (-not (Test-Path $py)) {
    if (-not $Python) {
        if (Get-Command py -ErrorAction SilentlyContinue) { $Python = "py" } else { $Python = "python" }
    }
    Write-Host "creating the venv $Venv ..."
    if ($Python -eq "py") { & py -3 -m venv $Venv } else { & $Python -m venv $Venv }
    if ($LASTEXITCODE -ne 0 -or -not (Test-Path $py)) { Fail "cannot create the venv $Venv (Python 3.12+ needed)" }
}

$head = (& git ls-remote $Repo "refs/heads/$Branch" 2>$null | Select-Object -First 1)
if (-not $head) { Fail "cannot read $Branch from $Repo" }
$head = ($head -split "\s+")[0]
$probe = "import karox6`ntry:`n from karox6 import _build as b; c = b.COMMIT`nexcept Exception:`n c = ''`nprint(karox6.__version__, c)"
$before = (& $py -c $probe 2>$null)
if ($LASTEXITCODE -ne 0) { $before = "" }
$oldVersion, $oldCommit = if ($before) { $before -split " ", 2 } else { "none", "" }
if (-not $Force -and $oldCommit -eq $head) {
    Write-Host "KaroX $oldVersion ($($head.Substring(0, 7))) is up to date."
    exit 0
}

$temp = Join-Path ([System.IO.Path]::GetTempPath()) ("karox6-install-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    $clone = Join-Path $temp "repo"
    Write-Host "fetching $Branch from $Repo ..."
    & git clone --quiet --filter=blob:none --single-branch --branch $Branch $Repo $clone
    if ($LASTEXITCODE -ne 0) { Fail "cannot clone $Branch from $Repo" }
    $commit = (& git -C $clone rev-parse HEAD).Trim()
    $build = "`"`"`"Written by install_latest.ps1: where this build came from.`"`"`"`n`nREPO = r'$Repo'`nBRANCH = r'$Branch'`nCOMMIT = r'$commit'`n"
    [System.IO.File]::WriteAllText((Join-Path $clone "src\karox6\_build.py"), $build)

    Write-Host "building ..."
    $dist = Join-Path $temp "dist"
    & $py -m pip wheel --disable-pip-version-check --no-deps --quiet --wheel-dir $dist $clone
    $wheel = Get-ChildItem $dist -Filter "karox6-*.whl" -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($LASTEXITCODE -ne 0 -or -not $wheel) { Fail "building the wheel failed (the installed KaroX was kept)" }

    Write-Host "checking the new build ..."
    $check = Join-Path $temp "check"
    & $py -m pip install --disable-pip-version-check --no-deps --no-compile --quiet --target $check $wheel.FullName
    $started = (& $py -I -c "import sys; sys.path.insert(0, sys.argv[1]); import karox6.cli as c; raise SystemExit(c.main(['--version']))" $check 2>&1)
    if ($LASTEXITCODE -ne 0) { Fail "the new build does not start: $started" }

    Write-Host "installing ..."
    & $py -m pip install --disable-pip-version-check --quiet $wheel.FullName
    if ($LASTEXITCODE -ne 0) { Fail "installing the dependencies failed (the installed KaroX was kept)" }
    # A running karox6.exe cannot be overwritten but can be renamed out of pip's way.
    $scripts = Join-Path $Venv "Scripts"
    $moved = @()
    foreach ($name in "karox6.exe", "karox.exe") {
        $exe = Join-Path $scripts $name
        if (Test-Path $exe) {
            $old = "$exe.$PID.old"
            try { Move-Item -Force $exe $old; $moved += , @($exe, $old) } catch { }
        }
    }
    & $py -m pip install --disable-pip-version-check --quiet --force-reinstall --no-deps $wheel.FullName
    if ($LASTEXITCODE -ne 0) {
        foreach ($pair in $moved) { if (-not (Test-Path $pair[0])) { Move-Item $pair[1] $pair[0] } }
        Fail "installing the new build failed (the installed KaroX was kept)"
    }
    foreach ($pair in $moved) { Remove-Item -Force $pair[1] -ErrorAction SilentlyContinue }
    $after = (& (Join-Path $scripts "karox6.exe") --version)
    if ($LASTEXITCODE -ne 0) { Fail "karox6 --version fails after the install" }
    Write-Host "KaroX: $oldVersion -> $($after -replace '^karox ', '') ($($commit.Substring(0, 7)))"
    if ($oldCommit) {
        & git -C $clone log --no-merges --format="  %h %s" -20 "$oldCommit..HEAD" 2>$null
    }
}
finally {
    Remove-Item -Recurse -Force $temp -ErrorAction SilentlyContinue
}
