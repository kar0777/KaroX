from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any
from unittest import mock
from unittest.mock import patch

import pytest

from _tui_harness import isolated_karox_directories
from karox import tui
from karox.connection_status import OverallStatus
from karox.models import AccessProfile
from karox.tui_connections import (
    _apply_saved_web_profile_object,
    attach_saved_browser_credential,
    build_saved_web_profile_settings,
    detach_saved_browser_credential,
    saved_web_profile_settings,
)
from karox.web_bridge_profiles import SavedWebBridgeProfile


def _profile(**overrides: Any) -> SavedWebBridgeProfile:
    values: dict[str, Any] = {
        "name": "chatgpt-dev",
        "target_profile": "chatgpt-web",
        "tools": ("karox.repo.read_file",),
        "access_profile": AccessProfile.WORKSPACE_WRITE,
        "tunnel": "tailscale",
        "port": 8765,
        "browser_external_https": True,
        "browser_headed": True,
        "browser_user_takeover": True,
        "browser_network_inspection": True,
        "browser_allowed_domains": (),
        "browser_denied_domains": ("blocked.example",),
        "browser_allowed_emails": ("robot@example.invalid",),
        "browser_credential_refs": ("os-keyring:browser/existing-test",),
    }
    values.update(overrides)
    return SavedWebBridgeProfile(**values)


class _ProfileStore:
    def __init__(
        self,
        profile: SavedWebBridgeProfile,
        siblings: tuple[SavedWebBridgeProfile, ...] = (),
    ) -> None:
        self.profile = profile
        self.siblings = siblings
        self.puts: list[SavedWebBridgeProfile] = []

    def get(self, name: str) -> SavedWebBridgeProfile:
        assert name == self.profile.name
        return self.profile

    def list(self) -> tuple[SavedWebBridgeProfile, ...]:
        return (self.profile, *self.siblings)

    def put(self, profile: SavedWebBridgeProfile, *args: Any, **kwargs: Any) -> None:
        del args, kwargs
        self.profile = profile
        self.puts.append(profile)


def test_form_projection_and_builder_round_trip_browser_policy() -> None:
    current = _profile()
    projected = saved_web_profile_settings(current)
    assert projected["port"] == "8765"
    assert projected["browser_user_takeover"] is True
    assert projected["browser_credential_refs"] == (
        "os-keyring:browser/existing-test",
    )

    updated = build_saved_web_profile_settings(
        current,
        {
            **projected,
            "port": "9876",
            "tunnel": "custom",
            "public_url": "https://bridge.example",
            "browser_allowed_domains": "example.com, docs.example.com",
            "browser_denied_domains": "blocked.example\ntracker.example",
            "browser_allowed_emails": "robot@example.invalid, second@example.invalid",
        },
    )
    assert updated.port == 9876
    assert updated.tunnel == "custom"
    assert updated.public_url == "https://bridge.example"
    assert updated.browser_allowed_domains == ("example.com", "docs.example.com")
    assert updated.browser_denied_domains == (
        "blocked.example",
        "tracker.example",
    )
    assert updated.browser_credential_refs == current.browser_credential_refs


def test_builder_rejects_takeover_without_visible_managed_browser() -> None:
    current = _profile()
    values = saved_web_profile_settings(current)
    values["browser_headed"] = False
    values["browser_user_takeover"] = True
    with pytest.raises(ValueError, match="visible managed browser"):
        build_saved_web_profile_settings(current, values)


def test_tui_profile_apply_delegates_restart_consent_to_canonical_service() -> None:
    previous = _profile()
    updated = _profile(port=9876)
    with mock.patch(
        "karox.web_bridge_launcher.apply_saved_bridge_profile",
        return_value={"status": "restarted", "profile": updated},
    ) as apply:
        result = _apply_saved_web_profile_object(
            previous.name,
            updated,
            bridge_running=True,
        )

    assert result["status"] == "restarted"
    apply.assert_called_once_with(
        previous.name,
        updated,
        allow_restart=True,
    )


