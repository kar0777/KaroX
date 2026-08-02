"""Launch a dedicated system-Chrome profile for headed KaroX browser sessions.

Playwright's bundled Chromium is excellent for deterministic verification, but
identity providers can refuse it as an automated browser.  This module launches
the user's installed Google Chrome as a normal external process, with a
KaroX-only persistent profile, and then attaches through the standard Chrome
DevTools Protocol (CDP).

The everyday Chrome profile is never opened.  Cookies in the dedicated profile
survive a KaroX restart, while the browser still routes traffic through KaroX's
session proxy.  A tiny unpacked extension supplies proxy credentials and owns a
branded dark new-tab page; it does not read page contents or browsing history.
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
        "description": "Local KaroX browser profile, proxy authentication, and branded new tab.",
        "version": "1.0.0",
        "permissions": ["webRequest", "webRequestAuthProvider"],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"},
        "chrome_url_overrides": {"newtab": "newtab.html"},
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


def _extension_new_tab() -> str:
    return r'''<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>✦ KaroX Browser ✦</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  html, body { width: 100%; height: 100%; margin: 0; overflow: hidden; }
  body {
    display: grid;
    place-items: center;
    background:
      radial-gradient(circle at 18% 18%, rgba(123,92,255,.20), transparent 28%),
      radial-gradient(circle at 82% 72%, rgba(0,214,255,.12), transparent 34%),
      linear-gradient(145deg, #05060a 0%, #0a0c14 45%, #030408 100%);
    color: #f7f8ff;
    font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
  }
  #stars, #stars::before, #stars::after {
    position: fixed; inset: -20%; content: ""; pointer-events: none;
    background-image:
      radial-gradient(circle, rgba(255,255,255,.95) 0 1px, transparent 1.5px),
      radial-gradient(circle, rgba(143,205,255,.8) 0 1px, transparent 1.6px),
      radial-gradient(circle, rgba(202,172,255,.75) 0 1px, transparent 1.4px);
    background-size: 83px 83px, 137px 137px, 191px 191px;
    background-position: 11px 19px, 43px 71px, 107px 29px;
    animation: drift 34s linear infinite;
    opacity: .52;
  }
  #stars::before { transform: scale(1.3) rotate(9deg); animation-duration: 51s; opacity: .34; }
  #stars::after { transform: scale(.72) rotate(-7deg); animation-duration: 25s; opacity: .24; }
  @keyframes drift { to { transform: translate3d(120px, 80px, 0) rotate(.001deg); } }
  .shell {
    position: relative;
    width: min(760px, calc(100vw - 48px));
    padding: 58px 48px 46px;
    border: 1px solid rgba(255,255,255,.12);
    border-radius: 28px;
    background: linear-gradient(145deg, rgba(18,21,34,.82), rgba(7,9,16,.72));
    box-shadow: 0 24px 90px rgba(0,0,0,.55), inset 0 1px 0 rgba(255,255,255,.08);
    backdrop-filter: blur(20px);
    text-align: center;
  }
  .mark {
    display: inline-flex; align-items: center; gap: 12px;
    font-size: clamp(54px, 9vw, 96px); font-weight: 850; letter-spacing: -.07em;
    line-height: .9;
    background: linear-gradient(90deg, #ffffff 0%, #a9bbff 23%, #c989ff 46%, #72e8ff 70%, #ffffff 100%);
    background-size: 240% auto;
    -webkit-background-clip: text; background-clip: text; color: transparent;
    animation: shimmer 4.8s linear infinite;
    filter: drop-shadow(0 0 24px rgba(139,144,255,.18));
  }
  @keyframes shimmer { to { background-position: -240% center; } }
  .spark { font-size: .48em; color: #d8dcff; animation: pulse 2.4s ease-in-out infinite; }
  @keyframes pulse { 50% { transform: scale(1.22) rotate(18deg); opacity: .58; } }
  .subtitle { margin-top: 22px; color: rgba(232,235,255,.72); font-size: 17px; letter-spacing: .025em; }
  .status {
    margin: 34px auto 0; display: inline-flex; align-items: center; gap: 10px;
    padding: 10px 15px; border-radius: 999px;
    border: 1px solid rgba(126,229,255,.18); background: rgba(41,91,108,.13);
    color: #bcefff; font-size: 13px;
  }
  .dot { width: 8px; height: 8px; border-radius: 50%; background: #62f4b5; box-shadow: 0 0 15px #62f4b5; }
  .note { margin-top: 28px; color: rgba(217,221,242,.48); font-size: 12px; }
</style>
</head>
<body>
<div id="stars"></div>
<main class="shell">
  <div class="mark"><span class="spark">✦</span><span>KaroX</span><span class="spark">✦</span></div>
  <div class="subtitle">Отдельный защищённый профиль для браузерных задач</div>
  <div class="status"><span class="dot"></span><span>Браузер готов · управление ограничено сессией KaroX</span></div>
  <div class="note">Обычный профиль Chrome, его cookies и вкладки не используются.</div>
</main>
</body>
</html>
'''


def write_karo_extension(username: str, password: str) -> Path:
    root = (runtime_dir() / "vnext" / "browser-extension").resolve()
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text(_extension_manifest(), encoding="utf-8")
    (root / "background.js").write_text(
        _extension_background(username, password), encoding="utf-8"
    )
    (root / "newtab.html").write_text(_extension_new_tab(), encoding="utf-8")
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
            )
            return
        except Exception:
            pass
    try:
        process.kill()
    except Exception:
        pass


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
        f"--window-size={width},{height}",
        "chrome://newtab/",
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
