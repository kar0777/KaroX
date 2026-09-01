from __future__ import annotations

from unittest.mock import patch

import pytest

from _support import SRC  # noqa: F401
from _tui_harness import isolated_karox_directories

from karox import tui


def test_live_progress_adds_measured_counts_without_leaking_detail() -> None:
    reading = tui._activity_text(
        tui.ActivityAction(kind=tui.ACTIVITY_READING, files_read=7),
        english=False,
    )
    searching = tui._activity_text(
        tui.ActivityAction(kind=tui.ACTIVITY_SEARCHING, searches=3),
        english=True,
    )

    assert reading == "Просматриваю код · 7 файлов"
    assert searching == "Finding the relevant code · 3 searches"
    assert "repo." not in reading + searching
    assert "\\" not in reading + searching
