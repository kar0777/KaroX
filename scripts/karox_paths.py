#!/usr/bin/env python3
"""Canonical KaroX paths and one-time RepoPilotBridge migration."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path
from typing import Any

APP_NAME = "KaroX"
LEGACY_NAME = "RepoPilotBridge"


def _home() -> Path:
    return Path.home()


def _override(name: str) -> Path | None:
    value = os.environ.get(name, "").strip()
    return Path(value).expanduser() if value else None


def config_dir() -> Path:
    override = _override("KAROX_CONFIG_DIR")
    if override:
        return override
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", _home() / "AppData" / "Roaming")) / APP_NAME
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / APP_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", _home() / ".config")) / APP_NAME


def runtime_dir() -> Path:
    override = _override("KAROX_RUNTIME_DIR")
    if override:
        return override
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", _home() / "AppData" / "Local")) / APP_NAME
    return Path(os.environ.get("XDG_DATA_HOME", _home() / ".local" / "share")) / APP_NAME


def legacy_config_dir() -> Path:
    override = _override("KAROX_LEGACY_CONFIG_DIR")
    if override:
        return override
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", _home() / "AppData" / "Roaming")) / LEGACY_NAME
    if sys.platform == "darwin":
        return _home() / "Library" / "Application Support" / LEGACY_NAME
    return Path(os.environ.get("XDG_CONFIG_HOME", _home() / ".config")) / LEGACY_NAME


def legacy_runtime_dir() -> Path:
    override = _override("KAROX_LEGACY_RUNTIME_DIR")
    if override:
        return override
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", _home() / "AppData" / "Local")) / LEGACY_NAME
    return Path(os.environ.get("XDG_DATA_HOME", _home() / ".local" / "share")) / LEGACY_NAME


def _copy_missing(src: Path, dst: Path) -> int:
    if not src.exists():
        return 0
    copied = 0
    if src.is_file():
        if not dst.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return 1
        return 0
    for item in src.rglob("*"):
        relative = item.relative_to(src)
        target = dst / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied += 1
    return copied


def _rewrite_text_paths(root: Path, old_config: Path, old_runtime: Path, new_config: Path, new_runtime: Path) -> int:
    rewritten = 0
    allowed = {".json", ".jsonl", ".txt", ".log", ".md", ".ini", ".cfg"}
    if not root.exists():
        return 0
    replacements = {
        str(old_config): str(new_config),
        str(old_runtime): str(new_runtime),
        str(old_config).replace("\\", "/"): str(new_config).replace("\\", "/"),
        str(old_runtime).replace("\\", "/"): str(new_runtime).replace("\\", "/"),
    }
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in allowed:
            continue
        try:
            original = path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError):
            continue
        updated = original
        for old, new in replacements.items():
            updated = updated.replace(old, new)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            rewritten += 1
    return rewritten


def migrate_legacy() -> dict[str, Any]:
    new_config = config_dir()
    new_runtime = runtime_dir()
    old_config = legacy_config_dir()
    old_runtime = legacy_runtime_dir()
    new_config.mkdir(parents=True, exist_ok=True)
    new_runtime.mkdir(parents=True, exist_ok=True)

    copied_config = _copy_missing(old_config, new_config)
    copied_runtime = 0
    for name in ("sessions", "logs", "cache", "runs", "bin"):
        copied_runtime += _copy_missing(old_runtime / name, new_runtime / name)
    rewritten = _rewrite_text_paths(new_config, old_config, old_runtime, new_config, new_runtime)
    rewritten += _rewrite_text_paths(new_runtime / "sessions", old_config, old_runtime, new_config, new_runtime)

    return {
        "ok": True,
        "configDir": str(new_config),
        "runtimeDir": str(new_runtime),
        "legacyConfigDir": str(old_config),
        "legacyRuntimeDir": str(old_runtime),
        "legacyConfigExists": old_config.exists(),
        "legacyRuntimeExists": old_runtime.exists(),
        "copiedConfigFiles": copied_config,
        "copiedRuntimeFiles": copied_runtime,
        "rewrittenFiles": rewritten,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Resolve and migrate KaroX application paths.")
    parser.add_argument("command", choices=("show", "migrate"), nargs="?", default="show")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    payload = migrate_legacy() if args.command == "migrate" else {
        "configDir": str(config_dir()),
        "runtimeDir": str(runtime_dir()),
        "legacyConfigDir": str(legacy_config_dir()),
        "legacyRuntimeDir": str(legacy_runtime_dir()),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False))
    else:
        for key, value in payload.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
