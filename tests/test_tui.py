"""Contracts for the compact terminal client.

The screen is exercised through Textual's own pilot, and the run is a recorded
stream: no model, no network, no keyring. That is the point of the subprocess
boundary -- the UI can be tested at its real shape without a real agent.
"""

from __future__ import annotations

import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from karox import cli
from karox.agent import AgentEvent, AgentEventKind
from karox.tui import app as app_module
from karox.tui.app import KaroxApp
from karox.tui.backend import CLIBackend, StreamResult
from karox.tui.i18n import _EN, _RU
from karox.tui.protocol import (
    PROTOCOL_VERSION,
    Event,
    Outcome,
    SessionState,
    decode_line,
    tool_category,
)
from karox.tui.widgets import (
    CATEGORY_ICONS,
    AssistantMessage,
    Notice,
    ToolCard,
    UserMessage,
    header_segments,
    tool_card_text,
    welcome_text,
)


class _FakeBackend:
    def __init__(
        self,
        repository: Path,
        *,
        outcome: Outcome | None = None,
        script: list[Event] | None = None,
        record: dict[str, object] | None = None,
        record_after_run: dict[str, object] | None = None,
        skills: list[dict[str, object]] | None = None,
        fail_stream: bool = False,
    ) -> None:
        self.repository = repository
        self.stops = 0
        self.selected: list[tuple[str, str]] = []
        self._outcome = outcome
        self._script = script
        self._fail_stream = fail_stream
        self.record = record
        self._record_after_run = record_after_run
        self.skills = skills or []
        self.skill_details: dict[str, dict[str, object]] = {}
        self.sessions: list[dict[str, object]] = []

    def session(self, session_id: str) -> dict[str, object] | None:
        if self.record is not None and self.record.get("session_id") == session_id:
            return self.record
        return self.record if self.record is not None and session_id else None

    def sessions(self) -> list[dict[str, object]]:
        return self.sessions

    def selected_model(self) -> dict[str, object]:
        return {
            "provider_id": "demo",
            "model_id": "demo-1",
            "selected": True,
            "context_window": 128000,
        }

    def models(self) -> list[dict[str, object]]:
        return [
            {
                "provider_id": "demo",
                "model_id": "demo-1",
                "selected": True,
                "context_window": 128000,
            },
            {
                "provider_id": "demo",
                "model_id": "demo-2",
                "selected": False,
                "context_window": 64000,
            },
        ]

    def select_model(self, provider: str, model: str) -> dict[str, object]:
        self.selected.append((provider, model))
        return {"status": "selected"}

    def provider_presets(self) -> list[dict[str, object]]:
        return [
            {
                "preset_id": "openai",
                "display_name": "OpenAI",
                "installable": True,
                "base_url": "https://api.openai.com/v1",
            }
        ]

    def skill_list(self) -> list[dict[str, object]]:
        return self.skills

    def skill_show(self, name: str) -> dict[str, object] | None:
        return self.skill_details.get(name)

    def doctor(self) -> dict[str, object]:
        return {"status": "ok", "checks": {}}

    def bridge_profiles(self) -> list[dict[str, object]]:
        return []

    def mcp_status(self, session_id: str | None = None) -> dict[str, object]:
        return {"servers": []}

    def mcp_remove(self, server_id: str) -> dict[str, object]:
        return {"status": "removed"}

    def mcp_probe(self, server_id: str) -> dict[str, object]:
        return {"status": "ok", "tool_count": 0}

    def mcp_select_all(self, server_id: str, session_id: str) -> dict[str, object]:
        return {"selection": {"tools": {}}}

    def mcp_deselect(self, server_id: str, session_id: str) -> dict[str, object]:
        return {"status": "deselected"}

    def stop(self) -> None:
        self.stops += 1

    def stream_agent(self, request, on_event):
        if self._fail_stream:
            return StreamResult(outcome=None, exit_code=1, stderr="provider exploded")
        if self._record_after_run is not None:
            # The kernel saves the durable record as the run proceeds; the TUI
            # must only see a checkpoint once one actually exists.
            self.record = self._record_after_run
        script = self._script
        if script is None:
            script = [
                Event(kind="step_started", step=1),
                Event(kind="text_delta", step=1, text="Hello"),
                Event(
                    kind="tool_started",
                    step=1,
                    tool="repo.read_file",
                    call_id="call-1",
                ),
                Event(
                    kind="tool_finished",
                    step=1,
                    tool="repo.read_file",
                    call_id="call-1",
                    ok=True,
                    summary="read 12 lines",
                    duration_seconds=0.12,
                ),
                Event(kind="text_delta", step=1, text=" world"),
            ]
        for event in script:
            on_event(event)
        outcome = self._outcome or Outcome(
            session_id=request.session_id,
            status="verified",
            phase="complete",
            verified=True,
            reason="change",
            steps=1,
            changed_files=("app.py",),
            usage={"total_tokens": 1234},
            provider_message="Hello world",
        )
        return StreamResult(outcome=outcome, exit_code=0)


