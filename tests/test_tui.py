"""Contracts for the compact terminal client.

The screen is exercised through Textual's own pilot, and the run is a recorded
stream: no model, no network, no keyring. That is the point of the subprocess
boundary -- the UI can be tested at its real shape without a real agent.
"""

from __future__ import annotations

import contextlib
import io
import json
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from textual.widgets import Static

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
from karox.tui import commands as slash
from karox.tui import views
from karox.tui.widgets import (
    AssistantMessage,
    Notice,
    SecretPrompt,
    StatusLine,
    TextBlock,
    ToolCard,
    UserMessage,
    WelcomeCard,
    header_segments,
    tool_card_text,
    welcome_text,
)

#: The check AppTests' user approved before the test starts (see ``app()``).
APPROVED_CHECK = ("python", "-m", "pytest", "-q")


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
        self.session_rows: list[dict[str, object]] = []
        self.catalog_rows: list[dict[str, object]] = [
            {
                "provider_id": "wandb",
                "display_name": "Weights & Biases",
                "env": ["WANDB_API_KEY"],
                "url_variables": [],
                "model_count": 2,
                "installable": True,
                "connected_as": None,
            },
            {
                "provider_id": "deepseek",
                "display_name": "DeepSeek",
                "env": ["DEEPSEEK_API_KEY"],
                "url_variables": [],
                "model_count": 2,
                "installable": True,
                "connected_as": None,
            },
        ]
        self.connect_calls: list[dict[str, object]] = []
        self.connect_activates = False
        self.extra_models: list[dict[str, object]] = []
        self.servers: list[dict[str, object]] = []
        self.probes: list[str] = []
        self.removed: list[str] = []
        self.requests: list[object] = []
        self.mcp_selections: list[tuple[str, str, str]] = []
        self.mcp_deselections: list[tuple[str, str]] = []
        self.mcp_tool_rows: list[dict[str, object]] = [
            {"remote_name": "list_issues", "read_only": True},
            {"remote_name": "create_issue", "read_only": False},
        ]
        # A gate holds the stream after event ``gate_after`` until the test
        # releases it (or the app stops the run), so a live run can be seen.
        self.gate: threading.Event | None = None
        self.gate_after = 0
        self.gate_reached = threading.Event()
        self._stop_requested = threading.Event()

    def session(self, session_id: str) -> dict[str, object] | None:
        if self.record is not None and self.record.get("session_id") == session_id:
            return self.record
        return self.record if self.record is not None and session_id else None

    def sessions(self) -> list[dict[str, object]]:
        return self.session_rows

    def provider_catalog(self, query: str = "") -> list[dict[str, object]]:
        return list(self.catalog_rows)

    def provider_connect(
        self,
        catalog_id: str,
        *,
        api_key: str = "",
        provider_id: str = "",
        model: str = "",
        variables: tuple[tuple[str, str], ...] = (),
    ) -> dict[str, object]:
        self.connect_calls.append(
            {
                "catalog_id": catalog_id,
                "api_key": api_key,
                "provider_id": provider_id,
                "variables": tuple(variables),
            }
        )
        self.extra_models = [
            {"provider_id": catalog_id, "model_id": "big-model", "selected": False},
            {"provider_id": catalog_id, "model_id": "small-model", "selected": False},
        ]
        return {
            "provider_id": catalog_id,
            "display_name": catalog_id,
            "selected_model": "big-model",
            "activated": self.connect_activates,
            "models_installed": 2,
            "priced_models": 1,
        }

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
            *self.extra_models,
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

    def mcp_status(self, session_id: str | None = None) -> dict[str, object]:
        return {"servers": list(self.servers)}

    def mcp_remove(self, server_id: str) -> dict[str, object]:
        self.removed.append(server_id)
        return {"status": "removed"}

    def mcp_probe(self, server_id: str) -> dict[str, object]:
        self.probes.append(server_id)
        return {"status": "ok", "tool_count": 4}

    def mcp_tools(self, server_id: str) -> list[dict[str, object]]:
        return list(self.mcp_tool_rows)

    def mcp_select(
        self, server_id: str, session_id: str, scope: str = "all"
    ) -> dict[str, object]:
        self.mcp_selections.append((server_id, session_id, scope))
        tools = {
            str(item["remote_name"]): {
                "permission": "allow" if scope == "all" or item["read_only"] else "deny"
            }
            for item in self.mcp_tool_rows
        }
        return {"selection": {"tools": tools}}

    def mcp_deselect(self, server_id: str, session_id: str) -> dict[str, object]:
        self.mcp_deselections.append((server_id, session_id))
        return {"status": "deselected"}

    def stop(self) -> None:
        self.stops += 1
        self._stop_requested.set()
        if self.gate is not None:
            self.gate.set()

    def stream_agent(self, request, on_event):
        self.requests.append(request)
        self._stop_requested.clear()
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
        for index, event in enumerate(script):
            on_event(event)
            if self.gate is not None and index == self.gate_after:
                self.gate_reached.set()
                self.gate.wait(10.0)
                if self._stop_requested.is_set():
                    return StreamResult(outcome=None, exit_code=-15, stopped=True)
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
    def test_running_tool_line_names_the_call_and_its_target(self) -> None:
        running = str(
            tool_card_text("repo.read_file", "read", None, language="en", detail="src/app.py")
        )
        self.assertTrue(running.startswith("● Read(src/app.py)"))
        self.assertIn("⎿  Running…", running)
        mcp = str(tool_card_text("mcp.github.list_issues", "mcp", None, language="en"))
        self.assertIn("github · list_issues (MCP)", mcp)
        done = str(
            tool_card_text(
                "mcp.github.list_issues",
                "mcp",
                True,
                "bytes=120",
                0.9,
                language="en",
                preview="3 open issues",
            )
        )
        self.assertIn("⎿  Done", done)
        self.assertIn("3 open issues", done)
        self.assertNotIn("Running", done)

    def test_result_line_speaks_words_not_key_value_facts(self) -> None:
        read = str(
            tool_card_text("repo.read_file", "read", True, "src/app.py, lines=40", language="en")
        )
        self.assertIn("Read 40 lines", read)
        self.assertNotIn("lines=40", read)
        russian = str(
            tool_card_text("repo.read_file", "read", True, "src/app.py, lines=3", language="ru")
        )
        self.assertIn("Прочитано 3 строки", russian)
        search = str(
            tool_card_text("repo.search", "read", True, "matches=5, files=2", language="ru")
        )
        self.assertIn("Найдено 5 совпадений · 2 файла", search)
        command = str(tool_card_text("dev.command", "other", True, "exit 0", language="en"))
        self.assertIn("Exit code 0", command)

    def test_edit_preview_is_a_numbered_diff_folded_until_expanded(self) -> None:
        preview = "@@ -10,3 +10,3 @@\n def f():\n-    return 1\n+    return 2\n" + "".join(
            f"+    line {index}\n" for index in range(12)
        )
        folded = tool_card_text(
            "repo.edit_file", "edit", True, "src/app.py, +13 -1", language="en",
            detail="src/app.py", preview=preview,
        )
        plain = folded.plain
        self.assertIn("● Update(src/app.py)", plain)
        self.assertIn("Updated: +13 −1", plain)
        self.assertIn("11 -     return 1", plain)
        self.assertIn("11 +     return 2", plain)
        self.assertIn("ctrl+o", plain)
        expanded = tool_card_text(
            "repo.edit_file", "edit", True, "src/app.py, +13 -1", language="en",
            detail="src/app.py", preview=preview, expanded=True,
        ).plain
        self.assertIn("line 11", expanded)
        self.assertNotIn("ctrl+o", expanded)

    def test_failure_is_red_and_carries_the_reason(self) -> None:
        text = str(
            tool_card_text(
                "repo.edit_file", "edit", False, "McpAccessDenied", 0.1,
                language="en", error="denied by policy",
            )
        )
        self.assertIn("Error: denied by policy", text)
        russian = str(
            tool_card_text("repo.edit_file", "edit", False, "denied", 0.1, language="ru")
        )
        self.assertIn("Ошибка", russian)
        self.assertNotIn("failed", russian)
        interrupted = str(
            tool_card_text("dev.command", "other", None, language="ru", interrupted=True)
        )
        self.assertIn("Прервано", interrupted)

    def test_plan_update_renders_a_checklist(self) -> None:
        detail = "done\tread the repo\ndoing\tedit the file\ntodo\trun checks"
        plain = tool_card_text(
            "plan.update", "plan", True, "items=3, done=1", language="en", detail=detail
        ).plain
        self.assertTrue(plain.startswith("● Plan"))
        self.assertIn("☒ read the repo", plain)
        self.assertIn("◼ edit the file", plain)
        self.assertIn("☐ run checks", plain)

    def test_welcome_card_renders_facts(self) -> None:
        text = str(welcome_text("Ready", "body text", ("repo proj", "model demo/x")))
        self.assertIn("Ready", text)
        self.assertIn("repo proj", text)
        self.assertIn("model demo/x", text)


