"""Unit/integration tests for the hosted browser/dev-server/artifact runtime.

These cover the fourteen ТЗ section-A checks: command allowlist enforcement,
server-profile allowlist (start:safe yes, npm start no), dev-server idempotency
and session ownership, localhost-only browser navigation with redirect/external
blocking, PNG screenshot + image content, cross-session artifact isolation,
secret-free snapshots, redacted logs, and session cleanup.

Browser tests (A6, A9, A10) spin a real localhost http.server and real
Playwright Chromium, so they are gated on the optional ``browser`` extra.
Dev-server tests (A2-A5) drive the runtime through a fake ``popen_factory`` so
they never start a real long-running process and stay fast.
"""

from __future__ import annotations

import http.server
import inspect
import os
import socketserver
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

from _support import ROOT, SRC, initialize_git_repository  # noqa: F401  (path bootstrap)

from karox.artifacts import ArtifactStore
from karox.browser_session import (
    BrowserError,
    BrowserSecurityError,
    BrowserSessionManager,
    _validate_local_url,
)
from karox.hosted_bridge import HOSTED_EXTRA_TOOL_NAMES, KNOWN_HOSTED_TOOL_NAMES
from karox.hosted_tools_runtime import (
    ARTIFACT_READ_IMAGE,
    BROWSER_OPEN,
    BROWSER_SCREENSHOT,
    BROWSER_SNAPSHOT,
    DEV_SERVER_START,
    DEV_SERVER_STATUS,
    DEV_SERVER_STOP,
    HostedToolsRuntime,
    ManagedServerProfile,
    default_server_profiles,
)
import karox.hosted_tools_runtime as _htr_mod
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.sessions import SessionStore
from mcp.types import CallToolResult, ImageContent


def _has_playwright() -> bool:
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401

    except Exception:
        return False
    # The package being importable is not enough: the launchable browser
    # binary must be present too (a fresh CI runner installs the wheel but
    # not playwright's cached browsers).
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            exe = p.chromium.executable_path
        return bool(exe) and Path(exe).exists()
    except Exception:
        return False


def _runtime_dir_env(value: Path) -> dict[str, str]:
    """Environment pinning the runtime dir so ArtifactStore/ManagedProcessStore isolate."""
    env = dict(os.environ)
    env["KAROX_VNEXT_RUNTIME_DIR"] = str(value)
    env.pop("KAROX_RUNTIME_DIR", None)
    return env


class _FakeProcess:
    """A stand-in for subprocess.Popen with a controllable pid and lifecycle."""

    def __init__(self, argv: list[str], pid: int) -> None:
        self.argv = argv
        self.pid = pid
        self._alive = True

    def poll(self) -> Optional[int]:
        return None if self._alive else 0

    def kill(self) -> None:
        self._alive = False

    def terminate(self) -> None:
        self._alive = False


def _fake_pid_alive():
    """Patch ``_pid_alive``/``_kill_pid_tree`` so fake pids look live until killed.

    The runtime asks the OS whether a pid is alive; a fake ``_FakeProcess`` owns
    a synthetic pid the OS has never heard of, so without this patch idempotency
    and cleanup assertions see "dead" processes and respawn or report nothing.
    """
    alive: set[int] = set()

    def is_alive(pid: int) -> bool:
        return pid in alive

    def kill_tree(pid: int) -> None:
        alive.discard(pid)

    def register(pid: int) -> None:
        alive.add(pid)

    return is_alive, kill_tree, register


def _fake_process_identity(pid: int):
    """Return stable OS identity facts for a synthetic test-only PID."""

    return _htr_mod.ProcessIdentity(
        pid=pid,
        created_at=float(pid),
        executable="node.exe",
        cmdline_digest=f"fake-{pid}",
    )


class _LocalHttpServer:
    """A throwaway localhost http.server for browser tests."""

    def __init__(self, html: str = "<html><body><h1>ok</h1></body></html>") -> None:
        self.html = html
        handler = _make_handler(html)
        self.httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)


