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

ConPTY leaves a stray ``[201~`` behind at the end of a paste. It closes the paste
only where a marker can be: at the end of a run of text, or followed by nothing but
the Enter that submits. The same five characters anywhere else are text. A marker
cut in two by a read boundary (``[20`` | ``1~``) is held back until the rest comes.

Only the Windows console needs any of this. A POSIX terminal sends a real
``[200~ … [201~`` that ``Vt100Parser`` delivers as one ``BracketedPaste`` press, and
the burst heuristics would only misread auto-repeat or an input method's burst
there, so ``coalescing_input`` leaves such an input alone.
"""

from __future__ import annotations

import sys
import time
from typing import TYPE_CHECKING

from prompt_toolkit.key_binding.key_processor import KeyPress
from prompt_toolkit.keys import Keys

if TYPE_CHECKING:
    from prompt_toolkit.input.base import Input

_BULK_CHARS = 32  # more text than this in one batch is a paste, not typing
_HOT_SECONDS = 0.1  # after a merged batch, text this soon after merges too
_NEWLINE_KEYS = (Keys.ControlJ, Keys.ControlM)
_END_MARK = "[201~"  # what ConPTY leaves of the paste-end marker
_START_MARK = "[200~"


def _windows_console() -> bool:
    return sys.platform == "win32"


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


def _partial_marker(text: str, hot: bool) -> int:
    """How many trailing characters of ``text`` may be the start of a paste-end marker
    still to arrive (one character only counts inside a paste: a typed ``[`` must not
    wait)."""
    for size in range(len(_END_MARK) - 1, 0, -1):
        if text.endswith(_END_MARK[:size]):
            return size if size > 1 or hot else 0
    return 0


class _PasteRun(KeyPress):
    """A ``BracketedPaste`` the heuristics made of plain key events (a real one from the
    terminal is an ordinary ``KeyPress``)."""

    def __init__(self, data: str) -> None:
        super().__init__(Keys.BracketedPaste, data)


class _Batch:
    """What ``merge_batch`` made of a batch of presses."""

    def __init__(self) -> None:
        self.out: list[KeyPress] = []
        self.held: list[KeyPress] = []  # the start of a marker whose rest is not here yet
        self.open = False  # a merged paste may continue in the next batch
        self.ended = False  # a marker closed the paste


def merge_batch(keys: list[KeyPress], hot: bool, hold: bool = False) -> _Batch:
    """Merge each run of text presses that a paste would make into one
    ``BracketedPaste`` press. ``hot`` merges every text run (the tail of a paste
    still arriving). A paste-end marker closes the paste: what comes after it (an
    Enter that submits) stays ordinary key presses. With ``hold`` the start of a
    marker at the very end is returned in ``held`` for the caller to put in front of
    the next batch."""
    batch = _Batch()
    run: list[KeyPress] = []

    def drop_escape() -> None:
        # ConPTY hands the marker over as an Escape press and its text: the Escape
        # would wait for a key to join (an Enter right after the paste: Alt+Enter).
        if batch.out and batch.out[-1].key == Keys.Escape:
            batch.out.pop()

    def flush(last: bool) -> None:
        nonlocal hot
        if not run:
            return
        chars = [_press_char(p) or "" for p in run]
        text = "".join(chars)
        exact = len(text) == len(run)  # one character per press: indexes line up
        cut = len(text)
        if exact:
            at = text.rfind(_END_MARK)
            if at >= 0 and not text[at + len(_END_MARK) :].strip("\r\n"):
                cut = at  # the marker has only Enter keys after it: it is the end
        if cut < len(text):
            if cut == 0 or text.startswith(_START_MARK):
                drop_escape()
            data = "".join(chars[:cut]).removeprefix(_START_MARK)
            if data:
                batch.out.append(_PasteRun(data))
            batch.out.extend(run[cut + len(_END_MARK) :])
            batch.open, batch.ended, hot = False, True, False
        else:
            keep = len(run)
            if hold and last and exact:
                size = _partial_marker(text, hot or batch.open)
                batch.held = run[len(run) - size :] if size else []
                keep -= size
            body = run[:keep]
            ends = sum(p.key in _NEWLINE_KEYS for p in body)
            inside = ends - (1 if body and body[-1].key in _NEWLINE_KEYS else 0)
            if body and (hot or inside >= 2 or keep - ends >= _BULK_CHARS):
                if text.startswith(_START_MARK):
                    drop_escape()
                data = "".join(chars[:keep]).removeprefix(_START_MARK)
                if data:
                    batch.out.append(_PasteRun(data))
                    batch.open, batch.ended = True, False
            else:
                batch.out.extend(body)
        run.clear()

    for press in keys:
        if _press_char(press) is None:
            flush(False)
            batch.out.append(press)
        else:
            run.append(press)
    flush(True)
    return batch


def merge_pasted_runs(keys: list[KeyPress], hot: bool) -> tuple[list[KeyPress], bool]:
    """``merge_batch`` without a marker held back: the presses and whether a merged
    paste is still open (the next batch should merge too)."""
    batch = merge_batch(keys, hot)
    return batch.out, batch.open


class _CoalescingInput:
    """An ``Input`` wrapper: ``read_keys`` merges a paste's key-event burst.

    A merged paste may go on in the next read, so it is kept back until its marker, any
    other key, or ``flush_keys`` (prompt_toolkit calls it when no read follows soon)
    ends it. It is handed over whole and always before whatever comes after it, so the
    app inserts it in one piece and in order."""

    def __init__(self, inner: Input) -> None:
        self._inner = inner
        self._hot_until = 0.0
        self._held: list[KeyPress] = []
        self._pending: str | None = None  # a merged paste not handed over yet

    def _merge(self, keys: list[KeyPress], reading: bool) -> list[KeyPress]:
        keys, self._held = [*self._held, *keys], []
        now = time.monotonic()
        batch = merge_batch(keys, self._pending is not None or now <= self._hot_until, hold=reading)
        self._held = batch.held
        out: list[KeyPress] = []
        pending = self._pending
        for press in batch.out:
            if isinstance(press, _PasteRun):
                pending = (pending or "") + (press.data or "")
                continue
            if pending is not None:
                out.append(KeyPress(Keys.BracketedPaste, pending))
                pending = None
            out.append(press)
        if pending is not None and (batch.ended or not reading):
            out.append(KeyPress(Keys.BracketedPaste, pending))
            pending = None
        self._pending = pending
        if batch.open:
            self._hot_until = now + _HOT_SECONDS
        elif batch.ended or not reading:
            self._hot_until = 0.0
        return out

    def read_keys(self) -> list[KeyPress]:
        return self._merge(list(self._inner.read_keys()), True)

    def flush_keys(self) -> list[KeyPress]:
        return self._merge(list(self._inner.flush_keys()), False)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


def coalescing_input(inner: Input) -> Input:
    """``inner`` with paste bursts merged into ``BracketedPaste`` presses (a Windows
    console; any other input is returned as it is)."""
    if not _windows_console():
        return inner
    return _CoalescingInput(inner)  # type: ignore[return-value]
