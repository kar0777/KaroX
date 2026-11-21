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
from collections import deque
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, override

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
from prompt_toolkit.layout import Dimension, Float, FloatContainer, HSplit, Layout, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl, UIContent, UIControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.layout.processors import PasswordProcessor
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
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
from karox6.tui_text import (
    StatusSnapshot,
    _git_branch,
    _glyphs,
    _plain_text,
    narrow,
    status_bar_fragments,
)

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


def _runs(text: str) -> list[Run]:
    """``text`` (plain, or with SGR colour codes) as styled runs. The codes the chat itself
    writes are read here directly (a run per colour change, not a fragment per character);
    anything richer goes through prompt_toolkit's ANSI parser."""
    if "\x1b" not in text:
        return [("", text)] if text else []
    out: list[Run] = []
    color = ""
    flags: list[str] = []
    pos = 0
    for match in _SGR.finditer(text):
        if match.start() > pos:
            out.append((" ".join([color, *flags]).strip(), text[pos : match.start()]))
        pos = match.end()
        for raw in (match.group(1) or "0").split(";"):
            code = int(raw or 0)
            if code == 0:
                color, flags = "", []
            elif code in _FG:
                color = _FG[code]
            elif code == 39:
                color = ""
            elif code in _FLAGS:
                if _FLAGS[code] not in flags:
                    flags.append(_FLAGS[code])
            elif code in _FLAG_OFF:
                flags = [f for f in flags if f not in _FLAG_OFF[code]]
            else:  # 256 colours, true colour, backgrounds: the full parser knows them
                return _merge(list(ANSI(text).__pt_formatted_text__()))
    if "\x1b" in text[pos:]:  # some other escape sequence
        return _merge(list(ANSI(text).__pt_formatted_text__()))
    if pos < len(text):
        out.append((" ".join([color, *flags]).strip(), text[pos:]))
    return out


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


def _expand_tabs(text: str, start: int) -> str:
    """Tabs as spaces up to the next multiple of four characters; ``start`` is how many
    characters the line already holds."""
    out: list[str] = []
    col = start
    for ch in text:
        if ch == "\t":
            pad = _TAB - col % _TAB
            out.append(" " * pad)
            col += pad
        else:
            out.append(ch)
            col += 1
    return "".join(out)


class _TranscriptControl(UIControl):
    """Scrollable, incrementally wrapped conversation pane.

    A logical line is a list of styled runs. Text that streams into the last line is
    wrapped where the previous piece ended, so a long paragraph costs its new characters
    per frame, not its whole length (a 30,000-character paragraph took 11 s of CPU before).
    """

    def __init__(self) -> None:
        self._lines: list[list[Run]] = [[]]
        self._chars: int = 0  # characters in the open (last) line, for tab stops
        self._wrap_width: int = 0
        self._wrapped_cache: list[list[StyleAndTextTuples]] = []
        self._visual_offsets: list[int] = []
        self._visual_lines: list[StyleAndTextTuples] = [[]]
        self._dirty_from: int = 0
        # The open line as far as it is wrapped: (its index, runs wrapped, column reached).
        self._tail: tuple[int, int, int] | None = None

        self.follow: bool = True
        self.scroll_top: int = 0
        self.last_width: int = 80
        self.last_height: int = 20
        self.total_visual_lines: int = 1

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
        self.follow = True
        self.scroll_top = 0
        self.total_visual_lines = 1

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
                    self._chars += len(part)
        self._dirty_from = min(self._dirty_from, open_index)
        if len(self._lines) > MAX_LOGICAL_LINES:
            drop = _TRIM_BATCH
            del self._lines[:drop]
            self._wrapped_cache = []
            self._visual_offsets = []
            self._visual_lines = []
            self._dirty_from = 0
            self._tail = None
            if not self.follow:
                self.scroll_top = max(0, self.scroll_top - drop)

    def _displayed_logical_count(self) -> int:
        if len(self._lines) > 1 and not self._lines[-1]:
            return len(self._lines) - 1
        return len(self._lines)

    def _sync_wrap(self, width: int) -> None:
        width = max(1, width)
        count = self._displayed_logical_count()
        if width != self._wrap_width:
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
        self.total_visual_lines = len(self._visual_lines)

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
        total = len(visual)
        cursor_y = min(self.scroll_top, total - 1)
        return UIContent(
            get_line=lambda i: visual[i],
            line_count=total,
            cursor_position=Point(x=0, y=cursor_y),
            show_cursor=False,
        )

    @override
    def mouse_handler(self, mouse_event: MouseEvent) -> NotImplementedOrNone:
        if mouse_event.event_type == MouseEventType.SCROLL_UP:
            self.scroll_by(-_WHEEL_LINES)
            return None
        if mouse_event.event_type == MouseEventType.SCROLL_DOWN:
            self.scroll_by(_WHEEL_LINES)
            return None
        return NotImplemented


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


