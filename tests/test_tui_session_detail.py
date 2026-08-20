"""The Session Detail screen is a real screen over the one ``SessionViewStore``.

Every test opens the detail screen the way the product opens it -- through the
Session Browser's primary action, on an application wired to a private event bus
and a private session directory -- and then asserts on the screen's own rendered
sections.

Two rules the tests themselves follow. Nothing here asserts on prose written by
hand: every word comes from the UI catalogs in :mod:`karox.tui`, so an assertion
compares against the catalog and stays true in both languages. And nothing here
reaches into a reducer: the screen is fed by publishing events, exactly as an
agent feeds it.

The token-shaped strings below are fixtures proving a secret cannot reach the
screen. They are not credentials.
"""

from __future__ import annotations

import dataclasses
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

FAKE_APPROVAL_TOKEN = "appr-" + ("v" * 30)
FAKE_BEARER = "bearer-" + ("w" * 24)


class _Harness:
    """An application wired to a private bus and a private session directory."""

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

    def state(self, session_id: str, summary: str = "state", **data: object) -> None:
        self.bus.publish(
            EventKind.SESSION_STATE,
            session_id=session_id,
            summary=summary,
            data=data,
        )

    def tool(self, session_id: str, **data: object) -> None:
        self.bus.publish(
            EventKind.TOOL_CALL,
            session_id=session_id,
            summary="tool",
            data=data,
        )

    def error(self, session_id: str, code: str, reason: str) -> None:
        self.bus.publish(
            EventKind.ERROR,
            session_id=session_id,
            summary=tui.SUMMARY_RUN_FAILED,
            level=EventLevel.ERROR,
            data={"code": code, "reason": reason},
        )

    def pending_risk(self, session_id: str, *, secrets: bool = False) -> None:
        """A verdict that stops the agent and waits on the person at the screen."""

        data: dict[str, object] = {
            "allowed": False,
            "risk": "high",
            "reason": "confirmation_required",
            "action_digest": "d" * 32,
            "reasons": ["writes outside the workspace", "deletes tracked files"],
        }
        payload_secrets: tuple[str, ...] = ()
        if secrets:
            data["confirmation_token"] = FAKE_APPROVAL_TOKEN
            data["authorization"] = FAKE_BEARER
            payload_secrets = (FAKE_APPROVAL_TOKEN, FAKE_BEARER)
        self.bus.publish(
            EventKind.RISK_DECISION,
            session_id=session_id,
            summary="needs confirmation",
            level=EventLevel.WARNING,
            data=data,
            secrets=payload_secrets,
        )


class _DetailCase(unittest.IsolatedAsyncioTestCase):
    """Open the detail screen through the browser, and drive the one drain."""

    def setUp(self) -> None:
        self.harness = _Harness(self)

    async def _browser(self, app: tui.KaroXApp, pilot: object) -> tui.SessionBrowserScreen:
        app.action_session_browser()
        await pilot.pause()  # type: ignore[attr-defined]
        screen = app.screen
        assert isinstance(screen, tui.SessionBrowserScreen)
        return screen

    async def _open_via_browser(
        self, app: tui.KaroXApp, pilot: object
    ) -> tui.SessionDetailScreen:
        """Press Enter on the selected row and land on the detail screen."""

        await self._browser(app, pilot)
        await pilot.press("enter")  # type: ignore[attr-defined]
        await pilot.pause()  # type: ignore[attr-defined]
        screen = app.screen
        assert isinstance(screen, tui.SessionDetailScreen), (
            f"the primary action did not open the detail screen: {screen!r}"
        )
        return screen

    async def _open(
        self, app: tui.KaroXApp, pilot: object, session_id: str, action: str
    ) -> tui.SessionDetailScreen:
        """Deliver one browser choice through the real application callback."""

        app._session_browser_done(tui.SessionAction(session_id, action))
        await pilot.pause()  # type: ignore[attr-defined]
        screen = app.screen
        assert isinstance(screen, tui.SessionDetailScreen)
        return screen

    def _drain(self, app: tui.KaroXApp) -> None:
        app._drain_session_view()