def _rich_script() -> list[Event]:
    """One recorded run touching every state family the UI exposes."""

    return [
        Event(kind="step_started", step=1),
        Event(kind="phase_changed", step=1, phase="building"),
        Event(kind="text_delta", step=1, text="working on it"),
        Event(kind="tool_started", step=1, tool="repo.read_file", call_id="c-read"),
        Event(
            kind="tool_finished",
            step=1,
            tool="repo.read_file",
            call_id="c-read",
            ok=True,
            summary="read 40 lines",
            duration_seconds=0.05,
        ),
        Event(kind="file_read", step=1, path="src/app.py"),
        Event(kind="tool_started", step=1, tool="repo.inspect", call_id="c-inspect"),
        Event(
            kind="tool_finished",
            step=1,
            tool="repo.inspect",
            call_id="c-inspect",
            ok=True,
            summary="focused map",
            duration_seconds=0.4,
        ),
        Event(kind="tool_started", step=1, tool="mcp.github.list_issues", call_id="c-mcp"),
        Event(
            kind="tool_finished",
            step=1,
            tool="mcp.github.list_issues",
            call_id="c-mcp",
            ok=True,
            summary="3 issues",
            duration_seconds=0.9,
        ),
        Event(kind="tool_started", step=1, tool="skill.load", call_id="c-skill"),
        Event(
            kind="tool_finished",
            step=1,
            tool="skill.load",
            call_id="c-skill",
            ok=True,
            summary="skill loaded",
            duration_seconds=0.1,
        ),
        Event(kind="tool_started", step=1, tool="repo.edit_file", call_id="c-edit"),
        Event(
            kind="tool_finished",
            step=1,
            tool="repo.edit_file",
            call_id="c-edit",
            ok=True,
            summary="path=src/app.py",
            duration_seconds=0.2,
        ),
        Event(kind="file_edited", step=1, path="src/app.py"),
        Event(kind="test_started", step=1, tool="checks.run", call_id="c-test"),
        Event(
            kind="test_finished",
            step=1,
            tool="checks.run",
            call_id="c-test",
            ok=True,
            detail="exit 0",
        ),
        Event(
            kind="step_finished",
            step=1,
            usage={"prompt_tokens": 102400, "total_tokens": 1500},
        ),
        Event(kind="step_started", step=2),
        Event(kind="compacted", step=2, summary="middle replaced"),
        Event(kind="text_delta", step=2, text="done"),
        Event(
            kind="step_finished",
            step=2,
            usage={"prompt_tokens": 90000, "total_tokens": 800},
        ),
        Event(kind="finished", step=2, status="verified", reason="change"),
    ]


def _session_record() -> dict[str, object]:
    return {
        "session_id": "tui-test-1",
        "name": "demo session",
        "repository": "",
        "branch": "main",
        "task": "make it work",
        "task_history": ["first ask", "make it work"],
        "status": "active",
        "phase": "verifying",
        "revision": 7,
        "updated_at": 1761382800.0,
        "plan_items": [
            {"text": "read the repo", "status": "done"},
            {"text": "edit the file", "status": "doing"},
            {"text": "run checks", "status": "todo"},
        ],
        "checkpoints": [
            {
                "checkpoint_id": "cp-1",
                "created_at": 1761382000.0,
                "tracked_count": 4,
                "untracked_count": 1,
            }
        ],
        "checks": [
            {"ok": True, "argv": ["python", "-m", "pytest", "-q"], "exit_code": 0},
            {"ok": False, "argv": ["ruff", "check"], "exit_code": 1},
        ],
        "changed_files": ["src/app.py"],
        "created_files": [],
        "skills": [{"name": "threejs_scene", "step": 3}],
        "mcp_servers": [{"server_id": "github", "tools": {"list_issues": "allow"}}],
        "jobs": [],
        "usage": {"requests": 2, "total_tokens": 2300, "costs": {"USD": 0.01}},
        "verified_snapshot": {"step": 6},
    }


