"""One connection screen, and it answers three questions.

B1. ``/connect`` used to open a menu of KaroX's own protocol families -- "MCP
clients", "Model providers", "new API provider" -- behind which two separate
screens read two separate stores. A person could not learn what was connected
without first classifying a thing they had not yet seen, and nowhere in the
product showed everything at once.

These tests pin the replacement:

* what is connected, whether it works, and what can be added -- and nothing else;
* one list over the two stores that were already the source of truth;
* one entry per connection;
* semantic ids, so reordering the screen cannot repoint a flow;
* and no internal vocabulary anywhere on the root.

The view model is a pure function of the two sources, so most of this runs
without a terminal. The parts that only exist in a running application --
routing, focus, Esc -- drive the real app through its production callbacks.
"""

from __future__ import annotations

from _unittest_compat import enter_context

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from _tui_harness import isolated_karox_directories

from karox import tui
from karox import tui_connections as hub

# Vocabulary that belongs to KaroX, not to the person reading the screen. Every
# one of these was reachable from the old root, either as a button label or in a
# row like "My ClickUp  [running]  (clickup - streamable_http - bearer)".
INTERNAL_TERMS = (
    "adapter_kind",
    "openai_compatible_chat",
    "anthropic_messages",
    "access_profile",
    "workspace_write",
    "streamable_http",
    "auth_scheme",
    "bearer",
    "credential_ref",
    "os-keyring",
    "cloudflare",
    "tailscale",
    "8765",
    "endpoint_path",
    "preset_id",
    "runtime_profile",
    "url_stability",
    "mcp client",
    "model provider",
)


class _Provider:
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id
        self.adapter_kind = "openai_compatible_chat"


class _Model:
    def __init__(self, provider_id: str, model_id: str) -> None:
        self.provider_id = provider_id
        self.model_id = model_id


class _Registry:
    """The provider registry, reduced to the three calls the hub makes."""

    def __init__(self, catalog: dict, selected: object = None) -> None:
        self._catalog = catalog
        self._selected = selected

    def providers(self):
        return [_Provider(pid) for pid in self._catalog]

    def models(self, provider_id: str):
        return [_Model(provider_id, m) for m in self._catalog.get(provider_id, ())]

    def selected_model(self):
        return self._selected


class _Target:
    def __init__(self, connection_id: str, name: str, preset_id: str) -> None:
        self.connection_id = connection_id
        self.name = name
        self.preset_id = preset_id
        # Present on the real object, and deliberately never read into a row.
        self.transport = "streamable_http"
        self.auth_scheme = "bearer"
        self.tunnel = "cloudflare"
        self.port = 8765
        self.endpoint_path = "/mcp"
        self.credential_ref = "os-keyring:connection/abc"


class _State:
    def __init__(self, target: _Target, state: str) -> None:
        self.target = target
        self.state = state
        self.endpoint = "https://tunnel.example.com/mcp"


