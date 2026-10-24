"""Cross-platform one-time provisioning for KaroX's managed browser runtime.

KaroX v5 treats browser automation as a product feature rather than a developer
extra.  The Python Playwright package is installed with the normal wheel; this
module makes sure its matching Chromium binary exists when browser work is first
requested.  No shell is used, no package manager is invoked, and no platform-
specific path is hard-coded.

The install is intentionally lazy: a user who never enables browser tooling does
not pay the Chromium download cost during ``pip install``.  If the machine is
offline, the core runtime remains usable and browser startup fails with a compact
retryable diagnostic instead of an import traceback.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, MutableMapping, Optional


class BrowserBootstrapError(RuntimeError):
    """Managed browser provisioning failed without exposing command output."""


@dataclass(frozen=True)
class BrowserBootstrapStatus:
    ready: bool
    package_available: bool
    chromium_available: bool
    installed_now: bool = False
    executable: Optional[str] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, object]:
        return {
            "ready": self.ready,
            "package_available": self.package_available,
            "chromium_available": self.chromium_available,
            "installed_now": self.installed_now,
            "executable": self.executable,
            "error": self.error,
        }


def _playwright_package_available() -> bool:
    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _chromium_executable() -> Optional[Path]:
    """Return Playwright's installed Chromium executable without launching it."""

    if not _playwright_package_available():
        return None
    try:
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        try:
            path = Path(playwright.chromium.executable_path).expanduser()
        finally:
            playwright.stop()
    except Exception:
        return None
    return path.resolve() if path.is_file() else None


def playwright_chromium_status() -> BrowserBootstrapStatus:
    """Inspect the browser runtime without downloading or launching Chromium."""

    package_available = _playwright_package_available()
    if not package_available:
        return BrowserBootstrapStatus(
            ready=False,
            package_available=False,
            chromium_available=False,
            error="playwright_package_missing",
        )
    executable = _chromium_executable()
    if executable is None:
        return BrowserBootstrapStatus(
            ready=False,
            package_available=True,
            chromium_available=False,
            error="chromium_not_installed",
        )
    return BrowserBootstrapStatus(
        ready=True,
        package_available=True,
        chromium_available=True,
        executable=str(executable),
    )


def _installer_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """Build a conservative environment for the official Playwright installer.

    Proxy and CA configuration are preserved so corporate/offline-friendly
    environments still work.  Variables that can redirect Playwright to an
    arbitrary browser download host are removed; KaroX must not silently turn a
    repository-controlled environment change into executable-code download from
    an attacker-selected origin.
    """

    env: MutableMapping[str, str] = dict(source or os.environ)
    for name in (
        "PLAYWRIGHT_DOWNLOAD_HOST",
        "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST",
        "PLAYWRIGHT_FIREFOX_DOWNLOAD_HOST",
        "PLAYWRIGHT_WEBKIT_DOWNLOAD_HOST",
        "NODE_OPTIONS",
    ):
        env.pop(name, None)
    return dict(env)


def install_playwright_chromium(
    *,
    timeout_seconds: float = 1200.0,
    environment: Mapping[str, str] | None = None,
) -> BrowserBootstrapStatus:
    """Install Playwright's Chromium using this exact Python environment."""

    if not _playwright_package_available():
        return BrowserBootstrapStatus(
            ready=False,
            package_available=False,
            chromium_available=False,
            error="playwright_package_missing",
        )
    if timeout_seconds <= 0:
        raise ValueError("browser bootstrap timeout must be positive")

    before = _chromium_executable()
    if before is not None:
        return BrowserBootstrapStatus(
            ready=True,
            package_available=True,
            chromium_available=True,
            executable=str(before),
        )

    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            shell=False,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            ),
            timeout=float(timeout_seconds),
            check=False,
            env=_installer_environment(environment),
        )
    except subprocess.TimeoutExpired:
        return BrowserBootstrapStatus(
            ready=False,
            package_available=True,
            chromium_available=False,
            error="chromium_install_timeout",
        )
    except OSError:
        return BrowserBootstrapStatus(
            ready=False,
            package_available=True,
            chromium_available=False,
            error="chromium_installer_unavailable",
        )

    if result.returncode != 0:
        return BrowserBootstrapStatus(
            ready=False,
            package_available=True,
            chromium_available=False,
            error="chromium_install_failed",
        )

    after = _chromium_executable()
    if after is None:
        return BrowserBootstrapStatus(
            ready=False,
            package_available=True,
            chromium_available=False,
            error="chromium_missing_after_install",
        )
    return BrowserBootstrapStatus(
        ready=True,
        package_available=True,
        chromium_available=True,
        installed_now=True,
        executable=str(after),
    )


def ensure_playwright_chromium(*, timeout_seconds: float = 1200.0) -> Path:
    """Return a usable Chromium path, provisioning it once when necessary."""

    status = install_playwright_chromium(timeout_seconds=timeout_seconds)
    if not status.ready or not status.executable:
        raise BrowserBootstrapError(
            "KaroX could not prepare its managed Chromium runtime"
            + (f" ({status.error})" if status.error else "")
        )
    return Path(status.executable)


__all__ = [
    "BrowserBootstrapError",
    "BrowserBootstrapStatus",
    "ensure_playwright_chromium",
    "install_playwright_chromium",
    "playwright_chromium_status",
]