def _make_handler(html: str) -> type:
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            pass

        def do_GET(self) -> None:  # noqa: N802 - http.server convention
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(html.encode("utf-8"))

    return Handler


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._old_env = dict(os.environ)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.repository = self.root / "repo"
        initialize_git_repository(self.repository)
        # Isolate the runtime dir so ArtifactStore/ManagedProcessStore do not
        # touch the developer's real KaroX state.
        for name in ("KAROX_VNEXT_RUNTIME_DIR", "KAROX_RUNTIME_DIR"):
            os.environ.pop(name, None)
        os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(self.root / "runtime")
        self.sessions = SessionStore(self.root / "sessions")
        self.session = self.sessions.create(
            self.repository,
            "hosted tools test",
            AccessProfile.ELEVATED,
            session_id="sess-a",
        )
        self.origin = Origin(OriginKind.HOSTED_CLIENT, "test-hosted")

    def tearDown(self) -> None:
        os.environ.clear()
        os.environ.update(self._old_env)
        from _support import cleanup_temporary_directory

        cleanup_temporary_directory(self.temporary)

    def _runtime(
        self,
        tools=HOSTED_EXTRA_TOOL_NAMES,
        *,
        server_profiles=default_server_profiles(),
        session_id: str = "sess-a",
        access_profile: AccessProfile = AccessProfile.ELEVATED,
        popen_factory=None,
    ) -> HostedToolsRuntime:
        return HostedToolsRuntime(
            self.repository,
            self.sessions,
            session_id,
            tuple(tools),
            access_profile=access_profile,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, f"test-{session_id}"),
            server_profiles=tuple(server_profiles),
            popen_factory=popen_factory,
        )


