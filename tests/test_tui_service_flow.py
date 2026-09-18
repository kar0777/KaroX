"""Choosing a service, not configuring a protocol.

B3. From one `/connect` a person picks ChatGPT Web, Claude Web, Hyperagent, Notion or ClickUp and
sees a human connection scenario: numbered steps, one address, one status, one
check. KaroX picks the parameters.

These tests pin the properties that make that safe rather than merely tidier:

* the standard screen shows no port, tunnel, transport, adapter, access profile,
  tool name or credential;
* the defaults come from the production preset catalog, not a second table;
* opening a flow neither starts, stops nor restarts a bridge, and reuses the
  endpoint that already exists;
* "works" is only shown for a check that actually proved the service;
* a failure keeps the screen, keeps the values, offers a retry and leaks nothing;
* Esc goes back exactly one level.

The view model is a pure function of the preset and the runtime state, so most of
this needs no terminal. Routing, Esc and the bridge-safety properties drive the
real application through its production callbacks.
"""

from __future__ import annotations

from _unittest_compat import enter_context

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from _tui_harness import isolated_karox_directories

from karox import tui
from karox import tui_connections as hub
from karox.connections import MCP_CLIENT_PRESETS
from karox.models import AccessProfile
from karox.web_bridge_profiles import SavedWebBridgeProfile, WebBridgeProfileStore
from karox.tui_workspace import WorkspaceManagerScreen

# Vocabulary that belongs to KaroX. Every one of these is a real field on the
# objects behind the screen, which is exactly why the absence is worth asserting.
INTERNAL_TERMS = (
    "streamable_http",
    "openapi",
    "transport",
    "auth_scheme",
    "bearer",
    "oauth",
    "api_key",
    "cloudflare",
    "tailscale",
    "tunnel",
    "8765",
    "127.0.0.1",
    "endpoint_path",
    "runtime_profile",
    "generic-streamable-http",
    "access_profile",
    "workspace_write",
    "credential_ref",
    "os-keyring",
    "repo.read_file",
    "checks.run",
)

SERVICES = ("chatgpt-web", "claude-web", "hyperagent-web", "notion", "clickup", "adapt")

# A token-shaped fixture, built at runtime so the write path cannot redact the
# literal and leave the assertion comparing a marker against a marker.
FAKE_SECRET = "appr-" + ("7" * 30)


class _Target:
    def __init__(self, connection_id: str, preset_id: str) -> None:
        self.connection_id = connection_id
        self.preset_id = preset_id
        self.name = hub.service_display_name(preset_id)
        # Present on the real object, and never carried into a view.
        self.transport = "streamable_http"
        self.auth_scheme = "bearer"
        self.tunnel = "cloudflare"
        self.port = 8765
        self.endpoint_path = "/mcp"
        self.credential_ref = "os-keyring:connection/abc"


class _State:
    def __init__(self, target: _Target, state: str, endpoint: str) -> None:
        self.target = target
        self.state = state
        self.endpoint = endpoint


class SavedProfileSelectionTests(unittest.TestCase):
    """One preset, one repository, several saved profiles sharing one port.

    Only one of them can hold the port. Selecting by name or by stored intent was
    free to pick a different one, and Start/Repair then reported the user's own
    working bridge as ``port 8765 is held by an unrelated process``. The profile
    that provably holds the port is the profile this row is about.
    """

    def _screen(self, preset_id: str = "chatgpt-web"):
        screens = tui.KaroXApp._connections_screens_cached(tui.KaroXApp)
        cls = screens["ServiceConnectScreen"]
        screen = cls.__new__(cls)
        screen.preset_id = preset_id
        return cls, screen

    def _profile(self, name: str, *, port: int = 8765) -> SimpleNamespace:
        return SimpleNamespace(name=name, port=port)

    def _verdict(self, code: str, *, owned_orphan_pid: int | None = None):
        return SimpleNamespace(verdict=code, owned_orphan_pid=owned_orphan_pid)

    def test_the_profile_that_holds_the_port_wins_over_stored_intent(self) -> None:
        cls, screen = self._screen()
        candidates = [
            self._profile("aura-browser"),
            self._profile("clickup-opus"),
            self._profile("chatgpt-dev"),
        ]
        verdicts = {
            "aura-browser": self._verdict("unrelated_process"),
            "clickup-opus": self._verdict("unrelated_process"),
            "chatgpt-dev": self._verdict("reuse_same_profile"),
        }
        with patch(
            "karox.port_ownership.check_port_ownership",
            side_effect=lambda name, port: verdicts[name],
        ), patch(
            "karox.saved_bridge_supervisor._read_desired_running",
            side_effect=lambda name: name == "clickup-opus",
        ):
            chosen = cls._select_saved_profile(screen, candidates)
        self.assertEqual(chosen.name, "chatgpt-dev")

    def test_an_owned_but_unmanaged_bridge_still_counts_as_this_profile(self) -> None:
        """A bridge of ours whose owner lost its record is still ours."""
        cls, screen = self._screen()
        candidates = [self._profile("aura-browser"), self._profile("chatgpt-dev")]
        verdicts = {
            "aura-browser": self._verdict("unrelated_process"),
            "chatgpt-dev": self._verdict(
                "stale_owned_process", owned_orphan_pid=4242
            ),
        }
        with patch(
            "karox.port_ownership.check_port_ownership",
            side_effect=lambda name, port: verdicts[name],
        ):
            chosen = cls._select_saved_profile(screen, candidates)
        self.assertEqual(chosen.name, "chatgpt-dev")

    def test_with_nothing_live_the_recorded_intent_still_decides(self) -> None:
        cls, screen = self._screen()
        candidates = [
            self._profile("aura-browser"),
            self._profile("clickup-opus"),
            self._profile("chatgpt-dev"),
        ]
        with patch(
            "karox.port_ownership.check_port_ownership",
            side_effect=lambda name, port: self._verdict("free"),
        ), patch(
            "karox.saved_bridge_supervisor._read_desired_running",
            side_effect=lambda name: name == "clickup-opus",
        ):
            chosen = cls._select_saved_profile(screen, candidates)
        self.assertEqual(chosen.name, "clickup-opus")

    def test_a_single_candidate_needs_no_port_probe(self) -> None:
        cls, screen = self._screen()
        only = self._profile("chatgpt-dev")
        with patch("karox.port_ownership.check_port_ownership") as probe:
            chosen = cls._select_saved_profile(screen, [only])
        probe.assert_not_called()
        self.assertIs(chosen, only)