def _build_style(color: bool) -> Style:
    if not color:
        return Style.from_dict(
            {
                "status-bar": "reverse",
                "question-bar": "reverse",
                "secret-bar": "reverse",
                "completion-menu.completion.current": "reverse",
            }
        )
    return Style.from_dict(
        {
            "status-bar": "bg:#262626 fg:#e4e4e4",
            "status-bar.model": "bold fg:#87d7ff",
            "status-bar.dim": "fg:#9e9e9e",
            "status-bar.activity": "bold fg:#ffd75f",
            "status-bar.scroll": "bold fg:#ffaf5f",
            "status-bar.allow": "bold fg:#ffd75f",
            "status-bar.deny": "bold fg:#ff5f5f",
            "prompt": "bold ansicyan",
            "prompt.more": "fg:#8a8a8a",
            "running-hint": "italic fg:#8a8a8a",
            "question-bar": "bold bg:#5f4b00 fg:#ffffff",
            "question-prompt": "bold fg:#ffd75f",
            "secret-bar": "bold bg:#1c3d5a fg:#ffffff",
            "secret-prompt": "bold fg:#87d7ff",
            "completion-menu": "bg:#262626 fg:#d0d0d0",
            "completion-menu.completion.current": "bold bg:#005f87 fg:#ffffff",
            "completion-menu.meta.completion": "bg:#262626 fg:#9e9e9e",
            "completion-menu.meta.completion.current": "bg:#005f87 fg:#e4e4e4",
            "scrollbar.background": "bg:#1c1c1c",
            "scrollbar.button": "bg:#585858",
        }
    )


