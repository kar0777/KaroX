"""What the KaroX terminal actually draws, at three window sizes.

Two kinds of test live here.

The snapshots pin the rendered screen so that a refactor which was supposed to
change nothing can prove it, and one that changed something has to say what. They
are not assertions about quality -- a snapshot records a defect as faithfully as
it records a fix, which is deliberate: the narrow-window snapshot currently shows
a conversation area two rows tall.

The rest are assertions about layout guarantees the product makes. Several are
marked ``expectedFailure`` with a bug identifier from
``docs/UX_BUG_INVENTORY.md``. That is not a way to ignore them: unittest reports
an unexpected success as a failure, so when the defect is fixed the run goes red
until the decorator is removed. A bug recorded this way cannot be silently
forgotten, and cannot be silently reintroduced afterwards.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from _tui_harness import (  # noqa: F401 - inserts src on sys.path via _support
    NARROW,
    STANDARD,
    WIDE,
    assert_snapshot,
    karox_app,
    region_lines,
    screen_lines,
    visible_text,
)

import karox.tui as tui


@unittest.skipUnless(tui._HAS_TEXTUAL, "textual is not installed")
class ChatScreenSnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_chat_screen_at_a_wide_window(self) -> None:
        async with karox_app(size=WIDE) as (app, _pilot):
            assert_snapshot(self, "chat-ready-wide", screen_lines(app))

    async def test_ready_chat_screen_at_a_standard_window(self) -> None:
        async with karox_app(size=STANDARD) as (app, _pilot):
            assert_snapshot(self, "chat-ready-standard", screen_lines(app))

    async def test_ready_chat_screen_at_a_narrow_window(self) -> None:
        async with karox_app(size=NARROW) as (app, _pilot):
            assert_snapshot(self, "chat-ready-narrow", screen_lines(app))

    async def test_a_long_answer_at_a_standard_window(self) -> None:
        async with karox_app(size=STANDARD) as (app, pilot):
            app._write_user("почему проверка не прошла")
            app._write_assistant(
                "Проверка не прошла на трёх тестах в `tests/test_core.py`. "
                "Причина одна: `_bounded_stream` декодировал вывод дочернего "
                "процесса как UTF-8 с `errors=\"ignore\"`, поэтому кириллица "
                "не искажалась, а удалялась.\n\n"
                "- строка 1301: чтение без усечения\n"
                "- строка 1324: голова усечённого потока\n"
                "- строка 1325: хвост усечённого потока\n"
            )
            await pilot.pause(0.2)
            assert_snapshot(self, "answer-long-standard", screen_lines(app))

    async def test_the_command_menu_at_a_standard_window(self) -> None:
        async with karox_app(size=STANDARD) as (app, pilot):
            await pilot.press("/")
            await pilot.pause(0.2)
            assert_snapshot(self, "command-menu-standard", screen_lines(app))


@unittest.skipUnless(tui._HAS_TEXTUAL, "textual is not installed")
class ConversationAreaShareTests(unittest.IsolatedAsyncioTestCase):
    """The chrome must not crowd out the conversation.

    A comment in the application's own stylesheet records that fixed chrome once
    came to seventeen rows before a word of conversation, and one existing test
    pins the result at a 24-row window. Nothing pinned it below that, which is
    where the guarantee actually stops holding.
    """

    async def test_a_standard_window_gives_the_chat_at_least_half(self) -> None:
        async with karox_app(size=STANDARD) as (app, _pilot):
            log = app.query_one("#conversation", tui.TranscriptView)
            self.assertGreaterEqual(
                log.size.height,
                STANDARD[1] // 2,
                f"the chat has {log.size.height} of {STANDARD[1]} rows",
            )

    async def test_a_narrow_window_gives_the_chat_a_usable_share(self) -> None:
        # UX-010: at 46x14 with the product's default settings the conversation
        # area is two rows tall -- 14% of the window -- because the brand, ticker,
        # status bar, separators and composer take a fixed twelve rows regardless
        # of how many rows there are to divide. Hiding the sponsor line below
        # twenty rows recovered one of them -- the chat now gets three, measured --
        # which is enough for the welcome to be readable but still not a share
        # anyone would call usable. The remaining rows are in the status bar and
        # the composer, and reclaiming them is a layout change rather than a
        # visibility one.
        #
        # sponsors=True is the point of the test rather than a detail: the ticker
        # is on by default and costs exactly the row that makes the difference.
        # With it off the chat gets four rows and this passes.
        async with karox_app(size=NARROW, sponsors=True) as (app, _pilot):
            log = app.query_one("#conversation", tui.TranscriptView)
            self.assertGreaterEqual(
                log.size.height,
                4,
                f"the chat has {log.size.height} of {NARROW[1]} rows",
            )

    async def test_the_welcome_is_fully_visible_when_it_first_appears(self) -> None:
        # UX-011: whatever the window size, the message shown before the user has
        # typed anything must be readable in full. At 46x14 with default settings
        # the conversation area holds two rows of a three-line welcome and has
        # already scrolled to the bottom, so "KaroX готов." -- the line that says
        # the product is working at all -- is the one line the user never sees,
        # with no scrollbar or marker to suggest anything is above.
        async with karox_app(size=NARROW, sponsors=True) as (app, _pilot):
            self.assertIn("KaroX готов", visible_text(app))

    async def test_command_palette_keeps_its_composer_visible_and_actionable(self) -> None:
        """A short terminal must not hide the input behind command suggestions."""

        for size in (NARROW, STANDARD):
            async with karox_app(size=size) as (app, pilot):
                await pilot.press("/")
                await pilot.pause()
                menu = app.query_one("#command-menu", tui.Static)
                composer = app.query_one("#composer", tui.CommandInput)
                self.assertTrue(app._command_menu_open)
                self.assertLessEqual(
                    menu.region.bottom,
                    composer.region.y,
                    f"menu obscures its composer at {size}: {menu.region} / {composer.region}",
                )
                self.assertTrue(composer.has_focus)

                # The window can page the visible rows, but selection and Tab
                # still operate on the complete list rather than only the rows
                # currently painted in the narrow terminal.
                await pilot.press("down")
                await pilot.press("tab")
                await pilot.pause()
                self.assertEqual(composer.value, "/effort")
                self.assertTrue(composer.has_focus)

    async def test_a_long_command_catalogue_always_paints_the_selection(self) -> None:
        """A window that outgrows its own border hides the highlighted row.

        ``#command-menu`` is ``max-height: 14`` laid out border-box, so only
        twelve content rows can ever be painted. A window computed in content
        rows could ask for fourteen and place the selection in the two that are
        clipped -- the menu then shows other commands while Enter acts on one
        the user cannot see.
        """

        catalogue = {
            f"/my-{index:02d}": f"user command · demo {index}" for index in range(1, 30)
        }
        for size in (NARROW, STANDARD, WIDE):
            with patch.object(tui, "_discoverable_commands", return_value=dict(catalogue)):
                async with karox_app(size=size) as (app, pilot):
                    await pilot.pause()
                    app._update_command_menu("/my")
                    await pilot.pause()
                    menu = app.query_one("#command-menu", tui.Static)
                    self.assertGreater(
                        len(app._filtered_commands),
                        menu.content_size.height,
                        f"the fixture no longer fills the menu at {size}",
                    )
                    for index in (0, len(app._filtered_commands) - 1):
                        app._command_index = index
                        app._update_command_menu("/my")
                        await pilot.pause()
                        painted = region_lines(app, menu)
                        self.assertLessEqual(
                            len(painted),
                            menu.region.height,
                            f"the menu paints outside its own region at {size}",
                        )
                        self.assertTrue(
                            any(">" in line for line in painted),
                            f"the selected command is painted nowhere at {size} "
                            f"(row {index} of {len(app._filtered_commands)})",
                        )


@unittest.skipUnless(tui._HAS_TEXTUAL, "textual is not installed")
class StatusBarLayoutTests(unittest.IsolatedAsyncioTestCase):
    """Five status columns of equal width, for values of very unequal length."""

    async def test_status_columns_do_not_run_into_each_other(self) -> None:
        # UX-012: the status bar is a Horizontal of five Statics at width: 1fr, so
        # at 80 columns each gets 15 cells and there is no gutter between them.
        # "контекст: лимит" is exactly 15 characters wide, so it fills its column
        # and the next one starts immediately: the row reads
        # "контекст: лимитмост: выключен", which looks like a rendering fault
        # rather than two fields.
        async with karox_app(size=STANDARD) as (app, _pilot):
            rendered = "\n".join(region_lines(app, app.query_one("#header-status")))
            self.assertNotRegex(
                rendered,
                r"\S(мост|bridge):",
                f"status columns have no gutter:\n{rendered}",
            )

    async def test_a_truncated_status_value_says_it_was_truncated(self) -> None:
        # UX-013: at 46 columns each status column gets 8 cells, so
        # "openai/model-a" is drawn as "openai/m" and the repository name breaks
        # across two rows mid-word ("репозито" / "рий:"). A value cut without any
        # marker reads as a different value, and a user checking which model is
        # selected is exactly the person who cannot afford that.
        async with karox_app(size=NARROW) as (app, _pilot):
            rendered = "\n".join(region_lines(app, app.query_one("#header-status")))
            self.assertNotIn(
                "openai/m",
                rendered.replace("openai/model-a", ""),
                f"model identity silently truncated:\n{rendered}",
            )


if __name__ == "__main__":
    unittest.main()
