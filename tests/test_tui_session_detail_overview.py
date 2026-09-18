"""The overview-first contract of Session Detail.

The companion to ``tests/test_tui_session_detail.py``. That file proves the
screen is a real observer of ``SessionViewStore`` -- which sections exist, what
they publish, what the bounds are. This one proves the D hierarchy: that the
first thing a reader sees answers "what is this and does it need me", that a
section with nothing in it is absent rather than empty, that a live update does
not drag the viewport, and that none of it can be made to print a secret.

Two rules hold throughout. Nothing asserts on prose written by hand -- every
word comes from a catalog in :mod:`karox.tui`, so a wording change moves both
languages together. And nothing inspects source: the security cases plant a
payload, mount the real screen, and read what the widgets actually render,
because ``inspect.getsource`` proves a line was written and never that it ran.

The key-shaped and token-shaped strings below are fixtures, assembled at
runtime. They are not credentials.
"""

from __future__ import annotations

from _unittest_compat import enter_context

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox import tui
from karox.event_bus import EventBus, EventKind, EventLevel
from karox.models import AccessProfile
from karox.session_view import ACTION_OPEN, ACTION_REVIEW_RISK
from karox.sessions import SessionStore

FAKE_APPROVAL_TOKEN = "appr-" + ("z" * 30)
FAKE_BEARER = "bearer-" + ("y" * 24)
FAKE_KEY_SHAPED = "sk-" + "live-" + ("7" * 32)
FAKE_CREDENTIAL_REF = "keyring://karox/" + ("c" * 20)

NARROW = (46, 14)
STANDARD = (80, 24)
WIDE = (120, 30)

# Every section the screen knows, minus the two that are shown even when empty
# because their emptiness is itself the answer a reader came for.
OPTIONAL_SECTIONS = (
    "attention",
    "errors",
    "timeline",
    "tools",
    "usage",
    "workspace",
    "browser",
    "evidence",
    "performance",
)


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
            EventKind.TOOL_CALL, session_id=session_id, summary="tool", data=data
        )

    def error(self, session_id: str, code: str, reason: str) -> None:
        self.bus.publish(
            EventKind.ERROR,
            session_id=session_id,
            summary=tui.SUMMARY_RUN_FAILED,
            level=EventLevel.ERROR,
            data={"code": code, "reason": reason},
        )

    def pending_risk(
        self,
        session_id: str,
        *,
        secrets: bool = False,
        digest: str = "d" * 32,
    ) -> None:
        data: dict[str, object] = {
            "allowed": False,
            "risk": "high",
            "reason": "confirmation_required",
            "action_digest": digest,
            "reasons": ["writes outside the workspace", "deletes tracked files"],
        }
        payload_secrets: tuple[str, ...] = ()
        if secrets:
            data["confirmation_token"] = FAKE_APPROVAL_TOKEN
            data["authorization"] = FAKE_BEARER
            data["credential_ref"] = FAKE_CREDENTIAL_REF
            payload_secrets = (FAKE_APPROVAL_TOKEN, FAKE_BEARER, FAKE_CREDENTIAL_REF)
        self.bus.publish(
            EventKind.RISK_DECISION,
            session_id=session_id,
            summary="needs confirmation",
            level=EventLevel.WARNING,
            data=data,
            secrets=payload_secrets,
        )


class _OverviewCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.harness = _Harness(self)

    async def _open(
        self,
        app: tui.KaroXApp,
        pilot: object,
        session_id: str,
        action: str = ACTION_OPEN,
    ) -> tui.SessionDetailScreen:
        """One browser choice, through the real application callback."""

        app._session_browser_done(tui.SessionAction(session_id, action))
        await pilot.pause()  # type: ignore[attr-defined]
        screen = app.screen
        assert isinstance(screen, tui.SessionDetailScreen)
        return screen

    def _drain(self, app: tui.KaroXApp) -> None:
        app._drain_session_view()

    def _footer(self, screen: tui.SessionDetailScreen) -> str:
        return str(screen.query_one("#session-detail-hint", tui.Static).render())


# ------------------------------------------------------------- 4. the overview


class OverviewContentTests(_OverviewCase):
    """The first thing on screen answers the first question a person has."""

    async def test_the_task_comes_first_and_the_identifier_does_not(self) -> None:
        identifier = "task-1754300000-abcdef12"
        self.harness.state(
            identifier,
            status=tui.STATUS_RUNNING,
            task="fix the failing integration tests",
            current_step="repo.search",
            provider="openai",
            model="model-a",
            access_profile=AccessProfile.WORKSPACE_WRITE.value,
            workspace_mode="worktree",
        )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, identifier)
            header = screen.header_text()
            self.assertTrue(header.startswith("fix the failing integration tests"))
            # Human status and human activity, from the shared catalogs.
            self.assertIn(tui._SESSION_STATUS_TEXT[tui.STATUS_RUNNING][1], header)
            self.assertIn(tui._ACTIVITY_WORDS[tui.ACTIVITY_SEARCHING][1], header)
            self.assertIn("openai/model-a", header)
            for absent in (
                identifier,
                AccessProfile.WORKSPACE_WRITE.value,
                "worktree",
                "repo.search",
                "tokens",
                "cost",
            ):
                with self.subTest(absent=absent):
                    self.assertNotIn(absent, header)

    async def test_a_session_without_a_task_gets_the_browser_fallback(self) -> None:
        """Same principle as the browser, and the real id is still below."""

        identifier = "task-1754300000-abcdef12"
        self.harness.state(identifier, status=tui.STATUS_RUNNING)
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, identifier)
            header = screen.header_text()
            self.assertIn("abcdef12", header)
            self.assertNotIn(identifier, header)
            # Nothing is hidden from a person debugging, only from the summary.
            self.assertIn(identifier, screen.section_text("diagnostics"))

    async def test_an_untranslated_waiting_reason_is_not_printed(self) -> None:
        self.harness.state(
            "s-odd",
            tui.SUMMARY_RUN_STOPPED,
            status=tui.STATUS_STOPPED,
            waiting_reason="teleport_failure",
        )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-odd")
            # A raw identifier in a summary is a leak, not a warning. The
            # diagnostics block below is where an untranslated fact belongs.
            self.assertNotIn("teleport_failure", screen.header_text())
            self.assertNotIn("attention", screen.visible_sections())
            self.assertIn("teleport_failure", screen.section_text("diagnostics"))

    async def test_a_translated_waiting_reason_is_shown_in_both_languages(
        self,
    ) -> None:
        self.harness.state(
            "s-limit",
            tui.SUMMARY_RUN_STOPPED,
            status=tui.STATUS_STOPPED,
            waiting_reason="step_limit",
        )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-limit")
            self.assertIn(
                tui._WAITING_REASON_TEXT["step_limit"][1],
                screen.header_text() + screen.section_text("attention"),
            )
            with patch.object(tui, "_save_language", lambda value: None):
                app._set_language("ru")
            await pilot.pause()
            self.assertIn(
                tui._WAITING_REASON_TEXT["step_limit"][0],
                screen.header_text() + screen.section_text("attention"),
            )


# ------------------------------------------------------------ 5/6. attention


class AttentionPriorityTests(_OverviewCase):
    """One state is reported, in the order a person would act on it."""

    async def test_a_confirmation_outranks_a_failure(self) -> None:
        self.harness.state("s-both", status=tui.STATUS_RUNNING, task="do the thing")
        self.harness.error("s-both", "provider_error", "upstream refused")
        self.harness.pending_risk("s-both")
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-both", ACTION_REVIEW_RISK)
            attention = screen.section_text("attention")
            self.assertIn(tui._detail_words("needs_confirmation", True), attention)
            # The failure is still published, in its own section.
            self.assertIn("errors", screen.visible_sections())

    async def test_a_failure_is_summarised_above_the_timeline(self) -> None:
        self.harness.state("s-fail", status=tui.STATUS_FAILED, task="do the thing")
        self.harness.error("s-fail", "provider_error", "upstream refused")
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-fail")
            visible = screen.visible_sections()
            self.assertIn("attention", visible)
            self.assertLess(visible.index("attention"), visible.index("timeline"))
            attention = screen.section_text("attention")
            # The first useful fact, and a pointer to the rest.
            self.assertIn("provider_error", attention)
            self.assertIn(tui._detail_words("details_below", True), attention)
            # One short block, not a traceback.
            self.assertLessEqual(len(attention.splitlines()), 4)

    async def test_a_calm_session_shows_no_attention_block_at_all(self) -> None:
        self.harness.state(
            "s-calm",
            tui.SUMMARY_RUN_COMPLETED,
            status=tui.STATUS_COMPLETED,
            task="do the thing",
        )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-calm")
            self.assertNotIn("attention", screen.visible_sections())
            self.assertNotIn(
                tui._detail_words("no_risk", True), screen.rendered_text()
            )