def test_tui_profile_apply_does_not_grant_restart_for_stopped_snapshot() -> None:
    previous = _profile()
    updated = _profile(port=9876)
    with mock.patch(
        "karox.web_bridge_launcher.apply_saved_bridge_profile",
        return_value={"status": "saved", "profile": updated},
    ) as apply:
        result = _apply_saved_web_profile_object(
            previous.name,
            updated,
            bridge_running=False,
        )

    assert result["status"] == "saved"
    apply.assert_called_once_with(
        previous.name,
        updated,
        allow_restart=False,
    )


def test_attach_credential_persists_only_opaque_ref_without_mutating_plain_email_policy() -> None:
    previous = _profile(browser_credential_refs=(), browser_allowed_emails=())
    store = _ProfileStore(previous)
    credential_store = mock.Mock()
    metadata = {
        "reference": "os-keyring:browser/gmail-test",
        "fingerprint": "sha256:abcdef123456",
        "backend": "os-keyring",
    }
    credential_store.validate_bundle.return_value = metadata
    credential_store.set.return_value = metadata

    with (
        mock.patch("karox.web_bridge_profiles.WebBridgeProfileStore", return_value=store),
        mock.patch("karox.browser_credentials.BrowserCredentialStore", return_value=credential_store),
        mock.patch(
            "karox.web_bridge_launcher.apply_saved_bridge_profile",
            return_value={"status": "saved"},
        ) as apply,
    ):
        result = attach_saved_browser_credential(
            previous.name,
            name="gmail-test",
            username="Robot@Example.Invalid",
            password="local-test-password",
            bridge_running=False,
        )

    credential_store.validate_bundle.assert_called_once_with(
        "gmail-test",
        username="Robot@Example.Invalid",
        password="local-test-password",
    )
    credential_store.set.assert_called_once_with(
        "gmail-test",
        username="Robot@Example.Invalid",
        password="local-test-password",
    )
    assert result["reference"] == "os-keyring:browser/gmail-test"
    updated = apply.call_args.args[1]
    assert updated.browser_credential_refs == ("os-keyring:browser/gmail-test",)
    assert updated.browser_allowed_emails == ()
    assert "local-test-password" not in repr(updated.to_dict())
    assert apply.call_args.kwargs["allow_restart"] is False


def test_attach_keyring_failure_rolls_profile_authority_back_without_deleting_old_secret() -> None:
    previous = _profile(browser_credential_refs=())
    store = _ProfileStore(previous)
    credential_store = mock.Mock()
    credential_store.validate_bundle.return_value = {
        "reference": "os-keyring:browser/gmail-test",
        "fingerprint": "sha256:abcdef123456",
        "backend": "os-keyring",
    }
    credential_store.set.side_effect = RuntimeError("synthetic keyring failure")
    applied_profiles: list[SavedWebBridgeProfile] = []

    def apply(_name: str, profile: SavedWebBridgeProfile, **_kwargs: Any) -> dict[str, Any]:
        applied_profiles.append(profile)
        return {"status": "saved"}

    with (
        mock.patch("karox.web_bridge_profiles.WebBridgeProfileStore", return_value=store),
        mock.patch("karox.browser_credentials.BrowserCredentialStore", return_value=credential_store),
        mock.patch("karox.web_bridge_launcher.apply_saved_bridge_profile", side_effect=apply),
    ):
        with pytest.raises(RuntimeError, match="synthetic keyring failure"):
            attach_saved_browser_credential(
                previous.name,
                name="gmail-test",
                username="Robot@Example.Invalid",
                password="local-test-password",
                bridge_running=False,
            )

    assert len(applied_profiles) == 2
    assert applied_profiles[0].browser_credential_refs == (
        "os-keyring:browser/gmail-test",
    )
    assert applied_profiles[1] == previous
    credential_store.delete.assert_not_called()


