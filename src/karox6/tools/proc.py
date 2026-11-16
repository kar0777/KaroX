"""Child processes: start, capture output to a log, wait with timeout, kill the whole tree."""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import signal
import subprocess
import sys
import time
from collections.abc import Mapping
from pathlib import Path

from karox6.lifetime import bind_to_karox

HEAD_CAP = 64 * 1024
TAIL_CAP = 64 * 1024
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")


def clean_text(data: bytes) -> str:
    text = data.decode("utf-8", errors="replace")
    text = text.replace("\r\n", "\n")
    return _ANSI.sub("", text)


class ProcessRun:
    """One running child process whose merged stdout/stderr streams into ``log_path``."""

    def __init__(self, proc: asyncio.subprocess.Process, log_path: Path, command: str) -> None:
        self.proc = proc
        self.log_path = log_path
        self.command = command
        self.started = time.monotonic()
        self.total_bytes = 0
        self.head = bytearray()
        self.tail = bytearray()
        self.killed = False
        self._log = log_path.open("wb")
        self.reader = asyncio.create_task(self._pump())

    @classmethod
    async def start(
        cls, argv: list[str], *, cwd: Path, env: Mapping[str, str], log_path: Path, command: str
    ) -> ProcessRun:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict[str, object] = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            env=dict(env),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            **kwargs,  # type: ignore[arg-type]
        )
        bind_to_karox(proc.pid)
        return cls(proc, log_path, command)

    async def _pump(self) -> None:
        stream = self.proc.stdout
        assert stream is not None
        try:
            while True:
                chunk = await stream.read(65536)
                if not chunk:
                    break
                self.total_bytes += len(chunk)
                with contextlib.suppress(OSError, ValueError):
                    self._log.write(chunk)
                    self._log.flush()
                if len(self.head) < HEAD_CAP:
                    room = HEAD_CAP - len(self.head)
                    self.head += chunk[:room]
                self.tail += chunk
                if len(self.tail) > TAIL_CAP:
                    del self.tail[: len(self.tail) - TAIL_CAP]
        finally:
            with contextlib.suppress(OSError):
                self._log.close()

    @property
    def pid(self) -> int:
        return self.proc.pid

    @property
    def returncode(self) -> int | None:
        return self.proc.returncode

    @property
    def elapsed_s(self) -> float:
        return time.monotonic() - self.started

    async def wait(self, timeout_s: float | None) -> bool:
        """Wait for exit; True when the process exited within ``timeout_s``.

        After exit, give the reader a short grace to drain the pipe. A grandchild that
        keeps the pipe open (``server &``) must not block the call: the reader keeps
        logging in the background.
        """
        try:
            await asyncio.wait_for(asyncio.shield(self.proc.wait()), timeout_s)
        except TimeoutError:
            return False
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(self.reader), 2.0)
        self._release()
        return True

    def _release(self) -> None:
        """Close the pipe transport once the process and its reader are done. asyncio
        closes it from a later callback; a loop that stops first (the end of asyncio.run)
        leaves it to the garbage collector, which warns about an unclosed transport."""
        transport = getattr(self.proc, "_transport", None)
        if transport is not None and self.proc.returncode is not None and self.reader.done():
            transport.close()

    def output_text(self) -> tuple[str, bool]:
        """(text, complete). When output exceeded the buffers, text is head + tail only."""
        if self.total_bytes <= HEAD_CAP:
            return clean_text(bytes(self.head)), True
        if self.total_bytes <= 8 * 1024 * 1024:
            with contextlib.suppress(OSError):
                self._flush_log()
                return clean_text(self.log_path.read_bytes()), True
        omitted = self.total_bytes - len(self.head) - len(self.tail)
        text = (
            clean_text(bytes(self.head))
            + f"\n\n[... {max(omitted, 0)} bytes omitted ...]\n\n"
            + clean_text(bytes(self.tail))
        )
        return text, False

    def _flush_log(self) -> None:
        with contextlib.suppress(OSError, ValueError):
            self._log.flush()

    async def kill(self) -> None:
        if self.proc.returncode is None:
            self.killed = True
            await asyncio.to_thread(kill_tree, self.proc.pid)
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(self.proc.wait(), 5.0)
        if not self.reader.done():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self.reader), 1.0)
        if not self.reader.done():
            self.reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self.reader
        self._release()


def kill_tree(pid: int) -> None:
    """Kill a process and all of its descendants (best effort, all platforms)."""
    if sys.platform == "win32":
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(pid)],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=15,
                check=False,
            )
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pid, sig)
        except ProcessLookupError:
            return
        except PermissionError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(pid, sig)
        if sig == signal.SIGTERM:
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                try:
                    os.killpg(pid, 0)
                except (ProcessLookupError, PermissionError):
                    return
                time.sleep(0.05)