class HubViewModelTests(unittest.TestCase):
    """The right rows, from the stores that already own them."""

    def test_the_hub_shows_existing_ai_providers(self) -> None:
        registry = _Registry(
            {"openrouter": ("claude-opus",)},
            selected=_Model("openrouter", "claude-opus"),
        )
        rows = hub.provider_hub_rows(registry)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].family, hub.HUB_FAMILY_AI)
        self.assertEqual(rows[0].name, "OpenRouter")
        self.assertEqual(rows[0].detail, "claude-opus")
        # B5 audit 5.5 narrowed this. B1 wrote HUB_STATUS_WORKING here because
        # "has a model" was the only signal the hub had. It is not evidence:
        # a revoked key, a moved endpoint and a suspended account all look
        # exactly like this in the registry. Nothing has checked this provider,
        # so the honest word is "ready to check", and only a verification
        # inside the detail screen may promote it.
        self.assertEqual(rows[0].status, hub.HUB_STATUS_READY)

    def test_a_provider_with_no_model_asks_for_attention(self) -> None:
        """A saved key that can send nothing is what this screen exists to reveal."""

        rows = hub.provider_hub_rows(_Registry({"openai": ()}))
        self.assertEqual(rows[0].status, hub.HUB_STATUS_ATTENTION)
        self.assertEqual(rows[0].detail, "")

    def test_root_current_provider_shows_only_the_selected_model(self) -> None:
        registry = _Registry(
            {"old-provider": ("old-model",), "openrouter": ("claude-opus",)},
            selected=_Model("openrouter", "claude-opus"),
        )
        rows = hub.current_provider_hub_rows(registry)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].row_id, "ai:openrouter")
        self.assertEqual(rows[0].detail, "claude-opus")

    def test_root_current_provider_does_not_guess_when_nothing_is_selected(self) -> None:
        rows = hub.current_provider_hub_rows(_Registry({"old-provider": ("old-model",)}))
        self.assertEqual(rows, ())

    def test_the_hub_shows_existing_web_and_mcp_clients(self) -> None:
        states = [
            _State(_Target("c1", "My ClickUp", "clickup"), "running"),
            _State(
                _Target("c2", "Scratch API", "generic-mcp"),
                "configured_not_running",
            ),
        ]
        rows = hub.service_hub_rows(states)
        self.assertEqual([row.name for row in rows], ["My ClickUp", "Scratch API"])
        self.assertEqual(rows[0].family, hub.HUB_FAMILY_SERVICE)
        self.assertEqual(rows[0].status, hub.HUB_STATUS_WORKING)
        self.assertEqual(rows[1].family, hub.HUB_FAMILY_OTHER)
        self.assertEqual(rows[1].status, hub.HUB_STATUS_STOPPED)

    def test_root_hides_stopped_history_but_keeps_active_and_broken_services(self) -> None:
        states = [
            _State(_Target("old", "Old ClickUp", "clickup"), "stopped"),
            _State(_Target("live", "Live ChatGPT", "chatgpt-web"), "running"),
            _State(_Target("broken", "Broken service", "clickup"), "degraded"),
        ]
        rows = hub.active_service_hub_rows(states)
        self.assertEqual([row.name for row in rows], ["Live ChatGPT", "Broken service"])
        self.assertNotIn("Old ClickUp", [row.name for row in rows])

    def test_a_runtime_nobody_mapped_reads_as_an_error_not_as_fine(self) -> None:
        rows = hub.service_hub_rows(
            [_State(_Target("c3", "Wedged", "clickup"), "runtime_error")]
        )
        self.assertEqual(rows[0].status, hub.HUB_STATUS_ERROR)

    def test_one_list_does_not_duplicate_one_connection(self) -> None:
        """The failure the two-screen hub made easy."""

        providers = hub.provider_hub_rows(_Registry({"openai": ("gpt-4o",)}))
        services = hub.service_hub_rows(
            [_State(_Target("c1", "My ClickUp", "clickup"), "running")]
        )
        merged = hub.merge_hub_rows(providers, services, providers, services)
        self.assertEqual(len(merged), 2)
        self.assertEqual(len({row.row_id for row in merged}), 2)

    def test_a_row_has_nowhere_to_put_a_technical_field(self) -> None:
        """Structural, not filtered: the type is the boundary."""

        fields = set(hub.HubRow.__dataclass_fields__)
        self.assertEqual(fields, {"row_id", "family", "name", "detail", "status"})
        for banned in (
            "adapter_kind",
            "access_profile",
            "transport",
            "auth_scheme",
            "port",
            "tunnel",
            "endpoint",
            "credential_ref",
        ):
            with self.subTest(field=banned):
                self.assertNotIn(banned, fields)

    def test_rendered_rows_carry_no_internal_vocabulary(self) -> None:
        registry = _Registry(
            {"openrouter": ("claude-opus",)},
            selected=_Model("openrouter", "claude-opus"),
        )
        states = [
            _State(_Target("c1", "My ClickUp", "clickup"), "running"),
            _State(_Target("c2", "Scratch", "custom"), "degraded"),
        ]
        rows = hub.merge_hub_rows(
            hub.provider_hub_rows(registry), hub.service_hub_rows(states)
        )
        for english in (True, False):
            text = " ".join(hub.hub_row_text(row, english) for row in rows).casefold()
            for term in INTERNAL_TERMS:
                with self.subTest(term=term, english=english):
                    self.assertNotIn(term, text)

    def test_an_absent_fact_leaves_no_dash_and_no_stray_separator(self) -> None:
        row = hub.HubRow(
            row_id="service:c1",
            family=hub.HUB_FAMILY_SERVICE,
            name="ChatGPT Web",
            status=hub.HUB_STATUS_WORKING,
        )
        text = hub.hub_row_text(row, True)
        self.assertEqual(text, "ChatGPT Web \u00b7 works")
        self.assertNotIn("\u2014", text)
        self.assertNotIn("\u00b7\u00b7", text)

    def test_a_broken_source_costs_its_own_rows_and_not_the_screen(self) -> None:
        class _Angry:
            def providers(self):
                raise RuntimeError("keyring is locked")

        self.assertEqual(hub.provider_hub_rows(_Angry()), ())