class FullScreenTui:
    """Full-screen alternate-screen front end for :class:`karox6.tui.ChatApp`.

    Layout (top to bottom):
    1. Scrollable conversation pane (fills available height).
    2. One-line status bar (model, project/branch, live activity, context/tokens, approval mode).
    3. Input area pinned to the bottom (multiline prompt, inline question/approval, hidden secret,
       or running hint), with floating ``CompletionsMenu`` and ``Ctrl+R`` ``SearchToolbar``.
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
    ) -> None:
        self._workspace = (workspace or Path.cwd()).resolve()
        self._home = home
        self._color = color
        self._glyph = _glyphs(encoding or getattr(sys.stdout, "encoding", None))
        self._fallback_model = model
        self._fallback_provider = provider
        self._fallback_approval = approval_mode
        self._fallback_branch = _git_branch(self._workspace)
        self._chat_app: ChatApp | None = None

        self._mode: str = "prompt"  # "prompt" | "running" | "question" | "secret"
        self._prompt_text: str = self._glyph["prompt"]
        self._more_text: str = self._glyph["more"]
        self._question_prompt: str = ""
        self._secret_prompt: str = ""
        self._turn_active: bool = False
        self._turn_status_fn: Callable[[], str] | None = None
        self._turn_interrupt_fn: Callable[[], None] | None = None

        self._read_future: asyncio.Future[str] | None = None
        self._question_future: asyncio.Future[str] | None = None
        self._paste_task: asyncio.Future[None] | None = None
        self._queued_inputs: deque[str | BaseException] = deque()

        self._transcript = _TranscriptControl()

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
        self._search_toolbar = SearchToolbar(ignore_case=True)

        self._transcript_window = Window(
            content=self._transcript,
            wrap_lines=False,
            get_vertical_scroll=lambda _w: self._transcript.scroll_top,
            right_margins=[_QuietScrollbar(display_arrows=False)],
            always_hide_cursor=True,
        )
        self._status_window = Window(
            content=FormattedTextControl(self._status_bar_text, show_cursor=False),
            height=1,
            style="class:status-bar",
            wrap_lines=False,
            always_hide_cursor=True,
        )
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
        self._question_banner = Window(
            content=FormattedTextControl(self._question_banner_text, show_cursor=False),
            height=1,
            style="class:question-bar",
            always_hide_cursor=True,
        )
        self._secret_banner = Window(
            content=FormattedTextControl(self._secret_banner_text, show_cursor=False),
            height=1,
            style="class:secret-bar",
            always_hide_cursor=True,
        )

        in_prompt = Condition(lambda: self._mode == "prompt")
        in_running = Condition(lambda: self._mode == "running")
        in_input = in_prompt | in_running
        in_question = Condition(lambda: self._mode == "question")
        in_secret = Condition(lambda: self._mode == "secret")

        body = HSplit(
            [
                self._transcript_window,
                self._status_window,
                ConditionalContainer(self._search_toolbar, filter=is_searching),
                ConditionalContainer(self._question_banner, filter=in_question),
                ConditionalContainer(self._secret_banner, filter=in_secret),
                ConditionalContainer(self._input_window, filter=in_input),
                ConditionalContainer(self._answer_window, filter=in_question),
                ConditionalContainer(self._secret_window, filter=in_secret),
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

        self._app: Application[None] = Application(
            layout=Layout(root, focused_element=self._input_window),
            style=_build_style(color),
            color_depth=None if color else ColorDepth.DEPTH_1_BIT,
            key_bindings=self._build_key_bindings(),
            full_screen=True,
            mouse_support=True,
            min_redraw_interval=0.02,
            terminal_size_polling_interval=0.2,
            input=input,
            output=output,
        )
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
        self.invalidate()

    def columns(self) -> int:
        """Cells a line of the conversation can take (the scrollbar column left out)."""
        return max(self._app.output.get_size().columns - 1, 20)

    def _paste_done(self, task: asyncio.Future[None]) -> None:
        self._paste_task = None
        if not task.cancelled() and task.exception() is not None:
            self.write(f"  cannot read the clipboard: {task.exception()}\n")

    def prefill(self, text: str) -> None:
        """Put ``text`` into the input line, cursor at its end (an undone message comes
        back for editing); text the user has already typed is not replaced."""
        if not self._input_buffer.text:
            self._input_buffer.set_document(Document(text, len(text)), bypass_readonly=True)
            self.invalidate()

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
        for fut in (self._read_future, self._question_future):
            if fut is not None and not fut.done():
                fut.set_exception(exc)

    # -- ChatApp I/O callables ---------------------------------------------------------

    async def read_line(self, prompt: str) -> str:
        self._prompt_text = prompt
        self._set_mode("prompt")
        if self._queued_inputs:
            item = self._queued_inputs.popleft()
            if isinstance(item, BaseException):
                raise item
            self._echo_user_message(item)
            return item
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
        if self._app_task is not None and self._app_task.done():
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
        if self._app_task is not None and self._app_task.done():
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

    # -- turn & mode transitions -------------------------------------------------------

    def _begin_turn(self, status: Callable[[], str], interrupt: Callable[[], None]) -> None:
        self._turn_active = True
        self._turn_status_fn = status
        self._turn_interrupt_fn = interrupt
        if self._mode not in ("question", "secret"):
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
        if self._color:
            rendered = [f"\x1b[1;36m{prefix}\x1b[0m{_plain_text(lines[0])}"]
            rendered.extend(f"\x1b[2m{more}\x1b[0m{_plain_text(line)}" for line in lines[1:])
        else:
            rendered = [f"{prefix}{_plain_text(lines[0])}"]
            rendered.extend(f"{more}{_plain_text(line)}" for line in lines[1:])
        self._transcript.scroll_to_bottom()
        self.write("\n".join(rendered) + "\n")

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
        snap = self._snapshot()
        if snap.running and not snap.activity and self._turn_status_fn is not None:
            from dataclasses import replace

            snap = replace(snap, activity=self._turn_status_fn())
        scrolled = 0 if self._transcript.follow else self._transcript.lines_below
        queued = sum(1 for item in self._queued_inputs if isinstance(item, str))
        fragments = status_bar_fragments(
            snap,
            self._glyph,
            self._app.output.get_size().columns,
            scrolled=scrolled,
            queued=queued if self._turn_active else 0,
        )
        return [
            (f"class:status-bar.{style}" if style else "class:status-bar", text)
            for style, text in fragments
        ]

    def _question_banner_text(self) -> StyleAndTextTuples:
        dot = self._glyph["dot"]
        if "run it?" in self._question_prompt.lower():
            return [
                (
                    "class:question-bar",
                    f" Approval required — y: run{dot}n: deny{dot}a: always{dot}Esc: stop turn",
                )
            ]
        return [
            (
                "class:question-bar",
                f" Input required — Enter: confirm{dot}Esc / Ctrl+C: cancel",
            )
        ]

    def _secret_banner_text(self) -> StyleAndTextTuples:
        dot = self._glyph["dot"]
        return [
            (
                "class:secret-bar",
                f" Hidden input (not echoed) — Enter: submit{dot}Esc / Ctrl+C: cancel",
            )
        ]

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

        @keys.add("end", filter=Condition(_end_should_scroll) & ~is_searching)
        def _end_jump_bottom(event: KeyPressEvent) -> None:
            if self._transcript.scroll_to_bottom():
                event.app.invalidate()

        # -- running turn interrupt --

        @keys.add("escape", eager=True, filter=in_running)
        @keys.add("c-c", eager=True, filter=in_running)
        @keys.add("<sigint>", eager=True, filter=in_running)
        def _stop_turn(event: KeyPressEvent) -> None:
            self._queued_inputs.clear()
            if self._turn_interrupt_fn is not None:
                self._turn_interrupt_fn()
            event.app.invalidate()

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

        @keys.add("escape", "enter", filter=in_prompt & ~is_searching)
        def _alt_enter(event: KeyPressEvent) -> None:
            del event
            self._input_buffer.insert_text("\n")

        @keys.add("escape", "v", filter=in_prompt & ~is_searching)
        def _alt_v(event: KeyPressEvent) -> None:
            del event
            if self._chat_app is not None and self._paste_task is None:
                # Reading the clipboard must not block typing.
                task = asyncio.ensure_future(self._chat_app.paste_image())
                self._paste_task = task
                task.add_done_callback(self._paste_done)

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
            buf = self._input_buffer
            doc = buf.document
            if doc.text_before_cursor.endswith("\\") and not doc.text_after_cursor:
                buf.delete_before_cursor(1)
                buf.insert_text("\n")
                return
            if pasted_line_end(event):
                buf.insert_text("\n")
                return
            state = buf.complete_state
            if state is not None and state.current_completion is not None:
                buf.apply_completion(state.current_completion)
                return
            text = buf.text
            if text.strip():
                buf.append_to_history()
            buf.cancel_completion()
            buf.reset()
            if self._read_future is not None and not self._read_future.done():
                self._echo_user_message(text)
                self._read_future.set_result(text)
            elif text.strip():
                self._queued_inputs.append(text)
                self.invalidate()  # the status bar counts what is waiting

        return keys