class ProtocolTests(unittest.TestCase):
    def test_event_line_is_decoded_with_both_reasoning_channels(self) -> None:
        line = json.dumps(
            {
                "type": "event",
                "kind": "reasoning_delta",
                "step": 2,
                "reasoning_delta": "private",
                "reasoning_summary_delta": "public",
            }
        )
        decoded = decode_line(line)
        assert decoded is not None
        assert decoded.event is not None
        self.assertEqual(decoded.event.kind, "reasoning_delta")
        self.assertEqual(decoded.event.reasoning, "private")
        self.assertEqual(decoded.event.reasoning_summary, "public")

    def test_result_line_carries_the_report(self) -> None:
        line = json.dumps(
            {
                "type": "result",
                "report": {
                    "session_id": "s1",
                    "status": "verified",
                    "verified": True,
                    "reason": "change",
                    "steps": 3,
                    "changed_files": ["a.py", "b.py"],
                    "checks": [{"ok": True}, {"ok": False}],
                    "usage": {"input_tokens": 10, "output_tokens": 5},
                },
            }
        )
        decoded = decode_line(line)
        assert decoded is not None and decoded.outcome is not None
        self.assertEqual(decoded.outcome.token_total, 15)
        self.assertEqual(decoded.outcome.check_tally, (1, 2))
        self.assertEqual(decoded.outcome.changed_files, ("a.py", "b.py"))

    def test_malformed_or_foreign_lines_are_ignored(self) -> None:
        self.assertIsNone(decode_line(""))
        self.assertIsNone(decode_line("not json"))
        self.assertIsNone(decode_line('{"type": "other"}'))
        self.assertIsNone(decode_line("[1, 2, 3]"))

    def test_version_field_is_accepted_and_forward_tolerant(self) -> None:
        self.assertEqual(PROTOCOL_VERSION, 1)
        current = json.dumps(
            {"type": "event", "v": PROTOCOL_VERSION, "kind": "step_started", "step": 1}
        )
        decoded = decode_line(current)
        assert decoded is not None and decoded.event is not None
        self.assertEqual(decoded.event.kind, "step_started")
        future = json.dumps(
            {
                "type": "event",
                "v": PROTOCOL_VERSION + 1,
                "kind": "text_delta",
                "text_delta": "hi",
                "some_future_field": {"nested": True},
            }
        )
        decoded = decode_line(future)
        assert decoded is not None and decoded.event is not None
        self.assertEqual(decoded.event.text, "hi")
        # A version marker on the result document is equally tolerated.
        result = json.dumps(
            {"type": "result", "v": PROTOCOL_VERSION + 2, "report": {"session_id": "s"}}
        )
        decoded = decode_line(result)
        assert decoded is not None and decoded.outcome is not None
        self.assertEqual(decoded.outcome.session_id, "s")
        # A non-integer version never crashes the parser.
        broken = json.dumps({"type": "event", "v": "one", "kind": "step_started"})
        decoded = decode_line(broken)
        assert decoded is not None and decoded.event is not None

    def test_outcome_extracts_costs(self) -> None:
        line = json.dumps(
            {
                "type": "result",
                "report": {
                    "session_id": "s1",
                    "usage": {"total_tokens": 10, "costs": {"USD": 0.02, "bad": "x"}},
                },
            }
        )
        decoded = decode_line(line)
        assert decoded is not None and decoded.outcome is not None
        self.assertEqual(decoded.outcome.costs, {"USD": 0.02})


class ToolCategoryTests(unittest.TestCase):
    def test_kernel_tool_names_map_onto_categories(self) -> None:
        self.assertEqual(tool_category("repo.read_file"), "read")
        self.assertEqual(tool_category("repo.read_lines"), "read")
        self.assertEqual(tool_category("repo.list_files"), "read")
        self.assertEqual(tool_category("repo.search"), "read")
        self.assertEqual(tool_category("repo.edit_file"), "edit")
        self.assertEqual(tool_category("repo.write_file"), "edit")
        self.assertEqual(tool_category("repo.apply_patch"), "edit")
        self.assertEqual(tool_category("checks.run"), "test")
        self.assertEqual(tool_category("checks.run_affected"), "test")
        self.assertEqual(tool_category("tests.run"), "test")
        self.assertEqual(tool_category("mcp.github.list_issues"), "mcp")
        self.assertEqual(tool_category("skill.load"), "skill")
        self.assertEqual(tool_category("skill.list"), "skill")
        self.assertEqual(tool_category("plan.update"), "plan")
        self.assertEqual(tool_category("web.fetch"), "web")
        self.assertEqual(tool_category("repo.inspect"), "inspect")

    def test_subagent_and_unknown_names(self) -> None:
        self.assertEqual(tool_category("agent.helper"), "subagent")
        self.assertEqual(tool_category("task.run"), "subagent")
        self.assertEqual(tool_category("subagent.general"), "subagent")
        self.assertEqual(tool_category("something.new"), "other")
        self.assertEqual(tool_category(""), "other")


