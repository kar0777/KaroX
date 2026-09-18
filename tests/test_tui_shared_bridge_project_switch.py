"""Regression: the shared bridge stays configured for every approved project.

Owner-found live defect: a saved multi-project bridge anchored at project A
fell back to "not configured / Launch KaroX" as soon as project B was selected
in KaroX, inviting the user to create a duplicate physical bridge. Project
selection chooses where NEW tasks run; it must never change bridge identity.

The B7 guarantee stays intact: a project that is NOT in the bridge's approved
registry still does not match, so two single-project profiles for two
repositories never collapse into one ambiguous service row.
"""

from __future__ import annotations

from _unittest_compat import enter_context

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import _path_setup  # noqa: F401  - test isolation must load first

from _tui_harness import isolated_karox_directories
from karox import tui_connections
from karox.models import AccessProfile
from karox.web_bridge_profiles import SavedWebBridgeProfile, WebBridgeProfileStore


class _HostAppStub:
    """The two callbacks build_connections_screens needs, and nothing else."""

    def _copy_text(self, text: str) -> None:  # pragma: no cover - not exercised
        pass

    def _refresh_status(self) -> None:  # pragma: no cover - not exercised
        pass


class SharedBridgeProjectSwitchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.repository = enter_context(self, isolated_karox_directories())
        base = Path(enter_context(self, TemporaryDirectory()))
        # Unicode + space in the second project path on purpose: the live
        # defect was found with D:\проекты\faceboooook selected.
        self.project_a = base / "project-a"
        self.project_b = base / "проект б"
        self.project_c = base / "unrelated-project"
        for path in (self.project_a, self.project_b, self.project_c):
            path.mkdir()
        WebBridgeProfileStore().put(
            SavedWebBridgeProfile(
                name="shared-bridge",
                target_profile="chatgpt-web",
                repository=str(self.project_a),
                projects=(
                    {
                        "project_id": "proj-a",
                        "path": str(self.project_a),
                        "label": "A",
                    },
                    {
                        "project_id": "proj-b",
                        "path": str(self.project_b),
                        "label": "Б",
                    },
                ),
                default_project_id="proj-a",
                tools=("karox.repo.read_file", "karox.git.status"),
                access_profile=AccessProfile.WORKSPACE_WRITE,
            ),
            replace_existing=False,
        )
        screens = tui_connections.build_connections_screens(_HostAppStub())
        self._service_cls = screens["ServiceConnectScreen"]

    def _screen_with_selected_project(self, selected: Path):
        screen = self._service_cls("en", preset_id="chatgpt-web")
        resolved = selected.resolve()
        screen._current_repository = lambda: resolved  # type: ignore[method-assign]
        return screen

    def test_anchor_project_finds_the_saved_bridge(self) -> None:
        state = self._screen_with_selected_project(self.project_a)._state()
        self.assertIsNotNone(state)
        self.assertEqual(state.target.name, "shared-bridge")

    def test_switching_to_another_approved_project_keeps_the_bridge(self) -> None:
        """The owner-found regression: approved project B must stay connected."""
        state = self._screen_with_selected_project(self.project_b)._state()
        self.assertIsNotNone(
            state,
            "selecting an approved registry project must not degrade the "
            "shared bridge to 'not configured'",
        )
        self.assertEqual(state.target.name, "shared-bridge")

    def test_project_outside_the_registry_still_does_not_match(self) -> None:
        """B7 stays true: no collapse onto a bridge that never approved C."""
        self.assertIsNone(
            self._screen_with_selected_project(self.project_c)._state()
        )

    def test_covers_current_helper_reads_the_registry(self) -> None:
        screen = self._screen_with_selected_project(self.project_b)
        saved = WebBridgeProfileStore().get("shared-bridge")
        self.assertTrue(screen._saved_profile_covers_current(saved))
        screen_c = self._screen_with_selected_project(self.project_c)
        self.assertFalse(screen_c._saved_profile_covers_current(saved))


class ServiceScreenSmallTerminalScrollTests(unittest.IsolatedAsyncioTestCase):
    """Owner-found: the small connected ChatGPT screen hid lower controls.

    The dialog was a plain Vertical with max-height: content past the window
    edge was clipped with no keyboard or wheel scroll. The container is now a
    VerticalScroll, so every action stays reachable at every supported size.
    """

    def setUp(self) -> None:
        from unittest.mock import patch

        from karox import tui

        self.repository = enter_context(self, isolated_karox_directories())
        enter_context(self, patch.object(tui, "_load_language", return_value="en"))
        enter_context(self, patch.object(tui, "_selected_model", return_value=None))

    async def test_small_terminal_keeps_every_control_reachable(self) -> None:
        from textual.containers import VerticalScroll
        from textual.widgets import Static

        from karox import tui

        app = tui.KaroXApp(self.repository, language="en")
        async with app.run_test(size=(46, 14)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            service = screens["ServiceConnectScreen"]("en", preset_id="chatgpt-web")
            app.push_screen(service)
            await pilot.pause()
            dialog = service.query_one("#svc-dialog")
            self.assertIsInstance(dialog, VerticalScroll)
            # Deterministic overflow: more steps than a 14-row terminal shows.
            service.query_one("#svc-steps", Static).update(
                "\n".join(f"step {i}" for i in range(30))
            )
            await pilot.pause()
            self.assertTrue(dialog.allow_vertical_scroll)
            self.assertGreater(dialog.max_scroll_y, 0)
            dialog.scroll_end(animate=False)
            await pilot.pause()
            self.assertGreater(dialog.scroll_offset.y, 0)


if __name__ == "__main__":
    unittest.main()
