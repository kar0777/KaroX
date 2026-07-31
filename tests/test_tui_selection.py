"""What selecting and copying in the transcript actually does today.

The transcript is a ``RichLog``, which does not take part in Textual's selection
machinery: it has no ``get_selection`` that returns anything and renders no
highlight. ``ChatLog`` therefore implements selection by hand -- mouse handlers
that build a ``Selection`` from line numbers, a ``render_line`` override that
repaints whole rows, and a ``get_selection`` that maps a vertical range onto a
list of whole messages through a table of heights snapshotted at write time.

Every one of those pieces loses information, and the existing tests do not notice
because they assert that *something* was selected and highlighted rather than that
it was the right something. The tests below assert the right something. Most are
``expectedFailure`` against an identifier in ``docs/UX_BUG_INVENTORY.md``; unittest
counts an unexpected success as a failure, so a fix cannot land without deleting
the decorator, and a regression afterwards cannot pass unnoticed.
"""

from __future__ import annotations

import unittest

from textual.geometry import Offset
from textual.selection import Selection

from _tui_harness import (  # noqa: F401 - inserts src on sys.path via _support
    STANDARD,
    karox_app,
)

import karox.tui as tui


def _transcript(app: object) -> object:
    return app.query_one("#conversation", tui.ChatLog)


def _row_holding(log: object, needle: str) -> tuple[int, int]:
    """Return ``(widget_y, column)`` of the row containing ``needle``.

    ``render_line`` takes a viewport row while a ``Selection`` carries widget
    coordinates, so the scroll offset has to be added. Getting that wrong is how
    a selection test ends up asserting against a different message than the one it
    meant to.
    """
    for viewport_y in range(log.size.height):
        text = log.render_line(viewport_y).text
        if needle in text:
            return viewport_y + log.scroll_offset.y, text.index(needle)
    raise AssertionError(f"no rendered row contains {needle!r}")


async def _conversation(app: object, pilot: object) -> object:
    app._write_user("первая задача")
    app._write_assistant("ALPHA BETA GAMMA")
    app._write_user("вторая задача")
    app._write_assistant("DELTA EPSILON ZETA")
    await pilot.pause(0.3)
    return _transcript(app)


@unittest.skipUnless(tui._HAS_TEXTUAL, "textual is not installed")
class SelectionPrecisionTests(unittest.IsolatedAsyncioTestCase):
    @unittest.expectedFailure  # UX-001
    async def test_selecting_four_characters_copies_four_characters(self) -> None:
        # UX-001: the mouse handlers discard the horizontal position entirely --
        # ``_begin_drag`` anchors at ``Offset(0, line)`` and ``_extend_drag`` ends
        # at ``Offset(10_000, line)`` -- and ``get_selection`` returns whole
        # entries from its plain-text list. Selecting the word BETA out of
        # "ALPHA BETA GAMMA" returns the entire message.
        async with karox_app(size=STANDARD) as (app, pilot):
            log = await _conversation(app, pilot)
            widget_y, column = _row_holding(log, "BETA")
            app.screen.selections[log] = Selection(
                Offset(column, widget_y), Offset(column + 4, widget_y)
            )

            self.assertEqual(app.screen.get_selected_text(), "BETA")

    @unittest.expectedFailure  # UX-001
    async def test_selecting_one_line_of_a_message_copies_one_line(self) -> None:
        # The same defect at line granularity, which is the common case: a
        # three-line answer with one interesting line in it copies all three.
        async with karox_app(size=STANDARD) as (app, pilot):
            app._write_assistant("ПЕРВАЯ СТРОКА\nВТОРАЯ СТРОКА\nТРЕТЬЯ СТРОКА")
            await pilot.pause(0.3)
            log = _transcript(app)
            widget_y, column = _row_holding(log, "ВТОРАЯ СТРОКА")
            app.screen.selections[log] = Selection(
                Offset(column, widget_y), Offset(column + len("ВТОРАЯ СТРОКА"), widget_y)
            )

            self.assertEqual(app.screen.get_selected_text(), "ВТОРАЯ СТРОКА")


