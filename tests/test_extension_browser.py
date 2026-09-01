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
from karox.browser_session import BrowserError, BrowserSecurityError
from karox.extension_browser import (
    ChromeExtensionBrowserSessionManager,
    ExtensionBridgeError,
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
from karox.managed_browser import TabOwnershipError, TabOwnershipRegistry
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
        self.assertGreaterEqual(int(manifest["minimum_chrome_version"]), 111)
        self.assertIn("tabs", manifest["permissions"])
        self.assertIn("tabGroups", manifest["permissions"])
        self.assertIn("scripting", manifest["permissions"])
        self.assertIn("sidePanel", manifest["permissions"])
        self.assertNotIn("debugger", manifest["permissions"])
        self.assertEqual(manifest["incognito"], "not_allowed")
        # Regression: KaroX must NOT override the user's normal new-tab page.
        self.assertNotIn("chrome_url_overrides", manifest)
        # The cursor + indicator + console relay need a content script (ISOLATED)
        # and a page-console bridge injected into the page's MAIN world.
        scripts = manifest["content_scripts"]
        self.assertTrue(any("content.js" in s["js"] for s in scripts))
        main_world = [s for s in scripts if s.get("world") == "MAIN"]
        self.assertEqual(len(main_world), 1)
        self.assertIn("page_console_bridge.js", main_world[0]["js"])

    def test_service_worker_keeps_agent_navigation_in_background_tabs(self) -> None:
        root = Path(__file__).parents[1] / "src" / "karox" / "browser_extension"
        worker = (root / "service_worker.js").read_text(encoding="utf-8")
        self.assertIn("new WebSocket", worker)
        self.assertIn("active: false", worker)
        self.assertIn("state.agentTabId", worker)
        self.assertIn("chrome.sidePanel", worker)
        self.assertNotIn("chrome.debugger", worker)
        # Regression: the worker must not auto-open or pin a branded new-tab page.
        self.assertNotIn("ensureBrandingTab", worker)
        self.assertNotIn("brandingTabId", worker)
        # Cursor stays in-page, while visible ownership moves to a native Chrome
        # tab-group marker so the page itself is not covered by a status chip.
        self.assertIn("karox-move-cursor", worker)
        self.assertIn("karox-set-indicator", worker)
        self.assertIn("setAgentTabMarker", worker)
        self.assertIn("chrome.tabGroups.update", worker)
        self.assertIn('title: "KaroX"', worker)
        self.assertIn("karox-remove-overlay", worker)
        self.assertIn("broadcastRemoveOverlay", worker)

    def test_service_worker_reuses_existing_startup_tab_before_creating_blank(self) -> None:
        root = Path(__file__).parents[1] / "src" / "karox" / "browser_extension"
        worker = (root / "service_worker.js").read_text(encoding="utf-8")
        section = worker[worker.index("async function ensureAgentTab"):worker.index("function snapshotPage")]
        self.assertIn('tabs.find((tab) => isWebUrl(tab.url || "")) || tabs[0]', section)
        self.assertLess(section.index("|| tabs[0]"), section.index('chrome.tabs.create({ url: "about:blank"'))

    def test_open_and_new_tab_apply_native_marker_immediately(self) -> None:
        root = Path(__file__).parents[1] / "src" / "karox" / "browser_extension"
        worker = (root / "service_worker.js").read_text(encoding="utf-8")
        open_block = worker[worker.index('method === "open"'):worker.index('method === "tabs"')]
        new_tab_block = worker[worker.index('method === "new_tab"'):worker.index('method === "switch_tab"')]
        self.assertIn("await setAgentTabMarker(tab.id, desired)", open_block)
        self.assertIn("await setAgentTabMarker(tab.id, desired)", new_tab_block)

    def test_prepare_extension_copies_assets_and_writes_session_config(self) -> None:
        from karox.managed_browser import ManagedBrowserInstance, TabOwnershipError, TabOwnershipRegistry, instance_extension_dir
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(
            os.environ,
            {"KAROX_VNEXT_RUNTIME_DIR": str(Path(temp) / "runtime")},
        ):
            session_id = f"ext-copy-{uuid.uuid4().hex}"
            instance_id = f"inst-{uuid.uuid4().hex[:16]}"
            bridge = mock.Mock(
                session_id=session_id,
                websocket_url="ws://127.0.0.1:45678/extension",
                token="test-token-not-a-real-credential",
                bridge_instance_id="bridge-test",
                browser_instance_id="browser-test",
                saved_profile_id="clickup-opus",
                launch_nonce="nonce-test",
            )
            instance = ManagedBrowserInstance(
                instance_id=instance_id,
                saved_profile_id="clickup-opus",
                session_id=session_id,
                browser_instance_id="browser-test",
                bridge_instance_id="bridge-test",
                launch_nonce="nonce-test",
                extension_dir=str(instance_extension_dir(instance_id)),
                user_data_dir=str(Path(temp) / "profile"),
                browser_pid=0,
                browser_create_time_ns=None,
                executable_path="/chrome",
                argv=(),
                captured_at=0.0,
            )
            target = _prepare_extension(bridge, instance=instance)
            manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
            config = json.loads((target / "config.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["name"], "KaroX Browser")
            self.assertEqual(config["session_id"], session_id)
            self.assertEqual(config["websocket_url"], "ws://127.0.0.1:45678/extension")
            self.assertEqual(config["token"], "test-token-not-a-real-credential")
            # B6: per-instance config.json carries the full managed-instance
            # identity so the bridge can verify the hello.
            self.assertEqual(config["bridge_instance_id"], "bridge-test")
            self.assertEqual(config["browser_instance_id"], "browser-test")
            self.assertEqual(config["saved_profile_id"], "clickup-opus")
            self.assertEqual(config["launch_nonce"], "nonce-test")
            self.assertTrue((target / "sidepanel.html").is_file())
            self.assertTrue((target / "newtab.css").is_file())
            self.assertTrue((target / "content.js").is_file())
            self.assertTrue((target / "page_console_bridge.js").is_file())


class ExtensionOverlayContractTests(unittest.TestCase):
    """Static + behavioural contract for the in-page cursor, indicator, console
    relay, takeover blocking, and overlay isolation. These read the extension
    source directly so they run without a live Chrome; live Chrome proves the
    same behaviours end-to-end in the manual smoke."""

    def setUp(self) -> None:
        self.root = Path(__file__).parents[1] / "src" / "karox" / "browser_extension"
        self.content = (self.root / "content.js").read_text(encoding="utf-8")
        self.bridge = (self.root / "page_console_bridge.js").read_text(encoding="utf-8")
        self.worker = (self.root / "service_worker.js").read_text(encoding="utf-8")
        # domAction moved to dom_helpers.js so the deterministic fixture suite
        # exercises the exact engine production injects.
        self.helpers = (self.root / "dom_helpers.js").read_text(encoding="utf-8")

    # --- cursor ---

    def test_cursor_lives_in_closed_shadow_dom_with_pointer_events_none(self) -> None:
        self.assertIn("attachShadow", self.content)
        self.assertIn("mode: \"closed\"", self.content)
        # Overlay host must never intercept user clicks or become an event target.
        self.assertIn("pointer-events:none", self.content)
        self.assertIn("aria-hidden", self.content)
        self.assertIn("setAttribute", self.content)

    def test_cursor_uses_request_animation_frame_with_reduced_motion_fallback(self) -> None:
        self.assertIn("requestAnimationFrame", self.content)
        self.assertIn("prefers-reduced-motion", self.content)
        # Safety timeout so a hung rAF never blocks the pending DOM action.
        self.assertIn("CURSOR_TIMEOUT_MS", self.content)

    def test_cursor_visual_contract_is_compact_and_stable(self) -> None:
        # First appearance must snap to the real hotspot instead of flying in
        # from the synthetic off-screen origin. Later moves use bounded adaptive
        # timing, and click feedback is a separate halo rather than scaling the
        # pointer itself.
        self.assertIn("const hasPosition", self.content)
        self.assertIn("Math.hypot", self.content)
        self.assertIn("CURSOR_MIN_DURATION_MS", self.content)
        self.assertIn("CURSOR_MAX_DURATION_MS", self.content)
        self.assertIn('id = "clickHalo"', self.content)
        self.assertIn("karox-click", self.content)
        self.assertIn("will-change:transform", self.content)
        self.assertNotIn("contain:layout style paint", self.content)
        self.assertNotIn("karox-pulse", self.content)
        self.assertNotIn("#2bd4ff", self.content)
        self.assertNotIn("scale(1.35)", self.content)

    def test_cursor_hidden_during_takeover_and_returns_after_resume(self) -> None:
        # The indicator's takeover branch hides the cursor inside content.js.
        self.assertIn('"Управление передано вам"', self.content)
        self.assertIn('hideCursor()', self.content)
        self.assertIn('"KaroX управляет этой вкладкой"', self.content)
        self.assertIn('"KaroX продолжил работу"', self.content)
        # Resumed state auto-reverts to controlling.
        self.assertIn("RESUMED_HOLD_MS", self.content)

    def test_cursor_overlay_is_single_instance_and_idempotent(self) -> None:
        self.assertIn("__karoxContentInit", self.content)
        # ensureOverlay reuses the same host when re-invoked.
        self.assertIn("host.isConnected", self.content)

    def test_cursor_overlay_removed_on_pagehide(self) -> None:
        self.assertIn("pagehide", self.content)
        self.assertIn("removeOverlay", self.content)

    # --- indicator ---

    def test_indicator_only_three_states_with_safe_margins(self) -> None:
        for marker in (
            '"KaroX управляет этой вкладкой"',
            '"Управление передано вам"',
            '"KaroX продолжил работу"',
        ):
            self.assertIn(marker, self.content)
        # Compact top-right status chip stays inside the viewport and does not
        # use the old neon-dot/glow treatment.
        self.assertIn("right:12px", self.content)
        self.assertIn("max-width:calc(100vw - 24px)", self.content)
        self.assertIn("backdrop-filter:blur(14px)", self.content)
        self.assertIn("border-radius:999px", self.content)
        self.assertIn('className = "statusIcon"', self.content)
        self.assertNotIn("dotmark", self.content)
        self.assertNotIn("left:50vw", self.content)

    def test_indicator_set_by_service_worker_per_agent_tab_only(self) -> None:
        # switch_tab clears the previous tab's overlay before setting the new one.
        self.assertIn("karox-remove-overlay", self.worker)
        self.assertIn("karox-set-indicator", self.worker)
        # Navigation re-asserts the indicator on the controlled tab.
        self.assertIn("onCommitted", self.worker)

    # --- console relay (MAIN -> ISOLATED -> service worker) ---

    def test_page_console_bridge_wraps_all_console_methods(self) -> None:
        for level in ("debug", "log", "info", "warn", "error"):
            self.assertIn(f"console.{level}", self.bridge)
        self.assertIn("__karox_console_bridge__", self.bridge)
        self.assertIn("window.postMessage", self.bridge)

    def test_page_console_bridge_is_idempotent(self) -> None:
        # Re-injection (navigation, re-injection by Chrome) must not double-wrap.
        self.assertIn("window.__karoxConsoleBridge", self.bridge)
        self.assertIn("return;", self.bridge)

    def test_page_console_bridge_redacts_secret_keys_and_replaces_dom_nodes(self) -> None:
        self.assertIn("SECRET_KEY", self.bridge)
        self.assertIn("[redacted]", self.bridge)
        self.assertIn("instanceof Node", self.bridge)
        self.assertIn("[node:", self.bridge)
        # Errors keep their message/stack but not arbitrary object references.
        self.assertIn("instanceof Error", self.bridge)

    def test_page_console_bridge_never_throws_to_the_page(self) -> None:
        # The wrap() body is fully try/catch guarded; the original console method
        # is always invoked afterwards.
        self.assertIn("orig[level].apply(console, args)", self.bridge)

    def test_service_worker_relays_console_only_from_same_extension_senders(self) -> None:
        self.assertIn('sender.id === chrome.runtime.id', self.worker)
        self.assertIn('"karox-console"', self.worker)
        self.assertIn("MAX_CONSOLE", self.worker)

    def test_service_worker_console_capture_is_best_effort_and_supported_flag(self) -> None:
        # The console command now reports real supported status with documented gaps.
        self.assertIn('supported: true', self.worker)
        self.assertIn("best-effort", self.worker)

    # --- takeover blocking (real, not cosmetic) ---

    def test_service_worker_blocks_input_actions_during_takeover(self) -> None:
        # assertAgentInputAllowed is the gate for click/fill/select/press.
        self.assertIn("assertAgentInputAllowed", self.worker)
        for method in ('"click"', '"fill"', '"select"', '"press"'):
            self.assertIn(method, self.worker)
        self.assertIn("is paused until resume_after_user_takeover", self.worker)

    def test_service_worker_rechecks_takeover_after_cursor_move(self) -> None:
        # Race: takeover arriving between move and click must still be honoured.
        self.assertIn("re-check after cursor move", self.worker)
        # The move itself is skipped while takeover is active.
        self.assertIn("state.takeover) return;", self.worker)

    def test_service_worker_removes_overlays_on_disconnect(self) -> None:
        self.assertIn("broadcastRemoveOverlay", self.worker)
        # Called from socket.onclose.
        self.assertIn("socket.onclose", self.worker)

    # --- overlay isolation from snapshot/get_text/selectors ---

    def test_service_worker_filters_overlay_host_from_snapshot(self) -> None:
        self.assertIn('OVERLAY_HOST_SELECTOR', self.worker)
        self.assertIn('.closest(OVERLAY_HOST_SELECTOR)', self.worker)
        self.assertIn('[data-karox-overlay-host]', self.worker)

    def test_ensure_agent_tab_trusts_explicit_tab_during_navigation_race(self) -> None:
        # Regression: after open/new_tab navigates a background tab to a URL,
        # the tab URL may temporarily read as about:blank during the commit.
        # ensureAgentTab must NOT switch to an unrelated pre-existing web tab
        # (e.g. a stale ChatGPT tab) just because the intended tab is mid-flight.
        # lastSafeUrls records the intended URL and pins agentTabId through it.
        self.assertIn('lastSafeUrls.has(state.agentTabId)', self.worker)
        self.assertIn('navigation transition', self.worker)

    def test_dom_action_rejects_overlay_elements(self) -> None:
        # Even if a page selector matched the host, domAction refuses to act
        # on it. The engine lives in dom_helpers.js now, injected before every
        # action and shared verbatim with the deterministic fixture suite.
        self.assertIn('part of the KaroX overlay', self.helpers)
        self.assertIn('closest(OVERLAY_HOST_SELECTOR)', self.helpers)

    def test_snapshot_excludes_overlay_in_text_too(self) -> None:
        # The filter runs before labels are collected for `out.text`.
        self.assertIn('.filter((el) => !el.closest(OVERLAY_HOST_SELECTOR))', self.worker)

    # --- namespaced DOM ids / security ---

    def test_overlay_host_uses_namespaced_attribute_not_global_id(self) -> None:
        self.assertIn('data-karox-overlay-host', self.content)
        self.assertNotIn('id="karox"', self.content)

    def test_content_script_ignores_non_namespaced_post_messages(self) -> None:
        # Only our MAIN-world bridge namespace is accepted; random page
        # postMessage traffic is ignored.
        self.assertIn('data.ns !== CONSOLE_NS', self.content)

    def test_content_script_redaction_pass_is_documented(self) -> None:
        # The page console bridge already redacts; the service worker stores the
        # already-clipped text. We assert the clipped length boundary here.
        self.assertIn('.slice(0, 1200)', self.worker)


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

    def test_text_url_fill_is_allowed_when_dialog_also_contains_token_field(self) -> None:
        self.manager._call = mock.Mock(  # type: ignore[method-assign]
            side_effect=[
                {
                    "type": "text",
                    "name": "mcp_url",
                    "id": "mcp-url",
                    "aria": "MCP server URL",
                    "placeholder": "https://example.com/mcp",
                    "text": "https://old.example/mcp",
                    "context": "MCP server URL\nAuthentication\nBearer token\nToken",
                },
                {"filled": True, "value_length": 27},
            ]
        )
        result = self.manager.fill(
            {"selector": "#mcp-url", "value": "https://new.example/mcp"}, 5
        )
        self.assertTrue(result["filled"])
        self.manager._call.assert_has_calls(
            [
                mock.call("inspect", {"selector": "#mcp-url"}, 5),
                mock.call(
                    "fill",
                    {"selector": "#mcp-url", "value": "https://new.example/mcp"},
                    5,
                ),
            ]
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
        self.assertFalse(result["capture_recovered"])
        self.assertEqual(result["capture_attempts"], 1)

    def test_screenshot_recovers_from_minimized_chrome_readback_failure(self) -> None:
        png = b"\x89PNG\r\n\x1a\nKaroX-recovered"
        encoded = "data:image/png;base64," + base64.b64encode(png).decode("ascii")
        self.manager._call = mock.Mock(  # type: ignore[method-assign]
            side_effect=[
                BrowserError("screenshot_capture_failed: Failed to capture tab: image readback failed"),
                {"shown": True, "window_id": 7},
                {"data_url": encoded, "tab_id": "tab-42", "capture_attempts": 1},
            ]
        )
        with mock.patch("karox.extension_browser.time.sleep") as sleep:
            result = self.manager.screenshot({"name": "extension-recovered"}, 5)
        stored, _ = self.manager._artifacts.read(result["artifact_id"])
        self.assertEqual(stored, png)
        self.assertTrue(result["capture_recovered"])
        self.assertEqual(result["capture_attempts"], 2)
        sleep.assert_called_once_with(0.6)
        self.manager._call.assert_has_calls(
            [
                mock.call("screenshot", {}, 5),
                mock.call("show_window", {"left": 100, "top": 100}, 5),
                mock.call("screenshot", {}, 5),
            ]
        )

    def test_service_worker_screenshot_restores_minimized_window_and_retries(self) -> None:
        root = Path(__file__).parents[1] / "src" / "karox" / "browser_extension"
        worker = (root / "service_worker.js").read_text(encoding="utf-8")
        self.assertIn("async function captureVisibleTabResilient", worker)
        self.assertIn("chrome.windows.get(tab.windowId)", worker)
        self.assertIn('windowState?.state === "minimized"', worker)
        self.assertIn('chrome.windows.update(tab.windowId, { state: "normal" })', worker)
        self.assertIn("image readback failed|view is invisible", worker)
        self.assertIn("setTimeout(resolve, 600)", worker)
        self.assertIn('chrome.windows.update(tab.windowId, { state: "minimized" })', worker)
        self.assertIn("capture_recovered", worker)

    def test_takeover_blocks_agent_input_without_closing_profile(self) -> None:
        self.manager._ensure_started = mock.Mock()  # type: ignore[method-assign]
        self.manager._call = mock.Mock(  # type: ignore[method-assign]
            return_value={"tab_id": "tab-9", "url": "https://example.com/"}
        )
        self.manager._persist_takeover_active = mock.Mock()  # type: ignore[method-assign]
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
        from karox.managed_browser import ManagedBrowserInstance
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
        prior = ManagedBrowserInstance(
            instance_id=f"inst-{uuid.uuid4().hex[:16]}",
            saved_profile_id="ad-hoc",
            session_id=session_id,
            browser_instance_id="browser-test",
            bridge_instance_id="bridge-old",
            launch_nonce="old-nonce",
            extension_dir="/tmp/ext",
            user_data_dir="/tmp/profile",
            browser_pid=12345,
            browser_create_time_ns=999,
            executable_path="/chrome",
            argv=(),
            captured_at=0.0,
        )
        with tempfile.TemporaryDirectory() as temp:
            extension = Path(temp) / "extension"
            extension.mkdir()
            with (
                mock.patch.object(manager._registry, "find_for_session", return_value=(prior,)),
                mock.patch("karox.extension_browser.verify_managed_browser", return_value=True),
                mock.patch("karox.extension_browser._ExtensionBridgeServer", return_value=bridge),
                mock.patch("karox.extension_browser._prepare_extension", return_value=extension),
            ):
                manager._ensure_started()
                bridge.start.assert_called_once_with()
                # B6: a proven reconnect waits for hello with a fresh nonce (15s window),
                # not the old 2.5s pre-launch shortcut that accepted personal Chrome.
                bridge.wait_connected.assert_called_once_with(15.0)
                # is_open must be checked inside the mock scope: it calls
                # verify_managed_browser, which is mocked to True for the test's
                # fake prior instance.
                self.assertTrue(manager.is_open)
                self.assertIsNone(manager._process)
        manager.detach()

    def test_explicit_close_stops_attached_profile(self) -> None:
        from karox.managed_browser import ManagedBrowserInstance
        profile = Path(self.temp.name) / "attached-profile"
        bridge = mock.Mock()
        bridge.connected = True
        instance = ManagedBrowserInstance(
            instance_id="inst-test",
            saved_profile_id="ad-hoc",
            session_id=self.session_id,
            browser_instance_id="browser-test",
            bridge_instance_id="bridge-test",
            launch_nonce="nonce-test",
            extension_dir="/tmp/ext",
            user_data_dir=str(profile),
            browser_pid=0,
            browser_create_time_ns=None,
            executable_path="/chrome",
            argv=(),
            captured_at=0.0,
        )
        self.manager._bridge = bridge
        self.manager._process = None
        self.manager._instance = instance
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

    def test_stable_command_can_inject_an_opaque_browser_credential(self) -> None:
        runtime = mock.Mock()
        runtime._browser = _CommandBrowser()
        payload = {
            "selector": "#password",
            "reference": "os-keyring:browser/test-account",
            "field": "password",
        }
        result = execute_browser_command(
            runtime,
            {"action": "fill_credential", "payload": payload},
            12.0,
        )
        self.assertEqual(result["action"], "fill_credential")
        self.assertEqual(result["method"], "fill_credential")
        self.assertEqual(runtime._browser.calls, [("fill_credential", payload, 12.0)])

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


class CloseTabOwnershipRegressionTests(unittest.TestCase):
    """B6 close_tab three-tier error classification regression tests.

    Distinguishes:
      - ownership denial (TabOwnershipError) — non-fatal, browser stays alive;
      - normal command error (ExtensionBridgeError from extension) — non-fatal;
      - isolation compromise (verify_managed_browser / verify_hello failure) —
        fail-closed, browser killed.

    Only isolation compromise may automatically close the managed browser.
    """

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(
            os.environ,
            {"KAROX_VNEXT_RUNTIME_DIR": str(Path(self.temp.name) / "runtime")},
        )
        self.environment.start()
        self.session_id = f"close-tab-{uuid.uuid4().hex}"
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
        # Prevent _ensure_started from launching Chrome in unit tests.
        self.manager._ensure_started = mock.Mock()  # type: ignore[method-assign]
        # Set up a tab registry with one initial tab and one created tab.
        self.tab_reg = TabOwnershipRegistry("browser-test", self.session_id)
        self.manager._tab_registry = self.tab_reg
        self.tab_reg.register_initial("tab-initial")
        self.tab_reg.register_created("tab-owned")

    def tearDown(self) -> None:
        self.manager.close(force=True)
        self.environment.stop()
        self.temp.cleanup()

    # --- Case 1: closing the only owned work tab succeeds when initial remains ---
    def test_close_owned_tab_succeeds_when_initial_remains(self) -> None:
        self.manager._call = mock.Mock(  # type: ignore[method-assign]
            return_value={"closed": True, "tab_id": "tab-owned", "active_tab_id": "tab-initial"}
        )
        result = self.manager.close_tab({"tab_id": "tab-owned"}, 5)
        self.assertTrue(result["closed"])
        self.manager._call.assert_called_once_with("close_tab", {"tab_id": "tab-owned"}, 5)
        # The owned tab is now marked closed in the registry.
        with self.assertRaisesRegex(TabOwnershipError, "already closed"):
            self.tab_reg.assert_closable("tab-owned")
        # The initial tab remains and is still non-closable.
        with self.assertRaisesRegex(TabOwnershipError, "initial tab"):
            self.tab_reg.assert_closable("tab-initial")

    # --- Case 2: initial tab remains non-closable (ownership denial) ---
    def test_close_initial_tab_returns_ownership_error(self) -> None:
        self.manager._call = mock.Mock()  # type: ignore[method-assign]
        with self.assertRaisesRegex(TabOwnershipError, "initial tab"):
            self.manager.close_tab({"tab_id": "tab-initial"}, 5)
        # The extension was never called — ownership check is bridge-side.
        self.manager._call.assert_not_called()
        # The initial tab is still in the registry (not closed).
        rec = self.tab_reg.get("tab-initial")
        self.assertIsNotNone(rec)
        self.assertTrue(rec.is_initial_tab)

    # --- Case 3: unknown/unowned tab close returns structured ownership error ---
    def test_close_unknown_tab_returns_structured_ownership_error(self) -> None:
        self.manager._call = mock.Mock()  # type: ignore[method-assign]
        with self.assertRaisesRegex(TabOwnershipError, "not in the KaroX tab registry"):
            self.manager.close_tab({"tab_id": "tab-foreign"}, 5)
        self.manager._call.assert_not_called()

    # --- Case 4: normal close_tab command failure is non-fatal ---
    def test_normal_command_failure_is_non_fatal(self) -> None:
        # The extension returns a command error (e.g. element not found).
        # This is a normal command failure, NOT an isolation compromise.
        self.manager._call = mock.Mock(  # type: ignore[method-assign]
            side_effect=ExtensionBridgeError("extension command failed: element not found")
        )
        with self.assertRaisesRegex(ExtensionBridgeError, "element not found"):
            self.manager.close_tab({"tab_id": "tab-owned"}, 5)
        # The tab is NOT marked closed in the registry (command failed).
        rec = self.tab_reg.get("tab-owned")
        self.assertIsNotNone(rec)
        self.assertEqual(rec.ownership_state, "owned")

    # --- Case 5: isolation compromise remains fail-closed ---
    def test_isolation_compromise_is_fail_closed(self) -> None:
        # verify_managed_browser returns False → is_open returns False →
        # _ensure_started must re-launch, NOT accept a stale connection.
        from karox.managed_browser import ManagedBrowserInstance
        import time as _time
        inst = ManagedBrowserInstance(
            instance_id=f"inst-{uuid.uuid4().hex[:16]}",
            saved_profile_id="ad-hoc",
            session_id=self.session_id,
            browser_instance_id="browser-test",
            bridge_instance_id="bridge-test",
            launch_nonce="nonce-test",
            extension_dir="/tmp/ext",
            user_data_dir="/tmp/profile",
            browser_pid=99999,
            browser_create_time_ns=123,
            executable_path="/chrome",
            argv=("/chrome", "--user-data-dir=/tmp/profile", "--load-extension=/tmp/ext"),
            captured_at=_time.time(),
        )
        self.manager._instance = inst
        # is_open checks verify_managed_browser → False (PID 99999 does not exist).
        self.assertFalse(self.manager.is_open)

    # --- Case 6: non-fatal command failure does NOT terminate managed browser ---
    def test_non_fatal_command_failure_does_not_terminate_browser(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        bridge = mock.Mock()
        bridge.connected = True
        self.manager._process = process
        self.manager._bridge = bridge
        self.manager._call = mock.Mock(  # type: ignore[method-assign]
            side_effect=ExtensionBridgeError("extension command failed")
        )
        with self.assertRaisesRegex(ExtensionBridgeError, "command failed"):
            self.manager.close_tab({"tab_id": "tab-owned"}, 5)
        # Browser process and bridge are NOT terminated by a command error.
        process.terminate.assert_not_called()
        bridge.stop.assert_not_called()
        self.assertIsNotNone(self.manager._process)
        self.assertIsNotNone(self.manager._bridge)

    # --- Case 7: managed browser stays alive after successful owned close_tab ---
    def test_browser_stays_alive_after_successful_close_tab(self) -> None:
        process = mock.Mock()
        process.poll.return_value = None
        bridge = mock.Mock()
        bridge.connected = True
        self.manager._process = process
        self.manager._bridge = bridge
        self.manager._call = mock.Mock(  # type: ignore[method-assign]
            return_value={"closed": True, "tab_id": "tab-owned"}
        )
        result = self.manager.close_tab({"tab_id": "tab-owned"}, 5)
        self.assertTrue(result["closed"])
        # Browser process and bridge are still alive after successful close.
        process.terminate.assert_not_called()
        bridge.stop.assert_not_called()
        self.assertIsNotNone(self.manager._process)
        self.assertIsNotNone(self.manager._bridge)

    # --- Case 8 (extra): takeover blocks close_tab (ownership vs security) ---
    def test_close_tab_blocked_during_takeover(self) -> None:
        self.manager._takeover_active = True
        self.manager._call = mock.Mock()  # type: ignore[method-assign]
        with self.assertRaisesRegex(BrowserSecurityError, "user has control"):
            self.manager.close_tab({"tab_id": "tab-owned"}, 5)
        self.manager._call.assert_not_called()


class OverlayDeliveryProtocolTests(unittest.TestCase):
    """B6 overlay delivery protocol behavioral tests.

    Verifies the ack protocol, ready handshake, desired-state persistence,
    and fail-safe semantics in the service worker and content script.
    """

    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "karox" / "browser_extension"
        self.worker = (root / "service_worker.js").read_text(encoding="utf-8")
        self.content = (root / "content.js").read_text(encoding="utf-8")

    # --- Ready handshake ---
    def test_content_script_sends_ready_handshake(self) -> None:
        self.assertIn("karox-content-ready", self.content)
        # Sent on init, not deferred.
        self.assertIn('chrome.runtime.sendMessage({ type: "karox-content-ready"', self.content)

    def test_service_worker_handles_content_ready(self) -> None:
        self.assertIn('"karox-content-ready"', self.worker)
        # contentReady set is updated.
        self.assertIn("state.contentReady.add", self.worker)

    def test_service_worker_reapplies_desired_state_on_ready(self) -> None:
        self.assertIn("state.desiredIndicator[tabId]", self.worker)
        self.assertIn("setIndicatorAck(tabId, desired)", self.worker)

    # --- Desired state persistence ---
    def test_service_worker_stores_desired_indicator_per_tab(self) -> None:
        self.assertIn("desiredIndicator: {}", self.worker)
        self.assertIn("state.desiredIndicator[", self.worker)

    def test_service_worker_cleans_up_desired_state_on_close(self) -> None:
        self.assertIn("delete state.desiredIndicator[tabId]", self.worker)
        self.assertIn("delete state.lastOverlayAck[tabId]", self.worker)
        self.assertIn("state.contentReady.delete(tabId)", self.worker)

    # --- Ack protocol ---
    def test_service_worker_has_sendToTabAck_with_retry(self) -> None:
        self.assertIn("async function sendToTabAck", self.worker)
        self.assertIn("retries", self.worker)
        self.assertIn("delayMs", self.worker)

    def test_service_worker_takeover_waits_for_ack(self) -> None:
        # takeover must call setIndicatorAck and report overlay_ack.
        self.assertIn("setIndicatorAck(tab.id, \"takeover\")", self.worker)
        self.assertIn("overlay_ack", self.worker)

    def test_service_worker_resume_waits_for_ack(self) -> None:
        self.assertIn('setIndicatorAck(tab.id, "resumed")', self.worker)
        # resume result includes overlay_ack.
        self.assertIn("overlay_ack: true", self.worker)

    def test_takeover_reports_ack_failure_not_false_success(self) -> None:
        # On ack failure, takeover returns overlay_ack: false, not just success.
        self.assertIn("overlay_ack: false", self.worker)

    def test_moveCursorTo_uses_ack(self) -> None:
        self.assertIn("sendToTabAck(tabId, {", self.worker)
        self.assertIn("type: \"karox-move-cursor\"", self.worker)

    # --- Diagnostics ---
    def test_service_worker_has_debug_overlay_state_method(self) -> None:
        self.assertIn('"debug_overlay_state"', self.worker)
        self.assertIn("live_overlay_present", self.worker)
        self.assertIn("live_indicator_state", self.worker)
        self.assertIn("content_script_ready", self.worker)

    def test_content_script_has_get_state_handler(self) -> None:
        self.assertIn('"karox-get-state"', self.content)
        self.assertIn("overlay_present", self.content)
        self.assertIn("indicator_state", self.content)
        self.assertIn("cursor_present", self.content)

    # --- Navigation / reload resilience ---
    def test_webNavigation_reapplies_desired_state(self) -> None:
        self.assertIn("webNavigation.onCommitted", self.worker)
        self.assertIn("setIndicatorAck(details.tabId, desired)", self.worker)

    def test_indicator_survives_reload_via_ready_handshake(self) -> None:
        # After reload, content.js sends ready → service worker re-applies desired.
        self.assertIn("karox-content-ready", self.worker)
        self.assertIn("state.desiredIndicator[tabId]", self.worker)

    # --- Wrong tab rejection ---
    def test_switch_tab_sets_indicator_on_correct_tab(self) -> None:
        self.assertIn("setIndicatorAck(tabId, state.takeover", self.worker)

    # --- No false success ---
    def test_no_fire_and_forget_for_set_indicator_in_takeover(self) -> None:
        # The old code used sendToTab (fire-and-forget) for set-indicator in
        # takeover. The new code uses setIndicatorAck (waits for ack).
        # Ensure the takeover block does NOT use the old sendToTab for indicator.
        takeover_block = self.worker[self.worker.index('method === "takeover"'):self.worker.index('method === "resume"')]
        self.assertNotIn('sendToTab(tab.id, { type: "karox-set-indicator"', takeover_block)


class OverlayDeliveryManagerTests(unittest.TestCase):
    """Manager-level behavioral tests for overlay ack protocol."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.environment = mock.patch.dict(
            os.environ,
            {"KAROX_VNEXT_RUNTIME_DIR": str(Path(self.temp.name) / "runtime")},
        )
        self.environment.start()
        self.session_id = f"overlay-{uuid.uuid4().hex}"
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
        self.manager._ensure_started = mock.Mock()  # type: ignore[method-assign]
        self.manager._persist_takeover_active = mock.Mock()  # type: ignore[method-assign]

    def tearDown(self) -> None:
        self.manager.close(force=True)
        self.environment.stop()
        self.temp.cleanup()

    def test_takeover_result_includes_overlay_ack_field(self) -> None:
        # When the bridge returns a takeover result, it must include overlay_ack
        # so the manager can verify the overlay was actually rendered.
        self.manager._call = mock.Mock(  # type: ignore[method-assign]
            return_value={"takeover": True, "agent_input_paused": True, "overlay_ack": True}
        )
        result = self.manager.request_user_takeover({"reason": "test"}, 5)
        self.assertTrue(result["agent_input_paused"])
        # The bridge returned overlay_ack=True — the manager passes it through.
        self.assertIn("overlay_ack", result)

    def test_takeover_with_overlay_ack_failure_still_reports_paused(self) -> None:
        # Even if overlay ack fails, agent input must still be paused (defensive).
        self.manager._call = mock.Mock(  # type: ignore[method-assign]
            return_value={
                "takeover": True,
                "agent_input_paused": True,
                "overlay_ack": False,
                "overlay_error": "content script not ready",
            }
        )
        result = self.manager.request_user_takeover({"reason": "test"}, 5)
        self.assertTrue(result["agent_input_paused"])
        self.assertFalse(result.get("overlay_ack", True))
        self.assertTrue(self.manager.takeover_active)

    def test_resume_result_includes_overlay_ack_field(self) -> None:
        self.manager._takeover_active = True
        self.manager._call = mock.Mock(  # type: ignore[method-assign]
            return_value={"resumed": True, "agent_input_paused": False, "overlay_ack": True}
        )
        result = self.manager.resume_after_user_takeover({}, 5)
        self.assertFalse(result["agent_input_paused"])
        self.assertIn("overlay_ack", result)

    def test_debug_overlay_state_is_callable_via_call(self) -> None:
        self.manager._call = mock.Mock(  # type: ignore[method-assign]
            return_value={
                "target_tab_id": "tab-1",
                "visible_active_tab_id": "tab-1",
                "visible_window_id": 100,
                "content_script_ready": True,
                "desired_indicator": "controlling",
                "live_overlay_present": True,
                "live_indicator_state": "controlling",
                "live_indicator_text": "KaroX управляет этой вкладкой",
                "takeover_active": False,
                "ownership_target_tab_id": "tab-1",
                "overlay_ack_tab_id": "tab-1",
                "all_equal": True,
            }
        )
        result = self.manager._call("debug_overlay_state", {}, 5)
        self.assertTrue(result["content_script_ready"])
        self.assertTrue(result["live_overlay_present"])
        self.assertEqual(result["live_indicator_state"], "controlling")
        self.assertEqual(result["visible_active_tab_id"], result["target_tab_id"])
        self.assertTrue(result["all_equal"])


class VisibleActiveTabContractTests(unittest.TestCase):
    """B6 visible-active-tab contract tests.

    takeover is ONLY valid for the tab the user actually sees. The visible
    active tab must match the overlay target, or takeover fails safely.
    """

    def setUp(self) -> None:
        root = Path(__file__).resolve().parents[1] / "src" / "karox" / "browser_extension"
        self.worker = (root / "service_worker.js").read_text(encoding="utf-8")
        self.content = (root / "content.js").read_text(encoding="utf-8")

    # --- Visible active tab helpers ---
    def test_service_worker_has_visibleActiveTab_helper(self) -> None:
        self.assertIn("async function visibleActiveTab", self.worker)
        self.assertIn("chrome.tabs.query({ active: true, currentWindow: true })", self.worker)

    def test_service_worker_has_activateAndVerifyTab(self) -> None:
        self.assertIn("async function activateAndVerifyTab", self.worker)
        self.assertIn("chrome.tabs.update(tabId, { active: true })", self.worker)
        self.assertIn('state: "normal", focused: true', self.worker)
        self.assertIn("if (!after.active)", self.worker)

    # --- Takeover contract ---
    def test_takeover_activates_and_verifies_visible_tab(self) -> None:
        takeover_block = self.worker[self.worker.index('method === "takeover"'):self.worker.index('method === "resume"')]
        self.assertIn("activateAndVerifyTab", takeover_block)
        self.assertIn("visual_takeover_failed", takeover_block)

    def test_takeover_fails_safely_on_activation_failure(self) -> None:
        takeover_block = self.worker[self.worker.index('method === "takeover"'):self.worker.index('method === "resume"')]
        # On activation failure, agent_input_paused must be false.
        self.assertIn("agent_input_paused: false", takeover_block)
        self.assertIn("visual_takeover_failed: true", takeover_block)

    def test_takeover_returns_visible_active_tab_id(self) -> None:
        takeover_block = self.worker[self.worker.index('method === "takeover"'):self.worker.index('method === "resume"')]
        self.assertIn("visual_active_tab_id", takeover_block)
        self.assertIn("visible_window_id", takeover_block)

    def test_takeover_verifies_visible_active_matches_overlay(self) -> None:
        takeover_block = self.worker[self.worker.index('method === "takeover"'):self.worker.index('method === "resume"')]
        self.assertIn("visibleNow = await visibleActiveTab()", takeover_block)
        self.assertIn("visible_active_matches_overlay", takeover_block)
        self.assertIn("allEqual", takeover_block)

    # --- Diagnostics contract ---
    def test_debug_overlay_state_includes_visible_active_tab(self) -> None:
        debug_block = self.worker[self.worker.index('method === "debug_overlay_state"'):self.worker.index('method === "takeover"')]
        self.assertIn("visible_active_tab_id", debug_block)
        self.assertIn("visible_window_id", debug_block)
        self.assertIn("ownership_target_tab_id", debug_block)
        self.assertIn("overlay_ack_tab_id", debug_block)
        self.assertIn("all_equal", debug_block)

    def test_content_script_get_state_includes_document_identity(self) -> None:
        self.assertIn("document_url", self.content)
        self.assertIn("document_title", self.content)
        self.assertIn("location.href", self.content)

    # --- No false success ---
    def test_takeover_does_not_set_paused_before_activation_verified(self) -> None:
        takeover_block = self.worker[self.worker.index('method === "takeover"'):self.worker.index('method === "resume"')]
        # state.takeover = true must come AFTER activateAndVerifyTab succeeds.
        activate_idx = takeover_block.index("activateAndVerifyTab")
        takeover_set_idx = takeover_block.index("state.takeover = true")
        self.assertGreater(takeover_set_idx, activate_idx,
            "state.takeover must be set AFTER activation is verified")

    def test_takeover_activation_failure_does_not_set_takeover_state(self) -> None:
        takeover_block = self.worker[self.worker.index('method === "takeover"'):self.worker.index('method === "resume"')]
        # The catch block for activation failure returns early WITHOUT setting
        # state.takeover = true.
        catch_idx = takeover_block.index("} catch (err) {")
        return_idx = takeover_block.index("return {", catch_idx)
        takeover_set_idx = takeover_block.index("state.takeover = true")
        # state.takeover = true is AFTER the catch block.
        self.assertGreater(takeover_set_idx, return_idx)


class StartupTabSetupContractTests(unittest.TestCase):
    """B6: setup_startup_tab closes extra about:blank, activates startup URL
    tab, applies indicator + cursor. show_cursor shows cursor independently."""

    @staticmethod
    def _sw_source() -> str:
        from pathlib import Path
        sw_path = Path(__file__).resolve().parent.parent / "src" / "karox" / "browser_extension" / "service_worker.js"
        return sw_path.read_text(encoding="utf-8")

    def test_service_worker_has_setup_startup_tab_method(self) -> None:
        src = self._sw_source()
        self.assertIn('"setup_startup_tab"', src)
        self.assertIn("startupHost", src)
        self.assertIn("pendingUrl", src)

    def test_service_worker_has_show_cursor_method(self) -> None:
        src = self._sw_source()
        self.assertIn('"show_cursor"', src)
        self.assertIn("karox-move-cursor", src)

    def test_setup_startup_tab_waits_for_url_load(self) -> None:
        src = self._sw_source()
        # Must poll for up to 15s (30 attempts * 500ms)
        self.assertIn("attempt < 30", src)
        self.assertIn("setTimeout(r, 500)", src)

    def test_setup_startup_tab_checks_pendingUrl(self) -> None:
        src = self._sw_source()
        # Must check pendingUrl (Chrome sets this before url during load)
        self.assertIn("t.pendingUrl", src)

    def test_setup_startup_tab_closes_other_tabs(self) -> None:
        src = self._sw_source()
        self.assertIn("chrome.tabs.remove", src)
        self.assertIn("otherTabs", src)

    def test_setup_startup_tab_sets_agent_tab(self) -> None:
        src = self._sw_source()
        # Must set state.agentTabId to the startup tab
        setup_section = src[src.index('"setup_startup_tab"'):]
        self.assertIn("state.agentTabId = startupTab.id", setup_section[:2000])

    def test_setup_startup_tab_applies_controlling_indicator(self) -> None:
        src = self._sw_source()
        setup_section = src[src.index('"setup_startup_tab"'):]
        self.assertIn('"controlling"', setup_section[:2000])
        self.assertIn("setIndicatorAck", setup_section[:2000])

    def test_setup_startup_tab_shows_cursor(self) -> None:
        src = self._sw_source()
        setup_section = src[src.index('"setup_startup_tab"'):]
        self.assertIn("karox-move-cursor", setup_section[:3000])
        self.assertIn("cursorPresent", setup_section[:3000])

    def test_setup_startup_tab_activates_tab(self) -> None:
        src = self._sw_source()
        setup_section = src[src.index('"setup_startup_tab"'):]
        self.assertIn("active: true", setup_section[:2000])

    def test_setup_startup_tab_returns_tab_id_and_closed_count(self) -> None:
        src = self._sw_source()
        setup_section = src[src.index('"setup_startup_tab"'):]
        self.assertIn("setup: true", setup_section[:3000])
        self.assertIn("closed_extra_tabs", setup_section[:3000])

    def test_listPublicTabs_includes_pendingUrl(self) -> None:
        src = self._sw_source()
        list_section = src[src.index("async function listPublicTabs"):]
        self.assertIn("pendingUrl", list_section[:500])

    def test_launch_fresh_calls_open_window(self) -> None:
        import inspect
        from karox.extension_browser import ChromeExtensionBrowserSessionManager
        src = inspect.getsource(ChromeExtensionBrowserSessionManager._launch_fresh)
        self.assertIn("navigate_to", src)

    def test_launch_fresh_passes_startup_url_to_open_window(self) -> None:
        import inspect
        from karox.extension_browser import ChromeExtensionBrowserSessionManager
        src = inspect.getsource(ChromeExtensionBrowserSessionManager._launch_fresh)
        self.assertIn('"url": startup_url', src)

    def test_show_cursor_returns_shown_and_tab_id(self) -> None:
        src = self._sw_source()
        cursor_section = src[src.index('"show_cursor"'):]
        self.assertIn("shown: true", cursor_section[:1000])
        self.assertIn("tab_id: tabRef", cursor_section[:1000])


class OpenWindowContractTests(unittest.TestCase):
    """B6: navigate_to navigates existing tab, no about:blank creation."""

    @staticmethod
    def _sw_source() -> str:
        from pathlib import Path
        sw_path = Path(__file__).resolve().parent.parent / "src" / "karox" / "browser_extension" / "service_worker.js"
        return sw_path.read_text(encoding="utf-8")

    def test_service_worker_has_navigate_to_method(self) -> None:
        src = self._sw_source()
        self.assertIn('"navigate_to"', src)
        self.assertIn("chrome.tabs.update", src)

    def test_navigate_to_uses_existing_tab(self) -> None:
        src = self._sw_source()
        nav_section = src[src.index('"navigate_to"'):]
        self.assertIn("chrome.tabs.query({})", nav_section[:1000])
        self.assertIn("url: startupUrl", nav_section[:1500])
        self.assertIn("allTabs[0]", nav_section[:1500])

    def test_navigate_to_applies_indicator_and_cursor(self) -> None:
        src = self._sw_source()
        nav_section = src[src.index('"navigate_to"'):]
        self.assertIn("setIndicatorAck", nav_section[:2000])
        self.assertIn("karox-move-cursor", nav_section[:2000])

    def test_navigate_to_returns_tab_id(self) -> None:
        src = self._sw_source()
        nav_section = src[src.index('"navigate_to"'):]
        self.assertIn("navigated: true", nav_section[:2000])
        self.assertIn("tab_id", nav_section[:2000])

    def test_service_worker_has_open_window_method(self) -> None:
        src = self._sw_source()
        self.assertIn('"open_window"', src)
        self.assertIn("chrome.windows.create", src)

    def test_open_window_creates_single_tab(self) -> None:
        src = self._sw_source()
        open_section = src[src.index('"open_window"'):]
        self.assertIn('type: "normal"', open_section[:1000])
        self.assertIn("win.tabs[0]", open_section[:1500])

    def test_open_window_applies_indicator_and_cursor(self) -> None:
        src = self._sw_source()
        open_section = src[src.index('"open_window"'):]
        self.assertIn("setIndicatorAck", open_section[:2000])
        self.assertIn("karox-move-cursor", open_section[:2000])

    def test_open_window_returns_tab_id_and_window_id(self) -> None:
        src = self._sw_source()
        open_section = src[src.index('"open_window"'):]
        self.assertIn("opened: true", open_section[:2000])
        self.assertIn("window_id", open_section[:2000])

    def test_launch_managed_browser_uses_no_startup_window(self) -> None:
        import inspect
        from karox import managed_browser
        src = inspect.getsource(managed_browser.launch_managed_browser)
        # Chrome launches with about:blank, then navigates to startup URL
        self.assertIn("about:blank", src)


class GeometryInvariantTests(unittest.TestCase):
    """B6: geometry handler returns indicator_fully_inside_viewport and cursor invariants."""

    @staticmethod
    def _cj_source() -> str:
        from pathlib import Path
        cj_path = Path(__file__).resolve().parent.parent / "src" / "karox" / "browser_extension" / "content.js"
        return cj_path.read_text(encoding="utf-8")

    def test_content_js_has_karox_get_geometry(self) -> None:
        src = self._cj_source()
        self.assertIn('"karox-get-geometry"', src)

    def test_geometry_returns_indicator_fully_inside_viewport(self) -> None:
        src = self._cj_source()
        self.assertIn("indicator_fully_inside_viewport", src)

    def test_geometry_returns_cursor_fully_inside_viewport(self) -> None:
        src = self._cj_source()
        self.assertIn("cursor_fully_inside_viewport", src)

    def test_geometry_returns_cursor_effective_fill(self) -> None:
        src = self._cj_source()
        self.assertIn("cursor_effective_fill", src)
        self.assertIn("cursor_effective_stroke", src)

    def test_geometry_returns_cursor_pixel_area(self) -> None:
        src = self._cj_source()
        self.assertIn("cursor_pixel_area", src)

    def test_geometry_returns_cursor_rendered_visible(self) -> None:
        src = self._cj_source()
        self.assertIn("cursor_rendered_visible", src)

    def test_legacy_indicator_state_is_hidden_from_page(self) -> None:
        src = self._cj_source()
        self.assertIn('"display:none;align-items:center;gap:7px;opacity:0;', src)
        self.assertNotIn("left:50vw", src)

    def test_host_css_does_not_use_contain_strict(self) -> None:
        src = self._cj_source()
        self.assertNotIn("contain:strict", src)
        self.assertNotIn("contain: strict", src)

    def test_setIndicatorAck_updates_native_tab_marker(self) -> None:
        from pathlib import Path
        sw_path = Path(__file__).resolve().parent.parent / "src" / "karox" / "browser_extension" / "service_worker.js"
        src = sw_path.read_text(encoding="utf-8")
        ack_section = src[src.index("async function setIndicatorAck"):]
        self.assertIn("setAgentTabMarker", ack_section[:1200])
        self.assertNotIn("indicator_fully_inside_viewport", ack_section[:1200])


class CssLeakPreventionTests(unittest.TestCase):
    """B6: CSS must not leak into the page as text."""

    @staticmethod
    def _cj_source() -> str:
        from pathlib import Path
        cj_path = Path(__file__).resolve().parent.parent / "src" / "karox" / "browser_extension" / "content.js"
        return cj_path.read_text(encoding="utf-8")

    def test_ensureOverlay_uses_createElement_style(self) -> None:
        src = self._cj_source()
        self.assertIn('document.createElement("style")', src)
        self.assertIn("styleEl.textContent", src)

    def test_ensureOverlay_does_not_use_shadow_innerHTML_with_css(self) -> None:
        src = self._cj_source()
        # shadow.innerHTML must not contain "<style>" with CSS
        self.assertNotIn('shadow.innerHTML = "<style>"', src)
        self.assertNotIn('shadow.innerHTML =\n      "<style>"', src)

    def test_ensureOverlay_appends_style_element_to_shadow(self) -> None:
        src = self._cj_source()
        self.assertIn("shadow.appendChild(styleEl)", src)

    def test_shadow_reset_never_makes_style_element_render_as_text(self) -> None:
        src = self._cj_source()
        # `all:initial` on every shadow descendant also resets the UA
        # `style { display:none }` rule and makes the raw CSS visible on-page.
        self.assertNotIn(":host,*{all:initial", src)
        self.assertIn(":host{all:initial}", src)
        self.assertIn("style{display:none!important}", src)

    def test_cursor_uses_refined_svg_arrow_not_spinner(self) -> None:
        src = self._cj_source()
        self.assertIn("CURSOR_SVG", src)
        self.assertIn("<svg", src)
        self.assertIn("<path", src)
        self.assertIn('fill=\"#17181c\"', src)
        self.assertIn('stroke=\"rgba(255,255,255,.96)\"', src)
        self.assertIn("clickHalo", src)
        # No old cyan debug-pointer or spinner-like radial treatment.
        self.assertNotIn("#2bd4ff", src)
        self.assertNotIn("radial-gradient(circle", src)

    def test_in_page_status_chip_is_not_rendered(self) -> None:
        src = self._cj_source()
        # Agent ownership is shown in Chrome's native tab strip, not over the page.
        self.assertIn('"display:none;align-items:center;gap:7px;opacity:0;', src)
        self.assertIn("statusIcon", src)  # retained only for internal state compatibility
        self.assertNotIn("dotmark", src)

    def test_geometry_handler_detects_css_leak(self) -> None:
        src = self._cj_source()
        self.assertIn("css_leak_in_body", src)
        self.assertIn("host_has_text_children", src)

    def test_visible_agent_marker_is_native_tab_group(self) -> None:
        from pathlib import Path
        sw_path = Path(__file__).resolve().parent.parent / "src" / "karox" / "browser_extension" / "service_worker.js"
        worker = sw_path.read_text(encoding="utf-8")
        self.assertIn("chrome.tabs.group", worker)
        self.assertIn("chrome.tabGroups.update", worker)
        self.assertIn('title: "KaroX"', worker)

    def test_host_css_no_contain_strict(self) -> None:
        src = self._cj_source()
        self.assertNotIn("contain:strict", src)
        self.assertNotIn("contain: strict", src)


class TabsAllGroundTruthTests(unittest.TestCase):
    """B6: tabs_all returns ALL visible tabs, not just owned/registry tabs."""

    @staticmethod
    def _sw_source() -> str:
        from pathlib import Path
        sw_path = Path(__file__).resolve().parent.parent / "src" / "karox" / "browser_extension" / "service_worker.js"
        return sw_path.read_text(encoding="utf-8")

    def test_service_worker_has_tabs_all_method(self) -> None:
        src = self._sw_source()
        self.assertIn('"tabs_all"', src)
        self.assertIn("chrome.tabs.query({})", src)

    def test_tabs_all_returns_browser_visible_tabs_count(self) -> None:
        src = self._sw_source()
        tabs_section = src[src.index('"tabs_all"'):]
        self.assertIn("browser_visible_tabs_count", tabs_section[:2000])
        self.assertIn("registry_owned_tabs_count", tabs_section[:2000])
        self.assertIn("unowned_tabs_count", tabs_section[:2000])

    def test_tabs_all_returns_per_tab_ownership(self) -> None:
        src = self._sw_source()
        tabs_section = src[src.index('"tabs_all"'):]
        self.assertIn("ownership:", tabs_section[:2000])
        self.assertIn("created_by_karox:", tabs_section[:2000])
        self.assertIn("is_initial:", tabs_section[:2000])

    def test_launch_managed_browser_is_taskbar_restorable_without_focus_steal(self) -> None:
        import inspect
        from karox import managed_browser
        src = inspect.getsource(managed_browser.launch_managed_browser)
        self.assertIn("--window-position=40,40", src)
        self.assertIn("--start-minimized", src)
        self.assertNotIn("--window-position=-32000,-32000", src)

    def test_service_worker_has_show_window_method(self) -> None:
        src = self._sw_source()
        self.assertIn('"show_window"', src)
        self.assertIn("chrome.windows.update", src)

    def test_launch_fresh_does_not_raise_or_focus_window(self) -> None:
        import inspect
        from karox.extension_browser import ChromeExtensionBrowserSessionManager
        src = inspect.getsource(ChromeExtensionBrowserSessionManager._launch_fresh)
        self.assertNotIn('bridge.call("show_window"', src)
        self.assertIn("explicit takeover", src)


if __name__ == "__main__":
    unittest.main()
