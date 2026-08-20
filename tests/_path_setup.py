"""Import-for-side-effect path bootstrap for tests that need only ``src`` on the path.

``tests/_support.py`` also inserts ``src`` into :data:`sys.path`, but it drags in
subprocess, temporary-directory, and environment-isolation helpers that several
modules do not use.  Those modules import this instead, so the dependency they
declare is the one they actually have: make ``karox`` importable from a source
checkout without installing the wheel first.

Keeping the insertion here rather than in a ``conftest.py`` means the same
modules also run under ``python -m unittest``, which is the runner the release
gates and ``scripts/check_test_count.py`` use.

This module is also the process-wide production/test isolation bootstrap: a
rehearsal profile once leaked into the real ``%APPDATA%\\KaroX`` store because
isolation was a per-test convention. At first import this module:

1. captures the machine's *real* config/runtime/legacy locations (before any
   test patches the environment) and exports them in
   ``KAROX_TEST_FORBIDDEN_DIRS``;
2. redirects every override spelling ``karox.paths`` honours to a fresh
   per-process temporary directory;
3. arms ``KAROX_TEST_ISOLATION=1`` so ``karox.paths`` fails fast with
   ``TestIsolationViolation`` if resolution still lands inside a captured real
   location, and ``karox.credentials`` refuses the real OS keyring.

A test may still patch ``APPDATA``/``XDG_*`` at a temporary base -- that
resolves outside the forbidden list and passes. Inherited overrides are
trusted only when an outer *test* process set them (it also exports
``KAROX_TEST_ISOLATION=1``); a bare override inherited from a developer shell
is somebody's real configuration, so it is captured as forbidden and replaced.
"""

from __future__ import annotations

import atexit
import os
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


_CONFIG_OVERRIDES = ("KAROX_VNEXT_CONFIG_DIR", "KAROX_CONFIG_DIR")
_RUNTIME_OVERRIDES = ("KAROX_VNEXT_RUNTIME_DIR", "KAROX_RUNTIME_DIR")
_LEGACY_OVERRIDES = ("KAROX_LEGACY_CONFIG_DIR",)
_ALL_OVERRIDES = (*_CONFIG_OVERRIDES, *_RUNTIME_OVERRIDES, *_LEGACY_OVERRIDES)

_SANDBOX_PREFIX = "karox-test-isolation-"


def _outer_test_process_owns_environment() -> bool:
    return os.environ.get("KAROX_TEST_ISOLATION", "").strip() == "1" and all(
        os.environ.get(name, "").strip() for name in _ALL_OVERRIDES
    )


def _capture_real_locations() -> list[str]:
    """Real user locations, computed before this process patches anything."""
    forbidden: list[str] = []
    # Any override inherited from a developer shell names a real location.
    for name in _ALL_OVERRIDES:
        value = os.environ.get(name, "").strip()
        if value:
            forbidden.append(value)
    # The platform defaults karox.paths would resolve without overrides.
    saved = {name: os.environ.pop(name, None) for name in _ALL_OVERRIDES}
    flag = os.environ.pop("KAROX_TEST_ISOLATION", None)
    try:
        from karox import paths as _paths

        forbidden.append(str(_paths.config_dir()))
        forbidden.append(str(_paths.runtime_dir()))
        forbidden.append(str(_paths.legacy_config_dir()))
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value
        if flag is not None:
            os.environ["KAROX_TEST_ISOLATION"] = flag
    return forbidden


def _ensure_isolated_user_dirs() -> Path | None:
    if _outer_test_process_owns_environment():
        return None  # e.g. an xdist worker inheriting its parent's sandbox
    forbidden = _capture_real_locations()
    base = Path(tempfile.mkdtemp(prefix=_SANDBOX_PREFIX))
    targets = {
        _CONFIG_OVERRIDES: base / "config",
        _RUNTIME_OVERRIDES: base / "runtime",
        _LEGACY_OVERRIDES: base / "legacy-config",
    }
    for names, target in targets.items():
        target.mkdir(parents=True, exist_ok=True)
        for name in names:
            os.environ[name] = str(target)
    os.environ["KAROX_TEST_FORBIDDEN_DIRS"] = os.pathsep.join(forbidden)
    os.environ["KAROX_TEST_SANDBOX_DIR"] = str(base)
    os.environ["KAROX_TEST_ISOLATION"] = "1"

    def _cleanup(path: Path = base) -> None:
        shutil.rmtree(path, ignore_errors=True)

    atexit.register(_cleanup)
    return base


SANDBOX = _ensure_isolated_user_dirs()

__all__ = ["ROOT", "SRC", "SANDBOX"]
