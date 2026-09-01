"""Normalized agent event protocol (one provider-neutral typed pipeline).

The kernel emits typed facts (phase, warning, error, file read/edited, test
started/finished, artifact created), the transcript shadow maps them onto
registered typed store events, and renderers derive their own view. Providers
and adapters never construct UI presentation, and nothing here invents a fact
that the tool result did not carry.
"""

from __future__ import annotations

import io
import unittest
from contextlib import redirect_stderr
from typing import Any

from _support import SRC  # noqa: F401

from karox.agent import AgentEvent, AgentEventKind, _derived_tool_events
from karox.transcript import EVENT_TYPES
from karox.transcript_shadow import _AGENT_KIND_MAP, make_transcript_observer

NORMALIZED_KINDS = (
    AgentEventKind.PHASE_CHANGED,
    AgentEventKind.WARNING,
    AgentEventKind.ERROR,
    AgentEventKind.FILE_READ,
    AgentEventKind.FILE_EDITED,
    AgentEventKind.TEST_STARTED,
    AgentEventKind.TEST_FINISHED,
    AgentEventKind.ARTIFACT_CREATED,
)


class _FakeResult:
    def __init__(self, ok: bool, data: dict[str, Any]) -> None:
        self.ok = ok
        self.data = data


class _FakeStore:
    def __init__(self) -> None:
        self.appended: list[dict[str, Any]] = []

    def append(self, **kwargs: Any) -> None:
        self.appended.append(kwargs)


class EventVocabularyTests(unittest.TestCase):
    def test_normalized_kinds_exist(self) -> None:
        for kind in NORMALIZED_KINDS:
            self.assertIsInstance(kind, AgentEventKind)

    def test_event_carries_normalized_fields(self) -> None:
        event = AgentEvent(
            AgentEventKind.FILE_EDITED, 3, path="src/x.py", detail="d"
        )
        self.assertEqual(event.path, "src/x.py")
        self.assertEqual(event.detail, "d")
        self.assertIsNone(event.phase)

    def test_every_normalized_kind_maps_to_a_registered_typed_event(self) -> None:
        for kind in NORMALIZED_KINDS:
            typed = _AGENT_KIND_MAP.get(kind)
            self.assertIsNotNone(typed, kind)
            self.assertIn(typed, EVENT_TYPES)


class DerivedToolEventTests(unittest.TestCase):
    def test_successful_read_derives_file_read(self) -> None:
        events = _derived_tool_events(
            "repo.read_file", _FakeResult(True, {"path": "src/a.py"})
        )
        self.assertEqual(
            events, [(AgentEventKind.FILE_READ, {"path": "src/a.py"})]
        )

    def test_edit_requires_a_real_change(self) -> None:
        unchanged = _derived_tool_events(
            "repo.edit_file", _FakeResult(True, {"path": "a", "changed": False})
        )
        self.assertEqual(unchanged, [])
        changed = _derived_tool_events(
            "repo.write_file", _FakeResult(True, {"path": "a", "changed": True})
        )
        self.assertEqual(changed[0][0], AgentEventKind.FILE_EDITED)

    def test_failed_read_derives_nothing(self) -> None:
        self.assertEqual(
            _derived_tool_events(
                "repo.read_file", _FakeResult(False, {"path": "a"})
            ),
            [],
        )

    def test_check_family_derives_test_finished_with_exit_code(self) -> None:
        events = _derived_tool_events(
            "checks.run", _FakeResult(True, {"exit_code": 0})
        )
        self.assertEqual(
            events,
            [(AgentEventKind.TEST_FINISHED, {"ok": True, "detail": "exit 0"})],
        )

    def test_missing_fields_invent_nothing(self) -> None:
        self.assertEqual(
            _derived_tool_events("repo.read_file", _FakeResult(True, {})), []
        )
        events = _derived_tool_events("tests.run", _FakeResult(False, {}))
        self.assertEqual(
            events, [(AgentEventKind.TEST_FINISHED, {"ok": False, "detail": None})]
        )

    def test_unrelated_tools_derive_nothing(self) -> None:
        self.assertEqual(
            _derived_tool_events(
                "repo.search", _FakeResult(True, {"path": "x"})
            ),
            [],
        )


