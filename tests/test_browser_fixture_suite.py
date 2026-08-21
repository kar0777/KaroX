"""Deterministic local browser fixtures for the production DOM engine.

These tests drive the exact ``dom_helpers.js`` the extension injects -- the
locator grammar, the ambiguity contract, and every page-side action -- inside
a real browser rendering local fixture pages. No public website is involved,
so a failure is a code failure, never the weather.

The suite needs a local Chromium-family browser. When the machine genuinely
has none the suite skips with the reason on record; the extension smoke tests
stay the live-path proof.
"""

from __future__ import annotations

import base64
import unittest
from pathlib import Path

from _support import SRC  # noqa: F401

import karox

_HELPERS = Path(karox.__file__).resolve().parent / "browser_extension" / "dom_helpers.js"
_FIXTURES = Path(__file__).resolve().parent / "browser_fixtures"

try:  # pragma: no cover - environment probe, not logic
    from playwright.sync_api import sync_playwright

    _PLAYWRIGHT_REASON = ""
except Exception as exc:  # pragma: no cover
    sync_playwright = None  # type: ignore[assignment]
    _PLAYWRIGHT_REASON = f"playwright is unavailable: {exc}"


def _launch(playwright):  # pragma: no cover - exercised only with a browser
    try:
        return playwright.chromium.launch(channel="chrome", headless=True)
    except Exception:
        return playwright.chromium.launch(headless=True)


@unittest.skipIf(sync_playwright is None, _PLAYWRIGHT_REASON)
class _FixturePage(unittest.TestCase):
    """One shared browser; each test navigates its own fixture copy."""

    playwright = None
    browser = None

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.playwright = sync_playwright().start()
            cls.browser = _launch(cls.playwright)
        except Exception as exc:
            if cls.playwright is not None:
                cls.playwright.stop()
                cls.playwright = None
            raise unittest.SkipTest(f"no local browser could be launched: {exc}")

    @classmethod
    def tearDownClass(cls) -> None:
        # Lifecycle contract: nothing outlives the suite -- no orphan browser,
        # no orphan context, no leaked transport.
        if cls.browser is not None:
            cls.browser.close()
            cls.browser = None
        if cls.playwright is not None:
            cls.playwright.stop()
            cls.playwright = None

    def open_fixture(self, name: str):
        page = self.browser.new_page()
        self.addCleanup(page.close)
        page.goto((_FIXTURES / name).as_uri())
        page.add_script_tag(path=str(_HELPERS))
        return page

    @staticmethod
    def act(page, **payload):
        return page.evaluate("(payload) => domAction(payload)", payload)


class LocatorTests(_FixturePage):
    def test_every_strategy_finds_the_form_field(self) -> None:
        page = self.open_fixture("fixture_form.html")
        for selector in (
            "#username",
            "label=Username",
            "placeholder=Your name",
            "testid=username-input",
            # The accessible name follows the accname order: the label
            # "Username" outranks the placeholder text.
            "role=textbox[name=\"Username\"]",
        ):
            with self.subTest(selector=selector):
                meta = self.act(page, action="inspect", selector=selector)
                self.assertEqual(meta.get("id"), "username", selector)

    def test_data_test_id_variant_is_found(self) -> None:
        page = self.open_fixture("fixture_form.html")
        meta = self.act(page, action="inspect", selector="testid=alternate-field")
        self.assertEqual(meta.get("id"), "alt-testid")

    def test_exact_text_outranks_substring(self) -> None:
        page = self.open_fixture("fixture_form.html")
        meta = self.act(page, action="inspect", selector="text=Save")
        self.assertEqual(meta.get("id"), "save")

    def test_ambiguous_target_returns_candidates_not_a_guess(self) -> None:
        page = self.open_fixture("fixture_form.html")
        result = self.act(page, action="click", selector="text=Delete")
        self.assertEqual(result.get("error_kind"), "ambiguous_target")
        candidates = result.get("candidates") or []
        self.assertEqual(len(candidates), 2)
        for candidate in candidates:
            self.assertEqual(candidate.get("text"), "Delete")
        # No click happened anywhere.
        self.assertEqual(page.text_content("#click-log"), "")

    def test_nth_disambiguates_explicitly(self) -> None:
        page = self.open_fixture("fixture_form.html")
        result = self.act(page, action="inspect", selector="text=Delete >> nth=1")
        self.assertEqual(result.get("name"), "")
        self.assertNotIn("error_kind", result)

    def test_missing_element_is_a_typed_absence(self) -> None:
        page = self.open_fixture("fixture_form.html")
        result = self.act(page, action="click", selector="#does-not-exist")
        self.assertEqual(result.get("error_kind"), "element_not_found")


