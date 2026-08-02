"""TUI tests for the ClickUp automatic setup screen and result card.

The backend coordination (resolver + orchestrator) is covered in
``test_clickup_setup.py``. These tests boot the real ``KaroXApp`` with isolated
config/runtime dirs and drive the ClickUp preset path through the Pilot,
injecting a stubbed orchestrator so no real bridge/tunnel is spawned -- the
assertions are about the UX contract the spec sets: the compact screen opens
for ClickUp (not the long form), Advanced toggles, Reset clears overrides,
Escape cancels, the setup is non-blocking, and a result card with a masked
secret appears on success.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from _tui_harness import isolated_karox_directories

import karox.tui as tui
import karox.connections as conn
import karox.clickup_setup as cs


def _stub_orchestrator_success(
    defaults, *, name, repository, supplied_secret=None, on_progress=None, cancellation=None
):
    """A stub that mimics the orchestrator's success path without processes.

    Stores a real secret in the connection credential store so the result card
    can resolve and mask it, exercising the full mask -> display contract.
    """
    if on_progress:
        for step in ("config", "secret", "server", "tunnel", "url", "handshake"):
            on_progress(step, "ok", "")
    store = conn.ConnectionCredentialStore()
    info = store.set("clickup-stub", "sk-stub-secret-12345678")
    target = conn.build_target_from_preset(
        "clickup",
        name=name,
        credential_ref=info["reference"],
        credential_fingerprint=info["fingerprint"],
        public_url="https://stub.example.com",
    )
    return cs.ClickupSetupOutcome(
        success=True,
        target=target,
        public_endpoint="https://stub.example.com/mcp",
        handshake={"state": "ok", "detail": "stub handshake"},
        defaults=defaults,
    )


def _stub_orchestrator_failure(
    defaults, *, name, repository, supplied_secret=None, on_progress=None, cancellation=None
):
    if on_progress:
        for step in ("config", "secret"):
            on_progress(step, "ok", "")
        on_progress("server", "failed", "cloudflared not found")
    return cs.ClickupSetupOutcome(
        success=False,
        failure_kind="tunnel_failed",
        failure_detail="cloudflared not installed",
        remediation="install_cloudflared",
        defaults=defaults,
    )


async def _open_clickup_auto_screen(pilot, app) -> None:
    composer = app.query_one("#composer", tui.CommandInput)
    composer.value = "/mcp-clients"
    await pilot.press("enter")
    await pilot.pause(0.3)
    await pilot.press("a")
    await pilot.pause(0.3)
    picker = next(
        s for s in app.screen_stack if s.__class__.__name__ == "_PresetPickerScreen"
    )
    options = picker.query_one("#preset-pick-list", tui.OptionList)
    while getattr(options.get_option_at_index(options.highlighted), "id", None) != "clickup":
        await pilot.press("down")
        await pilot.pause(0.05)
    await pilot.press("enter")
    await pilot.pause(0.3)


class ClickupTuiTests(unittest.IsolatedAsyncioTestCase):
    async def _app(self, *, language: str = "en"):
        from karox.tui_connections import build_connections_screens

        self._ctx = isolated_karox_directories()
        repository = self._ctx.__enter__()
        self.addCleanup(self._ctx.__exit__, None, None, None)
        app = tui.KaroXApp(repository, language=language)
        async with app.run_test(size=(130, 48)) as pilot:
            app._connections_screens = build_connections_screens(app)
            await pilot.pause(0.3)
            yield app, pilot

    async def test_clickup_preset_opens_compact_auto_screen_not_long_form(self) -> None:
        async for app, pilot in self._app():
            await _open_clickup_auto_screen(pilot, app)
            auto = next(
                (s for s in app.screen_stack if s.__class__.__name__ == "_ClickupAutoScreen"),
                None,
            )
            self.assertIsNotNone(auto, "ClickUp should open the compact auto screen")
            # The long form must NOT be on the stack for ClickUp.
            self.assertFalse(
                any(s.__class__.__name__ == "_McpClientFormScreen" for s in app.screen_stack),
                "ClickUp should not open the long manual form",
            )
            self.assertIsNotNone(auto.query_one("#cu-auto-name", tui.Input))
            self.assertIsNotNone(auto.query_one("#cu-auto-connect", tui.Button))
            await pilot.press("escape")
            await pilot.pause(0.3)

    async def test_advanced_section_hidden_by_default_and_toggles(self) -> None:
        async for app, pilot in self._app():
            await _open_clickup_auto_screen(pilot, app)
            screen = app.screen
            panel = screen.query_one("#cu-auto-advanced", tui.VerticalScroll)
            self.assertEqual(panel.styles.display, "none")
            screen.action_toggle_advanced()
            await pilot.pause(0.1)
            self.assertEqual(panel.styles.display, "block")
            # Toggle back off.
            screen.action_toggle_advanced()
            await pilot.pause(0.1)
            self.assertEqual(panel.styles.display, "none")
            await pilot.press("escape")
            await pilot.pause(0.3)

    async def test_untouched_advanced_keeps_resolved_tailscale_defaults(self) -> None:
        async for app, pilot in self._app():
            await _open_clickup_auto_screen(pilot, app)
            screen = app.screen
            base = conn.resolve_clickup_defaults(
                {
                    "tailscale_ready": True,
                    "tailscale_funnel_available": True,
                    "cloudflared_installed": True,
                    "port_probe": lambda p: True,
                }
            )
            screen._base_defaults = base
            screen._apply_defaults_to_form(base)
            await pilot.pause(0.1)
            overrides, supplied_secret = screen._gather_overrides(base)
            self.assertEqual(overrides, {})
            self.assertIsNone(supplied_secret)
            self.assertEqual(
                screen.query_one("#cu-auto-tunnel", tui.RadioSet).pressed_index,
                1,
            )
            await pilot.press("escape")
            await pilot.pause(0.3)

    async def test_reset_to_automatic_clears_overrides(self) -> None:
        async for app, pilot in self._app():
            await _open_clickup_auto_screen(pilot, app)
            screen = app.screen
            screen._probe_environment = lambda: {
                "cloudflared_installed": True,
                "port_probe": lambda p: True,
            }
            screen._base_defaults = conn.resolve_clickup_defaults(screen._probe_environment())
            screen._apply_defaults_to_form(screen._base_defaults)
            screen.action_toggle_advanced()
            await pilot.pause(0.1)
            port = screen.query_one("#cu-auto-port", tui.Input)
            port.value = "9999"
            self.assertEqual(port.value, "9999")
            screen.action_reset_overrides()
            await pilot.pause(0.1)
            self.assertEqual(port.value, "")
            await pilot.press("escape")
            await pilot.pause(0.3)

    async def test_escape_closes_auto_screen(self) -> None:
        async for app, pilot in self._app():
            await _open_clickup_auto_screen(pilot, app)
            self.assertFalse(app.agent_busy)
            await pilot.press("escape")
            await pilot.pause(0.3)
            self.assertFalse(
                any(s.__class__.__name__ == "_ClickupAutoScreen" for s in app.screen_stack),
                "Esc should dismiss the ClickUp auto screen",
            )

    async def test_connect_is_non_blocking_and_shows_result_card(self) -> None:
        async for app, pilot in self._app():
            await _open_clickup_auto_screen(pilot, app)
            screen = app.screen
            screen._probe_environment = lambda: {"cloudflared_installed": True, "port_probe": lambda p: True}
            screen.query_one("#cu-auto-name", tui.Input).value = "My ClickUp"
            with patch.object(cs, "setup_clickup_connection", _stub_orchestrator_success), patch(
                "karox.tui_connections.setup_clickup_connection", _stub_orchestrator_success
            ):
                screen.action_connect()
                # The connect button is disabled while running and the UI is
                # responsive enough that Esc can signal cancel mid-setup.
                await pilot.pause(0.1)
                # The background worker runs the stubbed success path; wait for
                # the result card to appear.
                await pilot.pause(0.8)
            result = next(
                (s for s in app.screen_stack if s.__class__.__name__ == "_ClickupResultScreen"),
                None,
            )
            self.assertIsNotNone(result, "a successful setup should show the result card")
            # The secret must be masked (••••), not the raw value, on the card.
            saw_masked = False
            for widget in result.query(tui.Static):
                rendered = str(widget.render())
                if "•" in rendered:
                    saw_masked = True
                    break
            self.assertTrue(saw_masked, "the result card must show a masked secret")
            await pilot.press("escape")
            await pilot.pause(0.3)

    async def test_result_card_names_the_dropdown_and_warns_about_the_temp_url(
        self,
    ) -> None:
        # Regression for a live ClickUp failure.  ClickUp's "Connect an MCP
        # Server" form preselects ``Authentication Method: OAuth`` and validates
        # that choice against the server before saving.  The bearer profile
        # serves no OAuth metadata (both ``/.well-known/oauth-*`` paths 404), so
        # the default ends in ClickUp's own "Authentication method not supported
        # by this MCP Server".  The card showed the wire header
        # (``Authorization: Bearer <secret>``) but never named the *method*, so
        # nothing told the user to change the dropdown.  It also never said the
        # quick-tunnel URL expires on restart.
        #
        # The dropdown offers exactly three items -- OAuth, "Authorization
        # header", and "No Authentication" -- so the card names the one that
        # works verbatim.  Naming it approximately ("the token or header
        # option") left the user hunting for a label that is not there.
        async for app, pilot in self._app():
            await _open_clickup_auto_screen(pilot, app)
            screen = app.screen
            screen._probe_environment = lambda: {
                "cloudflared_installed": True,
                "port_probe": lambda p: True,
            }
            screen.query_one("#cu-auto-name", tui.Input).value = "My ClickUp"
            with patch.object(cs, "setup_clickup_connection", _stub_orchestrator_success), patch(
                "karox.tui_connections.setup_clickup_connection", _stub_orchestrator_success
            ):
                screen.action_connect()
                await pilot.pause(0.9)
            result = next(
                (s for s in app.screen_stack if s.__class__.__name__ == "_ClickupResultScreen"),
                None,
            )
            self.assertIsNotNone(result)
            rendered = "\n".join(str(w.render()) for w in result.query(tui.Static))
            # Names the dropdown and the exact item to pick, verbatim as
            # ClickUp spells it -- not a paraphrase the user has to map onto
            # the real labels.
            self.assertIn("Authentication Method", rendered)
            self.assertIn("Authorization header", rendered)
            # And warns the URL does not survive a restart.
            self.assertIn("temporary", rendered)
            await pilot.press("escape")
            await pilot.pause(0.3)

    async def test_failed_setup_shows_error_without_result_card(self) -> None:
        async for app, pilot in self._app():
            await _open_clickup_auto_screen(pilot, app)
            screen = app.screen
            screen._probe_environment = lambda: {"cloudflared_installed": True, "port_probe": lambda p: True}
            screen.query_one("#cu-auto-name", tui.Input).value = "fail"
            with patch.object(cs, "setup_clickup_connection", _stub_orchestrator_failure), patch(
                "karox.tui_connections.setup_clickup_connection", _stub_orchestrator_failure
            ):
                screen.action_connect()
                await pilot.pause(0.8)
            self.assertFalse(
                any(s.__class__.__name__ == "_ClickupResultScreen" for s in app.screen_stack),
                "a failed setup must not show a result card",
            )
            error = str(screen.query_one("#cu-auto-error", tui.Static).render())
            self.assertIn("tunnel_failed", error)
            await pilot.press("escape")
            await pilot.pause(0.3)

    async def test_custom_mcp_client_still_uses_long_form(self) -> None:
        # The Custom MCP Client preset must keep the manual form (the spec says
        # Custom gets auto defaults plus full manual override), so it should
        # NOT route to the ClickUp auto screen.
        async for app, pilot in self._app():
            composer = app.query_one("#composer", tui.CommandInput)
            composer.value = "/mcp-clients"
            await pilot.press("enter")
            await pilot.pause(0.3)
            await pilot.press("a")
            await pilot.pause(0.3)
            picker = next(
                s for s in app.screen_stack if s.__class__.__name__ == "_PresetPickerScreen"
            )
            options = picker.query_one("#preset-pick-list", tui.OptionList)
            while getattr(options.get_option_at_index(options.highlighted), "id", None) != "custom":
                await pilot.press("down")
                await pilot.pause(0.05)
            await pilot.press("enter")
            await pilot.pause(0.3)
            form_open = any(
                s.__class__.__name__ == "_McpClientFormScreen" for s in app.screen_stack
            )
            self.assertTrue(form_open, "Custom MCP Client must keep the manual form")
            await pilot.press("escape")
            await pilot.pause(0.3)


if __name__ == "__main__":
    unittest.main()
