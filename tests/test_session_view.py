"""The UI data layer folds typed events, not log text.

These tests pin the properties the TUI, the CLI and an evidence export all
depend on: the fold is incremental by sequence number, it is bounded and honest
about what it discarded, it never raises on a hostile payload, it carries no
user-facing prose, and it copies only an explicit allowlist of payload keys so a
field added by a future publisher cannot reach a screen by accident.

The token-shaped strings below are fixtures proving a value cannot reach a view,
not credentials.
"""

from __future__ import annotations

import unittest

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.event_bus import EventBus, EventKind, EventLevel
from karox.session_view import (
    ACTION_OPEN,
    ACTION_RESUME,
    ACTION_REVIEW_RISK,
    ACTION_STOP,
    WAIT_CONFIRMATION,
    SessionViewStore,
)

FAKE_TOKEN = "tok-" + ("q" * 28)


class _Clock:
    """A clock that only moves when a test moves it.

    Elapsed time is computed against "now" for a live session, so a real clock
    would make every elapsed assertion a race.
    """

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def _bus() -> EventBus:
    # A fixed timestamp source keeps durations exact rather than approximate.
    ticks = iter(float(value) for value in range(1000, 100000))
    return EventBus(capacity=100, now=lambda: next(ticks))


class SessionBrowserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.clock = _Clock()
        self.store = SessionViewStore(now=self.clock)
        self.bus = _bus()

    def test_a_session_row_answers_every_question_before_opening_it(self) -> None:
        self.bus.publish(
            EventKind.SESSION_STATE,
            session_id="s-1",
            source="chatgpt-web",
            summary="started",
            data={
                "task": "Fix the failing wheel gate",
                "agent": "native",
                "provider": "openai",
                "model": "model-a",
                "workspace_mode": "serialized_write",
                "access_profile": "workspace_write",
                "status": "running",
                "current_step": "reading core.py",
                "changed_files": ["src/karox/core.py", "tests/test_core.py"],
            },
        )
        self.store.sync(self.bus)

        row = self.store.summary("s-1")
        assert row is not None
        self.assertEqual(row.title, "Fix the failing wheel gate")
        self.assertEqual(row.agent, "native")
        self.assertEqual(row.source, "chatgpt-web")
        self.assertEqual(row.provider, "openai")
        self.assertEqual(row.model, "model-a")
        self.assertEqual(row.workspace_mode, "serialized_write")
        self.assertEqual(row.access_profile, "workspace_write")
        self.assertEqual(row.status, "running")
        self.assertEqual(row.current_step, "reading core.py")
        # A list of paths is a count in the browser; the paths belong to the diff view.
        self.assertEqual(row.changed_files, 2)
        self.assertEqual(row.last_event_summary, "started")
        # One action, and for a running session it is the one that stops it.
        self.assertEqual(row.primary_action, ACTION_STOP)

    def test_elapsed_time_runs_for_a_live_session_and_freezes_when_it_ends(self) -> None:
        self.bus.publish(
            EventKind.SESSION_STATE,
            session_id="s-1",
            data={"status": "running"},
        )
        self.store.sync(self.bus)
        self.clock.now = 1030.0
        first = self.store.summary("s-1")
        assert first is not None
        self.assertGreater(first.elapsed_seconds, 0.0)

        self.bus.publish(
            EventKind.SESSION_STATE,
            session_id="s-1",
            data={"status": "finished"},
        )
        self.store.sync(self.bus)
        frozen = self.store.summary("s-1")
        assert frozen is not None
        self.clock.now = 9999.0
        again = self.store.summary("s-1")
        assert again is not None
        # A finished session must not keep counting: its duration is a fact.
        self.assertEqual(frozen.elapsed_seconds, again.elapsed_seconds)

    def test_rows_are_ordered_by_activity_with_a_total_tie_break(self) -> None:
        for session in ("s-a", "s-b", "s-c"):
            self.bus.publish(
                EventKind.SESSION_STATE, session_id=session, data={"status": "running"}
            )
        self.bus.publish(
            EventKind.AGENT_ACTION, session_id="s-a", data={"step": "newest"}
        )
        self.store.sync(self.bus)
        order = [row.session_id for row in self.store.summaries()]
        self.assertEqual(order, ["s-a", "s-c", "s-b"])
        # Repeated reads cannot reorder rows under the user's cursor.
        self.assertEqual(order, [row.session_id for row in self.store.summaries()])

    def test_a_waiting_session_offers_resume_and_a_finished_one_offers_open(self) -> None:
        self.bus.publish(
            EventKind.SESSION_STATE, session_id="s-1", data={"status": "paused"}
        )
        self.bus.publish(
            EventKind.SESSION_STATE, session_id="s-2", data={"status": "failed"}
        )
        self.store.sync(self.bus)
        first = self.store.summary("s-1")
        second = self.store.summary("s-2")
        assert first is not None and second is not None
        self.assertEqual(first.primary_action, ACTION_RESUME)
        self.assertEqual(second.primary_action, ACTION_OPEN)

    def test_a_durable_record_fills_a_session_that_has_no_events_yet(self) -> None:
        """A session created a second ago is the one a user is looking at."""

        self.store.merge_record(
            {
                "session_id": "s-new",
                "task": "Investigate the CRLF drift",
                "access_profile": "read_only",
                "status": "active",
                "created_at": 900.0,
                "updated_at": 950.0,
                "changed_files": 3,
            }
        )
        row = self.store.summary("s-new")
        assert row is not None
        self.assertEqual(row.title, "Investigate the CRLF drift")
        self.assertEqual(row.access_profile, "read_only")
        self.assertEqual(row.status, "active")
        self.assertEqual(row.changed_files, 3)

    def test_an_event_reported_status_wins_over_the_file_on_disk(self) -> None:
        self.bus.publish(
            EventKind.SESSION_STATE, session_id="s-1", data={"status": "running"}
        )
        self.store.sync(self.bus)
        self.store.merge_record({"session_id": "s-1", "status": "active"})
        row = self.store.summary("s-1")
        assert row is not None
        # The record is a snapshot; the stream is current.
        self.assertEqual(row.status, "running")


class IncrementalRefreshTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SessionViewStore(now=_Clock())
        self.bus = _bus()

    def test_sync_folds_only_what_is_new(self) -> None:
        self.bus.publish(EventKind.AGENT_ACTION, session_id="s-1", summary="one")
        self.assertEqual(self.store.sync(self.bus), 1)
        # Nothing happened in between, so a refresh is free.
        self.assertEqual(self.store.sync(self.bus), 0)
        self.bus.publish(EventKind.AGENT_ACTION, session_id="s-1", summary="two")
        self.assertEqual(self.store.sync(self.bus), 1)
        self.assertEqual(self.store.cursor, self.bus.latest_seq())

    def test_a_live_subscription_and_a_backfill_do_not_double_count(self) -> None:
        detach = self.store.attach(self.bus)
        try:
            self.bus.publish(EventKind.AGENT_ACTION, session_id="s-1", summary="live")
            # The same event is already folded, so the backfill folds nothing.
            self.assertEqual(self.store.sync(self.bus), 0)
            self.assertEqual(self.store.applied, 1)
        finally:
            detach()
        self.bus.publish(EventKind.AGENT_ACTION, session_id="s-1", summary="after")
        self.assertEqual(self.store.applied, 1)

    def test_a_burst_marks_a_session_dirty_once(self) -> None:
        """This is what makes a debounced redraw possible at all."""

        for index in range(50):
            self.bus.publish(
                EventKind.AGENT_ACTION, session_id="s-1", summary=f"step {index}"
            )
        self.bus.publish(EventKind.AGENT_ACTION, session_id="s-2", summary="other")
        self.store.sync(self.bus)
        self.assertEqual(self.store.consume_dirty(), ("s-1", "s-2"))
        # Consuming clears it, so an idle tick redraws nothing.
        self.assertEqual(self.store.consume_dirty(), ())

    def test_the_timeline_can_be_read_incrementally(self) -> None:
        for index in range(5):
            self.bus.publish(
                EventKind.AGENT_ACTION, session_id="s-1", summary=f"step {index}"
            )
        self.store.sync(self.bus)
        entries = self.store.timeline("s-1")
        self.assertEqual(len(entries), 5)
        tail = self.store.timeline("s-1", since_seq=entries[2].seq)
        self.assertEqual([item.summary for item in tail], ["step 3", "step 4"])

    def test_the_timeline_is_bounded_and_says_what_it_discarded(self) -> None:
        store = SessionViewStore(timeline_limit=10, now=_Clock())
        for index in range(25):
            self.bus.publish(
                EventKind.AGENT_ACTION, session_id="s-1", summary=f"step {index}"
            )
        store.sync(self.bus)
        detail = store.detail("s-1")
        assert detail is not None
        self.assertEqual(len(detail.timeline), 10)
        self.assertEqual(detail.truncated_timeline, 15)
        self.assertEqual(detail.timeline[-1].summary, "step 24")

    def test_dropped_bus_events_are_reported_not_hidden(self) -> None:
        bus = EventBus(capacity=3)
        for index in range(10):
            bus.publish(EventKind.AGENT_ACTION, session_id="s-1", summary=str(index))
        self.store.sync(bus)
        detail = self.store.detail("s-1")
        assert detail is not None
        self.assertEqual(detail.dropped_events, bus.dropped)
        self.assertGreater(detail.dropped_events, 0)

    def test_tracked_sessions_are_bounded_and_evictions_are_counted(self) -> None:
        store = SessionViewStore(session_limit=3, now=_Clock())
        for index in range(6):
            self.bus.publish(
                EventKind.AGENT_ACTION, session_id=f"s-{index}", summary="x"
            )
        store.sync(self.bus)
        self.assertEqual(len(store.session_ids()), 3)
        self.assertEqual(store.evicted_sessions, 3)
        self.assertIsNone(store.summary("s-0"))
        self.assertIsNotNone(store.summary("s-5"))

    def test_a_large_history_folds_once_per_event(self) -> None:
        bus = EventBus(capacity=5000)
        for index in range(2000):
            bus.publish(
                EventKind.TOOL_CALL,
                session_id=f"s-{index % 20}",
                summary="repo.read",
                data={"call_id": f"c-{index}", "tool": "repo.read", "phase": "started"},
            )
        store = SessionViewStore(now=_Clock())
        self.assertEqual(store.sync(bus), 2000)
        self.assertEqual(store.applied, 2000)
        self.assertEqual(len(store.session_ids()), 20)
        # A second sync over the same 2000 events does no work at all.
        self.assertEqual(store.sync(bus), 0)


class ToolCallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SessionViewStore(now=_Clock())
        self.bus = _bus()

    def test_a_tool_call_gets_a_real_duration_from_the_stream(self) -> None:
        self.bus.publish(
            EventKind.TOOL_CALL,
            session_id="s-1",
            summary="repo.write",
            data={"call_id": "c-1", "tool": "repo.write", "phase": "started"},
        )
        self.bus.publish(
            EventKind.TOOL_CALL,
            session_id="s-1",
            summary="repo.write ok",
            data={
                "call_id": "c-1",
                "tool": "repo.write",
                "phase": "finished",
                "ok": True,
                "duration_ms": 12.5,
            },
        )
        self.store.sync(self.bus)
        detail = self.store.detail("s-1")
        assert detail is not None
        self.assertEqual(len(detail.tool_calls), 1)
        call = detail.tool_calls[0]
        self.assertEqual(call.name, "repo.write")
        self.assertEqual(call.duration_ms, 12.5)
        self.assertTrue(call.ok)
        self.assertFalse(call.running)

    def test_a_duration_is_derived_when_the_publisher_omits_it(self) -> None:
        self.bus.publish(
            EventKind.TOOL_CALL,
            session_id="s-1",
            data={"call_id": "c-1", "tool": "tests.run", "phase": "started"},
        )
        self.bus.publish(
            EventKind.TOOL_CALL,
            session_id="s-1",
            data={"call_id": "c-1", "phase": "finished", "ok": False},
        )
        self.store.sync(self.bus)
        detail = self.store.detail("s-1")
        assert detail is not None
        call = detail.tool_calls[0]
        # The bus clock advanced one second between the two events.
        self.assertEqual(call.duration_ms, 1000.0)
        self.assertEqual(call.name, "tests.run")
        self.assertIs(call.ok, False)

    def test_a_three_tool_turn_keeps_all_three(self) -> None:
        """The old activity line overwrote itself; the fold must not."""

        for index in range(3):
            self.bus.publish(
                EventKind.TOOL_CALL,
                session_id="s-1",
                data={
                    "call_id": f"c-{index}",
                    "tool": f"tool-{index}",
                    "phase": "started",
                },
            )
        self.store.sync(self.bus)
        detail = self.store.detail("s-1")
        assert detail is not None
        self.assertEqual(
            [call.name for call in detail.tool_calls],
            ["tool-0", "tool-1", "tool-2"],
        )
        self.assertTrue(all(call.running for call in detail.tool_calls))

    def test_tool_calls_are_bounded(self) -> None:
        store = SessionViewStore(tool_call_limit=4, now=_Clock())
        for index in range(10):
            self.bus.publish(
                EventKind.TOOL_CALL,
                session_id="s-1",
                data={"call_id": f"c-{index}", "tool": "repo.read", "phase": "started"},
            )
        store.sync(self.bus)
        detail = store.detail("s-1")
        assert detail is not None
        self.assertEqual(len(detail.tool_calls), 4)
        self.assertEqual(detail.tool_calls[-1].call_id, "c-9")


class RiskAndConfirmationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SessionViewStore(now=_Clock())
        self.bus = _bus()

    def _stop(self, digest: str = "d" * 64) -> None:
        """Publish exactly what karox.core publishes when Smart Stop fires."""

        self.bus.publish(
            EventKind.RISK_DECISION,
            session_id="s-1",
            summary="git.push: critical",
            level=EventLevel.WARNING,
            data={
                "allowed": False,
                "reason": "confirmation_required",
                "risk": "critical",
                "reasons": ["push", "irreversible"],
                "action_digest": digest,
                "preview": {"file_count": 12},
            },
        )

    def test_a_stopped_action_becomes_a_pending_confirmation(self) -> None:
        self._stop()
        self.store.sync(self.bus)
        row = self.store.summary("s-1")
        detail = self.store.detail("s-1")
        assert row is not None and detail is not None
        self.assertEqual(row.waiting_reason, WAIT_CONFIRMATION)
        # A stopped agent is waiting on the person reading this row, so that
        # outranks stop and resume.
        self.assertEqual(row.primary_action, ACTION_REVIEW_RISK)
        assert detail.pending_confirmation is not None
        self.assertEqual(detail.pending_confirmation.level, "critical")
        self.assertEqual(
            detail.pending_confirmation.reasons, ("push", "irreversible")
        )
        self.assertEqual(detail.pending_confirmation.preview, {"file_count": 12})

    def test_a_decision_clears_the_pending_confirmation(self) -> None:
        self._stop()
        self.store.sync(self.bus)
        self.bus.publish(
            EventKind.CONFIRMATION,
            session_id="s-1",
            summary="approved",
            data={"decision": "approved", "action_digest": "d" * 64},
        )
        self.store.sync(self.bus)
        detail = self.store.detail("s-1")
        row = self.store.summary("s-1")
        assert detail is not None and row is not None
        self.assertIsNone(detail.pending_confirmation)
        self.assertEqual(row.waiting_reason, "")

    def test_a_new_verdict_for_the_same_action_clears_the_stop(self) -> None:
        digest = "e" * 64
        self._stop(digest)
        self.store.sync(self.bus)
        self.bus.publish(
            EventKind.RISK_DECISION,
            session_id="s-1",
            data={
                "allowed": True,
                "reason": "allowed",
                "risk": "critical",
                "action_digest": digest,
            },
        )
        self.store.sync(self.bus)
        detail = self.store.detail("s-1")
        assert detail is not None
        self.assertIsNone(detail.pending_confirmation)
        assert detail.summary.risk is not None
        self.assertTrue(detail.summary.risk.allowed)

    def test_a_stop_for_a_different_action_does_not_clear_the_first(self) -> None:
        self._stop("a" * 64)
        self.store.sync(self.bus)
        self.bus.publish(
            EventKind.RISK_DECISION,
            session_id="s-1",
            data={
                "allowed": True,
                "reason": "allowed",
                "risk": "low",
                "action_digest": "b" * 64,
            },
        )
        self.store.sync(self.bus)
        detail = self.store.detail("s-1")
        assert detail is not None
        # Approving a read must not silently release a blocked push.
        assert detail.pending_confirmation is not None
        self.assertEqual(detail.pending_confirmation.action_digest, "a" * 64)

    def test_no_confirmation_token_can_reach_a_view(self) -> None:
        """The ledger keeps tokens out of events; this layer has no key for one."""

        self.bus.publish(
            EventKind.CONFIRMATION,
            session_id="s-1",
            summary="requested",
            data={
                "decision": "requested",
                "action_digest": "f" * 64,
                "confirmation_token": FAKE_TOKEN,
                "token": FAKE_TOKEN,
            },
        )
        self.store.sync(self.bus)
        detail = self.store.detail("s-1")
        assert detail is not None
        rendered = repr(detail.to_dict())
        self.assertNotIn(FAKE_TOKEN, rendered)
        self.assertNotIn("confirmation_token", rendered)


class AllowlistAndRobustnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SessionViewStore(now=_Clock())
        self.bus = _bus()

    def test_only_allowlisted_payload_keys_reach_a_timeline_entry(self) -> None:
        self.bus.publish(
            EventKind.TOOL_CALL,
            session_id="s-1",
            summary="repo.write",
            data={
                "call_id": "c-1",
                "tool": "repo.write",
                "phase": "started",
                # Nothing asked for these, so nothing displays them.
                "internal_cursor": "private",
                "future_field": {"nested": "value"},
            },
        )
        self.store.sync(self.bus)
        entry = self.store.timeline("s-1")[0]
        self.assertEqual(set(entry.data), {"call_id", "tool", "phase"})

    def test_a_hostile_payload_cannot_break_the_fold(self) -> None:
        class Hostile:
            def __str__(self) -> str:
                raise RuntimeError("no")

        self.bus.publish(
            EventKind.SESSION_STATE,
            session_id="s-1",
            data={"status": "running", "current_step": Hostile()},
        )
        self.bus.publish(
            EventKind.AGENT_ACTION, session_id="s-1", summary="still working"
        )
        self.store.sync(self.bus)
        row = self.store.summary("s-1")
        assert row is not None
        # The bad event was dropped; the good one after it still folded.
        self.assertEqual(row.last_event_summary, "still working")

    def test_nonsense_numbers_and_types_are_ignored_rather_than_shown(self) -> None:
        self.bus.publish(
            EventKind.AGENT_ACTION,
            session_id="s-1",
            data={
                "usage": {"total_tokens": "lots", "input_tokens": True},
                "cost": {"USD": None},
                "budgets": {"usage": {"used": float("nan"), "limit": "none"}},
            },
        )
        self.store.sync(self.bus)
        detail = self.store.detail("s-1")
        assert detail is not None
        self.assertEqual(detail.usage, {})
        self.assertEqual(detail.costs, {})
        self.assertEqual(detail.summary.token_budget.used, 0.0)
        self.assertIsNone(detail.summary.token_budget.limit)

    def test_an_event_without_a_session_advances_the_cursor_and_nothing_else(self) -> None:
        self.bus.publish(EventKind.HEALTH_CHANGE, session_id="", summary="global")
        self.store.sync(self.bus)
        self.assertEqual(self.store.session_ids(), ())
        self.assertEqual(self.store.cursor, 1)

    def test_a_view_model_carries_no_user_facing_prose(self) -> None:
        """Translation belongs to the catalogs, not to the data layer."""

        self.bus.publish(
            EventKind.SESSION_STATE, session_id="s-1", data={"status": "paused"}
        )
        self.store.sync(self.bus)
        row = self.store.summary("s-1")
        assert row is not None
        self.assertEqual(row.primary_action, ACTION_RESUME)
        # Stable identifiers, so both languages render from the same row.
        self.assertRegex(row.primary_action, r"^[a-z_]+$")
        self.assertRegex(row.status, r"^[a-z_]+$")


class DetailViewTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = SessionViewStore(now=_Clock())
        self.bus = _bus()

    def test_usage_cost_and_budgets_accumulate(self) -> None:
        self.bus.publish(
            EventKind.AGENT_ACTION,
            session_id="s-1",
            data={
                "usage": {"total_tokens": 1200},
                "cost": {"USD": 0.42},
                "budgets": {
                    "usage": {"limit": 4000},
                    "cost": {"limit": 2.0, "unit": "USD"},
                },
            },
        )
        self.store.sync(self.bus)
        detail = self.store.detail("s-1")
        assert detail is not None
        self.assertEqual(detail.usage["total_tokens"], 1200.0)
        self.assertEqual(detail.costs["USD"], 0.42)
        tokens = detail.summary.token_budget
        self.assertEqual((tokens.used, tokens.limit), (1200.0, 4000.0))
        self.assertAlmostEqual(tokens.fraction or 0.0, 0.3)
        self.assertFalse(tokens.exhausted)
        self.assertEqual(detail.summary.cost_budget.remaining, 2.0 - 0.42)

    def test_a_budget_cannot_be_published_under_a_secret_shaped_name(self) -> None:
        """Why budgets live under ``budgets`` rather than ``token_budget``.

        ``security.redact`` blanks any key containing "token", on the way into
        the bus, before this layer sees anything. That is correct and must not be
        widened for a UI's convenience -- so this pins the consequence, and a
        future publisher that reinvents ``token_budget`` or ``budgets.tokens``
        fails here rather than silently reporting a session with no limit.
        """

        self.bus.publish(
            EventKind.AGENT_ACTION,
            session_id="s-1",
            data={
                "token_budget": {"limit": 4000},
                "budgets": {
                    "tokens": {"limit": 4000},
                    "usage": {"limit": 4000, "unit": "tokens"},
                },
            },
        )
        self.store.sync(self.bus)
        raw = self.bus.snapshot()[0]
        # Both spellings containing the word are destroyed, at both nesting depths.
        self.assertEqual(raw.data["token_budget"], "[REDACTED]")
        self.assertEqual(raw.data["budgets"]["tokens"], "[REDACTED]")
        # The legal name survived, and the unit is a value so it is untouched.
        self.assertEqual(raw.data["budgets"]["usage"]["limit"], 4000)
        row = self.store.summary("s-1")
        assert row is not None
        self.assertEqual(row.token_budget.limit, 4000.0)
        self.assertEqual(row.token_budget.unit, "tokens")
        self.assertNotIn("token_budget", self.store.timeline("s-1")[0].data)

    def test_performance_is_split_by_component(self) -> None:
        for component, duration in (("provider", 900.0), ("karox", 3.0), ("karox", 5.0)):
            self.bus.publish(
                EventKind.PERFORMANCE_SPAN,
                session_id="s-1",
                data={
                    "span": "dispatch",
                    "component": component,
                    "duration_ms": duration,
                },
            )
        self.store.sync(self.bus)
        detail = self.store.detail("s-1")
        assert detail is not None
        self.assertEqual(detail.performance["karox"]["count"], 2.0)
        self.assertEqual(detail.performance["karox"]["total_ms"], 8.0)
        self.assertEqual(detail.performance["provider"]["max_ms"], 900.0)

    def test_errors_are_collected_and_counted(self) -> None:
        self.bus.publish(
            EventKind.ERROR,
            session_id="s-1",
            summary="tests failed",
            level=EventLevel.ERROR,
            data={"code": "verification_failed"},
        )
        self.store.sync(self.bus)
        detail = self.store.detail("s-1")
        assert detail is not None
        self.assertEqual(detail.summary.error_count, 1)
        self.assertEqual(detail.errors[0].data["code"], "verification_failed")

    def test_browser_git_diff_and_evidence_reach_the_detail_view(self) -> None:
        self.bus.publish(
            EventKind.BROWSER_ACTION,
            session_id="s-1",
            data={
                "action": "click",
                "tab_id": "t-1",
                "origin": "http://127.0.0.1:3000",
                "navigation_generation": 4,
                "takeover": False,
            },
        )
        self.bus.publish(
            EventKind.SESSION_STATE,
            session_id="s-1",
            data={
                "status": "running",
                "git": {"branch": "feat/x", "dirty": "yes"},
                "diff": {"files": 3, "insertions": 40, "deletions": 5},
            },
        )
        self.bus.publish(
            EventKind.EVIDENCE,
            session_id="s-1",
            data={"evidence_id": "ev-1", "kind": "check", "summary": "ruff passed"},
        )
        self.store.sync(self.bus)
        detail = self.store.detail("s-1")
        assert detail is not None
        self.assertEqual(detail.browser["action"], "click")
        self.assertEqual(detail.browser["navigation_generation"], 4)
        self.assertIs(detail.browser["takeover"], False)
        self.assertEqual(detail.git["branch"], "feat/x")
        self.assertEqual(detail.diff["insertions"], 40.0)
        self.assertEqual(detail.evidence[0]["evidence_id"], "ev-1")

    def test_the_whole_store_serialises_for_a_json_interface(self) -> None:
        self.bus.publish(
            EventKind.SESSION_STATE,
            session_id="s-1",
            data={"status": "running", "task": "do the thing"},
        )
        self.store.sync(self.bus)
        payload = self.store.to_dict()
        self.assertEqual(payload["cursor"], self.bus.latest_seq())
        self.assertEqual(payload["sessions"][0]["session_id"], "s-1")
        self.assertEqual(payload["sessions"][0]["title"], "do the thing")


class SubscriberIsolationTests(unittest.TestCase):
    def test_a_raising_ui_subscriber_cannot_stop_the_fold(self) -> None:
        bus = _bus()
        store = SessionViewStore(now=_Clock())

        def broken(event: object) -> None:
            raise RuntimeError("the UI crashed")

        bus.subscribe(broken)
        detach = store.attach(bus)
        try:
            bus.publish(
                EventKind.SESSION_STATE, session_id="s-1", data={"status": "running"}
            )
        finally:
            detach()
        row = store.summary("s-1")
        assert row is not None
        self.assertEqual(row.status, "running")
        self.assertEqual(bus.subscriber_errors, 1)


if __name__ == "__main__":  # pragma: no cover - manual invocation
    unittest.main()
