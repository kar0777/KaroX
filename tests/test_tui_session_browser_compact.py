"""The compact contract of the Session Browser: activity, width, footer, safety.

The companion to ``tests/test_tui_session_browser.py``. That file proves the
browser is a real screen over ``SessionViewStore`` -- which rows exist, in what
order, which one is selected. This one proves the C presentation contract: what
a row is allowed to say, how much room it thinks it has, what the footer drops
first, and that a hostile payload has no path to either.

Two rules hold throughout. Nothing asserts on prose written by hand -- every
word comes from a catalog in :mod:`karox.tui`, so a wording change moves both
languages at once and a test cannot pass in English while the Russian row is
wrong. And nothing inspects source: the security cases plant a payload, mount
the real screen and read what the widget actually renders, because
``inspect.getsource`` proves only that a line was written, never that it ran.

The key-shaped and token-shaped strings below are fixtures. They are assembled
at runtime, are not credentials, and exist to be searched for in rendered text.
"""

from __future__ import annotations

from _unittest_compat import enter_context

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox import session_view, tui
from karox.event_bus import EventBus, EventKind, EventLevel
from karox.models import AccessProfile
from karox.session_view import (
    ACTION_OPEN,
    ACTION_RESUME,
    ACTION_REVIEW_RISK,
    ACTION_STOP,
    SessionSummary,
)
from karox.sessions import SessionStore

# Assembled rather than written, because the repository write path redacts
# key-shaped literals and a redacted fixture proves nothing.
FAKE_APPROVAL_TOKEN = "appr-" + ("q" * 30)
FAKE_KEY_SHAPED = "sk-" + "live-" + ("4" * 32)

NARROW = (46, 14)
STANDARD = (80, 24)
WIDE = (120, 30)

# The footer width the *layout* guarantees at the narrowest supported terminal.
# Claiming to support width=1 by printing a sliced word is not support, so the
# pure width sweeps start here instead, and
# ``ResponsiveLayoutTests.test_the_layout_guarantees_the_footer_minimum`` pins
# that the mounted screen really does hand the footer at least this much.
FOOTER_MINIMUM = 40


class _Harness:
    """An application wired to a private bus and a private session directory."""

    def __init__(self, stack: unittest.TestCase) -> None:
        self.bus = EventBus()
        self.root = Path(
            enter_context(stack, tempfile.TemporaryDirectory())  # type: ignore[attr-defined]
        )
        self.sessions = SessionStore(self.root)
        enter_context(stack,   # type: ignore[attr-defined]
            patch.object(tui, "event_bus", lambda: self.bus)
        )
        enter_context(stack,   # type: ignore[attr-defined]
            patch.object(tui, "session_dir", lambda: self.root)
        )
        enter_context(stack,   # type: ignore[attr-defined]
            patch.object(tui, "_load_language", return_value="en")
        )
        enter_context(stack,   # type: ignore[attr-defined]
            patch.object(tui, "_selected_model", return_value=None)
        )

    def app(self, session_id: str | None = None) -> tui.KaroXApp:
        return tui.KaroXApp(Path.cwd(), session_id=session_id, language="en")

    def publish_state(
        self, session_id: str, summary: str = "state", **data: object
    ) -> None:
        self.bus.publish(
            EventKind.SESSION_STATE,
            session_id=session_id,
            summary=summary,
            data=data,
        )

    def publish_pending_risk(self, session_id: str, *, token: bool = False) -> None:
        data: dict[str, object] = {
            "allowed": False,
            "risk": "high",
            "reason": "confirmation_required",
            "action_digest": "d" * 16,
        }
        secrets: tuple[str, ...] = ()
        if token:
            data["confirmation_token"] = FAKE_APPROVAL_TOKEN
            secrets = (FAKE_APPROVAL_TOKEN,)
        self.bus.publish(
            EventKind.RISK_DECISION,
            session_id=session_id,
            summary="needs confirmation",
            level=EventLevel.WARNING,
            data=data,
            secrets=secrets,
        )


class _CompactCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.harness = _Harness(self)

    async def _open(self, app: tui.KaroXApp, pilot: object) -> tui.SessionBrowserScreen:
        app.action_session_browser()
        await pilot.pause()  # type: ignore[attr-defined]
        screen = app.screen
        assert isinstance(screen, tui.SessionBrowserScreen)
        return screen

    def _drain(self, app: tui.KaroXApp) -> None:
        app._drain_session_view()

    def _summary(self, app: tui.KaroXApp, session_id: str) -> SessionSummary:
        row = app._view_store.summary(session_id)
        assert row is not None
        return row

    def _hint(self, screen: tui.SessionBrowserScreen) -> str:
        return str(screen.query_one("#session-browser-hint", tui.Static).render())

    def _title(self, screen: tui.SessionBrowserScreen) -> str:
        return str(screen.query_one("#session-browser-title", tui.Static).render())

    def _activity(self, kind: str, english: bool) -> str:
        return tui._ACTIVITY_WORDS[kind][1 if english else 0]


# ---------------------------------------------------------------- 4. activity