def test_detach_removes_runtime_authority_before_best_effort_keyring_delete() -> None:
    previous = _profile(
        browser_credential_refs=("os-keyring:browser/gmail-test",),
    )
    store = _ProfileStore(previous)
    credential_store = mock.Mock()

    with (
        mock.patch("karox.web_bridge_profiles.WebBridgeProfileStore", return_value=store),
        mock.patch("karox.browser_credentials.BrowserCredentialStore", return_value=credential_store),
        mock.patch(
            "karox.web_bridge_launcher.apply_saved_bridge_profile",
            return_value={"status": "saved"},
        ) as apply,
    ):
        result = detach_saved_browser_credential(
            previous.name,
            "os-keyring:browser/gmail-test",
            bridge_running=False,
        )

    assert result["detached"] is True
    assert result["keyring_deleted"] is True
    updated = apply.call_args.args[1]
    assert updated.browser_credential_refs == ()
    assert apply.call_args.kwargs["allow_restart"] is False
    credential_store.delete.assert_called_once_with("gmail-test")


def test_detach_keeps_keyring_entry_when_another_profile_uses_same_ref() -> None:
    reference = "os-keyring:browser/shared-test"
    previous = _profile(browser_credential_refs=(reference,))
    sibling = _profile(name="claude-dev", browser_credential_refs=(reference,))
    store = _ProfileStore(previous, (sibling,))
    credential_store = mock.Mock()

    with (
        mock.patch("karox.web_bridge_profiles.WebBridgeProfileStore", return_value=store),
        mock.patch("karox.browser_credentials.BrowserCredentialStore", return_value=credential_store),
        mock.patch(
            "karox.web_bridge_launcher.apply_saved_bridge_profile",
            return_value={"status": "saved"},
        ),
    ):
        result = detach_saved_browser_credential(
            previous.name,
            reference,
            bridge_running=False,
        )

    assert result["detached"] is True
    assert result["keyring_shared"] is True
    assert result["keyring_deleted"] is False
    credential_store.delete.assert_not_called()


class SavedWebProfileTextualTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = self.enterContext(isolated_karox_directories())
        self.enterContext(patch.object(tui, "_load_language", return_value="en"))
        self.enterContext(patch.object(tui, "_selected_model", return_value=None))
        from karox.web_bridge_profiles import WebBridgeProfileStore

        WebBridgeProfileStore().put(
            _profile(repository=str(self.repository), name="chatgpt-dev"),
        )

    def app(self) -> tui.KaroXApp:
        return tui.KaroXApp(self.repository, language="en")

    async def test_saved_profile_screen_renders_real_browser_policy_and_cancel_is_noop(self) -> None:
        from textual.widgets import Input, RadioButton, RadioSet, Switch
        from karox.web_bridge_profiles import WebBridgeProfileStore

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            screen = screens["SavedWebProfileSettingsScreen"](
                "en",
                profile_name="chatgpt-dev",
                bridge_running=False,
            )
            app.push_screen(screen)
            await pilot.pause()
            self.assertEqual(screen.query_one("#swp-port", Input).value, "8765")
            tunnel = screen.query_one("#swp-tunnel", RadioSet)
            self.assertEqual(tunnel.pressed_index, 0)
            self.assertEqual(str(screen.query_one("#swp-public-url", Input).styles.display), "none")
            screen.query_one("#swp-tunnel-custom", RadioButton).value = True
            await pilot.pause()
            self.assertEqual(screen._selected_tunnel(), "custom")
            self.assertNotEqual(str(screen.query_one("#swp-public-url", Input).styles.display), "none")
            screen.query_one("#swp-tunnel-tailscale", RadioButton).value = True
            await pilot.pause()
            self.assertTrue(screen.query_one("#swp-external", Switch).value)
            self.assertTrue(screen.query_one("#swp-headed", Switch).value)
            self.assertTrue(screen.query_one("#swp-takeover", Switch).value)
            screen.query_one("#swp-port", Input).value = "9999"
            screen.action_cancel()
            await pilot.pause()

        self.assertEqual(WebBridgeProfileStore().get("chatgpt-dev").port, 8765)

    async def test_advanced_browser_switches_are_compact_not_black_half_width_panels(self) -> None:
        from textual.widgets import Switch

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            screen = screens["SavedWebProfileSettingsScreen"](
                "en",
                profile_name="chatgpt-dev",
                bridge_running=False,
            )
            app.push_screen(screen)
            await pilot.pause()
            dialog = screen.query_one("#swp-dialog")
            for selector in (
                "#swp-external",
                "#swp-headed",
                "#swp-takeover",
                "#swp-network",
                "#swp-payment",
            ):
                switch = screen.query_one(selector, Switch)
                self.assertLessEqual(switch.size.width, 8)
                self.assertLess(switch.size.width, dialog.size.width // 3)
            screen.action_cancel()
            await pilot.pause()

    async def test_service_actions_are_a_non_scrolling_grid_inside_the_dialog(self) -> None:
        from textual.containers import Grid, HorizontalScroll

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            service = screens["ServiceConnectScreen"]("en", preset_id="chatgpt-web")
            app.push_screen(service)
            await pilot.pause()
            actions = service.query_one("#svc-actions")
            dialog = service.query_one("#svc-dialog")
            self.assertIsInstance(actions, Grid)
            self.assertNotIsInstance(actions, HorizontalScroll)
            self.assertGreaterEqual(actions.region.x, dialog.region.x)
            self.assertLessEqual(actions.region.right, dialog.region.right)
            service.action_cancel()
            await pilot.pause()

    async def test_browser_credential_screen_uses_password_input_and_never_renders_secret(self) -> None:
        from textual.widgets import Input, OptionList

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            screen = screens["BrowserCredentialManagerScreen"](
                "en",
                profile_name="chatgpt-dev",
                bridge_running=False,
            )
            app.push_screen(screen)
            await pilot.pause()
            password = screen.query_one("#bc-password", Input)
            self.assertTrue(password.password)
            options = screen.query_one("#bc-attached-list", OptionList)
            self.assertEqual(options.option_count, 1)
            self.assertEqual(options.highlighted, 0)
            self.assertEqual(
                screen._selected_ref(),
                "os-keyring:browser/existing-test",
            )
            password.value = "should-never-render"
            rendered = "\n".join(
                str(widget.render())
                for widget in screen.query("Static")
            )
            self.assertNotIn("should-never-render", rendered)
            screen.action_close()
            await pilot.pause()

    async def test_live_browser_credential_attach_requires_restart_confirmation(self) -> None:
        from textual.widgets import Input

        app = self.app()
        pushed: list[Any] = []
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            screen = screens["BrowserCredentialManagerScreen"](
                "en",
                profile_name="chatgpt-dev",
                bridge_running=True,
            )
            app.push_screen(screen)
            await pilot.pause()
            screen.query_one("#bc-name", Input).value = "new-test"
            screen.query_one("#bc-username", Input).value = "robot@example.invalid"
            screen.query_one("#bc-password", Input).value = "local-only-password"
            with (
                patch.object(
                    app,
                    "push_screen",
                    side_effect=lambda modal, *_args, **_kwargs: pushed.append(modal),
                ),
                patch("karox.tui_connections.attach_saved_browser_credential") as attach,
            ):
                screen._save()
                await pilot.pause()
            self.assertEqual([type(item).__name__ for item in pushed], ["_ConfirmScreen"])
            attach.assert_not_called()
            screen.action_close()
            await pilot.pause()

    async def test_enabling_payment_permission_requires_separate_confirmation(self) -> None:
        from textual.widgets import Switch

        app = self.app()
        pushed: list[Any] = []
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            screen = screens["SavedWebProfileSettingsScreen"](
                "en",
                profile_name="chatgpt-dev",
                bridge_running=False,
            )
            app.push_screen(screen)
            await pilot.pause()
            screen.query_one("#swp-payment", Switch).value = True
            with (
                patch.object(
                    app,
                    "push_screen",
                    side_effect=lambda modal, *_args, **_kwargs: pushed.append(modal),
                ),
                patch("karox.tui_connections.apply_saved_web_profile_settings") as apply,
            ):
                screen.action_save()
                await pilot.pause()
            self.assertEqual([type(item).__name__ for item in pushed], ["_ConfirmScreen"])
            apply.assert_not_called()
            screen.action_cancel()
            await pilot.pause()

    async def test_notion_settings_hide_irrelevant_browser_controls(self) -> None:
        from karox.web_bridge_profiles import WebBridgeProfileStore

        WebBridgeProfileStore().put(
            _profile(
                name="notion-dev",
                target_profile="notion",
                repository=str(self.repository),
                browser_external_https=False,
                browser_headed=False,
                browser_user_takeover=False,
                browser_network_inspection=False,
                browser_denied_domains=(),
                browser_allowed_emails=(),
                browser_credential_refs=(),
            )
        )
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            screen = screens["SavedWebProfileSettingsScreen"](
                "en",
                profile_name="notion-dev",
                bridge_running=False,
            )
            app.push_screen(screen)
            await pilot.pause()
            self.assertEqual(list(screen.query("#swp-external")), [])
            self.assertEqual(list(screen.query("#swp-credentials")), [])
            values = screen.values()
            self.assertNotIn("browser_external_https", values)
            screen.action_cancel()
            await pilot.pause()

    async def test_service_advanced_routes_saved_web_profile_to_real_editor(self) -> None:
        app = self.app()
        pushed: list[Any] = []
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            service = screens["ServiceConnectScreen"]("en", preset_id="chatgpt-web")
            service._saved_profile_name = lambda: "chatgpt-dev"  # type: ignore[method-assign]
            app.push_screen(service)
            await pilot.pause()
            with patch.object(
                app,
                "push_screen",
                side_effect=lambda screen, *_args, **_kwargs: pushed.append(screen),
            ):
                service.action_advanced()
                await pilot.pause()

        self.assertEqual(
            [type(screen).__name__ for screen in pushed],
            ["SavedWebProfileSettingsScreen"],
        )

    async def test_service_diagnostics_prioritizes_recovery_health_over_process_ids(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            service = screens["ServiceConnectScreen"]("en", preset_id="chatgpt-web")
            app.push_screen(service)
            await pilot.pause()
            service._has_snapshot = True
            service._last_state = SimpleNamespace(
                target=SimpleNamespace(name="chatgpt-dev", connection_id="saved-chatgpt"),
                runtime={
                    "state": "running",
                    "bridge_pid": 111,
                    "tunnel_pid": 222,
                    "session_id": "web-saved-test",
                },
                endpoint="https://bridge.example/mcp",
                state="running",
            )
            service._typed_status = SimpleNamespace(overall=OverallStatus.FULLY_VERIFIED)
            with patch(
                "karox.saved_bridge_supervisor.saved_bridge_supervisor_status",
                return_value={
                    "desired_running": True,
                    "supervisor_alive": True,
                    "supervisor_heartbeat_fresh": True,
                    "restart_count": 2,
                    "supervisor_heartbeat_age_seconds": 0.2,
                },
            ):
                service.action_diagnostics()
                await pilot.pause()
            diagnostics = app.screen
            self.assertEqual(type(diagnostics).__name__, "ServiceDiagnosticsScreen")
            summary = str(diagnostics.query_one("#svc-diag-summary").render())
            technical = str(diagnostics.query_one("#svc-diag-tech-body").render())
            self.assertIn("Auto-recovery: active", summary)
            self.assertIn("Automatic restarts: 2", summary)
            self.assertIn("Managed browser: HTTPS + takeover ready", summary)
            self.assertNotIn("Bridge PID", summary)
            self.assertIn("Bridge PID: 111", technical)
            self.assertIn("Session ID: web-saved-test", technical)
            diagnostics.action_close()
            await pilot.pause()
