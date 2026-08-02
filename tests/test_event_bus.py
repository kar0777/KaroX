"""The event stream is what the UI reads instead of process stdout.

These tests pin the four properties the rest of the product depends on: the
buffer is bounded and honest about what it dropped, payloads are redacted on
the way in, a broken subscriber cannot stop an agent, and a UI can refresh
incrementally by sequence number.

The token-shaped strings below are redaction fixtures, not credentials.
"""

from __future__ import annotations

import json
import threading
import unittest

from _support import SRC  # noqa: F401 - inserts src on sys.path

from karox.event_bus import (
    DEFAULT_CAPACITY,
    Event,
    EventBus,
    EventKind,
    EventLevel,
    event_bus,
)

# Built at runtime so the literal never appears in the file as one string.
FAKE_API_KEY = "sk-" + ("a" * 32)
FAKE_PASTED_VALUE = "pasted-value-" + ("z" * 12)


class _Clock:
    def __init__(self, start: float = 100.0) -> None:
        self.now = start

    def __call__(self) -> float:
        self.now += 1.0
        return self.now


class EventBusBasicsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = EventBus(capacity=5, now=_Clock())

    def test_events_are_typed_ordered_and_timestamped(self) -> None:
        first = self.bus.publish(
            EventKind.TOOL_CALL, session_id="s-1", summary="repo.read"
        )
        second = self.bus.publish(
            EventKind.AGENT_ACTION, session_id="s-1", summary="thinking"
        )
        self.assertIsInstance(first, Event)
        self.assertEqual((first.seq, second.seq), (1, 2))
        self.assertLess(first.timestamp, second.timestamp)
        self.assertEqual(first.kind, EventKind.TOOL_CALL)
        self.assertEqual(self.bus.latest_seq(), 2)

    def test_a_capacity_limit_bounds_memory_and_is_reported(self) -> None:
        for index in range(9):
            self.bus.publish(
                EventKind.TOOL_CALL, session_id="s-1", summary=f"call {index}"
            )
        buffered = self.bus.snapshot()
        self.assertEqual(len(buffered), 5)
        # Honest about loss rather than pretending the history is complete.
        self.assertEqual(self.bus.dropped, 4)
        self.assertEqual([event.summary for event in buffered][0], "call 4")

    def test_a_ui_can_refresh_incrementally(self) -> None:
        for index in range(3):
            self.bus.publish(EventKind.TOOL_CALL, session_id="s-1", summary=str(index))
        marker = self.bus.latest_seq()
        self.bus.publish(EventKind.TOOL_CALL, session_id="s-1", summary="new")
        fresh = self.bus.snapshot(since_seq=marker)
        self.assertEqual([event.summary for event in fresh], ["new"])

    def test_snapshot_filters_by_kind_and_session(self) -> None:
        self.bus.publish(EventKind.TOOL_CALL, session_id="s-1", summary="a")
        self.bus.publish(EventKind.BROWSER_ACTION, session_id="s-1", summary="b")
        self.bus.publish(EventKind.TOOL_CALL, session_id="s-2", summary="c")
        by_kind = self.bus.snapshot(kinds=(EventKind.BROWSER_ACTION,))
        self.assertEqual([event.summary for event in by_kind], ["b"])
        by_session = self.bus.snapshot(session_id="s-2")
        self.assertEqual([event.summary for event in by_session], ["c"])

    def test_clearing_events_does_not_reset_the_sequence(self) -> None:
        # Clearing the log view must not make a later event look older than one
        # a UI already rendered.
        self.bus.publish(EventKind.TOOL_CALL, session_id="s-1", summary="a")
        self.bus.clear()
        self.assertEqual(self.bus.snapshot(), [])
        nxt = self.bus.publish(EventKind.TOOL_CALL, session_id="s-1", summary="b")
        self.assertEqual(nxt.seq, 2)

    def test_a_zero_capacity_bus_is_refused(self) -> None:
        for capacity in (0, -1):
            with self.subTest(capacity=capacity):
                with self.assertRaises(ValueError):
                    EventBus(capacity=capacity)

    def test_long_summaries_are_clipped_to_one_ui_line(self) -> None:
        event = self.bus.publish(
            EventKind.ERROR, session_id="s-1", summary="boom " * 400
        )
        self.assertLessEqual(len(event.summary), 300)

    def test_newlines_never_break_a_single_line_renderer(self) -> None:
        event = self.bus.publish(
            EventKind.ERROR, session_id="s-1", summary="line one\nline two"
        )
        self.assertNotIn("\n", event.summary)


class RedactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = EventBus(capacity=10)

    def test_secret_shaped_keys_never_enter_the_buffer(self) -> None:
        event = self.bus.publish(
            EventKind.CONNECTION_STATE,
            session_id="s-1",
            summary="connected",
            data={"url": "https://example.test/mcp", "authorization": "abc123"},
        )
        self.assertEqual(event.data["url"], "https://example.test/mcp")
        self.assertEqual(event.data["authorization"], "[REDACTED]")

    def test_a_known_secret_value_is_removed_even_under_a_harmless_key(self) -> None:
        event = self.bus.publish(
            EventKind.CONNECTION_STATE,
            session_id="s-1",
            summary=f"pasted into field: {FAKE_PASTED_VALUE}",
            data={"note": f"use {FAKE_PASTED_VALUE} here"},
            secrets=(FAKE_PASTED_VALUE,),
        )
        blob = json.dumps(event.to_dict())
        self.assertNotIn(FAKE_PASTED_VALUE, blob)

    def test_a_support_export_carries_no_credentials(self) -> None:
        self.bus.publish(
            EventKind.TOOL_CALL,
            session_id="s-1",
            summary="call",
            data={"api_key": FAKE_API_KEY, "ok": True},
        )
        record = self.bus.export_support_record()
        blob = json.dumps(record)
        self.assertNotIn(FAKE_API_KEY, blob)
        self.assertEqual(record["buffered"], 1)
        self.assertEqual(record["dropped"], 0)

    def test_a_support_export_is_bounded(self) -> None:
        bus = EventBus(capacity=100)
        for index in range(100):
            bus.publish(EventKind.TOOL_CALL, session_id="s-1", summary=str(index))
        self.assertEqual(len(bus.export_support_record(limit=10)["events"]), 10)


class SubscriberTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = EventBus(capacity=10)

    def test_subscribers_receive_events_and_can_unsubscribe(self) -> None:
        seen: list[str] = []
        stop = self.bus.subscribe(lambda event: seen.append(event.summary))
        self.bus.publish(EventKind.TOOL_CALL, session_id="s-1", summary="a")
        stop()
        self.bus.publish(EventKind.TOOL_CALL, session_id="s-1", summary="b")
        self.assertEqual(seen, ["a"])

    def test_a_broken_subscriber_cannot_stop_an_agent(self) -> None:
        delivered: list[str] = []

        def explode(event: Event) -> None:
            raise RuntimeError("the UI is broken")

        self.bus.subscribe(explode)
        self.bus.subscribe(lambda event: delivered.append(event.summary))
        self.bus.publish(EventKind.TOOL_CALL, session_id="s-1", summary="work")
        self.assertEqual(delivered, ["work"])
        self.assertEqual(self.bus.subscriber_errors, 1)

    def test_a_subscriber_may_publish_without_deadlocking(self) -> None:
        # Delivery happens outside the lock precisely so a UI callback that
        # reacts by publishing does not freeze the process.
        def echo(event: Event) -> None:
            if event.summary == "ping":
                self.bus.publish(
                    EventKind.AGENT_ACTION, session_id="s-1", summary="pong"
                )

        self.bus.subscribe(echo)
        finished = threading.Event()

        def run() -> None:
            self.bus.publish(EventKind.TOOL_CALL, session_id="s-1", summary="ping")
            finished.set()

        worker = threading.Thread(target=run, daemon=True)
        worker.start()
        worker.join(timeout=5.0)
        self.assertTrue(finished.is_set(), "publishing from a subscriber deadlocked")
        self.assertIn("pong", [event.summary for event in self.bus.snapshot()])

    def test_a_non_callable_subscriber_is_refused(self) -> None:
        with self.assertRaises(TypeError):
            self.bus.subscribe("not callable")  # type: ignore[arg-type]


class SpanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.bus = EventBus(capacity=10)
        self.ticks = iter([0.0, 0.25])

    def _monotonic(self) -> float:
        return next(self.ticks)

    def test_a_span_separates_karox_overhead_from_the_slow_thing(self) -> None:
        with self.bus.span(
            "browser.click",
            session_id="s-1",
            component="browser",
            monotonic=self._monotonic,
        ) as extra:
            extra["tab"] = "main"
        event = self.bus.snapshot()[-1]
        self.assertEqual(event.kind, EventKind.PERFORMANCE_SPAN)
        self.assertEqual(event.data["component"], "browser")
        self.assertEqual(event.data["duration_ms"], 250.0)
        self.assertEqual(event.data["tab"], "main")

    def test_a_failing_path_is_still_measured(self) -> None:
        with self.assertRaises(ValueError):
            with self.bus.span(
                "repo.write", session_id="s-1", monotonic=self._monotonic
            ):
                raise ValueError("disk full")
        event = self.bus.snapshot()[-1]
        self.assertEqual(event.data["failed"], "ValueError")
        self.assertEqual(event.level, EventLevel.WARNING)


class SharedBusTests(unittest.TestCase):
    def test_one_bus_describes_the_process(self) -> None:
        self.assertIs(event_bus(), event_bus())
        self.assertEqual(event_bus().capacity, DEFAULT_CAPACITY)


if __name__ == "__main__":
    unittest.main()
