"""Full-screen terminal UI built with prompt_toolkit's Application.

Keeps ChatApp's logic untouched by implementing its injected I/O callables
(``write``, ``read_line``, ``read_answer``, ``read_secret``) and ``display_factory``
over one persistent alternate-screen ``Application``.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import re
import sys
import time
from bisect import bisect_right
from collections import deque
from collections.abc import AsyncIterator, Callable, Coroutine
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, Any, NamedTuple, cast, override

from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, ThreadedCompleter
from prompt_toolkit.data_structures import Point
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition, has_completions, is_searching
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples
from prompt_toolkit.history import History
from prompt_toolkit.input import Input
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout import (
    Dimension,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import PasswordProcessor
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType
from prompt_toolkit.output import ColorDepth, Output
from prompt_toolkit.styles import Style
from prompt_toolkit.utils import get_cwidth
from prompt_toolkit.widgets import SearchToolbar

from karox6.tui import (
    _CLEAR_SCREEN,
    ChatApp,
    _PromptInterrupt,
    build_completer,
    build_history,
    pasted_line_end,
)
from karox6.tui_input import coalescing_input
from karox6.tui_paste import WARN_BYTES, PasteChunks
from karox6.tui_picker import ROWS as PICK_ROWS
from karox6.tui_picker import PickState
from karox6.tui_text import (
    DECO_END,
    DECO_START,
    StatusSnapshot,
    _git_branch,
    _glyphs,
    _plain_text,
    box_bottom,
    box_sides,
    box_top,
    cell_width,
    clip_cells,
    key_hints,
    narrow,
    status_bar_fragments,
)
from karox6.tui_theme import Theme, xterm_hex

if TYPE_CHECKING:
    from prompt_toolkit.key_binding.key_bindings import NotImplementedOrNone
    from prompt_toolkit.layout.containers import WindowRenderInfo

    from karox6.splash import Splash

MAX_LOGICAL_LINES = 20_000
_TRIM_BATCH = 2_000
_WHEEL_LINES = 3
_TAB = 4

Run = tuple[str, str]  # (prompt_toolkit style, text without control characters)

_SGR = re.compile("\x1b\\[([0-9;]*)m")
_FG = {
    30: "ansiblack",
    31: "ansired",
    32: "ansigreen",
    33: "ansiyellow",
    34: "ansiblue",
    35: "ansimagenta",
    36: "ansicyan",
    37: "ansigray",
    90: "ansibrightblack",
    91: "ansibrightred",
    92: "ansibrightgreen",
    93: "ansibrightyellow",
    94: "ansibrightblue",
    95: "ansibrightmagenta",
    96: "ansibrightcyan",
    97: "ansiwhite",
}
_FLAGS = {1: "bold", 2: "dim", 3: "italic", 4: "underline", 7: "reverse"}
_FLAG_OFF = {22: ("bold", "dim"), 23: ("italic",), 24: ("underline",), 27: ("reverse",)}


_ERASE_LINE = "\x1b[K"
_FILL_MARK = "\ue000"
FILL = "class:fill"
DECO = "class:deco"  # what the app draws around the text (a quote's bar, a rule), not text


def _runs(text: str) -> list[Run]:
    """``text`` as styled runs, the stretches the chat wrapped in ``Palette.frame`` marks
    (``DECO_START`` .. ``DECO_END``) flagged ``class:deco``: copying leaves them out."""
    if DECO_START not in text:
        return _fill_runs(text)
    out: list[Run] = []
    head, *pieces = text.split(DECO_START)
    out += _fill_runs(head)
    for piece in pieces:
        inner, _, rest = piece.partition(DECO_END)
        out += [(f"{style} {DECO}".strip(), chunk) for style, chunk in _fill_runs(inner)]
        out += _fill_runs(rest)
    return out


def _fill_runs(text: str) -> list[Run]:
    """``text`` (plain, or with SGR colour codes) as styled runs. "Erase to the end of the
    line" (``ESC [ K``) after coloured text, which a terminal answers by painting the rest
    of the line in the current background, marks that run ``class:fill``: the pane pads it
    to the edge at whatever width it is drawn (tinted diff lines)."""
    if _ERASE_LINE not in text:
        return _sgr_runs(text)
    out: list[Run] = []
    for style, chunk in _sgr_runs(text.replace(_ERASE_LINE, _FILL_MARK)):
        if _FILL_MARK in chunk:
            head = chunk.replace(_FILL_MARK, "")
            if head:
                out.append((f"{style} {FILL}".strip(), head))
        else:
            out.append((style, chunk))
    return out


def _sgr_runs(text: str) -> list[Run]:
    """``text`` (plain, or with SGR colour codes) as styled runs. The codes the chat itself
    writes are read here directly (a run per colour change, not a fragment per character);
    anything richer goes through prompt_toolkit's ANSI parser."""
    if "\x1b" not in text:
        return [("", text)] if text else []
    out: list[Run] = []
    color = ""
    background = ""
    flags: list[str] = []
    pos = 0
    for match in _SGR.finditer(text):
        if match.start() > pos:
            style = " ".join(x for x in (color, background, *flags) if x)
            out.append((style, text[pos : match.start()]))
        pos = match.end()
        codes = [int(raw or 0) for raw in (match.group(1) or "0").split(";")]
        i = 0
        while i < len(codes):
            code = codes[i]
            if code in (38, 48) and i + 1 < len(codes):
                # 256 colours (5;n) and true colour (2;r;g;b), as the theme writes them
                if codes[i + 1] == 5 and i + 2 < len(codes):
                    value = _xterm_hex(codes[i + 2])
                    i += 3
                elif codes[i + 1] == 2 and i + 4 < len(codes):
                    value = "#{:02x}{:02x}{:02x}".format(*codes[i + 2 : i + 5])
                    i += 5
                else:
                    return _merge(list(ANSI(text).__pt_formatted_text__()))
                if code == 38:
                    color = value
                else:
                    background = f"bg:{value}"
                continue
            if code == 0:
                color, background, flags = "", "", []
            elif code in _FG:
                color = _FG[code]
            elif code == 39:
                color = ""
            elif code == 49:
                background = ""
            elif code in _FLAGS:
                if _FLAGS[code] not in flags:
                    flags.append(_FLAGS[code])
            elif code in _FLAG_OFF:
                flags = [f for f in flags if f not in _FLAG_OFF[code]]
            else:  # other backgrounds and rarer codes: the full parser knows them
                return _merge(list(ANSI(text).__pt_formatted_text__()))
            i += 1
    if "\x1b" in text[pos:]:  # some other escape sequence
        return _merge(list(ANSI(text).__pt_formatted_text__()))
    if pos < len(text):
        out.append((" ".join(x for x in (color, background, *flags) if x), text[pos:]))
    return out


def _xterm_hex(index: int) -> str:
    """An xterm-256 palette entry as a prompt_toolkit colour."""
    if index < 16:
        return _FG[30 + index] if index < 8 else _FG[82 + index]
    return f"#{xterm_hex(index)}"


def _merge(fragments: StyleAndTextTuples) -> list[Run]:
    """Neighbouring fragments of one style as one run (the ANSI parser yields a fragment
    per character)."""
    out: list[Run] = []
    style = ""
    chars: list[str] = []
    for item in fragments:
        if item[0] != style and chars:
            out.append((style, "".join(chars)))
            chars = []
        style = item[0]
        chars.append(item[1])
    if chars:
        out.append((style, "".join(chars)))
    return out


def _extend(line: StyleAndTextTuples, style: str, text: str) -> None:
    if not text:
        return
    if line and line[-1][0] == style:
        line[-1] = (style, line[-1][1] + text)
    else:
        line.append((style, text))


def _wrap_into(lines: list[StyleAndTextTuples], col: int, runs: list[Run], width: int) -> int:
    """Append ``runs`` to the visual lines ``lines`` (the last one is open at cell ``col``),
    breaking at ``width`` cells; returns the new column. Text whose characters all take one
    cell is cut by slicing; other text goes character by character with its real widths."""
    for style, text in runs:
        if narrow(text):
            pos = 0
            while pos < len(text):
                room = width - col
                if room <= 0:
                    lines.append([])
                    col, room = 0, width
                piece = text[pos : pos + room]
                _extend(lines[-1], style, piece)
                col += len(piece)
                pos += len(piece)
            continue
        chars: list[str] = []
        for ch in text:
            cells = max(get_cwidth(ch), 0)
            if col > 0 and col + cells > width:
                _extend(lines[-1], style, "".join(chars))
                chars = []
                lines.append([])
                col = 0
            chars.append(ch)
            col += cells
        _extend(lines[-1], style, "".join(chars))
    return col


