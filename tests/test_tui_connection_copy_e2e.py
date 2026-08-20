from __future__ import annotations

import unittest
from unittest import mock

from _support import SRC  # noqa: F401
from _tui_harness import isolated_karox_directories
from karox import clipboard as clipboard_mod
from karox import tui
from karox.connections import McpClientTarget, connection_registry

SECRET = "e2e-bridge-secret-not-for-display"


class TuiConnectionCopyE2ETests(unittest.IsolatedAsyncioTestCase):
    async def test_boot_detail_copy_bearer_and_auto_clear(self) -> None:
        with isolated_karox_directories() as repository:
            with mock.patch.object(tui, "_load_language", return_value="en"), mock.patch.object(tui, "_selected_model", return_value=None), mock.patch("karox.connections.resolve_connection_secret", return_value=SECRET):
                copied: list[str] = []
                cleared: list[int] = []
                with mock.patch.object(clipboard_mod, "write_text", side_effect=lambda value: copied.append(value) or True), mock.patch.object(clipboard_mod, "schedule_clear", side_effect=lambda seconds=clipboard_mod.CLIPBOARD_AUTO_CLEAR_SECONDS: cleared.append(int(seconds))):
                    app = tui.KaroXApp(repository, language="en")
                    async with app.run_test(size=(120, 42)) as pilot:
                        await pilot.pause()
                        target = McpClientTarget(
                            connection_id="e2e-service",
                            name="E2E ChatGPT",
                            preset_id="chatgpt-web",
                            transport="streamable_http",
                            endpoint_path="/mcp",
                            auth_scheme="bearer",
                            tunnel="tailscale",
                            runtime_profile="chatgpt-web",
                            public_url="https://bridge.example.com",
                            url_stability="stable",
                            credential_ref="os-keyring:connection/e2e-service",
                            credential_fingerprint="sha256:e2e",
                            port=8765,
                            enabled=True,
                        )
                        connection_registry().put(target)
                        screen_type = app._connections_screens_cached()["ConnectionDetailScreen"]
                        screen = screen_type("en", kind="service", identity="e2e-service")
                        app.push_screen(screen)
                        await pilot.pause()
                        self.assertNotIn(SECRET, screen.rendered_text())
                        screen.action_copy_auth()
                        await pilot.pause()
                        self.assertNotIn(SECRET, screen.rendered_text())
                        await pilot.press("enter")
                        await pilot.pause()
                self.assertEqual(copied, [f"Bearer {SECRET}"])
                self.assertEqual(cleared, [clipboard_mod.CLIPBOARD_AUTO_CLEAR_SECONDS])
                self.assertNotIn(SECRET, screen.rendered_text())


if __name__ == "__main__":
    unittest.main()
