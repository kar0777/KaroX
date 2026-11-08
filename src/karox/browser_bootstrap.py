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

import hashlib
import importlib.util
import os
import re
import stat
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



def _playwright_package_available() -> bool:
    try:
        return importlib.util.find_spec("playwright") is not None
    except (ImportError, AttributeError, ValueError):
        return False


def _approved_preinstalled_chromium() -> Optional[Path]:
    """An explicitly approved, SHA-pinned offline browser; never a task executable.

    The user/host must provide BOTH variables before launching this Karo process.
    A path or a digest alone grants nothing. The runtime checks the actual bytes,
    not a basename, the model's stdout, or a mutable PATH entry.
    """

    name = os.environ.get("KAROX_APPROVED_CHROMIUM_EXECUTABLE", "")
    digest = os.environ.get("KAROX_APPROVED_CHROMIUM_SHA256", "")
    if not name or not re.fullmatch(r"[0-9a-f]{64}", digest):
        return None
    try:
        path = Path(name)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            return None
        resolved = path.resolve(strict=True)
        if path != resolved or resolved.is_relative_to(Path.cwd().resolve()):
            return None  # Never execute a browser supplied in the agent workspace.
        info = path.stat()
        if (not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK)
                or not 1_000_000 <= info.st_size <= 600_000_000):
            return None
        with path.open("rb") as stream:
            actual = hashlib.file_digest(stream, "sha256").hexdigest()
        return resolved if actual == digest else None
    except (OSError, ValueError):
        return None


def _chromium_executable() -> Optional[Path]:
    """Resolve the preinstalled browser without provisioning or downloading it."""

    if not _playwright_package_available():
        return None
    try:
        from playwright.sync_api import sync_playwright

        playwright = sync_playwright().start()
        try:
            path = Path(playwright.chromium.executable_path).expanduser()
        finally:
            playwright.stop()
        if path.is_file():
            return path.resolve()
    except Exception:
        pass
    return _approved_preinstalled_chromium()


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