class HubVocabularyTests(unittest.TestCase):
    """RU/EN parity, and the three things the root is allowed to offer."""

    def test_the_add_actions_are_the_three_the_product_promises(self) -> None:
        self.assertEqual(
            hub.HUB_ADD_ACTIONS,
            (hub.HUB_ADD_MODEL, hub.HUB_ADD_SERVICE, hub.HUB_ADD_OTHER),
        )

    def test_add_entries_name_products_rather_than_protocols(self) -> None:
        english = [hub.hub_add_words(a, True) for a in hub.HUB_ADD_ACTIONS]
        self.assertEqual(english[0], "AI model")
        self.assertIn("ChatGPT", english[1])
        self.assertIn("ClickUp", english[1])
        joined = " ".join(english).casefold()
        self.assertNotIn("mcp client", joined)
        self.assertNotIn("model provider", joined)
        self.assertNotIn("bridge", joined)

    def test_every_status_and_action_has_both_languages(self) -> None:
        for status in hub._HUB_STATUS_WORDS:
            with self.subTest(status=status):
                self.assertTrue(hub.hub_status_words(status, True).strip())
                self.assertTrue(hub.hub_status_words(status, False).strip())
        for action in hub.HUB_ADD_ACTIONS:
            with self.subTest(action=action):
                self.assertTrue(hub.hub_add_words(action, True).strip())
                self.assertTrue(hub.hub_add_words(action, False).strip())
        for action in hub.HUB_MANAGE_ACTIONS:
            with self.subTest(action=action):
                self.assertTrue(hub.hub_manage_words(action, True, count=3).strip())
                self.assertTrue(hub.hub_manage_words(action, False, count=3).strip())

    def test_russian_and_english_rows_carry_the_same_facts(self) -> None:
        row = hub.HubRow(
            row_id="ai:openrouter",
            family=hub.HUB_FAMILY_AI,
            name="OpenRouter",
            detail="claude-opus",
            status=hub.HUB_STATUS_WORKING,
        )
        english = hub.hub_row_text(row, True)
        russian = hub.hub_row_text(row, False)
        self.assertEqual(english.count(" \u00b7 "), russian.count(" \u00b7 "))
        self.assertIn("claude-opus", russian)
        self.assertNotEqual(english, russian)

    def test_an_unknown_status_is_shown_as_an_error_not_as_a_blank(self) -> None:
        self.assertEqual(hub.hub_status_words("teleporting", True), "error")

    def test_a_provider_id_nobody_published_a_name_for_is_still_readable(self) -> None:
        self.assertEqual(hub.provider_display_name("my-local-box"), "My Local Box")