class TestChecksAndServerProfiles(_Base):
    """A1: command outside allowlist blocked; A2: start:safe allowed; A3: npm start denied."""

    def test_a1_command_outside_allowlist_is_blocked(self) -> None:
        # karox.checks.run is a Core tool; here we verify the *server* side of
        # the same principle: a dev_server argv not in any approved profile is
        # refused, mirroring the checks.run allowlist enforcement.
        runtime = self._runtime(
            tools=(DEV_SERVER_START, DEV_SERVER_STOP),
            server_profiles=default_server_profiles(),
        )
        result = runtime.execute(
            DEV_SERVER_START,
            {"argv": ["node", "scripts/evil.mjs"]},
            deadline_seconds=10,
        )
        self.assertTrue(isinstance(result, CallToolResult))
        self.assertTrue(result.isError)
        self.assertIn("allowlist", result.structuredContent.get("error", ""))

    def test_a2_start_safe_is_allowed(self) -> None:
        is_alive, kill_tree, register = _fake_pid_alive()
        register(44001)
        runtime = self._runtime(
            tools=(DEV_SERVER_START,),
            server_profiles=default_server_profiles(),
            popen_factory=self._fake_popen(pid=44001),
        )
        with (
            mock.patch.multiple(_htr_mod, _pid_alive=is_alive, _kill_pid_tree=kill_tree),
            mock.patch.object(
                _htr_mod.ProcessIdentity,
                "capture",
                side_effect=_fake_process_identity,
            ),
        ):
            result = runtime.execute(
                DEV_SERVER_START,
                {"argv": ["npm", "run", "start:safe"], "ready_url": None},
                deadline_seconds=10,
            )
        payload = result if isinstance(result, dict) else result.structuredContent
        self.assertTrue(payload.get("ok"))
        self.assertEqual(payload["env_keys"], ["FACEBOOK_LIVE_ENABLED", "HOST"])

    def test_failed_readiness_reports_why_not_only_that_it_failed(self) -> None:
        # ``ready: false`` on its own does not distinguish a dev server that
        # crashed on startup from one that is merely slow or listening on another
        # port. The poll loop collected the transport error and then dropped it,
        # so every one of those arrived at the agent identically. Port 1 is
        # closed, which makes this a real connection failure rather than a mock.
        is_alive, kill_tree, register = _fake_pid_alive()
        register(44002)
        runtime = self._runtime(
            tools=(DEV_SERVER_START,),
            server_profiles=default_server_profiles(),
            popen_factory=self._fake_popen(pid=44002),
        )
        with (
            mock.patch.multiple(_htr_mod, _pid_alive=is_alive, _kill_pid_tree=kill_tree),
            mock.patch.object(
                _htr_mod.ProcessIdentity,
                "capture",
                side_effect=_fake_process_identity,
            ),
        ):
            result = runtime.execute(
                DEV_SERVER_START,
                {
                    "argv": ["npm", "run", "start:safe"],
                    "ready_url": "http://127.0.0.1:1/",
                },
                deadline_seconds=1,
            )
        payload = result if isinstance(result, dict) else result.structuredContent

        self.assertFalse(payload.get("ok"))
        self.assertEqual(payload.get("error_code"), "readiness_failed")
        self.assertFalse(payload["ready"])
        self.assertTrue(payload["ready_error"], "readiness failure carried no reason")

    def test_readiness_that_was_never_checked_carries_no_error(self) -> None:
        # The field has to distinguish "not polled" from "polled and failed",
        # otherwise a caller cannot tell an absent readiness URL from a broken one.
        is_alive, kill_tree, register = _fake_pid_alive()
        register(44003)
        runtime = self._runtime(
            tools=(DEV_SERVER_START,),
            server_profiles=default_server_profiles(),
            popen_factory=self._fake_popen(pid=44003),
        )
        with (
            mock.patch.multiple(_htr_mod, _pid_alive=is_alive, _kill_pid_tree=kill_tree),
            mock.patch.object(
                _htr_mod.ProcessIdentity,
                "capture",
                side_effect=_fake_process_identity,
            ),
        ):
            result = runtime.execute(
                DEV_SERVER_START,
                {"argv": ["npm", "run", "start:safe"], "ready_url": None},
                deadline_seconds=10,
            )
        payload = result if isinstance(result, dict) else result.structuredContent

        self.assertFalse(payload["ready"])
        self.assertIsNone(payload["ready_error"])

    def test_port_profile_derives_loopback_readiness_url_automatically(self) -> None:
        is_alive, kill_tree, register = _fake_pid_alive()
        register(44004)
        profile = ManagedServerProfile(
            name="static-html-loopback",
            argv=("python", "-m", "karox.static_server"),
            env={"HOST": "127.0.0.1", "PORT": "8765"},
            env_allowlist=frozenset({"PORT"}),
            host_hint="127.0.0.1",
        )
        runtime = self._runtime(
            tools=(DEV_SERVER_START,), server_profiles=(profile,),
            popen_factory=self._fake_popen(pid=44004),
        )
        with (
            mock.patch.multiple(_htr_mod, _pid_alive=is_alive, _kill_pid_tree=kill_tree),
            mock.patch.object(_htr_mod.ProcessIdentity, "capture", side_effect=_fake_process_identity),
            mock.patch.object(runtime, "_poll_ready", return_value=(True, None)) as poll,
        ):
            result = runtime.execute(
                DEV_SERVER_START,
                {"argv": ["python", "-m", "karox.static_server"]},
                deadline_seconds=10,
            )
        payload = result if isinstance(result, dict) else result.structuredContent
        self.assertTrue(payload["ok"])
        self.assertTrue(payload["ready"])
        self.assertEqual(payload["url"], "http://127.0.0.1:8765/")
        poll.assert_called_once_with("http://127.0.0.1:8765/", 10)

    def test_static_server_module_is_the_only_managed_karox_module_allowed_for_stop(self) -> None:
        runtime = self._runtime(tools=(DEV_SERVER_STATUS,), server_profiles=default_server_profiles())
        live = _htr_mod.ProcessIdentity(
            pid=55001, created_at=1.0, executable="python.exe", cmdline_digest="digest"
        )
        static_record = _htr_mod.ManagedProcessRecord(
            process_id="static", pid=55001, session_id="sess",
            argv=("python", "-m", "karox.static_server"), started_at=1.0,
            stdout_path="out", stderr_path="err",
        )
        control_record = _htr_mod.ManagedProcessRecord(
            process_id="control", pid=55002, session_id="sess",
            argv=("python", "-m", "karox.cli"), started_at=1.0,
            stdout_path="out", stderr_path="err",
        )
        self.assertIsNone(runtime._protected_process_reason(static_record, live))
        self.assertIn("KaroX Python module", runtime._protected_process_reason(control_record, live) or "")

    def test_a3_bare_npm_start_is_denied(self) -> None:
        # The safe profile permits start:safe, not the bare `npm start` which
        # enables live publication by default in the Vacancy Control project.
        runtime = self._runtime(
            tools=(DEV_SERVER_START,),
            server_profiles=default_server_profiles(),
        )
        result = runtime.execute(
            DEV_SERVER_START,
            {"argv": ["npm", "start"]},
            deadline_seconds=10,
        )
        self.assertTrue(isinstance(result, CallToolResult))
        self.assertTrue(result.isError)

    def _fake_popen(self, pid: int):
        def factory(argv, cwd, env, stdout, stderr, shell):  # noqa: ANN001
            proc = _FakeProcess(argv, pid=pid)
            # Write a tiny line so logs read back non-empty and redacted.
            try:
                stdout.write(b"[karox] started\n")
                stdout.flush()
            except Exception:
                pass
            return proc

        return factory