class SessionStateTests(unittest.TestCase):
    def test_full_record_is_parsed(self) -> None:
        state = SessionState.from_record(_session_record())
        self.assertEqual(state.session_id, "tui-test-1")
        self.assertEqual(state.task, "make it work")
        self.assertEqual(state.task_history, ("first ask", "make it work"))
        self.assertEqual(state.plan_tally, (1, 1, 1))
        self.assertEqual(state.check_tally, (1, 2))
        self.assertTrue(state.has_checkpoint)
        self.assertEqual(state.checkpoint_count, 1)
        self.assertEqual(state.last_checkpoint_at, 1761382000.0)
        self.assertEqual(state.skill_names, ("threejs_scene",))
        self.assertEqual(state.mcp_server_ids, ("github",))
        self.assertEqual(state.requests, 2)
        self.assertEqual(state.token_total, 2300)
        self.assertEqual(state.costs, {"USD": 0.01})
        self.assertEqual(state.verified_step, 6)
        self.assertEqual(state.changed_files, ("src/app.py",))
        self.assertEqual(state.status, "active")
        self.assertEqual(state.phase, "verifying")
        self.assertEqual(state.branch, "main")
        self.assertEqual(state.revision, 7)

    def test_garbage_records_produce_empty_state(self) -> None:
        for value in (None, 42, "x", [], {"task": 3, "plan_items": "no", "checks": 7}):
            state = SessionState.from_record(value)
            self.assertEqual(state.session_id, "")
            self.assertEqual(state.plan_tally, (0, 0, 0))
            self.assertEqual(state.check_tally, (0, 0))
            self.assertFalse(state.has_checkpoint)

    def test_bounded_lists(self) -> None:
        record = _session_record()
        record["plan_items"] = [
            {"text": f"item {index}", "status": "todo"} for index in range(500)
        ]
        record["checkpoints"] = [
            {"checkpoint_id": f"cp-{index}", "created_at": float(index)}
            for index in range(500)
        ]
        state = SessionState.from_record(record)
        self.assertLessEqual(len(state.plan_items), 60)
        self.assertLessEqual(len(state.checkpoints), 20)
        self.assertEqual(state.checkpoint_count, 500)
        self.assertEqual(state.last_checkpoint_at, 499.0)


class HeaderSegmentTests(unittest.TestCase):
    def fields(self, **overrides: object) -> list[str]:
        kwargs: dict[str, object] = {
            "model": "demo/demo-1",
            "repository": "proj",
            "branch": "main",
            "tokens": 45200,
            "context_fraction": None,
            "effort": "auto",
            "mode": "build",
            "activity": "",
            "running": False,
            "width": 120,
        }
        kwargs.update(overrides)
        return [text for text, _style in header_segments(**kwargs)]  # type: ignore[arg-type]

    def test_wide_header_carries_the_facts(self) -> None:
        fields = self.fields()
        self.assertEqual(fields[0], "demo/demo-1")
        self.assertIn("proj", fields)
        self.assertIn("main", fields)
        self.assertTrue(any("45.2k" in item for item in fields))

    def test_model_is_never_dropped(self) -> None:
        for width in (120, 100, 80, 60, 40, 24):
            fields = self.fields(width=width, running=True, activity="working")
            self.assertIn("demo/demo-1", fields)

    def test_fields_drop_whole_never_cut(self) -> None:
        narrow = self.fields(width=44)
        for field in narrow:
            self.assertNotIn("…", field)
        # The narrow tier keeps model and live state, not the branch.
        self.assertNotIn("main", narrow)

    def test_defaults_are_not_restated(self) -> None:
        fields = self.fields(width=120)
        self.assertFalse(any("auto" in item for item in fields))
        self.assertFalse(any("build" in item for item in fields))
        stated = self.fields(width=120, effort="high", mode="plan")
        self.assertTrue(any("high" in item for item in stated))
        self.assertTrue(any("plan" in item for item in stated))

    def test_context_note_only_near_the_limit(self) -> None:
        self.assertFalse(any("ctx" in item for item in self.fields(context_fraction=0.5)))
        self.assertFalse(any("ctx" in item for item in self.fields(context_fraction=0.79)))
        self.assertTrue(any("82%" in item for item in self.fields(context_fraction=0.82)))

    def test_running_shows_a_live_marker(self) -> None:
        self.assertTrue(any("live" in item for item in self.fields(running=True)))
        self.assertFalse(any("live" in item for item in self.fields(running=False)))