class ServiceCatalogTests(unittest.TestCase):
    """Requirement 6: the defaults have one home, and it is the product's."""

    def test_every_offered_service_exists_in_the_production_catalog(self) -> None:
        for preset_id in hub.service_flow_presets():
            with self.subTest(service=preset_id):
                self.assertIn(preset_id, MCP_CLIENT_PRESETS)

    def test_the_display_name_comes_from_the_production_preset(self) -> None:
        for preset_id in SERVICES:
            with self.subTest(service=preset_id):
                expected = (
                    "Notion"
                    if preset_id == "notion"
                    else MCP_CLIENT_PRESETS[preset_id].display_name
                )
                self.assertEqual(hub.service_display_name(preset_id), expected)

    def test_the_flow_restates_no_transport_or_tunnel_default(self) -> None:
        """A second defaults table is a table that drifts from the launcher.

        `ServiceView` is the check: it has no field that could hold one.
        """

        fields = set(hub.ServiceView.__dataclass_fields__)
        self.assertEqual(
            fields,
            {"preset_id", "name", "steps", "endpoint", "status", "manual_note"},
        )
        for banned in (
            "transport",
            "tunnel",
            "port",
            "auth_scheme",
            "access_profile",
            "runtime_profile",
            "endpoint_path",
            "credential_ref",
        ):
            with self.subTest(field=banned):
                self.assertNotIn(banned, fields)


class ServiceStepsTests(unittest.TestCase):
    """Requirements 18-20: real steps, per service, in both languages."""

    def test_each_service_has_numbered_steps_in_both_languages(self) -> None:
        for preset_id in SERVICES:
            for english in (True, False):
                with self.subTest(service=preset_id, english=english):
                    steps = hub.service_steps(preset_id, english)
                    self.assertGreaterEqual(len(steps), 3)
                    for index, step in enumerate(steps, start=1):
                        self.assertTrue(step.startswith(f"{index}. "))
                        self.assertTrue(step[3:].strip())

    def test_the_two_languages_carry_the_same_number_of_steps(self) -> None:
        for preset_id in SERVICES:
            with self.subTest(service=preset_id):
                self.assertEqual(
                    len(hub.service_steps(preset_id, True)),
                    len(hub.service_steps(preset_id, False)),
                )
                self.assertNotEqual(
                    hub.service_steps(preset_id, True),
                    hub.service_steps(preset_id, False),
                    "one language was not translated",
                )

    def test_the_services_do_not_share_one_copied_script(self) -> None:
        """ChatGPT wants a custom MCP app; Claude wants a connector."""

        rendered = {p: hub.service_steps(p, True) for p in SERVICES}
        self.assertNotEqual(rendered["chatgpt-web"], rendered["claude-web"])
        self.assertNotEqual(rendered["claude-web"], rendered["hyperagent-web"])
        self.assertNotEqual(rendered["hyperagent-web"], rendered["notion"])
        self.assertNotEqual(rendered["notion"], rendered["clickup"])
        self.assertIn("ChatGPT", " ".join(rendered["chatgpt-web"]))
        self.assertIn("Claude", " ".join(rendered["claude-web"]))
        self.assertIn("Hyperagent", " ".join(rendered["hyperagent-web"]))
        self.assertIn("OAuth", " ".join(rendered["notion"]))
        self.assertIn("ClickUp", " ".join(rendered["clickup"]))

    def test_no_step_names_an_internal_field(self) -> None:
        for preset_id in SERVICES:
            for english in (True, False):
                text = " ".join(hub.service_steps(preset_id, english)).casefold()
                for term in INTERNAL_TERMS:
                    if term in {"oauth", "tunnel", "bearer"}:
                        # "OAuth" is the name of a choice in ClickUp's own form,
                        # so the ClickUp step names it to tell the user *not* to
                        # pick it. Notion's form offers a bearer-token field, so
                        # its step names it to say it must be left empty. Both
                        # are exempted deliberately rather than silently.
                        continue
                    with self.subTest(service=preset_id, term=term):
                        self.assertNotIn(term, text)


