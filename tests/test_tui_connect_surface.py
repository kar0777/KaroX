"""One visible connection command, and one root flow behind it.

KaroX had five entry points for a single product scenario -- ``/connect``,
``/connections``, ``/providers``, ``/mcp-clients`` and ``/bridge`` -- which is
four ways for two screens to disagree about what is connected. These tests pin
the collapse: ``/connect`` is the only one a person is offered, the retired names
keep working as hidden aliases, and every one of them lands on the same
Connections screen.

The tests drive the real command handler and the real slash menu rather than
inspecting dictionaries only, because a catalog can be tidy while the routing
still opens the old wizard.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox import tui

RETIRED_COMMANDS = ("/connections", "/providers", "/mcp-clients", "/bridge")


class VisibleCommandSurfaceTests(unittest.TestCase):
    """What the menu and ``/help`` offer, in both languages."""

    def test_connect_is_the_only_visible_connection_command(self) -> None:
        for language in ("ru", "en"):
            with self.subTest(language=language):
                visible = tui._commands(language)
                heads = {name.split(" ", 1)[0] for name in visible}
                self.assertIn("/connect", heads)
                for retired in RETIRED_COMMANDS:
                    self.assertNotIn(
                        retired,
                        heads,
                        f"{retired} must not be offered next to /connect",
                    )

    def test_the_visible_set_is_compact_and_identical_in_both_languages(self) -> None:
        russian = {name.split(" ", 1)[0] for name in tui._commands("ru")}
        english = {name.split(" ", 1)[0] for name in tui._commands("en")}
        self.assertEqual(russian, english, "a command must not be visible in one language only")
        self.assertEqual(russian, set(tui.VISIBLE_COMMANDS))
        # Bare `/` is the task-first surface, not a cockpit. KaroX's advanced
        # differentiators remain searchable by prefix and in `/help all`.
        self.assertLessEqual(len(russian), 10)
        self.assertEqual(
            russian,
            {
                "/models",
                "/effort",
                "/mode",
                "/review",
                "/map",
                "/resume",
                "/new",
                "/project",
                "/connect",
                "/help",
            },
        )
        for advanced in ("/status", "/economy", "/orchestrate"):
            self.assertIn(advanced, tui.DISCOVERABLE_COMMANDS)

    def test_every_visible_command_has_a_description_in_both_languages(self) -> None:
        for language in ("ru", "en"):
            for name, description in tui._commands(language).items():
                with self.subTest(language=language, command=name):
                    self.assertTrue(description.strip(), f"{name} has no description")

    def test_the_retired_names_are_still_real_commands(self) -> None:
        """Hidden is not removed: the alias must still route somewhere."""

        for retired in RETIRED_COMMANDS:
            with self.subTest(command=retired):
                self.assertIn(retired, tui.DEPRECATED_COMMAND_ALIASES)

    def test_hidden_but_working_commands_are_not_deleted(self) -> None:
        """`/verify`, `/ask`, `/sponsors`, `/mcp` and `/bridge stop` still exist.

        They are out of the menu because the menu is for the few things that are
        not work, not because they were retired. Deleting a working command to
        shorten a list would be a regression dressed up as simplification.
        """

        for name in ("/verify JSON", "/mcp", "/sponsors", "/bridge stop"):
            with self.subTest(command=name):
                self.assertIn(name, tui.SLASH_COMMANDS)


class SlashMenuTests(unittest.IsolatedAsyncioTestCase):
    """The suggestion list a person actually sees while typing."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(tui, "_load_language", return_value="en"))
        self.enterContext(patch.object(tui, "_selected_model", return_value=None))
        self.enterContext(patch.object(tui, "session_dir", lambda: self.root))

    def app(self) -> tui.KaroXApp:
        return tui.KaroXApp(Path.cwd(), language="en")

    async def test_a_bare_slash_suggests_only_visible_commands(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            app._update_command_menu("/")
            suggestions = {name.split(" ", 1)[0] for name in app._filtered_commands}
            self.assertTrue(suggestions, "the menu offered nothing at all")
            self.assertIn("/connect", suggestions)
            for retired in RETIRED_COMMANDS:
                self.assertNotIn(retired, suggestions)

    async def test_typing_prefix_searches_all_human_commands(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            app._update_command_menu("/m")
            offered = {name.split(" ", 1)[0] for name in app._filtered_commands}
            self.assertTrue({"/models", "/mode", "/map", "/memory", "/mcp", "/mission"} <= offered)
            # Advanced KaroX features are discoverable without bloating bare `/`.
            self.assertIn("/memory", offered)
            self.assertIn("/mission", offered)

    async def test_typing_a_retired_name_does_not_suggest_it(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            for retired in ("/conn", "/prov", "/mcp-cl", "/brid", "/mod"):
                app._update_command_menu(retired)
                offered = set(app._filtered_commands)
                self.assertFalse(
                    offered & set(RETIRED_COMMANDS),
                    f"typing {retired!r} advertised a retired command: {offered}",
                )

    async def test_help_lists_only_visible_commands(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            written: list[str] = []
            with patch.object(app, "_write", side_effect=written.append):
                app._handle_command("/help")
            self.assertTrue(written)
            body = written[0]
            self.assertIn("/connect", body)
            for retired in RETIRED_COMMANDS:
                self.assertNotIn(retired, body, f"/help advertised {retired}")


class ConnectRoutingTests(unittest.IsolatedAsyncioTestCase):
    """Every path arrives at the same Connections screen."""

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.enterContext(patch.object(tui, "_load_language", return_value="en"))
        self.enterContext(patch.object(tui, "_selected_model", return_value=None))
        self.enterContext(patch.object(tui, "session_dir", lambda: self.root))

    def app(self) -> tui.KaroXApp:
        return tui.KaroXApp(Path.cwd(), language="en")

    async def _routed(self, command: str) -> tuple[list[object], list[str]]:
        """Run one command and report which screen class it pushed."""

        app = self.app()
        pushed: list[object] = []
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            with patch.object(
                app, "push_screen", side_effect=lambda screen, *_a, **_k: pushed.append(screen)
            ):
                app._handle_command(command)
            names = [type(screen).__name__ for screen in pushed]
        return pushed, names

    async def test_connect_opens_the_universal_hub(self) -> None:
        _pushed, names = await self._routed("/connect")
        self.assertEqual(len(names), 1, f"one root flow, not several: {names}")
        self.assertIn("Hub", names[0], f"/connect opened {names[0]}")

    async def test_connect_no_longer_opens_the_legacy_wizard(self) -> None:
        """The api/web/both screen asked the user to classify first.

        That is the question the hub answers for them, so the legacy screen must
        be off the production path -- reachable in the source, never from a key
        press or a command.
        """

        _pushed, names = await self._routed("/connect")
        self.assertNotIn("ConnectionChoiceScreen", names)

    async def test_retired_aliases_open_the_right_section(self) -> None:
        expected = {
            "/connections": "Hub",
            "/providers": "ModelProvidersScreen",
            "/mcp-clients": "McpClientsScreen",
            "/bridge": "McpClientsScreen",
        }
        for command, marker in expected.items():
            with self.subTest(command=command):
                _pushed, names = await self._routed(command)
                self.assertEqual(len(names), 1, f"{command} pushed {names}")
                self.assertIn(marker, names[0], f"{command} opened {names[0]}")

    async def test_models_opens_the_single_picker_not_a_management_screen(self) -> None:
        """`/models` is a model question, not a connection-management one.

        `/models` owns model choice directly. Effort is a separate `/effort`
        control, so model selection never opens a combined cockpit screen.
        """

        _pushed, names = await self._routed("/models")
        self.assertEqual(len(names), 1, f"/models pushed {names}")
        self.assertIn("ModelPickerScreen", names[0], f"/models opened {names[0]}")

    async def test_models_no_longer_runs_a_subprocess_listing(self) -> None:
        """`/models` used to shell out to `karox model list --json`.

        It now opens the picker, so no inspection subprocess runs at all.
        """

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            with patch.object(app, "_run_inspection") as inspection:
                with patch.object(app, "push_screen"):
                    app._handle_command("/models")
            inspection.assert_not_called()

    async def test_bridge_stop_still_stops_the_bridge(self) -> None:
        """The alias must not swallow a real action."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            with patch.object(app, "_stop_bridge") as stop:
                with patch.object(app, "push_screen") as pushed:
                    app._handle_command("/bridge stop")
            stop.assert_called_once_with()
            pushed.assert_not_called()

    async def test_sessions_and_doctor_keep_their_own_behaviour(self) -> None:
        """Collapsing the connection commands must not touch the others."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            with patch.object(app, "_run_inspection") as inspection:
                app._handle_command("/doctor")
            inspection.assert_called_once()


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