class WidgetTests(unittest.TestCase):
    def test_tool_card_shows_category_icon_while_running(self) -> None:
        running = tool_card_text("mcp.github.list_issues", "mcp", None, "", 0.0)
        text = str(running)
        self.assertIn("mcp.github.list_issues", text)
        self.assertIn("⬢", text)
        done = str(tool_card_text("mcp.github.list_issues", "mcp", True, "3 issues", 0.9))
        self.assertIn("✓", done)
        self.assertIn("ok", done)
        self.assertIn("3 issues", done)

    def test_tool_card_failure_is_red_and_marked(self) -> None:
        text = str(tool_card_text("repo.edit_file", "edit", False, "denied by policy", 0.1))
        self.assertIn("✗", text)
        self.assertIn("failed", text)

    def test_every_category_has_an_icon(self) -> None:
        for category in (
            "read", "edit", "test", "mcp", "skill", "plan", "web", "inspect",
            "subagent", "other",
        ):
            self.assertIn(category, CATEGORY_ICONS)

    def test_welcome_card_renders_facts(self) -> None:
        text = str(welcome_text("Ready", "body text", ("repo proj", "model demo/x")))
        self.assertIn("Ready", text)
        self.assertIn("repo proj", text)
        self.assertIn("model demo/x", text)


class BackendSurfaceTests(unittest.TestCase):
    def test_skill_list_uses_the_json_surface(self) -> None:
        backend = CLIBackend(Path("."))
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

        def fake_call(*arguments: str, **kwargs: object) -> object:
            calls.append((arguments, kwargs))
            return {"skills": [{"name": "a"}], "diagnostics": []}

        with patch.object(backend, "call", side_effect=fake_call):
            skills = backend.skill_list()
        self.assertEqual(skills, [{"name": "a"}])
        arguments = calls[0][0]
        self.assertEqual(arguments[0], "skill")
        self.assertEqual(arguments[1], "list")
        self.assertIn("--json", arguments)

    def test_skill_show_returns_the_skill_or_none(self) -> None:
        backend = CLIBackend(Path("."))
        with patch.object(
            backend, "call", return_value={"skill": {"name": "a"}, "diagnostics": []}
        ):
            self.assertEqual(backend.skill_show("a"), {"name": "a"})
        from karox.tui.backend import BackendError

        with patch.object(backend, "call", side_effect=BackendError("missing")):
            self.assertIsNone(backend.skill_show("nope"))


class I18nTests(unittest.TestCase):
    def test_catalogues_carry_the_same_keys(self) -> None:
        self.assertEqual(set(_RU), set(_EN))


