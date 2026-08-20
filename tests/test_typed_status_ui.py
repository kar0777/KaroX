"""Focused tests for the B7 typed-status rendering in ServiceConnectScreen.

Verifies that the ServiceConnectScreen shows the 8 typed status lines
(Configuration / Credential / Bridge / Tunnel / OAuth / ChatGPT client /
Tool verification / Overall) computed from the live status model, and that:

* a saved profile + available credential is shown as ``stopped_ready_to_restart``,
  never ``not_configured``;
* a running bridge with public URL is shown as ``ready_for_chatgpt_setup`` or
  ``waiting_for_chatgpt``;
* the 8 fields appear in both RU and EN;
* F5/Enter re-checks live processes and credential (not just a redraw);
* no secret, token, authorization header, or Bearer value appears in the
  rendered status text.
"""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from _tui_harness import isolated_karox_directories

from karox import tui
from karox.connection_status import (
    BridgeStatus,
    ChatGPTClientStatus,
    ConfigurationStatus,
    ConnectionLiveStatus,
    CredentialStatus,
    OAuthStatus,
    OverallStatus,
    ToolVerificationStatus,
    TunnelStatus,
)


def _stopped_ready_status(saved_profile: str = "chatgpt-pc") -> ConnectionLiveStatus:
    """A typed status for a saved profile with credential but stopped bridge."""
    return ConnectionLiveStatus(
        saved_profile=saved_profile,
        preset_id="chatgpt-web",
        session_id="web-saved-test",
        configuration=ConfigurationStatus.SAVED,
        credential=CredentialStatus.AVAILABLE,
        credential_fingerprint="sha256:abc123",
        bridge=BridgeStatus.STOPPED,
        bridge_pid=None,
        bridge_identity_verified=False,
        tunnel=TunnelStatus.STOPPED,
        tunnel_pid=None,
        public_url=None,
        oauth=OAuthStatus.UNAVAILABLE,
        chatgpt_client=ChatGPTClientStatus.NOT_SEEN,
        tool_verification=ToolVerificationStatus.NOT_TESTED,
        overall=OverallStatus.STOPPED_READY_TO_RESTART,
    )


def _running_ready_status(saved_profile: str = "chatgpt-pc") -> ConnectionLiveStatus:
    """A typed status for a running bridge with public URL, waiting for ChatGPT."""
    return ConnectionLiveStatus(
        saved_profile=saved_profile,
        preset_id="chatgpt-web",
        session_id="web-saved-test",
        configuration=ConfigurationStatus.SAVED,
        credential=CredentialStatus.AVAILABLE,
        credential_fingerprint="sha256:abc123",
        bridge=BridgeStatus.RUNNING,
        bridge_pid=99999,
        bridge_identity_verified=True,
        tunnel=TunnelStatus.PUBLIC_URL_READY,
        tunnel_pid=99998,
        public_url="https://example.ts.net",
        oauth=OAuthStatus.READY,
        chatgpt_client=ChatGPTClientStatus.NOT_SEEN,
        tool_verification=ToolVerificationStatus.NOT_TESTED,
        overall=OverallStatus.READY_FOR_CHATGPT_SETUP,
    )