class VerificationHonestyTests(_OverviewCase):
    """Starting a test is not passing it."""

    async def test_a_finished_passing_check_is_the_only_way_to_claim_success(
        self,
    ) -> None:
        self.harness.state("s-pass", status=tui.STATUS_RUNNING, task="do the thing")
        self.harness.tool(
            "s-pass", call_id="c-1", name="checks.run", ok=True, duration_ms=900
        )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-pass")
            self.assertIn(
                tui._detail_words("checks_passed", True),
                screen.section_text("progress"),
            )

    async def test_a_running_check_is_not_a_passing_check(self) -> None:
        self.harness.state("s-running", status=tui.STATUS_RUNNING, task="do it")
        self.harness.tool("s-running", call_id="c-1", name="checks.run")
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-running")
            progress = screen.section_text("progress")
            self.assertIn(tui._detail_words("checks_unknown", True), progress)
            self.assertNotIn(tui._detail_words("checks_passed", True), progress)

    async def test_none_of_the_tempting_proxies_count_as_proof(self) -> None:
        """A diff, a completed run and a finished non-test call prove nothing."""

        self.harness.state(
            "s-proxy",
            tui.SUMMARY_RUN_COMPLETED,
            status=tui.STATUS_COMPLETED,
            task="do it",
            changed_files=["a.py"],
            diff={"files": 1, "insertions": 5},
        )
        self.harness.tool(
            "s-proxy", call_id="c-1", name="repo.edit_file", ok=True, duration_ms=10
        )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-proxy")
            progress = screen.section_text("progress")
            self.assertIn(tui._detail_words("checks_unknown", True), progress)
            self.assertNotIn(tui._detail_words("checks_passed", True), progress)
            # The changes themselves are reported honestly.
            self.assertIn(tui._detail_words("changed_files", True), progress)
            self.assertIn(tui._detail_words("has_diff", True), progress)

    async def test_a_failed_check_reaches_attention_and_progress(self) -> None:
        self.harness.state("s-red", status=tui.STATUS_RUNNING, task="do it")
        self.harness.tool(
            "s-red", call_id="c-1", name="checks.run", ok=False, duration_ms=900
        )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-red")
            self.assertIn(
                tui._detail_words("checks_failed", True),
                screen.section_text("progress"),
            )
            self.assertIn(
                tui._detail_words("checks_failed", True),
                screen.section_text("attention"),
            )

    def test_the_pure_outcome_helper_has_three_answers(self) -> None:
        self.assertIn(
            tui._detail_words("checks_unknown", True), "Verification is not confirmed."
        )
        self.assertEqual(
            tui._detail_words("checks_unknown", False),
            "\u041f\u0440\u043e\u0432\u0435\u0440\u043a\u0430 \u043d\u0435 "
            "\u043f\u043e\u0434\u0442\u0432\u0435\u0440\u0436\u0434\u0435\u043d\u0430.",
        )
        # Neither locale may promise more than the other.
        for name in ("checks_passed", "checks_failed", "checks_unknown"):
            russian, english = tui._DETAIL_TEXT[name]
            with self.subTest(name=name):
                self.assertTrue(russian.strip() and english.strip())
                self.assertEqual(russian.endswith("."), english.endswith("."))


# ------------------------------------------------------- 7. section visibility


class SectionVisibilityTests(_OverviewCase):
    async def test_a_bare_session_hides_every_optional_section(self) -> None:
        self.harness.state("s-bare", status=tui.STATUS_RUNNING, task="do it")
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-bare")
            visible = screen.visible_sections()
            self.assertIn("progress", visible)
            self.assertIn("diagnostics", visible)
            for name in ("usage", "browser", "evidence", "performance", "errors"):
                with self.subTest(name=name):
                    self.assertNotIn(name, visible)
                    widget = screen.query_one(f"#section-{name}", tui.Static)
                    # Hidden, not unmounted: the DOM stays fixed so a section
                    # can appear later without a rebuild.
                    self.assertFalse(widget.display)

    async def test_a_section_appears_when_its_event_arrives(self) -> None:
        self.harness.state("s-grow", status=tui.STATUS_RUNNING, task="do it")
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-grow")
            self.assertNotIn("evidence", screen.visible_sections())
            self.harness.bus.publish(
                EventKind.EVIDENCE,
                session_id="s-grow",
                summary="evidence",
                data={"evidence_id": "e-1", "kind": "test_run"},
            )
            self._drain(app)
            await pilot.pause()
            self.assertIn("evidence", screen.visible_sections())
            self.assertTrue(
                screen.query_one("#section-evidence", tui.Static).display
            )

    async def test_the_screen_is_not_a_wall_of_no_data(self) -> None:
        """The count is the contract: a calm session is short."""

        self.harness.state("s-short", status=tui.STATUS_RUNNING, task="do it")
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-short")
            empty = [
                name
                for name in screen.visible_sections()
                if screen.section_text(name) == tui._detail_words("empty", True)
            ]
            # At most the two always-shown sections may read as empty, and in
            # practice progress always has something to say.
            self.assertLessEqual(len(empty), 1)


