"""Cross-platform application paths for the vNext runtime."""

from __future__ import annotations

import os
import sys
from pathlib import Path


APP_NAME = "KaroX"
LEGACY_NAME = "RepoPilotBridge"


def _override(*names: str) -> Path | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return Path(value).expanduser()
    return None


class TestIsolationViolation(RuntimeError):
    """A test process tried to resolve a real user directory.

    ``tests/_path_setup.py`` captures the machine's real config/runtime
    locations before any test patches the environment, exports them in
    ``KAROX_TEST_FORBIDDEN_DIRS``, and redirects every override spelling to a
    per-process temporary directory. A test may legitimately patch ``APPDATA``
    or ``XDG_*`` at a temporary base -- that resolves elsewhere and passes. If
    resolution still lands inside a captured real location, some code cleared
    the overrides without re-basing -- exactly the defect that once let a
    rehearsal profile contaminate the real ``%APPDATA%\\KaroX`` store -- so
    fail fast and name the fix.
    """


def _isolation_active() -> bool:
    return os.environ.get("KAROX_TEST_ISOLATION", "").strip() == "1"


def _forbidden_real_dirs() -> tuple[str, ...]:
    raw = os.environ.get("KAROX_TEST_FORBIDDEN_DIRS", "")
    return tuple(
        os.path.normcase(item.strip())
        for item in raw.split(os.pathsep)
        if item.strip()
    )


_SANDBOX_SUBDIRS = {
    "config directory": "config",
    "runtime directory": "runtime",
    "legacy config directory": "legacy-config",
}


def _guard_real_dir(candidate: Path, kind: str) -> Path:
    if not _isolation_active():
        return candidate
    resolved = os.path.normcase(str(candidate))
    for forbidden in _forbidden_real_dirs():
        if resolved == forbidden or resolved.startswith(forbidden + os.sep):
            # Many long-standing tests pop an override for good and would
            # otherwise punish an unrelated later test in the same worker.
            # The guarantee is "never the real location", so resolution is
            # re-pointed at the per-process sandbox when one exists; only an
            # unprepared process (a child spawned without the sandbox) fails
            # fast.
            sandbox = os.environ.get("KAROX_TEST_SANDBOX_DIR", "").strip()
            if sandbox:
                target = Path(sandbox) / _SANDBOX_SUBDIRS.get(kind, "misc")
                target.mkdir(parents=True, exist_ok=True)
                return target.resolve()
            raise TestIsolationViolation(
                f"test isolation is active and the {kind} resolved to the real "
                f"user location {candidate}. Set the KAROX_VNEXT_*/KAROX_* "
                "overrides (tests/_path_setup.py does) or patch the base "
                "directory (APPDATA/XDG_*) at a temporary path instead."
            )
    return candidate


def config_dir() -> Path:
    override = _override("KAROX_VNEXT_CONFIG_DIR", "KAROX_CONFIG_DIR")
    if override is not None:
        return override.resolve()
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return _guard_real_dir((base / APP_NAME).resolve(), "config directory")


def runtime_dir() -> Path:
    override = _override("KAROX_VNEXT_RUNTIME_DIR", "KAROX_RUNTIME_DIR")
    if override is not None:
        return override.resolve()
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share"))
    return _guard_real_dir((base / APP_NAME).resolve(), "runtime directory")


def legacy_config_dir() -> Path:
    override = _override("KAROX_LEGACY_CONFIG_DIR")
    if override is not None:
        return override.resolve()
    home = Path.home()
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming"))
    elif sys.platform == "darwin":
        base = home / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config"))
    return _guard_real_dir((base / LEGACY_NAME).resolve(), "legacy config directory")


def session_dir() -> Path:
    """Keep vNext records separate from unversioned legacy sessions."""
    return runtime_dir() / "vnext" / "sessions"


def oauth_state_dir() -> Path:
    """Where a bridge keeps the OAuth registrations that outlive its process."""
    return runtime_dir() / "vnext" / "oauth-bridge"


def migration_dir() -> Path:
    return config_dir() / "vnext"
