from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from textual.events import Paste

from _support import SRC  # noqa: F401
from _tui_harness import isolated_karox_directories
from karox import tui


pytestmark = pytest.mark.skipif(not tui._HAS_TEXTUAL, reason="textual is not installed")


def _large_text(size: int = 7500) -> str:
    return ("paste transport regression line\n" * 400)[:size]


@pytest.mark.asyncio
async def test_large_paste_targeted_at_input_collapses_losslessly() -> None:
    with isolated_karox_directories() as repository, patch.object(
        tui, "_selected_model", return_value=None
    ):
        app = tui.KaroXApp(repository, language="en")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", tui.CommandInput)
            composer.focus()
            text = _large_text()
            composer.post_message(Paste(text))
            await pilot.pause(0.2)

            assert composer.value.startswith("[paste #")
            saved = next(iter(app._pasted_blocks.values()))
            assert saved == text


@pytest.mark.asyncio
async def test_large_app_targeted_paste_works_after_a_question() -> None:
    with isolated_karox_directories() as repository, patch.object(
        tui, "_selected_model", return_value=None
    ):
        app = tui.KaroXApp(repository, language="ru")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app._submit_task("в какой папке ты работаешь?")
            await pilot.pause(0.1)
            composer = app.query_one("#composer", tui.CommandInput)
            composer.focus()
            text = _large_text()
            app.post_message(Paste(text))
            await pilot.pause(0.2)

            assert composer.has_focus
            assert not composer.disabled
            assert composer.value.startswith("[вставка #")
            saved = next(iter(app._pasted_blocks.values()))
            assert saved == text


@pytest.mark.asyncio
async def test_slash_paste_keeps_focus_through_real_submit_tail() -> None:
    with isolated_karox_directories() as repository, patch.object(
        tui, "_selected_model", return_value=None
    ):
        app = tui.KaroXApp(repository, language="en")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", tui.CommandInput)
            composer.focus()
            composer.value = "/paste"
            text = _large_text()
            with patch("karox.clipboard.read_text", return_value=text):
                await pilot.press("enter")
                await pilot.pause(0.2)

            assert composer.has_focus
            assert composer.value.startswith("[paste #")
            assert next(iter(app._pasted_blocks.values())) == text


@pytest.mark.asyncio
async def test_f8_large_paste_keeps_composer_focus() -> None:
    with isolated_karox_directories() as repository, patch.object(
        tui, "_selected_model", return_value=None
    ):
        app = tui.KaroXApp(repository, language="ru")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", tui.CommandInput)
            composer.focus()
            text = _large_text()
            with patch("karox.clipboard.read_text", return_value=text):
                await pilot.press("f8")
                await pilot.pause(0.2)

            assert composer.has_focus
            assert composer.value.startswith("[вставка #")
            assert next(iter(app._pasted_blocks.values())) == text


@pytest.mark.asyncio
async def test_terminal_confirmation_focus_loss_does_not_drop_app_paste() -> None:
    """Windows Terminal may blur the composer while its >5 KiB dialog is open."""
    with isolated_karox_directories() as repository, patch.object(
        tui, "_selected_model", return_value=None
    ):
        app = tui.KaroXApp(repository, language="ru")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            text = _large_text()
            fake_composer = SimpleNamespace(screen=app.screen, has_focus=False)
            event = Paste(text)

            with (
                patch.object(app, "query_one", return_value=fake_composer),
                patch.object(app, "_accept_composer_paste", return_value=True) as accept,
                patch.object(app, "_restore_composer_focus_after_paste") as restore,
            ):
                app.on_paste(event)

            accept.assert_called_once_with(fake_composer, text)
            restore.assert_called_once_with(fake_composer)


@pytest.mark.asyncio
async def test_backspace_removes_large_paste_as_one_editor_element() -> None:
    with isolated_karox_directories() as repository, patch.object(
        tui, "_selected_model", return_value=None
    ):
        app = tui.KaroXApp(repository, language="en")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", tui.CommandInput)
            composer.focus()
            text = _large_text()
            assert app._accept_composer_paste(composer, text)
            await pilot.pause()
            marker = composer.value
            assert marker.startswith("[paste #")
            assert app._pasted_blocks

            composer.cursor_position = len(marker)
            await pilot.press("backspace")
            await pilot.pause()

            assert composer.value == ""
            assert not app._pasted_blocks


@pytest.mark.asyncio
async def test_space_after_confirmed_large_paste_appends_instead_of_replacing_marker() -> None:
    """A focus restore must not leave the paste placeholder selected."""
    with isolated_karox_directories() as repository, patch.object(
        tui, "_selected_model", return_value=None
    ):
        app = tui.KaroXApp(repository, language="ru")
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer", tui.CommandInput)
            composer.focus()
            text = _large_text()
            assert app._accept_composer_paste(composer, text)
            marker = composer.value

            # Reproduce the stale selection Windows Terminal/Textual can restore
            # after the large-paste confirmation dialog closes.
            composer.selection = tui.Selection(0, len(marker))
            app._restore_composer_focus_after_paste(composer)
            await pilot.pause(0.1)

            assert composer.selection.is_empty
            assert composer.cursor_position == len(marker)
            await pilot.press("space")
            await pilot.pause()

            assert composer.value == marker + " "
            assert len(app._pasted_blocks) == 1
