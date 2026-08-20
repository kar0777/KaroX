"""Supply-chain contracts for the optional Windows cloudflared bootstrap."""
from __future__ import annotations

import shutil
import subprocess

import pytest

from _support import ROOT


INSTALLER = ROOT / "install.karox.ps1"
PINNED_VERSION = "2026.7.3"
PINNED_WINDOWS_AMD64_SHA256 = "8635da433b6df8194746e88ed9d2589566c20e38bfc2a80e431a348b7c765841"


def _text() -> str:
    return INSTALLER.read_text(encoding="utf-8-sig")


def test_cloudflared_download_is_version_pinned_not_latest() -> None:
    text = _text()
    assert f'$cloudflaredPinnedVersion = "{PINNED_VERSION}"' in text
    assert "releases/latest/download/cloudflared" not in text
    assert "releases/download/$cloudflaredPinnedVersion/cloudflared-windows-amd64.exe" in text


def test_cloudflared_download_is_sha256_verified_before_move() -> None:
    text = _text()
    assert f'$cloudflaredPinnedSha256 = "{PINNED_WINDOWS_AMD64_SHA256}"' in text
    hash_check = text.index("Get-FileHash -Algorithm SHA256 -LiteralPath $partial")
    compare = text.index("$actualSha256 -ne $cloudflaredPinnedSha256")
    move = text.index("Move-Item -LiteralPath $partial -Destination $newCloudflared")
    assert hash_check < compare < move
    assert "downloaded cloudflared SHA-256 did not match" in text


def test_cloudflared_no_longer_trusts_download_size_as_identity() -> None:
    text = _text()
    assert "$size = (Get-Item -LiteralPath $partial).Length" not in text
    assert "which is not the cloudflared binary" not in text


def test_automatic_binary_download_is_x64_only_until_an_arm64_pin_exists() -> None:
    text = _text()
    assert "RuntimeInformation]::OSArchitecture" in text
    assert '$cloudflaredArchitecture -ne "X64"' in text
    assert "Automatic cloudflared download is pinned only for Windows x64" in text


def test_installer_still_parses_in_powershell() -> None:
    executable = shutil.which("pwsh") or shutil.which("powershell")
    if executable is None:
        pytest.skip("PowerShell is not installed on this test host")
    literal = str(INSTALLER).replace("'", "''")
    command = f"[scriptblock]::Create((Get-Content -Raw -LiteralPath '{literal}')) | Out-Null"
    result = subprocess.run(
        [executable, "-NoProfile", "-Command", command],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr
