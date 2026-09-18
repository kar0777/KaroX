"""One advanced layer, opened on purpose, applying nothing until Save.

B4. The standard flows stay simple; Advanced settings is the one place the
technical facts live, and it behaves the same way for an AI provider and for a
service. These tests pin the rules that make it one layer rather than several
old wizards wearing a shared label:

* progressive disclosure -- the standard flow never shows it, F2 opens it;
* defaults come from the production preset, and Reset returns to them;
* every field offered is a field the flow can actually apply;
* permission modes are three human names over the existing `AccessProfile`,
  and tool names appear only in the technical detail behind them;
* a stored secret is never rendered back, and an empty box never deletes it;
* validation keeps the values and changes nothing;
* Cancel applies nothing, Save applies once;
* a change that would move a live address asks first.

The view model is pure, so most of this runs without a terminal. The screen
behaviour drives the real application through production callbacks.
"""

from __future__ import annotations

from _unittest_compat import enter_context

import unittest
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from _tui_harness import isolated_karox_directories

from karox import tui
from karox import tui_connections as hub
from karox.models import AccessProfile
from karox.policy import _PROFILE_CAPABILITIES

SERVICES = ("chatgpt-web", "claude-web", "clickup")

# Fields that belong to an AI provider and would be meaningless on a service.
PROVIDER_ONLY = ("base_url", "adapter", "context_window", "max_output")

# A token-shaped fixture, assembled at runtime so the write path cannot redact
# the literal and leave an assertion comparing a marker with a marker.
FAKE_SECRET = "sk-live-" + ("4" * 32)


class AdvancedContractTests(unittest.TestCase):
    """One set of rules, shared by both kinds."""

    def test_both_kinds_offer_the_same_groups_vocabulary(self) -> None:
        for kind, preset in (("provider", "openai"), ("service", "clickup")):
            with self.subTest(kind=kind):
                groups = hub.advanced_visible_groups(kind, preset)
                self.assertIn(hub.ADVANCED_GROUP_CONNECTION, groups)
                self.assertIn(hub.ADVANCED_GROUP_PERMISSIONS, groups)
                for group in groups:
                    self.assertIn(group, hub.ADVANCED_GROUPS)

    def test_an_empty_group_is_not_offered(self) -> None:
        """A service has no limits fields, so it gets no Limits heading."""

        groups = hub.advanced_visible_groups("service", "clickup")
        self.assertNotIn(hub.ADVANCED_GROUP_LIMITS, groups)
        self.assertIn(
            hub.ADVANCED_GROUP_LIMITS,
            hub.advanced_visible_groups("provider", "openai"),
        )

    def test_every_group_has_both_languages(self) -> None:
        for group in hub.ADVANCED_GROUPS:
            with self.subTest(group=group):
                self.assertTrue(hub.advanced_group_words(group, True).strip())
                self.assertTrue(hub.advanced_group_words(group, False).strip())

    def test_every_field_has_both_languages(self) -> None:
        for kind, preset in (("provider", "openai"), ("service", "clickup")):
            for field in hub.advanced_fields(kind, preset):
                with self.subTest(kind=kind, field=field.field_id):
                    self.assertTrue(field.label(True).strip())
                    self.assertTrue(field.label(False).strip())


class AdvancedDefaultsTests(unittest.TestCase):
    """Requirements 7-8: the preset is the source, and Reset returns to it."""

    def test_provider_defaults_come_from_the_production_preset(self) -> None:
        from karox.provider_presets import provider_preset

        preset = provider_preset("openai")
        defaults = hub.advanced_defaults("provider", "openai")
        self.assertEqual(defaults["base_url"], str(preset.base_url or ""))
        self.assertEqual(defaults["adapter"], str(preset.adapter_kind or ""))

    def test_service_defaults_come_from_the_production_preset(self) -> None:
        from karox.connections import MCP_CLIENT_PRESETS

        for preset_id in SERVICES:
            preset = MCP_CLIENT_PRESETS[preset_id]
            defaults = hub.advanced_defaults("service", preset_id)
            with self.subTest(service=preset_id):
                self.assertEqual(defaults["transport"], preset.transport)
                self.assertEqual(defaults["tunnel"], preset.tunnel_default)
                self.assertEqual(defaults["endpoint_path"], preset.endpoint_path)

    def test_a_secret_field_has_no_default(self) -> None:
        """There is nothing to prefill, and prefilling would mean rendering it."""

        for field in hub.advanced_fields("provider", "openai"):
            if field.secret:
                self.assertEqual(field.default, "")
        self.assertNotIn("api_key", hub.advanced_defaults("provider", "openai"))


