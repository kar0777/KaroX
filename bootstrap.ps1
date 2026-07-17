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
