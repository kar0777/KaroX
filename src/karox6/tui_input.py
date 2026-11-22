"""Turning a burst of key events into one ``Keys.BracketedPaste``.

A Windows console has no bracketed paste a program can see: ConPTY (and the
classic console) eat the ``[200~`` / ``[201~`` markers and a paste arrives as
ordinary key events, a 300 KB one as ~300,000 of them — the input buffer would
spend seconds inserting a character at a time. A batch of queued presses that is
nothing but text is therefore merged here into a single ``BracketedPaste``
keypress before the key processor sees it, so the paste takes the real paste
path: one event, one insert, one placeholder.

A batch counts as a paste when it carries at least two line ends inside it (one
line end can just be an Enter a fast typist pressed together with a character —
that one is left out so it still submits) or simply more text than fingers make
in one read. Once a batch merged, the batches right after it merge too, so a
paste long enough to span several reads stays one paste instead of leaking its
tail in as keystrokes.

POSIX terminals are untouched by this: a real ``[200~ … [201~`` already reaches
``Vt100Parser`` as one ``BracketedPaste`` press, which is not a text press and
passes through here unchanged.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys

if TYPE_CHECKING:
    from prompt_toolkit.input.base import Input

_BULK_CHARS = 32  # more text than this in one batch is a paste, not typing
_HOT_SECONDS = 0.1  # after a merged batch, text this soon after merges too
_NEWLINE_KEYS = (Keys.ControlJ, Keys.ControlM)


def _press_char(press: KeyPress) -> str | None:
    """The character a press types, or ``None`` for a real key (Escape, an arrow,
    Ctrl+V — anything a paste must not swallow)."""
    if not isinstance(press.key, Keys):
        return press.data or press.key
    if press.key == Keys.ControlJ:
        return "\n"
    if press.key == Keys.ControlM:
        return "\r"  # a CR LF pair collapses when the paste is normalized later
    if press.key == Keys.ControlI:
        return "\t"
    return None


def merge_pasted_runs(keys: list[KeyPress], hot: bool) -> tuple[list[KeyPress], bool]:
    """Merge each run of text presses that a paste would make into one
    ``BracketedPaste`` press. ``hot`` merges every text run (the tail of a paste
    still arriving); returns whether anything merged, so the caller keeps the
    paste hot for the next batch."""
    out: list[KeyPress] = []
    run: list[KeyPress] = []
    merged = False

    def flush() -> None:
        nonlocal merged
        if not run:
            return
        ends = sum(p.key in _NEWLINE_KEYS for p in run)
        inside = ends - (1 if run[-1].key in _NEWLINE_KEYS else 0)
        chars = len(run) - ends
        if hot or inside >= 2 or chars >= _BULK_CHARS:
            data = "".join(_press_char(p) or "" for p in run)
            # ConPTY eats the markers except for a stray "[201~" tail it leaves
            # behind; a "[200~" head goes with it in case a console is kinder.
            data = data.removeprefix("[200~").removesuffix("[201~")
            if data:
                out.append(KeyPress(Keys.BracketedPaste, data))
                merged = True
        else:
            out.extend(run)
        run.clear()

    for press in keys:
        if _press_char(press) is None:
            flush()
            out.append(press)
        else:
            run.append(press)
    flush()
    return out, merged


class _CoalescingInput:
    """An ``Input`` wrapper: ``read_keys`` merges a paste's key-event burst."""

    def __init__(self, inner: Input) -> None:
        self._inner = inner
        self._hot_until = 0.0

    def _merge(self, keys: list[KeyPress]) -> list[KeyPress]:
        out, merged = merge_pasted_runs(keys, time.monotonic() <= self._hot_until)
        if merged:
            self._hot_until = time.monotonic() + _HOT_SECONDS
        return out

    def read_keys(self) -> list[KeyPress]:
        return self._merge(list(self._inner.read_keys()))

    def flush_keys(self) -> list[KeyPress]:
        return self._merge(list(self._inner.flush_keys()))

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def coalescing_input(inner: Input) -> Input:
    """``inner`` with paste bursts merged into ``BracketedPaste`` presses."""
    return _CoalescingInput(inner)  # type: ignore[return-value]
