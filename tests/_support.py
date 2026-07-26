from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


"""Every spelling KaroX accepts for a path override, highest precedence first.

``paths.py`` prefers the ``KAROX_VNEXT_*`` names and falls back to the shorter
aliases, so a test that sets only an alias can be silently redirected by an
inherited ``KAROX_VNEXT_*`` -- to another run's directory, or to the developer's
real configuration. Isolation therefore has to name every spelling.
"""
_CONFIG_OVERRIDES = ("KAROX_VNEXT_CONFIG_DIR", "KAROX_CONFIG_DIR")
_RUNTIME_OVERRIDES = ("KAROX_VNEXT_RUNTIME_DIR", "KAROX_RUNTIME_DIR")
_LEGACY_OVERRIDES = ("KAROX_LEGACY_CONFIG_DIR",)


def child_environment(
    *,
    config_dir: Path | str | None = None,
    runtime_dir: Path | str | None = None,
    legacy_config_dir: Path | str | None = None,
    **extra: str,
) -> dict[str, str]:
    """Build a child environment a KaroX subprocess cannot escape.

    Copying the parent environment is convenient and load-bearing -- PATH, the
    git identity, the temp directory all have to come through -- but it also
    carries any override the developer or CI has exported. Each directory is set
    under its preferred name and every other spelling of it is removed, so what
    the caller asked for is what the child resolves.
    """

    environment = dict(os.environ)
    for names, value in (
        (_CONFIG_OVERRIDES, config_dir),
        (_RUNTIME_OVERRIDES, runtime_dir),
        (_LEGACY_OVERRIDES, legacy_config_dir),
    ):
        for name in names:
            environment.pop(name, None)
        if value is not None:
            environment[names[0]] = str(value)
    environment.update(extra)
    return environment


def cleanup_temporary_directory(
    temporary: tempfile.TemporaryDirectory, *, timeout: float = 5.0
) -> None:
    """Remove a test's temporary directory, tolerating a handle that is closing.

    Windows will not remove a directory any process still holds open -- including
    a child that was just killed and whose working directory it was. A test that
    deliberately kills a process therefore races its own teardown, and loses
    often enough to be recorded as a known flake. The handle clears in
    milliseconds, so this retries.

    It deliberately does not pass ``ignore_cleanup_errors``: that would leave the
    directory behind and say nothing, hiding a genuine leak. A handle that never
    clears still raises.
    """

    deadline = time.monotonic() + timeout
    while True:
        try:
            temporary.cleanup()
            return
        except OSError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


def initialize_git_repository(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.update(
        {
            "GIT_AUTHOR_NAME": "KaroX Test",
            "GIT_AUTHOR_EMAIL": "karox@example.invalid",
            "GIT_COMMITTER_NAME": "KaroX Test",
            "GIT_COMMITTER_EMAIL": "karox@example.invalid",
        }
    )
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=path,
        env=env,
        check=True,
        capture_output=True,
    )

