"""Selecting and copying in the transcript.

The transcript is a scroll container of one widget per message: a ``Static``
holding a ``Content`` for a user message or a notice, and Textual's own
``Markdown`` for an answer. Both render a ``Content``, which is what
``Widget.get_selection`` extracts from, so selection is Textual's rather than
ours -- character precise, highlighted, and correct after a resize because the
widgets are laid out again rather than replayed from recorded lines.

It replaced a ``RichLog`` subclass with hand-written mouse handlers, a
``render_line`` override reaching into a private attribute, and a table mapping
screen rows onto whole messages. The tests here assert what that arrangement got
wrong, so it cannot come back: precision, and the row a user points at resolving
to the message drawn on it.

Identifiers in comments refer to ``docs/UX_BUG_INVENTORY.md``. Anything still
marked ``expectedFailure`` is an open entry there.
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
    return app.query_one("#conversation", tui.TranscriptView)


def _text_of(node: object) -> str:
    """Everything selectable inside one widget, via the public selection API."""
    extracted = node.get_selection(Selection(None, None))
    return extracted[0] if extracted else ""


def _node_holding(app: object, needle: str) -> tuple[object, int, int]:
    """Find the widget whose own text contains ``needle``.

    Returns the widget, the line index inside it, and the column, which is what a
    ``Selection`` needs -- its offsets are relative to the widget, not the screen.
    """
    for node in _transcript(app).walk_children(with_self=False):
        text = _text_of(node)
        if needle not in text:
            continue
        for line_index, line in enumerate(text.splitlines()):
            if needle in line:
                return node, line_index, line.index(needle)
    raise AssertionError(f"no message widget contains {needle!r}")


async def _conversation(app: object, pilot: object) -> object:
    app._write_user("первая задача")
    app._write_assistant("ALPHA BETA GAMMA")
    app._write_user("вторая задача")
    app._write_assistant("DELTA EPSILON ZETA")
    await pilot.pause(0.3)
    return _transcript(app)


@unittest.skipUnless(tui._HAS_TEXTUAL, "textual is not installed")
class SelectionPrecisionTests(unittest.IsolatedAsyncioTestCase):
    async def test_selecting_four_characters_copies_four_characters(self) -> None:
        # UX-001. The previous implementation read only the vertical range and
        # returned whole entries from a parallel list, so selecting BETA out of
        # "ALPHA BETA GAMMA" produced the entire message.
        async with karox_app(size=STANDARD) as (app, pilot):
            await _conversation(app, pilot)
            node, line, column = _node_holding(app, "BETA")

            app.screen.selections[node] = Selection(
                Offset(column, line), Offset(column + 4, line)
            )

            self.assertEqual(app.screen.get_selected_text(), "BETA")

    async def test_selecting_one_line_of_a_message_copies_one_line(self) -> None:
        # UX-001 at line granularity, which is the common case: a three-line
        # answer with one interesting line in it used to copy all three.
        async with karox_app(size=STANDARD) as (app, pilot):
            app._write_user("ПЕРВАЯ СТРОКА\nВТОРАЯ СТРОКА\nТРЕТЬЯ СТРОКА")
            await pilot.pause(0.3)
            node, line, column = _node_holding(app, "ВТОРАЯ СТРОКА")

            app.screen.selections[node] = Selection(
                Offset(column, line),
                Offset(column + len("ВТОРАЯ СТРОКА"), line),
            )

            self.assertEqual(app.screen.get_selected_text(), "ВТОРАЯ СТРОКА")

    async def test_a_real_drag_selects_from_the_press_to_the_release(self) -> None:
        # UX-002. No drag could express a sub-line range at all: the anchor was
        # column 0 and the end was Offset(10_000, y) by construction, with the
        # mouse event's x read nowhere. Driven here through Pilot's real mouse
        # events, so it is the terminal path rather than a handler called directly.
        async with karox_app(size=STANDARD) as (app, pilot):
            await _conversation(app, pilot)
            node, line, column = _node_holding(app, "EPSILON")
            origin = node.region.offset

            await pilot.mouse_down(offset=origin + (column, line))
            await pilot.mouse_up(offset=origin + (column + len("EPSILON"), line))
            await pilot.pause(0.2)

            self.assertEqual(app.screen.get_selected_text(), "EPSILON")


@unittest.skipUnless(tui._HAS_TEXTUAL, "textual is not installed")
class SelectionMappingTests(unittest.IsolatedAsyncioTestCase):
    """The row a user points at, and which message it resolves to."""

    async def test_pointing_at_a_row_returns_the_message_drawn_on_it(self) -> None:
        # UX-003, asserted as the property rather than as the mechanism.
        #
        # The old transcript kept a table of cumulative heights snapshotted at
        # write time, and RichLog defers rendering until its size is known, so the
        # three-line welcome was recorded as one row. Every block after it began
        # two rows early: selecting the end of the welcome returned the first user
        # message. There is no table now -- each message is a widget and Textual
        # resolves the row -- and this walks every message to say so.
        async with karox_app(size=STANDARD) as (app, pilot):
            transcript = await _conversation(app, pilot)

            checked = 0
            for node in transcript.walk_children(with_self=False):
                own = _text_of(node)
                if not own.strip() or node.region.height < 1:
                    continue
                if not node.region.overlaps(app.screen.region):
                    continue  # scrolled out of view; nothing to point at
                app.screen.clear_selection()
                app.screen.selections[node] = Selection(Offset(0, 0), Offset(500, 0))
                got = app.screen.get_selected_text() or ""
                self.assertIn(
                    got.strip(),
                    own,
                    f"row 0 of {type(node).__name__} returned text it does not hold",
                )
                checked += 1

            self.assertGreater(checked, 3, "the walk found almost nothing to check")

    async def test_a_selection_still_belongs_to_its_message_after_a_resize(self) -> None:
        # The old height table was a snapshot: a resize re-wrapped everything and
        # nothing recomputed it, so afterwards a row mapped to whichever message
        # had been there at the old width.
        async with karox_app(size=(100, 30)) as (app, pilot):
            await _conversation(app, pilot)
            node, line, column = _node_holding(app, "DELTA")
            app.screen.selections[node] = Selection(
                Offset(column, line), Offset(column + 5, line)
            )
            self.assertEqual(app.screen.get_selected_text(), "DELTA")

            await pilot.resize_terminal(56, 30)
            await pilot.pause(0.3)

            self.assertEqual(app.screen.get_selected_text(), "DELTA")


@unittest.skipUnless(tui._HAS_TEXTUAL, "textual is not installed")
class TranscriptWidthTests(unittest.IsolatedAsyncioTestCase):
    """How much of the terminal the conversation is allowed to use."""

    LONG_ANSWER = (
        "Проверка не прошла, потому что декодирование вывода дочернего "
        "процесса использовало errors=ignore, и поэтому кириллица не "
        "искажалась, а удалялась целиком, оставляя агента без текста "
        "ошибки при непустом коде возврата."
    )

    async def test_an_answer_uses_the_width_of_a_wide_window(self) -> None:
        # UX-014. RichLog.write takes expand=False, and the transcript never
        # overrode it, so a renderable was laid out at its own measured width
        # while the content region served only as an upper bound: measured, a
        # 116-column conversation drew the answer 37 columns wide and left 79
        # empty. A CSS-framed widget at `width: 1fr` takes what it is given.
        async with karox_app(size=(120, 30)) as (app, pilot):
            app._write_assistant(self.LONG_ANSWER)
            await pilot.pause(0.4)
            transcript = _transcript(app)
            answer = transcript.children[-1]

            self.assertGreaterEqual(
                answer.size.width,
                int(transcript.size.width * 0.9),
                f"answer laid out at {answer.size.width} of "
                f"{transcript.size.width} available columns",
            )

    async def test_an_answer_is_relaid_out_when_the_window_changes(self) -> None:
        # UX-015. RichLog rendered each write once into a list of lines and kept
        # them, so an answer written at 56 columns occupied the same rows at 140.
        # A widget tree is laid out again on resize, so the same text needs fewer
        # rows once it has more columns.
        async with karox_app(size=(56, 30)) as (app, pilot):
            app._write_assistant(self.LONG_ANSWER)
            await pilot.pause(0.4)
            answer = _transcript(app).children[-1]
            narrow_height = answer.size.height

            await pilot.resize_terminal(140, 30)
            await pilot.pause(0.5)

            self.assertLess(
                answer.size.height,
                narrow_height,
                f"the answer still occupies {answer.size.height} rows at 140 "
                f"columns, the same as at 56, so it was never re-wrapped",
            )


@unittest.skipUnless(tui._HAS_TEXTUAL, "textual is not installed")
class CopyBindingTests(unittest.IsolatedAsyncioTestCase):
    async def test_copying_is_possible_while_a_task_is_running(self) -> None:
        # UX-005: Ctrl+C is bound to ``stop_or_copy``, which returns early to
        # ``stop_agent`` whenever the agent is busy. So during the one period a
        # user most wants to copy something -- a path or an error scrolling past
        # while a task runs -- the copy key aborts the task instead, and there is
        # no second binding that copies.
        async with karox_app(size=STANDARD) as (app, pilot):
            await _conversation(app, pilot)
            node, line, column = _node_holding(app, "ALPHA")
            app.screen.selections[node] = Selection(
                Offset(column, line), Offset(column + 16, line)
            )
            app.agent_busy = True

            await pilot.press("ctrl+c")
            await pilot.pause(0.2)

            self.assertIn("ALPHA", app.clipboard or "")
            self.assertFalse(
                app._stop_requested, "the copy key stopped the running task"
            )

    async def test_copying_the_last_answer_is_distinguishable_from_copying_a_selection(
        self,
    ) -> None:
        # UX-008: with no selection, ``action_stop_or_copy`` falls back to the last
        # assistant answer and reports the same "Скопировано" as a real selection
        # copy, so a user who selects a line, presses copy and is told it worked
        # cannot tell which of the two happened.
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
                "Скопировано выделение",
                "a fallback copy is announced exactly like a selection copy",
            )


if __name__ == "__main__":
    unittest.main()