# -------------------------------------------------------- 8. redraw discipline


class RedrawDisciplineTests(_OverviewCase):
    async def test_an_event_about_another_session_costs_nothing(self) -> None:
        self.harness.state("s-mine", status=tui.STATUS_RUNNING, task="mine")
        self.harness.state("s-other", status=tui.STATUS_RUNNING, task="other")
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-mine")
            before = screen.redraws
            self.harness.state(
                "s-other", status=tui.STATUS_COMPLETED, task="other"
            )
            self._drain(app)
            self.assertEqual(screen.redraws, before)

    async def test_one_event_about_this_session_costs_one_redraw(self) -> None:
        self.harness.state("s-mine", status=tui.STATUS_RUNNING, task="mine")
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-mine")
            before = screen.redraws
            self.harness.state(
                "s-mine", status=tui.STATUS_RUNNING, task="mine", current_step="x"
            )
            self._drain(app)
            self.assertEqual(screen.redraws, before + 1)

    async def test_an_ordinary_update_does_not_scroll_the_reader_away(
        self,
    ) -> None:
        self.harness.state("s-long", status=tui.STATUS_RUNNING, task="long one")
        for index in range(40):
            self.harness.state(
                "s-long", status=tui.STATUS_RUNNING, current_step=f"step-{index}"
            )
        app = self.harness.app()
        async with app.run_test(size=STANDARD) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-long")
            body = screen.query_one("#session-detail-body", tui.VerticalScroll)
            await pilot.press("pagedown")
            await pilot.pause()
            moved = body.scroll_offset.y
            self.harness.state(
                "s-long", status=tui.STATUS_RUNNING, current_step="step-40"
            )
            self._drain(app)
            await pilot.pause()
            self.assertEqual(body.scroll_offset.y, moved)

    async def test_focus_risk_does_not_repeat_on_every_tick(self) -> None:
        """One scroll on arrival, one when a *new* decision appears, no more.

        A screen that jumps back to the confirmation on every event is one
        nobody can read the timeline of, which is the very thing the reader
        scrolled down to check before deciding.
        """

        self.harness.state("s-risk", status=tui.STATUS_RUNNING, task="do it")
        self.harness.pending_risk("s-risk")
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-risk", ACTION_REVIEW_RISK)
            scrolls: list[str] = []
            with patch.object(
                screen, "scroll_to_section", side_effect=scrolls.append
            ):
                for index in range(5):
                    self.harness.state(
                        "s-risk",
                        status=tui.STATUS_RUNNING,
                        current_step=f"step-{index}",
                    )
                    self._drain(app)
                self.assertEqual(scrolls, [])

                # A different decision is a new thing to look at.
                self.harness.pending_risk("s-risk", digest="e" * 32)
                self._drain(app)
                self.assertEqual(scrolls, ["attention"])

    async def test_the_screen_never_consumes_the_dirty_set(self) -> None:
        """Consuming clears, so a second consumer starves the first."""

        self.harness.state("s-drain", status=tui.STATUS_RUNNING, task="do it")
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-drain")
            with patch.object(
                app._view_store, "consume_dirty", wraps=app._view_store.consume_dirty
            ) as consume:
                screen.refresh_detail()
                screen.apply_changes(("s-drain",))
                consume.assert_not_called()


# ------------------------------------------------------------- 9/10. layout


