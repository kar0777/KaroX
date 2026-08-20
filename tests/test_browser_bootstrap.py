"""Cross-platform contracts for one-time managed Chromium provisioning."""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from karox.browser_bootstrap import (
    BrowserBootstrapError,
    _installer_environment,
    ensure_playwright_chromium,
    install_playwright_chromium,
)


def test_installer_environment_preserves_proxy_but_drops_download_redirects() -> None:
    env = _installer_environment(
        {
            "HTTPS_PROXY": "http://proxy.example.invalid:8080",
            "REQUESTS_CA_BUNDLE": "/tmp/corp.pem",
            "PLAYWRIGHT_BROWSERS_PATH": "/tmp/ms-playwright",
            "PLAYWRIGHT_DOWNLOAD_HOST": "https://attacker.invalid",
            "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST": "https://attacker.invalid/chromium",
            "NODE_OPTIONS": "--require ./inject.js",
        }
    )
    assert env["HTTPS_PROXY"] == "http://proxy.example.invalid:8080"
    assert env["REQUESTS_CA_BUNDLE"] == "/tmp/corp.pem"
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == "/tmp/ms-playwright"
    assert "PLAYWRIGHT_DOWNLOAD_HOST" not in env
    assert "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST" not in env
    assert "NODE_OPTIONS" not in env


def test_missing_python_package_is_reported_without_running_installer() -> None:
    with patch("karox.browser_bootstrap._playwright_package_available", return_value=False):
        with patch("karox.browser_bootstrap.subprocess.run") as run:
            status = install_playwright_chromium()
    assert status.ready is False
    assert status.error == "playwright_package_missing"
    run.assert_not_called()


def test_existing_chromium_is_reused_without_install(tmp_path: Path) -> None:
    chromium = tmp_path / "chromium"
    chromium.write_bytes(b"")
    with patch("karox.browser_bootstrap._playwright_package_available", return_value=True):
        with patch("karox.browser_bootstrap._chromium_executable", return_value=chromium):
            with patch("karox.browser_bootstrap.subprocess.run") as run:
                status = install_playwright_chromium()
    assert status.ready is True
    assert status.installed_now is False
    assert status.executable == str(chromium)
    run.assert_not_called()


def test_install_uses_current_python_without_shell_and_hides_output(tmp_path: Path) -> None:
    chromium = tmp_path / "chromium"
    chromium.write_bytes(b"")
    with patch("karox.browser_bootstrap._playwright_package_available", return_value=True):
        with patch(
            "karox.browser_bootstrap._chromium_executable",
            side_effect=[None, chromium],
        ):
            with patch(
                "karox.browser_bootstrap.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0),
            ) as run:
                status = install_playwright_chromium(environment={"HTTPS_PROXY": "proxy"})

    assert status.ready is True
    assert status.installed_now is True
    kwargs = run.call_args.kwargs
    argv = run.call_args.args[0]
    assert argv[1:] == ["-m", "playwright", "install", "chromium"]
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"] is subprocess.DEVNULL
    assert kwargs["stderr"] is subprocess.DEVNULL
    assert kwargs["env"]["HTTPS_PROXY"] == "proxy"


def test_failed_install_returns_compact_safe_status() -> None:
    with patch("karox.browser_bootstrap._playwright_package_available", return_value=True):
        with patch("karox.browser_bootstrap._chromium_executable", return_value=None):
            with patch(
                "karox.browser_bootstrap.subprocess.run",
                return_value=subprocess.CompletedProcess([], 1),
            ):
                status = install_playwright_chromium()
    assert status.ready is False
    assert status.error == "chromium_install_failed"
    assert "stdout" not in repr(status)
    assert "stderr" not in repr(status)


def test_timeout_returns_retryable_status() -> None:
    with patch("karox.browser_bootstrap._playwright_package_available", return_value=True):
        with patch("karox.browser_bootstrap._chromium_executable", return_value=None):
            with patch(
                "karox.browser_bootstrap.subprocess.run",
                side_effect=subprocess.TimeoutExpired(["python"], 1),
            ):
                status = install_playwright_chromium(timeout_seconds=1)
    assert status.error == "chromium_install_timeout"


def test_ensure_raises_safe_error_when_bootstrap_cannot_finish() -> None:
    with patch("karox.browser_bootstrap.install_playwright_chromium") as install:
        install.return_value = type(
            "Status",
            (),
            {"ready": False, "executable": None, "error": "chromium_install_failed"},
        )()
        with pytest.raises(BrowserBootstrapError) as caught:
            ensure_playwright_chromium()
    assert "chromium_install_failed" in str(caught.value)


@pytest.mark.parametrize("timeout", [0, -1])
def test_invalid_timeout_is_rejected(timeout: float) -> None:
    with patch("karox.browser_bootstrap._playwright_package_available", return_value=True):
        with patch("karox.browser_bootstrap._chromium_executable", return_value=None):
            with pytest.raises(ValueError, match="timeout"):
                install_playwright_chromium(timeout_seconds=timeout)