class ToolTextTests(unittest.TestCase):
    def test_titles_follow_the_coding_cli_vocabulary(self) -> None:
        from karox.tui.tooltext import tool_title

        self.assertEqual(tool_title("repo.read_file"), "Read")
        self.assertEqual(tool_title("repo.edit_file"), "Update")
        self.assertEqual(tool_title("dev.command"), "Run")
        self.assertEqual(tool_title("git.status"), "Git")
        self.assertEqual(tool_title("mcp.github.list_issues"), "github · list_issues (MCP)")
        self.assertEqual(tool_title("something.new"), "something.new")

    def test_working_text_says_what_is_happening(self) -> None:
        from karox.tui.tooltext import working_text

        self.assertEqual(working_text("repo.read_file", "src/a.py", "en"), "Reading src/a.py…")
        self.assertEqual(working_text("repo.read_file", "", "ru"), "Читаю файл…")
        self.assertEqual(working_text("dev.command", "npm test", "ru"), "Запускаю npm test…")
        self.assertIn("github · list_issues", working_text("mcp.github.list_issues", "", "en"))

    def test_russian_plural_forms(self) -> None:
        from karox.tui.i18n import plural

        self.assertEqual(plural("ru", 1, "line"), "1 строка")
        self.assertEqual(plural("ru", 3, "line"), "3 строки")
        self.assertEqual(plural("ru", 11, "line"), "11 строк")
        self.assertEqual(plural("ru", 21, "file"), "21 файл")
        self.assertEqual(plural("en", 1, "file"), "1 file")
        self.assertEqual(plural("en", 2, "match"), "2 matches")

    def test_next_plan_item_prefers_the_current_one(self) -> None:
        from karox.tui.tooltext import next_plan_item, plan_rows

        rows = plan_rows("done\ta\ndoing\tb\ntodo\tc")
        self.assertEqual(next_plan_item(rows), "b")
        self.assertEqual(next_plan_item(plan_rows("done\ta\ntodo\tc")), "c")
        self.assertEqual(next_plan_item(plan_rows("done\ta")), "")


class TextualOverrideTests(unittest.TestCase):
    """KaroX widgets must not shadow Textual internals by accident."""

    # Hooks Textual expects subclasses to define, plus overrides that are
    # deliberate and call through to the base implementation.
    INTENDED = {"TITLE", "CSS_PATH", "BINDINGS", "DEFAULT_CSS", "compose", "action_quit"}
    DELIBERATE = {"ChatInput": {"_on_key"}}

    def test_no_karox_widget_shadows_a_textual_member_by_accident(self) -> None:
        import inspect

        from karox.tui import modals, widgets

        clashes: list[str] = []
        for module in (app_module, widgets, modals, views, slash):
            for name, cls in vars(module).items():
                if not inspect.isclass(cls) or cls.__module__ != module.__name__:
                    continue
                textual_bases = [
                    base for base in cls.__mro__[1:] if base.__module__.startswith("textual")
                ]
                if not textual_bases:
                    continue
                # Textual stamps these on every subclass it creates.
                stamped = set(type("Probe", (textual_bases[0],), {}).__dict__)
                allowed = self.INTENDED | self.DELIBERATE.get(name, set())
                for attr in cls.__dict__:
                    if attr.startswith("__") or attr in stamped or attr in allowed:
                        continue
                    if any(attr in vars(base) for base in textual_bases):
                        clashes.append(f"{name}.{attr}")
        self.assertEqual(clashes, [])


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


class SlashCommandTests(unittest.TestCase):
    def test_prefix_matches_come_first_and_aliases_resolve(self) -> None:
        self.assertEqual(slash.match("mo")[0].name, "model")
        self.assertEqual(slash.match("")[0].name, "help")
        self.assertEqual(len(slash.match("")), len(slash.COMMANDS))
        self.assertEqual(slash.resolve("resume").name, "sessions")  # type: ignore[union-attr]
        self.assertEqual(slash.resolve("Q").name, "quit")  # type: ignore[union-attr]
        self.assertIsNone(slash.resolve("bridge"))
        # Substring matches follow the prefix ones.
        names = [command.name for command in slash.match("ss")]
        self.assertIn("sessions", names)
        self.assertIn("session", names)

    def test_every_command_has_both_descriptions_and_a_handler(self) -> None:
        for command in slash.COMMANDS:
            self.assertIn(command.description, _RU)
            self.assertIn(command.description, _EN)
            self.assertTrue(
                hasattr(KaroxApp, "_cmd_" + command.name), f"no handler for /{command.name}"
            )

    def test_help_lists_every_command(self) -> None:
        text = views.help_text("en").plain
        for command in slash.COMMANDS:
            self.assertIn(f"/{command.name}", text)
        self.assertNotIn("F1", text)


