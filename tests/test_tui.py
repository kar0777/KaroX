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
from karox.tui.backend import StreamResult
from karox.tui.protocol import Event, Outcome, decode_line
from karox.tui.widgets import AssistantMessage, Notice, ToolCard, UserMessage


class _FakeBackend:
    def __init__(self, repository: Path, *, outcome: Outcome | None = None) -> None:
        self.repository = repository
        self.stops = 0
        self.selected: list[tuple[str, str]] = []
        self._outcome = outcome
        self.sessions: list[dict[str, object]] = []

    def session(self, session_id: str) -> dict[str, object] | None:
        return None

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
        on_event(Event(kind="step_started", step=1))
        on_event(Event(kind="text_delta", step=1, text="Hello"))
        on_event(
            Event(
                kind="tool_started",
                step=1,
                tool="repo.read_file",
                call_id="call-1",
            )
        )
        on_event(
            Event(
                kind="tool_finished",
                step=1,
                tool="repo.read_file",
                call_id="call-1",
                ok=True,
                summary="read 12 lines",
                duration_seconds=0.12,
            )
        )
        on_event(Event(kind="text_delta", step=1, text=" world"))
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