class InteractionTests(_FixturePage):
    def test_click_opens_the_dialog(self) -> None:
        page = self.open_fixture("fixture_form.html")
        result = self.act(page, action="click", selector="text=Open dialog")
        self.assertTrue(result.get("clicked"))
        meta = self.act(page, action="inspect", selector="#dialog")
        self.assertTrue(meta.get("visible"))

    def test_dblclick_fires_the_double_click_handler(self) -> None:
        page = self.open_fixture("fixture_form.html")
        self.act(page, action="dblclick", selector="#dbl-target")
        self.assertEqual(page.text_content("#click-log"), "dblclicked")

    def test_hover_reveals_the_tooltip(self) -> None:
        page = self.open_fixture("fixture_form.html")
        self.act(page, action="hover", selector="#hover-zone")
        meta = self.act(page, action="inspect", selector="#hover-tip")
        self.assertTrue(meta.get("visible"))

    def test_focus_moves_the_active_element(self) -> None:
        page = self.open_fixture("fixture_form.html")
        result = self.act(page, action="focus", selector="#notes")
        self.assertTrue(result.get("focused"))
        meta = self.act(page, action="inspect", selector="#notes")
        self.assertTrue(meta.get("focused"))

    def test_fill_clear_and_type_manage_the_value_honestly(self) -> None:
        page = self.open_fixture("fixture_form.html")
        filled = self.act(page, action="fill", selector="#username", value="Ada")
        self.assertEqual(filled.get("value_length"), 3)
        cleared = self.act(page, action="clear", selector="#username")
        self.assertEqual(cleared.get("value_length"), 0)
        typed = self.act(page, action="type", selector="#username", value="Lovelace")
        self.assertEqual(typed.get("value_length"), 8)
        self.assertEqual(page.input_value("#username"), "Lovelace")
        # Per-keystroke events actually fired, one per character.
        events = page.evaluate("window.__karoxTypedEvents || []")
        self.assertGreaterEqual(len([e for e in events if e == "insertText"]), 8)

    def test_select_checkbox_and_radio_reach_the_requested_state(self) -> None:
        page = self.open_fixture("fixture_form.html")
        selected = self.act(page, action="select", selector="#country", value=["pl", "fr"])
        self.assertTrue(selected.get("selected"))
        values = page.evaluate(
            "Array.from(document.querySelector('#country').selectedOptions).map(o => o.value)"
        )
        self.assertEqual(values, ["pl", "fr"])
        checked = self.act(page, action="set_checked", selector="#agree", checked=True)
        self.assertTrue(checked.get("checked"))
        # Idempotent: asking again keeps the state instead of toggling it away.
        checked = self.act(page, action="set_checked", selector="#agree", checked=True)
        self.assertTrue(checked.get("checked"))
        unchecked = self.act(page, action="set_checked", selector="#agree", checked=False)
        self.assertFalse(unchecked.get("checked"))
        radio = self.act(page, action="set_checked", selector="#plan-pro", checked=True)
        self.assertTrue(radio.get("checked"))
        self.assertFalse(page.is_checked("#plan-basic"))
        with self.assertRaises(Exception):
            self.act(page, action="set_checked", selector="#plan-pro", checked=False)

    def test_press_enter_submits_the_form(self) -> None:
        page = self.open_fixture("fixture_form.html")
        self.act(page, action="press", selector="#username", key="Enter")
        self.assertEqual(page.text_content("#submit-log"), "submitted")

    def test_press_carries_modifiers_without_side_effects(self) -> None:
        page = self.open_fixture("fixture_form.html")
        result = self.act(page, action="press", selector="#username", key="Control+Enter")
        self.assertTrue(result.get("pressed"))
        # A modified Enter is a shortcut, not a submit.
        self.assertEqual(page.text_content("#submit-log"), "")

    def test_upload_attaches_in_memory_files(self) -> None:
        page = self.open_fixture("fixture_form.html")
        content = base64.b64encode(b"hello fixture").decode()
        result = self.act(
            page,
            action="upload",
            selector="#file-upload",
            files=[
                {"name": "a.txt", "mime": "text/plain", "content_base64": content},
                {"name": "b.bin", "mime": "application/octet-stream", "content_base64": content},
            ],
        )
        self.assertEqual(result.get("count"), 2)
        recorded = page.get_attribute("#click-log", "data-files")
        self.assertEqual(recorded, "a.txt:13,b.bin:13")


