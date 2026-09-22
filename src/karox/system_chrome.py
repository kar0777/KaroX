"""Launch a dedicated system-Chrome profile for headed KaroX browser sessions.

Playwright's bundled Chromium is excellent for deterministic verification, but
identity providers can refuse it as an automated browser.  This module launches
the user's installed Google Chrome as a normal external process, with a
KaroX-only persistent profile, and then attaches through the standard Chrome
DevTools Protocol (CDP).

The everyday Chrome profile is never opened.  Cookies in the dedicated profile
survive a KaroX restart, while the browser still routes traffic through KaroX's
session proxy.  A tiny unpacked extension supplies proxy credentials; it does
not read page contents or browsing history, and it does not override the user's
new-tab page.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .detached_process import windowless_flags
from .paths import runtime_dir


class SystemChromeError(RuntimeError):
    """Raised when the dedicated system-Chrome process cannot be started."""


@dataclass
class SystemChromeLaunch:
    browser: Any
    context: Any
    process: subprocess.Popen[Any]
    profile_dir: Path
    extension_dir: Path
    log_path: Path
    engine: str = "system_chrome_cdp"


def _chrome_candidates() -> tuple[Path, ...]:
    home = Path.home()
    values: list[Path] = []
    if os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        program_files = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        program_files_x86 = Path(
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        )
        values.extend(
            (
                local / "Google" / "Chrome" / "Application" / "chrome.exe",
                program_files / "Google" / "Chrome" / "Application" / "chrome.exe",
                program_files_x86 / "Google" / "Chrome" / "Application" / "chrome.exe",
            )
        )
    elif sys.platform == "darwin":
        values.extend(
            (
                Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                home
                / "Applications"
                / "Google Chrome.app"
                / "Contents"
                / "MacOS"
                / "Google Chrome",
            )
        )
    else:
        for name in ("google-chrome-stable", "google-chrome", "chrome", "chromium"):
            resolved = shutil.which(name)
            if resolved:
                values.append(Path(resolved))
    return tuple(dict.fromkeys(path.expanduser() for path in values))


def find_system_chrome() -> Path:
    override = os.environ.get("KAROX_CHROME_EXECUTABLE", "").strip()
    if override:
        candidate = Path(override).expanduser().resolve()
        if candidate.is_file():
            return candidate
        raise SystemChromeError("KAROX_CHROME_EXECUTABLE does not point to a file")
    for candidate in _chrome_candidates():
        if candidate.is_file():
            return candidate.resolve()
    raise SystemChromeError(
        "Google Chrome was not found; install Chrome or set KAROX_CHROME_EXECUTABLE"
    )


def chrome_profile_dir() -> Path:
    override = os.environ.get("KAROX_CHROME_PROFILE_DIR", "").strip()
    if override:
        root = Path(override).expanduser().resolve()
    else:
        root = (runtime_dir() / "vnext" / "browser-profiles" / "karo").resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _extension_manifest() -> str:
    payload = {
        "manifest_version": 3,
        "name": "KaroX Browser",
        "description": "Local KaroX browser profile and proxy authentication.",
        "version": "1.0.0",
        "permissions": ["webRequest", "webRequestAuthProvider"],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"},
        "action": {"default_title": "KaroX Browser"},
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _extension_background(username: str, password: str) -> str:
    user = json.dumps(username)
    secret = json.dumps(password)
    return f"""const PROXY_USERNAME = {user};
const PROXY_PASSWORD = {secret};

