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

from karox6.lifetime import bind_to_karox, join_karox_job

HEAD_CAP = 64 * 1024
TAIL_CAP = 64 * 1024
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")


SILENT_NOTE_S = 120
# A pipe (not `||`) into a filter that can print only after its input ends; `tail -f`
# streams and is excluded.
_END_ONLY_FILTER = re.compile(
    r"(?<!\|)\|&?(?!\|)\s*(?:(?:tail|tac)\b(?![^|;&]*\s(?:-[A-Za-z]*[fF]|--follow))|sort\b|wc\b)"
)


def silence_note(run: ProcessRun) -> str:
    """Advice for a running command that has printed nothing for a while (no cap, no
    kill): bench models waited 3 x 600 s on a test suite piped to `tail` that could not
    show anything until it ended."""
    silent = int(run.silent_s)
    if not run.active or silent < SILENT_NOTE_S:
        return ""
    what = "it has printed nothing" if run.last_output is None else "no new output"
    note = f" {what} for {silent} s."
    if _END_ONLY_FILTER.search(run.command):
        note += (
            " Its output goes through a filter that prints only when its input ends, so "
            "progress stays hidden until the command finishes. Wait if the final output "
            "is enough; to see progress, run it narrower or write it to a file you read."
        )
    return note


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
        self.last_output: float | None = None  # monotonic time of the latest output byte
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
        join_karox_job()  # Windows: the child is born in the kill-on-exit job
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
                self.last_output = time.monotonic()
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

    @property
    def silent_s(self) -> float:
        """Seconds since the latest output (since the start when it printed nothing)."""
        return time.monotonic() - (self.last_output or self.started)

    async def wait(self, timeout_s: float | None) -> bool:
        """Wait for exit; True when the process exited within ``timeout_s``.

        After exit, give the reader a short grace to drain the pipe. A grandchild that
        keeps the pipe open (``server &``) must not block the call: the reader keeps
        logging in the background.
        """
        try:
            await asyncio.wait_for(self.exited(), timeout_s)
        except TimeoutError:
            return False
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(self.reader), 2.0)
        self._release()
        return True

    @property
    def active(self) -> bool:
        """Still running, or a process it left behind still writes to its output."""
        return self.proc.returncode is None or not self.reader.done()

    async def exited(self) -> int:
        """The exit status as soon as the process ends. (asyncio's Process.wait() also
        waits until its output pipe closes, which a child the command left running,
        `server &`, holds open: a finished command looked like one still running.)"""
        delay = 0.002
        while (code := self.proc.returncode) is None:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 0.05)
        return code

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

    async def kill_on_cancel(self) -> None:
        """kill() for a caller that is being cancelled: the kill (bounded by its own
        timeouts) is seen through even if the caller is cancelled again meanwhile, so a
        second Ctrl+C or a shutdown cannot leave the command running. A cancellation that
        arrived during the kill is raised afterwards."""
        kill = asyncio.ensure_future(self.kill())
        interrupted = False
        while not kill.done():
            try:
                await asyncio.shield(kill)
            except asyncio.CancelledError:
                interrupted = True
        if interrupted:
            raise asyncio.CancelledError

    async def kill(self) -> None:
        # A child the command left behind (`server &`) may still hold the output pipe
        # after the shell itself exited. On POSIX its process group outlives the shell and
        # can still be signalled; on Windows a dead PID may already be someone else's.
        leader_running = self.proc.returncode is None
        lingering = not self.reader.done() and sys.platform != "win32"
        if leader_running or lingering:
            self.killed = self.killed or leader_running
            await asyncio.to_thread(kill_tree, self.proc.pid)
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(self.exited(), 5.0)
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
