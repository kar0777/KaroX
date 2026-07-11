"""Process-wide UTF-8 console defaults for KaroX command-line tools.

Python imports ``sitecustomize`` automatically when this directory is on
``sys.path``. Running ``python scripts/<tool>.py`` therefore gets consistent
UTF-8 output even when Windows starts with a legacy console code page.
"""
from __future__ import annotations

import os
import sys


def _configure_stream(name: str) -> None:
    stream = getattr(sys, name, None)
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return


if os.name == "nt" or os.environ.get("KAROX_FORCE_UTF8", "1") == "1":
    _configure_stream("stdout")
    _configure_stream("stderr")