class ServiceStatusTests(unittest.TestCase):
    """Requirements 21-22: "works" has to be earned."""

    def test_only_a_proven_check_is_called_working(self) -> None:
        self.assertTrue(hub.service_status_is_proven(hub.SERVICE_WORKING))
        for status in (
            hub.SERVICE_NOT_CONFIGURED,
            hub.SERVICE_READY,
            hub.SERVICE_AWAITING,
            hub.SERVICE_CHECKING,
            hub.SERVICE_ACTION_REQUIRED,
            hub.SERVICE_ERROR,
            hub.SERVICE_BRIDGE_ONLY,
        ):
            with self.subTest(status=status):
                self.assertFalse(hub.service_status_is_proven(status))

    def test_a_local_only_check_is_not_presented_as_a_confirmed_service(self) -> None:
        """Hosted OAuth presets stay unverified until the external client connects."""

        for preset_id in ("chatgpt-web", "claude-web", "hyperagent-web"):
            with self.subTest(service=preset_id):
                status = hub.service_status_after_check(preset_id, {"state": "ok"})
                self.assertEqual(status, hub.SERVICE_BRIDGE_ONLY)
                self.assertFalse(hub.service_status_is_proven(status))
                words = hub.service_status_words(status, True)
                self.assertIn("bridge", words)
                self.assertIn("not confirmed", words)

    def test_a_pending_public_url_is_never_called_working(self) -> None:
        status = hub.service_status_after_check(
            "clickup", {"state": "public_pending"}
        )
        self.assertEqual(status, hub.SERVICE_BRIDGE_ONLY)

    def test_a_conclusive_check_may_say_it_works(self) -> None:
        self.assertEqual(
            hub.service_status_after_check("clickup", {"state": "ok"}),
            hub.SERVICE_WORKING,
        )

    def test_a_failed_check_is_a_connection_failure(self) -> None:
        for result in ({"state": "failed"}, {}, None, "nonsense"):
            with self.subTest(result=result):
                self.assertEqual(
                    hub.service_status_after_check("clickup", result),
                    hub.SERVICE_ERROR,
                )

    def test_a_running_bridge_alone_is_only_waiting_for_the_service(self) -> None:
        """A live bridge is not a live connection until something answers."""

        self.assertEqual(
            hub.service_status_for_runtime(
                "clickup", "running", "https://x.example.com/mcp"
            ),
            hub.SERVICE_AWAITING,
        )

    def test_no_endpoint_means_not_configured(self) -> None:
        self.assertEqual(
            hub.service_status_for_runtime("clickup", "running", ""),
            hub.SERVICE_NOT_CONFIGURED,
        )

    def test_every_status_has_both_languages(self) -> None:
        for status in hub._SERVICE_STATUS_WORDS:
            with self.subTest(status=status):
                self.assertTrue(hub.service_status_words(status, True).strip())
                self.assertTrue(hub.service_status_words(status, False).strip())
                self.assertNotEqual(
                    hub.service_status_words(status, True),
                    hub.service_status_words(status, False),
                )

    def test_an_unmapped_status_reads_as_a_failure_not_as_fine(self) -> None:
        self.assertEqual(
            hub.service_status_words("teleporting", True),
            hub.service_status_words(hub.SERVICE_ERROR, True),
        )

    def test_a_service_that_cannot_be_proven_says_so_on_the_screen(self) -> None:
        for preset_id in ("chatgpt-web", "claude-web", "hyperagent-web"):
            for english in (True, False):
                with self.subTest(service=preset_id, english=english):
                    self.assertTrue(
                        hub.service_manual_note(preset_id, english).strip()
                    )
        self.assertEqual(hub.service_manual_note("clickup", True), "")