chrome.webRequest.onAuthRequired.addListener(
  (details, callback) => {{
    if (!details.isProxy) {{
      callback({{}});
      return;
    }}
    callback({{
      authCredentials: {{
        username: PROXY_USERNAME,
        password: PROXY_PASSWORD,
      }},
    }});
  }},
  {{ urls: ["<all_urls>"] }},
  ["asyncBlocking"],
);
"""


def write_karo_extension(username: str, password: str) -> Path:
    root = (runtime_dir() / "vnext" / "browser-extension").resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(_extension_manifest(), encoding="utf-8")
    (root / "background.js").write_text(
        _extension_background(username, password), encoding="utf-8"
    )
    return root


def _wait_for_devtools_port(
    profile_dir: Path,
    process: subprocess.Popen[Any],
    *,
    timeout_seconds: float = 20.0,
) -> int:
    marker = profile_dir / "DevToolsActivePort"
    deadline = time.monotonic() + timeout_seconds
    last_text = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SystemChromeError(
                f"Google Chrome exited before CDP became ready (exit {process.returncode})"
            )
        try:
            text = marker.read_text(encoding="utf-8").strip()
            last_text = text
            first = text.splitlines()[0]
            port = int(first)
            if 1 <= port <= 65535:
                return port
        except (FileNotFoundError, OSError, ValueError, IndexError):
            pass
        time.sleep(0.1)
    detail = f": {last_text[:120]}" if last_text else ""
    raise SystemChromeError(f"Google Chrome CDP endpoint did not become ready{detail}")


def terminate_chrome_process(process: Optional[subprocess.Popen[Any]]) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5.0)
        return
    except Exception:
        pass
    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10.0,
                check=False,
                creationflags=windowless_flags(),
            )
            return
        except Exception:
            pass
    try:
        process.kill()
    except Exception:
        pass


def activate_chrome_window(process: Optional[subprocess.Popen[Any]]) -> bool:
    """Restore and focus the top-level window owned by this Chrome process on Windows."""
    if process is None or process.poll() is not None or os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        windows: list[int] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        @callback_type
        def collect(hwnd: int, _lparam: int) -> bool:
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value == process.pid and user32.IsWindowVisible(hwnd):
                windows.append(int(hwnd))
            return True

        user32.EnumWindows(collect, 0)
        if not windows:
            return False
        hwnd = windows[0]
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        user32.BringWindowToTop(hwnd)
        return bool(user32.SetForegroundWindow(hwnd))
    except Exception:
        return False


def launch_system_chrome(
    playwright_ctx: Any,
    *,
    proxy_server: str,
    proxy_username: str,
    proxy_password: str,
    width: int,
    height: int,
) -> SystemChromeLaunch:
    executable = find_system_chrome()
    profile = chrome_profile_dir()
    extension = write_karo_extension(proxy_username, proxy_password)
    marker = profile / "DevToolsActivePort"
    try:
        marker.unlink(missing_ok=True)
    except OSError:
        pass

    log_path = (runtime_dir() / "vnext" / "system-chrome.log").resolve()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_handle = log_path.open("ab", buffering=0)
    args = [
        str(executable),
        f"--user-data-dir={profile}",
        "--remote-debugging-address=127.0.0.1",
        "--remote-debugging-port=0",
        f"--proxy-server={proxy_server}",
        "--proxy-bypass-list=<-loopback>",
        f"--disable-extensions-except={extension}",
        f"--load-extension={extension}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        "--window-position=40,40",
        f"--window-size={width},{height}",
        "about:blank",
    ]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    try:
        process = subprocess.Popen(
            args,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=log_handle,
            shell=False,
            creationflags=creationflags,
        )
    except Exception as exc:
        log_handle.close()
        raise SystemChromeError("Google Chrome could not be started") from exc
    finally:
        # The child inherited the underlying handle; the parent does not need to
        # keep its Python wrapper open after Popen returned.
        try:
            log_handle.close()
        except Exception:
            pass

    try:
        port = _wait_for_devtools_port(profile, process)
        browser = playwright_ctx.chromium.connect_over_cdp(
            f"http://127.0.0.1:{port}", timeout=20_000
        )
        contexts = list(browser.contexts)
        if not contexts:
            raise SystemChromeError("system Chrome exposed no browser context")
        context = contexts[0]
        return SystemChromeLaunch(
            browser=browser,
            context=context,
            process=process,
            profile_dir=profile,
            extension_dir=extension,
            log_path=log_path,
        )
    except Exception:
        terminate_chrome_process(process)
        raise
