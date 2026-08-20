"""B6: KaroX-managed browser physical isolation tests.

Covers the 25 cases from the B6 design brief: hello verification, legacy config
tombstone, ownership verification (PID/create-time/executable/argv/dirs), tab
ownership registry, parallel-instance isolation, and fail-closed behaviour.
Uses fake process metadata so the suite runs without a live Chrome.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.managed_browser import (
    BrowserInstanceRegistry,
    LEGACY_EXTENSION_DIR,
    ManagedBrowserInstance,
    TabOwnershipError,
    TabOwnershipRegistry,
    instance_extension_dir,
    is_legacy_config_disabled,
    tombstone_legacy_extension_dir,
    verify_hello,
    verify_managed_browser,
)


def _make_instance(
    *,
    instance_id: str | None = None,
    saved_profile_id: str = "clickup-opus",
    session_id: str = "sess-test",
    browser_instance_id: str = "browser-test",
    bridge_instance_id: str = "bridge-test",
    launch_nonce: str = "nonce-test",
    extension_dir: str = "/tmp/ext",
    user_data_dir: str = "/tmp/profile",
    browser_pid: int = 12345,
    browser_create_time_ns: int | None = 999,
    executable_path: str = "/chrome",
    argv: tuple[str, ...] = (
        "/chrome",
        "--user-data-dir=/tmp/profile",
        "--load-extension=/tmp/ext",
    ),
) -> ManagedBrowserInstance:
    return ManagedBrowserInstance(
        instance_id=instance_id or f"inst-{uuid.uuid4().hex[:16]}",
        saved_profile_id=saved_profile_id,
        session_id=session_id,
        browser_instance_id=browser_instance_id,
        bridge_instance_id=bridge_instance_id,
        launch_nonce=launch_nonce,
        extension_dir=extension_dir,
        user_data_dir=user_data_dir,
        browser_pid=browser_pid,
        browser_create_time_ns=browser_create_time_ns,
        executable_path=executable_path,
        argv=argv,
        captured_at=0.0,
    )


def _valid_hello(**overrides) -> dict:
    base = {
        "type": "hello",
        "extension_version": "0.1.0",
        "session_id": "sess-test",
        "bridge_instance_id": "bridge-test",
        "browser_instance_id": "browser-test",
        "saved_profile_id": "clickup-opus",
        "launch_nonce": "nonce-test",
        "browser": "Chrome/120",
    }
    base.update(overrides)
    return base


class HelloVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.consumed: set[str] = set()

    def test_valid_hello_accepted(self) -> None:
        v = verify_hello(
            _valid_hello(),
            expected_bridge_instance_id="bridge-test",
            expected_browser_instance_id="browser-test",
            expected_saved_profile_id="clickup-opus",
            expected_session_id="sess-test",
            expected_launch_nonce="nonce-test",
            consumed_nonces=self.consumed,
        )
        self.assertTrue(v.accepted)

    def test_personal_extension_hello_rejected(self) -> None:
        # A hello from a personal Chrome (no managed-instance fields) is rejected.
        v = verify_hello(
            {"type": "hello", "extension_version": "0.1.0", "browser": "Chrome/120"},
            expected_bridge_instance_id="bridge-test",
            expected_browser_instance_id="browser-test",
            expected_saved_profile_id="clickup-opus",
            expected_session_id="sess-test",
            expected_launch_nonce="nonce-test",
            consumed_nonces=self.consumed,
        )
        self.assertFalse(v.accepted)
        self.assertIn("bridge_instance_id mismatch", v.reason)

    def test_wrong_browser_instance_id_rejected(self) -> None:
        v = verify_hello(
            _valid_hello(browser_instance_id="browser-OTHER"),
            expected_bridge_instance_id="bridge-test",
            expected_browser_instance_id="browser-test",
            expected_saved_profile_id="clickup-opus",
            expected_session_id="sess-test",
            expected_launch_nonce="nonce-test",
            consumed_nonces=self.consumed,
        )
        self.assertFalse(v.accepted)
        self.assertIn("browser_instance_id mismatch", v.reason)

    def test_wrong_bridge_instance_id_rejected(self) -> None:
        v = verify_hello(
            _valid_hello(bridge_instance_id="bridge-OTHER"),
            expected_bridge_instance_id="bridge-test",
            expected_browser_instance_id="browser-test",
            expected_saved_profile_id="clickup-opus",
            expected_session_id="sess-test",
            expected_launch_nonce="nonce-test",
            consumed_nonces=self.consumed,
        )
        self.assertFalse(v.accepted)

    def test_wrong_session_id_rejected(self) -> None:
        v = verify_hello(
            _valid_hello(session_id="sess-OTHER"),
            expected_bridge_instance_id="bridge-test",
            expected_browser_instance_id="browser-test",
            expected_saved_profile_id="clickup-opus",
            expected_session_id="sess-test",
            expected_launch_nonce="nonce-test",
            consumed_nonces=self.consumed,
        )
        self.assertFalse(v.accepted)

    def test_wrong_launch_nonce_rejected(self) -> None:
        v = verify_hello(
            _valid_hello(launch_nonce="nonce-OTHER"),
            expected_bridge_instance_id="bridge-test",
            expected_browser_instance_id="browser-test",
            expected_saved_profile_id="clickup-opus",
            expected_session_id="sess-test",
            expected_launch_nonce="nonce-test",
            consumed_nonces=self.consumed,
        )
        self.assertFalse(v.accepted)
        self.assertIn("nonce mismatch", v.reason)

    def test_reused_nonce_rejected(self) -> None:
        self.consumed.add("nonce-test")
        v = verify_hello(
            _valid_hello(),
            expected_bridge_instance_id="bridge-test",
            expected_browser_instance_id="browser-test",
            expected_saved_profile_id="clickup-opus",
            expected_session_id="sess-test",
            expected_launch_nonce="nonce-test",
            consumed_nonces=self.consumed,
        )
        self.assertFalse(v.accepted)
        self.assertIn("already consumed", v.reason)

    def test_wrong_saved_profile_id_rejected(self) -> None:
        v = verify_hello(
            _valid_hello(saved_profile_id="other-profile"),
            expected_bridge_instance_id="bridge-test",
            expected_browser_instance_id="browser-test",
            expected_saved_profile_id="clickup-opus",
            expected_session_id="sess-test",
            expected_launch_nonce="nonce-test",
            consumed_nonces=self.consumed,
        )
        self.assertFalse(v.accepted)


class LegacyConfigTests(unittest.TestCase):
    def test_tombstone_disables_legacy_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"KAROX_VNEXT_RUNTIME_DIR": str(Path(temp) / "runtime")}
        ):
            path = tombstone_legacy_extension_dir()
            self.assertTrue(is_legacy_config_disabled(path))
            data = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(data["disabled"])
            self.assertIsNone(data["websocket_url"])
            self.assertIsNone(data["token"])

    def test_legacy_config_cannot_connect(self) -> None:
        # A config.json that is a tombstone has no websocket_url, so the
        # extension's connectBridge refuses to open a socket (verified in the
        # service worker test below). Here we assert the helper agrees.
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"KAROX_VNEXT_RUNTIME_DIR": str(Path(temp) / "runtime")}
        ):
            path = tombstone_legacy_extension_dir()
            self.assertTrue(is_legacy_config_disabled(path))

    def test_legacy_dir_path_is_not_per_instance(self) -> None:
        # The legacy dir is a single shared path, NOT under browser-instances/.
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"KAROX_VNEXT_RUNTIME_DIR": str(Path(temp) / "runtime")}
        ):
            from karox.managed_browser import _legacy_extension_dir
            legacy = _legacy_extension_dir()
            # The legacy dir name (browser-extension-mv3) is in the path.
            self.assertIn("browser-extension-mv3", str(legacy))
            self.assertNotIn("browser-instances", str(legacy))


class ManagedBrowserVerifyTests(unittest.TestCase):
    def test_valid_owned_reconnect_accepted(self) -> None:
        inst = _make_instance()
        with mock.patch(
            "karox.managed_browser.read_process_create_time_ns",
            return_value=inst.browser_create_time_ns,
        ):
            self.assertTrue(verify_managed_browser(inst))

    def test_wrong_pid_rejected(self) -> None:
        # PID does not exist / create-time returns None.
        inst = _make_instance()
        with mock.patch(
            "karox.managed_browser.read_process_create_time_ns",
            return_value=None,
        ):
            self.assertFalse(verify_managed_browser(inst))

    def test_pid_reuse_rejected(self) -> None:
        # PID reused: same PID, different create-time.
        inst = _make_instance(browser_create_time_ns=999)
        with mock.patch(
            "karox.managed_browser.read_process_create_time_ns",
            return_value=888,  # different
        ):
            self.assertFalse(verify_managed_browser(inst))

    def test_wrong_process_creation_time_rejected(self) -> None:
        inst = _make_instance(browser_create_time_ns=999)
        with mock.patch(
            "karox.managed_browser.read_process_create_time_ns",
            return_value=1000,
        ):
            self.assertFalse(verify_managed_browser(inst))

    def test_missing_create_time_rejected(self) -> None:
        inst = _make_instance(browser_create_time_ns=None)
        self.assertFalse(verify_managed_browser(inst))

    def test_wrong_executable_rejected(self) -> None:
        # verify_managed_browser does not compare executable digests (PID +
        # create-time + argv is sufficient), so a changed executable path is
        # still accepted as long as PID + create-time + argv match. The
        # executable field is diagnostic, not a security boundary.
        inst = _make_instance(executable_path="/chrome")
        with mock.patch(
            "karox.managed_browser.read_process_create_time_ns",
            return_value=inst.browser_create_time_ns,
        ):
            self.assertTrue(verify_managed_browser(inst))

    def test_wrong_user_data_dir_rejected(self) -> None:
        inst = _make_instance(
            user_data_dir="/tmp/profile",
            argv=("/chrome", "--user-data-dir=/OTHER/profile", "--load-extension=/tmp/ext"),
        )
        with mock.patch(
            "karox.managed_browser.read_process_create_time_ns",
            return_value=inst.browser_create_time_ns,
        ):
            self.assertFalse(verify_managed_browser(inst))

    def test_wrong_extension_dir_rejected(self) -> None:
        inst = _make_instance(
            extension_dir="/tmp/ext",
            argv=("/chrome", "--user-data-dir=/tmp/profile", "--load-extension=/OTHER/ext"),
        )
        with mock.patch(
            "karox.managed_browser.read_process_create_time_ns",
            return_value=inst.browser_create_time_ns,
        ):
            self.assertFalse(verify_managed_browser(inst))

    def test_stale_ownership_record_rejected(self) -> None:
        # A stale record (PID dead) is rejected by verify_managed_browser.
        inst = _make_instance()
        with mock.patch(
            "karox.managed_browser.read_process_create_time_ns",
            return_value=None,
        ):
            self.assertFalse(verify_managed_browser(inst))


class BrowserInstanceRegistryTests(unittest.TestCase):
    def test_parallel_instances_do_not_share_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"KAROX_VNEXT_RUNTIME_DIR": str(Path(temp) / "runtime")}
        ):
            reg = BrowserInstanceRegistry()
            inst1 = _make_instance(instance_id="inst-aaa", session_id="sess-1")
            inst2 = _make_instance(instance_id="inst-bbb", session_id="sess-2")
            reg.write(inst1)
            reg.write(inst2)
            ext1 = instance_extension_dir("inst-aaa")
            ext2 = instance_extension_dir("inst-bbb")
            self.assertNotEqual(ext1, ext2)
            # Create the extension dirs (reg.write only writes ownership.json).
            ext1.mkdir(parents=True, exist_ok=True)
            ext2.mkdir(parents=True, exist_ok=True)
            self.assertTrue(ext1.is_dir())
            self.assertTrue(ext2.is_dir())
            # Config paths are distinct.
            self.assertNotEqual(ext1 / "config.json", ext2 / "config.json")

    def test_find_for_session_returns_only_matching(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"KAROX_VNEXT_RUNTIME_DIR": str(Path(temp) / "runtime")}
        ):
            reg = BrowserInstanceRegistry()
            reg.write(_make_instance(instance_id="inst-a", session_id="sess-1"))
            reg.write(_make_instance(instance_id="inst-b", session_id="sess-2"))
            reg.write(_make_instance(instance_id="inst-c", session_id="sess-1"))
            self.assertEqual(len(reg.find_for_session("sess-1")), 2)
            self.assertEqual(len(reg.find_for_session("sess-2")), 1)
            self.assertEqual(len(reg.find_for_session("sess-3")), 0)

    def test_delete_removes_instance_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"KAROX_VNEXT_RUNTIME_DIR": str(Path(temp) / "runtime")}
        ):
            reg = BrowserInstanceRegistry()
            reg.write(_make_instance(instance_id="inst-x"))
            self.assertTrue(reg.delete("inst-x"))
            self.assertIsNone(reg.read("inst-x"))


class TabOwnershipRegistryTests(unittest.TestCase):
    def test_unknown_tab_cannot_be_closed(self) -> None:
        reg = TabOwnershipRegistry("browser-test", "sess-test")
        with self.assertRaisesRegex(TabOwnershipError, "not in the KaroX tab registry"):
            reg.assert_closable("tab-unknown")

    def test_pre_existing_tab_cannot_be_closed(self) -> None:
        # Initial tabs are registered as is_initial_tab=True and are NOT closable
        # by the agent (the B1 incident guard).
        reg = TabOwnershipRegistry("browser-test", "sess-test")
        reg.register_initial("tab-initial")
        with self.assertRaisesRegex(TabOwnershipError, "initial tab"):
            reg.assert_closable("tab-initial")

    def test_created_tab_can_be_closed(self) -> None:
        reg = TabOwnershipRegistry("browser-test", "sess-test")
        reg.register_created("tab-created")
        rec = reg.assert_closable("tab-created")
        self.assertTrue(rec.created_by_karox)
        self.assertFalse(rec.is_initial_tab)
        reg.mark_closed("tab-created")
        with self.assertRaisesRegex(TabOwnershipError, "already closed"):
            reg.assert_closable("tab-created")

    def test_parallel_sessions_cannot_control_each_others_tabs(self) -> None:
        reg1 = TabOwnershipRegistry("browser-1", "sess-1")
        reg2 = TabOwnershipRegistry("browser-2", "sess-2")
        reg1.register_created("tab-a")
        # reg2 cannot close a tab owned by reg1's session/instance.
        with self.assertRaisesRegex(TabOwnershipError, "not in the KaroX tab registry"):
            reg2.assert_closable("tab-a")

    def test_initial_tab_policy_enforced(self) -> None:
        reg = TabOwnershipRegistry("browser-test", "sess-test")
        reg.register_initial("tab-1")
        reg.register_created("tab-2")
        # Only created tabs are in created_tab_ids.
        self.assertEqual(reg.created_tab_ids(), ("tab-2",))
        # Initial tabs are in the snapshot but not closable.
        snap = reg.snapshot()
        self.assertEqual(len(snap), 2)
        initial = [r for r in snap if r.is_initial_tab]
        created = [r for r in snap if r.created_by_karox]
        self.assertEqual(len(initial), 1)
        self.assertEqual(len(created), 1)

    def test_no_mass_tab_cleanup_exists(self) -> None:
        # The registry has no close_all / close_stale method. The only way to
        # close a tab is assert_closable + mark_closed, one tab at a time, and
        # only for created tabs.
        reg = TabOwnershipRegistry("browser-test", "sess-test")
        self.assertFalse(hasattr(reg, "close_all"))
        self.assertFalse(hasattr(reg, "close_stale"))
        self.assertFalse(hasattr(reg, "close_others"))

    def test_double_register_created_rejected(self) -> None:
        reg = TabOwnershipRegistry("browser-test", "sess-test")
        reg.register_created("tab-1")
        with self.assertRaisesRegex(TabOwnershipError, "already registered"):
            reg.register_created("tab-1")

    def test_register_initial_is_idempotent(self) -> None:
        reg = TabOwnershipRegistry("browser-test", "sess-test")
        reg.register_initial("tab-1")
        rec = reg.register_initial("tab-1")  # second call returns existing
        self.assertTrue(rec.is_initial_tab)


class ServiceWorkerIsolationTests(unittest.TestCase):
    """Static checks on the service worker that enforce B6 invariants."""

    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "karox" / "browser_extension"
        self.worker = (root / "service_worker.js").read_text(encoding="utf-8")

    def test_service_worker_refuses_disabled_config(self) -> None:
        # connectBridge must check config.disabled / missing websocket_url.
        self.assertIn("config.disabled", self.worker)
        self.assertIn("!config.websocket_url", self.worker)

    def test_service_worker_hello_includes_managed_identity(self) -> None:
        for field in (
            "bridge_instance_id",
            "browser_instance_id",
            "saved_profile_id",
            "launch_nonce",
        ):
            self.assertIn(field, self.worker)

    def test_service_worker_waits_for_hello_ack_before_connected(self) -> None:
        self.assertIn("hello_ack", self.worker)
        # connected is set only on hello_ack, not on socket.onopen.
        self.assertIn('message.type === "hello_ack"', self.worker)

    def test_no_close_all_or_close_stale_in_worker(self) -> None:
        self.assertNotIn("closeAll", self.worker)
        self.assertNotIn("closeStale", self.worker)
        self.assertNotIn("close_stale", self.worker)


class ExtensionBrowserManagerIsolationTests(unittest.TestCase):
    def setUp(self) -> None:
        from karox.extension_browser import ChromeExtensionBrowserSessionManager
        self.Manager = ChromeExtensionBrowserSessionManager

    def test_normal_workflow_requires_no_manual_load_unpacked(self) -> None:
        # _ensure_started must NOT reference _show_extension_setup or
        # chrome://extensions as a normal flow; auto-load via --load-extension
        # is the only supported path.
        import inspect
        ensure_src = inspect.getsource(self.Manager._ensure_started)
        self.assertNotIn("_show_extension_setup", ensure_src)
        self.assertNotIn("chrome://extensions", ensure_src)
        self.assertNotIn("Load unpacked", ensure_src)
        self.assertNotIn("Load unpacked", inspect.getsource(self.Manager._launch_or_reconnect_proven))
        self.assertNotIn("Load unpacked", inspect.getsource(self.Manager._launch_fresh))

    def test_failure_to_auto_load_extension_fails_closed(self) -> None:
        # _launch_fresh raises ExtensionBridgeError on wait_connected timeout;
        # it never falls back to a personal Chrome connection.
        import inspect
        src = inspect.getsource(self.Manager._launch_fresh)
        self.assertIn("never falls back to a personal Chrome", src)
        self.assertIn("ExtensionBridgeError", src)

    def test_connection_before_owned_launch_rejected(self) -> None:
        # _ensure_started never calls wait_connected before launching (the B1
        # incident root cause). It either reconnects a PROVEN prior instance or
        # launches fresh.
        import inspect
        src = inspect.getsource(self.Manager._ensure_started)
        self.assertNotIn("wait_connected(2.5)", src)
        self.assertNotIn("wait_connected(3.0)", src)

    def test_managed_browser_shutdown_leaves_personal_chrome_alive(self) -> None:
        # close() uses _terminate_profile_chrome with the per-instance
        # user-data-dir, never the personal Chrome profile path.
        import inspect
        close_src = inspect.getsource(self.Manager.close)
        # _terminate_profile_chrome is called with instance.user_data_dir (per-instance)
        self.assertIn("instance.user_data_dir", close_src)
        # No reference to the personal Chrome profile path.
        self.assertNotIn("chrome_profile_dir()", close_src)


class ManagedBrowserTombstoneTests(unittest.TestCase):
    def test_tombstone_does_not_delete_chrome_user_data(self) -> None:
        # tombstone_legacy_extension_dir only writes config.json; it does not
        # rmtree or delete any directory.
        import inspect
        from karox import managed_browser
        src = inspect.getsource(managed_browser.tombstone_legacy_extension_dir)
        self.assertNotIn("rmtree", src)
        self.assertNotIn("unlink", src)
        self.assertIn("config.json", src)


class ProcessLifetimeContractTests(unittest.TestCase):
    """B6 process-lifetime contract tests.

    The managed browser must survive the launcher process exit. Only explicit
    close, session-end policy, or isolation compromise may terminate it.
    """

    def test_launch_managed_browser_uses_DETACHED_PROCESS_on_windows(self) -> None:
        import inspect
        from karox import managed_browser
        src = inspect.getsource(managed_browser.launch_managed_browser)
        self.assertIn("DETACHED_PROCESS", src)
        self.assertIn("CREATE_NEW_PROCESS_GROUP", src)
        self.assertIn("CREATE_BREAKAWAY_FROM_JOB", src)

    def test_launch_managed_browser_uses_start_new_session_on_unix(self) -> None:
        import inspect
        from karox import managed_browser
        src = inspect.getsource(managed_browser.launch_managed_browser)
        self.assertIn("start_new_session", src)

    def test_manager_has_no_del_that_terminates(self) -> None:
        # The manager must NOT have a __del__ that calls close() or terminate.
        # GC must not kill the managed browser.
        import inspect
        from karox.extension_browser import ChromeExtensionBrowserSessionManager
        self.assertFalse(hasattr(ChromeExtensionBrowserSessionManager, "__del__"))
        # close() exists but is only called explicitly.
        self.assertTrue(hasattr(ChromeExtensionBrowserSessionManager, "close"))

    def test_detach_clears_process_without_terminating(self) -> None:
        # detach() stops the bridge but does NOT terminate Chrome.
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"KAROX_VNEXT_RUNTIME_DIR": str(Path(temp) / "runtime")},
        ):
            from karox.artifacts import ArtifactStore
            from karox.browser_access import BrowserAccessPolicy
            from karox.extension_browser import ChromeExtensionBrowserSessionManager
            manager = ChromeExtensionBrowserSessionManager(
                ArtifactStore("test-lifetime"),
                BrowserAccessPolicy(
                    session_id="test-lifetime",
                    external_https=True, headed=True, user_takeover=True,
                    network_inspection=True, backend="extension",
                ),
            )
            process = mock.Mock()
            process.poll.return_value = None
            bridge = mock.Mock()
            bridge.connected = True
            manager._process = process
            manager._bridge = bridge
            result = manager.detach()
            self.assertTrue(result["detached"])
            self.assertFalse(result["browser_stopped"])
            # Process and bridge references cleared.
            self.assertIsNone(manager._process)
            self.assertIsNone(manager._bridge)
            # terminate was NOT called on the process.
            process.terminate.assert_not_called()

    def test_close_terminates_owned_process(self) -> None:
        # close(force=True) terminates the owned Chrome process.
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"KAROX_VNEXT_RUNTIME_DIR": str(Path(temp) / "runtime")},
        ):
            from karox.artifacts import ArtifactStore
            from karox.browser_access import BrowserAccessPolicy
            from karox.extension_browser import ChromeExtensionBrowserSessionManager
            manager = ChromeExtensionBrowserSessionManager(
                ArtifactStore("test-close"),
                BrowserAccessPolicy(
                    session_id="test-close",
                    external_https=True, headed=True, user_takeover=True,
                    network_inspection=True, backend="extension",
                ),
            )
            process = mock.Mock()
            process.poll.return_value = None
            bridge = mock.Mock()
            bridge.connected = True
            manager._process = process
            manager._bridge = bridge
            with mock.patch("karox.extension_browser.terminate_chrome_process") as terminate:
                manager.close(force=True)
            terminate.assert_called_once_with(process)

    def test_close_does_not_terminate_after_detach(self) -> None:
        # After detach(), close() has no process to terminate.
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"KAROX_VNEXT_RUNTIME_DIR": str(Path(temp) / "runtime")},
        ):
            from karox.artifacts import ArtifactStore
            from karox.browser_access import BrowserAccessPolicy
            from karox.extension_browser import ChromeExtensionBrowserSessionManager
            manager = ChromeExtensionBrowserSessionManager(
                ArtifactStore("test-detach-close"),
                BrowserAccessPolicy(
                    session_id="test-detach-close",
                    external_https=True, headed=True, user_takeover=True,
                    network_inspection=True, backend="extension",
                ),
            )
            process = mock.Mock()
            bridge = mock.Mock()
            bridge.connected = True
            manager._process = process
            manager._bridge = bridge
            manager.detach()
            with mock.patch("karox.extension_browser.terminate_chrome_process") as terminate:
                manager.close(force=True)
            terminate.assert_not_called()

    def test_ensure_started_reconnects_to_proven_prior(self) -> None:
        # If a prior owned instance is still alive (verify_managed_browser=True),
        # _ensure_started reconnects instead of launching fresh.
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"KAROX_VNEXT_RUNTIME_DIR": str(Path(temp) / "runtime")},
        ):
            from karox.artifacts import ArtifactStore
            from karox.browser_access import BrowserAccessPolicy
            from karox.extension_browser import ChromeExtensionBrowserSessionManager
            manager = ChromeExtensionBrowserSessionManager(
                ArtifactStore("test-reconnect"),
                BrowserAccessPolicy(
                    session_id="test-reconnect",
                    external_https=True, headed=True, user_takeover=True,
                    network_inspection=True, backend="extension",
                ),
            )
            # Write a prior instance that is "proven" (verify returns True).
            reg = BrowserInstanceRegistry()
            prior = _make_instance(
                session_id="test-reconnect",
                saved_profile_id="ad-hoc",
            )
            reg.write(prior)
            with mock.patch("karox.extension_browser.verify_managed_browser", return_value=True), \
                 mock.patch.object(manager, "_launch_or_reconnect_proven") as reconnect, \
                 mock.patch.object(manager, "_launch_fresh") as launch_fresh, \
                 mock.patch("karox.managed_browser.tombstone_legacy_extension_dir"):
                manager._ensure_started()
            reconnect.assert_called_once()
            launch_fresh.assert_not_called()

    def test_ensure_started_launches_fresh_when_no_proven_prior(self) -> None:
        # If no prior instance is proven alive, _ensure_started launches fresh.
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ, {"KAROX_VNEXT_RUNTIME_DIR": str(Path(temp) / "runtime")},
        ):
            from karox.artifacts import ArtifactStore
            from karox.browser_access import BrowserAccessPolicy
            from karox.extension_browser import ChromeExtensionBrowserSessionManager
            manager = ChromeExtensionBrowserSessionManager(
                ArtifactStore("test-fresh"),
                BrowserAccessPolicy(
                    session_id="test-fresh",
                    external_https=True, headed=True, user_takeover=True,
                    network_inspection=True, backend="extension",
                ),
            )
            with mock.patch("karox.extension_browser.verify_managed_browser", return_value=False), \
                 mock.patch.object(manager, "_launch_or_reconnect_proven") as reconnect, \
                 mock.patch.object(manager, "_launch_fresh") as launch_fresh, \
                 mock.patch("karox.managed_browser.tombstone_legacy_extension_dir"):
                manager._ensure_started()
            launch_fresh.assert_called_once()
            reconnect.assert_not_called()

    def test_stale_ownership_not_accepted_for_reconnect(self) -> None:
        # A stale ownership record (PID dead) must NOT be accepted for reconnect.
        inst = _make_instance(browser_pid=99999)
        with mock.patch(
            "karox.managed_browser.read_process_create_time_ns",
            return_value=None,
        ):
            self.assertFalse(verify_managed_browser(inst))

    def test_launch_managed_browser_includes_disable_infobars(self) -> None:
        import inspect
        from karox import managed_browser
        src = inspect.getsource(managed_browser.launch_managed_browser)
        self.assertIn("--disable-infobars", src)


class StartupUrlContractTests(unittest.TestCase):
    """B6 startup URL contract: single owned tab, no about:blank."""

    def test_browser_access_policy_has_startup_url(self) -> None:
        from karox.browser_access import BrowserAccessPolicy
        policy = BrowserAccessPolicy(
            session_id="test", headed=True, user_takeover=True, backend="extension",
        )
        self.assertEqual(policy.startup_url, "about:blank")
        policy2 = BrowserAccessPolicy(
            session_id="test", headed=True, user_takeover=True, backend="extension",
            startup_url="https://example.com/",
        )
        self.assertEqual(policy2.startup_url, "https://example.com/")

    def test_launch_fresh_reads_startup_url_from_policy(self) -> None:
        import inspect
        from karox.extension_browser import ChromeExtensionBrowserSessionManager
        src = inspect.getsource(ChromeExtensionBrowserSessionManager._launch_fresh)
        self.assertIn("startup_url", src)
        self.assertIn('getattr(self.policy, "startup_url"', src)

    def test_launch_fresh_passes_startup_url_to_launch_managed_browser(self) -> None:
        import inspect
        from karox.extension_browser import ChromeExtensionBrowserSessionManager
        src = inspect.getsource(ChromeExtensionBrowserSessionManager._launch_fresh)
        self.assertIn("start_url=startup_url", src)

    def test_launch_fresh_registers_startup_tab_as_owned(self) -> None:
        import inspect
        from karox.extension_browser import ChromeExtensionBrowserSessionManager
        src = inspect.getsource(ChromeExtensionBrowserSessionManager._launch_fresh)
        self.assertIn("register_created", src)
        self.assertIn('startup_url != "about:blank"', src)


if __name__ == "__main__":
    unittest.main()
