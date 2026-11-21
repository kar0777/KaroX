"""A real pseudo-terminal with a screen emulator, for end-to-end tests of the chat UI.

The program runs in a ConPTY (Windows, through pywinpty) or a POSIX pty; everything it
writes goes through pyte, a VT100/xterm emulator, so a test sees the screen a user would
see, not prompt_toolkit's idea of it. Keys are written the way a terminal sends them.
"""

from __future__ import annotations

import codecs
import contextlib
import copy
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

import pyte

KEYS = {
    "enter": "\r",
    "esc": "\x1b",
    "tab": "\t",
    "shift-tab": "\x1b[Z",
    "backspace": "\x7f",
    "ctrl-c": "\x03",
    "ctrl-d": "\x04",
    "ctrl-j": "\n",
    "ctrl-r": "\x12",
    "ctrl-a": "\x01",
    "ctrl-e": "\x05",
    "alt-enter": "\x1b\r",
    "up": "\x1b[A",
    "down": "\x1b[B",
    "right": "\x1b[C",
    "left": "\x1b[D",
    "home": "\x1b[H",
    "end": "\x1b[F",
    "pageup": "\x1b[5~",
    "pagedown": "\x1b[6~",
    "ctrl-home": "\x1b[1;5H",
    "ctrl-end": "\x1b[1;5F",
    "paste-start": "\x1b[200~",
    "paste-end": "\x1b[201~",
}
_ALT_MODES = (47, 1047, 1049)


class AltScreen(pyte.Screen):
    """pyte's screen plus the alternate screen buffer, which pyte itself ignores: a
    full-screen program must give the user's scrollback back when it leaves."""

    def __init__(self, columns: int, lines: int) -> None:
        super().__init__(columns, lines)
        self.alternate = False
        self.alt_entered = 0
        self.alt_left = 0
        self._main: Any = None

    def set_mode(self, *modes: int, **kwargs: Any) -> None:
        if kwargs.get("private") and any(m in _ALT_MODES for m in modes) and not self.alternate:
            self.alternate = True
            self.alt_entered += 1
            self._main = (copy.deepcopy(self.buffer), copy.copy(self.cursor))
            self.erase_in_display(2)
        super().set_mode(*modes, **kwargs)

    def reset_mode(self, *modes: int, **kwargs: Any) -> None:
        if kwargs.get("private") and any(m in _ALT_MODES for m in modes) and self.alternate:
            self.alternate = False
            self.alt_left += 1
            if self._main is not None:
                self.buffer, self.cursor = self._main
                self._main = None
            self.dirty.update(range(self.lines))
        super().reset_mode(*modes, **kwargs)


