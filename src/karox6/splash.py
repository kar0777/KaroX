"""The first frame of the full-screen chat, painted before the heavy imports.

`karox6` in a terminal needs asyncio and prompt_toolkit before the app can draw anything:
about 300 ms of imports on a Windows PC, on top of the interpreter's own start. This
module paints that first frame at once with plain writes: the welcome card, the status
bar and the prompt, at the cells where the app draws them a moment later, from the same
text functions (``tui_text``), so the hand-over is not visible. Keys typed in between
wait in the terminal's input queue and reach the app when it starts reading. A POSIX
terminal would otherwise handle them itself until then (see ``_quiet_input``), so its
input mode is changed for that time and put back when the chat ends.

The frame is only painted when it can be exact: a terminal that understands escape
sequences, colours on, a window tall enough for the whole card, and text whose width
needs no tables (see ``tui_text.narrow``). Otherwise nothing is painted and the app
starts as before.
"""

from __future__ import annotations

import atexit
import contextlib
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, TextIO

from karox6.tui_text import (
    _ANSI,
    Fragments,
    Palette,
    StatusSnapshot,
    _git_branch,
    _glyphs,
    banner_text,
    box_bottom,
    box_sides,
    box_top,
    key_hints,
    narrow,
    status_bar_fragments,
)
from karox6.tui_theme import Theme

ENTER_ALTERNATE = "\x1b[?1049h"
LEAVE_ALTERNATE = "\x1b[?1049l"
# The card (9 lines) above the activity line, the input box (3) and the status bar, with
# room to spare.
MIN_ROWS = 15
BOTTOM_ROWS = 5  # activity line, input box (3 rows), status bar


class Splash:
    """The painted first frame: the alternate screen is ours until the app takes it."""

    def __init__(self, stream: TextIO, restore_input: Callable[[], None] | None = None) -> None:
        self._stream = stream
        self._restore_input = restore_input
        self.active = True

    def take_over(self) -> bool:
        """The app is about to draw. True when the frame was still up: the terminal is
        already in the alternate screen and must not be asked to enter it again."""
        if not self.active:
            return False
        self.active = False
        return True

    def close(self) -> None:
        """Leave the alternate screen if the app never took the frame over (it failed to
        start): the user's own screen comes back before any message is printed. The
        terminal's own input mode comes back too; that part is repeated on every call,
        because the app, when it ran, has put back the mode it found (ours), and the
        last call is the one made at exit."""
        if self.active:
            self.active = False
            _vt_write(self._stream, LEAVE_ALTERNATE)
        if self._restore_input is not None:
            self._restore_input()


def first_frame(
    *,
    version: str,
    model: str,
    provider: str,
    workspace: Path,
    approval_mode: str,
    context_window: int,
    encoding: str | None,
    rows: int,
    columns: int,
    theme: Theme | None = None,
    legacy_glyphs: bool = False,
) -> str | None:
    """The escape sequences and text of the first frame, or None when it cannot be exact."""
    if rows < MIN_ROWS or columns < 20:
        return None
    theme = theme or Theme("ansi", 4)
    paint = Palette(True, theme)
    glyph = _glyphs(encoding, legacy_glyphs)
    branch = _git_branch(workspace)
    card = banner_text(
        version=version,
        model=model,
        provider=provider,
        workspace=str(workspace),
        branch=branch,
        approval_mode=approval_mode,
        paint=paint,
        glyph=glyph,
        max_width=columns - 1,
    )
    lines = card.rstrip("\n").split("\n")
    snap = StatusSnapshot(
        model=model,
        provider=provider,
        workspace_name=workspace.name or str(workspace),
        branch=branch,
        approval_mode=approval_mode,
        running=False,
        activity="",
        total_tokens=0,
        context_used=0,
        context_window=context_window,
        context_pct=0,
        session_id=None,
    )
    left, right = box_sides(glyph)
    inner = columns - len(left) - len(right) - len(glyph["prompt"])
    bottom: list[Fragments] = [
        box_top(columns, glyph),
        [("subtle", left), ("accent+bold", glyph["prompt"]), ("", " " * inner), ("subtle", right)],
        box_bottom(columns, glyph, key_hints("prompt")),
        status_bar_fragments(snap, glyph, columns),
    ]
    plain = [_ANSI.sub("", line) for line in lines]
    plain += [text for row in bottom for _, text in row]
    if len(lines) > rows - BOTTOM_ROWS or not all(narrow(text) for text in plain):
        return None
    if any(len(text) > columns - 1 for text in plain[: len(lines)]):
        return None
    out = [ENTER_ALTERNATE, "\x1b[H\x1b[2J", "\r\n".join(lines)]
    for n, row in enumerate(bottom):
        out.append(f"\x1b[0m\x1b[{rows - 3 + n};1H")
        for roles, text in (piece for piece in row if piece[1]):
            codes = ";".join(c for c in (theme.sgr(r) for r in roles.split("+") if r) if c)
            out.append(f"\x1b[0;{codes}m{text}" if codes else f"\x1b[0m{text}")
    prompt_col = len(left) + len(glyph["prompt"]) + 1
    out.append(f"\x1b[0m\x1b[{rows - 2};{prompt_col}H")
    return "".join(out)