class ActivityMappingTests(_CompactCase):
    """A live step is said in words, or not at all.

    The bug this class exists for: the browser looked ``current_step`` up in
    ``_EVENT_SUMMARY_TEXT``, which holds lifecycle summaries such as
    ``agent_run_started``. ``current_step`` holds tool identifiers. The lookup
    therefore missed on essentially every real step, and the activity field was
    silently never populated -- no crash, no leak, just a column that never
    worked.
    """

    async def test_a_known_tool_is_said_in_words_in_both_spellings(self) -> None:
        cases = {
            "repo.read_file": tui.ACTIVITY_READING,
            "repo_read_file": tui.ACTIVITY_READING,
            "repo.search": tui.ACTIVITY_SEARCHING,
            "repo_search": tui.ACTIVITY_SEARCHING,
            "repo.edit_file": tui.ACTIVITY_EDITING,
            "repo_edit_file": tui.ACTIVITY_EDITING,
            "checks.run": tui.ACTIVITY_TESTING,
            "checks_run": tui.ACTIVITY_TESTING,
        }
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            self.harness.publish_state(
                "s-tool", status=tui.STATUS_RUNNING, task="do the thing"
            )
            screen = await self._open(app, pilot)
            for step, kind in cases.items():
                with self.subTest(step=step):
                    self.harness.publish_state(
                        "s-tool", status=tui.STATUS_RUNNING, current_step=step
                    )
                    self._drain(app)
                    text = screen.row_text("s-tool")
                    self.assertIn(self._activity(kind, True), text)
                    # The identifier itself never reaches a person.
                    self.assertNotIn(step, text)

    async def test_an_unknown_tool_says_nothing_rather_than_working(self) -> None:
        """Not ``_activity_kind_for_tool``, which is fail-soft to "Working".

        That fallback is right for the activity line, which is obliged to say
        something. Here it would put the same uninformative word beside every
        session running an uncatalogued tool and cost a column the task wanted.
        """

        self.harness.publish_state(
            "s-unknown",
            status=tui.STATUS_RUNNING,
            task="do the thing",
            current_step="apply_patch",
        )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            text = screen.row_text("s-unknown")
            self.assertNotIn("apply_patch", text)
            self.assertNotIn(self._activity(tui.ACTIVITY_WORKING, True), text)
            # The store still has it; only the browser declines to render it.
            self.assertEqual(self._summary(app, "s-unknown").current_step, "apply_patch")

    async def test_a_hostile_step_identifier_reaches_no_row(self) -> None:
        payload = "\x1b[2J[bold red]rm -rf /[/] " + FAKE_KEY_SHAPED
        self.harness.publish_state(
            "s-hostile",
            status=tui.STATUS_RUNNING,
            task="do the thing",
            current_step=payload,
        )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            text = screen.row_text("s-hostile")
            for fragment in ("rm -rf", "\x1b", "[bold red]", FAKE_KEY_SHAPED):
                with self.subTest(fragment=fragment[:12]):
                    self.assertNotIn(fragment, text)
            self.assertNotIn(payload, self._hint(screen))

    def test_a_waiting_reason_outranks_a_known_tool(self) -> None:
        row = _summary_fixture(
            status=tui.STATUS_STOPPED,
            waiting_reason="step_limit",
            current_step="repo.search",
            primary_action=ACTION_RESUME,
        )
        for english in (True, False):
            with self.subTest(english=english):
                text = tui._browser_activity_text(row, english)
                self.assertEqual(
                    text, tui._WAITING_REASON_TEXT["step_limit"][1 if english else 0]
                )
                self.assertNotEqual(
                    text, self._activity(tui.ACTIVITY_SEARCHING, english)
                )

    def test_a_pending_confirmation_outranks_everything(self) -> None:
        row = _summary_fixture(
            status=tui.STATUS_RUNNING,
            waiting_reason="step_limit",
            current_step="repo.search",
            primary_action=ACTION_REVIEW_RISK,
        )
        for english in (True, False):
            with self.subTest(english=english):
                self.assertEqual(
                    tui._browser_activity_text(row, english),
                    tui._SESSION_ACTION_TEXT[ACTION_REVIEW_RISK][1 if english else 0],
                )

    def test_the_browser_uses_the_one_activity_catalog(self) -> None:
        """No second vocabulary: same table as the ordinary-mode activity line."""

        for tool, kind in tui._TOOL_ACTIVITY_KINDS.items():
            with self.subTest(tool=tool):
                self.assertEqual(tui._browser_activity_kind(tool), kind)
                self.assertIn(kind, tui._ACTIVITY_WORDS)
        self.assertEqual(tui._browser_activity_kind("nothing.known"), "")
        self.assertEqual(tui._browser_activity_kind(None), "")


# --------------------------------------------------------------- 11. fallback


class TaskFallbackTests(_CompactCase):
    """A session with no task still needs a headline a person can read."""

    def test_a_short_id_is_shown_whole_in_both_languages(self) -> None:
        row = _summary_fixture(session_id="s-summary", title="")
        self.assertEqual(tui._browser_task_text(row, True, 48), "Session s-summary")
        russian = tui._browser_task_text(row, False, 48)
        # An earlier cut took the last six characters and produced "ummary":
        # a truncated word that reads as a name and is not one.
        self.assertIn("s-summary", russian)
        self.assertNotIn("ummary", russian.replace("s-summary", ""))

    def test_a_long_minted_id_keeps_only_its_distinguishing_segment(self) -> None:
        identifier = "task-1754300000-abcdef12"
        row = _summary_fixture(session_id=identifier, title="")
        text = tui._browser_task_text(row, True, 48)
        self.assertIn("abcdef12", text)
        self.assertNotIn(identifier, text)
        self.assertNotIn("1754300000", text)

    def test_an_empty_id_fails_soft(self) -> None:
        row = _summary_fixture(session_id="", title="")
        self.assertEqual(tui._browser_task_text(row, True, 48), "Session ?")

    def test_the_fallback_respects_the_width_budget(self) -> None:
        row = _summary_fixture(session_id="task-1754300000-abcdef12", title="")
        for budget in (12, 16, 24):
            with self.subTest(budget=budget):
                self.assertLessEqual(
                    len(tui._browser_task_text(row, True, budget)), budget
                )

    async def test_the_full_internal_id_is_absent_once_a_task_exists(self) -> None:
        identifier = "task-1754300000-abcdef12"
        self.harness.publish_state(
            identifier, status=tui.STATUS_RUNNING, task="fix the failing tests"
        )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            text = screen.row_text(identifier)
            self.assertTrue(text.startswith("fix the failing tests"))
            self.assertNotIn(identifier, text)


# ----------------------------------------------------------------- 7. footer


