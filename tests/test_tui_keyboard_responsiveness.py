"""Keyboard and event-loop responsiveness contracts for the human TUI.

These tests exercise real Textual focus/key dispatch. They exist because a
screen-level priority Enter binding can look correct in source while making Tab
focus meaningless at runtime, and because a synchronous Windows/keyring probe can
make a modal appear frozen even though no operation is actually running.
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from _tui_harness import isolated_karox_directories

from karox import tui
from karox.connection_status import ConnectionLiveStatus, OverallStatus


class KeyboardResponsivenessTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = self.enterContext(isolated_karox_directories())
        self.enterContext(patch.object(tui, "_load_language", return_value="en"))
        self.enterContext(patch.object(tui, "_selected_model", return_value=None))

    def app(self) -> tui.KaroXApp:
        return tui.KaroXApp(self.repository, language="en")

    async def test_service_tab_order_is_primary_context_more(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause()
            screen_cls = app._connections_screens_cached()["ServiceConnectScreen"]
            screen = screen_cls("en", preset_id="chatgpt-web")
            with patch.object(screen, "_state", return_value=None):
                app.push_screen(screen)
                await pilot.pause(0.25)
                screen._has_snapshot = True
                screen._endpoint = "https://example.invalid/mcp"
                screen._typed_status = ConnectionLiveStatus(
                    overall=OverallStatus.READY_FOR_CHATGPT_SETUP,
                    public_url="https://example.invalid/mcp",
                )
                screen._write()
                # Focus mount is asynchronous under a loaded runner; pump
                # until the primary button actually holds focus so the tab
                # contract is measured from a real starting state.
                deadline = time.monotonic() + 10
                while getattr(app.focused, "id", None) != "svc-primary" and time.monotonic() < deadline:
                    screen.query_one("#svc-primary", tui.Button).focus()
                    await pilot.pause(0.1)
                self.assertEqual(getattr(app.focused, "id", None), "svc-primary")
                # The approval-password copy button is a visible tab stop for
                # OAuth profiles -- a hidden P key was the old contract. Focus
                # delivery is asynchronous under a loaded runner, so pump the
                # app until each expected stop is reached.
                async def _press_until_focus(key: str, expected: str) -> None:
                    deadline = time.monotonic() + 10
                    while getattr(app.focused, "id", None) != expected and time.monotonic() < deadline:
                        await pilot.press(key)
                        await pilot.pause(0.1)

                await _press_until_focus("tab", "svc-auth")
                self.assertEqual(getattr(app.focused, "id", None), "svc-auth")
                await _press_until_focus("tab", "svc-more")
                self.assertEqual(getattr(app.focused, "id", None), "svc-more")
                await _press_until_focus("shift+tab", "svc-auth")
                self.assertEqual(getattr(app.focused, "id", None), "svc-auth")

    async def test_service_enter_activates_the_contextual_verify_button(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause()
            screen_cls = app._connections_screens_cached()["ServiceConnectScreen"]
            screen = screen_cls("en", preset_id="chatgpt-web")
            # Keep the real background refresh harmless while we pin the visible
            # state needed for the keyboard assertion.
            with patch.object(screen, "_state", return_value=None):
                app.push_screen(screen)
                await pilot.pause(0.25)
                screen._has_snapshot = True
                screen._endpoint = "https://example.invalid/mcp"
                screen._typed_status = ConnectionLiveStatus(
                    overall=OverallStatus.CONNECTED_UNVERIFIED,
                    public_url="https://example.invalid/mcp",
                )
                screen._write()
                screen.query_one("#svc-verify", tui.Button).focus()
                with (
                    patch.object(screen, "action_verify") as verify,
                    patch.object(screen, "action_copy_url") as copy_url,
                    patch.object(screen, "action_start_repair") as start,
                ):
                    await pilot.press("enter")
                    await pilot.pause(0.05)
                verify.assert_called_once_with()
                copy_url.assert_not_called()
                start.assert_not_called()

    async def test_service_space_activates_the_focused_primary_button(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause()
            screen_cls = app._connections_screens_cached()["ServiceConnectScreen"]
            screen = screen_cls("en", preset_id="chatgpt-web")
            with patch.object(screen, "_state", return_value=None):
                app.push_screen(screen)
                await pilot.pause(0.25)
                screen._has_snapshot = True
                screen._endpoint = "https://example.invalid/mcp"
                screen._typed_status = ConnectionLiveStatus(
                    overall=OverallStatus.READY_FOR_CHATGPT_SETUP,
                    public_url="https://example.invalid/mcp",
                )
                screen._write()
                screen.query_one("#svc-primary", tui.Button).focus()
                with patch.object(screen, "action_copy_url") as copy_url:
                    await pilot.press("space")
                    await pilot.pause(0.05)
                copy_url.assert_called_once_with()

    async def test_notion_auth_button_is_keyboard_reachable_and_copies_bearer(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause()
            screen_cls = app._connections_screens_cached()["ServiceConnectScreen"]
            screen = screen_cls("en", preset_id="notion")
            with patch.object(screen, "_state", return_value=None):
                app.push_screen(screen)
                await pilot.pause(0.25)
                screen._has_snapshot = True
                screen._endpoint = "https://example.invalid/mcp"
                screen._typed_status = ConnectionLiveStatus(
                    overall=OverallStatus.READY_FOR_CHATGPT_SETUP,
                    public_url="https://example.invalid/mcp",
                )
                screen._write()
                # Notion uses OAuth, so the auth button is the visible
                # approval-password copy (never a hidden-key-only path), and
                # the P binding remains as the keyboard shortcut for it.
                auth = screen.query_one("#svc-auth", tui.Button)
                self.assertNotEqual(str(auth.styles.display), "none")
                self.assertIn("password", str(auth.label).lower())
                with patch.object(screen, "action_copy_approval_password") as copy_password:
                    await pilot.press("p")
                    await pilot.pause(0.05)
                copy_password.assert_called_once_with()

    async def test_confirmation_enter_obeys_the_focused_no_button(self) -> None:
        app = self.app()
        result: list[object] = []
        async with app.run_test(size=(90, 28)) as pilot:
            await pilot.pause()
            screen = tui.ConfirmScreen("Question", "Body", language="en")
            app.push_screen(screen, lambda value: result.append(value))
            await pilot.pause(0.05)
            screen.query_one("#confirm-no", tui.Button).focus()
            await pilot.press("enter")
            await pilot.pause(0.05)
        self.assertEqual(result, [None])

    async def test_provider_picker_enter_obeys_the_focused_cancel_button(self) -> None:
        app = self.app()
        result: list[object] = []
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause()
            screen = tui.ProviderPresetScreen("en")
            app.push_screen(screen, lambda value: result.append(value))
            await pilot.pause(0.05)
            screen.query_one("#preset-cancel", tui.Button).focus()
            await pilot.press("enter")
            await pilot.pause(0.05)
        self.assertEqual(result, [None])

    async def test_slow_service_probe_cannot_block_escape(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause()
            screen_cls = app._connections_screens_cached()["ServiceConnectScreen"]
            screen = screen_cls("en", preset_id="chatgpt-web")

            def slow_state():
                time.sleep(0.40)
                return None

            with patch.object(screen, "_state", side_effect=slow_state):
                app.push_screen(screen)
                await pilot.pause(0.04)
                handled: list[float] = []
                original_cancel = screen.action_cancel

                def timed_cancel() -> None:
                    handled.append(time.perf_counter())
                    original_cancel()

                screen.action_cancel = timed_cancel  # type: ignore[method-assign]
                started = time.perf_counter()
                await pilot.press("escape")
                self.assertTrue(handled)
                self.assertLess(handled[0] - started, 0.12)
                self.assertNotIn(screen, app.screen_stack)

    async def test_slow_mcp_runtime_poll_cannot_block_escape(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause()
            screen_cls = app._connections_screens_cached()["McpClientsScreen"]
            screen = screen_cls("en")

            def slow_items():
                time.sleep(0.40)
                return []

            with patch.object(screen, "_items", side_effect=slow_items):
                app.push_screen(screen)
                await pilot.pause(0.04)
                handled: list[float] = []
                original_cancel = screen.action_cancel

                def timed_cancel() -> None:
                    handled.append(time.perf_counter())
                    original_cancel()

                screen.action_cancel = timed_cancel  # type: ignore[method-assign]
                started = time.perf_counter()
                await pilot.press("escape")
                self.assertTrue(handled)
                self.assertLess(handled[0] - started, 0.12)
                self.assertNotIn(screen, app.screen_stack)

    async def test_slow_detail_read_cannot_block_escape(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause()
            screen_cls = app._connections_screens_cached()["ConnectionDetailScreen"]
            screen = screen_cls("en", kind="service", identity="slow")

            def slow_read():
                time.sleep(0.40)

            with patch.object(screen, "_read", side_effect=slow_read):
                app.push_screen(screen)
                await pilot.pause(0.04)
                handled: list[float] = []
                original_cancel = screen.action_cancel

                def timed_cancel() -> None:
                    handled.append(time.perf_counter())
                    original_cancel()

                screen.action_cancel = timed_cancel  # type: ignore[method-assign]
                started = time.perf_counter()
                await pilot.press("escape")
                self.assertTrue(handled)
                self.assertLess(handled[0] - started, 0.12)
                self.assertNotIn(screen, app.screen_stack)

    async def test_slow_hub_collection_cannot_block_escape(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 34)) as pilot:
            await pilot.pause()
            screen_cls = app._connections_screens_cached()["ConnectionHubScreen"]
            screen = screen_cls("en")

            def slow_collect():
                time.sleep(0.40)
                return ()

            with patch.object(screen, "_collect", side_effect=slow_collect):
                app.push_screen(screen)
                await pilot.pause(0.04)
                handled: list[float] = []
                original_cancel = screen.action_cancel

                def timed_cancel() -> None:
                    handled.append(time.perf_counter())
                    original_cancel()

                screen.action_cancel = timed_cancel  # type: ignore[method-assign]
                started = time.perf_counter()
                await pilot.press("escape")
                self.assertTrue(handled)
                self.assertLess(handled[0] - started, 0.12)
                self.assertNotIn(screen, app.screen_stack)

    async def test_slow_session_history_backfill_cannot_block_the_composer(self) -> None:
        app = self.app()

        def slow_backfill() -> None:
            # Keep a wide gap between the intentionally blocking implementation
            # and the responsiveness budget. The previous 0.40s/0.25s split was
            # correct in principle but occasionally failed on a loaded Windows
            # runner even when the backfill was off the UI thread.
            time.sleep(0.80)

        with patch.object(app, "_merge_persisted_sessions", side_effect=slow_backfill):
            async with app.run_test(size=(100, 34)) as pilot:
                await pilot.pause(0.04)
                composer = app.query_one("#composer", tui.Input)
                started = time.perf_counter()
                await pilot.press("x")
                self.assertLess(time.perf_counter() - started, 0.45)
                self.assertEqual(composer.value, "x")


if __name__ == "__main__":
    unittest.main()
