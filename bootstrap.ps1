param(
    [string]$Repository = "kar0777/KaroX",
    [string]$Branch = "",
    [switch]$Clean,
    [switch]$ResolveOnly
)

$ErrorActionPreference = "Stop"
try { chcp.com 65001 > $null; [Console]::InputEncoding = [Text.Encoding]::UTF8; [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

if (-not $Branch -and $env:KAROX_BOOTSTRAP_REF) { $Branch = $env:KAROX_BOOTSTRAP_REF }
if (-not $Branch) {
    try {
        $releaseStatus = Invoke-RestMethod -UseBasicParsing -Uri "https://raw.githubusercontent.com/$Repository/main/RELEASE.json" -TimeoutSec 15
        if ($releaseStatus.tag) { $Branch = [string]$releaseStatus.tag }
        elseif ($releaseStatus.version) { $Branch = "v" + [string]$releaseStatus.version }
    } catch {
        Write-Host "Could not resolve the latest release from RELEASE.json: $($_.Exception.Message)" -ForegroundColor Yellow
    }
}
if (-not $Branch) { $Branch = "main" }

if ($ResolveOnly) {
    Write-Output $Branch
    exit 0
}

$AppRoot = Join-Path $env:LOCALAPPDATA "KaroX"
$ConfigDir = Join-Path $env:APPDATA "KaroX"
$SourceDir = Join-Path $AppRoot "source"
$ZipPath = Join-Path $env:TEMP ("karox-" + [guid]::NewGuid().ToString("N") + ".zip")
$ExtractDir = Join-Path $env:TEMP ("karox-" + [guid]::NewGuid().ToString("N"))
$ZipRefKind = if ($Branch -match "^v?\d+\.\d+\.\d+") { "tags" } else { "heads" }
$ZipUrl = "https://codeload.github.com/$Repository/zip/refs/$ZipRefKind/$Branch"

function Remove-PathIfExists($path) {
    if (Test-Path -LiteralPath $path) { Remove-Item -LiteralPath $path -Recurse -Force -ErrorAction SilentlyContinue }
}

function Install-PortableIfAvailable {
    if ($Branch -notmatch '^v5\.\d+\.\d+$') { return $false }
    $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()
    if ($arch -eq "X64") { $platform = "windows-x64" }
    elseif ($arch -eq "Arm64") { $platform = "windows-arm64" }
    else { return $false }

    $asset = "KaroX-$Branch-$platform-portable.zip"
    $checksumAsset = "KaroX-$Branch-SHA256SUMS.txt"
    $releaseBase = "https://github.com/$Repository/releases/download/$Branch"
    $archive = Join-Path $env:TEMP ("karox-portable-" + [Guid]::NewGuid().ToString("N") + ".zip")
    $checksums = Join-Path $env:TEMP ("karox-checksums-" + [Guid]::NewGuid().ToString("N") + ".txt")
    $extractRoot = Join-Path $env:TEMP ("karox-extract-" + [Guid]::NewGuid().ToString("N"))
    try {
        try {
            Invoke-WebRequest -UseBasicParsing -Uri "$releaseBase/$asset" -OutFile $archive
            Invoke-WebRequest -UseBasicParsing -Uri "$releaseBase/$checksumAsset" -OutFile $checksums
        } catch {
            return $false
        }

        $line = Get-Content -LiteralPath $checksums | Where-Object { $_.TrimEnd().EndsWith($asset) } | Select-Object -First 1
        if (!$line) { throw "Portable KaroX checksum entry is missing." }
        $expected = (($line -split '\s+')[0]).Trim().ToLowerInvariant()
        if ($expected -notmatch '^[0-9a-f]{64}$') { throw "Portable KaroX checksum entry is invalid." }
        $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $archive).Hash.ToLowerInvariant()
        if ($actual -ne $expected) { throw "Portable KaroX archive checksum mismatch; refusing to install." }

        Expand-Archive -LiteralPath $archive -DestinationPath $extractRoot -Force
        $bundleDirs = @(Get-ChildItem -LiteralPath $extractRoot -Directory)
        if ($bundleDirs.Count -ne 1) { throw "Portable KaroX archive has an unexpected layout." }
        $candidateDir = $bundleDirs[0].FullName
        $candidateLauncher = Join-Path $candidateDir "karox.cmd"
        $candidateUv = Join-Path $candidateDir "uv.exe"
        if (!(Test-Path -LiteralPath $candidateLauncher) -or !(Test-Path -LiteralPath $candidateUv)) {
            throw "Portable KaroX launcher or uv runtime is missing."
        }

        $portableDir = Join-Path $AppRoot "portable"
        $backupDir = Join-Path $AppRoot (".portable-old-" + [Guid]::NewGuid().ToString("N"))
        $launcher = Join-Path $AppRoot "KaroX.cmd"
        if (Test-Path -LiteralPath $portableDir) { Move-Item -LiteralPath $portableDir -Destination $backupDir }
        try {
            Move-Item -LiteralPath $candidateDir -Destination $portableDir
            $portableLauncher = Join-Path $portableDir "karox.cmd"
            Set-Content -LiteralPath $launcher -Encoding ASCII -Value "@echo off`r`ncall `"$portableLauncher`" %*"
        } catch {
            Remove-PathIfExists $portableDir
            if (Test-Path -LiteralPath $backupDir) { Move-Item -LiteralPath $backupDir -Destination $portableDir }
            throw
        }
        Remove-PathIfExists $backupDir
        $portableLauncher = Join-Path $portableDir "karox.cmd"
        try {
            $desktop = [Environment]::GetFolderPath("Desktop")
            if ($desktop) {
                $shell = New-Object -ComObject WScript.Shell
                $shortcut = $shell.CreateShortcut((Join-Path $desktop "KaroX.lnk"))
                $shortcut.TargetPath = $launcher
                $shortcut.WorkingDirectory = $portableDir
                $shortcut.Save()
            }
        } catch {}
        Write-Host "Portable KaroX installed: $portableDir"
        Write-Host "Launcher: $launcher"
        return $true
    }
    finally {
        Remove-Item -LiteralPath $archive -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $checksums -Force -ErrorAction SilentlyContinue
        Remove-PathIfExists $extractRoot
    }
}

Write-Host ""
Write-Host "KaroX stable installer" -ForegroundColor Cyan
Write-Host "----------------------------------------" -ForegroundColor DarkCyan
Write-Host "Repository : https://github.com/$Repository"
Write-Host "Release/ref: $Branch"
Write-Host ""

if ($Clean) {
    Remove-PathIfExists $ConfigDir
    Remove-PathIfExists $AppRoot
}
New-Item -ItemType Directory -Force -Path $AppRoot | Out-Null

if (Install-PortableIfAvailable) {
    $launcher = Join-Path $AppRoot "KaroX.cmd"
    if ($env:KAROX_NO_START -eq "1") { exit 0 }
    & $launcher
    exit $LASTEXITCODE
}
if ($Branch -match '^v5\.\d+\.\d+$') {
    Write-Warning "Portable asset was not available; falling back to the source installer."
}

try {
    Invoke-WebRequest -UseBasicParsing -Uri $ZipUrl -OutFile $ZipPath
    Remove-PathIfExists $ExtractDir
    Expand-Archive -LiteralPath $ZipPath -DestinationPath $ExtractDir -Force
    $repoDir = Get-ChildItem -LiteralPath $ExtractDir -Directory | Select-Object -First 1
    if (!$repoDir) { throw "GitHub archive did not contain a project directory." }

    Remove-PathIfExists $SourceDir
    New-Item -ItemType Directory -Force -Path $SourceDir | Out-Null
    Get-ChildItem -LiteralPath $repoDir.FullName -Force | ForEach-Object {
        Copy-Item -LiteralPath $_.FullName -Destination (Join-Path $SourceDir $_.Name) -Recurse -Force
    }

    $installer = Join-Path $SourceDir "install.karox.ps1"
    $guard = Join-Path $SourceDir "scripts\install_guard.ps1"
    if (!(Test-Path -LiteralPath $installer)) { throw "install.karox.ps1 was not found after extraction." }
    if (!(Test-Path -LiteralPath $guard)) { throw "scripts\install_guard.ps1 was not found after extraction." }
    $guardArgs = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", $guard, "-Installer", $installer)
    if ($env:KAROX_NO_START -ne "1") { $guardArgs += "-Start" }
    & powershell @guardArgs
    exit $LASTEXITCODE
}
finally {
    Remove-Item -LiteralPath $ZipPath -Force -ErrorAction SilentlyContinue
    Remove-PathIfExists $ExtractDir
}