class TranscriptShadowTests(unittest.TestCase):
    def test_new_kinds_reach_the_store_with_normalized_payload(self) -> None:
        store = _FakeStore()
        observe = make_transcript_observer(
            "s1", store=store, source_process="test"
        )
        observe(AgentEvent(AgentEventKind.PHASE_CHANGED, 2, phase="verification"))
        observe(
            AgentEvent(
                AgentEventKind.FILE_EDITED,
                2,
                tool="repo.edit_file",
                call_id="c1",
                path="src/a.py",
            )
        )
        observe(
            AgentEvent(
                AgentEventKind.ERROR,
                2,
                reason="unknown_tool",
                detail="tool is not available",
            )
        )
        kinds = [item["kind"] for item in store.appended]
        self.assertEqual(kinds, ["AgentPhaseChanged", "FileEdited", "AgentErrorEvent"])
        self.assertEqual(store.appended[0]["payload"]["phase"], "verification")
        self.assertEqual(store.appended[1]["payload"]["path"], "src/a.py")
        self.assertEqual(store.appended[2]["payload"]["reason"], "unknown_tool")
        self.assertEqual(
            store.appended[2]["payload"]["detail"], "tool is not available"
        )

    def test_private_streaming_deltas_are_skipped_but_public_summary_is_kept(self) -> None:
        store = _FakeStore()
        observe = make_transcript_observer("s1", store=store)
        observe(AgentEvent(AgentEventKind.TEXT_DELTA, 1, text_delta="x"))
        observe(AgentEvent(AgentEventKind.REASONING_DELTA, 1, reasoning_delta="private"))
        observe(
            AgentEvent(
                AgentEventKind.REASONING_SUMMARY_DELTA,
                1,
                summary="Checking the relevant code.",
            )
        )
        self.assertEqual(len(store.appended), 1)
        self.assertEqual(store.appended[0]["kind"], "ReasoningSummaryDelta")
        self.assertEqual(
            store.appended[0]["payload"]["summary"],
            "Checking the relevant code.",
        )
        self.assertNotIn("private", str(store.appended))


class RendererTests(unittest.TestCase):
    def _render(self, *events: AgentEvent) -> str:
        from karox import cli

        stream = io.StringIO()
        with redirect_stderr(stream):
            observe = cli._stream_progress()
            for event in events:
                observe(event)
        return stream.getvalue()

    def test_normalized_events_render_as_lines_not_percentages(self) -> None:
        output = self._render(
            AgentEvent(AgentEventKind.PHASE_CHANGED, 1, phase="execution"),
            AgentEvent(
                AgentEventKind.WARNING,
                1,
                reason="repeated_action",
                detail="identical call with unchanged results",
            ),
            AgentEvent(AgentEventKind.FILE_READ, 1, path="src/a.py"),
            AgentEvent(AgentEventKind.FILE_EDITED, 1, path="src/b.py"),
            AgentEvent(
                AgentEventKind.TEST_FINISHED,
                1,
                tool="checks.run",
                ok=True,
                detail="exit 0",
            ),
            AgentEvent(
                AgentEventKind.ERROR, 1, reason="unknown_tool", detail="nope"
            ),
        )
        self.assertIn("[phase: execution]", output)
        self.assertIn("!! repeated_action", output)
        self.assertIn("file read src/a.py", output)
        self.assertIn("file edited src/b.py", output)
        self.assertIn("test checks.run ok (exit 0)", output)
        self.assertIn("xx unknown_tool: nope", output)
        self.assertNotIn("%", output)

    def test_artifact_line_renders_kind_and_path(self) -> None:
        output = self._render(
            AgentEvent(
                AgentEventKind.ARTIFACT_CREATED,
                4,
                path="artifacts/plan.md",
                detail="plan",
            )
        )
        self.assertIn("artifact plan artifacts/plan.md", output)


if __name__ == "__main__":
    unittest.main()