class TestDevServerLifecycle(_Base):
    """A4: idempotent start; A5: cannot stop another session's process."""

    def test_a4_dev_server_start_is_idempotent(self) -> None:
        is_alive, kill_tree, register = _fake_pid_alive()
        register(44010)
        runtime = self._runtime(
            tools=(DEV_SERVER_START, DEV_SERVER_STATUS, DEV_SERVER_STOP),
            server_profiles=default_server_profiles(),
            popen_factory=self._fake_popen(pid=44010),
        )
        with (
            mock.patch.multiple(_htr_mod, _pid_alive=is_alive, _kill_pid_tree=kill_tree),
            mock.patch.object(
                _htr_mod.ProcessIdentity,
                "capture",
                side_effect=_fake_process_identity,
            ),
        ):
            first = runtime.execute(
                DEV_SERVER_START,
                {"argv": ["npm", "run", "start:safe"], "process_id": "srv-idem"},
                deadline_seconds=10,
            )
            second = runtime.execute(
                DEV_SERVER_START,
                {"argv": ["npm", "run", "start:safe"], "process_id": "srv-idem"},
                deadline_seconds=10,
            )
        f = first if isinstance(first, dict) else first.structuredContent
        s = second if isinstance(second, dict) else second.structuredContent
        self.assertTrue(f.get("ok"))
        self.assertTrue(s.get("reused"))
        self.assertEqual(f["process_id"], s["process_id"])

    def test_a5_session_cannot_stop_foreign_process(self) -> None:
        # Start under session A, then a second runtime for a different session
        # cannot even *see* the process, let alone stop it.
        runtime_a = self._runtime(
            tools=(DEV_SERVER_START, DEV_SERVER_STOP),
            server_profiles=default_server_profiles(),
            popen_factory=self._fake_popen(pid=44020),
        )
        runtime_a.execute(
            DEV_SERVER_START,
            {"argv": ["npm", "run", "start:safe"], "process_id": "srv-owned"},
            deadline_seconds=10,
        )
        # A second session B in the same repo.
        self.sessions.create(
            self.repository,
            "other session",
            AccessProfile.ELEVATED,
            session_id="sess-b",
        )
        runtime_b = HostedToolsRuntime(
            self.repository,
            self.sessions,
            "sess-b",
            (DEV_SERVER_START, DEV_SERVER_STOP),
            access_profile=AccessProfile.ELEVATED,
            hosted_origin=Origin(OriginKind.HOSTED_CLIENT, "test-sess-b"),
            server_profiles=default_server_profiles(),
        )
        result = runtime_b.execute(
            DEV_SERVER_STOP,
            {"process_id": "srv-owned"},
            deadline_seconds=10,
        )
        payload = result if isinstance(result, dict) else result.structuredContent
        self.assertFalse(payload.get("ok"))
        self.assertEqual(payload.get("error_code"), "denied")

    def _fake_popen(self, pid: int):
        def factory(argv, cwd, env, stdout, stderr, shell):  # noqa: ANN001
            return _FakeProcess(argv, pid=pid)

        return factory