def _wrap_fragments(fragments: StyleAndTextTuples, width: int) -> list[StyleAndTextTuples]:
    """Wrap formatted fragments to terminal cell ``width``, respecting wide Unicode chars."""
    if width <= 0:
        return [fragments]
    lines: list[StyleAndTextTuples] = [[]]
    _wrap_into(lines, 0, [(item[0], item[1]) for item in fragments], width)
    return lines


def _parse_lines(text: str) -> list[list[Run]]:
    """``text`` (ending in a newline) as logical lines of styled runs."""
    lines: list[list[Run]] = [[]]
    chars = 0
    for style, chunk in _runs(text.replace("\r\n", "\n").replace("\r", "")):
        for n, raw in enumerate(chunk.split("\n")):
            if n:
                lines.append([])
                chars = 0
            if raw:
                part = _expand_tabs(raw, chars) if "\t" in raw else raw
                lines[-1].append((style, part))
                chars += cell_width(part)
    if not lines[-1]:
        lines.pop()
    return lines


def _expand_tabs(text: str, start: int) -> str:
    """Tabs as spaces up to the next multiple of four cells; ``start`` is how many
    cells the line already holds."""
    out: list[str] = []
    col = start
    for ch in text:
        if ch == "\t":
            pad = _TAB - col % _TAB
            out.append(" " * pad)
            col += pad
        else:
            out.append(ch)
            col += 1 if ch.isascii() else max(get_cwidth(ch), 0)
    return "".join(out)


