"""Cross-platform application paths for the vNext runtime."""

from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "KaroX"
LEGACY_NAME = "RepoPilotBridge"


def _override(*names: str) -> Path | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return Path(value).expanduser()
    return None


def config_dir() -> Path:
    override = _override("KAROX_VNEXT_CONFIG_DIR", "KAROX_CONFIG_DIR")
    if override is not None:
        return override.resolve()
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return (base / APP_NAME).resolve()


def runtime_dir() -> Path:
    override = _override("KAROX_VNEXT_RUNTIME_DIR", "KAROX_RUNTIME_DIR")
    if override is not None:
        return override.resolve()
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return (base / APP_NAME).resolve()


def legacy_config_dir() -> Path:
    override = _override("KAROX_LEGACY_CONFIG_DIR")
    if override is not None:
        return override.resolve()
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return (base / LEGACY_NAME).resolve()


def session_dir() -> Path:
    """Keep vNext records separate from unversioned legacy sessions."""
    return runtime_dir() / "vnext" / "sessions"


def oauth_state_dir() -> Path:
    """Where a bridge keeps the OAuth registrations that outlive its process."""
    return runtime_dir() / "vnext" / "oauth-bridge"


def migration_dir() -> Path:
    return config_dir() / "vnext"