@unittest.skipUnless(tui._HAS_TEXTUAL, "textual is not installed")
class SelectionMappingTests(unittest.IsolatedAsyncioTestCase):
    """The table that maps a screen row onto a message, and how it goes wrong."""

    @unittest.expectedFailure  # UX-003
    async def test_the_recorded_height_of_a_message_matches_what_it_renders(self) -> None:
        # UX-003: ``ChatLog.write`` records ``virtual_size.height`` immediately
        # after ``super().write()``, before layout has settled, so the first entry
        # gets whatever the fallback produces. Measured: the three-line welcome is
        # recorded as ending at row 1 while it occupies rows 0, 1 and 2.
        #
        # The consequence is not an off-by-one in a diagnostic. Entry 1's block
        # becomes rows 1 to 5, which covers the last two rows of the welcome, so
        # selecting the end of the welcome copies the first user message instead.
        async with karox_app(size=STANDARD) as (app, pilot):
            log = _transcript(app)
            self.assertEqual(len(log._plain_lines), 1, "expected only the welcome")

            self.assertEqual(
                log._plain_line_end_heights[0],
                log.virtual_size.height,
                "the only entry does not account for every rendered row",
            )

    @unittest.expectedFailure  # UX-003
    async def test_selecting_the_welcome_does_not_return_a_later_message(self) -> None:
        async with karox_app(size=STANDARD) as (app, pilot):
            log = await _conversation(app, pilot)
            widget_y, _ = _row_holding(log, "Введите /")

            app.screen.selections[log] = Selection(
                Offset(0, widget_y), Offset(80, widget_y)
            )
            selected = app.screen.get_selected_text() or ""

            self.assertNotIn("первая задача", selected)
            self.assertIn("Введите /", selected)

    @unittest.expectedFailure  # UX-003
    async def test_a_message_written_before_the_size_is_known_records_a_real_height(
        self,
    ) -> None:
        # This is the mechanism behind UX-003, stated as its own assertion because
        # it explains every symptom.
        #
        # ``RichLog.write`` defers rendering until its size is known -- its own
        # docstring says a write from ``compose`` or ``on_mount`` is not rendered
        # immediately. The welcome message is written exactly there, so when
        # ``ChatLog.write`` reads ``virtual_size.height`` straight afterwards it
        # gets 0, and the ``max(total, previous + 1)`` fallback fabricates a height
        # of 1 for a message that occupies three rows.
        #
        # Nothing later repairs it: the table is append-only. So entry 1's block
        # starts two rows early and every subsequent lookup near the top of the log
        # resolves to the wrong message.
        async with karox_app(size=STANDARD) as (app, pilot):
            log = _transcript(app)
            rendered_rows = sum(
                1
                for y in range(log.virtual_size.height)
                if log.render_line(y).text.strip()
            )

            self.assertGreaterEqual(
                log._plain_line_end_heights[0],
                rendered_rows,
                "the welcome is recorded as shorter than it draws",
            )


