$ErrorActionPreference = "Stop"

# KaroX v5 portable bootstrap. Shipped beside a pinned uv.exe and exactly one
# karox_runtime wheel. No system Python is required.
$BundleDir = $PSScriptRoot
$Uv = Join-Path $BundleDir "uv.exe"
if (!(Test-Path -LiteralPath $Uv)) { throw "KaroX portable runtime is incomplete: bundled uv.exe is missing." }
$Wheels = @(Get-ChildItem -LiteralPath $BundleDir -Filter "karox_runtime-*.whl" -File)
if ($Wheels.Count -ne 1) { throw "KaroX portable runtime must contain exactly one wheel." }
$Wheel = $Wheels[0].FullName
$WheelName = $Wheels[0].Name

$RuntimeDir = if ($env:KAROX_RUNTIME_DIR) { $env:KAROX_RUNTIME_DIR } else { Join-Path $env:LOCALAPPDATA "KaroX" }
$ConfigDir = if ($env:KAROX_CONFIG_DIR) { $env:KAROX_CONFIG_DIR } else { Join-Path $env:APPDATA "KaroX" }
$VenvDir = Join-Path $RuntimeDir ".venv"
$Python = Join-Path $VenvDir "Scripts\python.exe"
$Marker = Join-Path $RuntimeDir "portable-wheel.txt"

New-Item -ItemType Directory -Force -Path $RuntimeDir, $ConfigDir, (Join-Path $RuntimeDir "uv-cache"), (Join-Path $RuntimeDir "python") | Out-Null
$env:KAROX_RUNTIME_DIR = $RuntimeDir
$env:KAROX_CONFIG_DIR = $ConfigDir
$env:UV_CACHE_DIR = Join-Path $RuntimeDir "uv-cache"
$env:UV_PYTHON_INSTALL_DIR = Join-Path $RuntimeDir "python"

# Ignore ambient package-index/mirror configuration while preserving proxy and
# certificate variables used by managed networks.
foreach ($name in @(
    "UV_INDEX", "UV_DEFAULT_INDEX", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL", "UV_FIND_LINKS",
    "PIP_INDEX_URL", "PIP_EXTRA_INDEX_URL", "PIP_FIND_LINKS", "PIP_CONFIG_FILE",
    "UV_PYTHON_INSTALL_MIRROR", "UV_PYTHON_CPYTHON_MIRROR", "UV_PYTHON_PYPY_MIRROR"
)) {
    Remove-Item -LiteralPath "Env:$name" -ErrorAction SilentlyContinue
}

if (!(Test-Path -LiteralPath $Python)) {
    & $Uv --no-config venv --managed-python --python 3.12 $VenvDir
    if ($LASTEXITCODE -ne 0) { throw "KaroX could not prepare its managed Python runtime." }
}

$Installed = if (Test-Path -LiteralPath $Marker) { (Get-Content -Raw -LiteralPath $Marker).Trim() } else { "" }
if ($Installed -ne $WheelName) {
    & $Uv --no-config pip install --python $Python --no-build --upgrade $Wheel
    if ($LASTEXITCODE -ne 0) { throw "KaroX portable wheel installation failed." }
    Set-Content -LiteralPath $Marker -Value $WheelName -Encoding UTF8
}

& $Python -m karox.cli @args
exit $LASTEXITCODE
