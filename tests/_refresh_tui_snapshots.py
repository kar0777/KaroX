"""Explicit snapshot refresher; not collected by normal pytest discovery."""

from __future__ import annotations

import os
import unittest

from _support import SRC  # noqa: F401
from _tui_harness import (
    NARROW,
    STANDARD,
    WIDE,
    UPDATE_VARIABLE,
    assert_snapshot,
    karox_app,
    screen_lines,
)

os.environ[UPDATE_VARIABLE] = "1"


class SnapshotRefresh(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_core_chat_snapshots(self) -> None:
        for name, size in (
            ("chat-ready-wide", WIDE),
            ("chat-ready-standard", STANDARD),
            ("chat-ready-narrow", NARROW),
        ):
            async with karox_app(size=size) as (app, _pilot):
                assert_snapshot(self, name, screen_lines(app))

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

        async with karox_app(size=STANDARD) as (app, pilot):
            await pilot.press("/")
            await pilot.pause(0.2)
            assert_snapshot(self, "command-menu-standard", screen_lines(app))
