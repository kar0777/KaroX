"""The standard provider path is provider, key, model, verify. Nothing else.

B2. A known preset already knows its endpoint and its adapter, so asking for
them is asking a question whose answer the product is holding. Those fields were
correctly hidden -- and hidden with no way back, which is a missing feature
wearing the clothes of progressive disclosure: a person whose provider moved to a
regional endpoint had to abandon the preset and re-enter everything as a custom
provider.

These tests pin both halves of the contract:

* the standard screen shows only what a person must supply;
* every hidden field is reachable in one deliberate step, without a mouse;
* a failed verification keeps what was typed and offers a retry;
* and nothing in an error message carries the key.

The flow is driven through the real screen and its production callbacks. Network
calls are the only thing faked, because a test that needs a live API key proves
nothing on a machine that has none.
"""

from __future__ import annotations

from _unittest_compat import enter_context

import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path
from _tui_harness import isolated_karox_directories

from karox import tui

# A key-shaped fixture. Not a credential: it exists to prove a key cannot reach
# an error message.
FAKE_KEY = "sk-live-" + ("9" * 32)


def _visible(screen, selector: str) -> bool:
    """Whether one widget is actually displayed, not merely mounted.

    A mounted widget with `display: none` still answers a query, so asserting on
    presence would pass for a field the user cannot see.
    """

    try:
        widget = screen.query_one(selector)
    except Exception:
        return False
    return str(widget.styles.display) != "none"


class ProviderFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = enter_context(self, isolated_karox_directories())
        enter_context(self, patch.object(tui, "_load_language", return_value="en"))
        enter_context(self, patch.object(tui, "_selected_model", return_value=None))

    def app(self) -> tui.KaroXApp:
        return tui.KaroXApp(self.repository, language="en")

    def known_preset(self) -> object:
        """A preset that carries a documented endpoint and an adapter.

        Chosen from the real catalog rather than constructed, so the test cannot
        pass against a preset shape the product no longer ships.
        """

        for preset in tui.provider_presets():
            if preset.installable and preset.endpoint_known:
                return preset
        self.fail("no installable preset with a known endpoint in the catalog")

    async def _screen(self, pilot, app, preset=None):
        screen = tui.ProviderSetupScreen("en", preset)
        app.push_screen(screen)
        await pilot.pause()
        return screen

    async def test_a_known_preset_hides_the_base_url_by_default(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, self.known_preset())

            # What a person must supply is on screen: key + one obvious Connect action.
            self.assertTrue(_visible(screen, "#provider-key"))
            self.assertTrue(_visible(screen, "#provider-discover"))
            self.assertFalse(_visible(screen, "#provider-save"))
            self.assertIn("Connect", str(screen.query_one("#provider-discover", tui.Button).label))
            # ... and what the preset already knows is not.
            self.assertFalse(_visible(screen, "#provider-url"))
            self.assertFalse(_visible(screen, "#provider-id"))
            self.assertFalse(_visible(screen, "#provider-adapter"))
            self.assertFalse(screen.advanced_open())

    async def test_advanced_settings_open_the_base_url(self) -> None:
        """The defect: hidden and unreachable is not disclosure, it is a dead end."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, self.known_preset())

            screen.action_advanced()
            await pilot.pause()

            self.assertTrue(screen.advanced_open())
            self.assertTrue(_visible(screen, "#provider-url"))
            self.assertTrue(_visible(screen, "#provider-id"))
            self.assertTrue(_visible(screen, "#provider-adapter"))
            for selector in tui.ProviderSetupScreen.ADVANCED_FIELD_IDS:
                with self.subTest(field=selector):
                    self.assertTrue(_visible(screen, selector))

    async def test_advanced_settings_close_again(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, self.known_preset())
            screen.action_advanced()
            await pilot.pause()
            screen.action_advanced()
            await pilot.pause()
            self.assertFalse(screen.advanced_open())
            self.assertFalse(_visible(screen, "#provider-url"))

    async def test_the_preset_endpoint_is_prefilled_not_asked_for(self) -> None:
        """Automatic means the field holds the answer, not that it is empty."""

        preset = self.known_preset()
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, preset)
            self.assertEqual(
                screen.query_one("#provider-url", tui.Input).value, preset.base_url
            )

    async def test_a_generic_provider_shows_its_required_fields_at_once(self) -> None:
        """With no preset there is nothing to disclose progressively."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, None)
            self.assertTrue(_visible(screen, "#provider-url"))
            self.assertTrue(_visible(screen, "#provider-adapter"))
            self.assertTrue(_visible(screen, "#provider-key"))
            self.assertEqual(screen.query_one("#provider-id", tui.Input).value, "")
            self.assertEqual(screen.query_one("#provider-url", tui.Input).value, "")
            self.assertEqual(
                screen.query_one("#provider-adapter", tui.RadioSet).pressed_button.id,
                "adapter-compatible",
            )

    async def test_the_limits_editor_is_still_reachable(self) -> None:
        """A working editor must not vanish because its button was renamed."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, self.known_preset())
            with patch.object(screen, "_open_limits") as limits:
                screen.action_limits()
            limits.assert_called_once_with()

    async def test_advanced_and_limits_both_work_without_a_mouse(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, self.known_preset())
            bound = {binding.key for binding in screen.BINDINGS}
            for key in ("f2", "f3", "f5", "f10", "escape"):
                with self.subTest(key=key):
                    self.assertIn(key, bound)

    async def test_a_failed_verification_keeps_what_was_typed(self) -> None:
        """Requirements 18 and 20 together: nothing is cleared, retry is offered.

        Losing a pasted key on a failed probe means retyping it to try the same
        thing again, which is the moment people give up on a connection screen.
        """

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, self.known_preset())
            screen.query_one("#provider-key", tui.Input).value = FAKE_KEY
            screen.query_one("#provider-model", tui.Input).value = "some-model"
            await pilot.pause()

            depth = len(app.screen_stack)
            # The real object the production path hands to `_probe_failed`, built
            # by the screen's own reader. Passing a stand-in would test a
            # signature rather than the branch a user actually reaches.
            setup = screen._setup_from_form(require_model=True)
            screen._probe_failed(RuntimeError("connection refused"), setup)
            await pilot.pause()

            # The form is still open ...
            self.assertEqual(len(app.screen_stack), depth)
            # ... with everything the user supplied still in it ...
            self.assertEqual(
                screen.query_one("#provider-key", tui.Input).value, FAKE_KEY
            )
            self.assertEqual(
                screen.query_one("#provider-model", tui.Input).value, "some-model"
            )
            self.assertTrue(screen.query_one("#provider-url", tui.Input).value)
            # ... and the retry is available rather than a dead disabled button.
            save = screen.query_one("#provider-save", tui.Button)
            self.assertFalse(save.disabled)
            self.assertIn("Retry", str(save.label))

    async def test_a_failed_verification_never_shows_the_key(self) -> None:
        """An error is read by whoever is standing behind the user."""

        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, self.known_preset())
            screen.query_one("#provider-key", tui.Input).value = FAKE_KEY
            screen.query_one("#provider-model", tui.Input).value = "some-model"
            await pilot.pause()

            setup = screen._setup_from_form(require_model=True)
            screen._probe_failed(
                RuntimeError(f"401 Unauthorized for key {FAKE_KEY}"), setup
            )
            await pilot.pause()

            shown = str(screen.query_one("#provider-error", tui.Static).render())
            self.assertNotIn(FAKE_KEY, shown)
            self.assertNotIn(FAKE_KEY[8:], shown)
            self.assertTrue(shown.strip(), "a failure must say something")

    async def test_the_key_input_is_masked(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, self.known_preset())
            self.assertTrue(screen.query_one("#provider-key", tui.Input).password)

    async def test_escape_leaves_the_form_without_saving(self) -> None:
        app = self.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            depth = len(app.screen_stack)
            await self._screen(pilot, app, self.known_preset())
            self.assertEqual(len(app.screen_stack), depth + 1)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), depth)

    async def test_a_narrow_terminal_keeps_the_required_field_and_verify_visible(
        self,
    ) -> None:
        app = self.app()
        async with app.run_test(size=(46, 14)) as pilot:
            await pilot.pause()
            screen = await self._screen(pilot, app, self.known_preset())
            self.assertTrue(_visible(screen, "#provider-key"))
            self.assertTrue(_visible(screen, "#provider-discover"))
            self.assertFalse(_visible(screen, "#provider-save"))
            dialog = screen.query_one("#provider-dialog")
            # Width is the property B2 fixed: a fixed 82 columns overflowed a
            # 46-column terminal. Height was already handled by the percentage
            # ceiling beside it and is asserted by the compactness test in
            # tests/test_tui.py, which this must not contradict.
            self.assertLessEqual(dialog.size.width, 46)


class ProviderFlowLanguageTests(unittest.IsolatedAsyncioTestCase):
    """RU/EN parity for the one label B2 introduced."""

    def setUp(self) -> None:
        self.repository = enter_context(self, isolated_karox_directories())
        enter_context(self, patch.object(tui, "_selected_model", return_value=None))

    async def test_the_advanced_button_is_labelled_in_both_languages(self) -> None:
        seen = []
        for language in ("ru", "en"):
            with patch.object(tui, "_load_language", return_value=language):
                app = tui.KaroXApp(self.repository, language=language)
                async with app.run_test(size=(120, 42)) as pilot:
                    await pilot.pause()
                    screen = tui.ProviderSetupScreen(language, None)
                    app.push_screen(screen)
                    await pilot.pause()
                    label = str(
                        screen.query_one("#provider-advanced", tui.Button).label
                    )
                    self.assertTrue(label.strip())
                    self.assertNotIn("None", label)
                    seen.append(label)
        self.assertNotEqual(seen[0], seen[1], "one language was not translated")


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
