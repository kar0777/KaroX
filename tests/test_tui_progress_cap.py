from __future__ import annotations

from unittest.mock import patch

import pytest

from _support import SRC  # noqa: F401
from _tui_harness import isolated_karox_directories

from karox import tui


@pytest.mark.asyncio
async def test_model_progress_is_bounded_to_three_lines_and_updates_latest() -> None:
    with isolated_karox_directories() as repository:
        with patch.object(tui, "_selected_model", return_value=None):
            app = tui.KaroXApp(repository, language="en")
            async with app.run_test(size=(120, 36)) as pilot:
                await pilot.pause()
                transcript = app._transcript()
                transcript.clear()
                app._reset_activity()

                app._write_progress("Inspecting the project structure.")
                app._write_progress("Checking the relevant implementation.")
                app._write_progress("Comparing the behavior with tests.")
                app._write_progress("Preparing the smallest safe change.")
                app._write_progress("Verifying the change now.")
                await pilot.pause()

                progress = list(app.query(".message-progress"))
                assert len(progress) == 3
                assert "Inspecting the project structure" in progress[0].plain_text
                assert "Checking the relevant implementation" in progress[1].plain_text
                assert "Verifying the change now" in progress[2].plain_text
                assert "Preparing the smallest safe change" not in transcript.plain_text
