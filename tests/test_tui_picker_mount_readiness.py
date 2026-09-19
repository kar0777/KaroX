"""The karox.tui ModelPickerScreen must always finish its mount.

The hosted runners hang here once: the mount finished via one fire-once
``call_after_refresh`` callback that Textual may drop, and a fast Enter then
rescheduled ``action_choose`` forever, so a worker hung for 45 minutes without a
single diagnostic. These tests pin the contract that replaced it: the picker
becomes ready even when its first render races composition, a fast Enter is
preserved, and a picker that never becomes ready fails loudly in bounded time.
"""

from __future__ import annotations

import asyncio
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

from _support import SRC  # noqa: F401
from _tui_harness import isolated_karox_directories

from karox import tui
from karox.tui import ModelPickerScreen


def _models() -> list[tui.DiscoveredModel]:
    return [
        tui.DiscoveredModel(model_id="alpha/one", context_window=8192),
        tui.DiscoveredModel(model_id="beta/two", context_window=16384),
    ]


class _FirstRenderRacesCompose:
    """Raise once inside ``_render_models``, then behave normally.

    That first call is the mount render, which is exactly what the loaded
    hosted runners raced: ``query_one("#model-options")`` ran while the
    composed children were still being mounted.
    """

    def __init__(self) -> None:
        self.calls = 0

    def patch(self) -> object:
        real_render = ModelPickerScreen._render_models
        state = self

        def flaky(screen: ModelPickerScreen) -> None:
            state.calls += 1
            if state.calls == 1:
                raise AssertionError("simulated compose race: #model-options missing")
            real_render(screen)

        return patch.object(ModelPickerScreen, "_render_models", flaky)


async def _wait_ready(app: object, screen: ModelPickerScreen) -> None:
    for _ in range(100):
        if screen._picker_ready:
            return
        await asyncio.sleep(0)
    raise AssertionError("picker never became ready")


class PickerMountReadinessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        isolated = isolated_karox_directories()
        self.repository = isolated.__enter__()
        self.addCleanup(isolated.__exit__, None, None, None)
        self.addCleanup(patch.object(tui, "_selected_model", return_value=None).start)
        self.addCleanup(patch.stopall)

    async def test_picker_becomes_ready_when_first_render_races_compose(self) -> None:
        """A compose race during the mount render still yields a usable picker."""

        race = _FirstRenderRacesCompose()
        result: list[object] = []
        with race.patch():
            app = tui.KaroXApp(Path(self.repository), language="en")
            async with app.run_test(size=(110, 34)) as pilot:
                screen = ModelPickerScreen(_models(), "en")
                app.push_screen(screen, result.append)
                await pilot.pause(0.05)
                await _wait_ready(app, screen)
                self.assertEqual(race.calls, 2)
                await pilot.press("enter")
                await pilot.pause(0.05)
        self.assertEqual(len(result), 1)
        self.assertEqual(getattr(result[0], "model_id", None), "alpha/one")

    async def test_fast_enter_before_mount_finishes_is_preserved(self) -> None:
        """Enter pressed while the mount is racing still chooses a model."""

        race = _FirstRenderRacesCompose()
        result: list[object] = []
        with race.patch():
            app = tui.KaroXApp(Path(self.repository), language="en")
            async with app.run_test(size=(110, 34)) as pilot:
                screen = ModelPickerScreen(_models(), "en")
                app.push_screen(screen, result.append)
                # Press while the first mount render has already failed and the
                # retry is still pending: the key must be deferred, not lost.
                await pilot.press("enter")
                await _wait_ready(app, screen)
                await pilot.pause(0.05)
        self.assertEqual(len(result), 1)
        self.assertEqual(getattr(result[0], "model_id", None), "alpha/one")


@pytest.mark.asyncio
async def test_deferred_mount_focus_never_steals_the_user_focus() -> None:
    """A deferred mount retry must not redirect the key the user just aimed.

    On a loaded runner the mount render can fail once and retry a moment
    later. If that deferred focus then overwrote the focus the user had
    already placed, their next key went to a different widget entirely —
    observed as Enter opening the wrong screen.
    """
    with isolated_karox_directories() as repository:
        app = tui.KaroXApp(Path(repository), language="en")
        async with app.run_test(size=(110, 34)) as pilot:
            screen = tui.LanguageScreen()
            real_query = screen.query_one.__func__
            attempts = {"n": 0}

            def lagging_query_one(self_screen, selector: str, expect_type: object = None):
                attempts["n"] += 1
                if attempts["n"] == 1:
                    raise tui.NoMatches(f"simulated compose race: {selector}")
                return real_query(self_screen, selector, expect_type)

            with patch.object(tui.LanguageScreen, "query_one", lagging_query_one):
                app.push_screen(screen)
                await pilot.pause(0.05)
                other = screen.query_one("#language-en", tui.Button)
                other.focus()
                await pilot.pause(0.05)
            # The deferred retry has now run; the user's focus must stand.
            assert app.focused is other


@pytest.mark.asyncio
async def test_never_ready_picker_fails_loudly_in_bounded_time() -> None:
    """A picker whose mount never completes raises instead of spinning forever.

    This is the exact hosted-runner hang: an unbounded ``call_after_refresh``
    reschedule of ``action_choose`` kept a worker alive for 45 silent minutes.
    """

    with isolated_karox_directories() as repository:
        with patch.object(ModelPickerScreen, "_finish_mount", lambda self: None):
            app = tui.KaroXApp(Path(repository), language="en")
            with pytest.raises(RuntimeError, match="did not finish mounting"):
                async with app.run_test(size=(110, 34)) as pilot:
                    app.push_screen(ModelPickerScreen(_models(), "en"))
                    await pilot.pause(0.05)
                    await pilot.press("enter")
                    for _ in range(200):
                        if app._exception is not None:
                            break
                        await asyncio.sleep(0)