class _TranscriptControl(UIControl):
    """Scrollable, incrementally wrapped conversation pane.

    A logical line is a list of styled runs. Text that streams into the last line is
    wrapped where the previous piece ended, so a long paragraph costs its new characters
    per frame, not its whole length (a 30,000-character paragraph took 11 s of CPU before).
    """

    def __init__(self) -> None:
        self._lines: list[list[Run]] = [[]]
        self._chars: int = 0  # cells in the open (last) line, for tab stops
        self._wrap_width: int = 0
        self._wrapped_cache: list[list[StyleAndTextTuples]] = []
        self._visual_offsets: list[int] = []
        self._visual_lines: list[StyleAndTextTuples] = [[]]
        self._dirty_from: int = 0
        # The open line as far as it is wrapped: (its index, runs wrapped, column reached).
        self._tail: tuple[int, int, int] | None = None

        # Tool output that Ctrl+O shows or folds: [first line, line count, folded, full].
        self._blocks: list[list[Any]] = []
        self.expanded: bool = False
        self._live: list[Run] = []  # the answer's line still being streamed
        self._live_lines: list[StyleAndTextTuples] = []

        self.follow: bool = True
        self.scroll_top: int = 0
        self.last_width: int = 80
        self.last_height: int = 20
        self.total_visual_lines: int = 1

        # Mouse drag selection: anchor and current point as (visual row, character
        # column) of the content on screen. ``on_copy`` gets the selected plain text.
        self._sel_anchor: tuple[int, int] | None = None
        self._sel_point: tuple[int, int] | None = None
        # The rows as they were when the button went down: text that streams in (and a
        # markdown line that re-formats when its closing mark arrives) must not move
        # under a selection being dragged.
        self._frozen: _RowsView | None = None
        self.on_copy: Callable[[str], None] | None = None

    def _reset_selection(self) -> None:
        self._sel_anchor = self._sel_point = None
        self._frozen = None

    @property
    def max_scroll(self) -> int:
        return max(0, self.total_visual_lines - self.last_height)

    @property
    def lines_below(self) -> int:
        return max(0, self.max_scroll - self.scroll_top)

    @property
    def plain_text(self) -> str:
        """All currently stored logical text (without ANSI escapes), for diagnostics/tests."""
        return "\n".join("".join(text for _, text in line) for line in self._lines)

    def clear(self) -> None:
        self._lines = [[]]
        self._chars = 0
        self._wrapped_cache = []
        self._visual_offsets = []
        self._visual_lines = [[]]
        self._dirty_from = 0
        self._tail = None
        self._blocks = []
        self._live = []
        self.follow = True
        self.scroll_top = 0
        self.total_visual_lines = 1
        self._reset_selection()

    def set_live(self, text: str) -> None:
        """Show ``text`` (one line, may hold colours) after the last line until replaced."""
        self._live = _runs(text.replace("\n", " ")) if text else []

    def append_block(self, folded: str, full: str) -> None:
        """Lines that Ctrl+O switches between ``folded`` and ``full`` (each ends in a
        newline)."""
        if self._lines[-1]:
            self.append("\n")
        start = len(self._lines) - 1
        self.append(full if self.expanded else folded)
        if self._lines[-1]:
            self.append("\n")
        self._blocks.append([start, len(self._lines) - 1 - start, folded, full])

    def toggle_blocks(self) -> bool:
        """Show every block's other form; True when anything on screen changed."""
        self.expanded = not self.expanded
        if not self._blocks:
            return False
        shift = 0
        for block in self._blocks:
            block[0] += shift
            start, count, folded, full = block
            lines = _parse_lines(full if self.expanded else folded)
            self._lines[start : start + count] = lines
            shift += len(lines) - count
            block[1] = len(lines)
        self._dirty_from = min(self._dirty_from, self._blocks[0][0])
        self._tail = None
        self._reset_selection()  # line numbers moved
        return True

    def append(self, text: str) -> None:
        if not text:
            return
        if _CLEAR_SCREEN in text:
            _, _, text = text.rpartition(_CLEAR_SCREEN)
            self.clear()
            if not text:
                return
        text = text.replace("\r\n", "\n").replace("\r", "")
        open_index = len(self._lines) - 1
        for style, chunk in _runs(text):
            for n, raw in enumerate(chunk.split("\n")):
                if n:
                    self._lines.append([])
                    self._chars = 0
                if raw:
                    part = _expand_tabs(raw, self._chars) if "\t" in raw else raw
                    self._lines[-1].append((style, part))
                    self._chars += cell_width(part)
        self._dirty_from = min(self._dirty_from, open_index)
        if len(self._lines) > MAX_LOGICAL_LINES:
            drop = _TRIM_BATCH
            del self._lines[:drop]
            self._wrapped_cache = []
            self._visual_offsets = []
            self._visual_lines = []
            self._dirty_from = 0
            self._tail = None
            for block in self._blocks:
                block[0] -= drop
            self._blocks = [block for block in self._blocks if block[0] >= 0]
            self._reset_selection()  # line numbers moved
            if not self.follow:
                self.scroll_top = max(0, self.scroll_top - drop)

    def _displayed_logical_count(self) -> int:
        if len(self._lines) > 1 and not self._lines[-1]:
            return len(self._lines) - 1
        return len(self._lines)

    def _sync_wrap(self, width: int) -> None:
        width = max(1, width)
        count = self._displayed_logical_count()
        anchor: tuple[int, int] | None = None
        if width != self._wrap_width:
            if self._wrap_width:
                self._reset_selection()  # the rows re-wrap: their numbers mean other text
                if not self.follow and self._visual_offsets:
                    # Scrolled up: keep the text at the top of the view there, not the
                    # row number (which after re-wrapping is somewhere else entirely).
                    index = bisect_right(self._visual_offsets, self.scroll_top) - 1
                    if index >= 0:
                        row = self.scroll_top - self._visual_offsets[index]
                        anchor = (index, row * self._wrap_width // width)
            self._wrap_width = width
            self._wrapped_cache = []
            self._visual_offsets = []
            self._visual_lines = []
            self._dirty_from = 0
            self._tail = None

        dirty = min(self._dirty_from, len(self._wrapped_cache), count)
        tail = self._tail
        if (
            tail is not None
            and dirty == tail[0] == len(self._wrapped_cache) - 1
            and tail[0] < count
            and self._visual_lines
        ):
            # Only the open line grew: wrap its new runs where the last frame stopped.
            index, done, col = tail
            line = self._lines[index]
            before = len(self._visual_lines)
            col = _wrap_into(self._visual_lines, col, line[done:], width)
            self._wrapped_cache[index].extend(self._visual_lines[before:])
            self._tail = (index, len(line), col)
            dirty = index + 1
        elif dirty < len(self._wrapped_cache):
            cut = self._visual_offsets[dirty]
            del self._visual_lines[cut:]
            del self._wrapped_cache[dirty:]
            del self._visual_offsets[dirty:]
        elif dirty == 0:
            self._visual_lines.clear()
            self._wrapped_cache.clear()
            self._visual_offsets.clear()

        for i in range(dirty, count):
            self._visual_offsets.append(len(self._visual_lines))
            wrapped: list[StyleAndTextTuples] = [[]]
            col = _wrap_into(wrapped, 0, self._lines[i], width)
            self._wrapped_cache.append(wrapped)
            self._visual_lines.extend(wrapped)
            self._tail = (i, len(self._lines[i]), col) if i == len(self._lines) - 1 else None

        if not self._visual_lines:
            self._visual_lines = [[]]
        self._dirty_from = count
        self._live_lines = _wrap_fragments(list(self._live), width) if self._live else []
        self.total_visual_lines = len(self._visual_lines) + len(self._live_lines)
        if anchor is not None and anchor[0] < len(self._visual_offsets):
            index, row = anchor
            self.scroll_top = self._visual_offsets[index] + min(
                row, len(self._wrapped_cache[index]) - 1
            )

    def scroll_by(self, delta: int) -> bool:
        max_top = self.max_scroll
        target = max(0, min(max_top, self.scroll_top + delta))
        if delta < 0 and target < max_top:
            self.follow = False
        elif target >= max_top:
            self.follow = True
        changed = target != self.scroll_top
        self.scroll_top = target
        return changed

    def scroll_page_up(self) -> bool:
        step = max(1, self.last_height - 2)
        return self.scroll_by(-step)

    def scroll_page_down(self) -> bool:
        step = max(1, self.last_height - 2)
        return self.scroll_by(step)

    def scroll_to_bottom(self) -> bool:
        max_top = self.max_scroll
        was_following = self.follow
        self.follow = True
        changed = self.scroll_top != max_top or not was_following
        self.scroll_top = max_top
        return changed

    def scroll_to_top(self) -> bool:
        self.follow = False
        changed = self.scroll_top != 0
        self.scroll_top = 0
        return changed

    @override
    def is_focusable(self) -> bool:
        return False

    @override
    def create_content(self, width: int, height: int) -> UIContent:
        self.last_width = max(1, width)
        self.last_height = max(1, height)
        self._sync_wrap(self.last_width)
        max_top = self.max_scroll
        if self.follow:
            self.scroll_top = max_top
        else:
            self.scroll_top = max(0, min(self.scroll_top, max_top))
        visual = self._visual_lines
        live = self._live_lines
        known = len(visual)
        total = known + len(live)
        cursor_y = min(self.scroll_top, total - 1)
        width = self.last_width
        sel = self._normalized_selection()

        def get_line(i: int) -> StyleAndTextTuples:
            line = visual[i] if i < known else live[i - known]
            if sel is not None and sel[0] <= i <= sel[2]:
                return _mark_selection(line, i, sel)
            if line and FILL in line[-1][0]:  # a tinted line runs to the edge
                used = sum(cell_width(fragment[1]) for fragment in line)
                if used < width:
                    return [*line, (line[-1][0], " " * (width - used))]
            return line

        return UIContent(
            get_line=get_line,
            line_count=total,
            cursor_position=Point(x=0, y=cursor_y),
            show_cursor=False,
        )

    # -- mouse drag selection ----------------------------------------------------------

    def _view(self) -> _RowsView:
        """The rows now: the ones frozen at the start of a drag, else the live ones."""
        if self._frozen is not None:
            return self._frozen
        return _RowsView(
            self._visual_lines, self._visual_offsets, self._live_lines, len(self._lines)
        )

    def _freeze(self) -> _RowsView:
        """A copy of the rows that no streaming write changes: every row but the open
        line's is never touched again, so only its rows are copied."""
        rows, offsets = list(self._visual_lines), list(self._visual_offsets)
        start = offsets[-1] if offsets else 0
        rows[start:] = [list(row) for row in rows[start:]]
        live = [list(row) for row in self._live_lines]
        return _RowsView(rows, offsets, live, len(self._lines))

    def _normalized_selection(self) -> tuple[int, int, int, int] | None:
        """The selection as (first row, first character, last row, end), the end being
        the boundary after the last selected character: None when there is none or it
        is empty. The mouse names the character it is over, and a position past the end
        of a row names its last character: a drag that ends on or beyond it takes it."""
        if self._sel_anchor is None or self._sel_point is None:
            return None
        a, b = sorted((self._sel_anchor, self._sel_point))
        if a == b:
            return None
        size = len(self._view().plain(b[0]))
        return a[0], a[1], b[0], size if b[1] >= size - 1 else b[1]

    def selection_text(self) -> str:
        """The selected text, plain: no colours, none of the decoration the chat itself
        drew (marked ``class:deco``), nothing else left out. Visual rows of one wrapped
        logical line join without a newline; separate logical lines with one."""
        sel = self._normalized_selection()
        if sel is None:
            return ""
        r1, c1, r2, c2 = sel
        view = self._view()
        parts: list[str] = []
        prev_logical = -1
        for r in range(r1, min(r2, view.total - 1) + 1):
            logical = view.logical(r)
            if r > r1 and logical != prev_logical:
                parts.append("\n")
            prev_logical = logical
            parts.append(_slice_row(view.row(r), c1 if r == r1 else 0, c2 if r == r2 else 1 << 30))
        return "".join(parts)

    @override
    def mouse_handler(self, mouse_event: MouseEvent) -> NotImplementedOrNone:
        kind = mouse_event.event_type
        if kind == MouseEventType.SCROLL_UP:
            self.scroll_by(-_WHEEL_LINES)
            return None
        if kind == MouseEventType.SCROLL_DOWN:
            self.scroll_by(_WHEEL_LINES)
            return None
        pos = (mouse_event.position.y, mouse_event.position.x)
        if kind == MouseEventType.MOUSE_DOWN:
            if mouse_event.button in (MouseButton.LEFT, MouseButton.UNKNOWN):
                self._frozen = self._freeze()
                self._sel_anchor = self._sel_point = pos
                return None
            return NotImplemented
        if kind == MouseEventType.MOUSE_MOVE:
            if mouse_event.button == MouseButton.LEFT and self._sel_anchor is not None:
                self._sel_point = pos
                return None  # repaint with the highlight
            return NotImplemented
        if kind == MouseEventType.MOUSE_UP:
            if self._sel_anchor is None:
                return NotImplemented
            text = self.selection_text()
            self._frozen = None
            if text and self.on_copy is not None:
                self.on_copy(text)
            return None
        return NotImplemented


class _RowsView(NamedTuple):
    """The visual rows of the conversation, for turning a selection into text."""

    rows: list[StyleAndTextTuples]
    offsets: list[int]  # the first visual row of each logical line
    live: list[StyleAndTextTuples]  # the answer line still streaming, after the rows
    logical_count: int

    @property
    def total(self) -> int:
        return len(self.rows) + len(self.live)

    def row(self, number: int) -> StyleAndTextTuples:
        known = len(self.rows)
        if number < known:
            return self.rows[number]
        return self.live[number - known] if number - known < len(self.live) else []

    def plain(self, number: int) -> str:
        return "".join(fragment[1] for fragment in self.row(number))

    def logical(self, number: int) -> int:
        """Rows of one wrapped logical line share its index; the streaming line counts
        as one logical line."""
        if number < len(self.rows):
            return max(bisect_right(self.offsets, number) - 1, 0)
        return self.logical_count


def _slice_row(row: StyleAndTextTuples, start: int, end: int) -> str:
    """The characters ``start`` .. ``end`` of a row, the decoration among them left out."""
    out: list[str] = []
    pos = 0
    for style, text, *_rest in row:
        s = min(max(start - pos, 0), len(text))
        e = min(max(end - pos, 0), len(text))
        if e > s and DECO not in style:
            out.append(text[s:e])
        pos += len(text)
    return "".join(out)


def _mark_selection(
    line: StyleAndTextTuples, row: int, sel: tuple[int, int, int, int]
) -> StyleAndTextTuples:
    """The fragments of ``line`` with the selected character range styled
    ``class:selected`` — the highlight the drag paints."""
    start = sel[1] if row == sel[0] else 0
    end = sel[3] if row == sel[2] else 1 << 30
    out: StyleAndTextTuples = []
    pos = 0
    for style, text, *_rest in line:
        s = min(max(start - pos, 0), len(text))
        e = min(max(end - pos, 0), len(text))
        _extend(out, style, text[:s])
        if e > s:
            _extend(out, f"{style} class:selected".strip(), text[s:e])
        _extend(out, style, text[e:])
        pos += len(text)
    return out


class _QuietScrollbar(ScrollbarMargin):
    """The scrollbar only when there is something to scroll: a full-height bar beside a
    conversation that fits its window says nothing. Its column stays reserved, so the
    text does not re-wrap when the bar appears."""

    @override
    def create_margin(
        self, window_render_info: WindowRenderInfo, width: int, height: int
    ) -> StyleAndTextTuples:
        if window_render_info.content_height <= window_render_info.window_height:
            return []
        return super().create_margin(window_render_info, width, height)


def _windows_terminal() -> bool:
    return sys.platform == "win32"


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


class _FullScreenTurnView:
    """Lightweight TurnView binding ChatApp turns to the persistent FullScreenTui."""

    def __init__(
        self,
        ui: FullScreenTui,
        status: Callable[[], str],
        interrupt: Callable[[], None],
    ) -> None:
        self._ui = ui
        self._status = status
        self._interrupt = interrupt
        self._ticker: asyncio.Task[None] | None = None

    async def __aenter__(self) -> _FullScreenTurnView:
        self._ui._begin_turn(self._status, self._interrupt)
        self._ticker = asyncio.create_task(self._tick())
        return self

    async def _tick(self) -> None:
        while True:
            await asyncio.sleep(0.12)
            self._ui.invalidate()

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._ticker is not None:
            self._ticker.cancel()
        self._ui._end_turn()
        if self._ticker is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._ticker
            self._ticker = None

    def write(self, text: str) -> None:
        self._ui.write(text)

    @contextlib.asynccontextmanager
    async def pause(self) -> AsyncIterator[None]:
        # Inline question/approval dialogs live inside the same Application.
        yield


_MENUS = {
    # theme: (menu, current item, description, current description)
    "dark": ("bg:#1f2335 fg:#c0caf5", "bold bg:#3d59a1 fg:#ffffff", "fg:#7c829c", "fg:#e0e6ff"),
    "light": ("bg:#eef1f5 fg:#24292f", "bold bg:#2563c9 fg:#ffffff", "fg:#6e7781", "fg:#ffffff"),
    "mid": ("bg:#303030 fg:#d0d0d0", "bold bg:#005f87 fg:#ffffff", "fg:#949494", "fg:#e4e4e4"),
    "ansi": (
        "bg:ansibrightblack fg:ansiwhite",
        "bold bg:ansicyan fg:ansiblack",
        "fg:ansigray",
        "fg:ansiblack",
    ),
}


def _build_style(color: bool, theme: Theme | None = None) -> Style:
    if not color:
        return Style.from_dict(
            {
                "completion-menu.completion.current": "reverse",
                "question-bar": "bold",
                "secret-bar": "bold",
                "selected": "reverse",
                "pick-current": "reverse",
                "pick-bar": "bold",
                "pick-match": "bold",
            }
        )
    theme = theme or Theme("ansi", 4)
    t = theme.pt
    menu, current, meta, meta_current = _MENUS.get(theme.name, _MENUS["mid"])
    subtle = t("subtle").replace("fg:", "bg:") if "#" in t("subtle") else "bg:ansibrightblack"
    return Style.from_dict(
        {
            "prompt": f"bold {t('accent')}",
            "prompt.more": t("muted"),
            "frame": t("subtle"),
            "question-bar": f"bold {t('warning')}",
            "question-prompt": f"bold {t('warning')}",
            "secret-bar": f"bold {t('accent')}",
            "secret-prompt": f"bold {t('accent')}",
            "completion-menu": menu,
            "completion-menu.completion": menu,
            "completion-menu.completion.current": current,
            "completion-menu.meta.completion": f"{menu} {meta}",
            "completion-menu.meta.completion.current": f"{current} {meta_current}",
            "scrollbar.background": "",
            "scrollbar.button": subtle,
            "selected": "reverse",
            "pick-bar": f"bold {t('accent')}",
            "pick-keys": t("muted"),
            "pick-prompt": f"bold {t('accent')}",
            "pick-item": "",
            "pick-current": current,
            "pick-match": "bold underline",
            "pick-mark": t("accent") if "#" not in current else "",
            "pick-dim": t("muted"),
            "pick-note": t("muted"),
        }
    )


class FullScreenTui:
    """Full-screen alternate-screen front end for :class:`karox6.tui.ChatApp`.

    Layout (top to bottom):
    1. Scrollable conversation pane (fills available height).
    2. The activity line: what the running turn does and for how long (blank between
       turns), queued messages and how far the view is scrolled up.
    3. The input box (multiline prompt, inline question/approval or hidden secret) with
       the keys worth knowing set into its lower edge; a floating ``CompletionsMenu`` and
       the ``Ctrl+R`` ``SearchToolbar``.
    4. The status bar (model, project/branch, context fill, cost, turn time, approvals).
    """

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        home: Path | None = None,
        history: Path | History | None = None,
        color: bool = True,
        encoding: str | None = None,
        completer: Completer | None = None,
        complete_in_thread: bool = True,
        input: Input | None = None,
        output: Output | None = None,
        model: str = "",
        provider: str = "",
        approval_mode: str = "ask",
        splash: Splash | None = None,
        theme: Theme | None = None,
        legacy_glyphs: bool = False,
        copy_to_clipboard: Callable[[str], str] | None = None,
    ) -> None:
        self._workspace = (workspace or Path.cwd()).resolve()
        self._home = home
        self._color = color
        self._theme = theme if theme is not None else Theme("ansi", 4 if color else 0)
        self._glyph = _glyphs(encoding or getattr(sys.stdout, "encoding", None), legacy_glyphs)
        self._fallback_model = model
        self._fallback_provider = provider
        self._fallback_approval = approval_mode
        self._fallback_branch = _git_branch(self._workspace)
        self._chat_app: ChatApp | None = None

        self._mode: str = "prompt"  # "prompt" | "running" | "question" | "secret" | "pick"
        self._pick: PickState | None = None  # the list being picked from (mode "pick")
        self._pick_future: asyncio.Future[str | None] | None = None
        self._prompt_text: str = self._glyph["prompt"]
        self._more_text: str = self._glyph["more"]
        self._question_prompt: str = ""
        self._secret_prompt: str = ""
        self._turn_active: bool = False
        self._turn_status_fn: Callable[[], str] | None = None
        self._turn_interrupt_fn: Callable[[], None] | None = None

        self._read_future: asyncio.Future[str] | None = None
        self._question_future: asyncio.Future[str] | None = None
        self._closed = False  # _stop ran: reads after it get EOF, never a dead future
        # Messages sent while a turn runs, each with the pictures attached to its draft.
        self._queued_inputs: deque[tuple[str, list[Path]] | BaseException] = deque()
        self._copy_fn = copy_to_clipboard
        self._pastes = PasteChunks()
        self._large_ok: str | None = None  # the draft whose size the user confirmed
        # Clipboard reads (Ctrl+V, Alt+V) run one after another, each for the draft that
        # was being typed when its key was pressed and at the cursor position then.
        self._draft = 0  # changes when a draft ends: what is still on its way is dropped
        self._paste_tasks: list[asyncio.Future[None]] = []
        self._paste_slot: int | None = None  # where the next clipboard text lands
        self._enter_waiting = False  # an Enter waiting for the clipboard reads to end
        self._cr_at: tuple[int, float] | None = None  # a paste ended in CR: (cursor, time)
        self._notice: tuple[str, float] | None = None  # (text, until) on the activity line
        self._notice_timer: asyncio.TimerHandle | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._copy_tasks: set[asyncio.Task[None]] = set()  # a selection copy in flight

        self._transcript = _TranscriptControl()
        self._transcript.on_copy = self._copy_selection

        resolved_home = home or (history.parent if isinstance(history, Path) else None)
        base_completer = completer or build_completer(workspace=self._workspace, home=resolved_home)
        active_completer: Completer = (
            ThreadedCompleter(base_completer) if complete_in_thread else base_completer
        )
        self._input_buffer = Buffer(
            name="karox_input",
            history=build_history(history),
            completer=active_completer,
            complete_while_typing=True,
            multiline=True,
        )
        self._answer_buffer = Buffer(name="karox_answer", multiline=False)
        self._secret_buffer = Buffer(name="karox_secret", multiline=False)
        self._pick_buffer = Buffer(
            name="karox_pick", multiline=False, on_text_changed=self._pick_filter_changed
        )
        self._search_toolbar = SearchToolbar(ignore_case=True)

        self._transcript_window = Window(
            content=self._transcript,
            wrap_lines=False,
            get_vertical_scroll=lambda _w: self._transcript.scroll_top,
            right_margins=[_QuietScrollbar(display_arrows=False)],
            always_hide_cursor=True,
        )

        def line(text: Callable[[], StyleAndTextTuples]) -> Window:
            return Window(
                content=FormattedTextControl(text, show_cursor=False),
                height=1,
                wrap_lines=False,
                always_hide_cursor=True,
            )

        self._activity_window = line(self._activity_text)
        self._status_window = line(self._status_bar_text)
        self._box_top = line(lambda: self._roles(box_top(self._width(), self._glyph)))
        self._box_bottom = line(self._box_bottom_text)
        left, right = box_sides(self._glyph)
        self._input_window = Window(
            content=BufferControl(
                buffer=self._input_buffer,
                search_buffer_control=self._search_toolbar.control,
                preview_search=True,
            ),
            height=Dimension(min=1, max=6),
            dont_extend_height=True,
            wrap_lines=True,
            get_line_prefix=self._input_line_prefix,
        )
        self._answer_window = Window(
            content=BufferControl(buffer=self._answer_buffer),
            height=1,
            wrap_lines=False,
            get_line_prefix=lambda _ln, _wc: [("class:question-prompt", self._question_prompt)],
        )
        self._secret_window = Window(
            content=BufferControl(
                buffer=self._secret_buffer,
                input_processors=[PasswordProcessor()],
            ),
            height=1,
            wrap_lines=False,
            get_line_prefix=lambda _ln, _wc: [("class:secret-prompt", self._secret_prompt)],
        )
        self._question_banner = line(self._question_banner_text)
        self._pick_window = Window(
            content=BufferControl(buffer=self._pick_buffer),
            height=1,
            wrap_lines=False,
            get_line_prefix=lambda _ln, _wc: [("class:pick-prompt", "filter › ")],
        )
        self._pick_list = Window(
            content=FormattedTextControl(self._pick_lines, show_cursor=False),
            height=lambda: Dimension.exact(self._pick.rows() if self._pick else 1),
            wrap_lines=False,
            always_hide_cursor=True,
        )
        pick_panel = HSplit([line(self._pick_header), self._pick_list])
        self._secret_banner = line(self._secret_banner_text)
        self._attach_window = line(self._attach_text)

        in_prompt = Condition(lambda: self._mode == "prompt")
        in_running = Condition(lambda: self._mode == "running")
        in_input = in_prompt | in_running
        in_question = Condition(lambda: self._mode == "question")
        in_secret = Condition(lambda: self._mode == "secret")
        in_pick = Condition(lambda: self._mode == "pick")
        has_attach = Condition(
            lambda: (
                self._chat_app is not None
                and bool(self._chat_app.pending_images or self._chat_app.carried_images)
            )
        )

        inside = HSplit(
            [
                ConditionalContainer(self._input_window, filter=in_input),
                ConditionalContainer(self._answer_window, filter=in_question),
                ConditionalContainer(self._secret_window, filter=in_secret),
                ConditionalContainer(self._pick_window, filter=in_pick),
                ConditionalContainer(self._attach_window, filter=in_input & has_attach),
            ]
        )
        box = HSplit(
            [
                self._box_top,
                VSplit(
                    [
                        Window(
                            FormattedTextControl([("class:frame", left)]),
                            width=cell_width(left),
                            dont_extend_width=True,
                        ),
                        inside,
                        Window(
                            FormattedTextControl([("class:frame", right)]),
                            width=cell_width(right),
                            dont_extend_width=True,
                        ),
                    ]
                ),
                self._box_bottom,
            ]
        )
        body = HSplit(
            [
                self._transcript_window,
                self._activity_window,
                ConditionalContainer(self._search_toolbar, filter=is_searching),
                ConditionalContainer(self._question_banner, filter=in_question),
                ConditionalContainer(self._secret_banner, filter=in_secret),
                ConditionalContainer(pick_panel, filter=in_pick),
                box,
                self._status_window,
            ]
        )
        root = FloatContainer(
            content=body,
            floats=[
                Float(
                    xcursor=True,
                    ycursor=True,
                    attach_to_window=self._input_window,
                    content=CompletionsMenu(
                        max_height=8,
                        scroll_offset=1,
                        display_arrows=True,
                        extra_filter=in_prompt & ~is_searching,
                    ),
                )
            ],
        )

        depth = {0: ColorDepth.DEPTH_1_BIT, 4: ColorDepth.DEPTH_4_BIT, 8: ColorDepth.DEPTH_8_BIT}
        self._app: Application[None] = Application(
            layout=Layout(root, focused_element=self._input_window),
            style=_build_style(color, self._theme),
            color_depth=depth.get(self._theme.depth, ColorDepth.DEPTH_24_BIT)
            if color
            else ColorDepth.DEPTH_1_BIT,
            key_bindings=self._build_key_bindings(),
            full_screen=True,
            mouse_support=True,
            min_redraw_interval=0.02,
            terminal_size_polling_interval=0.2,
            input=input,
            output=output,
        )
        self._app.input = coalescing_input(self._app.input)
        self._app.ttimeoutlen = 0.12
        if splash is not None:
            self._adopt(splash)

        self._app_task: asyncio.Task[None] | None = None
        self._active: bool = False

    def _adopt(self, splash: Splash) -> None:
        """The start-up frame (karox6.splash) has already put the terminal into the
        alternate screen and painted what the first frame shows. Entering again would
        make some terminals save the cursor a second time (and put it back in the wrong
        place on exit), so the app's own "enter" only moves the cursor home; the first
        frame then redraws the same cells."""
        output = self._app.output
        enter = output.enter_alternate_screen

        def enter_once() -> None:
            if splash.take_over():
                output.cursor_goto(0, 0)
            else:
                enter()

        output.enter_alternate_screen = enter_once  # type: ignore[method-assign]

    # -- public properties & wiring ----------------------------------------------------

    @property
    def app(self) -> Application[None]:
        return self._app

    @property
    def transcript(self) -> _TranscriptControl:
        return self._transcript

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def input_buffer(self) -> Buffer:
        return self._input_buffer

    @property
    def answer_buffer(self) -> Buffer:
        return self._answer_buffer

    @property
    def secret_buffer(self) -> Buffer:
        return self._secret_buffer

    def attach_app(self, chat_app: ChatApp) -> None:
        self._chat_app = chat_app
        chat_app.paint.mark_frames = True  # the conversation pane copies without the frames
        self.invalidate()

    def columns(self) -> int:
        """Cells a line of the conversation can take (the scrollbar column left out)."""
        return max(self._app.output.get_size().columns - 1, 20)

    def prefill(self, text: str) -> None:
        """Put ``text`` into the input line, cursor at its end (an undone message comes
        back for editing); text the user has already typed is not replaced."""
        if not self._input_buffer.text:
            self._input_buffer.set_document(Document(text, len(text)), bypass_readonly=True)
            self.invalidate()

    def copy_to_clipboard(self, text: str) -> str:
        """How the selection and ``/copy`` copy: the injected writer, or the real
        system clipboard. This runs in a worker thread; an OSC 52 sequence is written
        to the terminal by the event loop, not from here."""
        if self._copy_fn is not None:
            return self._copy_fn(text)
        from karox6.clipboard import copy_text

        return copy_text(text, emit_osc52=self._emit_osc52)

    def _emit_osc52(self, seq: str) -> None:
        loop = self._loop
        if loop is None or loop is _running_loop():
            self._write_osc52(seq)
        else:
            with contextlib.suppress(RuntimeError):  # the loop closed: the app is gone
                loop.call_soon_threadsafe(self._write_osc52, seq)

    def _write_osc52(self, seq: str) -> None:
        self._app.output.write_raw(seq)
        self._app.output.flush()

    def invalidate(self) -> None:
        if self._app.is_running and not self._app.is_done:
            self._app.invalidate()

    def write(self, text: str) -> None:
        self._transcript.append(text)
        self.invalidate()

    def turn_display(
        self,
        write: Callable[[str], None],
        status: Callable[[], str],
        interrupt: Callable[[], None],
    ) -> _FullScreenTurnView:
        del write
        return _FullScreenTurnView(self, status, interrupt)

    # -- lifecycle & guaranteed terminal restore ---------------------------------------

    async def __aenter__(self) -> FullScreenTui:
        self._active = True
        self._loop = asyncio.get_running_loop()
        atexit.register(self._restore_terminal)
        try:
            self._app_task = asyncio.create_task(self._app.run_async(handle_sigint=False))
            self._app_task.add_done_callback(self._on_app_done)
            # Let the Application enter the alternate screen and draw the first frame.
            await asyncio.sleep(0)
            if self._app_task.done():
                await self._app_task
        except BaseException:
            await self._stop()
            raise
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._stop()

    async def _stop(self) -> None:
        self._closed = True  # a read arriving after this point gets EOF, not a dead future
        self._enter_waiting = False
        if self._notice_timer is not None:
            self._notice_timer.cancel()
            self._notice_timer = None
        for pending in [*self._paste_tasks, *self._copy_tasks]:
            pending.cancel()  # the clipboard thread (a daemon) is left to finish alone
        self._fail_pending(EOFError())
        try:
            task = self._app_task
            self._app_task = None
            if self._app.is_running and not self._app.is_done:
                self._app.exit()
            elif task is not None and not task.done():
                task.cancel()
            if task is not None:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=2.0)
                except (asyncio.CancelledError, TimeoutError, EOFError, Exception):
                    if not task.done():
                        task.cancel()
                        with contextlib.suppress(BaseException):
                            await task
        finally:
            self._restore_terminal()

    def _restore_terminal(self) -> None:
        if not self._active:
            return
        self._active = False
        with contextlib.suppress(Exception):
            atexit.unregister(self._restore_terminal)
        with contextlib.suppress(Exception):
            self._app.renderer.reset(leave_alternate_screen=True)

    def _on_app_done(self, task: asyncio.Task[None]) -> None:
        exc: BaseException = EOFError()
        if not task.cancelled():
            err = task.exception()
            if err is not None:
                exc = err
        self._fail_pending(exc)

    def _fail_pending(self, exc: BaseException) -> None:
        for fut in (self._read_future, self._question_future, self._pick_future):
            if fut is not None and not fut.done():
                fut.set_exception(exc)

    # -- ChatApp I/O callables ---------------------------------------------------------

    async def read_line(self, prompt: str) -> str:
        if self._closed:
            raise EOFError
        self._prompt_text = prompt
        self._set_mode("prompt")
        if self._queued_inputs:
            item = self._queued_inputs.popleft()
            if isinstance(item, BaseException):
                raise item
            text, images = item
            if self._chat_app is not None:
                self._chat_app.images_for_next(images)
            self._echo_user_message(text)
            return text
        if self._app_task is not None and self._app_task.done():
            raise EOFError
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._read_future = fut
        try:
            return await fut
        finally:
            if self._read_future is fut:
                self._read_future = None

    async def read_answer(self, prompt: str) -> str:
        if self._closed or (self._app_task is not None and self._app_task.done()):
            raise EOFError
        self._question_prompt = prompt.lstrip() or "> "
        self._answer_buffer.reset()
        self._transcript.scroll_to_bottom()
        self._set_mode("question")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._question_future = fut
        try:
            return await fut
        finally:
            if self._question_future is fut:
                self._question_future = None
            self._set_mode("running" if self._turn_active else "prompt")

    async def read_secret(self, prompt: str) -> str:
        if self._closed or (self._app_task is not None and self._app_task.done()):
            raise EOFError
        self._secret_prompt = prompt.lstrip() or "Secret: "
        self._secret_buffer.reset()
        self._transcript.scroll_to_bottom()
        self._set_mode("secret")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._question_future = fut
        try:
            return await fut
        finally:
            if self._question_future is fut:
                self._question_future = None
            self._set_mode("running" if self._turn_active else "prompt")

    async def pick(
        self,
        title: str,
        items: list[str],
        current: str | None = None,
        *,
        allow_typed: bool = True,
        notes: dict[str, str] | None = None,
    ) -> str | None:
        """A choice from ``items`` (arrows, typing filters, Enter); None on Esc. With
        ``allow_typed``, Enter on a filter that matches nothing returns the typed text."""
        if self._closed or (self._app_task is not None and self._app_task.done()):
            raise EOFError
        self._pick = PickState(title, list(items), current, allow_typed, notes=dict(notes or {}))
        self._pick_buffer.reset()
        self._transcript.scroll_to_bottom()
        self._set_mode("pick")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str | None] = loop.create_future()
        self._pick_future = fut
        try:
            return await fut
        finally:
            if self._pick_future is fut:
                self._pick_future = None
            self._pick = None
            self._set_mode("running" if self._turn_active else "prompt")

    def _pick_filter_changed(self, buffer: Buffer) -> None:
        if self._pick is not None:
            self._pick.set_query(buffer.text)
            self.invalidate()

    def _pick_header(self) -> StyleAndTextTuples:
        if self._pick is None:
            return []
        return cast(StyleAndTextTuples, self._pick.header(self._glyph["dot"]))

    def _pick_lines(self) -> StyleAndTextTuples:
        if self._pick is None:
            return []
        pointer = self._glyph["prompt"].strip() or ">"
        mark = self._glyph.get("bullet", "*")
        return cast(StyleAndTextTuples, self._pick.lines(pointer, mark))

    def _pick_done(self, value: str | None) -> None:
        if self._pick is not None and value is not None:
            self.write(f"  {_plain_text(self._pick.title)}: {_plain_text(value)}\n")
        if self._pick_future is not None and not self._pick_future.done():
            self._pick_future.set_result(value)

    # -- turn & mode transitions -------------------------------------------------------

    def _begin_turn(self, status: Callable[[], str], interrupt: Callable[[], None]) -> None:
        self._turn_active = True
        self._turn_status_fn = status
        self._turn_interrupt_fn = interrupt
        if self._mode not in ("question", "secret", "pick"):
            self._set_mode("running")
        else:
            self.invalidate()

    def _end_turn(self) -> None:
        self._turn_active = False
        self._turn_status_fn = None
        self._turn_interrupt_fn = None
        if self._mode == "running":
            self._set_mode("prompt")
        else:
            self.invalidate()

    def _set_mode(self, mode: str) -> None:
        self._mode = mode
        target = {
            "prompt": self._input_window,
            "question": self._answer_window,
            "secret": self._secret_window,
            "pick": self._pick_window,
            "running": self._input_window,
        }[mode]
        with contextlib.suppress(ValueError):
            self._app.layout.focus(target)
        self.invalidate()

    # -- rendering helpers -------------------------------------------------------------

    def _input_line_prefix(self, line_number: int, wrap_count: int) -> StyleAndTextTuples:
        if line_number == 0 and wrap_count == 0:
            return [("class:prompt", self._prompt_text)]
        return [("class:prompt.more", self._more_text)]

    def _echo_user_message(self, text: str) -> None:
        if not text:
            return
        lines = text.split("\n")
        prefix = self._prompt_text
        more = self._more_text
        paint = self._chat_app.paint if self._chat_app is not None else None
        if self._color and paint is not None:
            # On a band of its own (dark and light themes): your messages are easy to find
            # among the answers and tool lines when scrolling back. "Erase to the end of
            # the line" paints the rest of the row in the band's colour.
            band = "+user_bg" if paint.theme.sgr("user_bg") else ""
            fill = "\x1b[K" if band else ""
            rendered = [
                paint(f"accent+bold{band}", prefix)
                + paint(f"user{band}", _plain_text(lines[0]) + fill)
            ]
            rendered.extend(
                paint(f"muted{band}", more) + paint(f"user{band}", _plain_text(line) + fill)
                for line in lines[1:]
            )
        else:
            rendered = [f"{prefix}{_plain_text(lines[0])}"]
            rendered.extend(f"{more}{_plain_text(line)}" for line in lines[1:])
        self._transcript.scroll_to_bottom()
        # A blank line before and after: the agent's answer starts apart from the question.
        self.write("\n" + "\n".join(rendered) + "\n\n")

    def write_block(self, folded: str, full: str) -> None:
        """Lines that Ctrl+O switches between their short and their full form."""
        self._transcript.append_block(folded, full)
        self.invalidate()

    def set_live(self, text: str) -> None:
        """The line of the answer still being streamed (replaced as it grows)."""
        self._transcript.set_live(text)
        self.invalidate()

    def _width(self) -> int:
        return max(self._app.output.get_size().columns, 1)

    def _roles(self, fragments: list[tuple[str, str]]) -> StyleAndTextTuples:
        """(colour roles, text) pieces as prompt_toolkit fragments of this theme."""
        if not self._color:
            return [("", text) for _, text in fragments]
        pt = self._theme.pt
        return [
            (" ".join(pt(role) for role in roles.split("+") if role), text)
            for roles, text in fragments
        ]

    def _snapshot(self) -> StatusSnapshot:
        if self._chat_app is not None:
            return self._chat_app.status_snapshot()
        return StatusSnapshot(
            model=self._fallback_model or "setup",
            provider=self._fallback_provider,
            workspace_name=self._workspace.name or str(self._workspace),
            branch=self._fallback_branch,
            approval_mode=self._fallback_approval,
            running=self._turn_active,
            activity=self._turn_status_fn() if self._turn_status_fn else "",
            total_tokens=0,
            context_used=0,
            context_window=0,
            context_pct=0,
            session_id=None,
        )

    def _status_bar_text(self) -> StyleAndTextTuples:
        return self._roles(status_bar_fragments(self._snapshot(), self._glyph, self._width()))

    def _activity_text(self) -> StyleAndTextTuples:
        """What the turn does (spinner, phase, time, how to stop it) on the left; queued
        messages and how far the view is scrolled up on the right."""
        g = self._glyph
        dot = g["dot"]
        left: list[tuple[str, str]] = []
        if self._turn_active:
            if self._chat_app is not None:
                phase, elapsed = self._chat_app.activity()
            else:
                phase, elapsed = (self._turn_status_fn() if self._turn_status_fn else ""), 0.0
            spin = g.get("spin", "|/-\\")
            spinner = spin[int(elapsed * 10) % len(spin)]
            left = [("", " "), ("accent", spinner), ("", " ")]
            tail = [("muted", f"  {elapsed:.1f}s{dot}"), ("muted+bold", "esc"), ("muted", " stop")]
            room = self._width() - 6 - sum(cell_width(t) for _, t in tail) - 24
            left += [("", clip_cells(_plain_text(phase), max(room, 10))), *tail]
        right: list[tuple[str, str]] = []
        queued = sum(1 for item in self._queued_inputs if isinstance(item, tuple))
        if queued and self._turn_active:
            right += [("warning", f"{queued} queued"), ("muted", " (esc clears)")]
        below = 0 if self._transcript.follow else self._transcript.lines_below
        if below > 0:
            if right:
                right.append(("muted", dot))
            right += [("warning", f"{g.get('up', '^')} {below} lines below"), ("muted", " (end)")]
        if self._notice is not None and time.monotonic() < self._notice[1]:
            if right:
                right.append(("muted", dot))
            right.append(("success", self._notice[0]))
        if right:
            used = sum(cell_width(t) for _, t in left) + sum(cell_width(t) for _, t in right)
            left.append(("", " " * max(self._width() - used - 1, 1)))
            left += right
        return self._roles(left)

    def _box_bottom_text(self) -> StyleAndTextTuples:
        return self._roles(box_bottom(self._width(), self._glyph, key_hints(self._mode)))

    def _question_banner_text(self) -> StyleAndTextTuples:
        dot = self._glyph["dot"]
        warn = self._glyph.get("warn", "!")
        if "run it?" in self._question_prompt.lower():
            return [
                (
                    "class:question-bar",
                    f" {warn} Approval required: y run{dot}n deny{dot}a always{dot}esc stop turn",
                )
            ]
        return [("class:question-bar", f" {warn} Input required: enter confirm{dot}esc cancel")]

    def _secret_banner_text(self) -> StyleAndTextTuples:
        dot = self._glyph["dot"]
        return [("class:secret-bar", f" Hidden input (not echoed): enter submit{dot}esc cancel")]

    def _attach_text(self) -> StyleAndTextTuples:
        """The pictures waiting to go with the next message, inside the input box."""
        from karox6.chat_features import picture_chip

        chat = self._chat_app
        pend = [*chat.carried_images, *chat.pending_images] if chat is not None else []
        chips = "  ".join(picture_chip(path, i + 1) for i, path in enumerate(pend))
        return self._roles([("muted", f" {chips}")])

    def _set_notice(self, text: str, seconds: float = 3.0) -> None:
        """A short message on the right of the activity line ("copied 42 chars")."""
        if self._closed:
            return
        self._notice = (text, time.monotonic() + seconds)
        if self._notice_timer is not None:
            self._notice_timer.cancel()
        with contextlib.suppress(RuntimeError):  # a loop might not be running in a test
            self._notice_timer = asyncio.get_running_loop().call_later(
                seconds + 0.05, self.invalidate
            )
        self.invalidate()

    def _copy_selection(self, text: str) -> None:
        """Drag released with text under it: put it on the clipboard off the event
        loop (a Windows clipboard call can block on another app's window)."""
        from karox6.clipboard import in_daemon_thread

        async def go() -> None:
            try:
                how = await in_daemon_thread(self.copy_to_clipboard, text)
            except Exception as exc:
                self._set_notice(f"copy failed: {exc}", 4.0)
            else:
                via = " via OSC 52" if how == "osc52" else ""
                self._set_notice(f"copied {len(text)} chars{via}")

        task = asyncio.ensure_future(go())
        self._copy_tasks.add(task)
        task.add_done_callback(self._copy_tasks.discard)

    # -- the draft and what is pasted into it -------------------------------------------

    def _end_draft(self) -> None:
        """The draft is over (sent, or cancelled with Ctrl+C): the pastes behind its
        placeholders go, and a clipboard read still on its way is dropped, so that
        neither lands in the next draft."""
        self._draft += 1
        self._pastes.clear()
        self._large_ok = None
        self._cr_at = None
        self._enter_waiting = False
        self._paste_slot = None
        for task in self._paste_tasks:
            task.cancel()
        self._paste_tasks.clear()

    def _queue_clipboard(self, job: Callable[[int], Coroutine[Any, Any, None]]) -> None:
        """Run ``job`` (it gets the draft it was asked for) after the clipboard reads
        asked before it: two quick Ctrl+V both land, in order."""
        if self._chat_app is None or self._closed:
            return
        if not self._paste_tasks:
            self._paste_slot = self._input_buffer.cursor_position
        previous = self._paste_tasks[-1] if self._paste_tasks else None
        draft = self._draft

        async def run() -> None:
            if previous is not None:
                await asyncio.wait([previous])
            if draft == self._draft:
                await job(draft)

        task = asyncio.ensure_future(run())
        self._paste_tasks.append(task)
        task.add_done_callback(self._paste_done)

    def _paste_done(self, task: asyncio.Future[None]) -> None:
        if task in self._paste_tasks:
            self._paste_tasks.remove(task)
        if not task.cancelled() and task.exception() is not None:
            self.write(f"  cannot read the clipboard: {task.exception()}\n")
        if not self._paste_tasks:
            self._paste_slot = None
            if self._enter_waiting and not self._closed:
                self._enter_waiting = False
                self._submit(None)

    def _paste_from_clipboard(self) -> None:
        """Ctrl+V, or a bracketed paste that arrived empty: text goes into the input
        like any paste; a picture attaches to the next message. The Windows Terminal
        sends nothing for an image-only clipboard, so the empty case looks there."""
        self._queue_clipboard(self._clipboard_text_or_image)

    def _attach_clipboard_image(self) -> None:
        async def job(_draft: int) -> None:
            await self._chat_app.paste_image()  # type: ignore[union-attr]

        self._queue_clipboard(job)

    async def _clipboard_text_or_image(self, draft: int) -> None:
        from karox6.clipboard import grab_text, in_daemon_thread

        text = await in_daemon_thread(grab_text)
        if draft != self._draft:
            return
        if text:
            self._insert_clipboard_text(text.replace("\r\n", "\n").replace("\r", "\n"))
            return
        chat = self._chat_app
        assert chat is not None
        await chat.paste_image()

    def _insert_clipboard_text(self, text: str) -> None:
        """Where the Ctrl+V was pressed, whatever was typed after it: the text lands
        there, behind earlier pastes of the same chain."""
        buf = self._input_buffer
        shown = self._pastes.offer(text)
        value, cursor = buf.text, buf.cursor_position
        at = min(self._paste_slot if self._paste_slot is not None else cursor, len(value))
        if at == cursor:
            buf.insert_text(shown)
        else:
            buf.save_to_undo_stack()
            after = cursor + len(shown) if cursor >= at else cursor
            buf.set_document(Document(value[:at] + shown + value[at:], after), bypass_readonly=True)
        self._paste_slot = at + len(shown)
        self.invalidate()

    def _insert_paste(self, raw: str) -> None:
        """A bracketed paste, at once and at the cursor, so what is typed around it
        stays in order. A CR LF pair cut between two pastes is still one line end."""
        buf = self._input_buffer
        now = time.monotonic()
        if raw.startswith("\n") and self._cr_at is not None:
            at, when = self._cr_at
            if at == buf.cursor_position and now - when < 0.5:
                raw = raw[1:]
        if raw:
            buf.insert_text(self._pastes.offer(raw.replace("\r\n", "\n").replace("\r", "\n")))
            self.invalidate()
        self._cr_at = (buf.cursor_position, now) if raw.endswith("\r") else None

    # -- key bindings ------------------------------------------------------------------

    def _build_key_bindings(self) -> KeyBindings:
        keys = KeyBindings()

        in_prompt = Condition(lambda: self._mode == "prompt")
        in_running = Condition(lambda: self._mode == "running")
        in_input = in_prompt | in_running
        in_question = Condition(lambda: self._mode == "question")
        in_secret = Condition(lambda: self._mode == "secret")
        in_modal = in_question | in_secret

        def _end_should_scroll() -> bool:
            if self._transcript.follow:
                return False
            if self._mode == "running" and not self._input_buffer.text:
                return True
            buf = self._app.current_buffer
            return not buf.text or buf.document.is_cursor_at_the_end_of_line

        # -- conversation scrolling (available in all modes) --

        @keys.add("pageup", eager=True)
        def _page_up(event: KeyPressEvent) -> None:
            if self._transcript.scroll_page_up():
                event.app.invalidate()

        @keys.add("pagedown", eager=True)
        def _page_down(event: KeyPressEvent) -> None:
            if self._transcript.scroll_page_down():
                event.app.invalidate()

        @keys.add("c-end", eager=True)
        def _ctrl_end(event: KeyPressEvent) -> None:
            if self._transcript.scroll_to_bottom():
                event.app.invalidate()

        @keys.add("c-home", eager=True)
        def _ctrl_home(event: KeyPressEvent) -> None:
            if self._transcript.scroll_to_top():
                event.app.invalidate()

        @keys.add("c-o", eager=True)
        def _toggle_output(event: KeyPressEvent) -> None:
            self._transcript.toggle_blocks()
            event.app.invalidate()

        @keys.add("end", filter=Condition(_end_should_scroll) & ~is_searching)
        def _end_jump_bottom(event: KeyPressEvent) -> None:
            if self._transcript.scroll_to_bottom():
                event.app.invalidate()

        # -- running turn interrupt --

        @keys.add("c-c", eager=True, filter=in_running)
        @keys.add("<sigint>", eager=True, filter=in_running)
        def _stop_turn(event: KeyPressEvent) -> None:
            self._queued_inputs.clear()
            if self._turn_interrupt_fn is not None:
                self._turn_interrupt_fn()
            event.app.invalidate()

        @keys.add("escape", eager=True, filter=in_running)
        def _escape_running(event: KeyPressEvent) -> None:
            # Alt+Enter and Alt+V arrive as Escape plus the key in one read. Escape is
            # eager (a stop must not wait for a possible second key), so the pair is
            # recognised here: a two-key binding would never see its second key.
            pending = event.key_processor.input_queue
            if pending and not is_searching():
                nxt = pending[0].key
                if nxt in (Keys.ControlM, Keys.ControlJ):
                    pending.popleft()
                    self._input_buffer.insert_text("\n")
                    return
                if nxt == "v":
                    pending.popleft()
                    self._attach_clipboard_image()
                    return
            _stop_turn(event)

        # -- inline question & secret dialogs --

        @keys.add("enter", filter=in_question)
        def _submit_question(event: KeyPressEvent) -> None:
            del event
            text = self._answer_buffer.text
            self._answer_buffer.reset()
            self._transcript.scroll_to_bottom()
            self.write(f"  {_plain_text(self._question_prompt)}{_plain_text(text)}\n")
            if self._question_future is not None and not self._question_future.done():
                self._question_future.set_result(text)

        @keys.add("enter", filter=in_secret)
        def _submit_secret(event: KeyPressEvent) -> None:
            del event
            text = self._secret_buffer.text
            self._secret_buffer.reset()
            if self._question_future is not None and not self._question_future.done():
                self._question_future.set_result(text)

        @keys.add("escape", eager=True, filter=in_modal)
        @keys.add("c-c", eager=True, filter=in_modal)
        @keys.add("<sigint>", eager=True, filter=in_modal)
        def _cancel_modal(event: KeyPressEvent) -> None:
            del event
            self._answer_buffer.reset()
            self._secret_buffer.reset()
            if self._question_future is not None and not self._question_future.done():
                self._question_future.set_exception(KeyboardInterrupt())

        @keys.add(
            "c-d",
            filter=in_modal & Condition(lambda: not self._app.current_buffer.text),
        )
        def _eof_modal(event: KeyPressEvent) -> None:
            del event
            if self._question_future is not None and not self._question_future.done():
                self._question_future.set_exception(EOFError())

        # -- the list picker --

        in_pick = Condition(lambda: self._mode == "pick")

        def _move(delta: int) -> Callable[[KeyPressEvent], None]:
            def handler(event: KeyPressEvent) -> None:
                del event
                if self._pick is not None:
                    self._pick.move(delta)
                    self.invalidate()

            return handler

        # eager, like the transcript's PageUp/PageDown: while picking, these move the list.
        for name, delta in (
            ("up", -1),
            ("c-p", -1),
            ("s-tab", -1),
            ("down", 1),
            ("c-n", 1),
            ("tab", 1),
            ("pageup", -PICK_ROWS),
            ("pagedown", PICK_ROWS),
        ):
            keys.add(name, eager=True, filter=in_pick)(_move(delta))

        @keys.add("enter", filter=in_pick)
        def _pick_enter(event: KeyPressEvent) -> None:
            del event
            if self._pick is not None:
                choice = self._pick.chosen()
                if choice is not None:
                    self._pick_done(choice)

        @keys.add("escape", eager=True, filter=in_pick)
        @keys.add("c-c", eager=True, filter=in_pick)
        @keys.add("<sigint>", eager=True, filter=in_pick)
        def _pick_cancel(event: KeyPressEvent) -> None:
            del event
            self._pick_done(None)

        # -- main prompt editing, history, completions & exit --

        @keys.add("c-c", eager=True, filter=in_prompt)
        @keys.add("<sigint>", eager=True, filter=in_prompt)
        def _interrupt_prompt(event: KeyPressEvent) -> None:
            if is_searching():
                self._search_toolbar.control.buffer.reset()
                event.app.layout.focus(self._input_window)
                return
            buf = self._input_buffer
            empty = not buf.text
            buf.cancel_completion()
            buf.reset()
            self._end_draft()
            if self._chat_app is not None:
                self._chat_app.pending_images.clear()  # their chips sat in the cleared box
            exc = _PromptInterrupt(empty=empty)
            if self._read_future is not None and not self._read_future.done():
                self._read_future.set_exception(exc)
            else:
                self._queued_inputs.append(exc)

        @keys.add(
            "c-d",
            filter=in_prompt & ~is_searching & Condition(lambda: not self._input_buffer.text),
        )
        def _eof_prompt(event: KeyPressEvent) -> None:
            del event
            exc = EOFError()
            if self._read_future is not None and not self._read_future.done():
                self._read_future.set_exception(exc)
            else:
                self._queued_inputs.append(exc)

        @keys.add("escape", filter=in_prompt & has_completions)
        def _cancel_completion(event: KeyPressEvent) -> None:
            del event
            self._input_buffer.cancel_completion()

        @keys.add("escape", "enter", filter=in_prompt & ~is_searching)  # running: see above
        def _alt_enter(event: KeyPressEvent) -> None:
            del event
            self._input_buffer.insert_text("\n")

        @keys.add("escape", "v", filter=in_prompt & ~is_searching)
        def _alt_v(event: KeyPressEvent) -> None:
            del event
            self._attach_clipboard_image()  # reading the clipboard must not block typing

        @keys.add(Keys.BracketedPaste, filter=in_input & ~is_searching)
        def _bracketed_paste(event: KeyPressEvent) -> None:
            if not event.data:
                # The Windows Terminal sends an empty bracketed paste when the clipboard
                # holds a picture only: Ctrl+V then means "attach it". Another terminal's
                # empty paste is just empty (and must not read a clipboard behind its
                # back, say the one of a remote session's display).
                if _windows_terminal():
                    self._paste_from_clipboard()
                return
            self._insert_paste(event.data)

        @keys.add("c-v", filter=in_input & ~is_searching)
        def _ctrl_v(event: KeyPressEvent) -> None:
            del event
            self._paste_from_clipboard()

        @keys.add("c-j", filter=in_input & ~is_searching)
        def _ctrl_j(event: KeyPressEvent) -> None:
            del event
            self._input_buffer.insert_text("\n")

        @keys.add("tab", filter=in_prompt & ~is_searching)
        def _tab(event: KeyPressEvent) -> None:
            del event
            buf = self._input_buffer
            if buf.complete_state:
                buf.complete_next()
            else:
                buf.start_completion(select_first=True)

        @keys.add("s-tab", filter=in_prompt & ~is_searching)
        def _shift_tab(event: KeyPressEvent) -> None:
            del event
            buf = self._input_buffer
            if buf.complete_state:
                buf.complete_previous()

        @keys.add("enter", filter=in_input & ~is_searching)
        def _enter(event: KeyPressEvent) -> None:
            if self._paste_tasks:
                # A clipboard read is still running: the message goes out once its
                # text (or picture) is in, not without it.
                self._enter_waiting = True
                return
            self._submit(event)

        return keys

    def _submit(self, event: KeyPressEvent | None) -> None:
        """Enter: a line end inside a paste, a completion, or the message itself. The
        ``event`` is None when the Enter was waiting for a clipboard read."""
        buf = self._input_buffer
        doc = buf.document
        if doc.text_before_cursor.endswith("\\") and not doc.text_after_cursor:
            buf.delete_before_cursor(1)
            buf.insert_text("\n")
            return
        if event is not None and pasted_line_end(event):
            buf.insert_text("\n")
            return
        state = buf.complete_state
        if state is not None and state.current_completion is not None:
            buf.apply_completion(state.current_completion)
            return
        shown = buf.text
        text = self._pastes.expand(shown)  # placeholders become their payloads
        size = len(text.encode("utf-8"))
        if size > WARN_BYTES and shown != self._large_ok:
            self._large_ok = shown
            self.write(
                f"  ! this message is {size / (1024 * 1024):.1f} MB — "
                "a model sees every byte. Enter again sends it.\n"
            )
            return
        if text != shown:
            buf.set_document(Document(text, len(text)), bypass_readonly=True)
        if text.strip():
            buf.append_to_history()
        buf.cancel_completion()
        buf.reset()
        self._end_draft()
        # The draft's pictures go with this line, not with whichever is read next (a
        # second Enter can come before the chat loop has taken the first line).
        images: list[Path] = []
        if self._chat_app is not None and (self._read_future is not None or text.strip()):
            images = list(self._chat_app.pending_images)
            self._chat_app.pending_images.clear()
        if self._read_future is not None and not self._read_future.done():
            self._echo_user_message(shown)
            if self._chat_app is not None:
                self._chat_app.images_for_next(images)
            self._read_future.set_result(text)
        elif text.strip():
            self._queued_inputs.append((text, images))
            self.invalidate()  # the status bar counts what is waiting
