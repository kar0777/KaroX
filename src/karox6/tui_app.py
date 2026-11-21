"""Full-screen terminal UI built with prompt_toolkit's Application.

Keeps ChatApp's logic untouched by implementing its injected I/O callables
(``write``, ``read_line``, ``read_answer``, ``read_secret``) and ``display_factory``
over one persistent alternate-screen ``Application``.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
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
from prompt_toolkit.filters import Condition, has_completions, is_searching
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples, fragment_list_width
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
    StatusSnapshot,
    _git_branch,
    _glyphs,
    _k,
    _plain_text,
    _PromptInterrupt,
    build_completer,
    build_history,
)

if TYPE_CHECKING:
    from prompt_toolkit.key_binding.key_bindings import NotImplementedOrNone

MAX_LOGICAL_LINES = 20_000
_TRIM_BATCH = 2_000
_WHEEL_LINES = 3


def _parse_ansi_line(raw: str) -> StyleAndTextTuples:
    expanded = raw.expandtabs(4)
    if "\x1b" not in expanded:
        return [("", expanded)] if expanded else []
    return list(ANSI(expanded).__pt_formatted_text__())


def _wrap_fragments(fragments: StyleAndTextTuples, width: int) -> list[StyleAndTextTuples]:
    """Wrap formatted fragments to terminal cell ``width``, respecting wide Unicode chars."""
    if width <= 0:
        return [fragments]
    if not fragments:
        return [[]]
    lines: list[StyleAndTextTuples] = []
    cur: StyleAndTextTuples = []
    cur_style = ""
    cur_chars: list[str] = []
    col = 0

    def flush_run() -> None:
        nonlocal cur_chars
        if cur_chars:
            cur.append((cur_style, "".join(cur_chars)))
            cur_chars = []

    def finish_line() -> None:
        nonlocal cur, col
        flush_run()
        lines.append(cur)
        cur = []
        col = 0

    for item in fragments:
        style = item[0]
        text = item[1]
        for ch in text:
            cw = max(get_cwidth(ch), 0)
            if col > 0 and col + cw > width:
                finish_line()
            if style != cur_style:
                flush_run()
                cur_style = style
            cur_chars.append(ch)
            col += cw
    finish_line()
    return lines or [[]]


class _TranscriptControl(UIControl):
    """Scrollable, incrementally wrapped ANSI conversation pane."""

    def __init__(self) -> None:
        self._raw_lines: list[str] = [""]
        self._parsed_lines: list[StyleAndTextTuples] = [[]]
        self._wrap_width: int = 0
        self._wrapped_cache: list[list[StyleAndTextTuples]] = []
        self._visual_offsets: list[int] = []
        self._visual_lines: list[StyleAndTextTuples] = [[]]
        self._dirty_from: int = 0

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
        from karox6.tui import _ANSI

        return _ANSI.sub("", "\n".join(self._raw_lines))

    def clear(self) -> None:
        self._raw_lines = [""]
        self._parsed_lines = [[]]
        self._wrapped_cache = []
        self._visual_offsets = []
        self._visual_lines = [[]]
        self._dirty_from = 0
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
        parts = text.split("\n")
        first_idx = len(self._raw_lines) - 1
        self._raw_lines[-1] += parts[0]
        self._parsed_lines[-1] = _parse_ansi_line(self._raw_lines[-1])
        self._dirty_from = min(self._dirty_from, first_idx)
        for part in parts[1:]:
            self._raw_lines.append(part)
            self._parsed_lines.append(_parse_ansi_line(part))
        if len(self._raw_lines) > MAX_LOGICAL_LINES:
            drop = _TRIM_BATCH
            del self._raw_lines[:drop]
            del self._parsed_lines[:drop]
            self._wrapped_cache = []
            self._visual_offsets = []
            self._visual_lines = []
            self._dirty_from = 0
            if not self.follow:
                self.scroll_top = max(0, self.scroll_top - drop)

    def _displayed_logical_count(self) -> int:
        if len(self._raw_lines) > 1 and self._raw_lines[-1] == "":
            return len(self._raw_lines) - 1
        return len(self._raw_lines)

    def _sync_wrap(self, width: int) -> None:
        width = max(1, width)
        count = self._displayed_logical_count()
        if width != self._wrap_width:
            self._wrap_width = width
            self._wrapped_cache = []
            self._visual_offsets = []
            self._visual_lines = []
            self._dirty_from = 0

        dirty = min(self._dirty_from, len(self._wrapped_cache), count)
        if dirty < len(self._wrapped_cache):
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
            wrapped = _wrap_fragments(self._parsed_lines[i], width)
            self._wrapped_cache.append(wrapped)
            self._visual_lines.extend(wrapped)

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
            right_margins=[ScrollbarMargin(display_arrows=False)],
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

        self._app_task: asyncio.Task[None] | None = None
        self._active: bool = False

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
        dot = self._glyph["dot"]
        left: StyleAndTextTuples = [
            ("class:status-bar", " "),
            ("class:status-bar.model", _plain_text(snap.model)),
        ]
        proj = _plain_text(snap.workspace_name)
        if snap.branch:
            proj = f"{proj} ({_plain_text(snap.branch)})"
        left.append(("class:status-bar.dim", f"{dot}{proj}"))

        activity = snap.activity
        if not activity and self._turn_status_fn is not None:
            activity = self._turn_status_fn()
        if snap.running and activity:
            left.append(("class:status-bar.dim", dot))
            left.append(("class:status-bar.activity", _plain_text(activity)))
        elif not self._transcript.follow and self._transcript.lines_below > 0:
            left.append(("class:status-bar.dim", dot))
            left.append(
                (
                    "class:status-bar.scroll",
                    f"scrolled +{self._transcript.lines_below}L (End)",
                )
            )

        right: StyleAndTextTuples = []
        if snap.context_window:
            ctx = f"ctx {_k(snap.context_used)}/{_k(snap.context_window)} ({snap.context_pct}%)"
            tok = f"{_k(snap.total_tokens)} tok{dot}{ctx}" if snap.total_tokens else ctx
            right.append(("class:status-bar.dim", tok + dot))
        mode_style = {
            "allow": "class:status-bar.allow",
            "deny": "class:status-bar.deny",
        }.get(snap.approval_mode, "class:status-bar.dim")
        right.append((mode_style, f"approve:{snap.approval_mode}"))
        right.append(("class:status-bar", " "))

        width = self._app.output.get_size().columns
        left_w = fragment_list_width(left)
        right_w = fragment_list_width(right)
        gap = width - left_w - right_w
        if gap >= 2:
            return [*left, ("class:status-bar", " " * gap), *right]
        return [*left, ("class:status-bar.dim", dot), *right]

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
            del event
            buf = self._input_buffer
            state = buf.complete_state
            if state is not None and state.current_completion is not None:
                buf.apply_completion(state.current_completion)
                return
            doc = buf.document
            if doc.text_before_cursor.endswith("\\") and not doc.text_after_cursor:
                buf.delete_before_cursor(1)
                buf.insert_text("\n")
                return
            text = buf.text
            if text.strip():
                buf.append_to_history()
            buf.cancel_completion()
            buf.reset()
            if self._read_future is not None and not self._read_future.done():
                self._echo_user_message(text)
                self._read_future.set_result(text)
            else:
                self._queued_inputs.append(text)

        return keys
