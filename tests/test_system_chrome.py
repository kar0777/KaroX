"""Focused tests for the dedicated headed system-Chrome launcher."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from karox.system_chrome import launch_system_chrome


class SystemChromeLauncherTests(unittest.TestCase):
    def test_headed_launch_forces_new_on_screen_window(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / "chrome.exe"
            executable.write_bytes(b"")
            profile = root / "profile"
            extension = root / "extension"
            profile.mkdir()
            extension.mkdir()

            process = mock.Mock()
            process.poll.return_value = None
            context = mock.Mock()
            browser = mock.Mock()
            browser.contexts = [context]
            playwright_ctx = mock.Mock()
            playwright_ctx.chromium.connect_over_cdp.return_value = browser

            with mock.patch(
                "karox.system_chrome.find_system_chrome", return_value=executable
            ), mock.patch(
                "karox.system_chrome.chrome_profile_dir", return_value=profile
            ), mock.patch(
                "karox.system_chrome.write_karo_extension", return_value=extension
            ), mock.patch(
                "karox.system_chrome.runtime_dir", return_value=root / "runtime"
            ), mock.patch(
                "karox.system_chrome.subprocess.Popen", return_value=process
            ) as popen, mock.patch(
                "karox.system_chrome._wait_for_devtools_port", return_value=9222
            ):
                launch = launch_system_chrome(
                    playwright_ctx,
                    proxy_server="http://127.0.0.1:9999",
                    proxy_username="user",
                    proxy_password="password",
                    width=1440,
                    height=900,
                )

            args = popen.call_args.args[0]
            self.assertIn("--new-window", args)
            self.assertIn("--window-position=40,40", args)
            self.assertIn("--window-size=1440,900", args)
            self.assertIs(launch.process, process)
            self.assertIs(launch.browser, browser)
            self.assertIs(launch.context, context)


if __name__ == "__main__":
    unittest.main()
