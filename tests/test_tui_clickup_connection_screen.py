"""Regression tests for ClickUp in the original Connection screen."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from _tui_harness import isolated_karox_directories

import karox.tui as tui


class ClickupConnectionScreenTests(unittest.IsolatedAsyncioTestCase):
    async def test_clickup_defaults_to_cloudflare_8766_in_original_screen(self) -> None:
        context = isolated_karox_directories()
        repository = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        app = tui.KaroXApp(repository, language="en")
        async with app.run_test(size=(130, 48)) as pilot:
            await pilot.pause(0.2)
            app.action_bridge()
            await pilot.pause(0.3)
            screen = next(
                item
                for item in app.screen_stack
                if isinstance(item, tui.BridgeSetupScreen)
            )
            screen.query_one("#profile-clickup", tui.RadioButton).value = True
            await pilot.pause(0.2)

            self.assertEqual(
                screen.query_one("#bridge-port", tui.Input).value,
                "8766",
            )
            self.assertTrue(
                screen.query_one("#tunnel-cloudflare", tui.RadioButton).value
            )
            note = str(screen.query_one("#bridge-profile-note", tui.Static).render())
            self.assertIn("separate terminal", note)

    async def test_clickup_parallel_launch_does_not_replace_live_chatgpt(self) -> None:
        context = isolated_karox_directories()
        repository = context.__enter__()
        self.addCleanup(context.__exit__, None, None, None)
        app = tui.KaroXApp(repository, language="en")

        class _LiveBridge:
            def __init__(self) -> None:
                self.terminated = False

            def poll(self):
                return None

            def terminate(self) -> None:
                self.terminated = True

        async with app.run_test(size=(130, 48)) as pilot:
            await pilot.pause(0.2)
            current = _LiveBridge()
            app.bridge_process = current
            setup = tui.BridgeSetup(
                profile="clickup",
                port=8766,
                tools=("karox.repo.read_file",),
                tunnel_provider="cloudflare",
            )
            with patch.object(app, "_launch_clickup_terminal") as launch:
                app._bridge_setup_done(setup)
                await pilot.pause(0.1)
                launch.assert_called_once_with(setup)

            self.assertIs(app.bridge_process, current)
            self.assertFalse(current.terminated)
            app.bridge_process = None


if __name__ == "__main__":
    unittest.main()