class Terminal:
    def __init__(
        self,
        argv: list[str],
        *,
        rows: int = 30,
        cols: int = 100,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        watch: tuple[str, ...] = (),
    ) -> None:
        self.rows, self.cols = rows, cols
        self.screen = AltScreen(cols, rows)
        self.stream = pyte.Stream(self.screen)
        self.raw = ""  # every character the program wrote, escape sequences included
        self.started = time.perf_counter()
        self.first_output: float | None = None  # seconds from start to the first byte
        self.chunks: list[tuple[float, int]] = []  # (seconds from start, characters) per read
        self._watched: dict[str, float | None] = dict.fromkeys(watch)
        self._lock = threading.Lock()
        self._fd = -1
        self._decode = codecs.getincrementaldecoder("utf-8")("replace").decode
        self._child: Any = None  # POSIX: subprocess.Popen on the pty
        self._proc: Any = None  # Windows: winpty's PtyProcess
        if sys.platform == "win32":
            from winpty import PtyProcess

            self._proc = PtyProcess.spawn(argv, cwd=cwd, env=env, dimensions=(rows, cols))
        else:
            self._spawn_posix(argv, env, cwd)
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    # -- process ------------------------------------------------------------------------

    def _spawn_posix(self, argv: list[str], env: dict[str, str] | None, cwd: str | None) -> None:
        import fcntl
        import pty
        import struct
        import subprocess
        import termios

        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", self.rows, self.cols, 0, 0))
        # A new session with the pty as its controlling terminal, the way a terminal
        # emulator starts a program. A one-line launcher does it after exec, not Python
        # code between fork and exec (pty.fork): this process has threads (the scripted
        # model's server), and a forked copy of a threaded interpreter can deadlock.
        launcher = (
            "import fcntl, os, sys, termios; os.setsid(); "
            "fcntl.ioctl(0, termios.TIOCSCTTY, 0); os.execvp(sys.argv[1], sys.argv[1:])"
        )
        try:
            self._child = subprocess.Popen(
                [sys.executable, "-c", launcher, *argv],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=env,
                cwd=cwd,
            )
        finally:
            os.close(slave)
        self._fd = master

    def _read(self) -> None:
        while True:
            try:
                if self._proc is not None:
                    data = self._proc.read(4096)
                else:
                    raw = os.read(self._fd, 4096)
                    if not raw:
                        break  # end of file (macOS, BSD; Linux raises EIO instead)
                    data = self._decode(raw)  # a character may be split between reads
            except (EOFError, OSError):
                break
            if not data:
                if self._proc is not None and not self._proc.isalive():
                    break
                continue
            with self._lock:
                now = time.perf_counter() - self.started
                if self.first_output is None:
                    self.first_output = now
                self.chunks.append((now, len(data)))
                self.raw += data
                self.stream.feed(data)
                if any(seen is None for seen in self._watched.values()):
                    shown = "\n".join(self.screen.display)
                    for text, seen in self._watched.items():
                        if seen is None and text in shown:
                            self._watched[text] = now

    def alive(self) -> bool:
        if self._proc is not None:
            return bool(self._proc.isalive())
        return self._child.poll() is None

    def wait_exit(self, timeout: float = 10.0) -> int:
        end = time.perf_counter() + timeout
        while self.alive():
            if time.perf_counter() > end:
                raise AssertionError(f"still running after {timeout}s\n{self.dump()}")
            time.sleep(0.02)
        self._reader.join(2.0)
        if self._proc is not None:
            return int(self._proc.exitstatus or 0)
        return int(self._child.returncode or 0)

    def close(self) -> None:
        if self._proc is not None:
            if self._proc.isalive():
                self._proc.terminate(force=True)
            with contextlib.suppress(Exception):
                self._proc.close()
        elif self._child is not None and self.alive():
            self._child.kill()
            self._child.wait()
        if self._fd >= 0:
            with contextlib.suppress(OSError):
                os.close(self._fd)
            self._fd = -1

    # -- input --------------------------------------------------------------------------

    def send(self, text: str) -> None:
        if self._proc is not None:
            self._proc.write(text)
        else:
            os.write(self._fd, text.encode("utf-8"))

    def key(self, *names: str) -> None:
        for name in names:
            self.send(KEYS[name])
            time.sleep(0.03)

    def line(self, text: str) -> None:
        self.send(text)
        time.sleep(0.05)
        self.key("enter")

    def paste(self, text: str) -> None:
        """Bracketed paste, as terminals send it."""
        self.send(KEYS["paste-start"] + text + KEYS["paste-end"])

    def resize(self, rows: int, cols: int) -> None:
        with self._lock:
            self.rows, self.cols = rows, cols
            self.screen.resize(rows, cols)
        if self._proc is not None:
            self._proc.setwinsize(rows, cols)
        else:
            import fcntl
            import struct
            import termios

            fcntl.ioctl(self._fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def input_mode(self) -> dict[str, bool] | None:
        """How the terminal itself treats typed keys right now (POSIX): echo, line editing
        and reading Enter (CR) as a line feed, all on in the line mode a program finds
        and must leave behind. None on Windows, where a console has no such mode."""
        if self._proc is not None:
            return None
        import termios

        attrs = termios.tcgetattr(self._fd)
        return {
            "echo": bool(attrs[3] & termios.ECHO),
            "line": bool(attrs[3] & termios.ICANON),
            "cr_as_lf": bool(attrs[0] & termios.ICRNL),
        }

    # -- timing -------------------------------------------------------------------------

    def watch(self, text: str) -> None:
        """Record when ``text`` first shows on the screen (checked on every read, so the
        time is exact to the read, not to a polling interval)."""
        with self._lock:
            shown = "\n".join(self.screen.display)
            self._watched[text] = (time.perf_counter() - self.started) if text in shown else None

    def seen_at(self, text: str, timeout: float = 15.0) -> float:
        """Seconds from the program's start until ``text`` was first on the screen."""
        end = time.perf_counter() + timeout
        while True:
            with self._lock:
                seen = self._watched.get(text)
            if seen is not None:
                return seen
            if time.perf_counter() > end:
                raise AssertionError(f"{text!r} never showed\n{self.dump()}")
            time.sleep(0.005)

    # -- screen -------------------------------------------------------------------------

    def lines(self) -> list[str]:
        with self._lock:
            return [line.rstrip() for line in self.screen.display]

    def text(self) -> str:
        return "\n".join(self.lines())

    def dump(self) -> str:
        frame = "\n".join(f"|{line}" for line in self.lines())
        mode = f"alternate={self.screen.alternate}"
        return f"--- screen {self.rows}x{self.cols} ({mode}) ---\n{frame}"

    def wait(self, what: str | Callable[[str], bool], timeout: float = 15.0) -> str:
        """Wait until ``what`` (a substring, or a predicate on the screen text) holds."""

        def check(text: str) -> bool:
            return what in text if isinstance(what, str) else what(text)

        end = time.perf_counter() + timeout
        while True:
            text = self.text()
            if check(text):
                return text
            if time.perf_counter() > end:
                raise AssertionError(f"timed out waiting for {what!r}\n{self.dump()}")
            if not self.alive():
                time.sleep(0.3)  # let the reader thread drain the last output
                text = self.text()
                if check(text):
                    return text
                raise AssertionError(f"the program exited before {what!r}\n{self.dump()}")
            time.sleep(0.02)

    def wait_gone(self, what: str, timeout: float = 15.0) -> str:
        return self.wait(lambda s: what not in s, timeout)
