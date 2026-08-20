"""Windows utility subprocesses used by the TUI must never flash a console."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox import clipboard, extension_browser, tui


@unittest.skipUnless(os.name == "nt", "Windows console-window regression")
class WindowsBackgroundConsoleTests(unittest.TestCase):
    def test_chrome_profile_probe_uses_create_no_window(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            extension_browser.subprocess, "run", return_value=completed
        ) as run:
            extension_browser._profile_chrome_process_ids(Path(tmp))
        self.assertEqual(
            run.call_args.kwargs["creationflags"],
            subprocess.CREATE_NO_WINDOW,
        )

    def test_clipboard_powershell_uses_create_no_window(self) -> None:
        completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=b"", stderr=b"")
        with patch.object(clipboard.subprocess, "run", return_value=completed) as run:
            self.assertTrue(clipboard.write_text("not-a-secret"))
        self.assertEqual(
            run.call_args.kwargs["creationflags"],
            subprocess.CREATE_NO_WINDOW,
        )

    def test_notion_parallel_bridge_uses_no_window_instead_of_new_console(self) -> None:
        # A parallel Notion bridge starts through the saved-profile launcher:
        # the TUI persists the profile in-process (no console command at all)
        # and the detached spawn must never open a console window of its own.
        from karox import detached_process as dp
        from karox.web_bridge_profiles import WebBridgeProfileError

        with patch("karox.web_bridge_profiles.WebBridgeProfileStore") as store:
            store.return_value.get.side_effect = WebBridgeProfileError(
                "saved bridge profile does not exist: test"
            )
            name = tui._persist_tui_saved_bridge_profile(
                Path.cwd(),
                tui.BridgeSetup(
                    profile="notion",
                    port=8767,
                    tools=("karox.repo.read_file",),
                    tunnel_provider="tailscale",
                ),
                language="en",
            )
        saved = store.return_value.put.call_args.args[0]
        self.assertEqual(saved.tunnel, "tailscale")

        with patch.object(dp.subprocess, "Popen") as popen:
            process, mechanism = dp.spawn_detached(
                ["python", "-m", "karox.cli", "bridge", "connect", "--saved", name],
            )
        self.assertIsNotNone(process)
        self.assertEqual(mechanism, "breakaway")
        flags = popen.call_args.kwargs["creationflags"]
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(flags & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertFalse(flags & subprocess.CREATE_NEW_CONSOLE)
        self.assertFalse(hasattr(tui.KaroXApp, "_launch_notion_terminal"))

    def test_hyperagent_parallel_bridge_uses_its_own_process_and_port(self) -> None:
        # Hyperagent runs beside other bridges as its own saved profile: its
        # own canonical name and port, launched as its own detached
        # `bridge connect --saved` process with no console window.
        from karox import detached_process as dp
        from karox.web_bridge_profiles import WebBridgeProfileError

        repository = Path.cwd()
        with patch("karox.web_bridge_profiles.WebBridgeProfileStore") as store:
            store.return_value.get.side_effect = WebBridgeProfileError(
                "saved bridge profile does not exist: test"
            )
            name = tui._persist_tui_saved_bridge_profile(
                repository,
                tui.BridgeSetup(
                    profile="hyperagent-web",
                    port=8768,
                    tools=("karox.repo.read_file", "karox.repo.write_file"),
                    tunnel_provider="tailscale",
                ),
                language="en",
            )
        saved = store.return_value.put.call_args.args[0]
        self.assertTrue(name.startswith("hyperagent-auto-"))
        self.assertEqual(saved.target_profile, "hyperagent-web")
        self.assertEqual(saved.port, 8768)
        self.assertEqual(saved.repository, str(repository.resolve()))

        with patch.object(dp.subprocess, "Popen") as popen:
            process, _mechanism = dp.spawn_detached(
                ["python", "-m", "karox.cli", "bridge", "connect", "--saved", name],
            )
        self.assertIsNotNone(process)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[argv.index("connect") + 1], "--saved")
        self.assertEqual(argv[argv.index("--saved") + 1], name)
        flags = popen.call_args.kwargs["creationflags"]
        self.assertTrue(flags & subprocess.CREATE_NO_WINDOW)
        self.assertTrue(flags & subprocess.CREATE_BREAKAWAY_FROM_JOB)
        self.assertFalse(flags & subprocess.CREATE_NEW_CONSOLE)
        self.assertFalse(hasattr(tui.KaroXApp, "_launch_hyperagent_terminal"))


if __name__ == "__main__":
    unittest.main()
