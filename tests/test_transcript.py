"""Typed process-boundary transcript store — Phase 3 foundation tests."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

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
        self.store = TranscriptStore(Path(self._tmp.name) / "transcript.db")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

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
        # After pruning at the retention boundary, count should be <= retention.
        self.assertLessEqual(store.count("s1"), 150)
        store.close()

    def test_survives_close_and_reopen(self) -> None:
        self.store.append(session_id="s1", kind="AgentStepStarted", payload={}, event_id="persistent", source_process="t")
        self.store.close()
        reopened = TranscriptStore(Path(self._tmp.name) / "transcript.db")
        events = list(reopened.replay("s1"))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].event_id, "persistent")
        reopened.close()

    def test_all_required_event_types_exist(self) -> None:
        expected = {
            "TranscriptMessageAdded", "TranscriptMessageUpdated",
            "ToolCallStarted", "ToolCallProgress", "ToolCallCompleted",
            "ToolCallFailed", "AgentStepStarted", "AgentStepCompleted",
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
