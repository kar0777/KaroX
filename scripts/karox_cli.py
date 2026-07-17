#!/usr/bin/env python3
"""Public UTF-8 entrypoint for KaroX product administration."""
from __future__ import annotations

import os
import sys


def _configure_stdio() -> None:
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except (OSError, ValueError):
                pass
    if os.name == "nt":
        os.environ.setdefault("PYTHONIOENCODING", "utf-8")
        os.environ.setdefault("PYTHONUTF8", "1")


_configure_stdio()

# Keep stop independent from the larger admin module. This lets the updater
# terminate live sessions even when application modules are about to be replaced.
if len(sys.argv) > 1 and sys.argv[1].lower() == "stop":
    from karox_stop import main as stop_main  # noqa: E402

    raise SystemExit(stop_main(sys.argv[2:]))

from karox_admin import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
