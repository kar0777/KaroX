"""Phase 3 shadow-mode parity tests.

Proves that the typed transcript stream produces the same tool-call and
step projections as the polled ``provider_history`` path. When every test
here passes, the typed stream is a faithful replacement and
``_poll_agent_history`` can be removed.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from karox.agent import AgentEvent, AgentEventKind
from karox.transcript import TranscriptStore
from karox.transcript_shadow import (
    ParityChecker,
    TranscriptReader,
    make_transcript_observer,
)


def _fake_history() -> list[dict]:
    """Simulated provider_history matching the agent events below."""
    return [
        {"role": "user", "content": "read the file"},
        {
            "role": "assistant",
            "content": "Reading README.md",
            "tool_calls": [
                {"call_id": "call-1", "name": "repo.read_file"},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "core_name": "repo.read_file",
            "result": {"ok": True, "data": {"content": "hello"}},
        },
        {
            "role": "assistant",
            "content": "Writing fixture",
            "tool_calls": [
                {"call_id": "call-2", "name": "repo.write_file"},
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-2",
            "core_name": "repo.write_file",
            "result": {"ok": True, "data": {"bytes": 42}},
        },
    ]


def _emit_agent_events(observer, *, session_id: str = "s1") -> None:
    """Emit the same sequence the above history represents."""
    # Step 0
    observer(AgentEvent(AgentEventKind.STEP_STARTED, 0))
    observer(AgentEvent(
        AgentEventKind.TOOL_STARTED, 0, tool="repo.read_file", call_id="call-1"
    ))
    observer(AgentEvent(
        AgentEventKind.TOOL_FINISHED, 0, tool="repo.read_file", call_id="call-1",
        ok=True, summary="86 chars", duration_seconds=0.12,
    ))
    observer(AgentEvent(
        AgentEventKind.STEP_FINISHED, 0, usage={"input_tokens": 500, "output_tokens": 50}
    ))
    # Step 1
    observer(AgentEvent(AgentEventKind.STEP_STARTED, 1))
    observer(AgentEvent(
        AgentEventKind.TOOL_STARTED, 1, tool="repo.write_file", call_id="call-2"
    ))
    observer(AgentEvent(
        AgentEventKind.TOOL_FINISHED, 1, tool="repo.write_file", call_id="call-2",
        ok=True, summary="42 bytes", duration_seconds=0.08,
    ))
    observer(AgentEvent(
        AgentEventKind.STEP_FINISHED, 1, usage={"input_tokens": 600, "output_tokens": 30}
    ))
    observer(AgentEvent(
        AgentEventKind.FINISHED, 1, status="verified", reason="verified"
    ))


class TranscriptObserverTests(unittest.TestCase):
    """The observer publishes typed events that match agent events."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TranscriptStore(Path(self._tmp.name) / "t.db")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_observer_publishes_tool_calls(self) -> None:
        forward_calls: list[AgentEvent] = []
        observer = make_transcript_observer(
            "s1", store=self.store,
            next_observer=forward_calls.append,
        )
        _emit_agent_events(observer)
        reader = TranscriptReader(self.store, "s1")
        calls = reader.tool_calls()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0].tool, "repo.read_file")
        self.assertEqual(calls[1].tool, "repo.write_file")
        self.assertTrue(calls[0].ok)
        # The forwarding observer must still receive every event.
        self.assertGreater(len(forward_calls), 6)

    def test_observer_publishes_steps(self) -> None:
        observer = make_transcript_observer("s1", store=self.store)
        _emit_agent_events(observer)
        reader = TranscriptReader(self.store, "s1")
        steps = reader.steps()
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[0].step, 0)
        self.assertEqual(steps[1].step, 1)

    def test_final_status_recorded(self) -> None:
        observer = make_transcript_observer("s1", store=self.store)
        _emit_agent_events(observer)
        reader = TranscriptReader(self.store, "s1")
        self.assertEqual(reader.final_status(), "verified")

    def test_text_delta_not_flooded(self) -> None:
        observer = make_transcript_observer("s1", store=self.store)
        for i in range(100):
            observer(AgentEvent(AgentEventKind.TEXT_DELTA, 0, text_delta=f"chunk{i}"))
        # TEXT_DELTA must not create events (they are streaming fragments).
        self.assertEqual(self.store.count("s1"), 0)


class ParityCheckerTests(unittest.TestCase):
    """The typed stream matches the polled provider_history."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TranscriptStore(Path(self._tmp.name) / "t.db")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_full_parity(self) -> None:
        observer = make_transcript_observer("s1", store=self.store)
        _emit_agent_events(observer)
        checker = ParityChecker(self.store)
        report = checker.compare("s1", _fake_history())
        self.assertTrue(
            report.match,
            f"discrepancies: {report.discrepancies}",
        )
        self.assertEqual(report.tool_call_count_typed, 2)
        self.assertEqual(report.tool_call_count_polled, 2)
        self.assertEqual(report.step_count_typed, 2)
        self.assertEqual(report.step_count_polled, 2)

    def test_mismatch_detected(self) -> None:
        observer = make_transcript_observer("s1", store=self.store)
        _emit_agent_events(observer)
        # Simulate a polled history with only one tool call (missing one).
        partial_history = _fake_history()[:3]  # only call-1
        checker = ParityChecker(self.store)
        report = checker.compare("s1", partial_history)
        self.assertFalse(report.match)
        self.assertTrue(any("count" in d for d in report.discrepancies))

    def test_empty_session_parity(self) -> None:
        checker = ParityChecker(self.store)
        report = checker.compare("empty", [])
        self.assertTrue(report.match)


class IdempotencyTests(unittest.TestCase):
    """Replaying the same events does not create duplicates."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TranscriptStore(Path(self._tmp.name) / "t.db")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_duplicate_event_ids_are_idempotent(self) -> None:
        self.store.append(
            session_id="s1", kind="ToolCallStarted",
            payload={"tool": "read"}, event_id="fixed-id",
        )
        self.store.append(
            session_id="s1", kind="ToolCallStarted",
            payload={"tool": "read"}, event_id="fixed-id",
        )
        self.assertEqual(self.store.count("s1"), 1)


class SecretSafetyTests(unittest.TestCase):
    """No secret leaks through the typed stream."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.store = TranscriptStore(Path(self._tmp.name) / "t.db")

    def tearDown(self) -> None:
        self.store.close()
        self._tmp.cleanup()

    def test_secret_in_payload_redacted(self) -> None:
        self.store.append(
            session_id="s1", kind="ToolCallStarted",
            payload={"tool": "read", "api_key": "super-secret-xyz"},
        )
        events = list(self.store.replay("s1"))
        self.assertNotIn("super-secret-xyz", str(events[0].payload))
        self.assertEqual(events[0].payload["api_key"], "[REDACTED]")


class CrossProcessTests(unittest.TestCase):
    """The store survives close and reopen (simulating a process restart)."""

    def test_replay_after_reopen(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "t.db"
            store1 = TranscriptStore(path)
            observer = make_transcript_observer("s1", store=store1)
            _emit_agent_events(observer)
            store1.close()
            # Reopen as a different "process" would.
            store2 = TranscriptStore(path)
            reader = TranscriptReader(store2, "s1")
            self.assertEqual(len(reader.tool_calls()), 2)
            self.assertEqual(len(reader.steps()), 2)
            store2.close()


if __name__ == "__main__":
    unittest.main()
