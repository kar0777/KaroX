"""`karox mcp --http ... --supervise`: the server stays up unattended.

The supervisor runs the real server as a child process and starts it again when it exits
on its own (a crash, a killed process): after 1 s, then twice as long each time up to a
minute; a child that ran five minutes was healthy, so the wait starts at 1 s again. Exit
code 2 means bad arguments or configuration, which a restart would only repeat, so the
supervisor stops as well.

Stopping the supervisor (Ctrl+C, SIGTERM; CTRL_BREAK on Windows) stops the child politely
first, so its cleanup runs and removes its Tailscale route. The child gets its own process
group: Ctrl+C in a terminal reaches only the supervisor, which then decides. On Windows
CTRL_BREAK is the only console signal one process can send another's group; `karox mcp`
handles it like SIGTERM (see cli.run_async).
"""

from __future__ import annotations

import contextlib
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from types import FrameType
from typing import Any

BAD_CONFIG = 2  # `karox mcp` exit code for bad arguments or configuration
TERMINATED = 143  # 128 + SIGTERM: `karox mcp` exits so on SIGTERM, the supervisor too


def without_flag(argv: Sequence[str], flag: str) -> list[str]:
    """``argv`` minus the switch ``flag``, also abbreviated: argparse already accepted
    ``argv``, so a ``--`` prefix of ``flag`` in it can only be that switch (an ambiguous one
    or one used as a value would have been an error)."""
    return [a for a in argv if not (len(a) > 2 and a.startswith("--") and flag.startswith(a))]


class Supervisor:
    def __init__(
        self,
        command: Sequence[str],
        *,
        restart_command: Sequence[str] | None = None,
        first_delay: float = 1.0,
        max_delay: float = 60.0,
        healthy_after: float = 300.0,
        stop_timeout: float = 10.0,
        poll: float = 0.2,
        write: Callable[[str], object] | None = None,
    ) -> None:
        self.command = list(command)
        self.restart_command = list(restart_command or command)
        self.first_delay = first_delay
        self.max_delay = max_delay
        self.healthy_after = healthy_after
        self.stop_timeout = stop_timeout
        self.poll = poll
        self.write = write or _stderr
        self._stop_code: int | None = None
        self._stop_requests = 0

    def stop(self, code: int = 0) -> None:
        """Stop the child, then return ``code`` from run(); asked again, kill the child at
        once. Safe from a signal handler or another thread: it only sets fields."""
        if self._stop_code is None:
            self._stop_code = code
        self._stop_requests += 1

    def stopping(self) -> bool:
        return self._stop_code is not None

    def run(self) -> int:
        previous = self._catch_signals()
        try:
            return self._loop()
        finally:
            for signum, handler in previous.items():
                signal.signal(signum, handler)

    def _loop(self) -> int:
        delay = self.first_delay
        command = self.command
        while not self.stopping():
            started = time.monotonic()
            code = self._run_once(command)
            command = self.restart_command
            if code is None or self.stopping():
                break
            if code == BAD_CONFIG:
                self.write(
                    "karox mcp: the server exited with code 2 (bad arguments or "
                    "configuration); not restarting it\n"
                )
                return BAD_CONFIG
            if time.monotonic() - started >= self.healthy_after:
                delay = self.first_delay
            self.write(
                f"karox mcp: the server exited with code {code}; restarting in {delay:g} s\n"
            )
            self._sleep(delay)
            delay = min(delay * 2, self.max_delay)
        return self._stop_code or 0

    def _run_once(self, command: list[str]) -> int | None:
        """The child's exit code, or None when the supervisor was asked to stop."""
        child = _start(command)
        try:
            while not self.stopping():
                with contextlib.suppress(subprocess.TimeoutExpired):
                    return child.wait(timeout=self.poll)
            return None
        finally:
            if child.poll() is None:
                self._stop_child(child)

    def _stop_child(self, child: subprocess.Popen[bytes]) -> None:
        """Ask the child to stop so its cleanup runs; kill it after stop_timeout, or at once
        once the supervisor was asked to stop twice (a second Ctrl+C)."""
        try:
            if sys.platform == "win32":
                child.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                child.send_signal(signal.SIGTERM)
            deadline = time.monotonic() + self.stop_timeout
        except OSError:  # no console to carry CTRL_BREAK: nothing polite is left
            deadline = 0.0
        while child.poll() is None:
            if time.monotonic() >= deadline or self._stop_requests > 1:
                child.kill()
                break
            with contextlib.suppress(subprocess.TimeoutExpired):
                child.wait(timeout=self.poll)
        child.wait()

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self.stopping() and (left := deadline - time.monotonic()) > 0:
            time.sleep(min(left, self.poll))

    def _catch_signals(self) -> dict[signal.Signals, Any]:
        """Ctrl+C stops with 0 and SIGTERM (CTRL_BREAK on Windows) with 143, as `karox mcp`
        does without --supervise. Short polls in the waits let the handlers run promptly."""
        codes = {signal.SIGINT: 0, signal.SIGTERM: TERMINATED}
        if sys.platform == "win32":
            codes[signal.SIGBREAK] = TERMINATED
        previous: dict[signal.Signals, Any] = {}
        for signum, code in codes.items():
            try:
                old = signal.signal(signum, self._handler(code))
            except ValueError:  # not the main thread (tests): stop() is called directly
                break
            if old is not None:
                previous[signum] = old
        return previous

    def _handler(self, code: int) -> Callable[[int, FrameType | None], None]:
        def handle(_signum: int, _frame: FrameType | None) -> None:
            self.stop(code)

        return handle


def _start(command: list[str]) -> subprocess.Popen[bytes]:
    from karox6.lifetime import bind_to_karox, join_karox_job

    # stdout and stderr pass through; the HTTP server never reads stdin. On Windows the
    # child is born in KaroX's kill-on-exit job: a supervisor that is killed outright
    # takes the server with it instead of leaving it holding the port.
    if sys.platform == "win32":
        join_karox_job()
        child = subprocess.Popen(
            command, stdin=subprocess.DEVNULL, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP
        )
        bind_to_karox(child.pid)
        return child
    return subprocess.Popen(command, stdin=subprocess.DEVNULL, process_group=0)


def _stderr(text: str) -> None:
    sys.stderr.write(text)
    sys.stderr.flush()