class ViewTests(unittest.TestCase):
    def test_model_items_mark_the_current_model_and_price(self) -> None:
        items, current = views.model_items(
            [
                {
                    "provider_id": "demo",
                    "model_id": "a",
                    "selected": True,
                    "context_window": 128000,
                    "pricing": {"input_per_million": 0.55, "output_per_million": 1.65},
                },
                {"provider_id": "demo", "model_id": "b", "selected": False},
                {"provider_id": "", "model_id": "broken"},
            ],
            "en",
        )
        self.assertEqual(current, "demo/a")
        self.assertEqual([item.key for item in items], ["demo/a", "demo/b"])
        self.assertTrue(items[0].marked)
        self.assertIn("128.0k", items[0].detail)
        self.assertIn("$0.55/$1.65", items[0].detail)
        self.assertIn("no published price", items[1].detail)

    def test_catalog_items_put_connected_first_and_end_with_custom(self) -> None:
        rows = [
            {"provider_id": "a", "display_name": "A", "env": ["A_KEY"], "model_count": 3},
            {"provider_id": "b", "display_name": "B", "env": [], "connected_as": "b", "model_count": 1},
            {"provider_id": "c", "display_name": "C", "installable": False},
        ]
        items = views.catalog_items(rows, "ru")
        self.assertEqual([item.key for item in items], ["b", "a", views.CUSTOM_PROVIDER_KEY])
        self.assertIn("подключён", items[0].detail)
        self.assertIn("A_KEY", items[1].detail)

    def test_session_items_keep_this_repository_newest_first(self) -> None:
        repository = Path(__file__).resolve().parent.parent
        rows = [
            {"session_id": "old", "repository": str(repository), "updated_at": 1.0, "task": "first"},
            {"session_id": "new", "repository": str(repository), "updated_at": 2.0, "task": "second"},
            {"session_id": "other", "repository": "/elsewhere", "updated_at": 3.0},
        ]
        items = views.session_items(rows, repository, "new", "en")
        self.assertEqual([item.key for item in items], ["new", "old"])
        self.assertTrue(items[0].marked)

    def test_session_lines_cover_every_section(self) -> None:
        state = SessionState.from_record(_session_record())
        text = views.lines_text(views.session_state_lines(state, "en")).plain
        for needle in ("make it work", "threejs_scene", "github", "src/app.py", "1/2"):
            self.assertIn(needle, text)
        empty = views.lines_text(views.session_state_lines(None, "ru")).plain
        self.assertIn("Сохранённой сессии пока нет", empty)


class BackendCommandLineTests(unittest.TestCase):
    def test_connect_sends_the_key_on_stdin_only(self) -> None:
        backend = CLIBackend(Path("."))
        calls: list[tuple[tuple[str, ...], dict[str, object]]] = []

        def fake_call(*arguments: str, **kwargs: object) -> object:
            calls.append((arguments, kwargs))
            return {"provider_id": "wandb"}

        with patch.object(backend, "call", side_effect=fake_call):
            backend.provider_connect(
                "cloudflare-workers-ai",
                api_key="sk-secret",
                provider_id="cf",
                variables=(("CLOUDFLARE_ACCOUNT_ID", "abc"),),
            )
            backend.provider_connect("wandb")
        arguments, kwargs = calls[0]
        self.assertNotIn("sk-secret", " ".join(arguments))
        self.assertEqual(kwargs.get("input_text"), "sk-secret\n")
        self.assertIn("--stdin-key", arguments)
        self.assertIn("CLOUDFLARE_ACCOUNT_ID=abc", arguments)
        self.assertIn("cf", arguments)
        reuse_arguments, reuse_kwargs = calls[1]
        self.assertNotIn("--stdin-key", reuse_arguments)
        self.assertIsNone(reuse_kwargs.get("input_text"))

    def test_calls_without_input_do_not_inherit_the_terminal(self) -> None:
        backend = CLIBackend(Path("."))
        seen: dict[str, object] = {}

        def fake_run(command, **kwargs):  # type: ignore[no-untyped-def]
            seen.update(kwargs)

            class Completed:
                returncode = 0
                stdout = "[]"
                stderr = ""

            return Completed()

        with patch("karox.tui.backend.subprocess.run", side_effect=fake_run):
            self.assertEqual(backend.provider_catalog(), [])
        self.assertIs(seen.get("stdin"), __import__("subprocess").DEVNULL)
        self.assertNotIn("input", seen)


class AppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = Path(__file__).resolve().parent.parent
        saver = patch.object(app_module.prefs, "save_preferences")
        saver.start()
        self.addCleanup(saver.stop)

    def app(
        self,
        backend: _FakeBackend | None = None,
        language: str = "en",
        *,
        approved: bool = True,
    ) -> KaroxApp:
        """An app whose user already approved one check, unless ``approved=False``.

        The runtime starts an agent only with a user-approved verification
        command; most tests are about what happens after that decision.
        """

        with patch.object(app_module.prefs, "load_preferences", return_value={}):
            app = KaroxApp(
                self.repository,
                backend=backend or _FakeBackend(self.repository),
                language=language,
            )
        if approved:
            app._verification = (APPROVED_CHECK,)
        return app

    async def settle(self, pilot, app: KaroxApp, timeout: float = 5.0) -> None:
        """Wait until the run and every background CLI call have finished."""

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await pilot.pause(0.02)
            busy = app._agent_running or any(not worker.is_finished for worker in app.workers)
            if not busy:
                break
        await pilot.pause()
        await pilot.pause()

    async def submit(self, pilot, app: KaroxApp, text: str) -> None:
        composer = app.query_one("#composer")
        composer.focus()
        composer.text = text
        await pilot.pause()
        if app.menu.active:
            # Typing a full command leaves the palette open on it; submitting
            # is the same Enter either way.
            app.menu.close()
        await pilot.press("enter")
        await self.settle(pilot, app)

    def chat_widgets(self, app: KaroxApp, kind: type) -> list:
        return [widget for widget in app.query_one("#chat").children if isinstance(widget, kind)]

    def transcript(self, app: KaroxApp) -> str:
        parts: list[str] = []
        for widget in app.query_one("#chat").children:
            if isinstance(widget, AssistantMessage):
                parts.append(widget.answer_text())
            elif isinstance(widget, TextBlock):
                parts.append(widget.plain_text())
            elif isinstance(widget, (Notice, UserMessage)):
                parts.append(widget.text)
            elif isinstance(widget, (ToolCard, WelcomeCard)):
                parts.append(str(widget.render()))
        return "\n".join(parts)

    async def test_welcome_shows_the_loaded_model_and_suggested_checks(self) -> None:
        app = self.app(approved=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            cards = self.chat_widgets(app, WelcomeCard)
            self.assertEqual(len(cards), 1)
            welcome = str(cards[0].render())
            self.assertIn("demo/demo-1", welcome)
            self.assertIn("/verify", welcome)
            self.assertIn("demo/demo-1", app.query_one(StatusLine).current_text())

    async def test_detected_checks_are_a_suggestion_not_user_approval(self) -> None:
        """Detection may be shown; only /verify may approve.

        The CLI treats --verification-command as the human's explicit decision.
        The TUI used to pass its own detected commands through that flag, which
        silently turned repository discovery into approval.
        """
        app = self.app(approved=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            self.assertTrue(app._detected_verification)
            self.assertEqual(app._verification, ())
            await self.submit(pilot, app, "/verify python -m pytest -q")
            self.assertEqual(app._verification, (("python", "-m", "pytest", "-q"),))
            await self.submit(pilot, app, "/verify")
            self.assertEqual(app._verification, (("python", "-m", "pytest", "-q"),))
            await self.submit(pilot, app, "/unverify")
            self.assertEqual(app._verification, ())

    async def test_a_task_without_an_approved_check_asks_for_one_first(self) -> None:
        """No approved check: a picker of detected commands, not a failed run.

        The runtime refuses an agent without a user-approved verification
        command, so the TUI used to start a run that failed at once with
        "native agents require at least one user-approved verification
        command". Now the task waits; nothing reaches the backend until the
        user picks a check, and the pick is the approval.
        """
        backend = _FakeBackend(self.repository)
        app = self.app(backend, approved=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "fix the bug")
            self.assertEqual(backend.requests, [])
            self.assertEqual(app.menu.kind, "verify")
            self.assertTrue(app.menu.active)
            self.assertEqual(self.chat_widgets(app, UserMessage), [])
            await pilot.press("enter")
            await self.settle(pilot, app)
            self.assertFalse(app.menu.active)
            self.assertEqual(app._verification, (app._detected_verification[0],))
            self.assertEqual(len(backend.requests), 1)
            request = backend.requests[0]
            self.assertEqual(request.task, "fix the bug")
            self.assertEqual(request.verification, (app._detected_verification[0],))
            self.assertIn("fix the bug", [widget.text for widget in self.chat_widgets(app, UserMessage)])

    async def test_a_pasted_verify_command_in_the_check_picker_is_run(self) -> None:
        backend = _FakeBackend(self.repository)
        app = self.app(backend, approved=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "fix the bug")
            self.assertEqual(app.menu.kind, "verify")
            composer = app.query_one("#composer")
            composer.text = "/verify python -m unittest -q"
            await pilot.pause()
            await pilot.press("enter")
            await self.settle(pilot, app)
            self.assertEqual(app._verification, (("python", "-m", "unittest", "-q"),))
            self.assertEqual([request.task for request in backend.requests], ["fix the bug"])

    async def test_cancelling_the_check_picker_gives_the_task_back(self) -> None:
        backend = _FakeBackend(self.repository)
        app = self.app(backend, approved=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "fix the bug")
            self.assertEqual(app.menu.kind, "verify")
            await pilot.press("escape")
            await self.settle(pilot, app)
            self.assertFalse(app.menu.active)
            self.assertEqual(str(app.composer.text), "fix the bug")
            self.assertEqual(backend.requests, [])
            self.assertEqual(app._verification, ())
            self.assertIsNone(app._verify_task)

    async def test_a_custom_check_typed_with_verify_starts_the_waiting_task(self) -> None:
        backend = _FakeBackend(self.repository)
        app = self.app(backend, approved=False)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "fix the bug")
            keys = app.menu.keys()
            self.assertEqual(keys[-1], "custom")
            app.menu.move(len(keys))
            self.assertEqual(app.menu.highlighted_key(), "custom")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(str(app.composer.text), "/verify ")
            self.assertEqual(backend.requests, [])
            await self.submit(pilot, app, "/verify python -m unittest -q")
            self.assertEqual(app._verification, (("python", "-m", "unittest", "-q"),))
            self.assertEqual(len(backend.requests), 1)
            self.assertEqual(backend.requests[0].task, "fix the bug")
            self.assertEqual(
                backend.requests[0].verification, (("python", "-m", "unittest", "-q"),)
            )

    async def test_a_stray_print_while_running_does_not_crash_the_app(self) -> None:
        """Textual routes every print() made while the app runs to App._print.

        KaroxApp once defined its own ``_print(title, body)``; the first print
        from any library during a session then raised TypeError and took the
        TUI down.
        """
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            print("stray output from a library")
            await pilot.pause()
            self.assertTrue(app.is_running)
            await self.submit(pilot, app, "/help")
            self.assertTrue(self.chat_widgets(app, TextBlock))

    async def test_help_prints_into_the_transcript(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "/help")
            self.assertEqual(len(app.screen_stack), 1)
            blocks = self.chat_widgets(app, TextBlock)
            self.assertEqual(len(blocks), 1)
            text = blocks[0].plain_text()
            self.assertIn("/connect", text)
            self.assertIn("/model", text)
            echoes = [item for item in self.chat_widgets(app, UserMessage) if item.command]
            self.assertEqual([item.text for item in echoes], ["/help"])

    async def test_slash_opens_the_palette_and_typing_filters_it(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            composer = app.query_one("#composer")
            composer.focus()
            await pilot.press("slash")
            await pilot.pause()
            menu = app.menu
            self.assertTrue(menu.active)
            self.assertEqual(menu.kind, "commands")
            self.assertEqual(len(menu.keys()), len(slash.COMMANDS))
            await pilot.press("m", "o")
            await pilot.pause()
            self.assertEqual(menu.keys()[0], "model")
            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(composer.text, "/model ")
            self.assertFalse(menu.active)
            composer.text = "/he"
            await pilot.pause()
            self.assertTrue(menu.active)
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(menu.active)
            self.assertFalse(app._agent_running)

    async def test_enter_on_the_palette_runs_the_highlighted_command(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            composer = app.query_one("#composer")
            composer.focus()
            composer.text = "/hel"
            await pilot.pause()
            await pilot.press("enter")
            await self.settle(pilot, app)
            self.assertEqual(composer.text, "")
            self.assertEqual(len(self.chat_widgets(app, TextBlock)), 1)
            self.assertIn("/help", composer.submitted_lines())

    async def test_model_picker_opens_inline_and_selects_with_keys(self) -> None:
        backend = _FakeBackend(self.repository)
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "/model")
            menu = app.menu
            self.assertTrue(menu.active)
            self.assertEqual(menu.kind, "model")
            self.assertEqual(len(app.screen_stack), 1)
            self.assertEqual(menu.highlighted_key(), "demo/demo-1")
            await pilot.press("down", "enter")
            await self.settle(pilot, app)
            self.assertEqual(backend.selected, [("demo", "demo-2")])
            self.assertFalse(menu.active)
            self.assertIn("demo/demo-2", self.transcript(app))

    async def test_picker_filter_is_never_sent_as_a_task(self) -> None:
        backend = _FakeBackend(self.repository)
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "/model")
            await pilot.press("d", "e", "m", "o", "-", "2")
            await pilot.pause()
            self.assertEqual(app.menu.keys(), ["demo/demo-2"])
            await pilot.press("z", "z", "z")
            await pilot.pause()
            self.assertEqual(app.menu.keys(), [])
            await pilot.press("enter")
            await self.settle(pilot, app)
            self.assertFalse(app._agent_running)
            self.assertFalse(
                [item for item in self.chat_widgets(app, UserMessage) if not item.command]
            )
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.menu.active)
            self.assertEqual(app.query_one("#composer").text, "")

    async def test_model_argument_selects_directly(self) -> None:
        backend = _FakeBackend(self.repository)
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "/model demo/demo-2")
            self.assertEqual(backend.selected, [("demo", "demo-2")])
            self.assertFalse(app.menu.active)

    async def test_connect_keeps_the_key_out_of_transcript_and_history(self) -> None:
        backend = _FakeBackend(self.repository)
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "/connect")
            menu = app.menu
            self.assertEqual(menu.kind, "connect")
            self.assertEqual(menu.keys()[-1], views.CUSTOM_PROVIDER_KEY)
            self.assertEqual(menu.highlighted_key(), "wandb")
            await pilot.press("enter")
            await pilot.pause()
            secret = app.query_one("#secret", SecretPrompt)
            composer = app.query_one("#composer")
            self.assertTrue(secret.display)
            self.assertTrue(secret.password)
            self.assertFalse(composer.display)
            self.assertIn("WANDB_API_KEY", secret.placeholder)
            for character in "sk-test-123":
                await pilot.press(character)
            await pilot.press("enter")
            await self.settle(pilot, app)
            self.assertEqual(len(backend.connect_calls), 1)
            self.assertEqual(backend.connect_calls[0]["catalog_id"], "wandb")
            self.assertEqual(backend.connect_calls[0]["api_key"], "sk-test-123")
            self.assertFalse(secret.display)
            self.assertTrue(composer.display)
            self.assertEqual(secret.value, "")
            transcript = self.transcript(app)
            self.assertNotIn("sk-test-123", transcript)
            self.assertNotIn("sk-test-123", " ".join(composer.submitted_lines()))
            self.assertIn("Weights & Biases", transcript)
            # Another model was active, so the new provider's models are offered.
            self.assertEqual(menu.kind, "model")
            self.assertEqual(menu.keys(), ["wandb/big-model", "wandb/small-model"])
            self.assertEqual(menu.highlighted_key(), "wandb/big-model")
            await pilot.press("enter")
            await self.settle(pilot, app)
            self.assertEqual(backend.selected, [("wandb", "big-model")])

    async def test_connect_by_id_goes_straight_to_the_key(self) -> None:
        backend = _FakeBackend(self.repository)
        backend.connect_activates = True
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "/connect deepseek")
            secret = app.query_one("#secret", SecretPrompt)
            self.assertTrue(secret.display)
            self.assertIn("DEEPSEEK_API_KEY", secret.placeholder)
            await pilot.press("escape")
            await self.settle(pilot, app)
            self.assertFalse(secret.display)
            self.assertEqual(backend.connect_calls, [])
            self.assertIn("Cancelled", self.transcript(app))
            # An empty key for a provider without a saved one is refused.
            await self.submit(pilot, app, "/connect deepseek")
            await pilot.press("enter")
            await self.settle(pilot, app)
            self.assertEqual(backend.connect_calls, [])
            # A provider that is already connected may keep its saved key.
            backend.catalog_rows[1]["connected_as"] = "deepseek"
            await self.submit(pilot, app, "/connect deepseek")
            await pilot.press("enter")
            await self.settle(pilot, app)
            self.assertEqual(len(backend.connect_calls), 1)
            self.assertEqual(backend.connect_calls[0]["api_key"], "")
            self.assertEqual(backend.connect_calls[0]["provider_id"], "deepseek")
            self.assertFalse(app.menu.active)

    async def test_endpoint_variables_are_asked_in_plain_text(self) -> None:
        backend = _FakeBackend(self.repository)
        backend.catalog_rows.append(
            {
                "provider_id": "cloudflare-workers-ai",
                "display_name": "Cloudflare Workers AI",
                "env": ["CLOUDFLARE_API_KEY"],
                "url_variables": ["CLOUDFLARE_ACCOUNT_ID"],
                "model_count": 5,
                "installable": True,
                "connected_as": None,
            }
        )
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            with patch.dict("os.environ", {"CLOUDFLARE_ACCOUNT_ID": ""}):
                await self.submit(pilot, app, "/connect cloudflare-workers-ai")
                await pilot.press("k", "e", "y", "enter")
                await pilot.pause()
                secret = app.query_one("#secret", SecretPrompt)
                self.assertTrue(secret.display)
                self.assertFalse(secret.password)
                self.assertIn("CLOUDFLARE_ACCOUNT_ID", secret.placeholder)
                await pilot.press("a", "c", "c", "1", "enter")
                await self.settle(pilot, app)
            self.assertEqual(
                backend.connect_calls[0]["variables"], (("CLOUDFLARE_ACCOUNT_ID", "acc1"),)
            )
            self.assertEqual(backend.connect_calls[0]["api_key"], "key")

    async def test_custom_endpoint_opens_the_form(self) -> None:
        from karox.tui.modals import ProviderSetupScreen

        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "/connect")
            for character in "custom":
                await pilot.press(character)
            await pilot.pause()
            self.assertEqual(app.menu.keys(), [views.CUSTOM_PROVIDER_KEY])
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, ProviderSetupScreen)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)

    async def test_answer_segments_follow_the_tool_lines(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "inspect the repo")
            kinds = [type(widget).__name__ for widget in app.query_one("#chat").children]
            start = kinds.index("UserMessage")
            self.assertEqual(
                kinds[start : start + 5],
                ["UserMessage", "AssistantMessage", "ToolCard", "AssistantMessage", "Notice"],
            )
            segments = self.chat_widgets(app, AssistantMessage)
            self.assertEqual([item.answer_text() for item in segments], ["Hello", " world"])
            self.assertEqual("".join(item.answer_text() for item in segments), "Hello world")
            self.assertEqual(app._tokens, 1234)
            self.assertTrue(app._continue)

    async def test_text_of_two_steps_is_never_glued(self) -> None:
        backend = _FakeBackend(self.repository, script=_rich_script())
        app = self.app(backend)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "do the whole thing")
            answers = [item.answer_text() for item in self.chat_widgets(app, AssistantMessage)]
            self.assertIn("working on it", answers)
            self.assertIn("done", answers)
            self.assertFalse(any("itdone" in item for item in answers))

    async def test_report_message_is_shown_only_when_nothing_streamed(self) -> None:
        outcome = Outcome(
            session_id="s-1",
            status="answered",
            reason="answer",
            steps=1,
            provider_message="Final answer from the report",
        )
        script = [
            Event(kind="step_started", step=1),
            Event(kind="tool_started", step=1, tool="repo.read_file", call_id="c"),
            Event(kind="tool_finished", step=1, tool="repo.read_file", call_id="c", ok=True),
        ]
        backend = _FakeBackend(self.repository, script=script, outcome=outcome)
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "answer me")
            answers = [item.answer_text() for item in self.chat_widgets(app, AssistantMessage)]
            self.assertEqual(answers, ["Final answer from the report"])
        streamed = _FakeBackend(self.repository)
        app = self.app(streamed)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "answer me")
            answers = [item.answer_text() for item in self.chat_widgets(app, AssistantMessage)]
            self.assertEqual(answers, ["Hello", " world"])

    async def test_tool_line_expands_in_place_on_click(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "inspect the repo")
            card = self.chat_widgets(app, ToolCard)[0]
            self.assertFalse(card.expanded)
            await pilot.click(ToolCard)
            await pilot.pause()
            self.assertTrue(card.expanded)
            self.assertEqual(len(app.screen_stack), 1)
            self.assertIn("call-1", str(card.render()))

    async def test_unknown_command_is_a_notice_not_a_run(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "/nope")
            notices = self.chat_widgets(app, Notice)
            self.assertEqual(len(notices), 1)
            self.assertIn("/nope", notices[0].text)
            self.assertFalse(app._agent_running)

    async def test_stop_before_any_run_is_harmless(self) -> None:
        backend = _FakeBackend(self.repository)
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            app.action_stop()
            self.assertEqual(backend.stops, 0)

    async def test_ctrl_c_clears_the_draft_before_it_quits(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            composer = app.query_one("#composer")
            composer.focus()
            await pilot.press("a", "b", "c")
            await pilot.press("ctrl+c")
            await pilot.pause()
            self.assertEqual(composer.text, "")
            self.assertTrue(app.is_running)

    async def test_run_exposes_state_counters(self) -> None:
        backend = _FakeBackend(self.repository, script=_rich_script())
        app = self.app(backend)
        async with app.run_test(size=(110, 32)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "do the whole thing")
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
            self.assertAlmostEqual(state.context_fraction or 0.0, 90000 / 128000, places=3)
            cards = self.chat_widgets(app, ToolCard)
            self.assertEqual(len(cards), 5)
            categories = {card.tool: card.category for card in cards}
            self.assertEqual(categories["mcp.github.list_issues"], "mcp")
            self.assertEqual(categories["skill.load"], "skill")
            self.assertEqual(categories["repo.inspect"], "inspect")
            await self.submit(pilot, app, "/status")
            status = self.chat_widgets(app, TextBlock)[-1].plain_text()
            self.assertIn("demo/demo-1", status)
            self.assertIn(app.session_id, status)
            self.assertIn("reads 1", status)

    async def test_context_note_appears_near_the_window_limit(self) -> None:
        script = [
            Event(kind="step_started", step=1),
            Event(kind="step_finished", step=1, usage={"prompt_tokens": 120000, "total_tokens": 500}),
            Event(kind="text_delta", step=1, text="tight"),
        ]
        backend = _FakeBackend(self.repository, script=script)
        app = self.app(backend)
        async with app.run_test(size=(120, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "nearly full context")
            self.assertGreaterEqual(app.run_state.context_fraction or 0.0, 0.9)
            self.assertIn("ctx 93%", app.query_one(StatusLine).current_text())

    async def test_checkpoint_state_refreshes_after_a_run(self) -> None:
        record = _session_record()
        backend = _FakeBackend(self.repository, record_after_run=record)
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            self.assertIsNone(app.checkpoint)
            await self.submit(pilot, app, "run and save")
            self.assertIsNotNone(app.checkpoint)
            assert app.checkpoint is not None
            self.assertEqual(app.checkpoint.checkpoint_count, 1)
            self.assertEqual(app.checkpoint.check_tally, (1, 2))

    async def test_session_command_prints_the_record_or_its_empty_state(self) -> None:
        record = _session_record()
        backend = _FakeBackend(self.repository, record=record)
        app = self.app(backend)
        async with app.run_test(size=(110, 34)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "/session")
            self.assertEqual(len(app.screen_stack), 1)
            body = self.chat_widgets(app, TextBlock)[-1].plain_text()
            self.assertIn("make it work", body)
            self.assertIn("threejs_scene", body)
            self.assertIn("github", body)

        empty_app = self.app(_FakeBackend(self.repository))
        async with empty_app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, empty_app)
            await self.submit(pilot, empty_app, "/session")
            body = self.chat_widgets(empty_app, TextBlock)[-1].plain_text()
            self.assertIn("No durable session record yet", body)

    async def test_sessions_picker_resumes_one(self) -> None:
        record = _session_record()
        record["repository"] = str(self.repository)
        backend = _FakeBackend(self.repository, record=record)
        backend.session_rows = [
            {
                "session_id": "tui-test-1",
                "repository": str(self.repository),
                "task": "make it work",
                "status": "active",
                "updated_at": 1761382800.0,
            }
        ]
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "/sessions")
            self.assertEqual(app.menu.kind, "session")
            self.assertEqual(app.menu.keys(), ["tui-test-1"])
            await pilot.press("enter")
            await self.settle(pilot, app)
            self.assertEqual(app.session_id, "tui-test-1")
            self.assertTrue(app._continue)
            self.assertIn("make it work", self.transcript(app))

    async def test_skills_picker_then_detail_block(self) -> None:
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
            await self.settle(pilot, app)
            await self.submit(pilot, app, "/skills")
            self.assertEqual(app.menu.kind, "skills")
            self.assertEqual(app.menu.keys(), ["alpha", "beta"])
            await pilot.press("enter")
            await self.settle(pilot, app)
            self.assertIn("use it wisely", self.chat_widgets(app, TextBlock)[-1].plain_text())
            self.assertEqual(len(app.screen_stack), 1)

        empty_app = self.app(_FakeBackend(self.repository))
        async with empty_app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, empty_app)
            await self.submit(pilot, empty_app, "/skills")
            notices = self.chat_widgets(empty_app, Notice)
            self.assertTrue(any(notice.level == "warning" for notice in notices))

    async def test_mcp_list_actions_and_add_form(self) -> None:
        from karox.tui.modals import McpAddScreen

        backend = _FakeBackend(self.repository)
        backend.servers = [
            {
                "server_id": "github",
                "transport": "stdio",
                "selection": {"state": "not_selected", "counts": {"allowed": 0}},
                "liveness": {"state": "not_probed"},
            }
        ]
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "/mcp")
            self.assertEqual(app.menu.keys(), ["github", views.ADD_SERVER_KEY])
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.menu.kind, "mcp-action")
            self.assertEqual(app.menu.keys(), ["probe", "select", "select_read", "remove"])
            await pilot.press("enter")
            await self.settle(pilot, app)
            self.assertEqual(backend.probes, ["github"])
            self.assertIn("4 tools", self.transcript(app))
            await self.submit(pilot, app, "/mcp")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("down", "down", "down", "enter")
            await pilot.pause()
            self.assertEqual(app.menu.kind, "mcp-confirm")
            self.assertEqual(app.menu.highlighted_key(), "no")
            await pilot.press("down", "enter")
            await self.settle(pilot, app)
            self.assertEqual(backend.removed, ["github"])
            await self.submit(pilot, app, "/mcp add")
            self.assertIsInstance(app.screen, McpAddScreen)
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(len(app.screen_stack), 1)

    async def test_effort_mode_and_language_pickers(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "/effort")
            self.assertEqual(app.menu.keys(), list(app_module.EFFORT_LEVELS))
            for character in "high":
                await pilot.press(character)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.effort, "high")
            await self.submit(pilot, app, "/mode plan")
            self.assertEqual(app.mode, "plan")
            facts = app.query_one(StatusLine).current_text()
            self.assertIn("plan", facts)
            self.assertIn("effort high", facts)
            await self.submit(pilot, app, "/mode nonsense")
            self.assertEqual(app.mode, "plan")
            await self.submit(pilot, app, "/lang ru")
            self.assertEqual(app.language, "ru")
            self.assertIn("Опишите задачу", app.query_one("#composer").placeholder)

    async def test_backend_failure_is_an_error_state_not_a_crash(self) -> None:
        backend = _FakeBackend(self.repository, fail_stream=True)
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "this will fail")
            self.assertEqual(app.run_state.state, "failed")
            self.assertFalse(app._agent_running)
            notices = self.chat_widgets(app, Notice)
            self.assertTrue(any(notice.level == "error" for notice in notices))
            self.assertTrue(any("provider exploded" in notice.text for notice in notices))

    async def test_result_notice_is_compact_and_carries_tally_and_cost(self) -> None:
        outcome = Outcome(
            session_id="s-x",
            status="failed",
            verified=False,
            reason="check_failed",
            steps=2,
            changed_files=("a.py",),
            checks=({"ok": True}, {"ok": False}),
            usage={"total_tokens": 99, "costs": {"USD": 0.0123}},
        )
        backend = _FakeBackend(self.repository, outcome=outcome)
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "break things")
            result = self.chat_widgets(app, Notice)[-1].text
            first = result.splitlines()[0]
            self.assertIn("1/2", first)
            self.assertIn("$0.01", first)
            self.assertIn("check_failed", result)
            self.assertEqual(app.run_state.state, "failed")

    async def test_narrow_terminal_keeps_a_one_line_status(self) -> None:
        app = self.app()
        async with app.run_test(size=(48, 16)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "narrow run")
            answers = "".join(
                item.answer_text() for item in self.chat_widgets(app, AssistantMessage)
            )
            self.assertEqual(answers, "Hello world")
            status = app.query_one(StatusLine)
            self.assertEqual(status.size.height, 1)
            self.assertIn("demo/demo-1", status.current_text())
            self.assertNotIn("\n", status.current_text())

    async def test_composer_recalls_lines_without_reopening_the_palette(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            composer = app.query_one("#composer")
            await self.submit(pilot, app, "first task")
            await self.submit(pilot, app, "/status")
            composer.focus()
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(composer.text, "/status")
            self.assertFalse(app.menu.active)
            await pilot.press("up")
            await pilot.pause()
            self.assertEqual(composer.text, "first task")
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(composer.text, "/status")

    async def test_pageup_scrolls_the_transcript_from_the_composer(self) -> None:
        app = self.app()
        async with app.run_test(size=(90, 20)) as pilot:
            await self.settle(pilot, app)
            for index in range(60):
                app._notice(f"line {index}", "info")
            await pilot.pause()
            chat = app.query_one("#chat")
            chat.scroll_end(animate=False)
            await pilot.pause()
            before = chat.scroll_y
            self.assertGreater(before, 0)
            app.query_one("#composer").focus()
            await pilot.press("pageup")
            await pilot.pause()
            self.assertLess(chat.scroll_y, before)
            self.assertIs(app.focused, app.query_one("#composer"))

    async def test_russian_screen_has_no_english_leftovers(self) -> None:
        app = self.app(language="ru")
        async with app.run_test(size=(110, 34)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "/help")
            await self.submit(pilot, app, "/status")
            composer = app.query_one("#composer")
            composer.text = "/"
            await pilot.pause()
            palette = " ".join(str(option.prompt) for option in app.menu.query_one("#menu-options").options)
            app.menu.close()
            screen = "\n".join(
                [
                    self.transcript(app),
                    palette,
                    app.query_one(StatusLine).current_text(),
                    composer.placeholder,
                ]
            )
            for leftover in ("selected", "F1", "F2", "ChatGPT", "Claude", "HyperAgent", "Press ", "Unknown"):
                self.assertNotIn(leftover, screen)
            self.assertIn("Команды", screen)
            self.assertIn("подключить провайдера", screen)

    # ------------------------------------------------ live run, queue, keys

    async def wait_for(self, pilot, condition, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and not condition():
            await pilot.pause(0.02)
        self.assertTrue(condition(), "condition not reached in time")
        await pilot.pause()
        await pilot.pause()

    async def type_and_send(self, pilot, app: KaroxApp, text: str) -> None:
        composer = app.query_one("#composer")
        composer.focus()
        composer.text = text
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()

    def working_sub(self, app: KaroxApp) -> str:
        return str(app.query_one("#working-sub", Static).render())

    async def test_working_line_says_what_the_agent_is_doing(self) -> None:
        script = [
            Event(kind="step_started", step=1),
            Event(
                kind="tool_started",
                step=1,
                tool="repo.read_file",
                call_id="c1",
                detail="src/app.py",
            ),
            Event(
                kind="tool_finished",
                step=1,
                tool="repo.read_file",
                call_id="c1",
                ok=True,
                summary="src/app.py, lines=40",
                duration_seconds=0.1,
            ),
            Event(kind="text_delta", step=1, text="done"),
        ]
        backend = _FakeBackend(self.repository, script=script)
        backend.gate = threading.Event()
        backend.gate_after = 1
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            self.assertFalse(app.working.display)
            await self.type_and_send(pilot, app, "read it")
            await self.wait_for(pilot, backend.gate_reached.is_set)
            self.assertTrue(app.working.display)
            line = app.working.current_text()
            self.assertIn("Reading src/app.py…", line)
            self.assertIn("esc to interrupt", line)
            card = self.chat_widgets(app, ToolCard)[0]
            self.assertIsNone(card.ok)
            self.assertIn("Read(src/app.py)", str(card.render()))
            backend.gate.set()
            await self.settle(pilot, app)
            self.assertFalse(app.working.display)
            self.assertTrue(card.ok)
            self.assertIn("Read 40 lines", str(card.render()))

    async def test_a_task_typed_while_running_runs_next(self) -> None:
        backend = _FakeBackend(self.repository)
        backend.gate = threading.Event()
        backend.gate_after = 2
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.type_and_send(pilot, app, "first task")
            await self.wait_for(pilot, backend.gate_reached.is_set)
            await self.type_and_send(pilot, app, "second task")
            self.assertEqual(app._queue, ["second task"])
            self.assertIn("Queued: second task", self.working_sub(app))
            backend.gate.set()
            await self.wait_for(pilot, lambda: len(backend.requests) == 2)
            await self.settle(pilot, app)
            self.assertEqual([request.task for request in backend.requests], ["first task", "second task"])
            users = [item.text for item in self.chat_widgets(app, UserMessage) if not item.command]
            self.assertEqual(users, ["first task", "second task"])
            self.assertEqual(app._queue, [])
            self.assertFalse(app._agent_running)

    async def test_stop_hands_type_ahead_back_and_settles_running_lines(self) -> None:
        backend = _FakeBackend(self.repository)
        backend.gate = threading.Event()
        backend.gate_after = 2
        app = self.app(backend)
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.type_and_send(pilot, app, "first task")
            await self.wait_for(pilot, backend.gate_reached.is_set)
            await self.type_and_send(pilot, app, "do this after")
            await pilot.press("escape")
            await self.settle(pilot, app)
            self.assertEqual(backend.stops, 1)
            self.assertEqual(app.run_state.state, "stopped")
            self.assertEqual(app.query_one("#composer").text, "do this after")
            self.assertEqual(app._queue, [])
            self.assertEqual(len(backend.requests), 1)
            card = self.chat_widgets(app, ToolCard)[0]
            self.assertIsNone(card.ok)
            self.assertIn("Interrupted", str(card.render()))

    async def test_a_refused_call_shows_its_reason_on_its_own_line(self) -> None:
        script = [
            Event(kind="step_started", step=1),
            Event(
                kind="tool_started",
                step=1,
                tool="mcp.github.create_issue",
                call_id="m1",
                detail="title: Bug",
            ),
            Event(
                kind="error",
                step=1,
                tool="mcp.github.create_issue",
                call_id="m1",
                reason="McpRemoteToolError",
                detail="MCP tool mcp.github.create_issue returned an error result: repository not found",
            ),
            Event(
                kind="tool_finished",
                step=1,
                tool="mcp.github.create_issue",
                call_id="m1",
                ok=False,
                summary="McpRemoteToolError",
            ),
            Event(kind="text_delta", step=1, text="could not create it"),
        ]
        app = self.app(_FakeBackend(self.repository, script=script))
        async with app.run_test(size=(110, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "file the bug")
            card = self.chat_widgets(app, ToolCard)[0]
            self.assertFalse(card.ok)
            rendered = str(card.render())
            self.assertIn("github · create_issue (MCP)(title: Bug)", rendered)
            self.assertIn("repository not found", rendered)
            self.assertFalse(
                any(notice.level == "error" for notice in self.chat_widgets(app, Notice))
            )

    async def test_retries_stay_on_the_working_line_and_warnings_read_as_words(self) -> None:
        script = [
            Event(kind="step_started", step=1),
            Event(
                kind="warning",
                step=1,
                reason="provider_retry",
                detail="model call failed (rate_limit, HTTP 429); retry 1 in 2.0s",
            ),
            Event(
                kind="warning",
                step=1,
                reason="engine_repair_prompt",
                detail="verification failed; one bounded repair turn follows",
            ),
            Event(kind="text_delta", step=1, text="fixed"),
        ]
        backend = _FakeBackend(self.repository, script=script)
        backend.gate = threading.Event()
        backend.gate_after = 1
        app = self.app(backend)
        async with app.run_test(size=(110, 30)) as pilot:
            await self.settle(pilot, app)
            await self.type_and_send(pilot, app, "go")
            await self.wait_for(pilot, backend.gate_reached.is_set)
            self.assertIn("Retrying the model call", self.working_sub(app))
            backend.gate.set()
            await self.settle(pilot, app)
            texts = [notice.text for notice in self.chat_widgets(app, Notice)]
            self.assertFalse(any("rate_limit" in text for text in texts))
            self.assertIn("A check failed — the agent gets one more turn to fix it.", texts)

    async def test_shift_tab_cycles_the_mode_without_a_transcript_line(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            before = len(self.chat_widgets(app, Notice))
            app.query_one("#composer").focus()
            await pilot.press("shift+tab")
            await pilot.pause()
            self.assertEqual(app.mode, "plan")
            self.assertIn("plan mode", app.query_one(StatusLine).current_text())
            await pilot.press("shift+tab", "shift+tab")
            await pilot.pause()
            self.assertEqual(app.mode, "build")
            self.assertIn("? for shortcuts", app.query_one(StatusLine).current_text())
            self.assertEqual(len(self.chat_widgets(app, Notice)), before)
            self.assertIs(app.focused, app.query_one("#composer"))

    async def test_ctrl_o_expands_every_tool_line(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "inspect the repo")
            card = self.chat_widgets(app, ToolCard)[0]
            self.assertFalse(card.expanded)
            await pilot.press("ctrl+o")
            await pilot.pause()
            self.assertTrue(card.expanded)
            self.assertIn("verbose", app.query_one(StatusLine).current_text())
            await self.submit(pilot, app, "again")
            cards = self.chat_widgets(app, ToolCard)
            self.assertEqual(len(cards), 2)
            self.assertTrue(all(item.expanded for item in cards))
            await pilot.press("ctrl+o")
            await pilot.pause()
            self.assertFalse(any(item.expanded for item in cards))

    async def test_question_mark_on_an_empty_composer_shows_shortcuts(self) -> None:
        app = self.app()
        async with app.run_test(size=(100, 30)) as pilot:
            await self.settle(pilot, app)
            composer = app.query_one("#composer")
            composer.focus()
            panel = app.query_one("#shortcuts", Static)
            await pilot.press("question_mark")
            await pilot.pause()
            self.assertTrue(panel.display)
            self.assertIn("ctrl+o", str(panel.render()))
            self.assertEqual(composer.text, "")
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(panel.display)
            await pilot.press("a", "question_mark")
            await pilot.pause()
            self.assertEqual(composer.text, "a?")
            self.assertFalse(panel.display)

    async def test_mcp_chosen_before_the_first_run_joins_with_it(self) -> None:
        backend = _FakeBackend(self.repository, record_after_run=_session_record())
        backend.servers = [
            {
                "server_id": "github",
                "transport": "stdio",
                "selection": {"state": "not_selected", "counts": {"allowed": 0}},
                "liveness": {"state": "not_probed"},
            }
        ]
        app = self.app(backend)
        async with app.run_test(size=(110, 30)) as pilot:
            await self.settle(pilot, app)
            await self.submit(pilot, app, "/mcp")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("down", "enter")
            await self.settle(pilot, app)
            self.assertEqual(app._pending_mcp, {"github": "all"})
            self.assertEqual(backend.mcp_selections, [])
            self.assertIn("with the first task (2 tools)", self.transcript(app))
            await self.submit(pilot, app, "/mcp")
            rows = " ".join(
                str(option.prompt) for option in app.menu.query_one("#menu-options").options
            )
            self.assertIn("joins with the first task", rows)
            app.menu.close()
            await self.submit(pilot, app, "use the issues")
            self.assertEqual(backend.requests[-1].mcp_servers, (("github", "all"),))
            self.assertEqual(app._pending_mcp, {})
            # The session exists now: a new choice is stored in it directly.
            await self.submit(pilot, app, "/mcp")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("down", "down", "enter")
            await self.settle(pilot, app)
            self.assertEqual(backend.mcp_selections, [("github", app.session_id, "read")])
            self.assertIn("tools allowed: 1", self.transcript(app))
            await self.submit(pilot, app, "next task")
            self.assertEqual(backend.requests[-1].mcp_servers, ())