class OpenFromBrowserTests(_DetailCase):
    async def test_open_leads_to_the_detail_screen(self) -> None:
        self.harness.state(
            "s-open", tui.SUMMARY_RUN_COMPLETED, status=tui.STATUS_COMPLETED
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            browser = await self._browser(app, pilot)
            row = app._view_store.summary("s-open")
            assert row is not None
            self.assertEqual(row.primary_action, ACTION_OPEN)
            self.assertEqual(browser.selected_session, "s-open")

            await pilot.press("enter")
            await pilot.pause()
            screen = app.screen
            assert isinstance(screen, tui.SessionDetailScreen)
            self.assertEqual(screen.session_id, "s-open")

    async def test_resume_opens_the_detail_and_starts_no_agent(self) -> None:
        self.harness.state(
            "s-resume",
            tui.SUMMARY_RUN_STOPPED,
            status=tui.STATUS_STOPPED,
            waiting_reason="no_changes",
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            row = app._view_store.summary("s-resume")
            assert row is not None
            self.assertEqual(row.primary_action, ACTION_RESUME)

            with patch.object(tui, "_run_agent_cli") as runner:
                screen = await self._open_via_browser(app, pilot)
            # Resuming the work stays an explicit task the person types.
            runner.assert_not_called()
            self.assertFalse(app.agent_busy)
            self.assertIsNone(app.agent_process)
            self.assertEqual(screen.session_id, "s-resume")
            # It does become the session the composer talks to.
            self.assertEqual(app.active_session, "s-resume")

    async def test_review_risk_opens_the_detail_with_the_risk_state(self) -> None:
        self.harness.state("s-risk", status=tui.STATUS_RUNNING)
        self.harness.pending_risk("s-risk")
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            row = app._view_store.summary("s-risk")
            assert row is not None
            self.assertEqual(row.primary_action, ACTION_REVIEW_RISK)

            screen = await self._open_via_browser(app, pilot)
            # D. The decision lives in Attention, directly under the overview,
            # because it is the one thing on this screen waiting on a person.
            # It used to be `risk`, nine sections down, behind a timeline.
            attention = screen.section_text("attention")
            self.assertIn("high", attention)
            self.assertIn(tui._detail_words("needs_confirmation", True), attention)
            self.assertEqual(screen.visible_sections()[0], "attention")
            # The technical verdict is still published, further down.
            self.assertIn(
                tui._detail_words("awaiting", True), screen.section_text("diagnostics")
            )
            # Reviewing a risk is not choosing which session the composer talks
            # to, so the active session is deliberately untouched.
            self.assertIsNone(app.active_session)

    async def test_stop_keeps_the_existing_stop_flow(self) -> None:
        app = self.harness.app("s-live")
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            app.agent_busy = True
            self.harness.state(
                "s-live", tui.SUMMARY_RUN_STARTED, status=tui.STATUS_RUNNING
            )
            await self._browser(app, pilot)
            row = app._view_store.summary("s-live")
            assert row is not None
            self.assertEqual(row.primary_action, ACTION_STOP)
            with patch.object(app, "action_stop_agent") as stop:
                await pilot.press("enter")
                await pilot.pause()
            stop.assert_called_once_with()
            # Stop does not open the detail screen.
            self.assertFalse(isinstance(app.screen, tui.SessionDetailScreen))

    async def test_escape_returns_to_the_browser(self) -> None:
        self.harness.state(
            "s-back", tui.SUMMARY_RUN_COMPLETED, status=tui.STATUS_COMPLETED
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            await self._open_via_browser(app, pilot)
            await pilot.press("escape")
            await pilot.pause()
            self.assertIsInstance(app.screen, tui.SessionBrowserScreen)

    async def test_the_browser_selection_survives_the_round_trip(self) -> None:
        for session_id in ("s-1", "s-2", "s-3"):
            self.harness.state(session_id, status=tui.STATUS_COMPLETED)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            browser = await self._browser(app, pilot)
            await pilot.press("down")
            self.assertEqual(browser.selected_session, "s-2")

            await pilot.press("enter")
            await pilot.pause()
            detail = app.screen
            assert isinstance(detail, tui.SessionDetailScreen)
            self.assertEqual(detail.session_id, "s-2")

            await pilot.press("escape")
            await pilot.pause()
            returned = app.screen
            assert isinstance(returned, tui.SessionBrowserScreen)
            # The cursor is where the user left it, not back at the top.
            self.assertEqual(returned.selected_session, "s-2")

    async def test_browser_and_detail_share_one_store(self) -> None:
        self.harness.state("s-one", status=tui.STATUS_COMPLETED)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            browser = await self._browser(app, pilot)
            self.assertIs(browser._store, app._view_store)
            await pilot.press("enter")
            await pilot.pause()
            detail = app.screen
            assert isinstance(detail, tui.SessionDetailScreen)
            # A second store would have its own cursor and would disagree with
            # the status bar about what a session is doing.
            self.assertIs(detail._store, app._view_store)


class HeaderTests(_DetailCase):
    async def test_the_header_is_built_from_the_view_model(self) -> None:
        self.harness.state(
            "s-head",
            tui.SUMMARY_RUN_STARTED,
            status=tui.STATUS_RUNNING,
            task="fix the failing tests",
            provider="openai",
            model="model-a",
            agent="karox",
            access_profile=AccessProfile.WORKSPACE_WRITE.value,
            workspace_mode="worktree",
            current_step="repo_edit_file",
            changed_files=["a.py", "b.py"],
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-head", ACTION_OPEN)
            header = screen.header_text()
            # D. What a person needs to answer "what is this and what is
            # happening to it", task first.
            self.assertTrue(header.startswith("fix the failing tests"))
            for expected in (
                tui._SESSION_STATUS_TEXT[tui.STATUS_RUNNING][1],
                tui._ACTIVITY_WORDS[tui.ACTIVITY_EDITING][1],
                "openai/model-a",
                tui._detail_words("changed_files", True),
            ):
                with self.subTest(expected=expected):
                    self.assertIn(expected, header)

            # And what they only need afterwards. None of it is deleted: every
            # one of these is asserted present in diagnostics below.
            for absent in (
                "s-head",
                AccessProfile.WORKSPACE_WRITE.value,
                "worktree",
                "repo_edit_file",
                "files=",
                "agent: karox",
                "AccessProfile.",
            ):
                with self.subTest(absent=absent):
                    self.assertNotIn(absent, header)

            diagnostics = screen.section_text("diagnostics")
            for expected in (
                "s-head",
                AccessProfile.WORKSPACE_WRITE.value,
                "worktree",
                "repo_edit_file",
                "agent: karox",
            ):
                with self.subTest(diagnostics=expected):
                    self.assertIn(expected, diagnostics)

    async def test_an_unknown_workspace_mode_is_a_dash_in_diagnostics(self) -> None:
        """A missing safety fact must not read as a safe default.

        The rule survives D; where it applies moved. The overview never
        promises the field, so a dash there would be a placeholder for
        something the reader was not told to expect. Diagnostics does promise
        it, so an absent value has to be visibly absent rather than blank.
        """

        self.harness.state("s-mode", status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-mode", ACTION_OPEN)
            self.assertIn(tui.UNKNOWN_FIELD, screen.section_text("diagnostics"))
            self.assertNotIn(tui.UNKNOWN_FIELD, screen.header_text())

    async def test_the_overview_and_the_technical_row_share_a_source(self) -> None:
        self.harness.state(
            "s-shared",
            tui.SUMMARY_RUN_STOPPED,
            status=tui.STATUS_STOPPED,
            waiting_reason="step_limit",
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-shared", ACTION_OPEN)
            row = app._view_store.summary("s-shared")
            assert row is not None
            # D. Two presentation contracts, one source of facts. The verbose
            # renderer is still published verbatim -- it moved to diagnostics,
            # where somebody debugging will look for it.
            self.assertIn(
                tui._session_row_text(row, True, verbose=True),
                screen.section_text("diagnostics"),
            )
            # The overview is its own contract and is not that string.
            header = screen.header_text()
            self.assertNotEqual(
                header, tui._session_row_text(row, True, verbose=True)
            )
            # They cannot contradict each other, because the status and the
            # waiting reason come from the same catalogs in both.
            for shared in (
                tui._SESSION_STATUS_TEXT[tui.STATUS_STOPPED][1],
                tui._WAITING_REASON_TEXT["step_limit"][1],
            ):
                with self.subTest(shared=shared):
                    self.assertIn(shared, header)
                    self.assertIn(shared, screen.section_text("diagnostics"))

    async def test_a_session_the_store_does_not_know_says_so(self) -> None:
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-missing", ACTION_OPEN)
            # Nine empty blocks would look like measured emptiness.
            self.assertIn("s-missing", screen.header_text())
            self.assertIn("no data", screen.header_text())
            for name in tui.SessionDetailScreen.SECTIONS:
                self.assertEqual(
                    screen.section_text(name), tui._detail_words("empty", True)
                )


class TimelineTests(_DetailCase):
    async def test_the_timeline_is_in_deterministic_sequence_order(self) -> None:
        for index in range(5):
            self.harness.state(
                "s-time", status=tui.STATUS_RUNNING, current_step=f"step-{index}"
            )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-time", ACTION_OPEN)
            lines = screen.section_text("timeline").splitlines()
            sequences = [
                int(line.split("·")[0].strip().lstrip("#"))
                for line in lines
                if line.startswith("#")
            ]
            self.assertEqual(len(sequences), 5)
            self.assertEqual(sequences, sorted(sequences))
            # Redrawing must not reorder anything.
            screen.refresh_detail()
            self.assertEqual(screen.section_text("timeline").splitlines(), lines)

    async def test_an_event_summary_is_localized_in_both_languages(self) -> None:
        self.harness.state(
            "s-lang", tui.SUMMARY_RUN_STOPPED, status=tui.STATUS_STOPPED
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-lang", ACTION_OPEN)
            english = screen.section_text("timeline")
            self.assertIn(tui._EVENT_SUMMARY_TEXT[tui.SUMMARY_RUN_STOPPED][1], english)
            self.assertIn(tui._EVENT_KIND_TEXT["session_state"][1], english)
            self.assertNotIn(tui.SUMMARY_RUN_STOPPED, english)

            screen.language = "ru"
            screen.refresh_detail()
            russian = screen.section_text("timeline")
            self.assertIn(tui._EVENT_SUMMARY_TEXT[tui.SUMMARY_RUN_STOPPED][0], russian)
            self.assertIn(tui._EVENT_KIND_TEXT["session_state"][0], russian)
            self.assertNotIn(tui.SUMMARY_RUN_STOPPED, russian)

    async def test_a_timeline_entry_reports_a_measured_duration(self) -> None:
        """The regression this closes: the timeline silently rendered nothing.

        A finished tool call carries ``duration_ms`` in its bounded entry data.
        The formatter reached for a helper that does not exist in this module, so
        the whole timeline section raised, was swallowed by the per-section guard
        and rendered as "no data" -- a screen that looked merely empty while the
        session had a full history. Ruff caught the undefined name; this test is
        what keeps the *behaviour* pinned rather than only the name.
        """

        self.harness.tool(
            "s-dur",
            call_id="c-1",
            tool="read_file",
            phase="finished",
            ok=True,
            duration_ms=1500,
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-dur", ACTION_OPEN)
            text = screen.section_text("timeline")
            self.assertNotEqual(text, tui._detail_words("empty", True))
            self.assertIn("1s", text)
            self.assertIn(tui._EVENT_KIND_TEXT["tool_call"][1], text)
            self.assertIn("read_file", text)

    async def test_an_unknown_summary_identifier_is_safe(self) -> None:
        self.harness.state(
            "s-unknown", "agent_run_teleported", status=tui.STATUS_RUNNING
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-unknown", ACTION_OPEN)
            # Rendered verbatim as a diagnostic rather than blanked or raised on.
            self.assertIn("agent_run_teleported", screen.section_text("timeline"))

    async def test_the_timeline_is_bounded_and_says_what_it_hid(self) -> None:
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            limit = tui.SessionDetailScreen.MAX_TIMELINE_ROWS
            for index in range(limit + 12):
                self.harness.state(
                    "s-many", status=tui.STATUS_RUNNING, current_step=f"step-{index}"
                )
            screen = await self._open(app, pilot, "s-many", ACTION_OPEN)
            text = screen.section_text("timeline")
            rows = [line for line in text.splitlines() if line.startswith("#")]
            self.assertEqual(len(rows), limit)
            self.assertIn(tui._detail_words("truncated", True), text)
            # The newest entries are the ones kept.
            self.assertIn(f"step-{limit + 11}", text)

    async def test_a_dropped_event_count_is_reported(self) -> None:
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            self.harness.state("s-drop", status=tui.STATUS_RUNNING)
            screen = await self._open(app, pilot, "s-drop", ACTION_OPEN)
            self.assertNotIn(
                tui._detail_words("dropped", True), screen.section_text("timeline")
            )
            # A store that lost events must say so rather than showing a
            # plausible-looking partial history.
            #
            # ``dropped_events`` is a *snapshot* field on ``SessionDetail``: the
            # store copies its own counter in when it builds the snapshot, so
            # patching the store property changes nothing that has already been
            # built. Filling the bus ring to force a real eviction would make the
            # test slow and depend on the ring size, so the screen is handed a
            # genuine view model with the field set -- which is exactly the input
            # its contract is about.
            real = app._view_store.detail("s-drop")
            assert real is not None
            with patch.object(
                app._view_store,
                "detail",
                return_value=dataclasses.replace(real, dropped_events=7),
            ):
                screen.refresh_detail()
                text = screen.section_text("timeline")
            self.assertIn(tui._detail_words("dropped", True), text)
            self.assertIn("7", text)


class ToolCallTests(_DetailCase):
    async def test_a_running_and_a_finished_call_are_distinguished(self) -> None:
        self.harness.tool("s-tools", call_id="c-1", tool="read_file", phase="started")
        self.harness.tool(
            "s-tools",
            call_id="c-1",
            tool="read_file",
            phase="finished",
            ok=True,
            duration_ms=1500,
        )
        self.harness.tool("s-tools", call_id="c-2", tool="apply_patch", phase="started")
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-tools", ACTION_OPEN)
            text = screen.section_text("tools")
            self.assertIn("read_file", text)
            self.assertIn(tui._detail_words("ok", True), text)
            self.assertIn("1s", text)
            self.assertIn("apply_patch", text)
            self.assertIn(tui._detail_words("running", True), text)

    async def test_a_failed_call_reports_failure_not_success(self) -> None:
        self.harness.tool(
            "s-fail",
            call_id="c-9",
            tool="checks_run",
            phase="finished",
            ok=False,
            duration_ms=250,
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-fail", ACTION_OPEN)
            text = screen.section_text("tools")
            self.assertIn(tui._detail_words("failed", True), text)
            self.assertNotIn(tui._detail_words("ok", True), text)
            self.assertIn("250ms", text)

    async def test_a_finished_call_with_no_verdict_invents_none(self) -> None:
        self.harness.tool(
            "s-quiet", call_id="c-3", tool="git_status", phase="finished"
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-quiet", ACTION_OPEN)
            text = screen.section_text("tools")
            # Nobody said whether it succeeded, so neither does the screen.
            self.assertIn(tui.UNKNOWN_FIELD, text)
            self.assertNotIn(tui._detail_words("ok", True), text)

    async def test_the_tool_list_is_bounded(self) -> None:
        limit = tui.SessionDetailScreen.MAX_TOOL_ROWS
        for index in range(limit + 5):
            self.harness.tool(
                "s-lots", call_id=f"c-{index}", tool=f"tool_{index}", phase="started"
            )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-lots", ACTION_OPEN)
            text = screen.section_text("tools")
            rows = [line for line in text.splitlines() if line.startswith("tool_")]
            self.assertEqual(len(rows), limit)
            self.assertIn(tui._detail_words("truncated", True), text)


class ErrorAndUsageTests(_DetailCase):
    async def test_the_error_block_shows_a_safe_code(self) -> None:
        self.harness.error("s-err", tui.ERROR_AGENT_FAILED, "provider_error")
        self.harness.state("s-err", tui.SUMMARY_RUN_FAILED, status=tui.STATUS_FAILED)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-err", ACTION_OPEN)
            text = screen.section_text("errors")
            self.assertIn(tui.ERROR_AGENT_FAILED, text)
            self.assertIn(tui._EVENT_SUMMARY_TEXT[tui.SUMMARY_RUN_FAILED][1], text)

    async def test_usage_without_measurements_shows_no_zeros(self) -> None:
        self.harness.state("s-free", status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-free", ACTION_OPEN)
            # "This run was free" and "nobody counted" are different claims.
            self.assertEqual(
                screen.section_text("usage"), tui._detail_words("empty", True)
            )
            self.assertNotIn("0", screen.section_text("usage"))

    async def test_measured_usage_cost_and_budgets_are_shown(self) -> None:
        self.harness.bus.publish(
            EventKind.AGENT_ACTION,
            session_id="s-usage",
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
            screen = await self._open(app, pilot, "s-usage", ACTION_OPEN)
            text = screen.section_text("usage")
            self.assertIn("total_tokens: 1500", text)
            self.assertIn("0.42", text)
            self.assertIn("8000", text)
            self.assertIn(tui._detail_words("limit", True), text)

    async def test_performance_spans_are_shown_when_measured(self) -> None:
        self.harness.bus.publish(
            EventKind.PERFORMANCE_SPAN,
            session_id="s-perf",
            summary="span",
            data={"component": "provider", "duration_ms": 900, "span": "completion"},
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-perf", ACTION_OPEN)
            self.assertIn("provider", screen.section_text("performance"))


class OptionalBlockTests(_DetailCase):
    async def test_workspace_browser_and_evidence_blocks_are_fail_soft(self) -> None:
        """An absent block says "no data" rather than inventing a fact."""

        self.harness.state("s-bare", status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-bare", ACTION_OPEN)
            for name in ("workspace", "browser", "evidence", "performance"):
                self.assertEqual(
                    screen.section_text(name),
                    tui._detail_words("empty", True),
                    f"{name} must report absence rather than a made-up value",
                )

    async def test_populated_workspace_browser_and_evidence_blocks(self) -> None:
        self.harness.state(
            "s-full",
            status=tui.STATUS_RUNNING,
            git={"branch": "main"},
            diff={"files": 2, "insertions": 30},
        )
        self.harness.bus.publish(
            EventKind.BROWSER_ACTION,
            session_id="s-full",
            summary="browser",
            data={"action": "open", "origin": "https://example.test", "takeover": True},
        )
        self.harness.bus.publish(
            EventKind.EVIDENCE,
            session_id="s-full",
            summary="evidence",
            data={"evidence_id": "e-1", "kind": "test_run", "summary": "9 passed"},
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-full", ACTION_OPEN)
            self.assertIn("branch: main", screen.section_text("workspace"))
            self.assertIn("files: 2", screen.section_text("workspace"))
            self.assertIn("example.test", screen.section_text("browser"))
            self.assertIn("takeover: yes", screen.section_text("browser"))
            self.assertIn("test_run", screen.section_text("evidence"))

    async def test_a_broken_section_costs_only_that_section(self) -> None:
        self.harness.state("s-broken-section", status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-broken-section", ACTION_OPEN)
            with patch.object(
                tui, "_timeline_entry_text", side_effect=RuntimeError("formatter")
            ):
                screen.refresh_detail()
            # The timeline failed; the header and the other sections did not.
            # The overview no longer carries the id, so the identity assertion
            # is where the id now lives.
            self.assertIn("s-broken-section", screen.section_text("diagnostics"))
            self.assertTrue(screen.header_text())
            self.assertEqual(
                screen.section_text("timeline"), tui._detail_words("empty", True)
            )
            # And the overview above it still says what this session is.
            self.assertNotIn(
                tui._detail_words("empty", True), screen.header_text()
            )


class SmartStopBlockTests(_DetailCase):
    async def test_a_pending_confirmation_is_described_by_named_fields(self) -> None:
        self.harness.state("s-stop", status=tui.STATUS_RUNNING)
        self.harness.pending_risk("s-stop")
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-stop", ACTION_REVIEW_RISK)
            # D. Attention answers "what am I being asked". It carries the
            # level and the reasons, which are what a decision is made on, and
            # deliberately not the digest -- a hash nobody can check is not a
            # fact a person weighs, and printing it invites treating an opaque
            # string as evidence.
            attention = screen.section_text("attention")
            self.assertIn(tui._detail_words("needs_confirmation", True), attention)
            self.assertIn(tui._detail_words("risk_level", True), attention)
            self.assertIn("high", attention)
            self.assertIn("deletes tracked files", attention)
            self.assertNotIn(tui._detail_words("action_digest", True), attention)

            # Diagnostics keeps the full technical verdict, digest included.
            text = screen.section_text("diagnostics")
            self.assertIn(tui._detail_words("blocked", True), text)
            self.assertIn(tui._detail_words("action_digest", True), text)
            self.assertIn(tui._detail_words("awaiting", True), text)
            self.assertIn(
                tui._WAITING_REASON_TEXT["confirmation_required"][1], text
            )

    async def test_no_confirmation_token_or_bearer_reaches_the_screen(self) -> None:
        self.harness.state("s-secret", status=tui.STATUS_RUNNING)
        self.harness.pending_risk("s-secret", secrets=True)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-secret", ACTION_REVIEW_RISK)
            rendered = screen.rendered_text()
            self.assertNotIn(FAKE_APPROVAL_TOKEN, rendered)
            self.assertNotIn(FAKE_BEARER, rendered)
            self.assertNotIn("appr-", rendered)
            self.assertNotIn("bearer-", rendered)
            # And the attention block is still useful without them: a reader
            # can still tell they are being asked something, and what about.
            self.assertIn(
                tui._detail_words("needs_confirmation", True),
                screen.section_text("attention"),
            )

    async def test_a_hostile_payload_does_not_reach_the_rendered_text(self) -> None:
        """An unexpected payload key is never copied, so it cannot be drawn."""

        self.harness.state(
            "s-hostile",
            status=tui.STATUS_RUNNING,
            surprise_field="SHOULD-NOT-APPEAR",
            nested={"deep": "ALSO-NOT-APPEAR"},
        )
        self.harness.tool(
            "s-hostile",
            call_id="c-h",
            tool="read_file",
            phase="started",
            secret_extra="NEVER-DRAWN",
        )
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-hostile", ACTION_OPEN)
            rendered = screen.rendered_text()
            for hostile in ("SHOULD-NOT-APPEAR", "ALSO-NOT-APPEAR", "NEVER-DRAWN"):
                self.assertNotIn(hostile, rendered)

    async def test_a_session_with_no_risk_says_nothing_is_pending(self) -> None:
        self.harness.state("s-calm", status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-calm", ACTION_OPEN)
            # D. An absent problem is said by an absent block. The permanent
            # "No confirmation is pending" card trained a reader to skip the
            # exact region that will one day matter.
            self.assertNotIn("attention", screen.visible_sections())
            self.assertNotIn(
                tui._detail_words("no_risk", True), screen.rendered_text()
            )


class IncrementalUpdateTests(_DetailCase):
    async def test_a_change_to_this_session_redraws_the_detail(self) -> None:
        self.harness.state("s-live", status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-live", ACTION_OPEN)
            before = screen.redraws
            self.harness.state(
                "s-live", status=tui.STATUS_RUNNING, current_step="apply_patch"
            )
            self._drain(app)
            self.assertGreater(screen.redraws, before)
            # D. The step reached the screen, but not as an identifier. An
            # uncatalogued tool contributes nothing to the overview and stays
            # verbatim in diagnostics, where the vocabulary is the point.
            self.assertNotIn("apply_patch", screen.header_text())
            self.assertIn("apply_patch", screen.section_text("diagnostics"))

    async def test_an_event_about_another_session_does_not_redraw(self) -> None:
        self.harness.state("s-mine", status=tui.STATUS_RUNNING)
        self.harness.state("s-other", status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-mine", ACTION_OPEN)
            before = screen.redraws
            self.harness.state(
                "s-other", status=tui.STATUS_RUNNING, current_step="somewhere-else"
            )
            self._drain(app)
            # Not one redraw, and nothing from the other session on screen.
            self.assertEqual(screen.redraws, before)
            self.assertNotIn("somewhere-else", screen.rendered_text())

    async def test_the_detail_screen_never_drains_the_dirty_set(self) -> None:
        """One drain point, or the status bar and the screens rob each other."""

        app = self.harness.app("s-drain")
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-drain", ACTION_OPEN)
            self.harness.state(
                "s-drain", status=tui.STATUS_RUNNING, current_step="apply_patch"
            )
            self.assertEqual(app._view_store.dirty, ("s-drain",))
            screen.apply_changes(("s-drain",))
            # The screen rendered and the application's drain is untouched.
            self.assertEqual(app._view_store.dirty, ("s-drain",))
            self._drain(app)
            self.assertEqual(app._view_store.dirty, ())

    async def test_apply_changes_reports_whether_it_redrew(self) -> None:
        self.harness.state("s-report", status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-report", ACTION_OPEN)
            self.assertTrue(screen.apply_changes(("s-report",)))
            self.assertFalse(screen.apply_changes(("s-somebody-else",)))
            # An empty set is the first draw, which is a redraw of everything.
            self.assertTrue(screen.apply_changes(()))

    async def test_a_broken_detail_renderer_does_not_break_the_interface(self) -> None:
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            self.harness.state("s-fault", status=tui.STATUS_RUNNING)
            screen = await self._open(app, pilot, "s-fault", ACTION_OPEN)
            with patch.object(
                screen, "apply_changes", side_effect=RuntimeError("widget")
            ):
                self.harness.state(
                    "s-fault", status=tui.STATUS_RUNNING, current_step="still-working"
                )
                # The publisher returned normally and the fold still happened.
                self._drain(app)
            row = app._view_store.summary("s-fault")
            assert row is not None
            self.assertEqual(row.current_step, "still-working")
            self.assertEqual(self.harness.bus.subscriber_errors, 0)
            # The interface is still usable.
            self.assertFalse(app.query_one("#composer", tui.Input).disabled)

    async def test_a_store_fault_costs_the_refresh_and_not_the_screen(self) -> None:
        self.harness.state("s-storefault", status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=(120, 42)) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-storefault", ACTION_OPEN)
            with patch.object(
                app._view_store, "detail", side_effect=RuntimeError("store")
            ):
                screen.refresh_detail()
            self.assertIn("no data", screen.header_text())
            # And it recovers on the next good refresh.
            screen.refresh_detail()
            self.assertIn("s-storefault", screen.header_text())


class LocalizationContractTests(unittest.TestCase):
    """Every identifier the detail screen can draw has words in both languages."""

    def test_every_event_kind_is_translated(self) -> None:
        from karox.event_bus import EventKind as Kind

        missing = sorted(
            kind.value for kind in Kind if kind.value not in tui._EVENT_KIND_TEXT
        )
        self.assertEqual(
            missing, [], f"these event kinds have no catalog entry: {missing}"
        )

    def test_every_event_level_is_translated(self) -> None:
        from karox.event_bus import EventLevel as Level

        missing = sorted(
            level.value for level in Level if level.value not in tui._EVENT_LEVEL_TEXT
        )
        self.assertEqual(missing, [])

    def test_every_section_has_a_title_in_both_languages(self) -> None:
        for name in tui.SessionDetailScreen.SECTIONS:
            with self.subTest(section=name):
                self.assertIn(name, tui._DETAIL_TEXT)
                russian, english = tui._DETAIL_TEXT[name]
                self.assertTrue(russian.strip())
                self.assertTrue(english.strip())

    def test_both_languages_are_populated_everywhere(self) -> None:
        for catalog_name, catalog in (
            ("_DETAIL_TEXT", tui._DETAIL_TEXT),
            ("_EVENT_KIND_TEXT", tui._EVENT_KIND_TEXT),
            ("_EVENT_LEVEL_TEXT", tui._EVENT_LEVEL_TEXT),
        ):
            for identifier, (russian, english) in catalog.items():
                with self.subTest(catalog=catalog_name, identifier=identifier):
                    self.assertTrue(russian.strip(), f"{identifier} has no Russian")
                    self.assertTrue(english.strip(), f"{identifier} has no English")

    def test_an_unknown_identifier_is_shown_verbatim(self) -> None:
        self.assertEqual(tui._event_kind_words("teleport", True), "teleport")
        self.assertEqual(tui._event_level_words("cosmic", False), "cosmic")
        self.assertEqual(tui._detail_words("unheard_of", True), "unheard_of")

    def test_a_duration_is_rendered_in_units(self) -> None:
        self.assertEqual(tui._duration_text(None), "")
        self.assertEqual(tui._duration_text(-1), "")
        self.assertEqual(tui._duration_text(250), "250ms")
        self.assertEqual(tui._duration_text(1500), "1s")
        self.assertEqual(tui._duration_text(65000), "1m05s")

    def test_a_missing_timestamp_is_not_nineteen_seventy(self) -> None:
        self.assertEqual(tui._clock_text(0), "")
        self.assertEqual(tui._clock_text(-5), "")
        self.assertEqual(len(tui._clock_text(1785741758.0)), 8)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
