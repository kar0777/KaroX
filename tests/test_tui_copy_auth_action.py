"""UI action ``Скопировать ключ авторизации`` (Copy Authorization Key).

Phase 0.2 requires that copying the bridge authorization value from the
``/connect`` Detail never surfaces the raw secret on screen, in stdout, or in
a log, and that the clipboard receives the full ``Bearer <secret>`` string
and auto-clears after 120 seconds.  These tests pin that contract against the
mounted ``ConnectionDetailScreen``.
"""

from __future__ import annotations

import unittest
from unittest import mock

import karox.clipboard as clipboard_mod
from karox.tui_connections import (
    DETAIL_COPY_AUTH,
    DETAIL_DELETE,
    DETAIL_EDIT,
    DETAIL_VERIFY,
    detail_actions,
    detail_action_words,
)

from _support import SRC  # noqa: F401 - inserts src on sys.path
from _tui_harness import isolated_karox_directories

from karox import tui
from karox import tui_connections as hub
from karox.connections import McpClientTarget


class DetailCopyAuthVocabularyTests(unittest.TestCase):
    """The action and its words exist and are language-complete."""

    def test_copy_auth_appears_only_for_service_with_secret(self) -> None:
        actions_no_secret = detail_actions(True, kind="service", has_secret=False)
        self.assertNotIn(DETAIL_COPY_AUTH, actions_no_secret)
        actions_with_secret = detail_actions(True, kind="service", has_secret=True)
        self.assertIn(DETAIL_COPY_AUTH, actions_with_secret)

    def test_provider_never_offers_copy_auth(self) -> None:
        actions = detail_actions(True, kind="provider", has_secret=True)
        self.assertNotIn(DETAIL_COPY_AUTH, actions)

    def test_copy_auth_has_words_in_both_languages(self) -> None:
        for english in (True, False):
            with self.subTest(english=english):
                self.assertTrue(detail_action_words(DETAIL_COPY_AUTH, english).strip())

    def test_copy_auth_sits_before_delete_and_back(self) -> None:
        actions = detail_actions(True, kind="service", has_secret=True)
        copy_idx = actions.index(DETAIL_COPY_AUTH)
        delete_idx = actions.index(DETAIL_DELETE)
        self.assertLess(copy_idx, delete_idx)


# A fixed secret that the clipboard receives but the screen never renders.
# Deliberately long and unmistakable so a substring search is unambiguous.
_FIXTURE_SECRET = "test-bridge-secret-DO-NOT-RENDER-1234567890abcdef"
_CID = "copy-auth-cid"


def _mcp_target(
    connection_id: str = _CID,
    preset_id: str = "clickup",
    name: str = "My ClickUp",
    enabled: bool = True,
) -> McpClientTarget:
    return McpClientTarget(
        connection_id=connection_id,
        name=name,
        preset_id=preset_id,
        transport="streamable_http",
        endpoint_path="/mcp",
        auth_scheme="bearer",
        tunnel="tailscale",
        runtime_profile="generic-streamable-http",
        public_url="https://bridge.example.com",
        url_stability="temporary",
        credential_ref=f"os-keyring:connection/{connection_id}",
        credential_fingerprint="sha256:fixture",
        port=8765,
        enabled=enabled,
    )


