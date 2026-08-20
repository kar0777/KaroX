"""Security and portability contracts for KaroX's no-system-Python launchers."""
from __future__ import annotations

from _support import ROOT


def _read(name: str) -> str:
    return (ROOT / "scripts" / name).read_text(encoding="utf-8-sig")


def test_posix_portable_launcher_uses_bundled_uv_and_managed_python() -> None:
    text = _read("portable_launch.sh")
    assert 'UV="$BUNDLE_DIR/uv"' in text
    assert '"$UV" --no-config venv --managed-python --python 3.12 "$VENV_DIR"' in text
    assert 'exec "$PYTHON" -m karox.cli "$@"' in text
    assert "command -v python" not in text
    assert "python3" not in text


def test_windows_portable_launcher_uses_bundled_uv_and_managed_python() -> None:
    text = _read("portable_launch.ps1")
    assert 'Join-Path $BundleDir "uv.exe"' in text
    assert "--no-config venv --managed-python --python 3.12" in text
    assert "& $Python -m karox.cli @args" in text
    assert "Get-Command python" not in text
    assert "winget" not in text.lower()


def test_portable_launchers_do_not_download_or_execute_remote_bootstrap_code() -> None:
    shell = _read("portable_launch.sh")
    powershell = _read("portable_launch.ps1")
    for text in (shell, powershell):
        lowered = text.lower()
        assert "curl " not in lowered
        assert "wget " not in lowered
        assert "invoke-webrequest" not in lowered
        assert "invoke-restmethod" not in lowered
        assert "iex" not in lowered


def test_portable_launchers_ignore_ambient_package_indexes_and_python_mirrors() -> None:
    shell = _read("portable_launch.sh")
    powershell = _read("portable_launch.ps1")
    for name in (
        "UV_INDEX_URL",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
        "UV_PYTHON_INSTALL_MIRROR",
        "UV_PYTHON_CPYTHON_MIRROR",
    ):
        assert name in shell
        assert name in powershell
    assert "--no-config pip install" in shell
    assert "--no-config pip install" in powershell
    assert "--no-build" in shell
    assert "--no-build" in powershell


def test_portable_launchers_keep_runtime_and_config_outside_bundle() -> None:
    shell = _read("portable_launch.sh")
    powershell = _read("portable_launch.ps1")
    assert "KAROX_RUNTIME_DIR" in shell and "KAROX_CONFIG_DIR" in shell
    assert "KAROX_RUNTIME_DIR" in powershell and "KAROX_CONFIG_DIR" in powershell
    assert "portable-wheel.txt" in shell
    assert "portable-wheel.txt" in powershell
