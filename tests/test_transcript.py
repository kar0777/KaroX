"""Typed process-boundary transcript store — Phase 3 foundation tests."""

from __future__ import annotations

import sqlite3
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
import unittest
from pathlib import Path
from unittest.mock import patch

from karox.transcript import (
    DEFAULT_RETENTION,
    EVENT_TYPES,
    TRANSCRIPT_SCHEMA_VERSION,
    TranscriptStore,
    TypedEvent,
)


class TypedEventDataclassTests(unittest.TestCase):
    def test_valid_event(self) -> None:
        e = TypedEvent(
            event_id="e1", session_id="s1", sequence=0, timestamp=1.0,
            source_process="test", kind="ToolCallStarted", payload={"tool": "read"},
        )
        self.assertEqual(e.kind, "ToolCallStarted")
        self.assertEqual(e.schema_version, TRANSCRIPT_SCHEMA_VERSION)

    def test_unknown_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TypedEvent(
                event_id="e1", session_id="s1", sequence=0, timestamp=1.0,
                source_process="t", kind="BogusKind", payload={},
            )

    def test_payload_redacts_secret_fields(self) -> None:
        e = TypedEvent(
            event_id="e1", session_id="s1", sequence=0, timestamp=1.0,
            source_process="t", kind="ToolCallStarted",
            payload={"tool": "read", "api_key": "super-secret", "nested": {"token": "x"}},
        )
        self.assertEqual(e.payload["api_key"], "[REDACTED]")
        self.assertEqual(e.payload["nested"]["token"], "[REDACTED]")
        self.assertEqual(e.payload["tool"], "read")

    def test_empty_event_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            TypedEvent(
                event_id="", session_id="s1", sequence=0, timestamp=1.0,
                source_process="t", kind="ErrorRecorded", payload={},
            )


class TranscriptStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        # unittest cleanups are LIFO and run after tearDown. Close every store
        # before deleting its directory, including stores opened by regressions.
        self.addCleanup(self._tmp.cleanup)
        self.store = TranscriptStore(Path(self._tmp.name) / "transcript.db")

    def tearDown(self) -> None:
        self.store.close()

    def test_append_and_replay_ordered(self) -> None:
        for i in range(5):
            self.store.append(
                session_id="s1", kind="ToolCallStarted",
                payload={"index": i}, source_process="test",
            )
        events = list(self.store.replay("s1"))
        self.assertEqual(len(events), 5)
        for i, e in enumerate(events):
            self.assertEqual(e.sequence, i)
            self.assertEqual(e.payload["index"], i)

    def test_idempotent_append_same_event_id(self) -> None:
        e1 = self.store.append(
            session_id="s1", kind="AgentStepStarted", payload={},
            event_id="fixed-id", source_process="test",
        )
        e2 = self.store.append(
            session_id="s1", kind="AgentStepStarted", payload={},
            event_id="fixed-id", source_process="test",
        )
        self.assertEqual(e1.event_id, e2.event_id)
        self.assertEqual(self.store.count("s1"), 1)

    def test_sessions_are_independent(self) -> None:
        self.store.append(session_id="s1", kind="ToolCallStarted", payload={}, source_process="t")
        self.store.append(session_id="s2", kind="ToolCallStarted", payload={}, source_process="t")
        self.assertEqual(self.store.count("s1"), 1)
        self.assertEqual(self.store.count("s2"), 1)
        self.assertEqual(self.store.latest_sequence("s1"), 0)
        self.assertEqual(self.store.latest_sequence("s2"), 0)

    def test_replay_from_sequence(self) -> None:
        for i in range(10):
            self.store.append(session_id="s1", kind="ToolCallProgress", payload={}, source_process="t")
        events = list(self.store.replay("s1", from_sequence=5))
        self.assertEqual(len(events), 5)
        self.assertEqual(events[0].sequence, 5)

    def test_replay_filtered_by_kind(self) -> None:
        self.store.append(session_id="s1", kind="ToolCallStarted", payload={}, source_process="t")
        self.store.append(session_id="s1", kind="ToolCallCompleted", payload={}, source_process="t")
        self.store.append(session_id="s1", kind="ToolCallStarted", payload={}, source_process="t")
        started = list(self.store.replay("s1", kind="ToolCallStarted"))
        self.assertEqual(len(started), 2)

    def test_listener_receives_events(self) -> None:
        received: list[TypedEvent] = []
        unsub = self.store.subscribe(received.append)
        self.store.append(session_id="s1", kind="ErrorRecorded", payload={"msg": "fail"}, source_process="t")
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].kind, "ErrorRecorded")
        unsub()
        self.store.append(session_id="s1", kind="ErrorRecorded", payload={}, source_process="t")
        self.assertEqual(len(received), 1)  # no new events after unsub

    def test_secret_never_stored(self) -> None:
        self.store.append(
            session_id="s1", kind="ToolCallStarted",
            payload={"api_key": "leaked-secret-12345"},
            source_process="t",
        )
        events = list(self.store.replay("s1"))
        self.assertNotIn("leaked-secret-12345", str(events[0].payload))
        self.assertEqual(events[0].payload["api_key"], "[REDACTED]")

    def test_retention_prunes_old_events(self) -> None:
        store = TranscriptStore(Path(self._tmp.name) / "retention.db", retention=100)
        for i in range(150):
            store.append(session_id="s1", kind="ToolCallProgress", payload={}, source_process="t")
        # Pruning is batched: between boundaries at most 2 * retention - 1
        # events remain. At a boundary exactly retention remain.
        self.assertEqual(store.count("s1"), 150)
        for _ in range(50):
            store.append(session_id="s1", kind="ToolCallProgress", payload={})
        self.assertEqual(store.count("s1"), 100)
        self.assertEqual([e.sequence for e in store.replay("s1")], list(range(100, 200)))
        store.close()

    def test_survives_close_and_reopen(self) -> None:
        self.store.append(session_id="s1", kind="AgentStepStarted", payload={}, event_id="persistent", source_process="t")
        self.store.close()
        reopened = TranscriptStore(Path(self._tmp.name) / "transcript.db")
        events = list(reopened.replay("s1"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, "persistent")
        reopened.close()

    def test_interleaved_sessions_each_prune(self) -> None:
        store = TranscriptStore(Path(self._tmp.name) / "interleaved.db", retention=100)
        self.addCleanup(store.close)
        for _ in range(350):
            # The global 100-write boundary always falls on s2, never s1.
            for session in ("s1", "s2"):
                store.append(session_id=session, kind="ToolCallProgress", payload={})
        for session in ("s1", "s2"):
            self.assertEqual(store.count(session), 150)
            self.assertEqual(store.latest_sequence(session), 349)
            self.assertEqual(next(store.replay(session)).sequence, 200)

    def test_retention_progress_survives_restarts(self) -> None:
        path = Path(self._tmp.name) / "restarts.db"
        for _ in range(4):
            store = TranscriptStore(path, retention=100)
            try:
                for _ in range(60):
                    store.append(session_id="s", kind="ToolCallProgress", payload={})
            finally:
                store.close()
        store = TranscriptStore(path, retention=100)
        self.addCleanup(store.close)
        self.assertEqual(store.count("s"), 140)
        self.assertEqual(store.latest_sequence("s"), 239)
        self.assertEqual(next(store.replay("s")).sequence, 100)

    def test_listener_self_unsubscribe_does_not_skip_next_listener(self) -> None:
        received = []

        def once(event: TypedEvent) -> None:
            received.append(("once", event.sequence))
            unsubscribe()

        unsubscribe = self.store.subscribe(once)
        self.store.subscribe(lambda event: received.append(("always", event.sequence)))
        for _ in range(2):
            self.store.append(session_id="s", kind="AgentStepStarted", payload={})
        self.assertEqual(received, [("once", 0), ("always", 0), ("always", 1)])

    def test_new_listener_starts_at_next_append(self) -> None:
        received = []
        registered = False

        def register(event: TypedEvent) -> None:
            nonlocal registered
            if not registered:
                registered = True
                self.store.subscribe(lambda item: received.append(item.sequence))

        self.store.subscribe(register)
        for _ in range(2):
            self.store.append(session_id="s", kind="AgentStepStarted", payload={})
        self.assertEqual(received, [1])

    def test_live_replay_does_not_fail_checkpoint_append(self) -> None:
        for _ in range(199):
            self.store.append(session_id="s", kind="ToolCallProgress", payload={})
        replay = self.store.replay("s")
        self.assertEqual(next(replay).sequence, 0)
        received = []
        self.store.subscribe(received.append)
        try:
            event = self.store.append(
                session_id="s", kind="ToolCallProgress", payload={}, event_id="at-checkpoint",
            )
            self.assertEqual(event.sequence, 199)
            self.assertEqual(received, [event])
            retry = self.store.append(
                session_id="s", kind="ToolCallProgress", payload={}, event_id="at-checkpoint",
            )
            self.assertEqual(retry, event)
            self.assertEqual(received, [event])
        finally:
            replay.close()
        self.assertEqual(self.store.count("s"), 200)

    def test_failed_prune_rolls_back_append(self) -> None:
        store = TranscriptStore(Path(self._tmp.name) / "rollback.db", retention=100)
        self.addCleanup(store.close)
        for _ in range(99):
            store.append(session_id="s", kind="ToolCallProgress", payload={})
        received = []
        store.subscribe(received.append)
        with patch.object(store, "_prune_session", side_effect=sqlite3.OperationalError("test")):
            with self.assertRaises(sqlite3.OperationalError):
                store.append(session_id="s", kind="ToolCallProgress", payload={}, event_id="retry")
        self.assertEqual(store.count("s"), 99)
        self.assertEqual(store.latest_sequence("s"), 98)
        self.assertEqual(received, [])
        event = store.append(session_id="s", kind="ToolCallProgress", payload={}, event_id="retry")
        self.assertEqual(event.sequence, 99)
        self.assertEqual(received, [event])

    def _concurrent_appends(self, same_id: bool) -> list[TypedEvent]:
        other = TranscriptStore(Path(self._tmp.name) / "transcript.db")
        self.addCleanup(other.close)
        barrier = threading.Barrier(2)

        def run(store: TranscriptStore, event_id: str) -> TypedEvent:
            next_sequence = store._next_sequence

            def synchronized_next(session_id: str) -> int:
                sequence = next_sequence(session_id)
                # Align unprotected reads to deterministically expose the old
                # race. A write transaction must not wait for the other writer.
                if not store._conn.in_transaction:
                    barrier.wait(timeout=5)
                return sequence

            with patch.object(store, "_next_sequence", synchronized_next):
                return store.append(
                    session_id="s", kind="ToolCallProgress", payload={}, event_id=event_id,
                )

        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(run, self.store, "one")
            two = pool.submit(run, other, "one" if same_id else "two")
            return [one.result(timeout=10), two.result(timeout=10)]

    def test_two_stores_allocate_distinct_sequences(self) -> None:
        events = self._concurrent_appends(same_id=False)
        self.assertEqual(sorted(event.sequence for event in events), [0, 1])
        self.assertEqual(self.store.count("s"), 2)

    def test_two_stores_retry_same_id(self) -> None:
        events = self._concurrent_appends(same_id=True)
        self.assertEqual(events[0], events[1])
        self.assertEqual(self.store.count("s"), 1)

    def test_sparse_kind_replay_has_bounded_query_work_after_index_upgrade(self) -> None:
        # Simulate an existing v1 database carrying the old two-column index.
        self.store._conn.execute("DROP INDEX IF EXISTS idx_session_kind_seq")
        self.store._conn.execute("CREATE INDEX IF NOT EXISTS idx_session_kind ON events(session_id, kind)")
        self.store.close()
        self.store = TranscriptStore(Path(self._tmp.name) / "transcript.db")
        for _ in range(1000):
            self.store.append(session_id="s", kind="ToolCallProgress", payload={})
        expected = self.store.append(session_id="s", kind="ErrorRecorded", payload={})
        self.assertEqual(list(self.store.replay("s", kind="ErrorRecorded")), [expected])
        self.assertEqual(list(self.store.replay("s", kind="ErrorRecorded", from_sequence=1001)), [])
        steps = 0

        def progress() -> int:
            nonlocal steps
            steps += 1
            return 0

        self.store._conn.set_progress_handler(progress, 1)
        try:
            self.assertEqual(list(self.store.replay("s", kind="AgentWarning")), [])
        finally:
            self.store._conn.set_progress_handler(None, 0)
        # Generous deterministic work bound, not a wall-clock performance gate.
        self.assertLess(steps, 200, f"empty filtered replay used {steps} SQLite VM steps")

    def test_all_required_event_types_exist(self) -> None:
        expected = {
            "TranscriptMessageAdded", "TranscriptMessageUpdated",
            "ToolCallStarted", "ToolCallProgress", "ToolCallCompleted",
            "ToolCallFailed", "AgentStepStarted", "AgentStepCompleted",
            "ReasoningSummaryDelta",
            "AgentPhaseChanged", "AgentWarning", "AgentErrorEvent",
            "FileRead", "FileEdited", "TestRunStarted", "TestRunCompleted",
            "ArtifactCreated",
            "VerificationStarted", "VerificationCompleted",
            "ConfirmationRequested", "ConfirmationResolved",
            "BrowserAction", "WorkspaceMutation", "SessionStateChanged",
            "UsageMeasured", "CostMeasured", "EvidenceRecorded", "ErrorRecorded",
        }
        self.assertEqual(EVENT_TYPES, expected)


if __name__ == "__main__":
    unittest.main()
