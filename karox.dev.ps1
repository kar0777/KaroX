param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$KaroXArgs
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
$InstalledPython = Join-Path $env:LOCALAPPDATA "KaroX\.venv\Scripts\python.exe"

function Find-KaroXDevPython {
    if (Test-Path -LiteralPath $InstalledPython) {
        return $InstalledPython
    }
    foreach ($name in @("py", "python", "python3")) {
        $command = Get-Command $name -ErrorAction SilentlyContinue
        if (!$command) { continue }
        if ($name -eq "py") {
            try {
                $resolved = (& py -3 -c "import sys; print(sys.executable)").Trim()
                if ($LASTEXITCODE -eq 0 -and $resolved) { return $resolved }
            } catch {}
        } else {
            return $command.Source
        }
    }
    throw "Python 3.10+ не найден. Сначала установите KaroX или Python."
}

if (!(Test-Path -LiteralPath (Join-Path $SourceDir "karox\cli.py"))) {
    throw "Рабочие исходники KaroX не найдены: $SourceDir"
}

$PythonExe = Find-KaroXDevPython
$PreviousPythonPath = $env:PYTHONPATH
$PreviousConfigDir = $env:KAROX_CONFIG_DIR
$PreviousRuntimeDir = $env:KAROX_RUNTIME_DIR

try {
    $env:PYTHONPATH = if ($PreviousPythonPath) {
        "$SourceDir;$PreviousPythonPath"
    } else {
        $SourceDir
    }
    if (!$env:KAROX_CONFIG_DIR) {
        $env:KAROX_CONFIG_DIR = Join-Path $env:APPDATA "KaroX"
    }
    if (!$env:KAROX_RUNTIME_DIR) {
        $env:KAROX_RUNTIME_DIR = Join-Path $env:LOCALAPPDATA "KaroX"
    }

    $ImportedPath = (& $PythonExe -c "import pathlib, karox; print(pathlib.Path(karox.__file__).resolve())").Trim()
    if ($LASTEXITCODE -ne 0) {
        throw "Не удалось импортировать KaroX из рабочей ветки."
    }

    $ExpectedPackageDir = [IO.Path]::GetFullPath((Join-Path $SourceDir "karox")).TrimEnd('\')
    $ResolvedImport = [IO.Path]::GetFullPath($ImportedPath)
    if (!$ResolvedImport.StartsWith($ExpectedPackageDir + "\", [StringComparison]::OrdinalIgnoreCase)) {
        throw "Запущена не рабочая копия KaroX: $ResolvedImport. Ожидалось внутри $ExpectedPackageDir"
    }

    Write-Host "KaroX dev source: $ResolvedImport" -ForegroundColor DarkCyan
    & $PythonExe -m karox.cli @KaroXArgs
    exit $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $PreviousPythonPath
    $env:KAROX_CONFIG_DIR = $PreviousConfigDir
    $env:KAROX_RUNTIME_DIR = $PreviousRuntimeDir
}
