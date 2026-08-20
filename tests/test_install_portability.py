"""Release contracts for a low-friction KaroX v5 installation.

These tests are intentionally platform-neutral.  They do not execute an installer
or touch the user's machine; they pin the source-level promises every Windows,
macOS, and Linux packaging path must preserve.
"""
from __future__ import annotations

from pathlib import Path

from _support import ROOT


def _text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8-sig")


def test_posix_installer_uses_project_metadata_as_single_dependency_source() -> None:
    text = _text("install.karox.sh")
    assert 'pip install --upgrade "$ROOT"' in text
    assert 'pip install -r "$ROOT/requirements.txt"' not in text
    assert "--no-deps" not in text


def test_windows_installer_uses_project_metadata_as_single_dependency_source() -> None:
    text = _text("install.karox.ps1")
    assert "-m pip install --upgrade $Root" in text
    assert 'pip install -r (Join-Path $Root "requirements.txt")' not in text
    assert "--no-deps" not in text


def test_playwright_is_a_normal_runtime_dependency_not_a_required_extra() -> None:
    pyproject = _text("pyproject.toml")
    requirements = _text("requirements.txt")
    assert '"playwright>=1.50,<2.0"' in pyproject
    assert "playwright>=1.50,<2.0" in requirements
    assert "browser = []" in pyproject


def test_browser_extension_assets_are_packaged_with_the_wheel() -> None:
    pyproject = _text("pyproject.toml")
    assert '"karox.browser_extension" = ["*.json", "*.js", "*.html", "*.css"]' in pyproject


def test_product_doctor_treats_browser_stack_as_product_dependencies() -> None:
    doctor = _text("scripts/product_doctor.py")
    for dependency in ("keyring", "playwright", "textual"):
        assert f'"{dependency}"' in doctor
    assert "KaroX will provision it automatically on first autonomous browser use" in doctor


def test_old_manual_playwright_command_is_not_part_of_primary_browser_runtime() -> None:
    access = _text("src/karox/browser_access.py")
    assert "install the 'browser' extra" not in access
    assert "ensure_playwright_chromium()" in access
    assert "self.policy.headed and self.policy.user_takeover" in access


def test_new_portability_modules_are_source_files_not_machine_generated_artifacts() -> None:
    for relative in (
        "src/karox/browser_bootstrap.py",
        "src/karox/browser_credentials.py",
        "src/karox/browser_credential_injection.py",
    ):
        path = ROOT / relative
        assert path.is_file(), relative
        assert path.stat().st_size > 500, relative
