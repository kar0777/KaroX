"""Grouped activity stream: typed events in, a legible chronology out.

Two layers of proof. The unit tests pin the grouping rules -- chronological
correctness, containment (no payload text ever rendered), honest counters,
warnings that survive every folding. The integration test drives the real
:class:`~karox.agent.AgentKernel` through a scripted provider, publishes its
events through the real transcript shadow into the real SQLite store, replays
that store into the grouped model, and reads the rendered lines -- the whole
product path, not a renderer unit test.
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from _support import SRC, initialize_git_repository  # noqa: F401

from karox.activity_stream import (
    GROUP_BROWSER,
    GROUP_IMPLEMENTING,
    GROUP_INVESTIGATING,
    GROUP_VERIFYING,
    GROUP_WORKING,
    STATUS_ATTENTION,
    STATUS_DONE,
    STATUS_RUNNING,
    ActivityStream,
    render_group_lines,
    render_stream_lines,
)
from karox.agent import AgentKernel, AgentLimits, SYSTEM_PROMPT
from karox.core import CoreRuntime
from karox.models import AccessProfile, Capability, Origin, OriginKind
from karox.policy import CapabilityPolicy
from karox.sessions import SessionStore
from karox.transcript import TranscriptStore
from karox.transcript_shadow import make_transcript_observer

from karox.providers import ModelRequest, ModelResponse, ToolCall


class QueueProvider:
    """A provider that answers from a script, so the kernel loop is real."""

    provider_name = "test_provider"

    def __init__(self, responses: list[ModelResponse]) -> None:
        self.responses = list(responses)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("provider received an unexpected request")
        return self.responses.pop(0)


def call(call_id: str, name: str, arguments: object) -> ToolCall:
    return ToolCall(call_id, name, json.dumps(arguments, ensure_ascii=False))


def model_response(
    *calls: ToolCall, content: str | None = None
) -> ModelResponse:
    return ModelResponse(
        content=content,
        tool_calls=tuple(calls),
        finish_reason="tool_calls" if calls else "stop",
        usage={"prompt_tokens": 2, "completion_tokens": 1},
        response_id=None,
    )


class GroupingRuleTests(unittest.TestCase):
    def test_reads_and_searches_fold_into_one_investigating_group(self) -> None:
        stream = ActivityStream()
        for index in range(3):
            stream.feed("ToolCallStarted", {"tool": "repo.search"})
            stream.feed("ToolCallCompleted", {"tool": "repo.search", "ok": True})
        stream.feed("ToolCallStarted", {"tool": "repo.read_file"})
        stream.feed("ToolCallCompleted", {"tool": "repo.read_file", "ok": True})
        stream.feed("FileRead", {"path": "src/a.py"})
        groups = stream.groups()
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group.kind, GROUP_INVESTIGATING)
        self.assertEqual(group.status, STATUS_RUNNING)
        self.assertEqual(group.searches, 3)
        self.assertEqual(group.files_read, 1)

    def test_a_family_change_closes_the_group_chronologically(self) -> None:
        stream = ActivityStream()
        stream.feed("ToolCallCompleted", {"tool": "repo.search", "ok": True})
        stream.feed("ToolCallCompleted", {"tool": "repo.edit_file", "ok": True})
        stream.feed("FileEdited", {"path": "src/a.py"})
        stream.feed("ToolCallCompleted", {"tool": "repo.read_file", "ok": True})
        kinds = [group.kind for group in stream.groups()]
        self.assertEqual(
            kinds, [GROUP_INVESTIGATING, GROUP_IMPLEMENTING, GROUP_INVESTIGATING]
        )

    def test_distinct_paths_are_counted_once(self) -> None:
        stream = ActivityStream()
        for _ in range(2):
            stream.feed("FileRead", {"path": "src/same.py"})
        stream.feed("FileRead", {"path": "src/other.py"})
        self.assertEqual(stream.groups()[0].files_read, 2)

    def test_phase_change_anchors_the_next_group(self) -> None:
        stream = ActivityStream()
        stream.feed("ToolCallCompleted", {"tool": "repo.search", "ok": True})
        stream.feed("AgentPhaseChanged", {"phase": "verification"})
        stream.feed("TestRunCompleted", {"ok": True})
        groups = stream.groups()
        self.assertEqual(groups[0].kind, GROUP_INVESTIGATING)
        self.assertEqual(groups[0].status, STATUS_DONE)
        self.assertEqual(groups[1].kind, GROUP_VERIFYING)
        self.assertEqual(groups[1].phase, "verification")
        self.assertEqual(groups[1].checks_passed, 1)

    def test_an_unknown_phase_is_working_not_the_identifier(self) -> None:
        stream = ActivityStream()
        stream.feed("AgentPhaseChanged", {"phase": "totally_new_phase"})
        stream.feed("ToolCallCompleted", {"tool": "browser.open", "ok": True})
        # The phase-anchored group absorbs the browser call; nothing rendered
        # may contain the raw phase identifier.
        for line in render_stream_lines(stream, english=True):
            self.assertNotIn("totally_new_phase", line)

    def test_browser_tools_group_as_browser(self) -> None:
        stream = ActivityStream()
        stream.feed("ToolCallCompleted", {"tool": "browser.open", "ok": True})
        stream.feed("ToolCallCompleted", {"tool": "browser.click", "ok": True})
        groups = stream.groups()
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].kind, GROUP_BROWSER)
        self.assertEqual(groups[0].actions, 2)

    def test_a_failed_tool_marks_attention_but_not_before_it_closes(self) -> None:
        stream = ActivityStream()
        stream.feed("ToolCallCompleted", {"tool": "repo.read_file", "ok": False})
        self.assertEqual(stream.groups()[0].status, STATUS_RUNNING)
        stream.finish()
        self.assertEqual(stream.groups()[0].status, STATUS_ATTENTION)
        self.assertEqual(stream.groups()[0].failures, 1)

    def test_warnings_and_errors_survive_every_folding(self) -> None:
        stream = ActivityStream()
        stream.feed("ToolCallCompleted", {"tool": "repo.edit_file", "ok": True})
        stream.feed("AgentWarning", {"reason": "repeated_refusal"})
        stream.feed("AgentErrorEvent", {"reason": "some_new_reason"})
        stream.finish()
        group = stream.groups()[0]
        self.assertEqual(group.status, STATUS_ATTENTION)
        self.assertEqual(group.warnings, ("repeated_refusal",))
        self.assertEqual(group.errors, ("some_new_reason",))
        lines = render_group_lines(group, english=True)
        self.assertTrue(any("refused repeatedly" in line for line in lines))
        # The unknown reason renders as the generic word, never as itself.
        self.assertTrue(any("run error" in line for line in lines))
        self.assertFalse(any("some_new_reason" in line for line in lines))

    def test_finished_state_closes_the_stream(self) -> None:
        stream = ActivityStream()
        stream.feed("ToolCallCompleted", {"tool": "checks.run", "ok": True})
        stream.feed("TestRunCompleted", {"ok": True})
        stream.feed("SessionStateChanged", {"status": "verified"})
        self.assertEqual(stream.finished_status, "verified")
        self.assertEqual(stream.groups()[-1].status, STATUS_DONE)

    def test_pop_closed_hands_each_group_to_the_renderer_exactly_once(self) -> None:
        stream = ActivityStream()
        stream.feed("ToolCallCompleted", {"tool": "repo.search", "ok": True})
        self.assertEqual(stream.pop_closed(), ())
        stream.feed("ToolCallCompleted", {"tool": "repo.write_file", "ok": True})
        first = stream.pop_closed()
        self.assertEqual(len(first), 1)
        self.assertEqual(first[0].kind, GROUP_INVESTIGATING)
        self.assertEqual(stream.pop_closed(), ())
        stream.finish()
        second = stream.pop_closed()
        self.assertEqual(len(second), 1)
        self.assertEqual(second[0].kind, GROUP_IMPLEMENTING)

    def test_empty_groups_are_never_published(self) -> None:
        stream = ActivityStream()
        stream.feed("AgentPhaseChanged", {"phase": "execution"})
        stream.feed("AgentPhaseChanged", {"phase": "verification"})
        stream.finish()
        self.assertEqual(stream.groups(), ())
        self.assertEqual(stream.pop_closed(), ())

    def test_unknown_event_kinds_contribute_nothing(self) -> None:
        stream = ActivityStream()
        stream.feed("SomethingNew", {"tool": "repo.search"})
        stream.feed(None, {})
        self.assertEqual(stream.groups(), ())


class ContainmentTests(unittest.TestCase):
    def test_no_payload_text_ever_reaches_a_rendered_line(self) -> None:
        hostile = "[red]$(rm -rf /)[/red] SECRET_TOKEN aHR0cHM6"
        stream = ActivityStream()
        stream.feed("ToolCallStarted", {"tool": hostile})
        stream.feed("ToolCallCompleted", {"tool": hostile, "ok": False})
        stream.feed("FileRead", {"path": hostile})
        stream.feed("FileEdited", {"path": hostile})
        stream.feed("AgentWarning", {"reason": hostile})
        stream.feed("AgentErrorEvent", {"reason": hostile})
        stream.feed("ArtifactCreated", {"path": hostile, "detail": hostile})
        stream.finish()
        for english in (True, False):
            for line in render_stream_lines(stream, english):
                for fragment in ("SECRET_TOKEN", "rm -rf", "[red]", "aHR0cHM6"):
                    self.assertNotIn(fragment, line)

    def test_no_percentage_progress_is_ever_rendered(self) -> None:
        stream = ActivityStream()
        for index in range(25):
            stream.feed("ToolCallCompleted", {"tool": "repo.read_file", "ok": True})
            stream.feed("FileRead", {"path": f"src/file{index}.py"})
        stream.finish()
        for line in render_stream_lines(stream, english=True):
            self.assertNotIn("%", line)

    def test_russian_rendering_uses_the_same_catalogs(self) -> None:
        stream = ActivityStream()
        stream.feed("ToolCallCompleted", {"tool": "repo.write_file", "ok": True})
        stream.feed("FileEdited", {"path": "src/a.py"})
        stream.feed("TestRunCompleted", {"ok": True})
        stream.finish()
        lines = render_stream_lines(stream, english=False)
        self.assertTrue(lines)
        joined = "\n".join(lines)
        self.assertNotIn("repo.write_file", joined)
        self.assertIn("●", joined)


class KernelToRendererIntegrationTests(unittest.TestCase):
    """Real kernel events → transcript store → grouped renderer."""

    def test_the_whole_pipeline_renders_a_grouped_run(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            repository = root / "repo"
            initialize_git_repository(repository)
            (repository / "sample.txt").write_text("before\n", encoding="utf-8")
            sessions = SessionStore(root / "sessions")
            sessions.create(
                repository,
                "change sample and verify it",
                AccessProfile.WORKSPACE_WRITE,
                session_id="session",
            )
            origin = Origin(OriginKind.NATIVE_AGENT, "test-agent")
            policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
            policy.set_grants(
                origin,
                {
                    Capability.REPO_READ,
                    Capability.REPO_WRITE,
                    Capability.PROCESS_RUN,
                    Capability.CHECKS_RUN,
                    Capability.GIT_READ,
                },
            )
            core = CoreRuntime(
                repository,
                policy,
                sessions,
                root / "audit.jsonl",
                verification_commands=[[sys.executable, "-c", "print('ok')"]],
            )
            store = TranscriptStore(root / "transcript.db")
            try:
                provider = QueueProvider(
                    [
                        model_response(
                            call(
                                "read",
                                "repo_read_file",
                                {"path": "sample.txt"},
                            )
                        ),
                        model_response(
                            call(
                                "write",
                                "repo_write_file",
                                {"path": "sample.txt", "content": "after\n"},
                            )
                        ),
                        model_response(
                            call(
                                "check",
                                "checks_run",
                                {"argv": [sys.executable, "-c", "print('ok')"]},
                            )
                        ),
                        model_response(
                            call("status", "git_status", {}),
                            call("diff", "git_diff", {}),
                        ),
                        model_response(content="verified locally"),
                    ]
                )
                observer = make_transcript_observer(
                    "session", store=store, source_process="test"
                )
                report = AgentKernel(
                    provider=provider,
                    model="test-model",
                    core=core,
                    sessions=sessions,
                    origin=origin,
                    limits=AgentLimits(max_seconds=30),
                    system_prompt=SYSTEM_PROMPT,
                    on_event=observer,
                ).run("session")
                self.assertTrue(report.verified)

                # The product path: replay the store, feed the grouped model.
                stream = ActivityStream()
                for event in store.replay("session"):
                    stream.feed(event.kind, event.payload)
                stream.finish()

                groups = stream.groups()
                kinds = [group.kind for group in groups]
                # Chronology: the read, then the edit, then verification.
                self.assertIn(GROUP_INVESTIGATING, kinds)
                self.assertIn(GROUP_IMPLEMENTING, kinds)
                self.assertIn(GROUP_VERIFYING, kinds)
                self.assertLess(
                    kinds.index(GROUP_INVESTIGATING),
                    kinds.index(GROUP_IMPLEMENTING),
                )
                self.assertLess(
                    kinds.index(GROUP_IMPLEMENTING),
                    kinds.index(GROUP_VERIFYING),
                )
                investigating = groups[kinds.index(GROUP_INVESTIGATING)]
                self.assertEqual(investigating.files_read, 1)
                implementing = groups[kinds.index(GROUP_IMPLEMENTING)]
                self.assertEqual(implementing.files_changed, 1)
                verifying = groups[kinds.index(GROUP_VERIFYING)]
                self.assertGreaterEqual(verifying.checks_passed, 1)

                lines = render_stream_lines(stream, english=True)
                joined = "\n".join(lines)
                self.assertIn("● Exploring project", joined)
                self.assertIn("● Implementing", joined)
                self.assertIn("● Verifying result", joined)
                self.assertIn("1 file changed", joined)
                # Raw tool identifiers and paths stay off this surface.
                self.assertNotIn("repo_write_file", joined)
                self.assertNotIn("repo.write_file", joined)
                self.assertNotIn("sample.txt", joined)
                self.assertNotIn("%", joined)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
