"""Portable-first bootstrap contracts without performing an installation."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from _support import ROOT

SH = ROOT / "bootstrap.sh"
PS = ROOT / "bootstrap.ps1"


def _posix_script_text() -> str:
    return SH.read_text(encoding="utf-8-sig").replace("\r", "")


def _working_bash() -> str | None:
    """A bash that can actually run scripts, or None.

    On Windows ``shutil.which("bash")`` often finds the WSL launcher
    ``C:\\WINDOWS\\system32\\bash.EXE``, which fails or hangs whenever no
    distribution is ready. A bash that cannot run ``true`` gets the same
    treatment as no bash at all: skip, do not fail the suite.
    """
    bash = shutil.which("bash")
    if bash is None:
        return None
    try:
        probe = subprocess.run(
            [bash, "-c", "true"], capture_output=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return bash if probe.returncode == 0 else None


def test_posix_bootstrap_prefers_stable_v5_portable_bundle_before_source_fallback() -> None:
    text = SH.read_text(encoding="utf-8-sig")
    assert text.index("portable_asset=") < text.index("SCRIPT_DIR=")
    assert "KaroX-${REF}-${portable_platform}-portable.tar.gz" in text
    assert "KaroX-${REF}-SHA256SUMS.txt" in text
    assert "sha256sum" in text and "shasum -a 256" in text
    assert "checksum mismatch; refusing to install" in text
    assert "falling back to the source installer" in text
    assert "portable_stage" in text and "portable_backup" in text


def test_windows_bootstrap_prefers_portable_bundle_and_rolls_back_activation() -> None:
    text = PS.read_text(encoding="utf-8-sig")
    assert text.index("function Install-PortableIfAvailable") < text.index("Invoke-WebRequest -UseBasicParsing -Uri $ZipUrl")
    assert '"windows-x64"' in text and '"windows-arm64"' in text
    assert "KaroX-$Branch-$platform-portable.zip" in text
    assert "Get-FileHash -Algorithm SHA256" in text
    assert "checksum mismatch; refusing to install" in text
    assert "$backupDir" in text
    assert "falling back to the source installer" in text


def test_portable_bootstraps_keep_source_fallback_for_nonstable_refs() -> None:
    shell = SH.read_text(encoding="utf-8-sig")
    powershell = PS.read_text(encoding="utf-8-sig")
    assert "install.karox.sh" in shell
    assert "install.karox.ps1" in powershell
    assert "codeload.github.com" in powershell
    assert "archive/refs/$REF_KIND/$REF.tar.gz" in shell


def test_posix_bootstrap_has_valid_bash_syntax_when_bash_is_available() -> None:
    bash = _working_bash()
    if bash is None:
        pytest.skip("no working bash on this test host")
    result = subprocess.run([bash, "-n"], input=_posix_script_text().encode("utf-8"), capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")


def test_powershell_bootstrap_parses_when_powershell_is_available() -> None:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is not installed on this test host")
    literal = str(PS).replace("'", "''")
    command = f"[scriptblock]::Create((Get-Content -Raw -LiteralPath '{literal}')) | Out-Null"
    result = subprocess.run([executable, "-NoProfile", "-Command", command], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def test_resolve_only_posix_path_does_not_install_or_use_network() -> None:
    bash = _working_bash()
    if bash is None:
        pytest.skip("no working bash on this test host")
    script = 'export KAROX_BOOTSTRAP_REF="v5.0.0"\n' + _posix_script_text()
    result = subprocess.run([bash, "-s", "--", "--resolve-only"], input=script.encode("utf-8"), capture_output=True, timeout=10)
    assert result.returncode == 0
    assert result.stdout.decode("utf-8", errors="replace").strip() == "v5.0.0"
