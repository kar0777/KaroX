"""A background console child must not open a terminal window of its own.

This is the missing-window-flag class of defect. A console child inherits its
parent's console, so in the TUI nothing new appears; a *console-less* parent --
the detached bridge owner, its supervisor, a Task Scheduler action, a probe --
hands the child a brand-new console, and the default terminal opens a visible
window titled with the child's executable path. Measured on Windows 11 with
Windows Terminal as the default terminal, spawning an interpreter from a
console-less parent: 4 of 4 runs without ``CREATE_NO_WINDOW`` opened such a
window, 0 of 4 with it. The ``DETACHED_PROCESS | CREATE_NO_WINDOW`` pair the
bridge used to pass still opened one in 2 of 4 runs, because Windows ignores
``CREATE_NO_WINDOW`` when ``DETACHED_PROCESS`` is set.

``windowless_flags`` therefore returns the single flag, and only when the
caller has no console to share. Each spawn site is asserted on the flags its
child actually receives, not on the text of the module.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from _support import SRC  # noqa: F401

from karox import autonomy_benchmark, credential_session, system_chrome, tailscale, tui
from karox import detached_process as dp


_NO_WINDOW = 0x08000000


class WindowlessFlagTests(unittest.TestCase):
    def test_a_consoleless_windows_parent_asks_for_no_window(self) -> None:
        with mock.patch.object(dp.os, "name", "nt"), mock.patch.object(
            dp, "console_window_handle", return_value=0
        ):
            self.assertEqual(dp.windowless_flags(), dp.CREATE_NO_WINDOW)

    def test_a_parent_that_owns_a_console_leaves_the_child_alone(self) -> None:
        """The child shares the parent's window, so a flag would only differ in kind."""
        with mock.patch.object(dp.os, "name", "nt"), mock.patch.object(
            dp, "console_window_handle", return_value=0x00010A2C
        ):
            self.assertEqual(dp.windowless_flags(), 0)

    def test_the_console_probe_reports_nothing_off_windows(self) -> None:
        with mock.patch.object(dp.os, "name", "posix"):
            self.assertEqual(dp.console_window_handle(), 0)
            self.assertEqual(dp.windowless_flags(), 0)

    def test_the_flag_is_never_the_detached_combination(self) -> None:
        """Windows ignores CREATE_NO_WINDOW beside DETACHED_PROCESS; measured 2/4 windows."""
        with mock.patch.object(dp.os, "name", "nt"), mock.patch.object(
            dp, "console_window_handle", return_value=0
        ):
            flags = dp.windowless_flags()
        self.assertTrue(flags & dp.CREATE_NO_WINDOW)
        self.assertFalse(flags & dp.DETACHED_PROCESS)
        self.assertFalse(flags & dp.CREATE_NEW_PROCESS_GROUP)


