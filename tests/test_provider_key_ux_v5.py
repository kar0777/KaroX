from __future__ import annotations

import unittest
from unittest.mock import patch

from _support import SRC  # noqa: F401
from _tui_harness import isolated_karox_directories

from karox import tui
from karox.credentials import CredentialStore
from karox.provider_controller import ProviderController
from karox.registry import ProviderRegistry


class MemoryBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.values[(service, account)] = secret

    def get(self, service: str, account: str) -> str | None:
        return self.values.get((service, account))

    def delete(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


def visible(widget) -> bool:
    return str(widget.styles.display) != "none"


class ProviderKeyUxTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = self.enterContext(isolated_karox_directories())
        self.registry = ProviderRegistry(
            self.repository.parent / "config" / "vnext" / "providers.json"
        )
        self.controller = ProviderController(
            registry=self.registry,
            credentials=CredentialStore(MemoryBackend()),
        )
        self.preset = next(
            item for item in tui.provider_presets() if item.preset_id == "openrouter"
        )
        self.discovery = tui.ModelDiscovery(
            models=(
                tui.DiscoveredModel(
                    "stealth/ox-alpha",
                    1_048_576,
                    16_384,
                    tools="true",
                    vision="true",
                    structured_output="true",
                    streaming="true",
                    input_per_million=0.0,
                    output_per_million=0.0,
                    free=True,
                ),
            ),
            base_url="https://openrouter.ai/api/v1",
            attempted_urls=("https://openrouter.ai/api/v1",),
        )

    async def _open(self):
        app = tui.KaroXApp(self.repository, language="en")
        context = (
            patch.object(tui, "_load_language", return_value="en"),
            patch.object(tui, "_selected_model", return_value=None),
            patch.object(tui, "_provider_controller", return_value=self.controller),
            patch.object(tui, "_discover_models_result", return_value=self.discovery),
            patch.object(
                tui,
                "_probe_provider",
                return_value={"finish_reason": "stop", "usage": {}, "transport_attempts": 1},
            ),
        )
        return app, context

    async def test_paste_enter_connects_before_model_selection(self) -> None:
        app, contexts = await self._open()
        with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4] as probe:
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                screen = tui.ProviderSetupScreen("en", self.preset)
                app.push_screen(screen)
                await pilot.pause()
                key = screen.query_one("#provider-key", tui.Input)
                key.value = "benchmark-key-not-real"
                key.focus()
                await pilot.press("enter")
                for _ in range(20):
                    await pilot.pause(0.05)
                    if isinstance(app.screen, tui.ModelPickerScreen):
                        break
                self.assertIsInstance(app.screen, tui.ModelPickerScreen)
                details = self.controller.details("openrouter")
                self.assertTrue(details.credential["available"])
                self.assertEqual([m.model_id for m in details.models], ["stealth/ox-alpha"])
                self.assertIsNone(self.registry.selected_model())
                self.assertEqual(key.value, "")
                self.assertFalse(visible(screen.query_one("#provider-save", tui.Button)))
                # Standard preset Connect validates the key by catalog discovery;
                # it must not spend model input/output tokens on a hidden probe.
                probe.assert_not_called()

    async def test_escape_after_catalog_does_not_lose_connection(self) -> None:
        app, contexts = await self._open()
        with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4]:
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                screen = tui.ProviderSetupScreen("en", self.preset)
                app.push_screen(screen)
                await pilot.pause()
                key = screen.query_one("#provider-key", tui.Input)
                key.value = "benchmark-key-not-real"
                key.focus()
                await pilot.press("enter")
                for _ in range(20):
                    await pilot.pause(0.05)
                    if isinstance(app.screen, tui.ModelPickerScreen):
                        break
                await pilot.press("escape")
                await pilot.pause()
                await pilot.press("escape")
                await pilot.pause()
                details = self.controller.details("openrouter")
                self.assertTrue(details.credential["available"])
                self.assertEqual(details.models[0].model_id, "stealth/ox-alpha")

    async def test_one_enter_in_model_picker_activates_saved_model(self) -> None:
        app, contexts = await self._open()
        with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4]:
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                screen = tui.ProviderSetupScreen("en", self.preset)
                app.push_screen(screen)
                await pilot.pause()
                key = screen.query_one("#provider-key", tui.Input)
                key.value = "benchmark-key-not-real"
                key.focus()
                await pilot.press("enter")
                for _ in range(20):
                    await pilot.pause(0.05)
                    if isinstance(app.screen, tui.ModelPickerScreen):
                        break
                await pilot.press("enter")
                for _ in range(20):
                    await pilot.pause(0.05)
                    if self.registry.selected_model() is not None:
                        break
                selected = self.registry.selected_model()
                self.assertIsNotNone(selected)
                assert selected is not None
                self.assertEqual(selected.model_id, "stealth/ox-alpha")

    async def test_standard_preset_has_one_primary_connect_action(self) -> None:
        app, contexts = await self._open()
        with contexts[0], contexts[1], contexts[2], contexts[3], contexts[4]:
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                screen = tui.ProviderSetupScreen("en", self.preset)
                app.push_screen(screen)
                await pilot.pause()
                self.assertIn("Connect", str(screen.query_one("#provider-discover", tui.Button).label))
                self.assertFalse(visible(screen.query_one("#provider-save", tui.Button)))


if __name__ == "__main__":
    unittest.main()
