param(
    [ValidateSet("tailscale", "cloudflare")]
    [string]$Tunnel = "tailscale",

    [ValidateRange(1024, 65535)]
    [int]$Port = 8766
)

$ErrorActionPreference = "Stop"

try {
    chcp.com 65001 > $null
    [Console]::InputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
    $OutputEncoding = [System.Text.Encoding]::UTF8
} catch {}

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$SourceDir = Join-Path $Root "src"
$PythonExe = Join-Path $env:LOCALAPPDATA "KaroX\.venv\Scripts\python.exe"

if (!(Test-Path -LiteralPath $PythonExe)) {
    foreach ($name in @("py", "python", "python3")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if (!$command) { continue }
        if ($name -eq "py") {
            try {
                $candidate = (& py -3 -c "import sys; print(sys.executable)").Trim()
                if ($LASTEXITCODE -eq 0 -and $candidate) {
                    $PythonExe = $candidate
                    break
                }
            } catch {}
        } else {
            $PythonExe = $command.Source
            break
        }
    }
}
if (!(Test-Path -LiteralPath $PythonExe)) {
    throw "Python 3.10+ не найден. Сначала установите KaroX или Python."
}
if (!(Test-Path -LiteralPath (Join-Path $SourceDir "karox\cli.py"))) {
    throw "Исходники KaroX не найдены: $SourceDir"
}

$env:PYTHONPATH = if ($env:PYTHONPATH) { "$SourceDir;$env:PYTHONPATH" } else { $SourceDir }
$env:KAROX_UI_LANGUAGE = "ru"
if (!$env:KAROX_CONFIG_DIR) { $env:KAROX_CONFIG_DIR = Join-Path $env:APPDATA "KaroX" }
if (!$env:KAROX_RUNTIME_DIR) { $env:KAROX_RUNTIME_DIR = Join-Path $env:LOCALAPPDATA "KaroX" }

$ImportedPath = (& $PythonExe -c "import pathlib, karox; print(pathlib.Path(karox.__file__).resolve())").Trim()
if ($LASTEXITCODE -ne 0) { throw "Не удалось импортировать KaroX из текущего src." }
$ExpectedPackageDir = [IO.Path]::GetFullPath((Join-Path $SourceDir "karox")).TrimEnd('\')
$ResolvedImport = [IO.Path]::GetFullPath($ImportedPath)
if (!$ResolvedImport.StartsWith($ExpectedPackageDir + "\", [StringComparison]::OrdinalIgnoreCase)) {
    throw "Запущена старая копия KaroX: $ResolvedImport. Ожидалась рабочая ветка: $ExpectedPackageDir"
}

$ProfileName = "full-dev"
$Tools = @(
    "karox.repo.read_file",
    "karox.repo.read_lines",
    "karox.repo.list_files",
    "karox.repo.search",
    "karox.repo.edit_file",
    "karox.repo.write_file",
    "karox.git.status",
    "karox.git.diff",
    "karox.git.log",
    "karox.checks.run"
)
$VerificationCommands = @(
    '["python","scripts/run_v5_preflight.py","--full","--keep-going"]',
    '["python","-m","unittest","discover","-s","tests","-p","test_web_bridge_profiles.py","-v"]',
    '["python","-m","unittest","discover","-s","tests","-p","test_web_bridge_launcher.py","-v"]',
    '["python","-m","unittest","discover","-s","tests","-p","test_hosted_bridge.py","-v"]',
    '["python","-m","unittest","discover","-s","tests","-p","test_tui.py","-v"]',
    '["python","-m","ruff","check","src","tests","scripts"]',
    '["python","-m","mypy","src/karox"]'
)

function Invoke-KaroX(
    [string[]]$Arguments,
    [switch]$Quiet
) {
    if ($Quiet) {
        & $PythonExe -m karox.cli @Arguments *> $null
    } else {
        & $PythonExe -m karox.cli @Arguments | Out-Host
    }
    return [int]$LASTEXITCODE
}

$showCode = Invoke-KaroX @("bridge", "saved", "show", $ProfileName, "--json") -Quiet
$ProfileArguments = @(
    "bridge", "saved",
    $(if ($showCode -eq 0) { "edit" } else { "create" }),
    $ProfileName,
    "--target-profile", "chatgpt-web",
    "--repository", $Root,
    "--write",
    "--tunnel", $Tunnel,
    "--port", ([string]$Port),
    "--deadline-preset", "full-suite",
    "--language", "ru"
)
foreach ($tool in $Tools) {
    $ProfileArguments += @("--tool", $tool)
}
foreach ($command in $VerificationCommands) {
    $ProfileArguments += @("--verification-command", $command)
}
$ProfileArguments += "--json"

$profileCode = Invoke-KaroX $ProfileArguments
if ($profileCode -ne 0) {
    throw "Не удалось создать или обновить профиль $ProfileName. Код: $profileCode"
}

Write-Host ""
Write-Host "KaroX source : $ResolvedImport" -ForegroundColor DarkCyan
Write-Host "Profile      : $ProfileName" -ForegroundColor Green
Write-Host "Tunnel       : $Tunnel" -ForegroundColor Green
Write-Host "Port         : $Port" -ForegroundColor Green
Write-Host "Repository   : $Root" -ForegroundColor Green
Write-Host ""

& $PythonExe -m karox.cli bridge connect --saved $ProfileName
exit $LASTEXITCODE