class FooterPolicyTests(unittest.TestCase):
    """One line, fields dropped whole, in a fixed order."""

    def _row(self, action: str) -> SessionSummary:
        return _summary_fixture(primary_action=action)

    def test_a_wide_footer_says_all_three_things(self) -> None:
        text = tui._session_browser_footer(
            self._row(ACTION_STOP), True, 90, shown=100, hidden=12
        )
        self.assertIn(tui._SESSION_ACTION_TEXT[ACTION_STOP][1], text)
        self.assertIn("12", text)
        self.assertIn("Esc", text)
        self.assertLessEqual(len(text), 90)

    def test_the_esc_hint_is_the_first_field_dropped(self) -> None:
        """Escape keeps working whether or not the footer advertises it.

        The hidden-session count does not exist anywhere else on the screen, so
        spending the last columns on the hint would make the browser silently
        claim to be showing everything it has.
        """

        for english in (True, False):
            with self.subTest(english=english):
                text = tui._session_browser_footer(
                    self._row(ACTION_REVIEW_RISK), english, 40, shown=100, hidden=12
                )
                self.assertLessEqual(len(text), 40)
                self.assertIn("12", text)
                self.assertNotIn("Esc", text)

    def test_the_longest_action_still_fits_a_narrow_footer(self) -> None:
        for action in tui._SESSION_ACTION_TEXT:
            for english in (True, False):
                with self.subTest(action=action, english=english):
                    text = tui._session_browser_footer(
                        self._row(action), english, FOOTER_MINIMUM
                    )
                    self.assertLessEqual(len(text), FOOTER_MINIMUM)
                    self.assertIn(
                        tui._SESSION_ACTION_TEXT[action][1 if english else 0], text
                    )

    def test_an_action_is_never_character_truncated(self) -> None:
        """The prefix goes, then the long spelling, and the word stays whole.

        ``Enter: \u043d\u0443\u0436\u043d\u043e \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u2026`` and ``confirmatio\u2026`` both ask the reader
        to guess what Enter is about to do, which is exactly the guess a
        confirmation exists to prevent. Below every width, in both languages,
        for every action, the rendered action is one of the catalogued whole
        forms and never a slice of one.
        """

        for action in tui._SESSION_ACTION_TEXT:
            for english in (True, False):
                whole = tui._session_action_spellings(action, english)
                for width in range(FOOTER_MINIMUM, 121):
                    for hidden in (0, 999):
                        text = tui._session_browser_footer(
                            self._row(action),
                            english,
                            width,
                            shown=100,
                            hidden=hidden,
                        )
                        self.assertLessEqual(len(text), width, f"width={width}")
                        self.assertNotIn("\n", text)
                        self.assertNotIn("\u2026", text)
                        self.assertTrue(
                            any(spelling in text for spelling in whole),
                            f"{action}/{english}/{width}: {text!r}",
                        )

    def test_the_short_action_is_a_whole_word_for_the_same_action(self) -> None:
        """Degrade the vocabulary, never the meaning.

        ``review_risk`` says "review" rather than "confirm" on purpose: Enter
        opens the decision, it does not approve it, and a short form promising
        approval would be a more dangerous lie than the long one it replaced.
        """

        for action in tui._SESSION_ACTION_TEXT:
            for english in (True, False):
                with self.subTest(action=action, english=english):
                    spellings = tui._session_action_spellings(action, english)
                    self.assertTrue(spellings)
                    for spelling in spellings:
                        self.assertTrue(spelling.strip())
                        self.assertNotIn("\u2026", spelling)
                        # What a leaked identifier actually looks like. An
                        # English verb that happens to match its own stable id
                        # is not one: "resume" is the honest word for resume,
                        # and renaming the action to avoid the coincidence
                        # would be the test dictating the product.
                        self.assertNotIn("_", spelling)
                        self.assertNotIn(".", spelling)
                        self.assertNotIn("review_risk", spelling)
                        self.assertEqual(spelling, spelling.strip())
                        if not english:
                            # A Russian spelling carrying Latin letters means a
                            # missing translation shown as if it were one.
                            self.assertFalse(
                                any("a" <= char.lower() <= "z" for char in spelling),
                                f"untranslated: {spelling!r}",
                            )
                    # Sorted longest-first, so a width walk stops at the first fit.
                    self.assertEqual(
                        list(spellings),
                        sorted(spellings, key=len, reverse=True),
                    )

    def test_the_known_actions_say_the_expected_words(self) -> None:
        """Table-driven, because "whole and localized" is not yet "correct".

        The short ``review_risk`` form is the one that matters: Enter opens the
        decision, so "review" is true and "confirm" would promise an approval
        the key does not give.
        """

        expected = {
            (ACTION_OPEN, True): ("open",),
            (ACTION_RESUME, True): ("resume",),
            (ACTION_STOP, True): ("stop",),
            (ACTION_REVIEW_RISK, True): ("confirmation needed", "review"),
            (ACTION_OPEN, False): ("\u043e\u0442\u043a\u0440\u044b\u0442\u044c",),
            (ACTION_RESUME, False): ("\u043f\u0440\u043e\u0434\u043e\u043b\u0436\u0438\u0442\u044c",),
            (ACTION_STOP, False): ("\u043e\u0441\u0442\u0430\u043d\u043e\u0432\u0438\u0442\u044c",),
            (ACTION_REVIEW_RISK, False): (
                "\u043d\u0443\u0436\u043d\u043e \u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0438\u0435",
                "\u043f\u0440\u043e\u0432\u0435\u0440\u0438\u0442\u044c",
            ),
        }
        for (action, english), words in expected.items():
            with self.subTest(action=action, english=english):
                self.assertEqual(
                    tui._session_action_spellings(action, english), words
                )
        # And the short confirmation form does not promise an approval.
        for english in (True, False):
            short = tui._session_action_spellings(ACTION_REVIEW_RISK, english)[-1]
            for promise in ("confirm", "approve", "\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0434\u0438\u0442\u044c"):
                self.assertNotIn(promise, short.lower())

    def test_no_uncatalogued_action_can_reach_the_footer(self) -> None:
        """Closure, which is a stronger guarantee than a fallback.

        ``_catalog_text`` renders an unknown identifier verbatim on purpose --
        the module-wide rule is that hiding a fact about a run is worse than
        showing an untranslated word, and the browser does not get to overrule
        that for one field. So the honest thing to prove is not what the
        fallback would print, but that the fallback is unreachable: the store
        mints ``primary_action`` from a closed set, and that set is exactly the
        catalog. Adding an action without words for it fails here rather than
        in front of a user.
        """

        published = {
            value
            for name, value in vars(session_view).items()
            if name.startswith("ACTION_") and isinstance(value, str)
        }
        self.assertTrue(published)
        self.assertEqual(published, set(tui._SESSION_ACTION_TEXT))
        # And every short form names an action that exists.
        self.assertLessEqual(set(tui._SESSION_ACTION_SHORT), published)

    def test_the_longest_action_and_overflow_coexist_at_the_minimum(self) -> None:
        for english in (True, False):
            with self.subTest(english=english):
                text = tui._session_browser_footer(
                    self._row(ACTION_REVIEW_RISK),
                    english,
                    FOOTER_MINIMUM,
                    shown=100,
                    hidden=12,
                )
                self.assertLessEqual(len(text), FOOTER_MINIMUM)
                self.assertIn("12", text)
                self.assertNotIn("\u2026", text)

    def test_an_empty_list_still_offers_the_way_out(self) -> None:
        self.assertIn("Esc", tui._session_browser_footer(None, True, 40))

    def test_zero_width_means_unmeasured_not_zero_columns(self) -> None:
        """Before mount there is nothing to measure; say everything."""

        text = tui._session_browser_footer(
            self._row(ACTION_STOP), True, 0, shown=100, hidden=12
        )
        self.assertIn(tui._SESSION_ACTION_TEXT[ACTION_STOP][1], text)
        self.assertIn("Esc", text)


