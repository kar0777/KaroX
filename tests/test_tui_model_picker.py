from __future__ import annotations

from _unittest_compat import enter_context

import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from _support import SRC  # noqa: F401
from _tui_harness import isolated_karox_directories

from karox import tui
from karox.registry import ModelRecord, ProviderRecord
from karox.tui_dashboard import (
    AUTO_EFFORT,
    MODEL_PICKER_CONNECT,
    EffortPickerScreen,
    ModelPickerScreen,
)


class ModelPickerUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = enter_context(self, isolated_karox_directories())
        enter_context(self, patch.object(tui, "_selected_model", return_value=None))

    async def test_model_picker_mounts_with_current_model_and_no_effort_rows(self) -> None:
        registry = Mock()
        registry.selected_model.return_value = ModelRecord(
            "empiriolabs", "glm-5-2", tools="true"
        )
        registry.providers.return_value = [
            ProviderRecord(
                provider_id="empiriolabs",
                base_url="https://example.invalid/v1",
                adapter_kind="openai_compatible_chat",
            )
        ]
        registry.models.return_value = [
            ModelRecord("empiriolabs", "glm-5-2", tools="true")
        ]

        with patch("karox.tui_dashboard.ProviderRegistry", return_value=registry):
            app = tui.KaroXApp(Path(self.repository), language="en")
            async with app.run_test(size=(100, 34)) as pilot:
                result: list[object] = []
                screen = ModelPickerScreen("en")
                app.push_screen(screen, lambda value: result.append(value))
                await pilot.pause(0.05)
                current = str(
                    screen.query_one("#model-picker-current", tui.Static).render()
                )
                self.assertIn("empiriolabs/glm-5-2", current)
                self.assertNotIn("Effort", current)
                options = screen.query_one("#model-picker-list", tui.OptionList)
                ids = [str(getattr(option, "id", "")) for option in options.options]
                self.assertTrue(any(value.startswith("model:") for value in ids))
                self.assertFalse(any(value.startswith("effort:") for value in ids))
                await pilot.press("enter")
                await pilot.pause(0.05)
                self.assertEqual(result, ["model:empiriolabs:glm-5-2"])

    async def test_model_picker_uses_connections_fallback_when_no_models_exist(self) -> None:
        registry = Mock()
        registry.selected_model.return_value = None
        registry.providers.return_value = []
        registry.models.return_value = []

        with patch("karox.tui_dashboard.ProviderRegistry", return_value=registry):
            app = tui.KaroXApp(Path(self.repository), language="en")
            async with app.run_test(size=(100, 34)) as pilot:
                screen = ModelPickerScreen("en")
                app.push_screen(screen)
                await pilot.pause(0.05)
                options = screen.query_one("#model-picker-list", tui.OptionList)
                ids = [str(getattr(option, "id", "")) for option in options.options]
                self.assertIn(MODEL_PICKER_CONNECT, ids)

    async def test_effort_picker_contains_only_first_class_effort_levels(self) -> None:
        app = tui.KaroXApp(Path(self.repository), language="en")
        async with app.run_test(size=(100, 34)) as pilot:
            result: list[object] = []
            screen = EffortPickerScreen("en", effort="high")
            app.push_screen(screen, lambda value: result.append(value))
            await pilot.pause(0.05)
            options = screen.query_one("#effort-picker-list", tui.OptionList)
            ids = [str(getattr(option, "id", "")) for option in options.options]
            self.assertEqual(ids, [AUTO_EFFORT, "low", "medium", "high", "extra-high", "ultra"])
            self.assertFalse(any(value.startswith("model:") for value in ids))
            self.assertEqual(options.highlighted, 3)
            await pilot.press("enter")
            await pilot.pause(0.05)
            self.assertEqual(result, ["high"])


if __name__ == "__main__":
    unittest.main()
