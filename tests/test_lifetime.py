"""Windows: what KaroX starts ends with it, even when KaroX is killed outright."""

from __future__ import annotations

import ctypes
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from karox6.lifetime import bind_to_karox, extended_limits


@pytest.mark.skipif(sys.maxsize < 2**32, reason="64-bit layout")
def test_job_limit_structure_has_the_windows_layout() -> None:
    assert ctypes.sizeof(extended_limits()) == 144


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX")
def test_no_job_outside_windows() -> None:
    assert bind_to_karox(1) is False


def _alive(pid: int) -> bool:
    out = subprocess.run(
        ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    return str(pid) in out.split()


@pytest.mark.skipif(sys.platform != "win32", reason="job objects are Windows")
@pytest.mark.parametrize("breakaway", [False, True])
def test_hard_killed_karox_takes_its_commands_along(tmp_path: Path, breakaway: bool) -> None:
    """A command dies with KaroX; a program that asks to detach (CREATE_BREAKAWAY_FROM_JOB,
    like an editor launched by a libuv tool) may still start and stay."""
    pid_file = tmp_path / "pid.txt"
    code = textwrap.dedent(
        """
        import asyncio, os, sys
        from pathlib import Path
        from karox6.tools.proc import ProcessRun

        SLEEPER = (
            "import os, sys, time; open(sys.argv[1], 'w').write(str(os.getpid())); "
            "time.sleep(300)"
        )
        DETACH = (
            "import subprocess, sys; subprocess.Popen([sys.executable, '-c', sys.argv[1], "
            "sys.argv[2]], creationflags=0x01000000 | 0x00000008)"  # breakaway | detached
        )

        async def main():
            pid_file, log, breakaway = sys.argv[1], Path(sys.argv[2]), sys.argv[3] == "1"
            argv = [sys.executable, "-c", DETACH, SLEEPER, pid_file] if breakaway else [
                sys.executable, "-c", SLEEPER, pid_file]
            await ProcessRun.start(argv, cwd=log.parent, env=dict(os.environ), log_path=log,
                                   command="sleeper")
            await asyncio.sleep(300)

        asyncio.run(main())
        """
    )
    karox = subprocess.Popen(
        [sys.executable, "-c", code, str(pid_file), str(tmp_path / "cmd.log"), str(int(breakaway))],
        stdin=subprocess.DEVNULL,
    )
    child = 0
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and not child:
            text = pid_file.read_text() if pid_file.exists() else ""
            child = int(text) if text.strip() else 0
            time.sleep(0.1)
        assert child, "the command never started"
        assert _alive(child)
        karox.kill()  # TerminateProcess: no cleanup code runs in KaroX
        karox.wait(timeout=10)
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and _alive(child):
            time.sleep(0.1)
        if breakaway:
            assert _alive(child), "a program that detached itself was killed"
        else:
            assert not _alive(child), "the command outlived a hard-killed KaroX"
    finally:
        if karox.poll() is None:
            karox.kill()
        if child and _alive(child):
            subprocess.run(["taskkill", "/F", "/PID", str(child)], capture_output=True, check=False)