class SecretContainmentTests(_FixturePage):
    def test_secret_fields_never_leak_text_through_metadata(self) -> None:
        page = self.open_fixture("fixture_form.html")
        page.fill("#password", "hunter2")
        meta = self.act(page, action="inspect", selector="#password")
        self.assertTrue(meta.get("secret"))
        self.assertEqual(meta.get("text"), "")
        text = self.act(page, action="text", selector="#password")
        self.assertEqual(text.get("text"), "")
        self.assertTrue(text.get("secret"))

    def test_fill_secret_marks_and_masks_the_field(self) -> None:
        page = self.open_fixture("fixture_form.html")
        result = self.act(page, action="fill_secret", selector="#username", value="token-123")
        self.assertTrue(result.get("secret"))
        self.assertNotIn("value_length", result)
        meta = self.act(page, action="inspect", selector="#username")
        self.assertTrue(meta.get("secret"))
        self.assertEqual(meta.get("text"), "")


class ScrollAndDynamicTests(_FixturePage):
    def test_page_scroll_and_scroll_to_bottom(self) -> None:
        page = self.open_fixture("fixture_dynamic.html")
        result = self.act(page, action="scroll", mode="page", dy=500)
        self.assertEqual(result.get("mode"), "page")
        self.assertGreaterEqual(result.get("y"), 400)
        result = self.act(page, action="scroll", mode="page", to="bottom")
        self.assertGreater(result.get("y"), 1000)
        result = self.act(page, action="scroll", mode="page", to="top")
        self.assertEqual(result.get("y"), 0)

    def test_container_scroll_moves_the_container_not_the_page(self) -> None:
        page = self.open_fixture("fixture_form.html")
        result = self.act(page, action="scroll", selector="#scroll-box", mode="container", dy=200)
        self.assertEqual(result.get("mode"), "container")
        self.assertGreaterEqual(result.get("y"), 150)
        self.assertEqual(page.evaluate("Math.round(window.scrollY)"), 0)

    def test_scroll_into_view_reaches_an_offscreen_element(self) -> None:
        page = self.open_fixture("fixture_dynamic.html")
        self.act(page, action="scroll", selector="#bottom-marker", mode="into_view")
        visible = page.evaluate(
            "(() => { const r = document.querySelector('#bottom-marker').getBoundingClientRect();"
            " return r.top >= 0 && r.bottom <= window.innerHeight; })()"
        )
        self.assertTrue(visible)

    def test_dynamic_content_becomes_visible_after_the_trigger(self) -> None:
        page = self.open_fixture("fixture_dynamic.html")
        meta = self.act(page, action="inspect", selector="testid=late-content")
        self.assertFalse(meta.get("visible"))
        self.act(page, action="click", selector="text=Start loading")
        page.wait_for_selector("#appear-later.ready", timeout=5000)
        meta = self.act(page, action="inspect", selector="testid=late-content")
        self.assertTrue(meta.get("visible"))

    def test_spa_navigation_and_history_round_trip(self) -> None:
        page = self.open_fixture("fixture_dynamic.html")
        self.act(page, action="click", selector="text=About")
        self.assertEqual(page.text_content("#route"), "about")
        page.go_back()
        page.wait_for_function("document.querySelector('#route').textContent === 'home'")
        page.go_forward()
        page.wait_for_function("document.querySelector('#route').textContent === 'about'")


if __name__ == "__main__":
    unittest.main()