class TypedStatusRenderingTests(unittest.IsolatedAsyncioTestCase):
    """The ServiceConnectScreen renders 8 typed status lines from compute_live_status."""

    async def _screen(self, pilot, app, preset_id: str = "chatgpt-web", *, language: str = "en"):
        screens = app._connections_screens_cached()
        screen = screens["ServiceConnectScreen"](language, preset_id=preset_id)
        app.push_screen(screen)
        await pilot.pause()
        return screen

    async def test_stopped_profile_shows_stopped_ready_to_restart_not_not_configured(self) -> None:
        """A saved profile + credential must show stopped_ready_to_restart, not not_configured."""
        fake_profile = [{"saved_profile": "chatgpt-pc", "session_id": "web-saved-test", "public_url": "", "bridge_pid": None, "tunnel_pid": None, "profile": "chatgpt-web", "tunnel": "tailscale"}]
        with isolated_karox_directories() as repository:
            fake_profile[0]["repository"] = str(repository)
            app = tui.KaroXApp(repository, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                with patch(
                    "karox.web_bridge_launcher.discover_saved_bridge_profiles_for_preset",
                    return_value=fake_profile,
                ), patch(
                    "karox.tui_connections.compute_live_status",
                    return_value=_stopped_ready_status(),
                ):
                    screen = await self._screen(pilot, app)
                    screen.refresh_view()
                    await pilot.pause()
                    text = screen._typed_status_text(screen._typed_status)
        self.assertIsNotNone(screen._typed_status)
        self.assertIn("ready to restart", text.lower())
        self.assertNotIn("not configured", text.lower())

    async def test_running_bridge_shows_ready_for_chatgpt_setup(self) -> None:
        """A running bridge with public URL shows ready_for_chatgpt_setup."""
        fake_profile = [{"saved_profile": "chatgpt-pc", "session_id": "web-saved-test", "public_url": "https://example.ts.net", "bridge_pid": 99999, "tunnel_pid": 99998, "profile": "chatgpt-web", "tunnel": "tailscale"}]
        with isolated_karox_directories() as repository:
            fake_profile[0]["repository"] = str(repository)
            app = tui.KaroXApp(repository, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                with patch(
                    "karox.web_bridge_launcher.discover_saved_bridge_profiles_for_preset",
                    return_value=fake_profile,
                ), patch(
                    "karox.tui_connections.compute_live_status",
                    return_value=_running_ready_status(),
                ):
                    screen = await self._screen(pilot, app)
                    screen.refresh_view()
                    await pilot.pause()
                    text = screen._typed_status_text(screen._typed_status)
        self.assertIsNotNone(screen._typed_status)
        self.assertIn("ready for chatgpt setup", text.lower())

    async def test_normal_screen_shows_one_actionable_summary(self) -> None:
        """The primary screen is user guidance; the 8 fields belong to diagnostics."""
        fake_profile = [{"saved_profile": "chatgpt-pc", "session_id": "web-saved-test", "public_url": "https://example.ts.net", "bridge_pid": 99999, "tunnel_pid": 99998, "profile": "chatgpt-web", "tunnel": "tailscale"}]
        with isolated_karox_directories() as repository:
            fake_profile[0]["repository"] = str(repository)
            app = tui.KaroXApp(repository, language="ru")
            async with app.run_test(size=(120, 42)) as pilot:
                with patch(
                    "karox.web_bridge_launcher.discover_saved_bridge_profiles_for_preset",
                    return_value=fake_profile,
                ), patch(
                    "karox.tui_connections.compute_live_status",
                    return_value=_running_ready_status(),
                ):
                    screen = await self._screen(pilot, app, language="ru")
                    screen.refresh_view()
                    await pilot.pause()
                    text = screen.status_text()
        self.assertIn("KaroX готов", text)
        self.assertIn("URL", text)
        for technical in ("Конфигурация:", "Credential:", "Bridge:", "Tunnel:", "OAuth:"):
            self.assertNotIn(technical, text)

    async def test_eight_typed_fields_appear_in_english(self) -> None:
        """All 8 typed status fields remain available for diagnostics (EN)."""
        fake_profile = [{"saved_profile": "chatgpt-pc", "session_id": "web-saved-test", "public_url": "", "bridge_pid": None, "tunnel_pid": None, "profile": "chatgpt-web", "tunnel": "tailscale"}]
        with isolated_karox_directories() as repository:
            fake_profile[0]["repository"] = str(repository)
            app = tui.KaroXApp(repository, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                with patch(
                    "karox.web_bridge_launcher.discover_saved_bridge_profiles_for_preset",
                    return_value=fake_profile,
                ), patch(
                    "karox.tui_connections.compute_live_status",
                    return_value=_stopped_ready_status(),
                ):
                    screen = await self._screen(pilot, app, language="en")
                    screen.refresh_view()
                    await pilot.pause()
                    text = screen._typed_status_text(screen._typed_status)
        self.assertIsNotNone(screen._typed_status)
        for field in (
            "Configuration:",
            "Credential:",
            "Bridge:",
            "Tunnel:",
            "OAuth:",
            "ChatGPT client:",
            "Tool verification:",
            "Overall:",
        ):
            self.assertIn(field, text)

    async def test_eight_typed_fields_appear_in_russian(self) -> None:
        """All 8 typed status fields appear in the rendered text (RU)."""
        fake_profile = [{"saved_profile": "chatgpt-pc", "session_id": "web-saved-test", "public_url": "", "bridge_pid": None, "tunnel_pid": None, "profile": "chatgpt-web", "tunnel": "tailscale"}]
        with isolated_karox_directories() as repository:
            fake_profile[0]["repository"] = str(repository)
            app = tui.KaroXApp(repository, language="ru")
            async with app.run_test(size=(120, 42)) as pilot:
                with patch(
                    "karox.web_bridge_launcher.discover_saved_bridge_profiles_for_preset",
                    return_value=fake_profile,
                ), patch(
                    "karox.tui_connections.compute_live_status",
                    return_value=_stopped_ready_status(),
                ):
                    screen = await self._screen(pilot, app, language="ru")
                    screen.refresh_view()
                    await pilot.pause()
                    text = screen._typed_status_text(screen._typed_status)
        self.assertIsNotNone(screen._typed_status)
        for field in (
            "Конфигурация:",
            "Credential:",
            "Bridge:",
            "Tunnel:",
            "OAuth:",
            "ChatGPT клиент:",
            "Проверка инструментов:",
            "Общее:",
        ):
            self.assertIn(field, text)

    async def test_no_secret_in_typed_status_text(self) -> None:
        """The typed status text never contains a secret, token, or Bearer value."""
        fake_profile = [{"saved_profile": "chatgpt-pc", "session_id": "web-saved-test", "public_url": "https://example.ts.net", "bridge_pid": 99999, "tunnel_pid": 99998, "profile": "chatgpt-web", "tunnel": "tailscale"}]
        with isolated_karox_directories() as repository:
            fake_profile[0]["repository"] = str(repository)
            app = tui.KaroXApp(repository, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                with patch(
                    "karox.web_bridge_launcher.discover_saved_bridge_profiles_for_preset",
                    return_value=fake_profile,
                ), patch(
                    "karox.tui_connections.compute_live_status",
                    return_value=_running_ready_status(),
                ):
                    screen = await self._screen(pilot, app)
                    screen.refresh_view()
                    await pilot.pause()
                    text = screen._typed_status_text(screen._typed_status)
        self.assertIsNotNone(screen._typed_status)
        forbidden = ("bearer", "secret", "token", "authorization", "password", "api_key")
        lower = text.lower()
        for word in forbidden:
            self.assertNotIn(word, lower, f"typed status text contains forbidden word '{word}'")
        # The fingerprint is available on the typed status object but NOT rendered
        # in the status text (it is a display-safe reference, not the secret).
        self.assertEqual(screen._typed_status.credential_fingerprint, "sha256:abc123")


class TypedStatusRecheckTests(unittest.IsolatedAsyncioTestCase):
    """F5/Enter re-checks live processes and credential, not just a redraw."""

    async def _screen(self, pilot, app, preset_id: str = "chatgpt-web", *, language: str = "en"):
        screens = app._connections_screens_cached()
        screen = screens["ServiceConnectScreen"](language, preset_id=preset_id)
        app.push_screen(screen)
        await pilot.pause()
        return screen

    async def test_verify_rechecks_typed_status_for_discovered_profile(self) -> None:
        """F5/Enter on a discovered profile re-runs compute_live_status."""
        fake_profile = [{"saved_profile": "chatgpt-pc", "session_id": "web-saved-test", "public_url": "https://example.ts.net", "bridge_pid": 99999, "tunnel_pid": 99998, "profile": "chatgpt-web", "tunnel": "tailscale"}]
        with isolated_karox_directories() as repository:
            fake_profile[0]["repository"] = str(repository)
            app = tui.KaroXApp(repository, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                with patch(
                    "karox.web_bridge_launcher.discover_saved_bridge_profiles_for_preset",
                    return_value=fake_profile,
                ), patch(
                    "karox.tui_connections.compute_live_status",
                    return_value=_stopped_ready_status(),
                ) as mock_compute:
                    screen = await self._screen(pilot, app)
                    screen.refresh_view()
                    await pilot.pause()
                    # compute_live_status was called during refresh_view.
                    self.assertGreaterEqual(mock_compute.call_count, 1)
                    # Now trigger a verify re-check.
                    screen.action_verify()
                    await pilot.pause()
                    await pilot.pause()
                    # compute_live_status was called again by the re-check.
                    self.assertGreaterEqual(mock_compute.call_count, 2)


class StatusFromTypedMappingTests(unittest.TestCase):
    """The _status_from_typed mapping converts OverallStatus to SERVICE_* constants."""

    def _screen_cls(self):
        """Get the ServiceConnectScreen class without instantiating the app."""
        # build_connections_screens returns a dict of screen classes bound to the app.
        # We need a minimal app-like object to build them.
        fake_app = MagicMock()
        fake_app._copy_text = MagicMock()
        from karox.tui_connections import build_connections_screens
        return build_connections_screens(fake_app)["ServiceConnectScreen"]

    def test_fully_verified_maps_to_working(self) -> None:
        from karox.tui_connections import SERVICE_WORKING
        screen_cls = self._screen_cls()
        typed = ConnectionLiveStatus(
            configuration=ConfigurationStatus.SAVED,
            credential=CredentialStatus.AVAILABLE,
            bridge=BridgeStatus.RUNNING,
            tunnel=TunnelStatus.PUBLIC_URL_READY,
            oauth=OAuthStatus.AUTHORIZED,
            chatgpt_client=ChatGPTClientStatus.CONNECTED,
            tool_verification=ToolVerificationStatus.PASSED,
            overall=OverallStatus.FULLY_VERIFIED,
        )
        # _status_from_typed is an instance method, but it's pure (only reads self).
        # Create a minimal instance to call it.
        instance = screen_cls.__new__(screen_cls)
        self.assertEqual(instance._status_from_typed(typed), SERVICE_WORKING)

    def test_stopped_ready_maps_to_awaiting(self) -> None:
        from karox.tui_connections import SERVICE_AWAITING
        screen_cls = self._screen_cls()
        typed = _stopped_ready_status()
        instance = screen_cls.__new__(screen_cls)
        self.assertEqual(instance._status_from_typed(typed), SERVICE_AWAITING)

    def test_not_configured_maps_to_not_configured(self) -> None:
        from karox.tui_connections import SERVICE_NOT_CONFIGURED
        screen_cls = self._screen_cls()
        instance = screen_cls.__new__(screen_cls)
        self.assertEqual(instance._status_from_typed(None), SERVICE_NOT_CONFIGURED)


if __name__ == "__main__":
    unittest.main()