# ------------------------------------------------------------- 6. responsive


class ResponsiveLayoutTests(_CompactCase):
    """Three terminals, three amounts of information, one shape."""

    def _seed(self) -> None:
        for index, session_id in enumerate(("s-1", "s-2", "s-3")):
            self.harness.publish_state(
                session_id,
                status=tui.STATUS_RUNNING,
                task=f"fix the failing integration tests in module {index}",
                provider="openai",
                model="model-a",
                current_step="repo.edit_file",
            )

    async def test_the_browser_is_usable_in_a_46_by_14_terminal(self) -> None:
        self._seed()
        app = self.harness.app()
        async with app.run_test(size=NARROW) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            dialog = screen.query_one("#session-browser")
            self.assertLessEqual(dialog.size.width, NARROW[0])
            self.assertEqual(self._title(screen).count("\n"), 0)
            hint = self._hint(screen)
            self.assertEqual(hint.count("\n"), 0)
            self.assertLessEqual(len(hint), screen.footer_width())
            # Several rows visible, none of them wrapped.
            self.assertEqual(len(screen.rows()), 3)
            for session_id in ("s-1", "s-2", "s-3"):
                with self.subTest(session_id=session_id):
                    text = screen.row_text(session_id)
                    self.assertNotIn("\n", text)
                    self.assertLessEqual(len(text), screen.content_width())

    async def test_a_narrow_row_survives_the_language_switch(self) -> None:
        """The Russian status is longer, and the task budget has to notice."""

        self._seed()
        app = self.harness.app()
        async with app.run_test(size=NARROW) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            with patch.object(tui, "_save_language", lambda language: None):
                app._set_language("ru")
            await pilot.pause()
            for session_id in ("s-1", "s-2", "s-3"):
                with self.subTest(session_id=session_id):
                    self.assertLessEqual(
                        len(screen.row_text(session_id)), screen.content_width()
                    )
            self.assertLessEqual(len(self._hint(screen)), screen.footer_width())

    async def test_a_standard_terminal_reads_as_a_list_not_a_report(self) -> None:
        self._seed()
        app = self.harness.app()
        async with app.run_test(size=STANDARD) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            text = screen.row_text("s-1")
            self.assertTrue(text.startswith("fix the failing integration tests"))
            self.assertIn(tui._SESSION_STATUS_TEXT[tui.STATUS_RUNNING][1], text)
            self.assertIn(self._activity(tui.ACTIVITY_EDITING, True), text)
            # The model is a detail fact until the terminal is genuinely wide.
            self.assertNotIn("openai/model-a", text)
            self.assertLessEqual(len(text), screen.content_width())

    async def test_a_wide_terminal_does_not_stretch_the_browser(self) -> None:
        self._seed()
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            dialog = screen.query_one("#session-browser")
            # A ceiling, not a percentage: 113 columns of mostly whitespace
            # between the status and the model reads as a table.
            self.assertLess(dialog.size.width, WIDE[0])
            self.assertLessEqual(dialog.size.width, 100)
            text = screen.row_text("s-1")
            # Wide earns the model, and only whole.
            self.assertIn("openai/model-a", text)
            for absent in ("tokens=", "cost=", "files=", "errors=", "\u2026model"):
                with self.subTest(absent=absent):
                    self.assertNotIn(absent, text)

    async def test_the_layout_guarantees_the_footer_minimum(self) -> None:
        """What makes the pure sweep's floor a real number and not a wish."""

        self._seed()
        app = self.harness.app()
        async with app.run_test(size=NARROW) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertGreaterEqual(screen.footer_width(), FOOTER_MINIMUM)
            self.assertGreaterEqual(screen.content_width(), FOOTER_MINIMUM - 2)

    async def test_the_confirmation_action_is_whole_at_46_columns(self) -> None:
        self.harness.publish_state(
            "s-risk",
            status=tui.STATUS_RUNNING,
            task="a task with a fairly long description attached to it",
        )
        self.harness.publish_pending_risk("s-risk")
        app = self.harness.app()
        async with app.run_test(size=NARROW) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            for language in ("en", "ru"):
                with self.subTest(language=language):
                    with patch.object(tui, "_save_language", lambda value: None):
                        app._set_language(language)
                    await pilot.pause()
                    hint = self._hint(screen)
                    whole = tui._session_action_spellings(
                        ACTION_REVIEW_RISK, language == "en"
                    )
                    self.assertTrue(any(spelling in hint for spelling in whole))
                    self.assertNotIn("\u2026", hint)
                    self.assertLessEqual(len(hint), screen.footer_width())

    async def test_a_narrow_row_and_footer_are_each_one_line(self) -> None:
        self._seed()
        app = self.harness.app()
        async with app.run_test(size=NARROW) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            for widget in screen.query_one(
                "#session-rows", tui.VerticalScroll
            ).query(tui.Static):
                with self.subTest(row=str(widget.id)):
                    self.assertEqual(widget.size.height, 1)
            self.assertEqual(
                screen.query_one("#session-browser-hint", tui.Static).size.height, 1
            )
            self.assertEqual(
                screen.query_one("#session-browser-title", tui.Static).size.height, 1
            )

    async def test_a_narrow_overflow_footer_is_still_honest(self) -> None:
        total = tui.SessionBrowserScreen.MAX_ROWS + 7
        for index in range(total):
            self.harness.publish_state(f"s-{index:03d}", status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=NARROW) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            for language in ("en", "ru"):
                with self.subTest(language=language):
                    with patch.object(tui, "_save_language", lambda value: None):
                        app._set_language(language)
                    await pilot.pause()
                    hint = self._hint(screen)
                    # The count exists nowhere else on the screen, so it is not
                    # the field that gets dropped to make room.
                    self.assertIn("7", hint)
                    self.assertLessEqual(len(hint), screen.footer_width())
                    self.assertNotIn("\n", hint)

    def test_an_oversized_tail_drops_the_model_whole(self) -> None:
        """The optional field goes; the task keeps a readable minimum."""

        row = _summary_fixture(
            title="fix the failing integration tests in the workspace worker",
            status=tui.STATUS_RUNNING,
            current_step="repo.edit_file",
            elapsed_seconds=3725,
            provider="some-rather-long-provider-name",
            model="an-extremely-long-model-identifier-v2",
        )
        fields = tui._session_browser_fields(row, True, tui.BROWSER_WIDE)
        text = tui._session_browser_row_text(row, True, tui.BROWSER_WIDE)
        self.assertLessEqual(len(text), tui.BROWSER_WIDE)
        # Dropped whole rather than sliced: half a model name cannot be acted on.
        self.assertNotIn("an-extremely-long-model", text)
        self.assertNotIn("some-rather-long-provider-name", text)
        # The status survives, and the task keeps its floor.
        self.assertIn(tui._SESSION_STATUS_TEXT[tui.STATUS_RUNNING][1], text)
        self.assertGreaterEqual(len(fields[0]), tui.TASK_MIN_COLUMNS)

    async def test_a_resize_cycle_keeps_the_selection_and_remeasures(self) -> None:
        self._seed()
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            await pilot.press("down")
            self.assertEqual(screen.selected_session, "s-2")
            widths: list[int] = [screen.content_width()]
            for size in (NARROW, STANDARD, WIDE):
                await pilot.resize_terminal(*size)
                await pilot.pause()
                self.assertEqual(screen.selected_session, "s-2")
                widths.append(screen.content_width())
                for session_id in ("s-1", "s-2", "s-3"):
                    self.assertLessEqual(
                        len(screen.row_text(session_id)), screen.content_width()
                    )
            # Actually re-measured rather than cached from the first layout.
            self.assertGreater(len(set(widths)), 1)
            self.assertEqual(widths[0], widths[-1])

    async def test_the_measured_width_is_not_the_terminal_width(self) -> None:
        """The regression this whole policy exists for.

        ``app.size.width`` gave the renderer columns spent on the modal, the
        border, the padding, the scroll container and the row padding. It
        decided a field fitted, and Textual then wrapped the line.
        """

        self._seed()
        app = self.harness.app()
        async with app.run_test(size=NARROW) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertLess(screen.content_width(), NARROW[0])
            self.assertLess(screen.footer_width(), NARROW[0])
            # The footer is a direct child of the dialog and the rows are not,
            # so the two budgets are genuinely different numbers.
            self.assertNotEqual(screen.content_width(), screen.footer_width())