class AdvancedApplicabilityTests(unittest.TestCase):
    """Requirements 9-12: never offer a control the flow cannot apply."""

    def test_a_service_is_not_offered_provider_only_fields(self) -> None:
        for preset_id in SERVICES:
            ids = {f.field_id for f in hub.advanced_fields("service", preset_id)}
            for banned in PROVIDER_ONLY:
                with self.subTest(service=preset_id, field=banned):
                    self.assertNotIn(banned, ids)

    def test_a_provider_is_not_offered_service_only_fields(self) -> None:
        ids = {f.field_id for f in hub.advanced_fields("provider", "openai")}
        for banned in ("tunnel", "port", "endpoint_path", "transport"):
            with self.subTest(field=banned):
                self.assertNotIn(banned, ids)

    def test_a_service_is_offered_its_applicable_fields(self) -> None:
        ids = {f.field_id for f in hub.advanced_fields("service", "clickup")}
        for expected in ("connection_name", "transport", "port", "tunnel"):
            with self.subTest(field=expected):
                self.assertIn(expected, ids)

    def test_a_provider_is_offered_its_applicable_fields(self) -> None:
        ids = {f.field_id for f in hub.advanced_fields("provider", "openai")}
        for expected in ("connection_name", "base_url", "timeout", "api_key"):
            with self.subTest(field=expected):
                self.assertIn(expected, ids)


class PermissionModeTests(unittest.TestCase):
    """Requirements 13-16: three names over the profiles that already exist."""

    def test_the_modes_are_human_names_in_both_languages(self) -> None:
        for mode in hub.PERMISSION_MODES:
            with self.subTest(mode=mode):
                english = hub.permission_mode_words(mode, True)
                russian = hub.permission_mode_words(mode, False)
                self.assertTrue(english.strip())
                self.assertTrue(russian.strip())
                self.assertNotEqual(english, russian)
                self.assertNotIn("_", english)

    def test_each_mode_maps_onto_an_existing_access_profile(self) -> None:
        """No new permission architecture: every mode is a profile that exists."""

        for mode in hub.PERMISSION_MODES:
            with self.subTest(mode=mode):
                profile = hub.permission_mode_profile(mode)
                self.assertIsInstance(profile, AccessProfile)
                self.assertIn(profile, _PROFILE_CAPABILITIES)

    def test_the_three_modes_are_distinct_profiles(self) -> None:
        profiles = {hub.permission_mode_profile(m) for m in hub.PERMISSION_MODES}
        self.assertEqual(len(profiles), 3)

    def test_a_saved_profile_is_displayed_as_a_mode(self) -> None:
        self.assertEqual(
            hub.permission_mode_for_profile(AccessProfile.READ_ONLY),
            hub.PERMISSION_READ_ONLY,
        )
        self.assertEqual(
            hub.permission_mode_for_profile(AccessProfile.WORKSPACE_WRITE),
            hub.PERMISSION_PROJECT,
        )
        self.assertEqual(
            hub.permission_mode_for_profile(AccessProfile.ELEVATED),
            hub.PERMISSION_EXTENDED,
        )

    def test_a_profile_outside_the_three_modes_still_displays(self) -> None:
        """`BROWSER_CONTROL` exists but is not one of the offered choices.

        It must still display honestly: its own label and its own capability
        list, never the repo-write list of Project access.
        """

        mode = hub.permission_mode_for_profile(AccessProfile.BROWSER_CONTROL)
        self.assertNotIn(mode, hub.PERMISSION_MODES)
        self.assertEqual(mode, hub.PERMISSION_BROWSER)
        self.assertIs(hub.permission_mode_profile(mode), AccessProfile.BROWSER_CONTROL)

    def test_each_mode_has_one_short_summary_and_no_tool_list(self) -> None:
        for mode in hub.PERMISSION_MODES:
            for english in (True, False):
                summary = hub.permission_mode_summary(mode, english)
                with self.subTest(mode=mode, english=english):
                    self.assertTrue(summary.strip())
                    self.assertNotIn("\n", summary)
                    for tool in ("repo.read", "repo.write", "checks.run", "git."):
                        self.assertNotIn(tool, summary)

    def test_the_exact_allowlist_comes_from_the_policy_layer(self) -> None:
        """Computed, not restated: a hand-written list would drift from policy."""

        for mode in hub.PERMISSION_MODES:
            expected = {
                str(c.value)
                for c in _PROFILE_CAPABILITIES[hub.permission_mode_profile(mode)]
            }
            with self.subTest(mode=mode):
                self.assertEqual(
                    set(hub.permission_mode_capabilities(mode)), expected
                )


