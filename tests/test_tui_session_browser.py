"""The Session Browser is a real screen over ``SessionViewStore``.

Every test here opens the browser the way the product opens it -- through
``KaroXApp.action_session_browser`` on an application wired to a private event
bus and a private session directory -- and then asserts on the screen's own
state: which rows exist, in what order, which one is selected, which single
action a row offers, and that updating one session redraws one row.

No test matches prose written by hand. Every word a row shows comes from the UI
catalogs in :mod:`karox.tui`, so the assertions compare against those catalogs; a
test pinning a Russian sentence would fail the day the wording improves and would
prove nothing about the second language.

The token-shaped string below is a fixture proving a confirmation token cannot
reach a row. It is not a credential.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox import tui
from karox.event_bus import EventBus, EventKind, EventLevel
from karox.models import AccessProfile
from karox.session_view import (
    ACTION_OPEN,
    ACTION_RESUME,
    ACTION_REVIEW_RISK,
    ACTION_STOP,
)
from karox.sessions import SessionStore

FAKE_APPROVAL_TOKEN = "appr-" + ("q" * 30)


class _Harness:
    """An application wired to a private bus and a private session directory.

    The process-wide bus is a singleton, so tests publishing into it would leak
    events into each other. Patching where the application looks the bus up also
    proves the application really does look it up instead of building its own.
    """

    def __init__(self, stack: unittest.TestCase) -> None:
        self.bus = EventBus()
        self.root = Path(
            stack.enterContext(tempfile.TemporaryDirectory())  # type: ignore[attr-defined]
        )
        self.sessions = SessionStore(self.root)
        stack.enterContext(  # type: ignore[attr-defined]
            patch.object(tui, "event_bus", lambda: self.bus)
        )
        stack.enterContext(  # type: ignore[attr-defined]
            patch.object(tui, "session_dir", lambda: self.root)
        )
        stack.enterContext(  # type: ignore[attr-defined]
            patch.object(tui, "_load_language", return_value="en")
        )
        stack.enterContext(  # type: ignore[attr-defined]
            patch.object(tui, "_selected_model", return_value=None)
        )

    def app(self, session_id: str | None = None) -> tui.KaroXApp:
        return tui.KaroXApp(Path.cwd(), session_id=session_id, language="en")

    def create_session(self, session_id: str, *, task: str = "durable task") -> None:
        self.sessions.create(
            session_id=session_id,
            repository=Path.cwd(),
            branch="main",
            access_profile=AccessProfile.WORKSPACE_WRITE,
            task=task,
        )

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
        """A verdict that stops the agent and waits on the person at the screen."""

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


class _BrowserCase(unittest.IsolatedAsyncioTestCase):
    """Shared plumbing: open the browser and drive the one drain point."""

    def setUp(self) -> None:
        self.harness = _Harness(self)

    async def _open(self, app: tui.KaroXApp, pilot: object) -> tui.SessionBrowserScreen:
        app.action_session_browser()
        await pilot.pause()  # type: ignore[attr-defined]
        screen = app.screen
        assert isinstance(screen, tui.SessionBrowserScreen)
        return screen

    def _drain(self, app: tui.KaroXApp) -> None:
        """One refresh tick: the application drains and forwards the dirty set."""

        app._drain_session_view()

    def _ids(self, screen: tui.SessionBrowserScreen) -> tuple[str, ...]:
        return tuple(row.session_id for row in screen.rows())

    def _selected_action(self, screen: tui.SessionBrowserScreen) -> str:
        selected = screen.selected_session
        assert selected is not None
        rows = {row.session_id: row for row in screen.rows()}
        return rows[selected].primary_action

    def _action_words(self, action: str, english: bool) -> str:
        return tui._SESSION_ACTION_TEXT[action][1 if english else 0]


class SessionListTests(_BrowserCase):
    async def test_several_sessions_are_listed_in_a_deterministic_order(self) -> None:
        for session_id in ("s-charlie", "s-alpha", "s-bravo"):
            self.harness.publish_state(session_id, status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertEqual(self._ids(screen), ("s-alpha", "s-bravo", "s-charlie"))
            # Stable under activity: the store sorts by last event, which would
            # move a row out from under the cursor, so the list sorts by id.
            self.harness.publish_state("s-bravo", status=tui.STATUS_COMPLETED)
            self._drain(app)
            self.assertEqual(self._ids(screen), ("s-alpha", "s-bravo", "s-charlie"))

    async def test_a_persisted_only_session_is_listed_before_any_event(self) -> None:
        """Durable startup backfill, not a second realtime source."""

        self.harness.create_session("s-disk", task="durable task")
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertEqual(self._ids(screen), ("s-disk",))
            row = app._view_store.summary("s-disk")
            assert row is not None
            self.assertEqual(row.last_event_seq, 0)
            text = screen.row_text("s-disk")
            # C. The task leads; the internal id and the access profile are
            # Session Detail's business. A person choosing a session picks it
            # by what it was asked to do.
            self.assertTrue(text.startswith("durable task"))
            self.assertNotIn("s-disk", text)
            self.assertNotIn(AccessProfile.WORKSPACE_WRITE.value, text)
            self.assertNotIn("AccessProfile.", text)

    async def test_the_list_is_bounded(self) -> None:
        for session_id in ("s-1", "s-2", "s-3"):
            self.harness.publish_state(session_id, status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            screen.MAX_ROWS = 2  # type: ignore[misc]
            screen.apply_changes(())
            await pilot.pause()
            self.assertEqual(self._ids(screen), ("s-2", "s-3"))
            self.assertEqual(screen.row_text("s-1"), "")

    async def test_the_browser_uses_the_compact_renderer(self) -> None:
        """C. Two presentation contracts over one set of facts.

        Until C the browser and the ``/sessions`` block shared a renderer, and
        that was the right call while both were lists of everything. They are
        no longer the same product: the browser answers "which session", and a
        block printed into the chat is a record of what happened. Forcing them
        back onto one function would mean one of the two is wrong.

        What must stay shared is the *source*: one `SessionSummary`, one set of
        status catalogs. Different words for the same fact is the failure this
        replaces, and it is asserted separately below.
        """

        self.harness.publish_state(
            "s-shared",
            tui.SUMMARY_RUN_STOPPED,
            status=tui.STATUS_STOPPED,
            waiting_reason="step_limit",
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            # Freeze the view store's clock before the screen renders. The row
            # in the browser renders when the screen opens; the expected string
            # below renders at assert time. A stopped-but-resumable run still
            # measures elapsed against "now", so on a slow (coverage-
            # instrumented) run the two renders can land on different seconds
            # and disagree by "0s" vs "1s" -- a wall-clock fact, not the shared
            # source this test pins.
            frozen = float(app._view_store._now())
            app._view_store._now = lambda: frozen
            screen = await self._open(app, pilot)
            row = app._view_store.summary("s-shared")
            assert row is not None
            self.assertEqual(
                screen.row_text("s-shared"),
                tui._session_browser_row_text(row, True, screen.content_width()),
            )

    async def test_the_sessions_block_keeps_its_verbose_renderer(self) -> None:
        """``/sessions`` is compatibility surface and C did not touch it."""

        self.harness.publish_state(
            "s-verbose",
            tui.SUMMARY_RUN_STOPPED,
            status=tui.STATUS_STOPPED,
            task="fix the failing tests",
            waiting_reason="step_limit",
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            row = app._view_store.summary("s-verbose")
            assert row is not None
            verbose = tui._session_row_text(row, True, verbose=True)

        # Everything the compact row drops is still available here.
        self.assertIn("s-verbose", verbose)
        self.assertIn(tui._SESSION_ACTION_TEXT[ACTION_RESUME][1], verbose)
        self.assertIn(tui._EVENT_SUMMARY_TEXT[tui.SUMMARY_RUN_STOPPED][1], verbose)

    async def test_both_renderers_agree_about_the_status_word(self) -> None:
        """Different fields, never a different meaning for the same fact."""

        self.harness.publish_state(
            "s-agree", tui.SUMMARY_RUN_STOPPED, status=tui.STATUS_STOPPED
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            row = app._view_store.summary("s-agree")
            assert row is not None
            status = tui._SESSION_STATUS_TEXT[tui.STATUS_STOPPED][1]
            self.assertIn(status, screen.row_text("s-agree"))
            self.assertIn(status, tui._session_row_text(row, True, verbose=True))


class IncrementalRedrawTests(_BrowserCase):
    async def test_only_the_changed_row_is_redrawn(self) -> None:
        self.harness.publish_state("s-quiet", status=tui.STATUS_RUNNING)
        self.harness.publish_state("s-busy", status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            # Freeze the store's clock: the row equality at the end of this
            # test compares a screen render against a fresh render, and a
            # running session measures elapsed against "now" -- on a slow
            # (coverage-instrumented) run the two renders can disagree by one
            # second, which is not what this test pins.
            frozen = float(app._view_store._now())
            app._view_store._now = lambda: frozen
            screen = await self._open(app, pilot)
            redrawn: list[str] = []
            with patch.object(
                screen,
                "_update_row",
                side_effect=lambda row: redrawn.append(row.session_id),
            ):
                for index in range(20):
                    self.harness.publish_state(
                        "s-busy",
                        status=tui.STATUS_RUNNING,
                        current_step=f"step-{index}",
                    )
                # The drain tick is the debounce: twenty events cost one redraw
                # of one row, and no other row is touched at all.
                self._drain(app)
                self.assertEqual(redrawn, ["s-busy"])
                # Nothing changed since, so the next tick redraws nothing.
                self._drain(app)
                self.assertEqual(redrawn, ["s-busy"])
            screen.apply_changes(("s-busy",))
            # The store holds the latest step; the row does not show it. A
            # tool identifier is not a fact a person chooses a session by.
            row = app._view_store.summary("s-busy")
            assert row is not None
            self.assertEqual(row.current_step, "step-19")
            text = screen.row_text("s-busy")
            self.assertNotIn("step-19", text)
            self.assertEqual(
                text,
                tui._session_browser_row_text(row, True, screen.content_width()),
            )

    async def test_the_screen_does_not_drain_dirty_marks_itself(self) -> None:
        """One drain point, or the status bar and the browser rob each other."""

        app = self.harness.app("s-both")
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.harness.publish_state(
                "s-both", status=tui.STATUS_RUNNING, current_step="apply_patch"
            )
            self.assertEqual(app._view_store.dirty, ("s-both",))
            screen.apply_changes(("s-both",))
            # The screen rendered and the status bar's drain is untouched.
            self.assertEqual(app._view_store.dirty, ("s-both",))
            self._drain(app)
            self.assertEqual(app._view_store.dirty, ())
            # Same rule: the store knows the tool, the row speaks human.
            row = app._view_store.summary("s-both")
            assert row is not None
            self.assertEqual(row.current_step, "apply_patch")
            self.assertNotIn("apply_patch", screen.row_text("s-both"))

    async def test_a_render_fault_does_not_stop_the_event_publisher(self) -> None:
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            with patch.object(
                screen, "apply_changes", side_effect=RuntimeError("widget")
            ):
                self.harness.publish_state("s-broken", status=tui.STATUS_RUNNING)
                self._drain(app)
            row = app._view_store.summary("s-broken")
            assert row is not None
            self.assertEqual(row.status, tui.STATUS_RUNNING)
            self.assertEqual(self.harness.bus.subscriber_errors, 0)


class PrimaryActionTests(_BrowserCase):
    """Exactly one action per row, chosen by the store, drawn by the catalog."""

    def _assert_action_only_in_footer(
        self, screen: object, session_id: str, expected: str
    ) -> None:
        """The action is named once, in the footer, and never on the row.

        C moved it there. Repeating Stop/Resume/Open on every line turned the
        list into a wall of buttons, and the one that matters is the one under
        the cursor. The store still decides *which* action; this only pins
        where it is said.
        """

        words = self._action_words(expected, True)
        self.assertNotIn(words, screen.row_text(session_id))
        footer = str(screen.query_one("#session-browser-hint").render())
        self.assertIn(words, footer)
        self.assertEqual(footer.count(words), 1)

    async def test_a_running_session_offers_stop(self) -> None:
        self.harness.publish_state(
            "s-run", tui.SUMMARY_RUN_STARTED, status=tui.STATUS_RUNNING
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertEqual(self._selected_action(screen), ACTION_STOP)
            self._assert_action_only_in_footer(screen, "s-run", ACTION_STOP)

    async def test_a_stopped_session_offers_resume(self) -> None:
        self.harness.publish_state(
            "s-stop",
            tui.SUMMARY_RUN_STOPPED,
            status=tui.STATUS_STOPPED,
            waiting_reason="no_changes",
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertEqual(self._selected_action(screen), ACTION_RESUME)
            self._assert_action_only_in_footer(screen, "s-stop", ACTION_RESUME)

    async def test_a_waiting_session_offers_resume(self) -> None:
        self.harness.publish_state("s-wait", status="waiting")
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertEqual(self._selected_action(screen), ACTION_RESUME)

    async def test_a_completed_session_offers_open(self) -> None:
        self.harness.publish_state(
            "s-done", tui.SUMMARY_RUN_COMPLETED, status=tui.STATUS_COMPLETED
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertEqual(self._selected_action(screen), ACTION_OPEN)
            self._assert_action_only_in_footer(screen, "s-done", ACTION_OPEN)

    async def test_a_failed_session_offers_open_and_hides_the_error_count(self) -> None:
        self.harness.bus.publish(
            EventKind.ERROR,
            session_id="s-fail",
            summary=tui.SUMMARY_RUN_FAILED,
            level=EventLevel.ERROR,
            data={"code": tui.ERROR_AGENT_FAILED, "reason": "provider_error"},
        )
        self.harness.publish_state(
            "s-fail", tui.SUMMARY_RUN_FAILED, status=tui.STATUS_FAILED
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertEqual(self._selected_action(screen), ACTION_OPEN)
            # C. "failed" is what a person needs to choose the session; how
            # many errors it produced is what they need after opening it.
            text = screen.row_text("s-fail")
            self.assertNotIn("errors=", text)
            self.assertIn(tui._SESSION_STATUS_TEXT[tui.STATUS_FAILED][1], text)
            row = app._view_store.summary("s-fail")
            assert row is not None
            self.assertEqual(row.error_count, 1)

    async def test_a_pending_confirmation_outranks_everything(self) -> None:
        self.harness.publish_state("s-risk", status=tui.STATUS_RUNNING)
        self.harness.publish_pending_risk("s-risk")
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            # A run stopped and waiting on the person reading the screen is the
            # only state that outranks a live one.
            self.assertEqual(self._selected_action(screen), ACTION_REVIEW_RISK)
            # The activity column says a confirmation is pending, because that
            # is the state waiting on the reader; the action itself is offered
            # once, in the footer.
            self.assertIn(
                self._action_words(ACTION_REVIEW_RISK, True),
                screen.row_text("s-risk"),
            )
            self.assertIn(
                self._action_words(ACTION_REVIEW_RISK, True),
                str(screen.query_one("#session-browser-hint").render()),
            )

    async def test_no_confirmation_token_reaches_a_row(self) -> None:
        self.harness.publish_state("s-secret", status=tui.STATUS_RUNNING)
        self.harness.publish_pending_risk("s-secret", token=True)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertNotIn(FAKE_APPROVAL_TOKEN, screen.row_text("s-secret"))
            self.assertNotIn("appr-", screen.row_text("s-secret"))
            self.assertNotIn(FAKE_APPROVAL_TOKEN, repr(app._view_store.to_dict()))


class RowFieldTests(_BrowserCase):
    async def test_a_row_carries_the_facts_a_person_chooses_a_session_by(self) -> None:
        self.harness.publish_state(
            "s-full",
            tui.SUMMARY_RUN_STARTED,
            status=tui.STATUS_RUNNING,
            task="fix the failing tests",
            provider="openai",
            model="model-a",
            access_profile=AccessProfile.WORKSPACE_WRITE.value,
            workspace_mode="worktree",
            current_step="repo_edit_file",
            changed_files=["a.py", "b.py"],
        )
        self.harness.bus.publish(
            EventKind.AGENT_ACTION,
            session_id="s-full",
            summary=tui.SUMMARY_RUN_USAGE,
            data={
                "usage": {"total_tokens": 1500},
                "cost": {"usd": 0.42},
                "budgets": {
                    "usage": {"used": 1500, "limit": 8000, "unit": "tokens"},
                    "cost": {"used": 0.42, "limit": 5.0, "unit": "usd"},
                },
            },
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            text = screen.row_text("s-full")
            # What a person needs in order to *choose* this session.
            self.assertTrue(text.startswith("fix the failing tests"))
            self.assertIn("running", text)
            self.assertIn("openai/model-a", text)
            # And what they only need after choosing it. Every one of these is
            # still in Session Detail; none belongs in a list a person scans.
            for absent in (
                "s-full",
                AccessProfile.WORKSPACE_WRITE.value,
                "worktree",
                "repo_edit_file",
                "files=",
                "tokens=",
                "cost=",
                "0.42",
                "errors=",
            ):
                with self.subTest(absent=absent):
                    self.assertNotIn(absent, text)

    async def test_the_workspace_mode_is_absent_whether_or_not_it_is_known(
        self,
    ) -> None:
        """C retired this field from the browser, and its placeholder with it.

        The old contract was right for a verbose row: an unknown safety fact
        must not read as a safe default, so it drew a dash. A compact row does
        not show the fact at all, and a dash standing in for something the row
        never promised is just noise -- the reader cannot tell what is missing.
        Session Detail still makes the distinction.
        """

        self.harness.publish_state("s-mode", status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertNotIn(tui.UNKNOWN_FIELD, screen.row_text("s-mode"))
            self.harness.publish_state(
                "s-mode", status=tui.STATUS_RUNNING, workspace_mode="worktree"
            )
            self._drain(app)
            self.assertNotIn("worktree", screen.row_text("s-mode"))
            # Still recorded, still available to the detail screen.
            row = app._view_store.summary("s-mode")
            assert row is not None
            self.assertEqual(row.workspace_mode, "worktree")

    async def test_usage_and_cost_are_absent_until_measured(self) -> None:
        self.harness.publish_state("s-free", status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            text = screen.row_text("s-free")
            # A zero would read as "this run was free" rather than "nobody
            # counted", which is the rule the usage publisher follows too.
            self.assertNotIn("tokens=", text)
            self.assertNotIn("cost=", text)

    def test_elapsed_time_is_rendered_in_units(self) -> None:
        self.assertEqual(tui._elapsed_text(0), "0s")
        self.assertEqual(tui._elapsed_text(42.7), "42s")
        self.assertEqual(tui._elapsed_text(125), "2m05s")
        self.assertEqual(tui._elapsed_text(3725), "1h02m")


class LocalizationTests(_BrowserCase):
    async def test_a_row_renders_in_both_languages(self) -> None:
        self.harness.publish_state(
            "s-lang",
            tui.SUMMARY_RUN_STOPPED,
            status=tui.STATUS_STOPPED,
            task="fix the failing tests",
            waiting_reason="step_limit",
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            english = screen.row_text("s-lang")
            self.assertIn("fix the failing tests", english)
            self.assertIn(tui._SESSION_STATUS_TEXT[tui.STATUS_STOPPED][1], english)
            self.assertIn(tui._WAITING_REASON_TEXT["step_limit"][1], english)
            # The action is in the footer now, once.
            self.assertNotIn(self._action_words(ACTION_RESUME, True), english)
            self.assertIn(
                self._action_words(ACTION_RESUME, True),
                str(screen.query_one("#session-browser-hint").render()),
            )

            # The same view model, the other catalog: the row carries stable
            # identifiers and only the UI translates them.
            row = app._view_store.summary("s-lang")
            assert row is not None
            width = screen.content_width()
            russian = tui._session_browser_row_text(row, False, width)
            self.assertIn(tui._SESSION_STATUS_TEXT[tui.STATUS_STOPPED][0], russian)
            self.assertIn(tui._WAITING_REASON_TEXT["step_limit"][0], russian)

            # No identifier reaches a person in either language, and the
            # lifecycle summary is not a compact-row fact at all -- it belongs
            # to `/sessions` and to the detail timeline.
            for text in (english, russian):
                self.assertNotIn("step_limit", text)
                self.assertNotIn(tui.SUMMARY_RUN_STOPPED, text)
            self.assertNotIn(
                tui._EVENT_SUMMARY_TEXT[tui.SUMMARY_RUN_STOPPED][1], english
            )
            self.assertNotIn(
                tui._EVENT_SUMMARY_TEXT[tui.SUMMARY_RUN_STOPPED][0], russian
            )

    async def test_switching_language_redraws_the_open_browser(self) -> None:
        self.harness.publish_state(
            "s-switch", tui.SUMMARY_RUN_STOPPED, status=tui.STATUS_STOPPED
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertIn(
                tui._SESSION_STATUS_TEXT[tui.STATUS_STOPPED][1],
                screen.row_text("s-switch"),
            )
            with patch.object(tui, "_save_language", lambda language: None):
                app._set_language("ru")
            await pilot.pause()
            self.assertIn(
                tui._SESSION_STATUS_TEXT[tui.STATUS_STOPPED][0],
                screen.row_text("s-switch"),
            )

    async def test_a_waiting_reason_is_localized_even_when_qualified(self) -> None:
        """``karox.agent`` appends the specific budget to the reason."""

        self.harness.publish_state(
            "s-budget",
            tui.SUMMARY_RUN_STOPPED,
            status=tui.STATUS_STOPPED,
            waiting_reason="budget_exceeded:output_tokens",
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            text = screen.row_text("s-budget")
            self.assertIn(tui._WAITING_REASON_TEXT["budget_exceeded"][1], text)
            self.assertNotIn("budget_exceeded:", text)

    async def test_the_event_summary_reaches_no_compact_row(self) -> None:
        """Neither the identifier nor its translation.

        The old contract was "never verbatim", which was right when the row
        carried the lifecycle summary at all. A compact row does not: "run
        completed" beside the status word "completed" is the same fact twice,
        and the history it belongs to lives in the detail timeline.
        """

        self.harness.publish_state(
            "s-summary", tui.SUMMARY_RUN_COMPLETED, status=tui.STATUS_COMPLETED
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            text = screen.row_text("s-summary")
            self.assertNotIn(tui.SUMMARY_RUN_COMPLETED, text)
            self.assertNotIn(
                tui._EVENT_SUMMARY_TEXT[tui.SUMMARY_RUN_COMPLETED][1], text
            )
            self.assertIn(tui._SESSION_STATUS_TEXT[tui.STATUS_COMPLETED][1], text)

    async def test_a_session_without_a_task_gets_a_readable_stand_in(self) -> None:
        """The fallback must not look like a truncated word.

        An earlier cut took the last six characters, so `s-summary` rendered
        as "Session ummary" -- which reads as a name and is not one.
        """

        self.harness.publish_state("s-summary", status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertTrue(screen.row_text("s-summary").startswith("Session s-summary"))


class SelectionTests(_BrowserCase):
    async def test_the_keyboard_moves_the_selection(self) -> None:
        for session_id in ("s-1", "s-2", "s-3"):
            self.harness.publish_state(session_id, status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertEqual(screen.selected_session, "s-1")
            await pilot.press("down")
            self.assertEqual(screen.selected_session, "s-2")
            await pilot.press("down")
            self.assertEqual(screen.selected_session, "s-3")
            # Wraps rather than sticking at the end, like every other list here.
            await pilot.press("down")
            self.assertEqual(screen.selected_session, "s-1")
            await pilot.press("up")
            self.assertEqual(screen.selected_session, "s-3")

    async def test_an_update_to_another_session_keeps_the_selection(self) -> None:
        for session_id in ("s-1", "s-2", "s-3"):
            self.harness.publish_state(session_id, status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            await pilot.press("down")
            self.assertEqual(screen.selected_session, "s-2")

            self.harness.publish_state("s-3", status=tui.STATUS_COMPLETED)
            self._drain(app)
            # The selection names a session, not a row index.
            self.assertEqual(screen.selected_session, "s-2")

            # A new session appearing above it does not move it either.
            self.harness.publish_state("s-0", status=tui.STATUS_RUNNING)
            self._drain(app)
            await pilot.pause()
            self.assertEqual(self._ids(screen)[0], "s-0")
            self.assertEqual(screen.selected_session, "s-2")


class SelectedActionTests(_BrowserCase):
    """Enter carries out the row's one action, through the application."""

    async def test_enter_opens_a_finished_session(self) -> None:
        self.harness.publish_state(
            "s-open", tui.SUMMARY_RUN_COMPLETED, status=tui.STATUS_COMPLETED
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            await self._open(app, pilot)
            await pilot.press("enter")
            await pilot.pause()
            # Opening a session means making it the active one: the transcript,
            # the status bar and the composer all follow the active session.
            self.assertEqual(app.active_session, "s-open")

    async def test_enter_resumes_a_stopped_session(self) -> None:
        self.harness.publish_state(
            "s-resume",
            tui.SUMMARY_RUN_STOPPED,
            status=tui.STATUS_STOPPED,
            waiting_reason="no_changes",
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            await self._open(app, pilot)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.active_session, "s-resume")

    async def test_stopping_reaches_the_real_stop_action(self) -> None:
        app = self.harness.app("s-live")
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            app.agent_busy = True
            self.harness.publish_state(
                "s-live", tui.SUMMARY_RUN_STARTED, status=tui.STATUS_RUNNING
            )
            await self._open(app, pilot)
            with patch.object(app, "action_stop_agent") as stop:
                await pilot.press("enter")
                await pilot.pause()
            stop.assert_called_once_with()

    async def test_stopping_a_run_this_window_does_not_own_says_so(self) -> None:
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            self.harness.publish_state("s-elsewhere", status=tui.STATUS_RUNNING)
            await self._open(app, pilot)
            notices: list[tuple[str, str]] = []
            with patch.object(
                app,
                "_write_notice",
                side_effect=lambda text, level="info": notices.append((text, level)),
            ):
                await pilot.press("enter")
                await pilot.pause()
            # A key press that silently does nothing is worse than one that
            # explains why it cannot.
            self.assertTrue(notices)
            self.assertEqual(notices[0][1], "warning")
            self.assertFalse(app.agent_busy)

    async def test_a_pending_confirmation_is_described_without_its_token(self) -> None:
        self.harness.publish_state("s-confirm", status=tui.STATUS_RUNNING)
        self.harness.publish_pending_risk("s-confirm", token=True)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            await self._open(app, pilot)
            notices: list[tuple[str, str]] = []
            with patch.object(
                app,
                "_write_notice",
                side_effect=lambda text, level="info": notices.append((text, level)),
            ):
                await pilot.press("enter")
                await pilot.pause()
            self.assertTrue(notices)
            self.assertNotIn(FAKE_APPROVAL_TOKEN, notices[0][0])

    async def test_escape_closes_the_browser_without_an_action(self) -> None:
        self.harness.publish_state(
            "s-esc", tui.SUMMARY_RUN_COMPLETED, status=tui.STATUS_COMPLETED
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            await self._open(app, pilot)
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(
                isinstance(app.screen, tui.SessionBrowserScreen),
                "escape must close the browser",
            )
            self.assertIsNone(app.active_session)


class CommandSurfaceTests(_BrowserCase):
    async def test_the_slash_command_opens_the_browser(self) -> None:
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            app._handle_command("/browser")
            await pilot.pause()
            self.assertIsInstance(app.screen, tui.SessionBrowserScreen)

    def test_the_command_is_not_in_the_pinned_menu(self) -> None:
        """``/browser`` is routed but deliberately absent from the slash menu.

        The menu is a bounded, curated list whose rendered layout is pinned by
        ``tests/test_tui_layout.py``: it shows the first thirteen entries and
        nothing more, so adding a fourteenth silently pushes ``/sponsors`` off
        the screen. Documenting the browser there is a real change to that
        curated list and needs the snapshot re-recorded deliberately
        (``KAROX_UPDATE_SNAPSHOTS=1``), not as a side effect of adding a screen.

        Until then the discoverable entry point is the ``ctrl+o`` binding, and
        this test pins the decision so the entry is not re-added by accident.
        """

        self.assertNotIn("/browser", tui.SLASH_COMMANDS)
        self.assertNotIn("/browser", tui._COMMANDS_RU)
        # The route itself exists and is what the previous test exercises.
        self.assertTrue(
            any(
                binding.action == "session_browser"
                for binding in tui.KaroXApp.BINDINGS
            ),
            "the Session Browser must stay reachable from the keyboard",
        )

    async def test_an_empty_store_says_so_instead_of_showing_nothing(self) -> None:
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot)
            self.assertEqual(screen.rows(), ())
            hint = screen.query_one("#session-browser-hint", tui.Static)
            self.assertIn("No sessions yet", str(hint.render()))


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
