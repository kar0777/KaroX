"""Real Windows execution-runtime tests for the KaroX guarded subprocess path.

Previous unit tests mocked ``Popen`` and so never exercised the Windows
``.cmd`` resolution gap that made ``npm`` fail with ``WinError 2``.  These
tests run real child processes through the guarded runners so the contract
holds on a Russian-locale Windows install with a Unicode repository path.

Skips are explicit and narrow: executable resolution and Unicode-repo checks
run on every platform; the browser fixture skips only when Playwright cannot
launch headless Chromium.
"""

from __future__ import annotations

import contextlib
import json
import os
import pathlib
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock as _mock

from _support import (  # noqa: F401 - inserts src on sys.path
    SRC,
    _CONFIG_OVERRIDES,
    _RUNTIME_OVERRIDES,
    child_environment,
)

from karox.process_launcher import (
    ExecutableResolutionError,
    is_executable_resolution_error,
    resolve_executable,
)


def _is_windows() -> bool:
    return os.name == "nt"


class ExecutableResolutionTests(unittest.TestCase):
    """``resolve_executable`` is the one place Windows ``.cmd`` resolution
    happens for every guarded runner."""

    def test_npm_resolves_to_cmd_or_exe_on_windows(self) -> None:
        if not _is_windows():
            self.skipTest("npm.cmd resolution is Windows-only")
        resolved = resolve_executable(["npm", "run", "start:safe"])
        self.assertNotEqual(resolved[0], "npm")
        self.assertTrue(pathlib.Path(resolved[0]).is_absolute())
        self.assertTrue(resolved[0].lower().endswith((".cmd", ".exe", ".bat")))
        self.assertEqual(resolved[1:], ["run", "start:safe"])

    def test_npx_resolves_on_windows(self) -> None:
        if not _is_windows():
            self.skipTest("npx.cmd resolution is Windows-only")
        resolved = resolve_executable(["npx", "some-pkg"])
        self.assertTrue(pathlib.Path(resolved[0]).is_absolute())

    def test_python_skips_windowsapps_alias_when_real_python_follows(self) -> None:
        if not _is_windows():
            self.skipTest("WindowsApps alias resolution is Windows-only")
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            alias_dir = root / "WindowsApps"
            real_dir = root / "Python313"
            alias_dir.mkdir()
            real_dir.mkdir()
            (alias_dir / "python.exe").write_bytes(b"alias")
            real_python = real_dir / "python.exe"
            real_python.write_bytes(b"real")
            with _mock.patch.dict(
                os.environ,
                {"PATH": os.pathsep.join((str(alias_dir), str(real_dir)))},
                clear=False,
            ):
                resolved = resolve_executable(["python", "-V"])
        self.assertEqual(Path(resolved[0]), real_python)
        self.assertEqual(resolved[1:], ["-V"])

    @unittest.skipIf(os.name != "nt", "argv[0] resolution raises only on Windows")
    def test_unknown_executable_raises_clear_error(self) -> None:
        with self.assertRaises(ExecutableResolutionError) as cm:
            resolve_executable(["karox-definitely-not-real-xyz", "arg"])
        self.assertEqual(cm.exception.executable, "karox-definitely-not-real-xyz")
        self.assertTrue(is_executable_resolution_error(cm.exception))
        self.assertIsInstance(cm.exception, FileNotFoundError)

    def test_non_first_arguments_preserved_verbatim_no_shell_string(self) -> None:
        # An argument that LOOKS like shell metacharacters must survive
        # unchanged and never be concatenated into a shell command.
        #
        # ``sys.executable`` rather than ``echo``: the resolver deliberately has
        # no notion of a shell built-in, because a built-in cannot be launched
        # with ``shell=False`` at all. Asserting that ``echo`` "stays" would
        # assert the opposite of the contract -- see the test below.
        tail = ["--", "x y; rm -rf /", "$HOME", "& whoami"]
        resolved = resolve_executable([sys.executable, *tail])
        self.assertEqual(resolved[1:], tail)

    def test_shell_builtin_is_rejected_rather_than_passed_through(self) -> None:
        # ``echo`` is a shell built-in on Windows with no executable on PATH, so
        # ``Popen(shell=False)`` could only fail with WinError 2 somewhere deeper.
        # Failing here, naming the command, is the whole reason this resolver
        # exists; passing the bare name through would hide it.
        #
        # "no executable on PATH" is a fact about the host, not about KaroX: a
        # machine with Git for Windows has ``C:\Program Files\Git\usr\bin\echo.EXE``
        # on PATH, the resolver rightly finds it, and this test then failed while
        # reporting nothing about the resolver.  Skip on such a host instead of
        # asserting the host's PATH.
        if not _is_windows():
            self.skipTest("echo is a real executable on POSIX hosts")
        if shutil.which("echo"):
            self.skipTest("this host has a real echo executable on PATH")
        with self.assertRaises(ExecutableResolutionError) as cm:
            resolve_executable(["echo", "hello"])
        self.assertEqual(cm.exception.executable, "echo")

    def test_shell_false_preserved_by_caller_contract(self) -> None:
        # The resolver never sets shell=True; it returns an argv list. A caller
        # that passes it to Popen(shell=False) must work. Demonstrate the
        # contract by running a real process through the resolved argv.
        argv = ["python.exe" if _is_windows() else "python3", "-c", "print('ok')"]
        resolved = resolve_executable(argv)
        import subprocess

        proc = subprocess.run(
            resolved,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
            timeout=20,
            check=False,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertIn("ok", proc.stdout)

    def test_empty_argv_returned_unchanged(self) -> None:
        self.assertEqual(resolve_executable([]), [])


class UnicodeRepoPathTests(unittest.TestCase):
    """A repository path containing Cyrillic must run a real child process
    through the checks runner without any ASCII/cp1251 corruption."""

    def _isolated_env(self) -> contextlib.ExitStack:
        stack = contextlib.ExitStack()
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        cfg = Path(tmp) / "config"; cfg.mkdir()
        rt = Path(tmp) / "runtime"; rt.mkdir()
        env = {k: str(cfg) for k in _CONFIG_OVERRIDES}
        env.update({k: str(rt) for k in _RUNTIME_OVERRIDES})
        stack.callback(os.chdir, Path.cwd())  # contextlib.chdir requires Python 3.11
        real = os.environ.copy()
        real.update(env)
        stack.enter_context(_patch.dict(os.environ, real, clear=False))
        return stack

    def test_unicode_repo_path_runs_real_child_process(self) -> None:
        import subprocess

        from karox.security import child_process_environment

        with self._isolated_env() as stack:
            # Keep each Unicode-path run isolated from other workers and retries.
            temporary = stack.enter_context(tempfile.TemporaryDirectory())
            base = Path(temporary) / "проекты" / "тестовый-проект"
            base.mkdir(parents=True, exist_ok=True)
            (base / "package.json").write_text(
                '{"name":"x","scripts":{"test":"node -e \\"1\\""}}',
                encoding="utf-8",
            )
            self.assertTrue(base.exists())
            self.assertTrue(base.is_dir())
            # Run a real child process with cwd = the Unicode path, proving the
            # path survives the subprocess boundary intact.
            #
            # ``env=child_process_environment()`` is the point of the test, not a
            # detail: KaroX decodes captured output as UTF-8, and a child that
            # inherits a cp866 console code page answers in cp866, so the Cyrillic
            # path came back as replacement characters until the runtime started
            # forcing the child's encoding. Asserting through the runtime's own
            # environment builder is what keeps that from regressing.
            proc = subprocess.run(
                [sys.executable, "-c",
                 "import os,sys; print('cwd=',os.getcwd()); "
                 "print('exists=', os.path.isdir(os.getcwd()))"],
                cwd=str(base),
                env=child_process_environment(),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=20,
                check=False,
            )
            self.assertEqual(proc.returncode, 0)
            self.assertIn("exists= True", proc.stdout)
            self.assertIn("тестовый-проект", proc.stdout)

    def test_checks_runner_uses_resolved_executable_on_unicode_repo(self) -> None:
        # Run checks.run against a Unicode repo path through the real CoreToolBridge,
        # proving the resolver + Unicode cwd work end-to-end (no WinError 2, no
        # "repository path does not exist").
        with self._isolated_env() as stack:
            tmp = Path(stack.enter_context(tempfile.TemporaryDirectory()))
            base = tmp / "проекты" / "тестовый-проект"
            base.mkdir(parents=True)
            # A pyproject-style project so _default_verification picks pytest;
            # but we override verification to a python one-liner that always
            # succeeds, so this test never depends on pytest being installed.
            (base / "tests").mkdir()
            (base / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
            from karox.sessions import SessionStore
            from karox.models import AccessProfile, Origin, OriginKind
            from karox.hosted_bridge import CoreToolBridge

            sessions = SessionStore(tmp / "sessions")
            sessions.create(base, "unicode", AccessProfile.WORKSPACE_WRITE, session_id="s-uni")
            origin = Origin(OriginKind.HOSTED_CLIENT, "core-s-uni")
            vc = ((sys.executable, "-c", "print('unicode-ok')"),)
            bridge = CoreToolBridge(
                base, sessions, "s-uni", ["karox.checks.run"],
                hosted_origin=origin, audit_path=tmp / "audit.jsonl",
                verification_commands=vc,
            )
            result = bridge.execute(
                "karox.checks.run",
                {"argv": [sys.executable, "-c", "print('unicode-ok')"]},
                idempotency_key="uni-check",
            )
            data = result.get("data", result)
            self.assertEqual(data.get("exit_code"), 0)
            self.assertNotIn("does not exist", str(data))
            self.assertIn("unicode-ok", data.get("stdout", ""))


class ManagedServerWindowsTests(unittest.TestCase):
    """A safe fixture server started/stopped through the real runtime, proving
    the resolver + lifecycle + idempotency + cross-session guards work."""

    def _isolated_env(self):
        stack = contextlib.ExitStack()
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        cfg = Path(tmp) / "config"; cfg.mkdir()
        rt = Path(tmp) / "runtime"; rt.mkdir()
        env = {k: str(cfg) for k in _CONFIG_OVERRIDES}
        env.update({k: str(rt) for k in _RUNTIME_OVERRIDES})
        real = os.environ.copy(); real.update(env)
        stack.enter_context(_patch.dict(os.environ, real, clear=False))
        return stack, tmp

    def _fixture_server_profile(self, port: int):
        # A safe dev-server profile that starts a real python HTTP server on
        # 127.0.0.1, mirroring the vacancy-control-safe profile shape but with
        # no Facebook involvement.
        from karox.hosted_tools_runtime import ManagedServerProfile

        argv = [sys.executable, "-c",
                f"import http.server,socketserver;"
                f"socketserver.TCPServer(('127.0.0.1',{port}),"
                f"http.server.SimpleHTTPRequestHandler).serve_forever()"]
        return ManagedServerProfile(
            name="python-http-fixture",
            argv=tuple(argv),
            env={},
            env_allowlist=frozenset({"PYTHONPATH"}),
            host_hint="127.0.0.1",
            ready_url=None,
        )

    def test_start_status_idempotent_stop_no_orphans(self) -> None:
        stack, tmp = self._isolated_env(); tmp = Path(tmp)
        with stack:
            from karox.sessions import SessionStore
            from karox.models import AccessProfile, Origin, OriginKind
            from karox.hosted_tools_runtime import HostedToolsRuntime

            port = 0  # let the OS pick
            # The fixture argv uses port 0 which the server reads as an
            # ephemeral port; for readiness we skip ready_url, so just assert
            # the process starts and is controllable.
            # Use a fixed free port:
            import socket as _s
            sk = _s.socket(); sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]; sk.close()
            profile = self._fixture_server_profile(port)

            repo = Path(tmp) / "repo"; repo.mkdir()
            sessions = SessionStore(tmp / "sessions")
            sessions.create(repo, "srv", AccessProfile.WORKSPACE_WRITE, session_id="s-srv")
            origin = Origin(OriginKind.HOSTED_CLIENT, "tools-s-srv")
            rt = HostedToolsRuntime(
                repo, sessions, "s-srv",
                ["karox.dev_server.start", "karox.dev_server.status",
                 "karox.dev_server.stop"],
                access_profile=AccessProfile.WORKSPACE_WRITE,
                hosted_origin=origin, server_profiles=(profile,),
                audit_path=tmp / "audit.jsonl",
            )
            try:
                start = rt.execute(
                    "karox.dev_server.start",
                    {"argv": list(profile.argv)},
                    idempotency_key="start-1",
                ).structuredContent
                self.assertTrue(start.get("ok"), start)
                self.assertTrue(start.get("running"))
                pid = start["pid"]
                proc_id = start["process_id"]

                # idempotent second start reuses the live process
                start2 = rt.execute(
                    "karox.dev_server.start",
                    {"argv": list(profile.argv), "process_id": proc_id},
                    idempotency_key="start-2",
                ).structuredContent
                self.assertTrue(start2.get("ok"))
                self.assertEqual(start2.get("pid"), pid)
                self.assertTrue(start2.get("reused"))

                # status reports the live process
                status = rt.execute(
                    "karox.dev_server.status", {"process_id": proc_id},
                    idempotency_key="status-1",
                ).structuredContent
                self.assertTrue(status.get("ok"))

                # stop ends it
                stop = rt.execute(
                    "karox.dev_server.stop", {"process_id": proc_id},
                    idempotency_key="stop-1",
                ).structuredContent
                self.assertTrue(stop.get("ok"))
                # wait briefly for the OS to reap the pid
                time.sleep(1.0)
                from karox.remote_tools import _pid_alive
                self.assertFalse(_pid_alive(pid), "orphaned server process after stop")
            finally:
                try:
                    rt.cleanup_session()
                except Exception:
                    pass

    def test_cross_session_stop_denied(self) -> None:
        stack, tmp = self._isolated_env(); tmp = Path(tmp)
        with stack:
            from karox.sessions import SessionStore
            from karox.models import AccessProfile, Origin, OriginKind
            from karox.hosted_tools_runtime import HostedToolsRuntime

            import socket as _s
            sk = _s.socket(); sk.bind(("127.0.0.1", 0))
            port = sk.getsockname()[1]; sk.close()
            profile = self._fixture_server_profile(port)
            repo = Path(tmp) / "repo"; repo.mkdir()
            sessions = SessionStore(tmp / "sessions")
            sessions.create(repo, "a", AccessProfile.WORKSPACE_WRITE, session_id="s-a")
            sessions.create(repo, "b", AccessProfile.WORKSPACE_WRITE, session_id="s-b")
            oa = Origin(OriginKind.HOSTED_CLIENT, "tools-s-a")
            ra = HostedToolsRuntime(
                repo, sessions, "s-a",
                ["karox.dev_server.start", "karox.dev_server.stop"],
                access_profile=AccessProfile.WORKSPACE_WRITE,
                hosted_origin=oa, server_profiles=(profile,),
                audit_path=tmp / "audit.jsonl",
            )
            ob = Origin(OriginKind.HOSTED_CLIENT, "tools-s-b")
            rb = HostedToolsRuntime(
                repo, sessions, "s-b",
                ["karox.dev_server.start", "karox.dev_server.stop"],
                access_profile=AccessProfile.WORKSPACE_WRITE,
                hosted_origin=ob, server_profiles=(profile,),
                audit_path=tmp / "audit2.jsonl",
            )
            try:
                start = ra.execute(
                    "karox.dev_server.start", {"argv": list(profile.argv)},
                    idempotency_key="a-start",
                ).structuredContent
                proc_id = start["process_id"]
                # session B must not stop session A's process
                stop = rb.execute(
                    "karox.dev_server.stop", {"process_id": proc_id},
                    idempotency_key="b-stop",
                ).structuredContent
                self.assertFalse(stop.get("ok"))
                self.assertIn("denied", stop.get("error", "").lower() + str(stop.get("error_code","")))
                # A still owns it and can stop its own
                ok = ra.execute(
                    "karox.dev_server.stop", {"process_id": proc_id},
                    idempotency_key="a-stop",
                ).structuredContent
                self.assertTrue(ok.get("ok"))
            finally:
                for r in (ra, rb):
                    try:
                        r.cleanup_session()
                    except Exception:
                        pass


class BrowserRuntimeFixtureTests(unittest.TestCase):
    """Prove the browser runtime works on a localhost fixture (NOT Facebook).

    Exercises open/snapshot/get_text/fill/click/select/press/screenshot/console/
    network_failures/close through the real BrowserSessionManager against a
    tiny localhost HTML page, plus the external-URL block.
    """

    @classmethod
    def setUpClass(cls) -> None:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            if os.environ.get("KAROX_REQUIRE_BROWSER_TESTS") == "1":
                raise RuntimeError("required Playwright dependency is missing") from exc
            raise unittest.SkipTest("playwright not installed") from exc
        # The context manager must stop the sync driver even when launch fails.
        # A leaked driver leaves a running loop on this thread and poisons later
        # IsolatedAsyncioTestCase setup (especially on Python 3.10).
        try:
            with sync_playwright() as ctx:
                browser = ctx.chromium.launch(headless=True)
                browser.close()
        except Exception as exc:  # pragma: no cover - environment-dependent
            if os.environ.get("KAROX_REQUIRE_BROWSER_TESTS") == "1":
                raise RuntimeError(f"required Chromium fixture unavailable: {exc}") from exc
            raise unittest.SkipTest(f"chromium unavailable: {exc}") from exc

    def _isolated_env(self):
        stack = contextlib.ExitStack()
        tmp = stack.enter_context(tempfile.TemporaryDirectory())
        cfg = Path(tmp) / "config"; cfg.mkdir()
        rt = Path(tmp) / "runtime"; rt.mkdir()
        env = {k: str(cfg) for k in _CONFIG_OVERRIDES}
        env.update({k: str(rt) for k in _RUNTIME_OVERRIDES})
        real = os.environ.copy(); real.update(env)
        stack.enter_context(_patch.dict(os.environ, real, clear=False))
        return stack, tmp

    def test_browser_runtime_on_localhost_fixture(self) -> None:
        from _localhost_fixture import start_localhost_fixture
        from karox.artifacts import ArtifactStore
        from karox.browser_session import BrowserSessionManager, BrowserSecurityError

        stack, tmp = self._isolated_env(); tmp = Path(tmp)
        with stack:
            fixtures = start_localhost_fixture()
            try:
                store = ArtifactStore("browser-fix-session")
                browser = BrowserSessionManager(store)
                deadline = 20.0
                # open
                opened = browser.open({"url": fixtures.url}, deadline)
                self.assertTrue(opened["open"])
                # snapshot
                snap = browser.snapshot({}, deadline)
                self.assertIn("url", snap)
                # get_text
                txt = browser.get_text({"selector": "#status"}, deadline)
                self.assertIn("ready", txt.get("text", ""))
                # fill + click
                browser.fill({"selector": "#name", "value": "kx"}, deadline)
                browser.click({"selector": "#go"}, deadline)
                # Wait for the click handler to update #status before reading.
                browser.wait_for({"milliseconds": 400}, deadline)
                txt2 = browser.get_text({"selector": "#status"}, deadline)
                self.assertIn("clicked", txt2.get("text", ""))
                self.assertIn("kx", txt2.get("text", ""))
                # select
                sel = browser.select({"selector": "#color", "value": "blue"}, deadline)
                self.assertTrue(sel.get("selected"))
                # press
                browser.press({"selector": "#name", "key": "Enter"}, deadline)
                # console
                con = browser.console({}, deadline)
                self.assertTrue(con.get("entries"))
                self.assertTrue(any("fixture ready" in e.get("text", "") for e in con["entries"]))
                # screenshot -> real image/png artifact
                shot = browser.screenshot({}, deadline)
                self.assertTrue(shot.get("artifact_id"))
                # read the artifact back as image/png
                data, mime = store.read_image(shot["artifact_id"])
                self.assertEqual(mime, "image/png")
                self.assertEqual(data[:8], b"\x89PNG\r\n\x1a\n")
                # network_failures
                net = browser.network_failures({}, deadline)
                self.assertIsInstance(net.get("failed_requests"), list)
                # close
                closed = browser.close()
                self.assertTrue(closed.get("closed"))
            finally:
                fixtures.stop()

    def test_external_url_blocked(self) -> None:
        from karox.artifacts import ArtifactStore
        from karox.browser_session import BrowserSessionManager, BrowserSecurityError

        stack, tmp = self._isolated_env(); tmp = Path(tmp)
        with stack:
            store = ArtifactStore("ext-session")
            browser = BrowserSessionManager(store)
            with self.assertRaises(BrowserSecurityError):
                browser.open({"url": "https://example.com"}, 10.0)
            browser.close()


# Alias used inside test methods for patch.dict convenience.
_patch = _mock.patch


if __name__ == "__main__":
    unittest.main()
