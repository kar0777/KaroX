"""A skipped browser probe must not poison subsequent async tests."""
from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import patch


class BrowserProbeCleanupTests(unittest.TestCase):
    @staticmethod
    def probe() -> None:
        # Import locally so pytest does not collect the fixture class twice.
        from test_windows_execution_runtime import BrowserRuntimeFixtureTests
        BrowserRuntimeFixtureTests.setUpClass()

    def test_failed_launch_always_exits_the_driver_context(self) -> None:
        with patch.dict(os.environ, {"KAROX_REQUIRE_BROWSER_TESTS": "0"}):
            with patch("playwright.sync_api.sync_playwright") as factory:
                manager = factory.return_value
                context = manager.__enter__.return_value
                context.chromium.launch.side_effect = RuntimeError("launch sentinel")
                with self.assertRaisesRegex(unittest.SkipTest, "launch sentinel"):
                    self.probe()
                manager.__exit__.assert_called_once()
                self.assertIs(manager.__exit__.call_args.args[0], RuntimeError)

    def test_success_closes_browser_and_driver(self) -> None:
        with patch("playwright.sync_api.sync_playwright") as factory:
            manager = factory.return_value
            browser = manager.__enter__.return_value.chromium.launch.return_value
            self.probe()
            browser.close.assert_called_once_with()
            manager.__exit__.assert_called_once_with(None, None, None)

    def test_required_browser_failure_cannot_turn_into_a_skip(self) -> None:
        with patch.dict(os.environ, {"KAROX_REQUIRE_BROWSER_TESTS": "1"}):
            with patch("playwright.sync_api.sync_playwright") as factory:
                manager = factory.return_value
                manager.__enter__.return_value.chromium.launch.side_effect = RuntimeError("required sentinel")
                with self.assertRaisesRegex(RuntimeError, "required Chromium fixture unavailable"):
                    self.probe()
                manager.__exit__.assert_called_once()

    def test_real_driver_launch_failure_leaves_asyncio_usable(self) -> None:
        # Starting/stopping the bundled driver requires no installed browser and
        # makes this an actual event-loop lifecycle regression, not just a mock.
        with patch.dict(os.environ, {"KAROX_REQUIRE_BROWSER_TESTS": "0"}):
            with patch("playwright.sync_api.BrowserType.launch", side_effect=RuntimeError("synthetic browser absence")):
                with self.assertRaisesRegex(unittest.SkipTest, "synthetic browser absence"):
                    self.probe()

        async def next_async_test() -> str:
            await asyncio.sleep(0)
            return "clean loop"

        self.assertEqual(asyncio.run(next_async_test()), "clean loop")