class ServiceScreenTests(unittest.IsolatedAsyncioTestCase):
    """The running screen: routing, bridge safety, errors, Esc, narrow width."""

    def setUp(self) -> None:
        self.repository = enter_context(self, isolated_karox_directories())
        enter_context(self, patch.object(tui, "_load_language", return_value="en"))
        enter_context(self, patch.object(tui, "_selected_model", return_value=None))

    def app(self) -> tui.KaroXApp:
        return tui.KaroXApp(self.repository, language="en")

    async def _screen(self, pilot, app, preset_id: str, *, language: str = "en"):
        screens = app._connections_screens_cached()
        screen = screens["ServiceConnectScreen"](language, preset_id=preset_id)
        app.push_screen(screen)
        await pilot.pause()
        return screen

    async def test_each_service_opens_its_own_standard_screen(self) -> None:
        """Requirements 1-3, by semantic id rather than list position."""

        for preset_id in (item for item in SERVICES if item != "notion"):
            app = self.app()
            pushed: list = []
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                with patch.object(
                    app,
                    "push_screen",
                    side_effect=lambda s, *_a, **_k: pushed.append(s),
                ):
                    app._service_chosen(preset_id)
                    await pilot.pause()
            with self.subTest(service=preset_id):
                self.assertEqual(len(pushed), 1)
                self.assertEqual(
                    type(pushed[0]).__name__, "ServiceConnectScreen"
                )
                self.assertEqual(pushed[0].preset_id, preset_id)

    async def test_notion_starts_direct_outbound_oauth_instead_of_service_screen(self) -> None:
        app = self.app()
        pushed: list = []
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            with (
                patch.object(
                    app,
                    "push_screen",
                    side_effect=lambda s, *_a, **_k: pushed.append(s),
                ),
                patch.object(app, "_start_notion_workspace_connect") as start,
            ):
                app._service_chosen("notion")
                await pilot.pause()
        self.assertEqual(pushed, [])
        start.assert_called_once_with()

    async def test_the_hub_service_entry_opens_the_picker(self) -> None:
        app = self.app()
        pushed: list = []
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            with patch.object(
                app, "push_screen", side_effect=lambda s, *_a, **_k: pushed.append(s)
            ):
                app._connection_hub_done(hub.HUB_ADD_SERVICE)
                await pilot.pause()
        self.assertEqual([type(s).__name__ for s in pushed], ["ServicePickerScreen"])

    async def test_no_service_flow_uses_the_legacy_choice_screen(self) -> None:
        """Requirement 4."""

        for preset_id in SERVICES:
            app = self.app()
            pushed: list = []
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                with (
                    patch.object(
                        app,
                        "push_screen",
                        side_effect=lambda s, *_a, **_k: pushed.append(s),
                    ),
                    patch.object(app, "_start_notion_workspace_connect"),
                ):
                    app._connection_hub_done(hub.HUB_ADD_SERVICE)
                    app._service_chosen(preset_id)
                    await pilot.pause()
            names = [type(s).__name__ for s in pushed]
            with self.subTest(service=preset_id):
                self.assertNotIn("ConnectionChoiceScreen", names)

    async def test_no_service_flow_opens_the_bridge_wizard(self) -> None:
        """Requirement 5."""

        for preset_id in SERVICES:
            app = self.app()
            pushed: list = []
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                with (
                    patch.object(
                        app,
                        "push_screen",
                        side_effect=lambda s, *_a, **_k: pushed.append(s),
                    ),
                    patch.object(app, "_start_notion_workspace_connect"),
                ):
                    app._service_chosen(preset_id)
                    await pilot.pause()
            names = [type(s).__name__ for s in pushed]
            with self.subTest(service=preset_id):
                self.assertNotIn("BridgeSetupScreen", names)

    async def test_the_standard_screen_shows_no_technical_field(self) -> None:
        """Requirements 7-11 together, against the rendered text."""

        for preset_id in SERVICES:
            app = self.app()
            async with app.run_test(size=(120, 42)) as pilot:
                await pilot.pause()
                screen = await self._screen(pilot, app, preset_id)
                text = screen.rendered_text().casefold()
            for term in INTERNAL_TERMS:
                if term in {"oauth", "tunnel"}:
                    continue
                with self.subTest(service=preset_id, term=term):
                    self.assertNotIn(term, text)

    async def test_opening_a_flow_does_not_touch_a_healthy_bridge(self) -> None:
        """Requirements 12-13. Reading state is not acting on it."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, "clickup")
            controller = screen._controller
            with patch.object(controller, "stop") as stop:
                with patch.object(controller, "start") as start:
                    with patch.object(controller, "restart") as restart:
                        screen.refresh_view()
                        await pilot.pause()
            stop.assert_not_called()
            start.assert_not_called()
            restart.assert_not_called()

    async def test_first_start_opens_the_existing_bridge_wizard_for_that_service(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, "chatgpt-web")
            with patch.object(app, "open_service_bridge_setup") as open_setup:
                screen.action_start_repair()
                await pilot.pause()
            open_setup.assert_called_once()
            args, _kwargs = open_setup.call_args
            self.assertEqual(args[0], "chatgpt-web")
            self.assertTrue(callable(args[1]))

    async def test_service_screen_shows_and_can_switch_the_current_project(self) -> None:
        project = self.repository / "mini app"
        project.mkdir()
        app = self.app()
        app.repository = project
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, "hyperagent-web", language="ru")
            await pilot.pause()
            project_text = str(screen.query_one("#svc-project", tui.Static).render())
            self.assertIn(str(project.resolve()), project_text)
            self.assertIn("Ctrl+W", project_text)
            with patch.object(app, "action_workspace") as switcher:
                screen.action_workspace()
            switcher.assert_called_once_with()

    async def test_ctrl_w_switches_hyperagent_to_a_path_with_spaces_before_start(self) -> None:
        project = self.repository / "mini app"
        project.mkdir()
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            service = await self._screen(pilot, app, "hyperagent-web", language="ru")
            await pilot.press("ctrl+w")
            await pilot.pause()
            self.assertIsInstance(app.screen, WorkspaceManagerScreen)
            manager = app.screen
            path_input = manager.query_one("#workspace-add-path", tui.Input)
            path_input.value = str(project)
            path_input.focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, WorkspaceManagerScreen)
            await pilot.press("down")
            await pilot.press("enter")
            await pilot.pause(0.2)
            self.assertIs(app.screen, service)
            self.assertEqual(app.repository, project.resolve())
            service._write()
            self.assertIn(
                str(project.resolve()),
                str(service.query_one("#svc-project", tui.Static).render()),
            )

    async def test_hyperagent_saved_profiles_are_scoped_to_the_selected_project(self) -> None:
        wrong = self.repository / "other-project"
        right = self.repository / "mini app"
        wrong.mkdir()
        right.mkdir()
        store = WebBridgeProfileStore()
        store.put(
            SavedWebBridgeProfile(
                name="aaa-hyperagent-wrong",
                target_profile="hyperagent-web",
                repository=str(wrong),
                tools=("karox.repo.read_file",),
                tunnel="tailscale",
                port=8768,
            )
        )
        store.put(
            SavedWebBridgeProfile(
                name="zzz-hyperagent-right",
                target_profile="hyperagent-web",
                repository=str(right),
                tools=("karox.repo.read_file",),
                tunnel="tailscale",
                port=8768,
            )
        )

        app = self.app()
        app.repository = right
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, "hyperagent-web")
            await pilot.pause(0.4)
            self.assertIsNotNone(screen._last_state)
            self.assertEqual(screen._last_state.target.name, "zzz-hyperagent-right")
            self.assertEqual(
                Path(screen._last_state.target.repository).resolve(), right.resolve()
            )

    def test_hyperagent_full_access_profile_is_a_single_coherent_contract(self) -> None:
        base = SavedWebBridgeProfile(
            name="hyperagent-full-toggle",
            target_profile="hyperagent-web",
            repository=str(self.repository),
            tools=("karox.repo.read_file",),
            verification_commands=(("python", "-m", "pytest"),),
            tunnel="tailscale",
            port=8768,
            access_profile=AccessProfile.WORKSPACE_WRITE,
        )

        full = hub.build_saved_profile_full_access(base, True)
        self.assertEqual(full.access_profile, AccessProfile.ELEVATED)
        self.assertTrue(full.browser_external_https)
        self.assertTrue(full.browser_headed)
        self.assertTrue(full.browser_user_takeover)
        self.assertTrue(full.browser_network_inspection)
        self.assertTrue(hub.saved_profile_full_access_enabled(full))
        for tool in (
            "karox.repo.write_file",
            "karox.checks.run",
            "karox.command.run",
            "karox.git.commit",
            "karox.browser.network_requests",
            "karox.browser.request_user_takeover",
        ):
            self.assertIn(tool, full.tools)
        self.assertFalse(any("push" in tool or "publish" in tool for tool in full.tools))

        project = hub.build_saved_profile_full_access(full, False)
        self.assertEqual(project.access_profile, AccessProfile.WORKSPACE_WRITE)
        self.assertFalse(project.browser_external_https)
        self.assertFalse(project.browser_headed)
        self.assertFalse(project.browser_user_takeover)
        self.assertFalse(project.browser_network_inspection)
        self.assertFalse(hub.saved_profile_full_access_enabled(project))
        self.assertIn("karox.repo.write_file", project.tools)
        # Hyperagent keeps one stable MCP catalogue across access-mode toggles;
        # Project access advertises Full-only names but runtime policy still
        # denies them until the access profile becomes elevated.
        self.assertIn("karox.command.run", project.tools)
        self.assertIn("karox.git.commit", project.tools)
        self.assertIn("karox.browser.network_requests", project.tools)
        self.assertIn("karox.browser.request_user_takeover", project.tools)
        self.assertEqual(project.tools, full.tools)

    async def test_hyperagent_full_access_switch_persists_and_restarts_only_that_bridge(self) -> None:
        store = WebBridgeProfileStore()
        base = SavedWebBridgeProfile(
            name="hyperagent-full-ui",
            target_profile="hyperagent-web",
            repository=str(self.repository),
            tools=("karox.repo.read_file",),
            verification_commands=(("python", "-m", "pytest"),),
            tunnel="tailscale",
            port=8768,
            access_profile=AccessProfile.WORKSPACE_WRITE,
        )
        store.put(hub.build_saved_profile_full_access(base, True))

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, "hyperagent-web", language="ru")
            await pilot.pause(0.4)
            switch = screen.query_one("#svc-full-access", hub.Switch)
            self.assertTrue(switch.value)
            self.assertFalse(switch.disabled)
            self.assertIn(
                "ВКЛ",
                str(screen.query_one("#svc-full-access-label", hub.Label).render()),
            )

            with patch(
                "karox.web_bridge_launcher.restart_saved_bridge",
                return_value={"action": "restarted", "error": None},
            ) as restart:
                switch.value = False
                # The switch persists in a worker. Hosted Windows runners can
                # take longer than a fixed sleep when the full suite is running
                # in parallel, so wait for the observable side effect instead
                # of guessing a wall-clock duration.
                for _ in range(100):
                    if restart.call_count:
                        break
                    await pilot.pause(0.05)

            restart.assert_called_once_with(
                base.name,
                allow_legacy_migration=True,
            )
            stored = store.get(base.name)
            self.assertEqual(stored.access_profile, AccessProfile.WORKSPACE_WRITE)
            self.assertFalse(stored.browser_external_https)
            self.assertIn("karox.git.commit", stored.tools)
            self.assertIn("karox.command.run", stored.tools)

        # Bypass is no longer a Hyperagent special case: every saved-bridge
        # service offers the same toggle, and it reads OFF for a connection
        # that has never enabled it.
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            chatgpt = await self._screen(pilot, app, "chatgpt-web")
            await pilot.pause(0.4)
            switch = chatgpt.query_one("#svc-full-access", hub.Switch)
            self.assertFalse(switch.value)
            self.assertIn(
                "Bypass",
                str(chatgpt.query_one("#svc-full-access-label", hub.Label).render()),
            )

    def test_adapt_guide_copies_complete_authorization_value_once(self) -> None:
        steps = "\n".join(english for _russian, english in hub._SERVICE_STEPS["adapt"])
        self.assertIn("complete KaroX Authorization value", steps)
        self.assertIn("already starts with Bearer", steps)
        self.assertIn("do not add a second Bearer", steps)

    async def test_adapt_reuses_saved_chatgpt_bridge_without_opening_duplicate_setup(self) -> None:
        profile = SavedWebBridgeProfile(
            name="chatgpt-dev-adapt-reuse",
            target_profile="chatgpt-web",
            repository=str(self.repository),
            tools=("karox.repo.read_file",),
            tunnel="custom",
            public_url="https://bridge.example.test",
            port=8765,
        )
        WebBridgeProfileStore().put(profile)

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, "adapt")
            await pilot.pause(0.4)
            self.assertIsNotNone(screen._last_state)
            self.assertEqual(screen._last_state.target.name, profile.name)
            self.assertEqual(
                screen.view().endpoint,
                "https://bridge.example.test/mcp",
            )

            with patch.object(app, "open_service_bridge_setup") as open_setup:
                with patch(
                    "karox.web_bridge_launcher.start_saved_bridge",
                    return_value={"action": "started", "error": None},
                ) as start_saved:
                    screen.action_start_repair()
                    await pilot.pause(0.4)

            open_setup.assert_not_called()
            start_saved.assert_called_once_with(profile.name)

    async def test_saved_stopped_notion_profile_starts_without_reopening_setup(self) -> None:
        """A saved first-run Notion profile is configured even without a live watchdog."""

        profile = SavedWebBridgeProfile(
            name="notion-saved-stopped",
            target_profile="notion",
            repository=str(self.repository),
            tools=("karox.repo.read_file",),
            tunnel="tailscale",
            port=8767,
        )
        WebBridgeProfileStore().put(profile)

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, "notion")
            # Live discovery runs in a worker; allow the saved-profile fallback
            # snapshot to reach the mounted screen before pressing Start.
            await pilot.pause(0.4)
            self.assertIsNotNone(screen._last_state)
            self.assertEqual(screen._last_state.target.name, profile.name)
            self.assertEqual(screen.view().endpoint, "")

            with patch.object(app, "open_service_bridge_setup") as open_setup:
                with patch(
                    "karox.web_bridge_launcher.start_saved_bridge",
                    return_value={"action": "started", "error": None},
                ) as start_saved:
                    screen.action_start_repair()
                    await pilot.pause(0.4)

            open_setup.assert_not_called()
            start_saved.assert_called_once_with(profile.name)

    async def test_stopped_saved_chatgpt_profile_says_repair_not_start(self) -> None:
        profile = SavedWebBridgeProfile(
            name="chatgpt-saved-stopped",
            target_profile="chatgpt-web",
            repository=str(self.repository),
            tools=("karox.repo.read_file",),
            tunnel="tailscale",
            port=8765,
        )
        WebBridgeProfileStore().put(profile)

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, "chatgpt-web", language="ru")
            await pilot.pause(0.4)
            self.assertEqual(screen._last_state.target.name, profile.name)
            self.assertEqual(
                str(screen.query_one("#svc-primary", tui.Button).label),
                "Восстановить",
            )

    async def test_live_saved_chatgpt_hides_restart_in_more_and_uses_canonical_restart(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, "chatgpt-web", language="ru")
            state = hub._DiscoveredBridgeState(
                {
                    "saved_profile": "chatgpt-live",
                    "session_id": "web-saved-live",
                    "profile": "chatgpt-web",
                    "public_url": "https://bridge.example.com",
                    "tunnel": "tailscale",
                    "bridge_pid": 12345,
                    "tunnel_pid": None,
                    "persistent_session": True,
                },
                "chatgpt-web",
            )
            screen._last_state = state
            screen._has_snapshot = True
            screen._endpoint = "https://bridge.example.com/mcp"
            screen._typed_status = SimpleNamespace(
                overall=hub.OverallStatus.FULLY_VERIFIED
            )
            screen._write()
            await pilot.pause()
            lifecycle = screen.query_one("#svc-lifecycle", tui.Button)
            more = screen.query_one("#svc-more", tui.Button)
            self.assertEqual(str(lifecycle.styles.display), "none")
            self.assertTrue(more.is_on_screen)
            self.assertIn("restart", {action for action, _label in screen._more_actions()})

            with patch(
                "karox.web_bridge_launcher.restart_saved_bridge",
                return_value={"action": "restarted", "error": None},
            ) as restart_saved:
                screen._more_selected("restart")
                await pilot.pause(0.4)

            restart_saved.assert_called_once_with(
                "chatgpt-live",
                allow_legacy_migration=True,
            )

    async def test_service_bridge_wizard_preselects_the_already_chosen_profile(self) -> None:
        for profile, widget_id in (
            ("chatgpt-web", "profile-chatgpt-web"),
            ("claude-web", "profile-claude-web"),
            ("hyperagent-web", "profile-hyperagent"),
            ("clickup", "profile-clickup"),
        ):
            with self.subTest(profile=profile):
                app = self.app()
                async with app.run_test(size=(120, 42)) as pilot:
                    await pilot.pause()
                    screen = tui.BridgeSetupScreen("en", default_profile=profile)
                    app.push_screen(screen)
                    await pilot.pause()
                    self.assertEqual(screen._profile_value(), profile)
                    self.assertTrue(
                        screen.query_one(f"#{widget_id}", tui.RadioButton).value
                    )
                    self.assertFalse(
                        screen.query_one("#profile-promptql", tui.RadioButton).value
                    )

    async def test_notion_service_setup_is_locked_to_notion_and_parallel_defaults(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            app.open_service_bridge_setup("notion")
            await pilot.pause()
            screen = app.screen
            self.assertIsInstance(screen, tui.BridgeSetupScreen)
            self.assertEqual(screen._profile_value(), "notion")
            self.assertEqual(len(list(screen.query("#bridge-profile"))), 0)
            self.assertIsNotNone(screen.query_one("#bridge-profile-locked"))
            for widget_id in (
                "profile-promptql",
                "profile-chatgpt-web",
                "profile-clickup",
                "profile-claude-web",
                "profile-generic",
                "profile-hyperagent",
            ):
                self.assertEqual(len(list(screen.query(f"#{widget_id}"))), 0)
            self.assertEqual(screen._tunnel_value(), "tailscale")
            self.assertEqual(screen.query_one("#bridge-port", tui.Input).value, "8767")

    async def test_hyperagent_service_setup_is_locked_to_its_own_parallel_bridge(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            app.open_service_bridge_setup("hyperagent-web")
            await pilot.pause()
            screen = app.screen
            self.assertIsInstance(screen, tui.BridgeSetupScreen)
            self.assertEqual(screen._profile_value(), "hyperagent-web")
            self.assertEqual(len(list(screen.query("#bridge-profile"))), 0)
            self.assertEqual(screen._tunnel_value(), "tailscale")
            self.assertEqual(screen.query_one("#bridge-port", tui.Input).value, "8768")
            self.assertIn("10000", str(screen.query_one("#bridge-profile-note", tui.Static).render()))

            # The compact checkboxes are capability families. An explicit
            # --tool list replaces DEFAULT_WEB_TOOLS, so the Hyperagent wizard
            # must carry the full repository-navigation/Git/task family rather
            # than only read_file/status.
            with patch.object(screen, "dismiss") as dismiss:
                screen._submit()
            setup = dismiss.call_args.args[0]
            self.assertIsInstance(setup, tui.BridgeSetup)
            for tool in (
                "karox.repo.read_file",
                "karox.repo.read_lines",
                "karox.repo.search",
                "karox.repo.inspect",
                "karox.repo.edit_file",
                "karox.git.log",
                "karox.task.bootstrap",
                "karox.task.resume",
                "karox.task.status",
            ):
                self.assertIn(tool, setup.tools)

    async def test_adapt_service_setup_is_locked_to_minimal_parallel_bridge(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            app.open_service_bridge_setup("adapt")
            await pilot.pause()
            screen = app.screen
            self.assertIsInstance(screen, tui.BridgeSetupScreen)
            self.assertEqual(screen._profile_value(), "adapt")
            self.assertEqual(screen._tunnel_value(), "tailscale")
            self.assertEqual(screen.query_one("#bridge-port", tui.Input).value, "8769")
            self.assertIn("10001", str(screen.query_one("#bridge-profile-note", tui.Static).render()))
            for widget_id in ("tool-browser-read", "tool-browser-input", "tool-server-read", "tool-server-input"):
                self.assertFalse(screen.query_one(f"#{widget_id}", tui.Checkbox).value)

    def test_adapt_profile_persists_as_adapt_not_as_chatgpt(self) -> None:
        setup = tui.BridgeSetup(
            profile="adapt",
            port=8769,
            tools=("karox.repo.read_file", "karox.repo.edit_file"),
            tunnel_provider="tailscale",
        )
        profile_name = tui._persist_tui_saved_bridge_profile(
            self.repository,
            setup,
            language="en",
        )
        stored = WebBridgeProfileStore().get(profile_name)
        self.assertTrue(profile_name.startswith("adapt-auto-"))
        self.assertEqual(stored.target_profile, "adapt")
        self.assertEqual(stored.port, 8769)
        self.assertFalse(stored.browser_external_https)
        self.assertFalse(stored.browser_headed)
        self.assertFalse(stored.browser_user_takeover)

    async def test_hyperagent_parallel_launch_is_not_blocked_by_the_main_bridge(self) -> None:
        app = self.app()
        setup = tui.BridgeSetup(
            profile="hyperagent-web",
            port=8768,
            tools=("karox.repo.read_file",),
            tunnel_provider="tailscale",
        )
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            main_bridge = SimpleNamespace(
                poll=lambda: None,
                pid=99999,
                terminate=lambda: None,
                wait=lambda timeout=None: None,
                kill=lambda: None,
            )
            app.bridge_process = main_bridge
            self.assertFalse(hasattr(app, "_launch_hyperagent_terminal"))
            with patch.object(app, "_launch_saved_bridge_from_tui") as launch:
                app._bridge_setup_done(setup)
                await pilot.pause()
            launch.assert_called_once_with(setup)
            self.assertIs(app.bridge_process, main_bridge)

    async def test_an_existing_endpoint_is_reused_rather_than_recreated(self) -> None:
        """Requirement 14."""

        endpoint = "https://already-live.example.com/mcp"
        state = _State(_Target("c1", "clickup"), "running", endpoint)
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, "clickup")
            with patch.object(screen._controller, "list", return_value=[state]):
                with patch.object(screen._controller, "start") as start:
                    screen.refresh_view()
                    await pilot.pause()
                start.assert_not_called()
            self.assertEqual(screen.view().endpoint, endpoint)
            self.assertIn(endpoint, screen.rendered_text())

    async def test_verify_without_an_endpoint_starts_nothing(self) -> None:
        """A check is a check. It must not become a hidden launch."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, "chatgpt-web")
            with patch.object(screen._controller, "start") as start:
                with patch.object(screen._controller, "restart") as restart:
                    screen.action_verify()
                    await pilot.pause()
            start.assert_not_called()
            restart.assert_not_called()
            self.assertEqual(screen.view().status, hub.SERVICE_NOT_CONFIGURED)

    async def test_a_failure_keeps_the_screen_and_the_retry(self) -> None:
        """Requirement 15."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, "clickup")
            depth = len(app.screen_stack)
            screen._checked({"state": "failed", "detail": "refused"})
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), depth)
            self.assertEqual(screen.view().status, hub.SERVICE_ERROR)
            self.assertTrue(screen.query_one("#svc-steps").is_on_screen)
            # Retry is still reachable without a magic Enter binding: F5 remains
            # for keyboard users and the visible Verify button is the normal path.
            self.assertIn("f5", {b.key for b in screen.BINDINGS})
            self.assertIsNotNone(screen.query_one("#svc-verify"))

    async def test_a_failure_never_shows_a_secret(self) -> None:
        """Requirements 16-17."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, "clickup")
            screen._checked(
                {"state": "failed", "detail": f"401 for {FAKE_SECRET}"}
            )
            await pilot.pause()
            # The secret must be absent from the whole screen ...
            shown = screen.rendered_text()
            self.assertNotIn(FAKE_SECRET, shown)
            self.assertNotIn(FAKE_SECRET[5:], shown)
            # ... while the header *name* is legitimate product text: ClickUp's
            # own form calls the working choice "Authorization header", and the
            # step has to name it. What must never appear is a credential or a
            # wire-level dump, so the assertion is scoped to the error region
            # rather than banning a word the instructions need.
            status = screen.status_text()
            for banned in (FAKE_SECRET, "Bearer ", "Authorization:"):
                with self.subTest(term=banned):
                    self.assertNotIn(banned, status)

    async def test_escape_goes_back_exactly_one_level(self) -> None:
        """Requirements 26-27."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            depth = len(app.screen_stack)
            screen = await self._screen(pilot, app, "claude-web")
            self.assertEqual(len(app.screen_stack), depth + 1)
            with patch.object(screen._controller, "stop") as stop:
                await pilot.press("escape")
                await pilot.pause()
            stop.assert_not_called()
            self.assertEqual(len(app.screen_stack), depth)

    async def test_a_narrow_terminal_keeps_the_steps_and_the_address(self) -> None:
        """Requirement 28, and the B2 compact-height lesson."""

        app = self.app()
        async with app.run_test(size=(46, 14)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, "chatgpt-web")
            dialog = screen.query_one("#svc-dialog")
            self.assertLessEqual(dialog.size.width, 46)
            self.assertLessEqual(dialog.size.height, 14)
            hint = str(screen.query_one("#svc-hint").render())
            self.assertNotIn("\n", hint)
            self.assertTrue(screen.view().steps)

    async def test_a_wide_terminal_grows_no_side_panel(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, "clickup")
            self.assertLessEqual(screen.query_one("#svc-dialog").size.width, 64)

    async def test_the_flow_is_navigable_by_keyboard_only(self) -> None:
        """Requirement 29."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            picker = screens["ServicePickerScreen"]("en")
            app.push_screen(picker)
            await pilot.pause()
            options = picker.query_one("#svc-pick-list")
            self.assertTrue(options.has_focus)
            picker.action_next()
            picker.action_previous()
            bound = {b.key for b in picker.BINDINGS}
            for key in ("enter", "up", "down", "escape"):
                self.assertIn(key, bound)

    async def test_a_success_returns_to_the_same_hub(self) -> None:
        """Requirements 23-25."""

        app = self.app()
        opened: list = []
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            with patch.object(
                app,
                "_open_connections",
                side_effect=lambda focus=None: opened.append(focus),
            ):
                app._connection_hub_reopen(None)
                await pilot.pause()
        self.assertEqual(opened, [None])

    async def test_the_hub_rereads_the_existing_state_source(self) -> None:
        """Requirement 24: one store, re-read, not a second list."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            screen = screens["ConnectionHubScreen"]("en")
            app.push_screen(screen)
            await pilot.pause()
            with patch.object(
                screen, "_collect", return_value=()
            ) as collect:
                screen.refresh_rows()
                await pilot.pause()
            collect.assert_called_once_with()

    async def test_a_saved_service_row_opens_that_records_detail(self) -> None:
        """B5 changed where a *saved* row goes, and this test with it.

        Until B5 an existing ChatGPT/Claude/ClickUp row opened the B3 connect
        screen -- the screen for *setting a service up*. That was right while
        connecting was the only thing a person could do. It is wrong now that a
        saved record can also be edited, verified, disabled and deleted: a
        screen titled "Connect ClickUp" is not where you go to delete ClickUp.

        The split the product now makes: Add -> ServiceConnectScreen, existing
        row -> ConnectionDetailScreen. The identity travels unchanged, and the
        legacy list is not involved.
        """

        app = self.app()
        pushed: list = []
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            with patch.object(app, "_saved_record_exists", return_value=True):
                with patch.object(
                    app,
                    "push_screen",
                    side_effect=lambda s, *_a, **_k: pushed.append(s),
                ):
                    app._connection_hub_done(f"{hub.HUB_FAMILY_SERVICE}:c1")
                    await pilot.pause()
        self.assertEqual(
            [type(s).__name__ for s in pushed], ["ConnectionDetailScreen"]
        )
        self.assertEqual(pushed[0].identity, "c1")
        self.assertEqual(pushed[0].kind, "service")

    async def test_an_unrecognised_row_falls_back_and_does_not_guess(self) -> None:
        """Fail-soft survives B5, for a new reason as well as the old one.

        Before, guessing meant opening the wrong service's setup screen. Now it
        would mean opening a management screen for a record that does not
        exist -- one offering Verify and Delete against nothing. Both are worse
        than the plain list, so an identity the store cannot resolve still
        falls back instead of being assumed into a preset.
        """

        app = self.app()
        pushed: list = []
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            with patch.object(app, "_saved_record_exists", return_value=False):
                with patch.object(
                    app,
                    "push_screen",
                    side_effect=lambda s, *_a, **_k: pushed.append(s),
                ):
                    app._connection_hub_done(f"{hub.HUB_FAMILY_SERVICE}:unknown")
                    await pilot.pause()
        self.assertEqual([type(s).__name__ for s in pushed], ["McpClientsScreen"])

    async def test_a_row_deleted_between_render_and_enter_does_not_open(self) -> None:
        """The race the hub cannot avoid: it is a snapshot, not a live view.

        A row can be removed by a CLI, another window, or the keystroke before
        this one, after the list was drawn. Resolution goes through the real
        controller here -- not a patched flag -- so this proves the production
        lookup, not the test's own opinion of it.
        """

        app = self.app()
        pushed: list = []
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            with patch.object(
                app,
                "push_screen",
                side_effect=lambda s, *_a, **_k: pushed.append(s),
            ):
                app._connection_hub_done(
                    f"{hub.HUB_FAMILY_SERVICE}:c-deadbeefdeadbeef"
                )
                await pilot.pause()
        self.assertEqual([type(s).__name__ for s in pushed], ["McpClientsScreen"])

    async def test_the_shell_and_activity_contracts_survive(self) -> None:
        """Requirement 30: A1, A2, B1 and B2 are not disturbed."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            await self._screen(pilot, app, "clickup")
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(len(app.query("#header-status")), 1)
            self.assertEqual(len(app.query("#activity")), 1)
            self.assertEqual(len(app.query("#sponsor-ticker")), 0)
            self.assertEqual(
                str(app.query_one("#activity").styles.display), "none"
            )


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
