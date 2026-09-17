"""Run one Python test command in an isolated Windows console process group.

KaroX has tests that intentionally exercise Windows process-group and Ctrl-Break
cleanup. A test runner attached to the GitHub Actions PowerShell console can
therefore receive a console-control event meant for a test child and abort with
``KeyboardInterrupt`` even though no test failed. Keep the runner out of that
console while preserving normal stdout/stderr and its exact exit code.

Usage examples::

    python scripts/run_ci_test_command.py -m pytest tests
    python scripts/run_ci_test_command.py -m unittest discover -s tests -p test_*.py -v

This is isolation, not suppression: the wrapper never installs a signal handler,
never converts a test failure to success, and an interrupt of the wrapper still
fails the CI step.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _windows_creationflags() -> int:
    if os.name != "nt":
        return 0
    return int(
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    )


def main(argv: list[str] | None = None) -> int:
    python_args = list(sys.argv[1:] if argv is None else argv)
    if not python_args:
        print("usage: run_ci_test_command.py PYTHON_ARGS...", file=sys.stderr)
        return 2
    process = subprocess.Popen(
        [sys.executable, *python_args],
        cwd=ROOT,
        creationflags=_windows_creationflags(),
    )
    try:
        return int(process.wait())
    except KeyboardInterrupt:
        # A real cancellation of the CI wrapper remains a failed run. Do not
        # translate it to green; just bound the child lifetime before exiting.
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
