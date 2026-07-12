param(
    [string]$Repository = "kar0777/KaroX",
    [string]$Branch = "v3.14.2",
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
try { chcp.com 65001 > $null; [Console]::InputEncoding = [Text.Encoding]::UTF8; [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

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
    if (!(Test-Path -LiteralPath $installer)) { throw "install.karox.ps1 was not found after extraction." }
    if ($env:KAROX_NO_START -eq "1") {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $installer
    } else {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $installer -Start
    }
    exit $LASTEXITCODE
}
finally {
    Remove-Item -LiteralPath $ZipPath -Force -ErrorAction SilentlyContinue
    Remove-PathIfExists $ExtractDir
}