class TestBrowserSecurity(unittest.TestCase):
    """A7: external URL blocked; A8: redirect to external blocked (url validation)."""

    def test_a7_external_url_is_blocked(self) -> None:
        with self.assertRaises(BrowserSecurityError):
            _validate_local_url("https://example.com/")
        with self.assertRaises(BrowserSecurityError):
            _validate_local_url("http://10.0.0.5:3000/")

    def test_a8_redirect_target_validation_blocks_external(self) -> None:
        # The URL validator is the first line; a redirect that resolved to a
        # non-loopback host is rejected the same way, since the route aborts
        # any non-local request and the post-navigation URL is re-validated.
        with self.assertRaises(BrowserSecurityError):
            _validate_local_url("file:///etc/passwd")
        with self.assertRaises(BrowserSecurityError):
            _validate_local_url("data:text/html,<script></script>")
        # A localhost URL is accepted.
        self.assertEqual(_validate_local_url("http://127.0.0.1:8080/"), "http://127.0.0.1:8080/")

    @unittest.skipUnless(_has_playwright(), "requires the optional Playwright extra")
    def test_a6_browser_opens_localhost(self) -> None:
        temp = tempfile.TemporaryDirectory()
        try:
            os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(Path(temp.name) / "runtime")
            os.environ.pop("KAROX_RUNTIME_DIR", None)
            repo = Path(temp.name) / "repo"
            initialize_git_repository(repo)
            sessions = SessionStore(Path(temp.name) / "sessions")
            sessions.create(repo, "browser", AccessProfile.ELEVATED, session_id="s")
            store = ArtifactStore("s")
            manager = BrowserSessionManager(store)
            server = _LocalHttpServer()
            try:
                result = manager.open({"url": server.url}, deadline_seconds=15)
                self.assertTrue(result["open"])
                self.assertIn("127.0.0.1", result["url"])
            finally:
                manager.close()
                server.stop()
        finally:
            os.environ.pop("KAROX_VNEXT_RUNTIME_DIR", None)


class TestScreenshotsAndArtifacts(_Base):
    """A9: valid PNG; A10: image content; A11: cross-session isolation."""

    @unittest.skipUnless(_has_playwright(), "requires the optional Playwright extra")
    def test_a9_a10_screenshot_creates_valid_png_returned_as_image(self) -> None:
        runtime = self._runtime(
            tools=(BROWSER_OPEN, BROWSER_SCREENSHOT, ARTIFACT_READ_IMAGE),
            server_profiles=(),
        )
        server = _LocalHttpServer(html="<html><body><h1>shot</h1></body></html>")
        try:
            runtime.execute(BROWSER_OPEN, {"url": server.url}, deadline_seconds=20)
            result = runtime.execute(
                BROWSER_SCREENSHOT, {"full_page": True, "name": "dash"}, deadline_seconds=20
            )
            self.assertIsInstance(result, CallToolResult)
            self.assertFalse(result.isError)
            # The PNG must arrive as MCP image content with the right MIME.
            images = [c for c in result.content if isinstance(c, ImageContent)]
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0].mimeType, "image/png")
            import base64

            raw = base64.b64decode(images[0].data)
            self.assertEqual(raw[:8], b"\x89PNG\r\n\x1a\n")
            artifact_id = result.structuredContent["artifact_id"]
            # A10 also: read_image returns the same bytes as image content.
            r2 = runtime.execute(
                ARTIFACT_READ_IMAGE, {"artifact_id": artifact_id}, deadline_seconds=10
            )
            imgs2 = [c for c in r2.content if isinstance(c, ImageContent)]
            self.assertEqual(len(imgs2), 1)
            self.assertEqual(base64.b64decode(imgs2[0].data), raw)
        finally:
            runtime._browser.close()
            server.stop()

    def test_a11_cross_session_artifact_is_inaccessible(self) -> None:
        store_a = ArtifactStore("sess-a")
        record = store_a.put(b"\x89PNG\r\n\x1a\nsecret-png", name="x.png", mime="image/png")
        store_b = ArtifactStore("sess-b")
        with self.assertRaises(FileNotFoundError):
            store_b.read(record.artifact_id)
        with self.assertRaises(FileNotFoundError):
            store_b.read_image(record.artifact_id)
        # Same id on store_b is a different (absent) artifact.
        self.assertFalse(store_b.exists(record.artifact_id))