class CopyAuthActionMountedTests(unittest.IsolatedAsyncioTestCase):
    """Drive the real mounted screen and assert the secret never renders."""

    def setUp(self) -> None:
        self.repository = self.enterContext(isolated_karox_directories())
        self.enterContext(mock.patch.object(tui, "_load_language", return_value="en"))
        self.enterContext(mock.patch.object(tui, "_selected_model", return_value=None))
        self.enterContext(
            mock.patch(
                "karox.connections.resolve_connection_secret",
                return_value=_FIXTURE_SECRET,
            )
        )

    async def test_copy_auth_puts_bearer_on_clipboard_without_rendering_secret(self) -> None:
        recorded: list[str] = []
        cleared: list[int] = []
        with mock.patch.object(
            clipboard_mod, "write_text", side_effect=lambda s: recorded.append(s) or True
        ), mock.patch.object(
            clipboard_mod, "schedule_clear", side_effect=lambda seconds=120: cleared.append(int(seconds))
        ):
            app = tui.KaroXApp(self.repository, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                # Seed a service record with a credential.
                from karox.connections import connection_registry

                connection_registry().put(_mcp_target())
                screens = app._connections_screens_cached()
                screen = screens["ConnectionDetailScreen"](
                    "en", kind="service", identity=_CID
                )
                app.push_screen(screen)
                await pilot.pause()

                self.assertIn(DETAIL_COPY_AUTH, screen.view()["actions"])

                # Drive the action: opens a confirmation, which we confirm.
                screen.action_copy_auth()
                await pilot.pause()
                # The confirmation screen is now on top; press Enter to confirm.
                await pilot.press("enter")
                await pilot.pause()

        # Clipboard received exactly the Bearer-wrapped value.
        self.assertEqual(recorded, [f"Bearer {_FIXTURE_SECRET}"])
        self.assertEqual(cleared, [clipboard_mod.CLIPBOARD_AUTO_CLEAR_SECONDS])
        # The secret must never appear in any rendered text on the detail screen.
        rendered = screen.rendered_text()
        self.assertNotIn(_FIXTURE_SECRET, rendered)
        self.assertNotIn(f"Bearer {_FIXTURE_SECRET}", rendered)

    async def test_declining_confirmation_does_not_touch_clipboard(self) -> None:
        recorded: list[str] = []
        with mock.patch.object(
            clipboard_mod, "write_text", side_effect=lambda s: recorded.append(s) or True
        ), mock.patch.object(clipboard_mod, "schedule_clear"):
            app = tui.KaroXApp(self.repository, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                from karox.connections import connection_registry

                connection_registry().put(_mcp_target())
                screens = app._connections_screens_cached()
                screen = screens["ConnectionDetailScreen"](
                    "en", kind="service", identity=_CID
                )
                app.push_screen(screen)
                await pilot.pause()
                screen.action_copy_auth()
                await pilot.pause()
                # Decline: Esc dismisses the confirmation.
                await pilot.press("escape")
                await pilot.pause()
        self.assertEqual(recorded, [])

    async def test_clipboard_unavailable_does_not_render_secret(self) -> None:
        with mock.patch.object(clipboard_mod, "write_text", return_value=False), mock.patch.object(
            clipboard_mod, "schedule_clear"
        ):
            app = tui.KaroXApp(self.repository, language="en")
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                from karox.connections import connection_registry

                connection_registry().put(_mcp_target())
                screens = app._connections_screens_cached()
                screen = screens["ConnectionDetailScreen"](
                    "en", kind="service", identity=_CID
                )
                app.push_screen(screen)
                await pilot.pause()
                screen.action_copy_auth()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
        rendered = screen.rendered_text()
        self.assertNotIn(_FIXTURE_SECRET, rendered)


class ServiceConnectAuthButtonTests(unittest.IsolatedAsyncioTestCase):
    """The approval-password copy action is a visible button, not a hidden key.

    Regression: ``#svc-auth`` had ``display: none`` for every OAuth profile,
    so ChatGPT Web users could only reach the approval password through the
    undocumented ``P`` key.
    """

    def _seed_screen(self, screen: object, endpoint: str) -> None:
        screen._has_snapshot = True  # type: ignore[attr-defined]
        screen._last_state = None  # type: ignore[attr-defined]
        screen._endpoint = endpoint  # type: ignore[attr-defined]
        screen._write()  # type: ignore[attr-defined]

    async def test_oauth_profile_shows_copy_password_button(self) -> None:
        from textual.widgets import Button

        with isolated_karox_directories() as repository:
            app = tui.KaroXApp(repository, language="ru")
            async with app.run_test(size=(120, 42)) as pilot:
                screens = app._connections_screens_cached()
                screen = screens["ServiceConnectScreen"]("ru", preset_id="chatgpt-web")
                app.push_screen(screen)
                await pilot.pause()
                self._seed_screen(screen, "https://bridge.example.com/mcp")
                await pilot.pause()
                auth = screen.query_one("#svc-auth", Button)
                self.assertNotEqual(str(auth.styles.display), "none")
                self.assertFalse(auth.disabled)
                self.assertIn("Скопировать пароль", str(auth.label))

    async def test_bearer_profile_keeps_copy_key_label(self) -> None:
        from textual.widgets import Button

        with isolated_karox_directories() as repository:
            app = tui.KaroXApp(repository, language="ru")
            async with app.run_test(size=(120, 42)) as pilot:
                screens = app._connections_screens_cached()
                screen = screens["ServiceConnectScreen"]("ru", preset_id="clickup")
                app.push_screen(screen)
                await pilot.pause()
                self._seed_screen(screen, "https://bridge.example.com/mcp")
                await pilot.pause()
                auth = screen.query_one("#svc-auth", Button)
                self.assertNotEqual(str(auth.styles.display), "none")
                self.assertIn("Скопировать ключ", str(auth.label))


if __name__ == "__main__":
    unittest.main()
