"""Contract tests for the dedicated KaroX Chrome-extension browser backend."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from karox.artifacts import ArtifactStore
from karox.browser_access import BrowserAccessPolicy, SecureBrowserSessionManager
from karox.browser_session import BrowserSecurityError
from karox.extension_browser import (
    ChromeExtensionBrowserSessionManager,
    _launch_extension_chrome,
    _prepare_extension,
)
from karox.hosted_bridge import HostedBridgeAccessDenied
from karox.hosted_tools_runtime import (
    BROWSER_COMMAND,
    HostedToolsRuntime,
    _BROWSER_MANAGERS,
    _HOSTED_EXTRA_TOOLS,
    _browser_manager_for_session,
    _release_browser_manager,
)
from karox.models import Capability
from karox.policy import PolicyDenied
from karox.workspace_worker import execute_browser_command


class _FakeBridgeConfig:
    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.websocket_url = "ws://127.0.0.1:45678/extension"
        self.token = "test-token-not-a-real-credential"


class ExtensionStaticContractTests(unittest.TestCase):
    def test_manifest_is_mv3_without_debugger_or_incognito_access(self) -> None:
        root = Path(__file__).parents[1] / "src" / "karox" / "browser_extension"
        manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertGreaterEqual(int(manifest["minimum_chrome_version"]), 116)
        self.assertIn("tabs", manifest["permissions"])
        self.assertIn("scripting", manifest["permissions"])
        self.assertIn("sidePanel", manifest["permissions"])
        self.assertNotIn("debugger", manifest["permissions"])
        self.assertEqual(manifest["incognito"], "not_allowed")
        self.assertEqual(manifest["chrome_url_overrides"]["newtab"], "newtab.html")

    def test_service_worker_keeps_agent_navigation_in_background_tabs(self) -> None:
        root = Path(__file__).parents[1] / "src" / "karox" / "browser_extension"
        worker = (root / "service_worker.js").read_text(encoding="utf-8")
        self.assertIn("new WebSocket", worker)
        self.assertIn("active: false", worker)
        self.assertIn("state.agentTabId", worker)
        self.assertIn("chrome.sidePanel", worker)
        self.assertNotIn("chrome.debugger", worker)

    def test_prepare_extension_copies_assets_and_writes_session_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ,
            {"KAROX_VNEXT_RUNTIME_DIR": str(Path(temp) / "runtime")},
        ):
            session_id = f"ext-copy-{uuid.uuid4().hex}"
            target = _prepare_extension(_FakeBridgeConfig(session_id))  # type: ignore[arg-type]
            manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            config = json.loads((target / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["name"], "KaroX Browser")
            self.assertEqual(config["session_id"], session_id)
            self.assertEqual(config["websocket_url"], "ws://127.0.0.1:45678/extension")
            self.assertEqual(config["token"], "test-token-not-a-real-credential")
            self.assertTrue((target / "sidepanel.html").is_file())
            self.assertTrue((target / "newtab.css").is_file())


class ExtensionBackendPolicyTests(unittest.TestCase):
    def test_backend_defaults_to_playwright(self) -> None:
        policy = BrowserAccessPolicy(session_id="default-backend")
        self.assertEqual(policy.backend, "playwright")

    def test_extension_requires_visible_takeover_mode(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires headed user takeover"):
            BrowserAccessPolicy(session_id="bad-extension", backend="extension")

    def test_manager_selection_is_explicit_not_inferred_from_headed(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ,
            {"KAROX_VNEXT_RUNTIME_DIR": str(Path(temp) / "runtime")},
        ):
            playwright_id = f"playwright-{uuid.uuid4().hex}"
            playwright_policy = BrowserAccessPolicy(
                session_id=playwright_id,
                headed=True,
                user_takeover=True,
                backend="playwright",
            )
            playwright = _browser_manager_for_session(
                ArtifactStore(playwright_id), playwright_policy
            )
            self.assertIsInstance(playwright, SecureBrowserSessionManager)
            playwright.close(force=True)

            extension_id = f"extension-{uuid.uuid4().hex}"
            extension_policy = BrowserAccessPolicy(
                session_id=extension_id,
                headed=True,
                user_takeover=True,
                backend="extension",
            )
            extension = _browser_manager_for_session(
                ArtifactStore(extension_id), extension_policy
            )
            self.assertIsInstance(extension, ChromeExtensionBrowserSessionManager)
            extension.close(force=True)


class ExtensionManagerSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(
            os.environ,
            {"KAROX_VNEXT_RUNTIME_DIR": str(Path(self.temp.name) / "runtime")},
        )
        self.environment.start()
        self.session_id = f"extension-safety-{uuid.uuid4().hex}"
        self.manager = ChromeExtensionBrowserSessionManager(
            ArtifactStore(self.session_id),
            BrowserAccessPolicy(
                session_id=self.session_id,
                external_https=True,
                headed=True,
                user_takeover=True,
                network_inspection=True,
                backend="extension",
            ),
        )

    def tearDown(self) -> None:
        self.manager.close(force=True)
        self.environment.stop()
        self.temp.cleanup()

    def test_password_fill_is_blocked_before_extension_input(self) -> None:
        self.manager._call = mock.Mock(  # type: ignore[method-assign]
            return_value={
                "type": "password",
                "name": "password",
                "id": "password",
                "aria": "Password",
                "text": "",
                "context": "Sign in",
            }
        )
        with self.assertRaisesRegex(BrowserSecurityError, "must be completed through user takeover"):
            self.manager.fill({"selector": "#password", "value": "not-sent"}, 5)
        self.manager._call.assert_called_once_with(
            "inspect", {"selector": "#password"}, 5
        )

    def test_screenshot_png_is_stored_as_session_artifact(self) -> None:
        png = b"\x89PNG\r\n\x1a\nKaroX-test"
        self.manager._call = mock.Mock(  # type: ignore[method-assign]
            return_value={
                "data_url": "data:image/png;base64," + base64.b64encode(png).decode("ascii"),
                "tab_id": "tab-42",
            }
        )
        result = self.manager.screenshot({"name": "extension-shot"}, 5)
        stored, record = self.manager._artifacts.read(result["artifact_id"])
        self.assertEqual(stored, png)
        self.assertEqual(record.mime, "image/png")
        self.assertEqual(result["tab_id"], "tab-42")
        self.assertTrue(result["viewport_only"])

    def test_takeover_blocks_agent_input_without_closing_profile(self) -> None:
        self.manager._ensure_started = mock.Mock()  # type: ignore[method-assign]
        self.manager._call = mock.Mock(  # type: ignore[method-assign]
            return_value={"tab_id": "tab-9", "url": "https://example.com/"}
        )
        takeover = self.manager.request_user_takeover({"reason": "Google login"}, 5)
        self.assertTrue(takeover["agent_input_paused"])
        self.assertTrue(self.manager.takeover_active)
        with self.assertRaisesRegex(BrowserSecurityError, "user has control"):
            self.manager.click({"selector": "button"}, 5)

    def test_detach_stops_bridge_without_terminating_chrome(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        bridge = mock.Mock()
        bridge.connected = True
        self.manager._process = process
        self.manager._bridge = bridge
        with mock.patch("karox.extension_browser.terminate_chrome_process") as terminate:
            result = self.manager.detach()
        bridge.stop.assert_called_once_with()
        terminate.assert_not_called()
        self.assertTrue(result["detached"])
        self.assertFalse(result["browser_stopped"])
        self.assertIsNone(self.manager._process)
        self.assertIsNone(self.manager._bridge)

    def test_existing_extension_reconnects_without_second_chrome(self) -> None:
        session_id = f"reconnect-{uuid.uuid4().hex}"
        manager = ChromeExtensionBrowserSessionManager(
            ArtifactStore(session_id),
            BrowserAccessPolicy(
                session_id=session_id,
                headed=True,
                user_takeover=True,
                backend="extension",
            ),
        )
        bridge = mock.Mock()
        bridge.connected = True
        with tempfile.TemporaryDirectory() as temp:
            extension = Path(temp) / "extension"
            extension.mkdir()
            with (
                mock.patch(
                    "karox.extension_browser._ExtensionBridgeServer",
                    return_value=bridge,
                ),
                mock.patch(
                    "karox.extension_browser._prepare_extension",
                    return_value=extension,
                ),
                mock.patch(
                    "karox.extension_browser.chrome_profile_dir",
                    return_value=Path(temp) / "profile",
                ),
                mock.patch("karox.extension_browser._launch_extension_chrome") as launch,
            ):
                manager._ensure_started()
        bridge.start.assert_called_once_with()
        bridge.wait_connected.assert_called_once_with(2.5)
        launch.assert_not_called()
        self.assertTrue(manager.is_open)
        self.assertIsNone(manager._process)
        manager.detach()

    def test_explicit_close_stops_attached_profile(self) -> None:
        profile = Path(self.temp.name) / "attached-profile"
        bridge = mock.Mock()
        bridge.connected = True
        self.manager._bridge = bridge
        self.manager._process = None
        self.manager._profile_dir = profile
        with mock.patch(
            "karox.extension_browser._terminate_profile_chrome",
            return_value=True,
        ) as terminate:
            result = self.manager.close(force=True)
        terminate.assert_called_once_with(profile)
        bridge.stop.assert_called_once_with()
        self.assertTrue(result["browser_stopped"])

    def test_release_manager_detaches_extension_backend(self) -> None:
        _BROWSER_MANAGERS[self.session_id] = (self.manager.policy, self.manager)
        try:
            with mock.patch.object(
                self.manager,
                "detach",
                return_value={"detached": True},
            ) as detach:
                result = _release_browser_manager(self.session_id, self.manager)
        finally:
            _BROWSER_MANAGERS.pop(self.session_id, None)
        self.assertEqual(result, {"detached": True})
        detach.assert_called_once_with()


class ExtensionLaunchIsolationTests(unittest.TestCase):
    def test_chrome_escapes_bridge_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            executable = root / ("chrome.exe" if os.name == "nt" else "chrome")
            executable.write_bytes(b"")
            extension = root / "extension"
            extension.mkdir()
            process = mock.Mock()
            with (
                mock.patch(
                    "karox.extension_browser.find_system_chrome",
                    return_value=executable,
                ),
                mock.patch(
                    "karox.extension_browser.chrome_profile_dir",
                    return_value=root / "profile",
                ),
                mock.patch(
                    "karox.extension_browser.runtime_dir",
                    return_value=root / "runtime",
                ),
                mock.patch(
                    "karox.extension_browser.subprocess.Popen",
                    return_value=process,
                ) as popen,
            ):
                launched, _profile = _launch_extension_chrome(
                    extension,
                    width=1200,
                    height=800,
                )
        self.assertIs(launched, process)
        options = popen.call_args.kwargs
        if os.name == "nt":
            breakaway = getattr(subprocess, "CREATE_BREAKAWAY_FROM_JOB", 0)
            self.assertNotEqual(breakaway, 0)
            self.assertTrue(options["creationflags"] & breakaway)
        else:
            self.assertTrue(options["start_new_session"])


class _CommandBrowser:
    def __init__(self) -> None:
        self.takeover_active = False
        self.calls: list[tuple[str, dict[str, object], float]] = []

    def snapshot(self, payload: dict[str, object], deadline: float) -> dict[str, object]:
        self.calls.append(("snapshot", payload, deadline))
        return {"tab_id": "tab-command", "title": "KaroX"}

    def close(self) -> dict[str, object]:
        self.calls.append(("close", {}, 0.0))
        return {"closed": True}

    def __getattr__(self, name: str):
        def method(payload: dict[str, object], deadline: float) -> dict[str, object]:
            self.calls.append((name, payload, deadline))
            return {"method": name}

        return method


class BrowserCommandContractTests(unittest.TestCase):
    def test_stable_descriptor_is_read_capable_but_not_declared_read_only(self) -> None:
        meta = _HOSTED_EXTRA_TOOLS[BROWSER_COMMAND]
        self.assertEqual(meta.capability, Capability.BROWSER_READ)
        self.assertFalse(meta.read_only)

    def test_hosted_command_requires_capability_for_each_action(self) -> None:
        runtime = object.__new__(HostedToolsRuntime)
        runtime.hosted_origin = object()  # type: ignore[assignment]
        runtime._browser = _CommandBrowser()
        required: list[Capability] = []

        class _Policy:
            def require(self, origin: object, capability: Capability) -> None:
                del origin
                required.append(capability)
                if capability is Capability.BROWSER_INPUT:
                    raise PolicyDenied("input denied")

        runtime.policy = _Policy()  # type: ignore[assignment]
        snapshot = HostedToolsRuntime._browser_command(
            runtime,
            {"action": "snapshot", "payload": {}},
            12.0,
        )
        self.assertEqual(snapshot["tab_id"], "tab-command")
        self.assertEqual(required, [Capability.BROWSER_READ])
        with self.assertRaisesRegex(HostedBridgeAccessDenied, "input denied"):
            HostedToolsRuntime._browser_command(
                runtime,
                {"action": "click", "payload": {"selector": "button"}},
                12.0,
            )
        self.assertEqual(
            required,
            [Capability.BROWSER_READ, Capability.BROWSER_INPUT],
        )

    def test_stable_command_dispatches_without_a_new_public_schema(self) -> None:
        runtime = mock.Mock()
        runtime._browser = _CommandBrowser()
        result = execute_browser_command(
            runtime,
            {"action": "snapshot", "payload": {}},
            12.0,
        )
        self.assertEqual(result["action"], "snapshot")
        self.assertEqual(result["tab_id"], "tab-command")
        self.assertEqual(runtime._browser.calls, [("snapshot", {}, 12.0)])

    def test_stable_command_keeps_close_explicit_and_takeover_safe(self) -> None:
        runtime = mock.Mock()
        browser = _CommandBrowser()
        runtime._browser = browser
        with self.assertRaisesRegex(BrowserSecurityError, "explicit user confirmation"):
            execute_browser_command(
                runtime,
                {"action": "close", "payload": {}},
                5.0,
            )
        browser.takeover_active = True
        with self.assertRaisesRegex(BrowserSecurityError, "takeover is active"):
            execute_browser_command(
                runtime,
                {
                    "action": "close",
                    "payload": {"user_confirmed": True},
                },
                5.0,
            )


if __name__ == "__main__":
    unittest.main()
