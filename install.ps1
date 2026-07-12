param([switch]$Start)
$ErrorActionPreference = "Stop"
$installer = Join-Path $PSScriptRoot "install.karox.ps1"
if (!(Test-Path -LiteralPath $installer)) { throw "install.karox.ps1 is missing." }
if ($Start) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $installer -Start
} else {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $installer
}
exit $LASTEXITCODE
