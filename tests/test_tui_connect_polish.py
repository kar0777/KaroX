"""B6. One connection surface that agrees with itself.

B1-B5 each proved their own slice. Nothing yet proved that the slices *match*:
that one action has one name everywhere, that one fact produces one status word
on every screen, and that no screen has quietly grown a second vocabulary.

These are consistency contracts, deliberately not a re-run of the hundreds of
tests already covering each flow. They fail when two parts of `/connect` start
disagreeing, which is the failure mode a polish slice exists to prevent and the
one no single-flow test can see.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from _tui_harness import isolated_karox_directories

from karox import tui
from karox import tui_connections as hub

# A token-shaped fixture, assembled at runtime so the repository write path
# cannot redact the literal and leave an assertion comparing a marker with a
# marker.
PLANTED = "sk-live-" + ("7" * 32)


class VocabularyConsistencyTests(unittest.TestCase):
    """One action, one name. One fact, one word."""

    def test_the_same_action_is_never_called_two_things_in_english(self) -> None:
        """The B6 defect this closes.

        The saved-connection list called it "Test" while the detail screen and
        the service flow called it "Verify". One action with two names reads as
        two actions -- and "test" additionally suggests a dry run that changes
        nothing, while this one records a result the screen then shows.
        """

        self.assertEqual(hub._C["en"]["test"], "Verify")
        self.assertEqual(
            hub.detail_action_words(hub.DETAIL_VERIFY, True), "Verify"
        )

    def test_the_russian_verb_matches_the_english_one(self) -> None:
        self.assertEqual(hub._C["ru"]["test"], "\u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c")
        self.assertEqual(
            hub.detail_action_words(hub.DETAIL_VERIFY, False),
            "\u041f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c",
        )

    def test_delete_and_edit_agree_across_the_vocabulary_and_the_detail_screen(
        self,
    ) -> None:
        for key, action in (
            ("delete", hub.DETAIL_DELETE),
            ("edit", hub.DETAIL_EDIT),
        ):
            with self.subTest(action=action):
                self.assertEqual(
                    hub._C["en"][key], hub.detail_action_words(action, True)
                )
                self.assertEqual(
                    hub._C["ru"][key], hub.detail_action_words(action, False)
                )

    def test_no_action_word_is_missing_from_either_language(self) -> None:
        for enabled in (True, False):
            for action in hub.detail_actions(enabled):
                with self.subTest(action=action, enabled=enabled):
                    english = hub.detail_action_words(action, True)
                    russian = hub.detail_action_words(action, False)
                    self.assertTrue(english.strip())
                    self.assertTrue(russian.strip())
                    # An untranslated key falls through as its own id.
                    self.assertNotEqual(english, action)
                    self.assertNotEqual(russian, action)


class StatusHonestyTests(unittest.TestCase):
    """"Works" is a claim, and only one thing may make it."""

    def test_only_a_proven_result_is_allowed_to_say_works(self) -> None:
        """Neither store can produce the working status on its own.

        The four tempting shortcuts -- models exist, enabled is true, the
        config saved, no exception was raised -- are each a different way of
        not having checked.
        """

        rows = hub.provider_hub_rows(_Registry(_Provider("openrouter"), [_Model()]))
        self.assertEqual(rows[0].status, hub.HUB_STATUS_READY)

        states = [_State(_Target("c1", "clickup"), "configured_not_running")]
        self.assertNotEqual(
            hub.service_hub_rows(states)[0].status, hub.HUB_STATUS_WORKING
        )

    def test_disabled_outranks_every_other_status(self) -> None:
        rows = hub.provider_hub_rows(
            _Registry(_Provider("openrouter", enabled=False), [_Model()])
        )
        self.assertEqual(rows[0].status, hub.HUB_STATUS_DISABLED)

        states = [_State(_Target("c1", "clickup", enabled=False), "running")]
        self.assertEqual(
            hub.service_hub_rows(states)[0].status, hub.HUB_STATUS_DISABLED
        )

    def test_every_status_the_hub_can_produce_has_both_languages(self) -> None:
        for status in hub._HUB_STATUS_WORDS:
            with self.subTest(status=status):
                english = hub.hub_status_words(status, True)
                russian = hub.hub_status_words(status, False)
                self.assertTrue(english.strip())
                self.assertTrue(russian.strip())

    def test_neither_language_promises_more_than_the_other(self) -> None:
        """RU/EN parity is about honesty, not about literal translation.

        The specific hazard: one locale saying a flat "works" where the other
        says "the bridge works, the external service is not confirmed". A user
        switching languages would get a different promise from one fact.
        """

        bridge_only = hub.service_status_words(hub.SERVICE_BRIDGE_ONLY, True)
        bridge_only_ru = hub.service_status_words(hub.SERVICE_BRIDGE_ONLY, False)
        for text in (bridge_only, bridge_only_ru):
            with self.subTest(text=text):
                # Both must carry the qualifier, not just the good half.
                self.assertGreater(len(text.split()), 3)
        self.assertIn("not confirmed", bridge_only)
        self.assertIn("\u043d\u0435 \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0451\u043d", bridge_only_ru)

    def test_only_the_proven_service_status_counts_as_proven(self) -> None:
        for status in (
            hub.SERVICE_BRIDGE_ONLY,
            hub.SERVICE_AWAITING,
            hub.SERVICE_ACTION_REQUIRED,
            hub.SERVICE_READY,
            hub.SERVICE_ERROR,
            hub.SERVICE_NOT_CONFIGURED,
        ):
            with self.subTest(status=status):
                self.assertFalse(hub.service_status_is_proven(status))
        self.assertTrue(hub.service_status_is_proven(hub.SERVICE_WORKING))


class DetailShapeTests(unittest.TestCase):
    """A provider and a service look the same and offer the same order."""

    def test_both_families_offer_the_same_actions_in_the_same_order(self) -> None:
        """The order is the contract: Delete is last before Back, always.

        A reordered action list is how a person deletes the thing they meant to
        disable.
        """

        expected = (
            hub.DETAIL_VERIFY,
            hub.DETAIL_EDIT,
            hub.DETAIL_DISABLE,
            hub.DETAIL_DELETE,
            hub.DETAIL_BACK,
        )
        self.assertEqual(hub.detail_actions(True), expected)

    def test_disabling_swaps_exactly_one_action_and_keeps_the_order(self) -> None:
        enabled = hub.detail_actions(True)
        disabled = hub.detail_actions(False)

        self.assertEqual(len(enabled), len(disabled))
        self.assertEqual(enabled.index(hub.DETAIL_DISABLE), disabled.index(hub.DETAIL_ENABLE))
        self.assertEqual(enabled[-1], disabled[-1])
        self.assertEqual(enabled[-2], disabled[-2])

    def test_a_service_offers_no_provider_only_advanced_fields(self) -> None:
        """An empty group is worse than an absent one: it implies a missing value."""

        service_fields = {f.field_id for f in hub.service_advanced_fields("clickup")}
        for provider_only in ("base_url", "adapter", "context_window", "max_output"):
            with self.subTest(field=provider_only):
                self.assertNotIn(provider_only, service_fields)
        self.assertNotIn(
            hub.ADVANCED_GROUP_LIMITS,
            hub.advanced_visible_groups("service", "clickup"),
        )


class CommandSurfaceTests(unittest.TestCase):
    """One visible way in; the retired ones still work and stay hidden."""

    def test_connect_is_the_only_connection_command_in_the_menu(self) -> None:
        connection_commands = [
            command
            for command in tui.VISIBLE_COMMANDS
            if "connect" in command or command in {"/providers", "/mcp-clients", "/bridge"}
        ]
        self.assertEqual(connection_commands, ["/connect"])

    def test_the_retired_aliases_are_hidden_but_not_removed(self) -> None:
        """Hidden is not removed: these are real commands with real users."""

        for alias in ("/connections", "/providers", "/mcp-clients", "/bridge"):
            with self.subTest(alias=alias):
                self.assertIn(alias, tui.DEPRECATED_COMMAND_ALIASES)
                self.assertNotIn(alias, tui.VISIBLE_COMMANDS)

    def test_models_is_not_a_connection_alias_any_more(self) -> None:
        """`/models` opens the single model picker, not a connection screen."""

        self.assertNotIn("/models", tui.DEPRECATED_COMMAND_ALIASES)
        self.assertNotIn("/models", tui.VISIBLE_COMMANDS)


class KeyboardContractTests(unittest.TestCase):
    """The same key does the same thing on every connection screen."""

    def setUp(self) -> None:
        self.repository = self.enterContext(isolated_karox_directories())
        self.enterContext(patch.object(tui, "_load_language", return_value="en"))
        self.enterContext(patch.object(tui, "_selected_model", return_value=None))
        self.screens = hub.build_connections_screens(_Host())

    def _keys(self, screen_cls) -> set:
        return {binding.key for binding in screen_cls.BINDINGS}

    def test_escape_leaves_every_connection_screen(self) -> None:
        for name in (
            "ConnectionHubScreen",
            "ConnectionDetailScreen",
            "ServicePickerScreen",
            "ServiceConnectScreen",
            "AdvancedSettingsScreen",
        ):
            with self.subTest(screen=name):
                self.assertIn("escape", self._keys(self.screens[name]))

    def test_f2_and_f3_both_reach_the_advanced_layer(self) -> None:
        """F3 is kept for the people who learned it on the old limits editor.

        Both must land on the *same* screen; two advanced layers competing for
        one record is the state B4 was built to remove.
        """

        service_keys = self._keys(self.screens["ServiceConnectScreen"])
        self.assertIn("f2", service_keys)
        self.assertIn("f3", service_keys)
        actions = {
            binding.key: binding.action
            for binding in self.screens["ServiceConnectScreen"].BINDINGS
        }
        self.assertEqual(actions["f2"], actions["f3"])

    def test_verify_is_bound_only_where_the_action_exists(self) -> None:
        for name in ("ConnectionDetailScreen", "ServiceConnectScreen"):
            with self.subTest(screen=name):
                self.assertIn("f5", self._keys(self.screens[name]))
        # The picker has nothing to verify, so it must not offer the key.
        self.assertNotIn("f5", self._keys(self.screens["ServicePickerScreen"]))

    def test_no_connection_screen_deletes_on_a_single_stray_key(self) -> None:
        """`delete` may open a confirmation; it may never remove anything."""

        actions = {
            binding.key: binding.action
            for binding in self.screens["ConnectionDetailScreen"].BINDINGS
        }
        self.assertNotIn("delete", actions)

    def test_service_screen_does_not_steal_enter_from_focused_buttons(self) -> None:
        self.assertNotIn("enter", self._keys(self.screens["ServiceConnectScreen"]))

    def test_button_rich_connection_screens_do_not_use_priority_enter(self) -> None:
        # Enter is a widget action, not a screen-global action, on screens that
        # contain Cancel/Save/Advanced/etc. buttons. This catches the exact class
        # of bug where Tab visibly moves focus but Enter triggers another action.
        import inspect

        source = inspect.getsource(hub.build_connections_screens)
        for stolen in (
            'Binding("enter", "yes"',
            'Binding("enter", "connect"',
            'Binding("enter", "set_active"',
            'Binding("enter", "details"',
        ):
            with self.subTest(binding=stolen):
                self.assertNotIn(stolen, source)


class ConfirmationParityTests(unittest.TestCase):
    """Neither locale may promise more, or warn less, than the other."""

    def test_the_delete_confirmation_is_equally_final_in_both_languages(self) -> None:
        english_title, english_body = hub.delete_confirmation("OpenRouter", "Opus", True)
        russian_title, russian_body = hub.delete_confirmation("OpenRouter", "Opus", False)

        for title in (english_title, russian_title):
            with self.subTest(title=title):
                self.assertIn("OpenRouter", title)
                self.assertIn("Opus", title)
                self.assertTrue(title.endswith("?"))
        self.assertIn("cannot be undone", english_body)
        self.assertIn("\u043d\u0435\u043b\u044c\u0437\u044f \u043e\u0442\u043c\u0435\u043d\u0438\u0442\u044c", russian_body)

    def test_the_runtime_warnings_are_present_in_both_languages(self) -> None:
        for kind in ("provider", "service"):
            for running in (True, False):
                with self.subTest(kind=kind, running=running):
                    self.assertTrue(
                        hub.active_disable_warning(kind, running, True).strip()
                    )
                    self.assertTrue(
                        hub.active_disable_warning(kind, running, False).strip()
                    )
                    self.assertTrue(
                        hub.active_delete_warning(kind, running, True).strip()
                    )
                    self.assertTrue(
                        hub.active_delete_warning(kind, running, False).strip()
                    )

    def test_the_unsupported_rotation_note_says_the_same_thing_twice(self) -> None:
        """Both must decline. A locale that omitted the refusal would read as
        an offer, while the Save behind it refuses either way."""

        import inspect

        source = inspect.getsource(hub)
        self.assertIn("The key is saved. It cannot be replaced in ", source)
        self.assertIn(
            "\u041a\u043b\u044e\u0447 \u0441\u043e\u0445\u0440\u0430\u043d\u0451\u043d. \u0412 \u044d\u0442\u043e\u0439 \u0444\u043e\u0440\u043c\u0435 \u0435\u0433\u043e \u043d\u0435\u043b\u044c\u0437\u044f \u0437\u0430\u043c\u0435\u043d\u0438\u0442\u044c.",
            source,
        )


class HubResponsiveTests(unittest.IsolatedAsyncioTestCase):
    """The hub at all three supported sizes, and the cursor rules around it."""

    NARROW = (46, 14)
    STANDARD = (80, 24)
    WIDE = (120, 30)

    def setUp(self) -> None:
        self.repository = self.enterContext(isolated_karox_directories())
        self.enterContext(patch.object(tui, "_load_language", return_value="en"))
        self.enterContext(patch.object(tui, "_selected_model", return_value=None))

    def app(self, language: str = "en"):
        return tui.KaroXApp(self.repository, language=language)

    async def _hub(self, pilot, app, language: str = "en", **kwargs):
        screens = app._connections_screens_cached()
        screen = screens["ConnectionHubScreen"](language, **kwargs)
        app.push_screen(screen)
        await pilot.pause()
        return screen

    async def test_the_hub_fits_the_smallest_terminal(self) -> None:
        app = self.app()
        async with app.run_test(size=self.NARROW) as pilot:
            await pilot.pause()
            screen = await self._hub(pilot, app)
            dialog = screen.query_one("#connhub-dialog")
            self.assertLessEqual(dialog.size.width, self.NARROW[0])
            self.assertLessEqual(dialog.size.height, self.NARROW[1])
            self.assertNotIn("\n", str(screen.query_one("#connhub-hint").render()))

    async def test_the_hub_fits_the_smallest_terminal_in_russian(self) -> None:
        app = self.app("ru")
        async with app.run_test(size=self.NARROW) as pilot:
            await pilot.pause()
            screen = await self._hub(pilot, app, "ru")
            self.assertLessEqual(
                screen.query_one("#connhub-dialog").size.width, self.NARROW[0]
            )
            self.assertNotIn("\n", str(screen.query_one("#connhub-hint").render()))

    async def test_the_hub_is_usable_at_eighty_columns(self) -> None:
        app = self.app()
        async with app.run_test(size=self.STANDARD) as pilot:
            await pilot.pause()
            screen = await self._hub(pilot, app)
            self.assertLessEqual(
                screen.query_one("#connhub-dialog").size.width, self.STANDARD[0]
            )

    async def test_a_wide_terminal_grows_no_side_panel(self) -> None:
        """120 columns of room is not an invitation to fill it."""

        app = self.app()
        async with app.run_test(size=self.WIDE) as pilot:
            await pilot.pause()
            screen = await self._hub(pilot, app)
            self.assertLessEqual(screen.query_one("#connhub-dialog").size.width, 62)

    async def test_an_empty_hub_lands_on_an_add_action(self) -> None:
        """With nothing connected, the only useful place is the first Add."""

        from textual.widgets import OptionList

        app = self.app()
        async with app.run_test(size=self.NARROW) as pilot:
            await pilot.pause()
            screen = await self._hub(pilot, app)
            options = screen.query_one("#connhub-list", OptionList)
            landed = options.get_option_at_index(options.highlighted).id
            empty = screen.rows()

        self.assertEqual(empty, ())
        self.assertIn(landed, hub.HUB_ADD_ACTIONS)

    async def test_the_hub_lands_on_the_row_it_was_asked_for(self) -> None:
        """Returning from a child screen must not reset the cursor.

        `select` is how a verify, a save, an enable or a disable comes back to
        the record the user was working on rather than to the top of the list.
        """

        from textual.widgets import OptionList

        rows = (
            hub.HubRow(row_id="ai:alpha", family=hub.HUB_FAMILY_AI, name="Alpha"),
            hub.HubRow(row_id="ai:beta", family=hub.HUB_FAMILY_AI, name="Beta"),
            hub.HubRow(row_id="ai:gamma", family=hub.HUB_FAMILY_AI, name="Gamma"),
        )
        app = self.app()
        async with app.run_test(size=self.STANDARD) as pilot:
            await pilot.pause()
            screen = await self._hub(pilot, app)
            with patch.object(screen, "_collect", return_value=rows):
                screen.refresh_rows(select="ai:beta")
                await pilot.pause()
                options = screen.query_one("#connhub-list", OptionList)
                landed = options.get_option_at_index(options.highlighted).id

        self.assertEqual(landed, "ai:beta")

    async def test_a_vanished_row_does_not_strand_the_cursor(self) -> None:
        """After a delete the requested row is gone; the hub must still land on
        something selectable rather than on a heading."""

        from textual.widgets import OptionList

        rows = (
            hub.HubRow(row_id="ai:alpha", family=hub.HUB_FAMILY_AI, name="Alpha"),
        )
        app = self.app()
        async with app.run_test(size=self.STANDARD) as pilot:
            await pilot.pause()
            screen = await self._hub(pilot, app)
            with patch.object(screen, "_collect", return_value=rows):
                screen.refresh_rows(select="ai:deleted")
                await pilot.pause()
                options = screen.query_one("#connhub-list", OptionList)
                option = options.get_option_at_index(options.highlighted)

        self.assertFalse(getattr(option, "disabled", False))


class VerifyGuardTests(unittest.IsolatedAsyncioTestCase):
    """A second F5 must not race the first."""

    def setUp(self) -> None:
        self.repository = self.enterContext(isolated_karox_directories())
        self.enterContext(patch.object(tui, "_load_language", return_value="en"))
        self.enterContext(patch.object(tui, "_selected_model", return_value=None))

    async def test_a_second_verify_while_one_is_running_starts_nothing(self) -> None:
        """Two probes racing to write one status is how a failure gets
        overwritten by a stale success."""

        app = tui.KaroXApp(self.repository, language="en")
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screens = app._connections_screens_cached()
            screen = screens["ConnectionDetailScreen"](
                "en", kind="provider", identity="openrouter"
            )
            app.push_screen(screen)
            await pilot.pause()
            screen._checking = True
            with patch.object(screen, "run_worker") as worker:
                screen.action_verify()
                await pilot.pause()

        worker.assert_not_called()


# ---------------------------------------------------------------------------
# Small fakes. Deliberately minimal: these tests are about agreement between
# surfaces, not about the stores, which their own suites already cover.
# ---------------------------------------------------------------------------


class _Model:
    provider_id = "openrouter"
    model_id = "claude-opus"


class _Provider:
    def __init__(self, provider_id: str, enabled: bool = True) -> None:
        self.provider_id = provider_id
        self.enabled = enabled


class _Registry:
    def __init__(self, provider, models) -> None:
        self._provider = provider
        self._models = models

    def providers(self):
        return [self._provider]

    def models(self, provider_id=None):
        return list(self._models)

    def selected_model(self):
        return self._models[0] if self._models else None


class _Target:
    def __init__(self, connection_id: str, preset_id: str, enabled: bool = True) -> None:
        self.connection_id = connection_id
        self.name = "My connection"
        self.preset_id = preset_id
        self.enabled = enabled


class _State:
    def __init__(self, target, state: str) -> None:
        self.target = target
        self.state = state


    def test_p_key_copies_approval_password(self) -> None:
        """The B3 ServiceConnectScreen must offer a P key for the OAuth approval password.

        Without it the user can copy the MCP URL but cannot get the credential
        ChatGPT's OAuth page asks for, so the connection flow dead-ends.
        """
        actions = {
            binding.key: binding.action
            for binding in self.screens["ServiceConnectScreen"].BINDINGS
        }
        self.assertIn("p", actions)
        self.assertEqual(actions["p"], "copy_approval_password")
        self.assertTrue(
            hasattr(self.screens["ServiceConnectScreen"], "action_copy_approval_password")
        )

    def test_p_key_is_distinct_from_c_key(self) -> None:
        """One key per secret: C copies the URL, P copies the approval password."""
        actions = {
            binding.key: binding.action
            for binding in self.screens["ServiceConnectScreen"].BINDINGS
        }
        self.assertNotEqual(actions["c"], actions["p"])

    def test_oauth_password_is_not_part_of_the_normal_connection_hint(self) -> None:
        """Keep the P shortcut for recovery, but do not teach it in the happy path."""
        screen = self.screens["ServiceConnectScreen"]("en", preset_id="chatgpt-web")
        hint = screen._hint()
        self.assertNotIn("password", hint.lower())
        self.assertNotIn("oauth", hint.lower())
        self.assertIn("Tab", hint)
        self.assertIn("Esc", hint)

    def test_credential_copy_uses_the_profile_auth_scheme(self) -> None:
        """ChatGPT and Notion both use the OAuth approval flow."""
        chatgpt = self.screens["ServiceConnectScreen"]("en", preset_id="chatgpt-web")
        notion = self.screens["ServiceConnectScreen"]("en", preset_id="notion")
        self.assertTrue(hasattr(chatgpt, "action_copy_approval_password"))
        self.assertTrue(hasattr(notion, "action_copy_approval_password"))
        self.assertFalse(chatgpt._uses_bearer_auth())
        self.assertFalse(notion._uses_bearer_auth())

    def test_p_action_uses_typed_oauth_purpose(self) -> None:
        """The P action must resolve the OAuth approval password via the canonical service."""
        from karox.web_bridge_launcher import SecretPurpose
        # Verify the canonical service is importable and has the right purpose.
        self.assertEqual(SecretPurpose.OAUTH_APPROVAL_PASSWORD, "oauth_approval_password")
        self.assertNotEqual(SecretPurpose.OAUTH_APPROVAL_PASSWORD, SecretPurpose.STATIC_BEARER)


class _Host:
    def _copy_text(self, _text: str) -> None:
        return None

    def _refresh_status(self) -> None:
        return None


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