class StreamJsonTests(unittest.TestCase):
    def test_stream_json_writes_one_document_per_event(self) -> None:
        event = AgentEvent(
            kind=AgentEventKind.TEXT_DELTA, step=1, text_delta="hi"
        )
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            cli._stream_json()(event)
        payload = json.loads(buffer.getvalue())
        self.assertEqual(payload["type"], "event")
        self.assertEqual(payload["kind"], "text_delta")
        self.assertEqual(payload["text_delta"], "hi")

    def test_emitted_events_round_trip_through_the_client_decoder(self) -> None:
        """The CLI emitter and the TUI decoder are one contract, tested as one.

        test_cli.py pins the emitter; ProtocolTests pin the decoder. This is
        the boundary itself: bytes written by ``_stream_json`` for real kernel
        events must decode into the same facts on the client side.
        """

        events = [
            AgentEvent(kind=AgentEventKind.STEP_STARTED, step=1),
            AgentEvent(
                kind=AgentEventKind.TOOL_STARTED,
                step=1,
                tool="repo.edit_file",
                call_id="c-1",
            ),
            AgentEvent(
                kind=AgentEventKind.TOOL_FINISHED,
                step=1,
                tool="repo.edit_file",
                call_id="c-1",
                ok=True,
                summary="path=app.py",
                duration_seconds=0.25,
            ),
            AgentEvent(
                kind=AgentEventKind.STEP_FINISHED,
                step=1,
                usage={"prompt_tokens": 900, "total_tokens": 120},
            ),
            AgentEvent(
                kind=AgentEventKind.FINISHED, step=1, status="verified", reason="change"
            ),
        ]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            observe = cli._stream_json()
            for event in events:
                observe(event)
        decoded = [
            decode_line(line) for line in buffer.getvalue().splitlines() if line.strip()
        ]
        self.assertEqual(len(decoded), len(events))
        kinds = [item.event.kind for item in decoded if item and item.event]
        self.assertEqual(
            kinds,
            ["step_started", "tool_started", "tool_finished", "step_finished", "finished"],
        )
        finished = decoded[2]
        assert finished is not None and finished.event is not None
        self.assertIs(finished.event.ok, True)
        self.assertEqual(finished.event.summary, "path=app.py")
        self.assertAlmostEqual(finished.event.duration_seconds, 0.25)
        step_done = decoded[3]
        assert step_done is not None and step_done.event is not None
        self.assertEqual(step_done.event.usage.get("prompt_tokens"), 900)


class AppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = Path(__file__).resolve().parent.parent

    def app(self, backend: _FakeBackend | None = None) -> KaroxApp:
        with patch.object(app_module.prefs, "load_preferences", return_value={}):
            return KaroxApp(
                self.repository,
                backend=backend or _FakeBackend(self.repository),
                language="en",
            )

    async def wait_idle(self, pilot, app: KaroxApp, timeout: float = 3.0) -> None:
        waited = 0.0
        while app._agent_running and waited < timeout:
            await pilot.pause(0.05)
            waited += 0.05
        await pilot.pause(0.1)
        await pilot.pause(0.1)

    async def test_detected_checks_are_a_suggestion_not_user_approval(self) -> None:
        """Detection may be shown; only /verify may approve.

        The CLI treats --verification-command as the human's explicit decision.
        The TUI used to pass its own detected commands through that flag, which
        silently turned repository discovery into approval.
        """
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertTrue(app._detected_verification)
            self.assertEqual(app._verification, ())
            app._add_verification("python -m pytest -q")
            self.assertEqual(app._verification, (("python", "-m", "pytest", "-q"),))
            app._add_verification("")
            self.assertEqual(app._verification, (("python", "-m", "pytest", "-q"),))

    async def test_welcome_and_help(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(len(app.query_one("#chat").children), 1)
            app.query_one("#composer").focus()
            app.query_one("#composer").text = "/help"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 2)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)

    async def test_task_streams_answer_and_tool_card(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.query_one("#composer").focus()
            app.query_one("#composer").text = "inspect the repo"
            await pilot.press("enter")
            await self.wait_idle(pilot, app)
            chat = app.query_one("#chat")
            kinds = [type(widget) for widget in chat.children]
            self.assertIn(UserMessage, kinds)
            self.assertIn(AssistantMessage, kinds)
            self.assertIn(ToolCard, kinds)
            self.assertTrue(any(kind is Notice for kind in kinds))
            assistant = next(
                widget for widget in chat.children if isinstance(widget, AssistantMessage)
            )
            self.assertEqual(assistant.answer_text(), "Hello world")
            self.assertEqual(app._tokens, 1234)
            self.assertTrue(app._continue)

    async def test_model_picker_updates_the_selection(self) -> None:
        backend = _FakeBackend(self.repository)
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("f2")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 2)
            await pilot.press("down", "enter")
            await self.wait_idle(pilot, app)
            self.assertEqual(backend.selected, [("demo", "demo-2")])

    async def test_unknown_command_is_a_notice_not_a_run(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.query_one("#composer").focus()
            app.query_one("#composer").text = "/nope"
            await pilot.press("enter")
            await pilot.pause()
            notices = [
                widget for widget in app.query_one("#chat").children if isinstance(widget, Notice)
            ]
            self.assertEqual(len(notices), 1)

    async def test_provider_modal_opens_in_a_small_terminal(self) -> None:
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            app.query_one("#composer").focus()
            app.query_one("#composer").text = "/provider"
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 2)
            from karox.tui.modals import ProviderSetupScreen

            self.assertIsInstance(app.screen, ProviderSetupScreen)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)

    async def test_stop_before_any_run_is_harmless(self) -> None:
        backend = _FakeBackend(self.repository)
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.action_stop()
            self.assertEqual(backend.stops, 0)

    async def test_run_exposes_state_counters(self) -> None:
        backend = _FakeBackend(self.repository, script=_rich_script())
        app = self.app(backend)
        async with app.run_test(size=(110, 32)) as pilot:
            await pilot.pause()
            app.query_one("#composer").focus()
            app.query_one("#composer").text = "do the whole thing"
            await pilot.press("enter")
            await self.wait_idle(pilot, app)
            state = app.run_state
            self.assertEqual(state.state, "done")
            self.assertEqual(state.step, 2)
            self.assertEqual(state.reads, 1)
            self.assertEqual(state.edits, 1)
            self.assertEqual(state.tests_ok, 1)
            self.assertEqual(state.tests_failed, 0)
            self.assertEqual(state.mcp_calls, 1)
            self.assertEqual(state.skill_calls, 1)
            self.assertEqual(state.inspections, 1)
            self.assertIn("src/app.py", state.files)
            # 1500 + 800 per-step totals, then the report's 1234 wins at the end.
            self.assertEqual(app._tokens, 1234)
            self.assertEqual(state.prompt_tokens, 90000)
            self.assertIsNotNone(state.context_fraction)
            self.assertAlmostEqual(state.context_fraction or 0.0, 90000 / 128000, places=3)
            # The chat holds one card per tool call in the script.
            cards = [
                widget
                for widget in app.query_one("#chat").children
                if isinstance(widget, ToolCard)
            ]
            self.assertEqual(len(cards), 5)
            categories = {card.tool: card.category for card in cards}
            self.assertEqual(categories["mcp.github.list_issues"], "mcp")
            self.assertEqual(categories["skill.load"], "skill")
            self.assertEqual(categories["repo.inspect"], "inspect")

    async def test_context_note_appears_near_the_window_limit(self) -> None:
        script = [
            Event(kind="step_started", step=1),
            Event(
                kind="step_finished",
                step=1,
                usage={"prompt_tokens": 120000, "total_tokens": 500},
            ),
            Event(kind="text_delta", step=1, text="tight"),
        ]
        backend = _FakeBackend(self.repository, script=script)
        app = self.app(backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await pilot.pause()
            app.query_one("#composer").focus()
            app.query_one("#composer").text = "nearly full context"
            await pilot.press("enter")
            await self.wait_idle(pilot, app)
            self.assertGreaterEqual(app.run_state.context_fraction or 0.0, 0.9)

    async def test_checkpoint_state_refreshes_after_a_run(self) -> None:
        record = _session_record()
        backend = _FakeBackend(self.repository, record_after_run=record)
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            self.assertIsNone(app.checkpoint)
            app.query_one("#composer").focus()
            app.query_one("#composer").text = "run and save"
            await pilot.press("enter")
            await self.wait_idle(pilot, app)
            self.assertIsNotNone(app.checkpoint)
            assert app.checkpoint is not None
            self.assertEqual(app.checkpoint.checkpoint_count, 1)
            self.assertEqual(app.checkpoint.check_tally, (1, 2))

    async def test_session_panel_opens_with_record_and_empty_state(self) -> None:
        from karox.tui.modals import SessionPanel

        record = _session_record()
        backend = _FakeBackend(self.repository, record=record)
        app = self.app(backend)
        async with app.run_test(size=(110, 34)) as pilot:
            await pilot.pause()
            app.query_one("#composer").focus()
            app.query_one("#composer").text = "do it"
            await pilot.press("enter")
            await self.wait_idle(pilot, app)
            await pilot.press("f5")
            await pilot.pause()
            self.assertIsInstance(app.screen, SessionPanel)
            panel = app.screen
            assert isinstance(panel, SessionPanel)
            body = panel.body_text()
            self.assertIn("make it work", body)
            self.assertIn("threejs_scene", body)
            self.assertIn("github", body)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)

        empty_backend = _FakeBackend(self.repository)
        empty_app = self.app(empty_backend)
        async with empty_app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            await pilot.press("f5")
            await pilot.pause()
            self.assertIsInstance(empty_app.screen, SessionPanel)
            panel = empty_app.screen
            assert isinstance(panel, SessionPanel)
            self.assertTrue(panel.body_text())

    async def test_skills_command_lists_and_handles_empty(self) -> None:
        from karox.tui.modals import ListScreen

        backend = _FakeBackend(
            self.repository,
            skills=[
                {"name": "alpha", "description": "first skill"},
                {"name": "beta", "description": "second skill"},
            ],
        )
        backend.skill_details["alpha"] = {
            "name": "alpha",
            "description": "first skill",
            "when_to_use": "use it wisely",
            "permissions": ["repo.read_file"],
            "files": ["SKILL.md"],
        }
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.query_one("#composer").focus()
            app.query_one("#composer").text = "/skills"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, ListScreen)
            await pilot.press("enter")
            await pilot.pause()
            from karox.tui.modals import TextViewerScreen

            self.assertIsInstance(app.screen, TextViewerScreen)
            viewer = app.screen
            assert isinstance(viewer, TextViewerScreen)
            self.assertIn("use it wisely", viewer.body_text())
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)

        empty_app = self.app(_FakeBackend(self.repository))
        async with empty_app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            empty_app.query_one("#composer").focus()
            empty_app.query_one("#composer").text = "/skills"
            await pilot.press("enter")
            await pilot.pause()
            notices = [
                widget
                for widget in empty_app.query_one("#chat").children
                if isinstance(widget, Notice)
            ]
            self.assertTrue(notices)

    async def test_backend_failure_is_an_error_state_not_a_crash(self) -> None:
        backend = _FakeBackend(self.repository, fail_stream=True)
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.query_one("#composer").focus()
            app.query_one("#composer").text = "this will fail"
            await pilot.press("enter")
            await self.wait_idle(pilot, app)
            self.assertEqual(app.run_state.state, "failed")
            self.assertFalse(app._agent_running)
            notices = [
                widget
                for widget in app.query_one("#chat").children
                if isinstance(widget, Notice)
            ]
            self.assertTrue(any(notice.level == "error" for notice in notices))
            self.assertTrue(any("provider exploded" in notice.text for notice in notices))

    async def test_verification_tally_reaches_the_result_notice(self) -> None:
        outcome = Outcome(
            session_id="s-x",
            status="failed",
            verified=False,
            reason="check_failed",
            steps=2,
            changed_files=("a.py",),
            checks=({"ok": True}, {"ok": False}),
            usage={"total_tokens": 99},
        )
        backend = _FakeBackend(self.repository, outcome=outcome)
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            app.query_one("#composer").focus()
            app.query_one("#composer").text = "break things"
            await pilot.press("enter")
            await self.wait_idle(pilot, app)
            notices = [
                widget
                for widget in app.query_one("#chat").children
                if isinstance(widget, Notice)
            ]
            self.assertTrue(any("1/2" in notice.text for notice in notices))
            self.assertEqual(app.run_state.state, "failed")

    async def test_narrow_terminal_completes_a_run(self) -> None:
        app = self.app()
        async with app.run_test(size=(48, 16)) as pilot:
            await pilot.pause()
            app.query_one("#composer").focus()
            app.query_one("#composer").text = "narrow run"
            await pilot.press("enter")
            await self.wait_idle(pilot, app)
            assistant = [
                widget
                for widget in app.query_one("#chat").children
                if isinstance(widget, AssistantMessage)
            ]
            self.assertTrue(assistant)
            self.assertEqual(assistant[0].answer_text(), "Hello world")
            header = app.query_one("#header")
            self.assertEqual(header.size.height, 1)
            self.assertNotIn("\n", header.current_text())

    async def test_composer_recalls_submitted_tasks(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await pilot.pause()
            composer = app.query_one("#composer")
            composer.focus()
            composer.text = "first task"
            await pilot.press("enter")
            await self.wait_idle(pilot, app)
            composer.focus()
            composer.text = "second task"
            await pilot.press("enter")
            await self.wait_idle(pilot, app)
            composer.focus()
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(composer.text, "second task")
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(composer.text, "first task")
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(composer.text, "second task")

    async def test_pageup_scrolls_the_chat_log(self) -> None:
        app = self.app()
        async with app.run_test(size=(90, 20)) as pilot:
            await pilot.pause()
            for index in range(60):
                app._notice(f"line {index}", "info")
            await pilot.pause()
            chat = app.query_one("#chat")
            chat.scroll_end(animate=False)
            await pilot.pause()
            before = chat.scroll_y
            self.assertGreater(before, 0)
            await pilot.press("pageup")
            await pilot.pause()
            self.assertLess(chat.scroll_y, before)
