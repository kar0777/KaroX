"""Behavioral coverage for background probes that must not open Windows consoles."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase, mock

from karox import (
    browser_bootstrap,
    check_jobs,
    ellipsis_checkpoint,
    map_service,
    orchestration_native,
    quickstart,
    tailscale,
)


_NO_WINDOW = 0x08000000


class BackgroundProbeFlagTests(TestCase):
    def _windows_module(self, module):
        # Replacing the module binding avoids changing global ``os.name``: that
        # would make pathlib attempt to construct WindowsPath on this test host.
        return mock.patch.object(module, "os", SimpleNamespace(name="nt", environ={}))

    def _no_window(self, module):
        return mock.patch.object(module.subprocess, "CREATE_NO_WINDOW", _NO_WINDOW, create=True)

    def _assert_background_launch(self, captured: dict[str, object], module) -> None:
        self.assertEqual(captured["creationflags"], _NO_WINDOW)
        self.assertIs(captured["stdin"], module.subprocess.DEVNULL)

    def test_git_probes_use_windows_no_window_and_devnull_without_launching_git(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            captured: dict[str, object] = {}

            def fake_quickstart_run(*_args, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(returncode=0, stdout=str(root) + "\n")

            with self._windows_module(quickstart), self._no_window(quickstart):
                self.assertEqual(quickstart.git_toplevel(root, run=fake_quickstart_run), root)
            self._assert_background_launch(captured, quickstart)

            def fake_check_jobs_run(*_args, **kwargs):
                captured.clear()
                captured.update(kwargs)
                return SimpleNamespace(returncode=0, stdout="HEAD\n")

            with self._windows_module(check_jobs), self._no_window(check_jobs), mock.patch.object(
                check_jobs.subprocess, "run", side_effect=fake_check_jobs_run
            ):
                check_jobs._workspace_state_fingerprint(root)
            self._assert_background_launch(captured, check_jobs)

            service = map_service.MapService.__new__(map_service.MapService)
            service.repository = root
            with self._windows_module(map_service), self._no_window(map_service), mock.patch.object(
                map_service.subprocess, "run", side_effect=fake_check_jobs_run
            ):
                self.assertEqual(service._git("status", "--porcelain"), "HEAD")
            self._assert_background_launch(captured, map_service)

            common_dir = root / ".git"
            common_dir.mkdir()

            def fake_orchestration_run(*_args, **kwargs):
                captured.clear()
                captured.update(kwargs)
                return SimpleNamespace(returncode=0, stdout=str(common_dir) + "\n", stderr="")

            with self._windows_module(orchestration_native), self._no_window(
                orchestration_native
            ), mock.patch.object(orchestration_native.subprocess, "run", side_effect=fake_orchestration_run):
                # The helper canonicalizes what Git printed; the fixture's root
                # may still carry a short alias (/private/var on macOS,
                # RUNNER~1 on a Windows runner), so compare canonical forms.
                self.assertEqual(
                    Path(
                        orchestration_native.NativeApiWorkerFactory._git_common_dir(root)
                    ).resolve(),
                    common_dir.resolve(),
                )
            self._assert_background_launch(captured, orchestration_native)

    def test_checkpoint_git_helpers_use_windows_no_window_and_devnull_without_launching_git(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(*_args, **kwargs):
            captured.clear()
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="tree\n")

        with self._windows_module(ellipsis_checkpoint), self._no_window(
            ellipsis_checkpoint
        ), mock.patch.object(ellipsis_checkpoint.subprocess, "run", side_effect=fake_run):
            self.assertEqual(ellipsis_checkpoint._git(Path("."), ["write-tree"]), "tree")
        self._assert_background_launch(captured, ellipsis_checkpoint)

        def fake_binary_run(*_args, **kwargs):
            captured.clear()
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout=b"file.py\0")

        with self._windows_module(ellipsis_checkpoint), self._no_window(
            ellipsis_checkpoint
        ), mock.patch.object(ellipsis_checkpoint.subprocess, "run", side_effect=fake_binary_run):
            self.assertEqual(ellipsis_checkpoint._git_z(Path("."), ["ls-files", "-z"]), ("file.py",))
        self._assert_background_launch(captured, ellipsis_checkpoint)

    def test_playwright_installer_uses_windows_no_window_and_devnull_without_launching(self) -> None:
        captured: dict[str, object] = {}
        chromium = Path("managed-chromium")

        def fake_run(*_args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0)

        with self._windows_module(browser_bootstrap), self._no_window(
            browser_bootstrap
        ), mock.patch.object(browser_bootstrap, "_playwright_package_available", return_value=True), mock.patch.object(
            browser_bootstrap, "_chromium_executable", side_effect=[None, chromium]
        ), mock.patch.object(browser_bootstrap.subprocess, "run", side_effect=fake_run):
            status = browser_bootstrap.install_playwright_chromium()

        self.assertTrue(status.ready)
        self.assertTrue(status.installed_now)
        self._assert_background_launch(captured, browser_bootstrap)

    def test_posix_probes_keep_zero_creationflags(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(*_args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="/repo\n")

        # Patch the module binding, never the shared ``os`` module: flipping
        # ``os.name`` globally makes pathlib build a ``PosixPath`` on a Windows
        # host, and Python 3.10 refuses to instantiate one there (the check was
        # dropped in 3.11), so this file cannot use that shortcut.
        posix = SimpleNamespace(name="posix", environ={})
        with mock.patch.object(quickstart, "os", posix):
            quickstart.git_toplevel(Path("."), run=fake_run)

        self.assertEqual(captured["creationflags"], 0)
        self.assertIs(captured["stdin"], subprocess.DEVNULL)

    def test_tailscale_gui_launch_has_no_detached_or_inherited_console_streams(self) -> None:
        captured: dict[str, object] = {}
        gui = r"C:\\Program Files\\Tailscale\\tailscale-ipn.exe"

        def fake_popen(*_args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace()

        with mock.patch.object(tailscale, "find_tailscale_gui", return_value=gui), mock.patch.object(
            tailscale.subprocess, "CREATE_NO_WINDOW", _NO_WINDOW, create=True
        ), mock.patch.object(tailscale.subprocess, "DETACHED_PROCESS", 0x00000008, create=True):
            # No tray app running: this covers the launch itself, which a real
            # Windows host that already runs the Tailscale GUI would skip.
            self.assertEqual(
                tailscale.launch_tailscale_gui(popen=fake_popen, running_pids=lambda: ()),
                gui,
            )

        self.assertEqual(captured["creationflags"], _NO_WINDOW)
        self.assertIs(captured["stdin"], subprocess.DEVNULL)
        self.assertIs(captured["stdout"], subprocess.DEVNULL)
        self.assertIs(captured["stderr"], subprocess.DEVNULL)
        self.assertTrue(captured["close_fds"])
        self.assertFalse(captured["creationflags"] & 0x00000008)

    def test_tailscale_cli_and_uac_wrapper_do_not_inherit_stdin(self) -> None:
        captured: dict[str, object] = {}

        def fake_run(*_args, **kwargs):
            captured.clear()
            captured.update(kwargs)
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with self._no_window(tailscale):
            self.assertIsNotNone(tailscale._bring_tailscale_up("tailscale", run=fake_run))
        self._assert_background_launch(captured, tailscale)

        with mock.patch.object(tailscale.os, "name", "nt"), mock.patch.object(
            tailscale, "_is_windows_admin", return_value=False
        ), self._no_window(tailscale):
            self.assertTrue(tailscale.restart_tailscale_service(run=fake_run))
        self._assert_background_launch(captured, tailscale)