class HubScreenTests(unittest.IsolatedAsyncioTestCase):
    """The running screen: routing, keyboard, Esc, and the narrow terminal."""

    def setUp(self) -> None:
        # The hub reads the real provider registry and the real connection
        # registry, both resolved through ``config_dir()``. Patching
        # ``session_dir`` alone left these tests reading whatever the developer
        # happens to have connected -- which made them non-deterministic and,
        # worse, let a machine-specific connection name decide whether an
        # assertion about KaroX's own vocabulary passed.
        self.repository = enter_context(self, isolated_karox_directories())
        self.root = Path(enter_context(self, tempfile.TemporaryDirectory()))
        enter_context(self, patch.object(tui, "_load_language", return_value="en"))
        enter_context(self, patch.object(tui, "_selected_model", return_value=None))
        enter_context(self, patch.object(tui, "session_dir", lambda: self.root))

    def app(self) -> tui.KaroXApp:
        return tui.KaroXApp(self.repository, language="en")

    async def _pushed(self, run) -> list:
        """Run one production entry point and report what it opened."""

        app = self.app()
        pushed: list = []
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            with patch.object(
                app,
                "push_screen",
                side_effect=lambda screen, *_a, **_k: pushed.append(screen),
            ):
                run(app)
            await pilot.pause()
        return [type(screen).__name__ for screen in pushed]

    async def test_connect_opens_exactly_one_hub(self) -> None:
        names = await self._pushed(lambda app: app._handle_command("/connect"))
        self.assertEqual(names, ["ConnectionHubScreen"])

    async def test_the_connections_key_opens_the_same_hub(self) -> None:
        """Ctrl+S used to push the legacy api/web/both wizard.

        Two roots for one scenario meant whichever one a person happened to use
        decided what they believed was connected.
        """

        names = await self._pushed(lambda app: app.action_onboarding())
        self.assertEqual(names, ["ConnectionHubScreen"])

    async def test_no_production_path_opens_the_legacy_choice_screen(self) -> None:
        entries = (
            lambda app: app._handle_command("/connect"),
            lambda app: app._handle_command("/connections"),
            lambda app: app._handle_command("/providers"),
            lambda app: app._handle_command("/models"),
            lambda app: app._handle_command("/mcp-clients"),
            lambda app: app._handle_command("/bridge"),
            lambda app: app.action_onboarding(),
            lambda app: app.action_connect(),
        )
        for index, entry in enumerate(entries):
            names = await self._pushed(entry)
            with self.subTest(entry=index, names=names):
                self.assertNotIn("ConnectionChoiceScreen", names)

    async def test_the_hidden_aliases_reach_the_same_controller(self) -> None:
        """A retired name opens a section, never a rival root."""

        expected = {
            "/providers": "ModelProvidersScreen",
            "/mcp-clients": "McpClientsScreen",
            "/bridge": "McpClientsScreen",
            "/connections": "ConnectionHubScreen",
        }
        for command, screen in expected.items():
            names = await self._pushed(lambda app, c=command: app._handle_command(c))
            with self.subTest(command=command):
                self.assertEqual(names, [screen])

    async def test_models_opens_the_picker_instead_of_a_rival_root(self) -> None:
        """`/models` is the model picker question now, not a hub section."""

        names = await self._pushed(lambda app: app._handle_command("/models"))
        self.assertEqual(names, ["ModelPickerScreen"])

    async def test_the_aliases_stay_out_of_the_menu_and_help(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            app._update_command_menu("/")
            offered = {n.split(" ", 1)[0] for n in app._filtered_commands}
            written: list = []
            with patch.object(app, "_write", side_effect=written.append):
                app._handle_command("/help")
        for alias in tui.DEPRECATED_COMMAND_ALIASES:
            with self.subTest(alias=alias):
                self.assertNotIn(alias, offered)
                self.assertNotIn(alias, written[0])
        self.assertIn("/connect", offered)

    async def _hub(self, pilot, app, *, language: str = "en"):
        screens = app._connections_screens_cached()
        screen = screens["ConnectionHubScreen"](language)
        app.push_screen(screen, app._connection_hub_done)
        await pilot.pause()
        return screen

    def _option_ids(self, screen) -> list:
        options = screen.query_one("#connhub-list")
        return [
            getattr(options.get_option_at_index(i), "id", None)
            for i in range(len(options.options))
        ]

    async def test_the_root_offers_the_three_add_entries_by_semantic_id(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._hub(pilot, app)
            # The hub renders a loading entry first and fills its options when
            # the controller answers. Asserting straight after the push read
            # ['connhub-loading'] on a loaded runner, so wait for the state the
            # assertion is about; the subTests below still fail if an entry
            # never arrives.
            ids: list = []
            for _ in range(100):
                ids = self._option_ids(screen)
                if all(action in ids for action in hub.HUB_ADD_ACTIONS):
                    break
                await pilot.pause()
            for action in hub.HUB_ADD_ACTIONS:
                with self.subTest(action=action):
                    self.assertIn(action, ids)

    async def test_root_does_not_dump_stopped_history_next_to_current_model(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._hub(pilot, app)
            registry = _Registry(
                {"empirio-labs": ("glm-5-2",), "old-provider": ("old-model",)},
                selected=_Model("empirio-labs", "glm-5-2"),
            )
            stale = [
                _State(_Target(f"old-{i}", "ClickUp", "clickup"), "stopped")
                for i in range(5)
            ]
            stale.extend(
                [
                    _State(_Target("custom-1", "Custom MCP client", "generic-mcp"), "stopped"),
                    _State(_Target("custom-2", "Custom MCP client", "generic-mcp"), "stopped"),
                ]
            )
            with patch.object(screen._providers, "registry", registry), patch.object(
                screen._controller, "list", return_value=stale
            ):
                screen.refresh_rows()
            options = screen.query_one("#connhub-list")
            text = "\n".join(
                str(options.get_option_at_index(i).prompt)
                for i in range(len(options.options))
            )
            self.assertIn("Current AI model", text)
            self.assertIn("glm-5-2", text)
            self.assertNotIn("  ClickUp · stopped", text)
            self.assertNotIn("  Custom MCP client · stopped", text)
            self.assertIn("All connections (7)", text)

    async def test_the_root_offers_explicit_management_entries(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._hub(pilot, app)
            ids = self._option_ids(screen)
            for action in hub.HUB_MANAGE_ACTIONS:
                with self.subTest(action=action):
                    self.assertIn(action, ids)

    async def test_manage_entries_route_to_the_full_saved_lists(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            pushed: list[str] = []
            with patch.object(
                app,
                "push_screen",
                side_effect=lambda screen, *_a, **_k: pushed.append(type(screen).__name__),
            ):
                app._connection_hub_done(hub.HUB_MANAGE_MODELS)
                await pilot.pause()
                app._connection_hub_done(hub.HUB_MANAGE_CONNECTIONS)
                await pilot.pause()
            self.assertEqual(pushed, ["ModelProvidersScreen", "McpClientsScreen"])

    async def test_the_root_shows_no_internal_vocabulary(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._hub(pilot, app)
            options = screen.query_one("#connhub-list")
            text = " ".join(
                str(getattr(options.get_option_at_index(i), "prompt", ""))
                for i in range(len(options.options))
            ).casefold()
            for term in INTERNAL_TERMS:
                with self.subTest(term=term):
                    self.assertNotIn(term, text)

    async def test_the_add_model_entry_opens_the_provider_flow(self) -> None:
        """Named by meaning: the test never mentions a list position."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            with patch.object(app, "action_provider_preset") as wizard:
                app._connection_hub_done(hub.HUB_ADD_MODEL)
                await pilot.pause()
            wizard.assert_called_once_with()
            self.assertTrue(app._connect_return_to_hub)

    async def test_a_saved_provider_returns_to_the_hub(self) -> None:
        """And does not open the bridge wizard on the way."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            app._connect_return_to_hub = True
            record = tui.ModelRecord("openai", "gpt-4o")
            opened: list = []
            with patch.object(tui, "_selected_model", return_value=record):
                with patch.object(app, "action_bridge") as bridge:
                    with patch.object(
                        app,
                        "_open_connections",
                        side_effect=lambda focus=None: opened.append(focus),
                    ):
                        app._provider_setup_done(object())
                        await pilot.pause()
            bridge.assert_not_called()
            self.assertEqual(opened, [None])
            self.assertFalse(app._connect_return_to_hub)

    async def test_a_wizard_opened_outside_the_hub_still_ends_in_the_chat(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            self.assertFalse(app._connect_return_to_hub)
            with patch.object(app, "_open_connections") as reopen:
                app._provider_preset_done(None)
                await pilot.pause()
            reopen.assert_not_called()
            self.assertTrue(app.query_one("#composer").has_focus)

    async def test_the_keyboard_reaches_every_entry_without_a_mouse(self) -> None:
        """Arrows must not stall on a heading, which is a dead key press."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._hub(pilot, app)
            options = screen.query_one("#connhub-list")
            reached = set()
            for _ in range(len(options.options) + 2):
                highlighted = options.highlighted
                if highlighted is not None:
                    option = options.get_option_at_index(highlighted)
                    self.assertFalse(
                        getattr(option, "disabled", False),
                        "the cursor stopped on a heading",
                    )
                    reached.add(getattr(option, "id", None))
                screen.action_next()
            for action in hub.HUB_ADD_ACTIONS:
                with self.subTest(action=action):
                    self.assertIn(action, reached)

    async def test_enter_on_a_heading_does_nothing(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._hub(pilot, app)
            depth = len(app.screen_stack)
            options = screen.query_one("#connhub-list")
            for index, identifier in enumerate(self._option_ids(screen)):
                if identifier and identifier.startswith("connhub-heading"):
                    options.highlighted = index
                    screen.action_choose()
                    await pilot.pause()
                    self.assertEqual(len(app.screen_stack), depth)
                    return

    async def test_escape_returns_to_the_chat_and_does_not_loop(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            depth = len(app.screen_stack)
            await self._hub(pilot, app)
            self.assertEqual(len(app.screen_stack), depth + 1)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), depth)
            self.assertTrue(app.query_one("#composer").has_focus)

    async def test_a_narrow_terminal_keeps_the_hub_usable(self) -> None:
        app = self.app()
        async with app.run_test(size=(46, 14)) as pilot:
            await pilot.pause()
            screen = await self._hub(pilot, app)
            options = screen.query_one("#connhub-list")
            self.assertGreaterEqual(len(options.options), len(hub.HUB_ADD_ACTIONS))
            hint = str(screen.query_one("#connhub-hint").render())
            self.assertNotIn("\n", hint)
            dialog = screen.query_one("#connhub-dialog")
            self.assertLessEqual(dialog.size.width, 46)
            self.assertLessEqual(dialog.size.height, 14)

    async def test_a_wide_terminal_does_not_grow_a_dashboard(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            screen = await self._hub(pilot, app)
            dialog = screen.query_one("#connhub-dialog")
            # Free space is left free rather than filled with telemetry.
            self.assertLessEqual(dialog.size.width, 62)

    async def test_opening_the_hub_does_not_stop_a_running_bridge(self) -> None:
        """Reading the list is not an action on the connections."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._hub(pilot, app)
            controller = screen._controller
            with patch.object(controller, "stop") as stop:
                with patch.object(controller, "restart") as restart:
                    screen.refresh_rows()
                    await pilot.pause()
            stop.assert_not_called()
            restart.assert_not_called()

    async def test_the_hub_does_not_disturb_the_a1_shell_or_the_a2_line(self) -> None:
        """B must not undo either finished slice."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            await self._hub(pilot, app)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(len(app.query("#header-status")), 1)
            self.assertEqual(len(app.query("#activity")), 1)
            self.assertEqual(len(app.query("#sponsor-ticker")), 0)
            for retired in (
                "#repo-status",
                "#model-status",
                "#session-status",
                "#context-status",
                "#bridge-status",
            ):
                with self.subTest(widget=retired):
                    self.assertEqual(len(app.query(retired)), 0)
            self.assertEqual(str(app.query_one("#activity").styles.display), "none")


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