# ------------------------------------------------------- 8. redraw discipline


class FooterRedrawTests(_CompactCase):
    """The footer describes the selected row, so only that may repaint it."""

    async def test_a_dirty_neighbour_redraws_one_row_and_not_the_footer(self) -> None:
        for session_id in ("s-a", "s-b"):
            self.harness.publish_state(
                session_id, status=tui.STATUS_RUNNING, task=f"task {session_id}"
            )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertEqual(screen.selected_session, "s-a")
            before = self._hint(screen)

            redrawn: list[str] = []
            original = screen._update_row

            def record(row: object) -> None:
                redrawn.append(row.session_id)  # type: ignore[attr-defined]
                original(row)  # type: ignore[arg-type]

            with patch.object(screen, "_update_row", side_effect=record):
                with patch.object(
                    screen, "_update_hint", wraps=screen._update_hint
                ) as hint:
                    self.harness.publish_state(
                        "s-b", status=tui.STATUS_COMPLETED, task="task s-b"
                    )
                    self._drain(app)
            self.assertEqual(redrawn, ["s-b"])
            hint.assert_not_called()
            self.assertEqual(self._hint(screen), before)

    async def test_a_dirty_selected_row_does_refresh_the_footer(self) -> None:
        """The positive control: the discipline must not become a freeze."""

        self.harness.publish_state(
            "s-a", tui.SUMMARY_RUN_STARTED, status=tui.STATUS_RUNNING, task="task a"
        )
        app = self.harness.app("s-a")
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            app.agent_busy = True
            screen = await self._open(app, pilot)
            self.assertIn(tui._SESSION_ACTION_TEXT[ACTION_STOP][1], self._hint(screen))
            self.harness.publish_state(
                "s-a", tui.SUMMARY_RUN_COMPLETED, status=tui.STATUS_COMPLETED
            )
            self._drain(app)
            await pilot.pause()
            self.assertIn(tui._SESSION_ACTION_TEXT[ACTION_OPEN][1], self._hint(screen))

    async def test_moving_the_cursor_refreshes_the_footer(self) -> None:
        self.harness.publish_state(
            "s-1", tui.SUMMARY_RUN_COMPLETED, status=tui.STATUS_COMPLETED
        )
        self.harness.publish_state(
            "s-2",
            tui.SUMMARY_RUN_STOPPED,
            status=tui.STATUS_STOPPED,
            waiting_reason="no_changes",
        )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertIn(tui._SESSION_ACTION_TEXT[ACTION_OPEN][1], self._hint(screen))
            await pilot.press("down")
            await pilot.pause()
            self.assertIn(
                tui._SESSION_ACTION_TEXT[ACTION_RESUME][1], self._hint(screen)
            )


