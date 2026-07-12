#!/usr/bin/env python3
from __future__ import annotations

import karox_admin as admin
from karox_paths import config_dir, runtime_dir


def main() -> int:
    config = config_dir().resolve()
    runtime = runtime_dir().resolve()
    admin.CONFIG_DIR = config
    admin.RUNTIME_DIR = runtime
    admin.SESSIONS_DIR = runtime / "sessions"
    admin.CACHE_DIR = runtime / "cache"
    import support_bundle
    return int(support_bundle.main())


if __name__ == "__main__":
    raise SystemExit(main())
