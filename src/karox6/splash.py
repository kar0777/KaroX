"""The first frame of the full-screen chat, painted before the heavy imports.

`karox6` in a terminal needs asyncio and prompt_toolkit before the app can draw anything:
about 300 ms of imports on a Windows PC, on top of the interpreter's own start. This
module paints that first frame at once with plain writes: the welcome card, the status
bar and the prompt, at the cells where the app draws them a moment later, from the same
text functions (``tui_text``), so the hand-over is not visible. Keys typed in between
wait in the terminal's input queue and reach the app when it starts reading.

The frame is only painted when it can be exact: a terminal that understands escape
sequences, colours on, a window tall enough for the whole card, and text whose width
needs no tables (see ``tui_text.narrow``). Otherwise nothing is painted and the app
starts as before.
"""

from __future__ import annotations

import atexit
import os
import sys
from pathlib import Path
from typing import Any, TextIO

from karox6.tui_text import (
    _ANSI,
    Palette,
    StatusSnapshot,
    _git_branch,
    _glyphs,
    banner_text,
    narrow,
    status_bar_fragments,
)

ENTER_ALTERNATE = "\x1b[?1049h"
LEAVE_ALTERNATE = "\x1b[?1049l"
MIN_ROWS = 12  # the card (9 lines) above the status bar and the prompt, with room to spare
# The app's own colours (tui_app._build_style) as 256-colour codes; each of them is a
# colour of that palette, so both renderings are the same on screen.
_BAR = "48;5;235"
_BAR_STYLES = {
    "": "38;5;254",
    "model": "1;38;5;117",
    "dim": "38;5;247",
    "activity": "1;38;5;221",
    "scroll": "1;38;5;215",
    "allow": "1;38;5;221",
    "deny": "1;38;5;203",
}


class Splash:
    """The painted first frame: the alternate screen is ours until the app takes it."""

    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
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
        start): the user's own screen comes back before any message is printed."""
        if self.active:
            self.active = False
            _vt_write(self._stream, LEAVE_ALTERNATE)


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
) -> str | None:
    """The escape sequences and text of the first frame, or None when it cannot be exact."""
    if rows < MIN_ROWS or columns < 20:
        return None
    paint = Palette(True)
    glyph = _glyphs(encoding)
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
    bar = status_bar_fragments(snap, glyph, columns)
    plain = [_ANSI.sub("", line) for line in lines] + [text for _, text in bar]
    if len(lines) > rows - 2 or not all(narrow(text) for text in plain):
        return None
    if any(len(text) > columns - 1 for text in plain[: len(lines)]):
        return None
    out = [ENTER_ALTERNATE, "\x1b[H\x1b[2J", "\r\n".join(lines)]
    out.append(f"\x1b[{rows - 1};1H")
    out.extend(f"\x1b[0;{_BAR};{_BAR_STYLES[style]}m{text}" for style, text in bar)
    out.append(f"\x1b[0m\x1b[{rows};1H\x1b[0;1;36m{glyph['prompt']}\x1b[0m")
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
    )
    if frame is None or not _vt_write(stream, frame):
        return None
    splash = Splash(stream)
    atexit.register(splash.close)  # whatever happens, the user's screen comes back
    return splash


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