# ------------------------------------------------------------- 9. localization


class LanguageRefreshTests(_CompactCase):
    async def test_every_localized_surface_follows_the_switch(self) -> None:
        """Including the title, which ``compose`` writes once and never again."""

        self.harness.publish_state(
            "s-lang",
            tui.SUMMARY_RUN_STOPPED,
            status=tui.STATUS_STOPPED,
            task="fix the failing tests",
            waiting_reason="no_changes",
        )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            english_title = self._title(screen)
            self.assertEqual(english_title, "Sessions")
            widgets = len(screen.query(tui.Static))

            with patch.object(tui, "_save_language", lambda language: None):
                app._set_language("ru")
            await pilot.pause()
            russian_title = self._title(screen)
            self.assertNotEqual(russian_title, english_title)
            self.assertIn(
                tui._SESSION_STATUS_TEXT[tui.STATUS_STOPPED][0],
                screen.row_text("s-lang"),
            )
            self.assertIn(
                tui._SESSION_ACTION_TEXT[ACTION_RESUME][0], self._hint(screen)
            )

            with patch.object(tui, "_save_language", lambda language: None):
                app._set_language("en")
            await pilot.pause()
            self.assertEqual(self._title(screen), english_title)
            self.assertIn(
                tui._SESSION_STATUS_TEXT[tui.STATUS_STOPPED][1],
                screen.row_text("s-lang"),
            )
            # No widget was created or destroyed on the way: this is a redraw,
            # not a rebuild, which is why the selection and scroll survive.
            self.assertEqual(len(screen.query(tui.Static)), widgets)

    async def test_the_switch_does_not_move_the_cursor(self) -> None:
        for session_id in ("s-1", "s-2", "s-3"):
            self.harness.publish_state(session_id, status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            await pilot.press("down")
            self.assertEqual(screen.selected_session, "s-2")
            with patch.object(tui, "_save_language", lambda language: None):
                app._set_language("ru")
            await pilot.pause()
            self.assertEqual(screen.selected_session, "s-2")

    async def test_the_empty_state_is_written_in_both_languages(self) -> None:
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertEqual(
                self._hint(screen), "No sessions yet. Start a task in chat."
            )
            with patch.object(tui, "_save_language", lambda language: None):
                app._set_language("ru")
            await pilot.pause()
            self.assertEqual(
                self._hint(screen),
                "\u0421\u0435\u0441\u0441\u0438\u0439 \u043f\u043e\u043a\u0430 "
                "\u043d\u0435\u0442. \u041d\u0430\u0447\u043d\u0438\u0442\u0435 "
                "\u0437\u0430\u0434\u0430\u0447\u0443 \u0432 \u0447\u0430\u0442\u0435.",
            )


# ---------------------------------------------------------------- 10. security


class PlantedPayloadTests(_CompactCase):
    """Nine hostile fixtures, mounted, read back from the real widgets.

    A user's task is *not* hidden -- it is the headline of the row and hiding
    it would break the screen's only job. It is shown as plain one-line text,
    which is a different guarantee and the one worth testing.
    """

    HOSTILE_TASK = (
        "drop tables\nsecond line\tafter a tab\x07"
        "\x1b[31mred\x1b[0m [bold red]markup[/] " + FAKE_KEY_SHAPED
    )
    LONG_ID = "task-1754300000-" + ("z" * 40)

    async def test_a_hostile_row_stays_one_plain_line(self) -> None:
        self.harness.publish_state(
            self.LONG_ID,
            "\x1b[2J[bold]hostile summary[/]",
            status=tui.STATUS_RUNNING,
            task=self.HOSTILE_TASK,
            current_step="\x1b[2Jrm -rf /",
        )
        self.harness.publish_pending_risk(self.LONG_ID, token=True)
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            text = screen.row_text(self.LONG_ID)

            # One visual line, no control characters, no escape sequences.
            for forbidden in ("\n", "\r", "\t", "\x1b", "\x07"):
                with self.subTest(forbidden=repr(forbidden)):
                    self.assertNotIn(forbidden, text)

            # Markup is inert rather than filtered: the widget is built with
            # `markup=False`, so brackets are characters and not instructions.
            widget = screen.query_one("#session-rows", tui.VerticalScroll).query(
                tui.Static
            )
            self.assertTrue(all(item._render_markup is False for item in widget))

            # None of the planted payloads reaches the reader.
            for absent in (
                FAKE_APPROVAL_TOKEN,
                "rm -rf",
                "hostile summary",
                self.LONG_ID,
            ):
                with self.subTest(absent=absent[:16]):
                    self.assertNotIn(absent, text)

            # And the footer is not a second surface for them.
            hint = self._hint(screen)
            for absent in (FAKE_APPROVAL_TOKEN, "rm -rf", "\x1b", self.LONG_ID):
                with self.subTest(footer=absent[:16]):
                    self.assertNotIn(absent, hint)

    async def test_a_key_shaped_task_is_shown_safely_rather_than_hidden(self) -> None:
        """The row's job is to identify the session the person started."""

        self.harness.publish_state(
            "s-key", status=tui.STATUS_RUNNING, task="rotate " + FAKE_KEY_SHAPED
        )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            text = screen.row_text("s-key")
            self.assertTrue(text.startswith("rotate "))
            self.assertNotIn("\n", text)
            self.assertLessEqual(len(text), screen.content_width())

    async def test_a_multiline_task_does_not_push_the_rows_below_it_down(
        self,
    ) -> None:
        self.harness.publish_state(
            "s-multi", status=tui.STATUS_RUNNING, task="first line\nsecond line"
        )
        self.harness.publish_state("s-next", status=tui.STATUS_RUNNING, task="next")
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertIn("first line second line", screen.row_text("s-multi"))
            rows = screen.query_one("#session-rows", tui.VerticalScroll).query(
                tui.Static
            )
            for widget in rows:
                with self.subTest(widget=str(widget.id)):
                    self.assertEqual(widget.size.height, 1)


# ------------------------------------------------- 12/13. selection, overflow


class SelectionStabilityTests(_CompactCase):
    async def test_the_initial_selection_is_honoured(self) -> None:
        for session_id in ("s-1", "s-2", "s-3"):
            self.harness.publish_state(session_id, status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            # The application hands the browser the session the user was last
            # looking at, which is what makes returning from Session Detail put
            # the cursor back rather than at the top.
            screen = tui.SessionBrowserScreen(
                app._view_store, language="en", initial_selection="s-3"
            )
            app.push_screen(screen)
            await pilot.pause()
            self.assertEqual(screen.selected_session, "s-3")

            # Without one, the first row in the stable order.
            plain = tui.SessionBrowserScreen(app._view_store, language="en")
            app.push_screen(plain)
            await pilot.pause()
            self.assertEqual(plain.selected_session, "s-1")

    async def test_a_resize_does_not_move_the_cursor(self) -> None:
        for session_id in ("s-1", "s-2", "s-3"):
            self.harness.publish_state(
                session_id, status=tui.STATUS_RUNNING, task="a reasonably long task"
            )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            await pilot.press("down")
            self.assertEqual(screen.selected_session, "s-2")
            await pilot.resize_terminal(*NARROW)
            await pilot.pause()
            self.assertEqual(screen.selected_session, "s-2")
            # And the rows were re-rendered under the new budget.
            self.assertLessEqual(
                len(screen.row_text("s-2")), screen.content_width()
            )

    async def test_a_vanished_selected_row_hands_over_to_its_neighbour(self) -> None:
        for session_id in ("s-1", "s-2", "s-3"):
            self.harness.publish_state(session_id, status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            await pilot.press("down")
            self.assertEqual(screen.selected_session, "s-2")

            survivors = tuple(
                row
                for row in app._view_store.summaries()
                if row.session_id != "s-2"
            )
            with patch.object(
                app._view_store, "summaries", return_value=survivors
            ):
                screen.apply_changes(())
                await pilot.pause()
                # The one after it, not the top of the list.
                self.assertEqual(screen.selected_session, "s-3")

                last_gone = tuple(
                    row for row in survivors if row.session_id != "s-3"
                )
                with patch.object(
                    app._view_store, "summaries", return_value=last_gone
                ):
                    screen.apply_changes(())
                    await pilot.pause()
                    # Nothing after it: fall back to the one before.
                    self.assertEqual(screen.selected_session, "s-1")

                    with patch.object(
                        app._view_store, "summaries", return_value=()
                    ):
                        screen.apply_changes(())
                        await pilot.pause()
                        self.assertIsNone(screen.selected_session)
                        self.assertIn("No sessions yet", self._hint(screen))

    async def test_the_footer_always_describes_the_selected_row(self) -> None:
        self.harness.publish_state(
            "s-1", tui.SUMMARY_RUN_COMPLETED, status=tui.STATUS_COMPLETED
        )
        self.harness.publish_state(
            "s-2",
            tui.SUMMARY_RUN_STOPPED,
            status=tui.STATUS_STOPPED,
            waiting_reason="no_changes",
        )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            for _ in range(4):
                selected = screen.selected_session
                assert selected is not None
                rows = {row.session_id: row for row in screen.rows()}
                expected = tui._SESSION_ACTION_TEXT[rows[selected].primary_action][1]
                self.assertIn(expected, self._hint(screen))
                await pilot.press("down")
                await pilot.pause()


class OverflowTests(_CompactCase):
    async def test_the_hidden_count_is_accurate_and_said_in_one_line(self) -> None:
        total = tui.SessionBrowserScreen.MAX_ROWS + 5
        for index in range(total):
            self.harness.publish_state(f"s-{index:03d}", status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertEqual(len(screen.rows()), tui.SessionBrowserScreen.MAX_ROWS)
            # The newest by the stable id order, not an arbitrary window.
            self.assertEqual(screen.rows()[-1].session_id, f"s-{total - 1:03d}")
            hint = self._hint(screen)
            self.assertIn("5", hint)
            self.assertIn("older", hint)
            self.assertNotIn("\n", hint)
            self.assertLessEqual(len(hint), screen.footer_width())
            self.assertIsNotNone(screen.selected_session)


class EmptyStatePolicyTests(_CompactCase):
    """An empty browser is still a footer, and still gets one line.

    This branch used to write its sentence straight into the widget, bypassing
    the width budget entirely. With ``height: 1`` on the hint that is not a
    cosmetic problem: the Russian sentence is long enough to be cut by Textual
    at whatever column the layout happens to end on, which is the one kind of
    truncation nobody chose.
    """

    def test_the_policy_degrades_to_a_whole_sentence(self) -> None:
        for english in (True, False):
            forms = [
                pair[1] if english else pair[0] for pair in tui._BROWSER_EMPTY_TEXT
            ]
            with self.subTest(english=english):
                # Wide: the full instruction.
                self.assertEqual(tui._browser_empty_text(english, 120), forms[0])
                # Narrow: a shorter *complete* sentence, not a slice.
                narrow = tui._browser_empty_text(english, len(forms[0]) - 1)
                self.assertEqual(narrow, forms[-1])
                # Absurdly narrow: still whole. The binding works regardless.
                self.assertEqual(tui._browser_empty_text(english, 4), forms[-1])
                for width in range(8, 121):
                    text = tui._browser_empty_text(english, width)
                    self.assertIn(text, forms)
                    self.assertNotIn("\u2026", text)
                    self.assertNotIn("\n", text)

    async def _assert_empty_footer(
        self, size: tuple[int, int], language: str
    ) -> None:
        app = self.harness.app()
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            if language == "ru":
                with patch.object(tui, "_save_language", lambda value: None):
                    app._set_language("ru")
                await pilot.pause()
            text = self._hint(screen)
            forms = [
                pair[1] if language == "en" else pair[0]
                for pair in tui._BROWSER_EMPTY_TEXT
            ]
            # A catalogued whole sentence, inside the measured budget, on one
            # line -- not a particular sentence, because which one is chosen is
            # a function of the width and asserting the answer would pin the
            # padding rather than the contract.
            self.assertIn(text, forms)
            self.assertLessEqual(len(text), screen.footer_width())
            self.assertNotIn("\n", text)
            self.assertEqual(
                screen.query_one("#session-browser-hint", tui.Static).size.height, 1
            )
            # Nothing is selected, and no identifier is left over from before.
            self.assertIsNone(screen.selected_session)
            self.assertEqual(screen.rows(), ())

    async def test_empty_english_at_46_by_14(self) -> None:
        await self._assert_empty_footer(NARROW, "en")

    async def test_empty_russian_at_46_by_14(self) -> None:
        await self._assert_empty_footer(NARROW, "ru")

    async def test_empty_english_at_80_by_24(self) -> None:
        await self._assert_empty_footer(STANDARD, "en")

    async def test_empty_russian_at_80_by_24(self) -> None:
        await self._assert_empty_footer(STANDARD, "ru")

    async def test_an_empty_browser_survives_a_language_round_trip(self) -> None:
        app = self.harness.app()
        async with app.run_test(size=NARROW) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            english = self._hint(screen)
            for language in ("ru", "en"):
                with patch.object(tui, "_save_language", lambda value: None):
                    app._set_language(language)
                await pilot.pause()
                text = self._hint(screen)
                forms = [
                    pair[1] if language == "en" else pair[0]
                    for pair in tui._BROWSER_EMPTY_TEXT
                ]
                self.assertIn(text, forms)
                self.assertLessEqual(len(text), screen.footer_width())
                self.assertIsNone(screen.selected_session)
            self.assertEqual(self._hint(screen), english)

    async def test_escape_still_closes_a_browser_with_no_esc_hint(self) -> None:
        """The hint is advertising; the binding is the contract."""

        app = self.harness.app()
        async with app.run_test(size=NARROW) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertNotIn("Esc", self._hint(screen))
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, tui.SessionBrowserScreen)


class SessionsCompatibilityTests(_CompactCase):
    """C changed the browser and must not have changed ``/sessions``.

    Through the real command path -- ``_inspection_finished`` is what the
    worker calls when ``karox session list --json`` returns -- rather than by
    calling the renderer directly, because the question is whether the product
    still publishes the verbose form, not whether the function still exists.
    """

    async def _live_block(self, app: tui.KaroXApp) -> str:
        written: list[str] = []
        with patch.object(app, "_write", side_effect=written.append):
            app._inspection_finished("/sessions", 0, "[]")
        return "\n".join(written)

    async def test_the_verbose_row_survives_in_both_languages(self) -> None:
        self.harness.publish_state(
            "task-1754300000-abcdef12",
            tui.SUMMARY_RUN_STOPPED,
            status=tui.STATUS_STOPPED,
            task="fix the failing tests",
            waiting_reason="no_changes",
            access_profile=AccessProfile.WORKSPACE_WRITE.value,
            current_step="repo.edit_file",
        )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            for language, index in (("en", 1), ("ru", 0)):
                with self.subTest(language=language):
                    with patch.object(tui, "_save_language", lambda value: None):
                        app._set_language(language)
                    await pilot.pause()
                    block = await self._live_block(app)
                    # Everything the compact row dropped is still published here.
                    self.assertIn("task-1754300000-abcdef12", block)
                    self.assertIn(
                        tui._EVENT_SUMMARY_TEXT[tui.SUMMARY_RUN_STOPPED][index], block
                    )
                    self.assertIn(
                        tui._SESSION_ACTION_TEXT[ACTION_RESUME][index], block
                    )
                    self.assertIn(
                        tui._SESSION_STATUS_TEXT[tui.STATUS_STOPPED][index], block
                    )
                    self.assertIn("repo.edit_file", block)

    async def test_the_browser_shows_none_of_what_sessions_shows(self) -> None:
        self.harness.publish_state(
            "task-1754300000-abcdef12",
            tui.SUMMARY_RUN_STOPPED,
            status=tui.STATUS_STOPPED,
            task="fix the failing tests",
            waiting_reason="no_changes",
            access_profile=AccessProfile.WORKSPACE_WRITE.value,
            current_step="repo.edit_file",
        )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            row = screen.row_text("task-1754300000-abcdef12")
            for absent in (
                "task-1754300000-abcdef12",
                AccessProfile.WORKSPACE_WRITE.value,
                "repo.edit_file",
                tui._EVENT_SUMMARY_TEXT[tui.SUMMARY_RUN_STOPPED][1],
                tui._SESSION_ACTION_TEXT[ACTION_RESUME][1],
            ):
                with self.subTest(absent=absent):
                    self.assertNotIn(absent, row)
            # Same store, same session, two products.
            self.assertIn("fix the failing tests", row)

    def test_the_two_renderers_are_separate_functions(self) -> None:
        self.assertIsNot(tui._session_row_text, tui._session_browser_row_text)
        row = _summary_fixture(
            title="fix the failing tests",
            status=tui.STATUS_STOPPED,
            primary_action=ACTION_RESUME,
        )
        verbose = tui._session_row_text(row, True, verbose=True)
        compact = tui._session_browser_row_text(row, True, 88)
        self.assertNotEqual(verbose, compact)
        # Different presentation, one source of words.
        status = tui._SESSION_STATUS_TEXT[tui.STATUS_STOPPED][1]
        self.assertIn(status, verbose)
        self.assertIn(status, compact)


def _summary_fixture(**overrides: object) -> SessionSummary:
    """A view model with the fields a presentation test wants and no store.

    Built by replacing fields on a default ``SessionSummary`` so a new field
    added to the view model cannot silently miss these tests.
    """

    base = SessionSummary(session_id="s-fixture")
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