class ResponsiveDetailTests(_OverviewCase):
    def _seed(self) -> None:
        self.harness.state(
            "s-wide",
            status=tui.STATUS_RUNNING,
            task="fix the failing integration tests in the workspace module",
            provider="openai",
            model="model-a",
            current_step="checks.run",
            changed_files=["a.py", "b.py"],
        )

    async def test_the_dialog_fits_a_46_by_14_terminal(self) -> None:
        self._seed()
        self.harness.error("s-wide", "provider_error", "x" * 300)
        app = self.harness.app()
        async with app.run_test(size=NARROW) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-wide")
            dialog = screen.query_one("#session-detail")
            self.assertLessEqual(dialog.size.width, NARROW[0])
            self.assertLessEqual(dialog.size.height, NARROW[1])
            footer = screen.query_one("#session-detail-hint", tui.Static)
            self.assertEqual(footer.size.height, 1)
            text = self._footer(screen)
            self.assertNotIn("\n", text)
            self.assertLessEqual(len(text), screen.footer_width())
            # The overview is above the scroll, so it cannot be pushed below
            # the fold by a long error.
            self.assertTrue(screen.header_text())
            header = screen.query_one("#session-detail-header", tui.Static)
            self.assertLessEqual(header.size.height, 5)

    async def test_the_overview_precedes_every_technical_section(self) -> None:
        self._seed()
        self.harness.pending_risk("s-wide")
        app = self.harness.app()
        async with app.run_test(size=STANDARD) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-wide", ACTION_REVIEW_RISK)
            visible = screen.visible_sections()
            # Attention, then what the run did, then the record.
            self.assertEqual(visible[0], "attention")
            self.assertEqual(visible[1], "progress")
            self.assertEqual(visible[-1], "diagnostics")
            for technical in ("timeline", "tools"):
                if technical in visible:
                    self.assertGreater(
                        visible.index(technical), visible.index("progress")
                    )

    async def test_a_wide_terminal_does_not_become_a_dashboard(self) -> None:
        self._seed()
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-wide")
            dialog = screen.query_one("#session-detail")
            self.assertLess(dialog.size.width, WIDE[0])
            self.assertLessEqual(dialog.size.width, 100)
            # One column of text, not a grid: the body is the only scrollable
            # region and every section is a child of it.
            self.assertLess(screen.content_width(), WIDE[0])
            self.assertNotEqual(screen.content_width(), screen.footer_width())

    def test_the_footer_is_one_whole_line_at_every_width(self) -> None:
        for english in (True, False):
            for width in range(20, 121):
                text = tui._detail_footer_text(english, width)
                self.assertLessEqual(len(text), width, f"width={width}")
                self.assertNotIn("\n", text)
                self.assertNotIn("\u2026", text)
            # Esc survives to the narrowest width, because it is the one hint
            # a reader cannot infer from a scrollbar.
            self.assertIn(
                "Esc", tui._detail_footer_text(english, 20)
            )
            wide = tui._detail_footer_text(english, 120)
            self.assertIn("PgUp", wide)


class LanguageRefreshTests(_OverviewCase):
    async def test_a_switch_restates_the_whole_open_screen(self) -> None:
        self.harness.state(
            "s-lang",
            status=tui.STATUS_RUNNING,
            task="fix the failing tests",
            current_step="checks.run",
        )
        self.harness.pending_risk("s-lang")
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-lang", ACTION_REVIEW_RISK)
            widgets = len(screen.query(tui.Static))

            with patch.object(tui, "_save_language", lambda value: None):
                app._set_language("ru")
            await pilot.pause()

            self.assertIn(
                tui._SESSION_STATUS_TEXT[tui.STATUS_RUNNING][0], screen.header_text()
            )
            self.assertIn(
                tui._detail_words("needs_confirmation", False),
                screen.section_text("attention"),
            )
            self.assertIn(
                tui._detail_words("checks_unknown", False),
                screen.section_text("progress"),
            )
            title = str(
                screen.query_one("#section-title-attention", tui.Static).render()
            )
            self.assertEqual(title, tui._detail_words("attention", False))
            self.assertIn("Esc", self._footer(screen))
            self.assertIn(
                "\u043d\u0430\u0437\u0430\u0434", self._footer(screen)
            )
            # A redraw, not a rebuild.
            self.assertEqual(len(screen.query(tui.Static)), widgets)

            with patch.object(tui, "_save_language", lambda value: None):
                app._set_language("en")
            await pilot.pause()
            self.assertIn(
                tui._SESSION_STATUS_TEXT[tui.STATUS_RUNNING][1], screen.header_text()
            )


# ---------------------------------------------------------------- 12. security