class AdvancedValidationTests(unittest.TestCase):
    """Requirement 19-20, at the level that decides them."""

    def test_a_valid_form_reports_no_error(self) -> None:
        field, message = hub.validate_advanced(
            "service",
            "clickup",
            {"connection_name": "My ClickUp", "port": "8765"},
            True,
        )
        self.assertEqual((field, message), ("", ""))

    def test_a_bad_port_names_the_field(self) -> None:
        for bad in ("nope", "0", "70000", "-1"):
            with self.subTest(port=bad):
                field, message = hub.validate_advanced(
                    "service", "clickup", {"port": bad}, True
                )
                self.assertEqual(field, "port")
                self.assertTrue(message.strip())

    def test_a_bad_base_url_names_the_field(self) -> None:
        field, message = hub.validate_advanced(
            "provider", "openai", {"base_url": "api.example.com"}, True
        )
        self.assertEqual(field, "base_url")
        self.assertIn("http", message)

    def test_a_bad_timeout_names_the_field(self) -> None:
        field, _message = hub.validate_advanced(
            "provider", "openai", {"timeout": "soon"}, True
        )
        self.assertEqual(field, "timeout")

    def test_validation_messages_exist_in_both_languages(self) -> None:
        english = hub.validate_advanced("service", "clickup", {"port": "x"}, True)[1]
        russian = hub.validate_advanced("service", "clickup", {"port": "x"}, False)[1]
        self.assertTrue(english.strip())
        self.assertTrue(russian.strip())
        self.assertNotEqual(english, russian)


class AdvancedChangeTests(unittest.TestCase):
    """Empty means default, and only real changes count."""

    def test_an_empty_optional_field_is_not_a_change(self) -> None:
        """Clearing a box must not overwrite a working value with nothing."""

        changed = hub.advanced_changed_fields(
            {"port": "8765", "tunnel": "cloudflare"},
            {"port": "", "tunnel": "cloudflare"},
        )
        self.assertEqual(changed, ())

    def test_a_real_edit_is_a_change(self) -> None:
        changed = hub.advanced_changed_fields(
            {"port": "8765"}, {"port": "9000"}
        )
        self.assertEqual(changed, ("port",))

    def test_transport_level_edits_require_a_restart(self) -> None:
        for field in ("transport", "port", "tunnel", "endpoint_path"):
            with self.subTest(field=field):
                self.assertTrue(hub.advanced_requires_restart((field,)))

    def test_a_cosmetic_edit_does_not_require_a_restart(self) -> None:
        self.assertFalse(hub.advanced_requires_restart(("connection_name",)))
        self.assertFalse(hub.advanced_requires_restart(()))


