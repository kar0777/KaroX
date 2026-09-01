from __future__ import annotations

import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from _support import SRC  # noqa: F401

from karox import tui


pytestmark = pytest.mark.skipif(not tui._HAS_TEXTUAL, reason="textual is not installed")


@pytest.mark.asyncio
async def test_workspace_switch_confirmation_is_compact_not_success_cards() -> None:
    with tempfile.TemporaryDirectory() as root:
        base = Path(root)
        current = base / "current"
        target = base / "target"
        current.mkdir()
        target.mkdir()
        preferences = base / "config" / "ui.json"

        with (
            patch.object(tui, "_selected_model", return_value=None),
            patch.object(tui, "_preferences_path", return_value=preferences),
        ):
            app = tui.KaroXApp(current, language="ru")
            async with app.run_test(size=(120, 36)) as pilot:
                await pilot.pause()
                app._transcript().clear()
                with patch.object(app, "_workspace_saved_profiles", return_value=(object(),)):
                    assert app._switch_workspace(str(target)) is True
                await pilot.pause()

                lines = list(app.query(".message-line"))
                success_cards = list(app.query(".notice-success"))
                text = app._transcript().plain_text

                assert len(lines) == 2
                assert not success_cards
                assert f"Папка → {target.resolve()}" in text
                assert "Подключения сохранены" in text


@pytest.mark.asyncio
async def test_benign_completion_disappears_but_coding_summary_remains() -> None:
    with patch.object(tui, "_selected_model", return_value=None):
        app = tui.KaroXApp(Path.cwd(), language="ru")
        async with app.run_test(size=(120, 36)) as pilot:
            await pilot.pause()

            app._begin_step("read", "repo.read_file")
            app._finish_activity(tui.ACTIVITY_COMPLETED, "no_changes")
            await pilot.pause()
            activity = app.query_one("#activity", tui.Static)
            assert str(activity.styles.display) == "none"

            app._reset_activity()
            app._begin_step("edit", "repo.edit_file")
            app._finish_activity(tui.ACTIVITY_COMPLETED, "", files=("a.py", "b.py"))
            await pilot.pause()
            assert str(activity.styles.display) == "block"
            assert "2 файла" in str(activity.render())