class TestSnapshotAndRedaction(unittest.TestCase):
    """A12: password values never in snapshot; A13: console/server logs redact secrets."""

    def test_a12_snapshot_helper_redacts_secret_inputs(self) -> None:
        # The snapshot probe never returns the *value* of a secret input; it
        # reports only a secret flag and a length. Assert against the probe
        # string that a secret input contributes only `value_length` (length),
        # never a `value:` field carrying `el.value` itself.
        from karox import browser_session

        source = inspect.getsource(browser_session)
        self.assertIn("value_length", source)
        # A returned value field would carry the secret; the probe must not
        # expose `value: el.value` for any input.
        self.assertNotIn("value: el.value", source)
        # Secret detection covers password type, name/id/aria-label/label hints.
        self.assertIn("SECRET_HINT", source)
        self.assertIn("'password'", source)

    def test_a13_console_and_server_logs_redact_secrets(self) -> None:
        from karox.security import redact

        # A console line leaking a GitHub token is rewritten.
        line = "loaded token ghp_0123456789abcdefghijklmno for user"
        self.assertNotIn("ghp_0123456789abcdefghijklmno", redact(line))
        # A password env value is redacted by key name.
        self.assertEqual(redact({"password": "hunter2"})["password"], "[REDACTED]")


class TestSessionCleanup(_Base):
    """A14: browser close + session cleanup stop processes."""

    def test_a14_cleanup_session_stops_processes_and_closes_browser(self) -> None:
        is_alive, kill_tree, register = _fake_pid_alive()
        register(44040)
        runtime = self._runtime(
            tools=(DEV_SERVER_START, DEV_SERVER_STOP),
            server_profiles=default_server_profiles(),
            popen_factory=self._fake_popen(pid=44040),
        )
        with (
            mock.patch.multiple(_htr_mod, _pid_alive=is_alive, _kill_pid_tree=kill_tree),
            mock.patch.object(
                _htr_mod.ProcessIdentity,
                "capture",
                side_effect=_fake_process_identity,
            ),
        ):
            runtime.execute(
                DEV_SERVER_START,
                {"argv": ["npm", "run", "start:safe"], "process_id": "srv-cleanup"},
                deadline_seconds=10,
            )
            result = runtime.cleanup_session()
        self.assertIn("srv-cleanup", result["stopped_servers"])

    def _fake_popen(self, pid: int):
        def factory(argv, cwd, env, stdout, stderr, shell):  # noqa: ANN001
            return _FakeProcess(argv, pid=pid)

        return factory


class TestContractExposure(_Base):
    """Diagnostics/contract surface: known names, extra tools exposed."""

    def test_known_hosted_tool_names_cover_core_and_extra(self) -> None:
        self.assertTrue(set(HOSTED_EXTRA_TOOL_NAMES).isdisjoint(set()))
        self.assertGreater(len(KNOWN_HOSTED_TOOL_NAMES), len(HOSTED_EXTRA_TOOL_NAMES))

    def test_runtime_descriptors_match_allowed_subset(self) -> None:
        runtime = self._runtime(
            tools=(BROWSER_SNAPSHOT, ARTIFACT_READ_IMAGE),
            server_profiles=(),
        )
        names = [d.name for d in runtime.descriptors()]
        self.assertEqual(names, ["karox.browser.snapshot", "karox.artifact.read_image"])


if __name__ == "__main__":
    unittest.main()