class SecretDisplayTests(unittest.TestCase):
    """Requirement 17: a stored secret is a fact, never a value."""

    def test_a_stored_secret_is_reported_as_saved(self) -> None:
        self.assertEqual(hub.secret_display(True, True), "saved")
        self.assertTrue(hub.secret_display(True, False).strip())

    def test_an_absent_secret_says_so(self) -> None:
        self.assertEqual(hub.secret_display(False, True), "not set")

    def test_the_display_never_carries_a_value(self) -> None:
        for english in (True, False):
            shown = hub.secret_display(True, english)
            self.assertNotIn(FAKE_SECRET, shown)
            self.assertLess(len(shown), 24)


class AdvancedScreenTests(unittest.IsolatedAsyncioTestCase):
    """The running screen: disclosure, apply-once, restart confirmation, Esc."""

    def setUp(self) -> None:
        self.repository = enter_context(self, isolated_karox_directories())
        enter_context(self, patch.object(tui, "_load_language", return_value="en"))
        enter_context(self, patch.object(tui, "_selected_model", return_value=None))

    def app(self) -> tui.KaroXApp:
        return tui.KaroXApp(self.repository, language="en")

    async def _advanced(self, pilot, app, **kwargs):
        screens = app._connections_screens_cached()
        screen = screens["AdvancedSettingsScreen"]("en", **kwargs)
        app.push_screen(screen)
        await pilot.pause()
        return screen

    async def test_the_service_standard_flow_hides_advanced_behind_a_key(self) -> None:
        """Requirements 1-2: never shown, always one deliberate press away."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            service = screens["ServiceConnectScreen"]("en", preset_id="clickup")
            app.push_screen(service)
            await pilot.pause()
            # Nothing technical on the standard screen ...
            text = service.rendered_text().casefold()
            for term in ("8765", "cloudflare", "streamable_http"):
                self.assertNotIn(term, text)
            # ... and F2 is bound to reach it.
            self.assertIn("f2", {b.key for b in service.BINDINGS})
            self.assertIn("f3", {b.key for b in service.BINDINGS})

    async def test_f2_routes_unconfigured_service_to_real_setup_advanced(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            service = screens["ServiceConnectScreen"]("en", preset_id="clickup")
            app.push_screen(service)
            await pilot.pause()
            with patch.object(app, "open_service_bridge_setup") as open_setup:
                service.action_advanced()
                await pilot.pause()
        open_setup.assert_called_once()
        self.assertEqual(open_setup.call_args.args[0], "clickup")
        self.assertTrue(callable(open_setup.call_args.args[1]))

    async def test_advanced_shows_the_base_url_and_connection_name(self) -> None:
        """Requirements 3-4."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._advanced(
                pilot, app, kind="provider", preset_id="openai"
            )
            values = screen.values()
            self.assertIn("base_url", values)
            self.assertIn("connection_name", values)
            self.assertTrue(values["base_url"])

    async def test_limits_live_in_the_same_structure(self) -> None:
        """Requirements 5-6: one layer, not a separate random entry point."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._advanced(
                pilot, app, kind="provider", preset_id="openai"
            )
            self.assertIn(hub.ADVANCED_GROUP_LIMITS, screen.groups())
            limits = {
                f.field_id
                for f in screen.fields()
                if f.group == hub.ADVANCED_GROUP_LIMITS
            }
            self.assertIn("context_window", limits)
            self.assertIn("max_output", limits)

    async def test_reset_returns_the_production_defaults(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._advanced(
                pilot, app, kind="service", preset_id="clickup"
            )
            screen.query_one("#adv-port", tui.Input).value = "9999"
            screen.query_one("#adv-transport", tui.Input).value = "nonsense"
            await pilot.pause()
            screen.action_reset()
            await pilot.pause()
            defaults = hub.advanced_defaults("service", "clickup")
            self.assertEqual(
                screen.query_one("#adv-transport", tui.Input).value,
                defaults["transport"],
            )

    async def test_a_saved_secret_is_never_rendered_back(self) -> None:
        """Requirement 17."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._advanced(
                pilot, app, kind="provider", preset_id="openai", has_secret=True
            )
            shown = screen.rendered_text()
            self.assertIn("saved", shown)
            self.assertNotIn(FAKE_SECRET, shown)
            # The replacement box is empty and masked.
            key_input = screen.query_one("#adv-api_key", tui.Input)
            self.assertEqual(key_input.value, "")
            self.assertTrue(key_input.password)

    async def test_an_empty_secret_box_is_not_a_deletion(self) -> None:
        """Requirement 18. Deleting a credential is B5, not an accident here."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._advanced(
                pilot, app, kind="provider", preset_id="openai", has_secret=True
            )
            screen.action_save()
            await pilot.pause()
            self.assertIsNotNone(screen.applied)
            # The secret is not part of what Save carries when left blank.
            self.assertNotIn("api_key", screen.applied or {})

    async def test_a_validation_error_keeps_the_values_and_applies_nothing(
        self,
    ) -> None:
        """Requirements 19-20."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._advanced(
                pilot, app, kind="service", preset_id="clickup"
            )
            screen.query_one("#adv-port", tui.Input).value = "not-a-port"
            screen.query_one("#adv-connection_name", tui.Input).value = "Keep me"
            await pilot.pause()
            depth = len(app.screen_stack)

            screen.action_save()
            await pilot.pause()

            self.assertIsNone(screen.applied)
            self.assertEqual(len(app.screen_stack), depth)
            self.assertEqual(
                screen.query_one("#adv-port", tui.Input).value, "not-a-port"
            )
            self.assertEqual(
                screen.query_one("#adv-connection_name", tui.Input).value, "Keep me"
            )
            self.assertTrue(screen.error_text().strip())

    async def test_cancel_applies_nothing(self) -> None:
        """Requirement 21."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._advanced(
                pilot, app, kind="service", preset_id="clickup"
            )
            screen.query_one("#adv-port", tui.Input).value = "9000"
            await pilot.pause()
            screen.action_cancel()
            await pilot.pause()
            self.assertIsNone(screen.applied)

    async def test_save_applies_exactly_once(self) -> None:
        """Requirement 22. A double press must not write twice."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._advanced(
                pilot, app, kind="service", preset_id="clickup"
            )
            screen.action_save()
            first = screen.applied
            screen.action_save()
            await pilot.pause()
            self.assertIsNotNone(first)
            self.assertIs(screen.applied, first)

    async def test_opening_advanced_does_not_touch_a_healthy_bridge(self) -> None:
        """Requirement 23."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            service = screens["ServiceConnectScreen"]("en", preset_id="clickup")
            app.push_screen(service)
            await pilot.pause()
            controller = service._controller
            with patch.object(controller, "stop") as stop:
                with patch.object(controller, "restart") as restart:
                    with patch.object(controller, "start") as start:
                        service.action_advanced()
                        await pilot.pause()
            stop.assert_not_called()
            restart.assert_not_called()
            start.assert_not_called()

    async def test_a_cosmetic_change_saves_without_a_restart_prompt(self) -> None:
        """Requirement 24."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._advanced(
                pilot,
                app,
                kind="service",
                preset_id="clickup",
                bridge_running=True,
            )
            screen.query_one("#adv-connection_name", tui.Input).value = "Renamed"
            await pilot.pause()
            depth = len(app.screen_stack)
            screen.action_save()
            await pilot.pause()
            # Applied directly: no confirmation screen was pushed.
            self.assertIsNotNone(screen.applied)
            self.assertLessEqual(len(app.screen_stack), depth)

    async def test_a_restart_required_change_asks_first(self) -> None:
        """Requirement 25."""

        app = self.app()
        pushed: list = []
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._advanced(
                pilot,
                app,
                kind="service",
                preset_id="clickup",
                bridge_running=True,
            )
            screen.query_one("#adv-port", tui.Input).value = "9100"
            await pilot.pause()
            with patch.object(
                app, "push_screen", side_effect=lambda s, *_a, **_k: pushed.append(s)
            ):
                screen.action_save()
                await pilot.pause()
            # Nothing applied yet, and a confirmation was raised.
            self.assertIsNone(screen.applied)
            self.assertEqual(len(pushed), 1)

    async def test_declining_the_restart_leaves_everything_running(self) -> None:
        """Requirement 26."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._advanced(
                pilot,
                app,
                kind="service",
                preset_id="clickup",
                bridge_running=True,
            )
            screen._restart_confirmed(None)
            await pilot.pause()
            self.assertIsNone(screen.applied)
            self.assertTrue(screen.error_text().strip())

    async def test_accepting_the_restart_applies_once(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._advanced(
                pilot,
                app,
                kind="service",
                preset_id="clickup",
                bridge_running=True,
            )
            screen._restart_confirmed(True)
            await pilot.pause()
            self.assertIsNotNone(screen.applied)

    async def test_escape_returns_to_the_standard_flow(self) -> None:
        """Requirement 27, and it must not close KaroX from a nested screen."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            service = screens["ServiceConnectScreen"]("en", preset_id="clickup")
            app.push_screen(service)
            await pilot.pause()
            depth = len(app.screen_stack)
            await self._advanced(pilot, app, kind="service", preset_id="clickup")
            self.assertEqual(len(app.screen_stack), depth + 1)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), depth)
            self.assertIs(app.screen_stack[-1], service)

    async def test_the_technical_allowlist_is_only_in_the_detail_screen(self) -> None:
        """Requirements 15-16."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._advanced(
                pilot, app, kind="service", preset_id="clickup"
            )
            # The advanced screen itself names the mode, not the tools.
            summary = screen.rendered_text()
            self.assertIn("Project access", summary)
            self.assertNotIn("repo.write", summary)

            screens = app._connections_screens_cached()
            detail = screens["PermissionDetailScreen"](
                "en", mode=hub.PERMISSION_PROJECT
            )
            app.push_screen(detail)
            await pilot.pause()
            self.assertIn("repo.write", detail.body_text())

    async def test_a_narrow_terminal_keeps_advanced_usable(self) -> None:
        """Requirement 28, and the B2 compact-height guarantee."""

        app = self.app()
        async with app.run_test(size=(46, 14)) as pilot:
            await pilot.pause()
            screen = await self._advanced(
                pilot, app, kind="service", preset_id="clickup"
            )
            dialog = screen.query_one("#adv-dialog")
            self.assertLessEqual(dialog.size.width, 46)
            self.assertLessEqual(dialog.size.height, 14)
            hint = str(screen.query_one("#adv-hint").render())
            self.assertNotIn("\n", hint)
            # At least one editable field is present and reachable.
            self.assertTrue(screen.values())

    async def test_a_wide_terminal_grows_no_dashboard(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            screen = await self._advanced(
                pilot, app, kind="provider", preset_id="openai"
            )
            self.assertLessEqual(screen.query_one("#adv-dialog").size.width, 72)

    async def test_the_keyboard_reaches_every_action(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._advanced(
                pilot, app, kind="service", preset_id="clickup"
            )
            bound = {b.key for b in screen.BINDINGS}
            for key in ("ctrl+s", "f4", "f6", "escape"):
                with self.subTest(key=key):
                    self.assertIn(key, bound)

    async def test_russian_renders_without_falling_back_to_english(self) -> None:
        """Requirement 29."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            screen = screens["AdvancedSettingsScreen"](
                "ru", kind="service", preset_id="clickup"
            )
            app.push_screen(screen)
            await pilot.pause()
            text = screen.rendered_text()
            self.assertIn("Работа с проектом", text)
            self.assertNotIn("Project access", text)

    async def test_the_shell_and_earlier_slices_survive(self) -> None:
        """Requirement 30."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            await self._advanced(pilot, app, kind="service", preset_id="clickup")
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
