"""The transient, inline view used while a chat turn is running."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from types import TracebackType
from typing import override

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition
from prompt_toolkit.formatted_text import ANSI, StyleAndTextTuples
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.layout import Dimension, HSplit, Layout, Window
from prompt_toolkit.layout.containers import ConditionalContainer
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition

_PREVIEW_CHARS = 4096


def question_bindings() -> KeyBindings:
    keys = KeyBindings()

    @keys.add("escape", eager=True)
    def stop(event: KeyPressEvent) -> None:
        event.app.exit(exception=KeyboardInterrupt(), style="class:aborting")

    return keys


class _InlineRows(HSplit):
    @override
    def write_to_screen(
        self,
        screen: Screen,
        mouse_handlers: MouseHandlers,
        write_position: WritePosition,
        parent_style: str,
        erase_bg: bool,
        z_index: int | None,
    ) -> None:
        # Inline applications may be offered all rows below the cursor. Only paint
        # the rows we need, so the renderer never scrolls to fill unused space.
        pos = write_position
        height = min(pos.height, self.preferred_height(pos.width, pos.height).preferred)
        area = WritePosition(pos.xpos, pos.ypos, pos.width, height)
        super().write_to_screen(screen, mouse_handlers, area, parent_style, erase_bg, z_index)


class TurnDisplay:
    """Show an unfinished stream line and a status row above the terminal cursor.

    Only completed lines are written to scrollback. The terminal renderer owns the
    temporary rows, including wrapping and resizing, until the line is complete.
    """

    def __init__(
        self,
        write: Callable[[str], None],
        status: Callable[[], str],
        interrupt: Callable[[], None],
    ) -> None:
        self._write = write
        self._pending: list[str] = []
        self._preview = ""
        self._paused = False
        keys = KeyBindings()

        @keys.add("escape", eager=True)
        @keys.add("c-c", eager=True)
        def stop(event: KeyPressEvent) -> None:
            interrupt()

        preview = Window(
            FormattedTextControl(self._preview_text, show_cursor=False),
            wrap_lines=True,
            dont_extend_height=True,
            height=Dimension(max=4),
        )
        self._app: Application[None] = Application(
            layout=Layout(
                _InlineRows(
                    [
                        Window(FormattedTextControl(status, show_cursor=False), height=1),
                        ConditionalContainer(
                            preview, filter=Condition(lambda: bool(self._preview))
                        ),
                    ]
                ),
                focused_element=preview,
            ),
            full_screen=False,
            erase_when_done=True,
            key_bindings=keys,
            refresh_interval=0.12,
            min_redraw_interval=0.02,
            terminal_size_polling_interval=0.2,
        )
        self._app.ttimeoutlen = 0.12  # allow a lone Esc through the VT escape parser promptly
        self._task: asyncio.Task[None] | None = None

    def _preview_text(self) -> StyleAndTextTuples:
        text = ANSI(self._preview.expandtabs()).__pt_formatted_text__()
        return [*text, ("[SetCursorPosition]", "")]

    async def __aenter__(self) -> TurnDisplay:
        try:
            await self._start()
        except BaseException:
            await self._stop()
            raise
        return self

    async def _start(self) -> None:
        self._task = asyncio.create_task(self._app.run_async(handle_sigint=False))
        # Let the application attach its input and draw the first frame.
        try:
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            if not self._app.is_running:
                self._task.cancel()
            raise
        if self._task.done():
            await self._task

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            await self._stop()
        finally:
            if self._pending:
                self._write("".join(self._pending))
                self._pending.clear()

    async def _stop(self) -> None:
        if self._app.is_running and not self._app.is_done:
            self._app.exit()
        if self._task is not None:
            with contextlib.suppress(EOFError):
                await asyncio.shield(self._task)
            self._task = None

    def write(self, text: str) -> None:
        if self._paused or not self._app.is_running or self._app.is_done:
            if self._pending:
                text = "".join(self._pending) + text
                self._pending.clear()
                self._preview = ""
            self._write(text)
            return
        complete, newline, remainder = text.rpartition("\n")
        if newline:
            self._app.renderer.erase()
            self._write("".join(self._pending) + complete + newline)
            self._pending.clear()
            self._preview = ""
        if remainder:
            self._pending.append(remainder)
            self._preview = (self._preview + remainder[-_PREVIEW_CHARS:])[-_PREVIEW_CHARS:]
        self._app.invalidate()

    @contextlib.asynccontextmanager
    async def pause(self) -> AsyncIterator[None]:
        if self._paused or not self._app.is_running or self._app.is_done:
            yield
            return
        # Fully detach input and resize handling so only the question owns the terminal.
        self._paused = True
        try:
            await self._stop()
            yield
        finally:
            self._paused = False
            task = asyncio.current_task()
            if task is not None and not task.cancelling():
                await self._start()