@unittest.skipUnless(tui._HAS_TEXTUAL, "textual is not installed")
class TranscriptWidthTests(unittest.IsolatedAsyncioTestCase):
    """How much of the terminal the conversation is allowed to use."""

    @unittest.expectedFailure  # UX-014
    async def test_an_answer_uses_the_width_of_a_wide_window(self) -> None:
        # UX-014: ``RichLog.write`` takes ``expand=False`` by default, and the
        # transcript never overrides it, so a renderable is drawn at its own
        # measured width and the content region is used only as an upper bound.
        # Measured: a 116-column conversation area draws the answer panel 37
        # columns wide and wraps the text into eight lines, leaving 79 columns
        # empty. A wide terminal reads like a phone.
        async with karox_app(size=(120, 30)) as (app, pilot):
            app._write_assistant(
                "Проверка не прошла, потому что декодирование вывода дочернего "
                "процесса использовало errors=ignore, и поэтому кириллица не "
                "искажалась, а удалялась целиком, оставляя агента без текста "
                "ошибки при непустом коде возврата."
            )
            await pilot.pause(0.4)
            log = _transcript(app)

            self.assertGreaterEqual(
                log.virtual_size.width,
                int(log.size.width * 0.8),
                f"answer laid out at {log.virtual_size.width} of "
                f"{log.size.width} available columns",
            )

    @unittest.expectedFailure  # UX-015
    async def test_an_answer_is_relaid_out_when_the_window_changes(self) -> None:
        # UX-015: ``RichLog`` renders each write once, to a list of lines, and
        # keeps them. A resize changes the viewport but not those lines, so an
        # answer written in a narrow window stays narrow after the window is
        # widened, and one written wide is clipped when the window shrinks below
        # its recorded width -- there is no reflow at any point.
        async with karox_app(size=(56, 30)) as (app, pilot):
            app._write_assistant(
                "Короткие строки, записанные в узком окне, обязаны "
                "перенестись заново, когда окно станет широким."
            )
            await pilot.pause(0.4)
            log = _transcript(app)
            narrow_height = log.virtual_size.height

            await pilot.resize_terminal(140, 30)
            await pilot.pause(0.5)

            self.assertLess(
                log.virtual_size.height,
                narrow_height,
                "the answer occupies the same number of rows at 140 columns "
                "as it did at 56, so it was never re-wrapped",
            )


@unittest.skipUnless(tui._HAS_TEXTUAL, "textual is not installed")
class CopyBindingTests(unittest.IsolatedAsyncioTestCase):
    @unittest.expectedFailure  # UX-005
    async def test_copying_is_possible_while_a_task_is_running(self) -> None:
        # UX-005: Ctrl+C is bound to ``stop_or_copy``, which returns early to
        # ``stop_agent`` whenever the agent is busy. So during the one period a
        # user most wants to copy something -- a path or an error scrolling past
        # while a task runs -- the copy key aborts the task instead, and there is
        # no second binding that copies.
        async with karox_app(size=STANDARD) as (app, pilot):
            log = await _conversation(app, pilot)
            widget_y, column = _row_holding(log, "ALPHA BETA GAMMA")
            app.screen.selections[log] = Selection(
                Offset(column, widget_y), Offset(column + 16, widget_y)
            )
            app.agent_busy = True

            await pilot.press("ctrl+c")
            await pilot.pause(0.2)

            self.assertIn("ALPHA", app.clipboard or "")
            self.assertFalse(
                app._stop_requested, "the copy key stopped the running task"
            )

    @unittest.expectedFailure  # UX-008
    async def test_copying_the_last_answer_is_distinguishable_from_copying_a_selection(
        self,
    ) -> None:
        # UX-008: with no selection, ``action_stop_or_copy`` falls back to the last
        # assistant answer and reports the same "Скопировано" as a real selection
        # copy. Combined with UX-001 and UX-003 -- where the mapping can return
        # nothing -- a user who selects a line, presses copy, and is told it
        # worked can end up with an entirely different message on the clipboard
        # and no way to tell.
        async with karox_app(size=STANDARD) as (app, pilot):
            await _conversation(app, pilot)
            app.screen.clear_selection()
            app.agent_busy = False
            notices: list[str] = []
            app.notify = lambda message, *a, **k: notices.append(str(message))

            await pilot.press("ctrl+c")
            await pilot.pause(0.2)

            self.assertEqual(app.clipboard, "DELTA EPSILON ZETA")
            self.assertTrue(notices, "copying said nothing at all")
            self.assertNotEqual(
                notices[-1],
                "Скопировано",
                "a fallback copy is announced exactly like a selection copy",
            )


if __name__ == "__main__":
    unittest.main()
