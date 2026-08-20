from __future__ import annotations

import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from _support import SRC  # noqa: F401
from _tui_harness import isolated_karox_directories

from karox import tui
from karox.registry import ModelRecord, ProviderRecord
from karox.tui_dashboard import MODEL_PICKER_CONNECT, ModelPickerScreen


class ModelPickerUiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = self.enterContext(isolated_karox_directories())
        self.enterContext(patch.object(tui, "_selected_model", return_value=None))

    async def test_picker_mounts_with_current_model_and_effort(self) -> None:
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
                screen = ModelPickerScreen("en", effort="high")
                app.push_screen(screen, lambda value: result.append(value))
                await pilot.pause(0.05)
                current = str(screen.query_one("#model-picker-current", tui.Static).render())
                self.assertIn("empiriolabs/glm-5-2", current)
                self.assertIn("Effort high", current)
                # E jumps to the effort rows in the same list; picking one
                # dismisses with the effort choice rather than a nested modal.
                await pilot.press("e")
                await pilot.pause(0.05)
                options = screen.query_one("#model-picker-list", tui.OptionList)
                self.assertIn("effort", str(options.get_option_at_index(options.highlighted).id))
                await pilot.press("enter")
                await pilot.pause(0.05)
                self.assertEqual(result, ["effort:auto"])

    async def test_picker_holds_effort_and_models_in_one_list(self) -> None:
        registry = Mock()
        registry.selected_model.return_value = None
        registry.providers.return_value = []
        registry.models.return_value = []

        with patch("karox.tui_dashboard.ProviderRegistry", return_value=registry):
            app = tui.KaroXApp(Path(self.repository), language="en")
            async with app.run_test(size=(100, 34)) as pilot:
                screen = ModelPickerScreen("en", effort="low")
                app.push_screen(screen)
                await pilot.pause(0.05)
                options = screen.query_one("#model-picker-list", tui.OptionList)
                ids = [str(getattr(o, "id", "")) for o in options.options]
                # One list: effort rows and the connections fallback together.
                self.assertIn("effort:auto", ids)
                self.assertIn("effort:low", ids)
                self.assertIn("effort:high", ids)
                self.assertIn(MODEL_PICKER_CONNECT, ids)
                await pilot.press("escape")
                await pilot.pause(0.05)


if __name__ == "__main__":
    unittest.main()
