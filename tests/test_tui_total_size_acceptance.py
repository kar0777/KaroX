"""Total size acceptance: high-value screens stay usable at every supported size.

Sizes 40x12 through 160x45 plus one awkward 73x19. Every screen must mount
cleanly, keep its dialog inside the viewport, be scrollable whenever content
overflows (a plain clipped container hides controls with no way to reach
them), and leave the screen stack cleanly on Esc. Russian is exercised at
every size — RU labels are wider and clip first; English is sampled.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import _path_setup  # noqa: F401  - test isolation must load first

from _tui_harness import isolated_karox_directories

SIZES = (
    (40, 12),
    (46, 14),
    (60, 18),
    (73, 19),
    (80, 24),
    (100, 28),
    (120, 30),
    (160, 45),
)
ENGLISH_SAMPLED_AT = {(46, 14), (120, 30)}


class TotalSizeAcceptanceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        from karox import tui

        self.repository = self.enterContext(isolated_karox_directories())
        self.enterContext(patch.object(tui, "_load_language", return_value="ru"))
        self.enterContext(patch.object(tui, "_selected_model", return_value=None))

    def _screens_for(self, app, language: str):
        from karox.project_registry import ProjectRegistry
        from karox.tui_workspace import WorkspaceManagerScreen

        screens = app._connections_screens_cached()
        return (
            screens["ConnectionHubScreen"](language),
            screens["ServiceConnectScreen"](language, preset_id="chatgpt-web"),
            screens["ModelProvidersScreen"](language),
            WorkspaceManagerScreen(
                ProjectRegistry.single(self.repository),
                self.repository,
                language=language,
            ),
        )

    def _assert_dialog_usable(self, screen, width: int, height: int) -> None:
        for child in screen.children:
            region = child.region
            self.assertLessEqual(
                region.height,
                height,
                f"{type(screen).__name__}: dialog taller than the terminal",
            )
            self.assertLessEqual(
                region.width,
                width,
                f"{type(screen).__name__}: dialog wider than the terminal",
            )
            overflows = child.virtual_size.height > child.container_size.height
            if overflows:
                self.assertTrue(
                    child.allow_vertical_scroll,
                    f"{type(screen).__name__}: content overflows at "
                    f"{width}x{height} but the dialog cannot scroll — lower "
                    "controls would be unreachable",
                )

    async def test_high_value_screens_survive_every_supported_size(self) -> None:
        from karox import tui

        for width, height in SIZES:
            languages = ["ru"]
            if (width, height) in ENGLISH_SAMPLED_AT:
                languages.append("en")
            with self.subTest(size=f"{width}x{height}"):
                app = tui.KaroXApp(self.repository, language="ru")
                async with app.run_test(size=(width, height)) as pilot:
                    await pilot.pause()
                    # Root/home renders at this size.
                    self.assertIsNotNone(app.screen)
                    for language in languages:
                        for screen in self._screens_for(app, language):
                            with self.subTest(
                                screen=type(screen).__name__, language=language
                            ):
                                app.push_screen(screen)
                                await pilot.pause()
                                self._assert_dialog_usable(screen, width, height)
                                await pilot.press("escape")
                                await pilot.pause()
                                self.assertNotIn(
                                    screen,
                                    app.screen_stack,
                                    f"{type(screen).__name__} did not close on Esc",
                                )


if __name__ == "__main__":
    unittest.main()