class PlantedPayloadTests(_OverviewCase):
    """Twelve hostile fixtures, mounted, read back from the real widgets."""

    HOSTILE_TASK = (
        "drop everything\nsecond line\tafter a tab\x07"
        "\x1b[31mred\x1b[0m [bold red]markup[/] " + FAKE_KEY_SHAPED
    )

    async def test_no_secret_reaches_any_rendered_surface(self) -> None:
        self.harness.state(
            "s-secret",
            "\x1b[2J[bold]hostile summary[/]",
            status=tui.STATUS_RUNNING,
            task=self.HOSTILE_TASK,
            current_step="\x1b[2Jrm -rf /",
            authorization=FAKE_BEARER,
            credential_ref=FAKE_CREDENTIAL_REF,
            headers={"Authorization": FAKE_BEARER},
        )
        self.harness.tool(
            "s-secret",
            call_id="c-1",
            name="checks.run",
            ok=False,
            detail=FAKE_KEY_SHAPED,
        )
        self.harness.error("s-secret", "provider_error", FAKE_BEARER)
        self.harness.pending_risk("s-secret", secrets=True)
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-secret", ACTION_REVIEW_RISK)
            rendered = screen.rendered_text()
            for absent in (
                FAKE_APPROVAL_TOKEN,
                FAKE_BEARER,
                FAKE_CREDENTIAL_REF,
                "appr-",
                "bearer-",
                "Authorization",
            ):
                with self.subTest(absent=absent[:16]):
                    self.assertNotIn(absent, rendered)

    async def test_the_overview_normalises_a_hostile_task(self) -> None:
        self.harness.state(
            "s-task", status=tui.STATUS_RUNNING, task=self.HOSTILE_TASK
        )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-task")
            header = screen.header_text()
            # The task is shown -- hiding it would break the screen's first job
            # -- but as plain one-line text on the first line of the overview.
            self.assertTrue(header.startswith("drop everything"))
            first = header.splitlines()[0]
            for forbidden in ("\t", "\x1b", "\x07", "\r"):
                with self.subTest(forbidden=repr(forbidden)):
                    self.assertNotIn(forbidden, first)
            # Markup is inert rather than filtered: the widget is built with
            # `markup=False`, so brackets are characters, not instructions.
            widget = screen.query_one("#session-detail-header", tui.Static)
            self.assertFalse(widget._render_markup)

    async def test_the_digest_stays_out_of_the_summary_surfaces(self) -> None:
        digest = "f" * 32
        self.harness.state("s-digest", status=tui.STATUS_RUNNING, task="do it")
        self.harness.pending_risk("s-digest", digest=digest)
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-digest", ACTION_REVIEW_RISK)
            self.assertNotIn(digest[:16], screen.header_text())
            self.assertNotIn(digest[:16], screen.section_text("attention"))
            # Still available where a person is debugging rather than deciding.
            self.assertIn(digest[:16], screen.section_text("diagnostics"))


# ------------------------------------------------------------ 13. degraded


class DegradedStateTests(_OverviewCase):
    async def test_an_unknown_session_says_so_once_and_stays_usable(self) -> None:
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-nowhere")
            self.assertIn("s-nowhere", screen.header_text())
            self.assertIn("no data", screen.header_text())
            # Not eleven `no data` blocks, which would read as measured
            # emptiness rather than an absent session.
            self.assertLessEqual(len(screen.visible_sections()), 2)
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, tui.SessionDetailScreen)

    async def test_a_persisted_only_session_says_it_has_no_live_activity(
        self,
    ) -> None:
        self.harness.create_session("s-durable", task="restored task")
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-durable")
            self.assertIn("restored task", screen.header_text())
            progress = screen.section_text("progress")
            self.assertIn(tui._detail_words("no_live_activity", True), progress)
            # And the technical sections do not pretend to be measurements.
            for name in ("usage", "performance", "evidence"):
                with self.subTest(name=name):
                    self.assertNotIn(name, screen.visible_sections())

    async def test_truncation_and_dropped_events_stay_visible(self) -> None:
        self.harness.state("s-many", status=tui.STATUS_RUNNING, task="do it")
        for index in range(tui.SessionDetailScreen.MAX_TIMELINE_ROWS + 10):
            self.harness.state(
                "s-many", status=tui.STATUS_RUNNING, current_step=f"step-{index}"
            )
        app = self.harness.app()
        async with app.run_test(size=WIDE) as pilot:
            await pilot.pause()
            screen = await self._open(app, pilot, "s-many")
            timeline = screen.section_text("timeline")
            self.assertIn(tui._detail_words("truncated", True), timeline)
            # The overview must not imply the history above is complete.
            self.assertNotIn(
                tui._detail_words("truncated", True), screen.header_text()
            )


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
