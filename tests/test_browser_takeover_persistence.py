from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from _support import ROOT  # noqa: F401 - inserts src on sys.path

from karox.artifacts import ArtifactStore
from karox.browser_access import BrowserAccessPolicy
from karox.browser_session import BrowserSecurityError
from karox.extension_browser import ChromeExtensionBrowserSessionManager
from karox.managed_browser import BrowserInstanceRegistry, ManagedBrowserInstance


def _instance(
    *,
    instance_id: str = "inst-takeover",
    saved_profile_id: str = "chatgpt-dev",
    session_id: str = "takeover-session",
    takeover_active: bool = False,
) -> ManagedBrowserInstance:
    return ManagedBrowserInstance(
        instance_id=instance_id,
        saved_profile_id=saved_profile_id,
        session_id=session_id,
        browser_instance_id=f"browser-{instance_id}",
        bridge_instance_id=f"bridge-{instance_id}",
        launch_nonce=f"nonce-{instance_id}",
        extension_dir=f"/tmp/{instance_id}/extension",
        user_data_dir=f"/tmp/{instance_id}/profile",
        browser_pid=12345,
        browser_create_time_ns=999,
        executable_path="/chrome",
        argv=(
            "/chrome",
            f"--user-data-dir=/tmp/{instance_id}/profile",
            f"--load-extension=/tmp/{instance_id}/extension",
        ),
        captured_at=1.0,
        takeover_active=takeover_active,
    )


class ManagedBrowserTakeoverPersistenceTests(unittest.TestCase):
    def test_old_ownership_record_without_takeover_field_defaults_to_false(self) -> None:
        value = _instance().to_dict()
        value.pop("takeover_active", None)
        loaded = ManagedBrowserInstance.from_dict(value)
        self.assertFalse(loaded.takeover_active)

    def test_takeover_field_round_trips_through_registry(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ,
            {"KAROX_VNEXT_RUNTIME_DIR": str(Path(temp) / "runtime")},
        ):
            registry = BrowserInstanceRegistry()
            registry.write(_instance(takeover_active=True))
            loaded = registry.read("inst-takeover")
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertTrue(loaded.takeover_active)


class ExtensionTakeoverRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(
            os.environ,
            {"KAROX_VNEXT_RUNTIME_DIR": str(Path(self.temporary.name) / "runtime")},
        )
        self.environment.start()
        self.session_id = "takeover-session"
        self.policy = BrowserAccessPolicy(
            session_id=self.session_id,
            external_https=True,
            headed=True,
            user_takeover=True,
            network_inspection=True,
            backend="extension",
            saved_profile_id="chatgpt-dev",
        )
        self.manager = ChromeExtensionBrowserSessionManager(
            ArtifactStore(self.session_id),
            self.policy,
        )

    def tearDown(self) -> None:
        self.manager.close(force=True)
        self.environment.stop()
        self.temporary.cleanup()

    def test_request_persists_pause_before_extension_takeover_call(self) -> None:
        self.manager._ensure_started = mock.Mock()  # type: ignore[method-assign]
        self.manager._instance = _instance()
        calls: list[tuple[str, bool]] = []

        def write(instance: ManagedBrowserInstance) -> Path:
            calls.append(("persist", instance.takeover_active))
            return Path("ownership.json")

        def extension_call(method: str, _params: object, _deadline: float) -> dict[str, object]:
            calls.append((method, self.manager.takeover_active))
            return {"overlay_ack": True}

        self.manager._registry.write = mock.Mock(side_effect=write)  # type: ignore[method-assign]
        self.manager._call = mock.Mock(side_effect=extension_call)  # type: ignore[method-assign]

        result = self.manager.request_user_takeover({"reason": "2FA"}, 5)

        self.assertTrue(result["agent_input_paused"])
        self.assertEqual(calls[0], ("persist", True))
        self.assertEqual(calls[1], ("takeover", True))
        self.assertTrue(self.manager.takeover_active)
        assert self.manager._instance is not None
        self.assertTrue(self.manager._instance.takeover_active)

    def test_explicit_visual_takeover_failure_clears_durable_pause(self) -> None:
        self.manager._ensure_started = mock.Mock()  # type: ignore[method-assign]
        self.manager._instance = _instance()
        persisted: list[bool] = []

        def write(instance: ManagedBrowserInstance) -> Path:
            persisted.append(instance.takeover_active)
            return Path("ownership.json")

        self.manager._registry.write = mock.Mock(side_effect=write)  # type: ignore[method-assign]
        self.manager._call = mock.Mock(  # type: ignore[method-assign]
            return_value={
                "visual_takeover_failed": True,
                "agent_input_paused": False,
            }
        )

        result = self.manager.request_user_takeover({"reason": "2FA"}, 5)

        self.assertFalse(result["takeover"])
        self.assertFalse(result["agent_input_paused"])
        self.assertEqual(persisted, [True, False])
        self.assertFalse(self.manager.takeover_active)
        assert self.manager._instance is not None
        self.assertFalse(self.manager._instance.takeover_active)

    def test_resume_clears_pause_only_after_extension_ack_and_persist(self) -> None:
        self.manager._ensure_started = mock.Mock()  # type: ignore[method-assign]
        self.manager._instance = _instance(takeover_active=True)
        self.manager._takeover_active = True
        calls: list[tuple[str, bool]] = []

        def write(instance: ManagedBrowserInstance) -> Path:
            calls.append(("persist", instance.takeover_active))
            return Path("ownership.json")

        def extension_call(method: str, _params: object, _deadline: float) -> dict[str, object]:
            calls.append((method, self.manager.takeover_active))
            return {"overlay_ack": True}

        self.manager._registry.write = mock.Mock(side_effect=write)  # type: ignore[method-assign]
        self.manager._call = mock.Mock(side_effect=extension_call)  # type: ignore[method-assign]

        result = self.manager.resume_after_user_takeover({}, 5)

        self.assertFalse(result["agent_input_paused"])
        self.assertEqual(calls[0], ("resume", True))
        self.assertEqual(calls[1], ("persist", False))
        self.assertFalse(self.manager.takeover_active)
        assert self.manager._instance is not None
        self.assertFalse(self.manager._instance.takeover_active)

    def test_reconnect_restores_durable_takeover_before_agent_input(self) -> None:
        prior = _instance(takeover_active=True)
        bridge = mock.Mock()
        bridge.connected = True
        with (
            mock.patch("karox.extension_browser._ExtensionBridgeServer", return_value=bridge),
            mock.patch("karox.extension_browser._prepare_extension"),
        ):
            self.manager._launch_or_reconnect_proven(prior, 1440, 900)

        self.assertTrue(self.manager.takeover_active)
        bridge.wait_connected.assert_called_once_with(15.0)
        with self.assertRaisesRegex(BrowserSecurityError, "user has control"):
            self.manager._assert_agent_input_allowed()

    def test_pre_reconnect_input_guard_reads_durable_takeover(self) -> None:
        prior = _instance(takeover_active=True)
        with (
            mock.patch.object(
                self.manager._registry,
                "find_for_session",
                return_value=(prior,),
            ),
            mock.patch(
                "karox.extension_browser.verify_managed_browser",
                return_value=True,
            ) as verify,
        ):
            with self.assertRaisesRegex(BrowserSecurityError, "user has control"):
                self.manager._assert_agent_input_allowed()

        verify.assert_called_once_with(prior)
        self.assertIsNone(self.manager._bridge)
        self.assertIsNone(self.manager._instance)

    def test_resume_after_child_crash_reconnects_before_clearing_durable_pause(self) -> None:
        prior = _instance(takeover_active=True)
        bridge = mock.Mock()
        bridge.connected = True
        bridge.call.return_value = {"overlay_ack": True}
        with (
            mock.patch.object(
                self.manager._registry,
                "find_for_session",
                return_value=(prior,),
            ),
            mock.patch("karox.extension_browser.verify_managed_browser", return_value=True),
            mock.patch("karox.extension_browser._ExtensionBridgeServer", return_value=bridge),
            mock.patch("karox.extension_browser._prepare_extension"),
        ):
            result = self.manager.resume_after_user_takeover({}, 5)

        self.assertTrue(result["resumed"])
        self.assertFalse(result["agent_input_paused"])
        bridge.wait_connected.assert_called_once_with(15.0)
        bridge.call.assert_called_once_with("resume", {}, 5.0)
        self.assertFalse(self.manager.takeover_active)

    def test_reconnect_uses_only_matching_saved_profile_for_same_session(self) -> None:
        matching = _instance(instance_id="inst-matching", saved_profile_id="chatgpt-dev")
        foreign = _instance(instance_id="inst-foreign", saved_profile_id="claude-dev")
        with (
            mock.patch.object(
                self.manager._registry,
                "find_for_session",
                return_value=(foreign, matching),
            ),
            mock.patch(
                "karox.extension_browser.verify_managed_browser",
                side_effect=lambda item: item.instance_id == "inst-matching",
            ) as verify,
            mock.patch.object(self.manager, "_launch_or_reconnect_proven") as reconnect,
            mock.patch.object(self.manager, "_launch_fresh") as fresh,
            mock.patch("karox.extension_browser.tombstone_legacy_extension_dir"),
        ):
            self.manager._ensure_started()

        reconnect.assert_called_once_with(matching, 1440, 900)
        fresh.assert_not_called()
        self.assertEqual([call.args[0].instance_id for call in verify.call_args_list], ["inst-matching"])

    def test_stale_cleanup_never_deletes_foreign_saved_profile_record(self) -> None:
        matching = _instance(instance_id="inst-matching", saved_profile_id="chatgpt-dev")
        foreign = _instance(instance_id="inst-foreign", saved_profile_id="claude-dev")
        with (
            mock.patch.object(
                self.manager._registry,
                "find_for_session",
                return_value=(foreign, matching),
            ),
            mock.patch("karox.extension_browser.verify_managed_browser", return_value=False),
            mock.patch.object(self.manager._registry, "delete", return_value=True) as delete,
            mock.patch.object(self.manager, "_launch_fresh") as fresh,
            mock.patch("karox.extension_browser.tombstone_legacy_extension_dir"),
        ):
            self.manager._ensure_started()

        delete.assert_called_once_with("inst-matching")
        fresh.assert_called_once_with(1440, 900)


if __name__ == "__main__":
    unittest.main()
