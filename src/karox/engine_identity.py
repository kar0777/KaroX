"""The engine implementation identity written into every report.

The vendored engine writes ``self.__class__.__module__`` into its trajectory;
the product report's ``engine`` block names the same concrete class. Keeping
this constant in a leaf module lets the kernel facade re-export it and the
finish mixin use it without importing the facade back.
"""

from __future__ import annotations

import os

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

from .engine import __version__ as _engine_version  # noqa: E402

ENGINE_IDENTITY = {
    "name": "karo-core",
    "version": _engine_version,
    "implementation": "karox.engine_kernel.EngineAgent",
}

__all__ = ["ENGINE_IDENTITY"]
