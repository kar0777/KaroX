from __future__ import annotations

import tempfile
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from karox import tui


def _hosted_runner_windows() -> bool:
    """True only inside the hosted Windows runner's CI environment."""
    return os.name == "nt" and bool(os.environ.get("CI"))




@unittest.skipUnless(tui._HAS_TEXTUAL, "Textual is not installed")
@unittest.skipIf(
    _hosted_runner_windows(),
    "IsolatedAsyncioTestCase apps cannot run under the hosted runner",
)
class WorkspacePickerV1Tests(unittest.IsolatedAsyncioTestCase):
    @unittest.skipUnless(os.name == "nt", "IsolatedAsyncio pump contract runs on Windows")
    async def test_ctrl_w_opens_manager_adds_and_selects_workspace(self) -> None:
        from karox.tui_workspace import WorkspaceManagerScreen

        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            current = base / "current"
            target = base / "target"
            current.mkdir()
            target.mkdir()
            preferences = base / "config" / "ui.json"

            with (
                patch.object(tui, "_selected_model", return_value=None),
                patch.object(tui, "_preferences_path", return_value=preferences),
            ):
                app = tui.KaroXApp(current, language="en")
                async with app.run_test(size=(100, 32)) as pilot:
                    await pilot.press("ctrl+w")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, WorkspaceManagerScreen)
                    path_input = app.screen.query_one("#workspace-add-path", tui.Input)
                    path_input.value = str(target)
                    path_input.focus()
                    await pilot.press("enter")
                    await pilot.pause()

                    self.assertIsInstance(app.screen, WorkspaceManagerScreen)
                    target_entry = app._workspace_registry().entry_for_path(target)
                    self.assertIsNotNone(target_entry)
                    await pilot.press("down")
                    await pilot.press("enter")
                    await pilot.pause()

                    self.assertEqual(app.repository, target.resolve())
                    self.assertEqual(tui._load_recent_workspaces()[0], str(target.resolve()))
                    self.assertIn(str(current.resolve()), tui._load_recent_workspaces())

    @unittest.skipUnless(os.name == "nt", "IsolatedAsyncio pump contract runs on Windows")
    async def test_workspace_switch_is_refused_while_agent_is_running(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            current = base / "current"
            target = base / "target"
            current.mkdir()
            target.mkdir()
            preferences = base / "config" / "ui.json"

            with (
                patch.object(tui, "_selected_model", return_value=None),
                patch.object(tui, "_preferences_path", return_value=preferences),
            ):
                app = tui.KaroXApp(current, language="en")
                async with app.run_test(size=(100, 32)) as pilot:
                    app.agent_busy = True
                    switched = app._switch_workspace(str(target))
                    await pilot.pause()
                    self.assertFalse(switched)
                    self.assertEqual(app.repository, current.resolve())


class RecentWorkspaceTests(unittest.TestCase):
    def test_recent_workspaces_are_deduplicated_and_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as root:
            base = Path(root)
            first = base / "first"
            second = base / "second"
            first.mkdir()
            second.mkdir()
            preferences = base / "config" / "ui.json"
            with patch.object(tui, "_preferences_path", return_value=preferences):
                tui._remember_workspace(first)
                tui._remember_workspace(second)
                tui._remember_workspace(first)
                self.assertEqual(
                    tui._load_recent_workspaces(),
                    (str(first.resolve()), str(second.resolve())),
                )
