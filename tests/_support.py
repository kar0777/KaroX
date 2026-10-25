"""Shared test support: make the release tree the import target.

``conftest.py`` puts this package's ``src/`` first for collection; importing
this module from a test performs the same insertion for code paths that run
outside the collector's import context (pilot runs, spawned children). The
constant is re-exported so tests can assert where imports resolve from.
"""

from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
