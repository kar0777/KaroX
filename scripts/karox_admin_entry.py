#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

import karox_admin as admin
from karox_paths import config_dir, runtime_dir


def configure() -> None:
    config = config_dir().resolve()
    runtime = runtime_dir().resolve()
    admin.CONFIG_DIR = config
    admin.RUNTIME_DIR = runtime
    admin.SESSIONS_DIR = runtime / "sessions"
    admin.CACHE_DIR = runtime / "cache"


def main() -> int:
    configure()
    return int(admin.main())


if __name__ == "__main__":
    raise SystemExit(main())