def show(
    *,
    version: str,
    model: str,
    provider: str,
    workspace: Path,
    approval_mode: str,
    context_window: int,
    stream: TextIO | None = None,
    theme: Theme | None = None,
    legacy_glyphs: bool = False,
) -> Splash | None:
    """Paint the first frame on the terminal; None when it was not painted."""
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") or os.environ.get("KAROX_NO_SPLASH"):
        return None
    if os.environ.get("TERM", "") == "dumb" or not _is_terminal(stream):
        return None
    try:
        size = os.get_terminal_size(stream.fileno())
    except (OSError, ValueError, AttributeError):
        return None
    # prompt_toolkit leaves the last column of a Windows console unused (the console
    # wraps there otherwise), so the app's frame is one cell narrower than the window.
    columns = size.columns - 1 if sys.platform == "win32" else size.columns
    frame = first_frame(
        version=version,
        model=model,
        provider=provider,
        workspace=workspace,
        approval_mode=approval_mode,
        context_window=context_window,
        encoding=getattr(stream, "encoding", None),
        rows=size.lines,
        columns=columns,
        theme=theme,
        legacy_glyphs=legacy_glyphs,
    )
    if frame is None:
        return None
    restore_input = _quiet_input()
    if not _vt_write(stream, frame):
        if restore_input is not None:
            restore_input()
        return None
    splash = Splash(stream, restore_input)
    atexit.register(splash.close)  # whatever happens, the user's screen comes back
    return splash


def _quiet_input(fd: int | None = None) -> Callable[[], None] | None:
    """Keep the terminal from handling keys itself while the frame is up (POSIX).

    Until the app switches to raw mode a POSIX terminal is in its line mode: it echoes
    what is typed (over the painted frame) and hands Enter on as a line feed, which the
    app reads as Ctrl+J, "new line", so a message typed at once would sit in the input
    box unsent. Echo, line editing and the CR/LF translation are switched off at once;
    Ctrl+C still interrupts the start. Returns the function that puts the mode back, or
    None when nothing was changed (Windows: a console queues key events as they are).
    """
    if sys.platform == "win32":
        return None
    import termios

    try:
        if fd is None:
            fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        quiet = termios.tcgetattr(fd)
    except (termios.error, OSError, ValueError, AttributeError):
        return None
    quiet[0] &= ~(termios.ICRNL | termios.INLCR | termios.IGNCR)  # input flags
    quiet[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN)  # local flags
    quiet[6][termios.VMIN] = 1  # a read returns as soon as one byte is there
    quiet[6][termios.VTIME] = 0
    try:
        termios.tcsetattr(fd, termios.TCSANOW, quiet)  # NOW: typed-ahead keys are kept
    except (termios.error, OSError):
        return None
    target = fd

    def restore() -> None:
        with contextlib.suppress(termios.error, OSError, ValueError):
            termios.tcsetattr(target, termios.TCSANOW, saved)

    return restore


def _is_terminal(stream: Any) -> bool:
    try:
        return bool(stream.isatty()) and bool(sys.stdin.isatty())
    except (AttributeError, ValueError):
        return False


def _vt_write(stream: TextIO, text: str) -> bool:
    """Write escape sequences to the terminal; False when it cannot interpret them (an
    old Windows console), in which case nothing is written."""
    if sys.platform != "win32":
        return _write(stream, text)
    import ctypes
    from ctypes import wintypes

    windll: Any = getattr(ctypes, "windll")  # noqa: B009 - Windows only
    kernel32 = windll.kernel32
    kernel32.GetStdHandle.restype = wintypes.HANDLE
    handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
    original = wintypes.DWORD(0)
    if not kernel32.GetConsoleMode(wintypes.HANDLE(handle), ctypes.byref(original)):
        return False
    # ENABLE_PROCESSED_OUTPUT | ENABLE_VIRTUAL_TERMINAL_PROCESSING, as prompt_toolkit sets
    # around each of its own writes.
    wanted = wintypes.DWORD(original.value | 0x0001 | 0x0004)
    if not kernel32.SetConsoleMode(wintypes.HANDLE(handle), wanted):
        return False
    try:
        return _write(stream, text)
    finally:
        kernel32.SetConsoleMode(wintypes.HANDLE(handle), original)


def _write(stream: TextIO, text: str) -> bool:
    try:
        stream.write(text)
        stream.flush()
    except (OSError, ValueError, UnicodeError):
        return False
    return True
