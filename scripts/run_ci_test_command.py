"""Run a Python test command without inheriting the Windows runner console.

Explicitly pipe and relay output: CREATE_NO_WINDOW with inherited console
handles can otherwise lose all diagnostics. Isolation never changes the child's
exit code or converts an interrupt/failure to success.
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
    environment = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1")
    # The child must have real pipe handles, not inherited console handles that
    # become invalid when Windows creates it without a console.
    process = subprocess.Popen(
        [sys.executable, *python_args],
        cwd=ROOT,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        creationflags=_windows_creationflags(),
    )
    try:
        assert process.stdout is not None
        for line in process.stdout:
            # A redirected Windows parent may itself use cp1252. Preserve the
            # diagnostic in escaped form instead of crashing on Unicode.
            encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
            sys.stdout.write(line.encode(encoding, errors="backslashreplace").decode(encoding))
            sys.stdout.flush()
        return int(process.wait())
    except KeyboardInterrupt:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        return 130
    finally:
        if process.stdout is not None:
            process.stdout.close()


if __name__ == "__main__":
    raise SystemExit(main())
