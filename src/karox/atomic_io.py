"""One atomic file write, shared by Core and the workspace transaction.

Both paths wrote the same tempfile/fchmod/fsync/replace sequence; a policy
change (permissions, durability, cleanup) had to be made twice and could drift.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional


def atomic_write(path: Path, content: bytes, mode: Optional[int] = None) -> None:
    """Replace ``path`` with ``content`` atomically, preserving ``mode``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            if mode is not None and hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