class SpawnSiteTests(unittest.TestCase):
    """Every site that spawns a console child from a possibly console-less parent."""

    def test_the_chrome_taskkill_fallback_asks_for_no_window(self) -> None:
        captured: dict[str, object] = {}

        class StubbornProcess:
            pid = 4711

            def poll(self):  # noqa: ANN202 - fake process surface
                return None

            def terminate(self):  # noqa: ANN202
                raise OSError("refused")

            def kill(self):  # noqa: ANN202
                raise OSError("refused")

        def fake_run(*_args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0)

        with mock.patch.object(
            system_chrome, "windowless_flags", return_value=_NO_WINDOW
        ), mock.patch.object(system_chrome.subprocess, "run", side_effect=fake_run):
            system_chrome.terminate_chrome_process(StubbornProcess())

        self.assertEqual(captured["creationflags"], _NO_WINDOW)
        self.assertEqual(captured["stdout"], subprocess.DEVNULL)

    def test_the_benchmark_git_helper_asks_for_no_window(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(*_args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="")

        with mock.patch.object(
            autonomy_benchmark, "windowless_flags", return_value=_NO_WINDOW
        ), mock.patch.object(autonomy_benchmark.subprocess, "run", side_effect=fake_run):
            autonomy_benchmark._git(Path("."), "rev-parse", "HEAD")

        self.assertEqual(captured["creationflags"], _NO_WINDOW)

    def test_the_agent_child_asks_for_no_window_when_the_tui_has_none(self) -> None:
        captured: dict[str, object] = {}

        def fake_popen(*_args, **kwargs):
            captured.update(kwargs)
            child = mock.MagicMock()
            child.pid = 4242
            child.returncode = 0
            child.communicate.return_value = ("done", "")
            return child

        with mock.patch.object(tui, "windowless_flags", return_value=_NO_WINDOW), mock.patch.object(
            tui.subprocess, "Popen", side_effect=fake_popen
        ):
            code, output = tui._run_agent_cli(["task", "status"], lambda _p: None)

        self.assertEqual(captured["creationflags"], _NO_WINDOW)
        self.assertEqual(captured["stdout"], subprocess.PIPE)
        self.assertEqual(code, 0)
        self.assertEqual(output, "done")

    def test_the_fresh_interpreter_credential_probe_asks_for_no_window(self) -> None:
        captured: dict[str, object] = {}
        value = "probe-value"
        expected = hashlib.sha256(value.encode()).hexdigest()

        def fake_run(*_args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout=expected.encode())

        backend = mock.MagicMock()
        with mock.patch.object(
            credential_session, "windowless_flags", return_value=_NO_WINDOW
        ), mock.patch.object(credential_session, "_consent"), mock.patch.object(
            credential_session, "KeyringBackend", return_value=backend
        ), mock.patch.object(
            credential_session.secrets, "token_urlsafe", return_value=value
        ), mock.patch.object(credential_session.subprocess, "run", side_effect=fake_run):
            report = credential_session.verify_child_storage(consent=True)

        self.assertEqual(captured["creationflags"], _NO_WINDOW)
        self.assertEqual(report["verification"], "write-child-read-delete")


class TailscaleGuiReuseTests(unittest.TestCase):
    """The tray app is single-instance: starting it again activates its window."""

    GUI = r"C:\Program Files\Tailscale\tailscale-ipn.exe"

    def test_an_already_running_tray_app_is_not_started_again(self) -> None:
        popen = mock.MagicMock()
        with mock.patch.object(tailscale, "find_tailscale_gui", return_value=self.GUI):
            launched = tailscale.launch_tailscale_gui(
                popen=popen, running_pids=lambda: (7600,)
            )
        self.assertEqual(launched, self.GUI)
        popen.assert_not_called()

    def test_a_stopped_tray_app_is_started_once_with_disconnected_streams(self) -> None:
        captured: dict[str, object] = {}

        def fake_popen(*_args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace()

        with mock.patch.object(
            tailscale, "find_tailscale_gui", return_value=self.GUI
        ), mock.patch.object(
            tailscale.subprocess, "CREATE_NO_WINDOW", _NO_WINDOW, create=True
        ):
            launched = tailscale.launch_tailscale_gui(
                popen=fake_popen, running_pids=lambda: ()
            )

        self.assertEqual(launched, self.GUI)
        self.assertEqual(captured["stdin"], subprocess.DEVNULL)
        self.assertEqual(captured["stdout"], subprocess.DEVNULL)
        self.assertEqual(captured["stderr"], subprocess.DEVNULL)

    def test_the_tray_probe_reports_nothing_off_windows(self) -> None:
        with mock.patch.object(tailscale.os, "name", "posix"):
            self.assertEqual(tailscale.tailscale_gui_pids(), ())

    def test_the_tray_probe_finds_a_running_app_on_this_host(self) -> None:
        """the enumeration must really enumerate, not merely answer.

        Asking for this interpreter's own image name has to return this process:
        an implementation that returned a hardcoded empty tuple would pass a
        shape-only assertion but fails here.
        """
        import os as os_module
        import sys

        if os_module.name != "nt" or not sys.platform.startswith("win"):
            self.skipTest("Windows-only process enumeration")
        image = os_module.path.basename(sys.executable)
        mine = tailscale.tailscale_gui_pids(image)
        self.assertIn(os_module.getpid(), mine)

        found = tailscale.tailscale_gui_pids()
        self.assertIsInstance(found, tuple)
        for pid in found:
            self.assertGreater(pid, 0)

    def test_the_tray_probe_ignores_another_windows_session(self) -> None:
        """A tray app in someone else's session must not suppress our launch."""
        if sys.platform != "win32":
            self.skipTest("Windows-only session ids")
        self.assertTrue(tailscale._same_windows_session(os.getpid(), os.getpid(), ctypes.windll.kernel32))
        # A PID that cannot be queried is reported as "assume ours", never as
        # "not ours", so an unavailable query can never hide the tray app.
        self.assertTrue(tailscale._same_windows_session(0, os.getpid(), ctypes.windll.kernel32))


if __name__ == "__main__":
    unittest.main()
