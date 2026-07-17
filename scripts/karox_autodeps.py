#!/usr/bin/env python3
"""KaroX 4.1 launch autopilot for optional dependencies.

Installs the optional "eyes & hands" packages (playwright, pillow, mss) the
first time KaroX starts after an install or update, so browser screenshots and
desktop capture work out of the box. Never fatal: any failure just leaves the
optional feature off, and the server reports that cleanly when the feature is
used.

Skip with KAROX_AUTO_DEPS=0. Re-run any time:
    python scripts/karox_autodeps.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
try:
    import karox_ui as ui
except Exception:  # UI must never break startup
    ui = None

APP_ROOT = Path(__file__).resolve().parents[1]
OPTIONAL_PACKAGES = (
    ("playwright", "playwright"),
    ("PIL", "pillow"),
    ("mss", "mss"),
)
PIP_TIMEOUT = 600
BROWSER_TIMEOUT = 1200


def _say(kind: str, title: str, detail: str = "") -> None:
    if ui:
        getattr(ui, kind)(title, detail)
    else:
        print(f"[{kind}] {title}" + (f" — {detail}" if detail else ""))


def read_version() -> str:
    try:
        return (APP_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"


def runtime_dir() -> Path:
    home = Path.home()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "RepoPilotBridge"
    if sys.platform == "darwin":
        return home / ".local" / "share" / "RepoPilotBridge"
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "RepoPilotBridge"


MARKER = runtime_dir() / "cache" / "autodeps.json"


def load_marker() -> dict:
    try:
        data = json.loads(MARKER.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_marker(data: dict) -> None:
    try:
        MARKER.parent.mkdir(parents=True, exist_ok=True)
        MARKER.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def importable(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False


def pip_install(package: str) -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "--quiet", package],
            capture_output=True,
            text=True,
            timeout=PIP_TIMEOUT,
        )
        return result.returncode == 0
    except Exception:
        return False


def ensure_chromium() -> bool:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True,
            text=True,
            timeout=BROWSER_TIMEOUT,
        )
        return result.returncode == 0
    except Exception:
        return False


def main() -> int:
    if os.environ.get("KAROX_AUTO_DEPS") == "0":
        return 0
    version = read_version()
    marker = load_marker()
    if marker.get("version") == version and marker.get("ok"):
        return 0

    if ui:
        ui.banner(version, "LAUNCH AUTOPILOT · optional features")
    missing = [(module, package) for module, package in OPTIONAL_PACKAGES if not importable(module)]
    installed = [package for module, package in OPTIONAL_PACKAGES if importable(module)]
    all_ok = True
    for module, package in missing:
        _say("step", f"Installing {package}", "one-time setup for browser/desktop features")
        if pip_install(package) and importable(module):
            _say("ok", f"{package} ready")
            installed.append(package)
        else:
            all_ok = False
            _say("warn", f"{package} was not installed", "feature stays off · retry: python scripts/karox_autodeps.py")

    chromium = bool(marker.get("chromium"))
    if importable("playwright") and not chromium:
        _say("step", "Preparing headless Chromium", "used for page screenshots and smoke tests")
        chromium = ensure_chromium()
        if chromium:
            _say("ok", "Chromium ready")
        else:
            all_ok = False
            _say("warn", "Chromium is not installed", "retry later: python -m playwright install chromium")

    save_marker(
        {
            "version": version,
            "ok": all_ok,
            "chromium": chromium,
            "packages": sorted(set(installed)),
            "checkedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
    )
    if all_ok:
        _say("ok", "Optional features are ready", "browser · screenshots · desktop capture")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
