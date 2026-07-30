"""Import-for-side-effect path bootstrap for tests that need only ``src`` on the path.

``tests/_support.py`` also inserts ``src`` into :data:`sys.path`, but it drags in
subprocess, temporary-directory, and environment-isolation helpers that several
modules do not use.  Those modules import this instead, so the dependency they
declare is the one they actually have: make ``karox`` importable from a source
checkout without installing the wheel first.

Keeping the insertion here rather than in a ``conftest.py`` means the same
modules also run under ``python -m unittest``, which is the runner the release
gates and ``scripts/check_test_count.py`` use.
"""

from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

__all__ = ["ROOT", "SRC"]
