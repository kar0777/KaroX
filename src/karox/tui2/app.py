"""The compact KaroX terminal application.

The screen is four rows of chrome around one conversation. Everything else is a
modal that returns a value: a model id, a session id, a saved provider, a notice.
The app never imports the agent runtime and never parses human progress text, so
the whole client can be exercised against a recorded stream.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.theme import Theme

from ..detached_process import windowless_flags
from . import prefs
from .backend import AgentRequest, BackendError, CLIBackend
from .i18n import detect_language, normalise_language, tr
from .modals import (
    HelpScreen,
    ListScreen,
    McpScreen,
    ProviderSetupScreen,
    TextViewerScreen,
)
from .protocol import Event, Outcome
from .widgets import (
    ActivityBar,
    AssistantMessage,
    ChatInput,
    ChatLog,
    HeaderBar,
    Notice,
    StatusBar,
    ToolCard,
    UserMessage,
    WelcomeCard,
)

_EFFORT_LEVELS = ("auto", "low", "medium", "high", "extra-high", "ultra")
_MODES = ("build", "plan", "ideate")


def run_tui(
    *,
    session_id: Optional[str] = None,
    repository: Optional[str] = None,
    language: Optional[str] = None,
) -> int:
    path = Path(repository or os.getcwd()).expanduser()
    if not path.is_dir():
        print(f"karox: repository is not a directory: {path}", file=sys.stderr)
        return 2
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("karox: the compact TUI needs an interactive terminal", file=sys.stderr)
        return 2
    result = KaroxApp(path, session_id=session_id, language=language).run()
    return int(result or 0)


class KaroxApp(App[int]):
    TITLE = "KaroX"
    CSS_PATH = "theme.tcss"
    BINDINGS = [
        Binding("f1", "help", "Help"),
        Binding("f2", "models", "Models"),
        Binding("f3", "sessions", "Sessions"),
        Binding("f4", "mcp", "MCP"),
        Binding("ctrl+n", "new_session", "New session", show=False),
        Binding("ctrl+r", "show_reasoning", "Reasoning", show=False),
        Binding("ctrl+l", "clear", "Clear", show=False),
        Binding("ctrl+q", "quit", "Quit", show=False, priority=True),
        Binding("ctrl+c", "interrupt", "Stop / quit", show=False, priority=True),
        Binding("escape", "stop", "Stop", show=False),
    ]

    def __init__(
        self,
        repository: Path,
        *,
        session_id: Optional[str] = None,
        language: Optional[str] = None,
        backend: Optional[CLIBackend] = None,
    ) -> None:
        super().__init__()
        self.repository = repository.expanduser().resolve()
        self.backend = backend or CLIBackend(self.repository)
        stored = prefs.load_preferences()
        self.language = (
            normalise_language(language)
            or normalise_language(stored.get("language"))
            or detect_language()
        )
        self.session_id = session_id or self._new_session_id()
        self._continue = False
        self._model: dict[str, Any] | None = None
        self._branch = ""
        self._tokens = 0
        self._agent_running = False
        self._stopping = False
        self._started_at = 0.0
        self._verification: tuple[tuple[str, ...], ...] = _detect_verification(
            self.repository
        )
        effort = stored.get("effort_level")
        self.effort = effort if effort in _EFFORT_LEVELS else "auto"
        mode = stored.get("agent_mode")
        self.mode = mode if mode in _MODES else "build"
        self._cards: dict[str, ToolCard] = {}
        self._last_assistant: AssistantMessage | None = None
        self.register_theme(
            Theme(
                name="karox",
                primary="#d6b36a",
                secondary="#8d96a5",
                accent="#d6b36a",
                warning="#d6b36a",
                error="#d97b7b",
                success="#7fb08a",
                foreground="#d8dce3",
                background="#0f1115",
                surface="#161a20",
                panel="#1c2129",
                dark=True,
            )
        )
        self.theme = "karox"

    def compose(self) -> ComposeResult:
        yield HeaderBar(id="header")
        yield ChatLog(id="chat")
        yield ActivityBar(id="activity")
        yield ChatInput(
            id="composer",
            placeholder=tr(self.language, "composer.placeholder"),
            soft_wrap=True,
        )
        yield StatusBar(id="status")

    # --------------------------------------------------------------- lifecycle

    def on_mount(self) -> None:
        self.query_one(ActivityBar).display = False
        self.query_one(StatusBar).set_hint(tr(self.language, "status.hint"))
        self.query_one("#composer", ChatInput).focus()
        self._branch = _git_branch(self.repository)
        self._verify_session_repository()
        self._refresh_model()
        self._welcome()
        self._refresh_chrome()
        self._refresh_status()
        self.set_interval(1.0, self._tick)

    def _tick(self) -> None:
        if self._agent_running:
            self._refresh_status()

    def _verify_session_repository(self) -> None:
        if not self.session_id:
            return
        record = self.backend.session(self.session_id)
        if record is None:
            return
        repository = record.get("repository")
        if isinstance(repository, str) and repository:
            try:
                same = os.path.normcase(str(Path(repository).resolve())) == os.path.normcase(
                    str(self.repository)
                )
            except OSError:
                same = False
            if not same:
                notice = tr(
                    self.language, "notice.session_repo", repository=repository
                )
                self.session_id = self._new_session_id()
                self._continue = False
                self._notice(notice, "warning")
                return
        self._continue = True

    def _welcome(self) -> None:
        chat = self.query_one(ChatLog)
        chat.add(
            WelcomeCard(
                tr(self.language, "welcome.title"),
                tr(self.language, "welcome.body"),
            )
        )

    # ------------------------------------------------------------------ input

    @on(ChatInput.Submitted)
    def submitted(self, event: ChatInput.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        if value.startswith("/"):
            self._command(value)
        else:
            self._start_task(value)

    def _command(self, value: str) -> None:
        head, _, rest = value[1:].partition(" ")
        command = head.strip().lower()
        argument = rest.strip()
        if command in {"help", "?"}:
            self.action_help()
        elif command == "provider":
            self.action_provider()
        elif command == "model":
            self.action_models()
        elif command in {"sessions", "resume"}:
            if argument:
                self._resume(argument)
            else:
                self.action_sessions()
        elif command in {"new", "clear-session"}:
            self.action_new_session()
        elif command == "mcp":
            self.action_mcp()
        elif command == "verify":
            self._add_verification(argument)
        elif command == "unverify":
            self._verification = ()
            self._notice(tr(self.language, "notice.verify_cleared"), "info")
        elif command == "effort":
            self._set_effort(argument)
        elif command == "mode":
            self._set_mode(argument)
        elif command == "lang":
            self._set_language(argument)
        elif command == "status":
            self._notice(self._status_text(), "info")
        elif command == "clear":
            self.action_clear()
        elif command == "stop":
            self.action_stop()
        elif command in {"quit", "exit"}:
            if self._agent_running:
                self.backend.stop()
            self.exit(0)
        else:
            self._notice(f"Unknown command: /{command}. Press F1 for help.", "warning")

    def _start_task(self, task: str) -> None:
        if self._agent_running:
            self._notice(tr(self.language, "notice.already_running"), "warning")
            return
        if self._model is None:
            self._refresh_model()
        if self._model is None:
            self._notice(tr(self.language, "notice.no_model"), "warning")
            return
        chat = self.query_one(ChatLog)
        chat.add(UserMessage(task))
        assistant = chat.add(AssistantMessage())
        self._last_assistant = assistant
        self._cards.clear()
        self._agent_running = True
        self._stopping = False
        self._started_at = time.monotonic()
        self.query_one(ActivityBar).set_activity(tr(self.language, "status.running"))
        self._refresh_status()
        self._refresh_chrome()
        # Let the user message and the answer card finish mounting before the
        # first event can arrive. Without this the run can complete in the same
        # tick and its stream would paint into widgets that are still queued.
        self.call_after_refresh(self._launch_run, task, assistant)

    def _launch_run(self, task: str, assistant: AssistantMessage) -> None:
        self.run_worker(
            lambda: self._stream(task, assistant),
            thread=True,
            exclusive=True,
            name="agent-run",
        )

    def _stream(self, task: str, assistant: AssistantMessage) -> None:
        request = AgentRequest(
            repository=self.repository,
            task=task,
            session_id=self.session_id,
            continue_task=self._continue,
            verification=self._verification,
            effort=None if self.effort == "auto" else self.effort,
            mode=None if self.mode == "build" else self.mode,
        )

        def on_event(event: Event) -> None:
            try:
                self.call_from_thread(self._apply_event, event)
            except RuntimeError:
                pass

        try:
            result = self.backend.stream_agent(request, on_event)
        except Exception as exc:  # noqa: BLE001 - a UI must survive any bridge failure
            self.call_from_thread(self._finish, assistant, None, -1, str(exc), False)
            return
        self.call_from_thread(
            self._finish,
            assistant,
            result.outcome,
            result.exit_code,
            result.stderr,
            result.stopped,
        )

    def _apply_event(self, event: Event) -> None:
        assistant = self._last_assistant
        activity = self.query_one(ActivityBar)
        if event.kind == "text_delta":
            if assistant is not None:
                assistant.append_text(event.text)
        elif event.kind == "reasoning_delta":
            if assistant is not None:
                assistant.append_reasoning(event.reasoning)
        elif event.kind == "reasoning_summary_delta":
            if assistant is not None:
                assistant.append_reasoning(event.reasoning_summary, summary=True)
        elif event.kind == "step_started":
            activity.set_activity(f"step {event.step} · waiting for the model")
        elif event.kind == "tool_started":
            card = ToolCard(event.tool, event.call_id)
            self.query_one(ChatLog).add(card)
            if event.call_id:
                self._cards[event.call_id] = card
            activity.set_activity(f"{event.tool} · running")
        elif event.kind == "tool_finished":
            finished = self._cards.get(event.call_id)
            if finished is None:
                finished = ToolCard(event.tool, event.call_id)
                self.query_one(ChatLog).add(finished)
                if event.call_id:
                    self._cards[event.call_id] = finished
            finished.finish(event.ok, event.summary, event.duration_seconds)
            activity.set_activity(
                f"{event.tool} · {'ok' if event.ok else 'failed'}",
                "success" if event.ok else "error",
            )
        elif event.kind == "phase_changed":
            activity.set_activity(f"phase · {event.phase}")
        elif event.kind == "compacted":
            self._notice(f"context compacted · {event.summary}", "info")
            activity.set_activity("context compacted")
        elif event.kind == "warning":
            activity.set_activity(
                f"{event.reason}: {event.detail}".strip(), "warning"
            )
        elif event.kind == "error":
            self._notice(
                f"{event.reason}: {event.detail}".strip(": "), "error"
            )
            activity.set_activity(event.reason or "error", "error")
        elif event.kind == "file_edited":
            activity.set_activity(f"edited · {event.path}")
        elif event.kind == "file_read":
            activity.set_activity(f"read · {event.path}")
        elif event.kind == "test_started":
            activity.set_activity(f"test · {event.tool} started")
        elif event.kind == "test_finished":
            activity.set_activity(
                f"test · {event.tool} {'ok' if event.ok else 'failed'}",
                "success" if event.ok else "error",
            )
        elif event.kind == "artifact_created":
            activity.set_activity(f"artifact · {event.detail or event.path}")
        elif event.kind == "finished":
            activity.set_activity(f"{event.status} · {event.reason}", "success")
        usage = event.usage.get("total_tokens")
        if isinstance(usage, int) and usage > 0:
            self._tokens = usage
            self._refresh_chrome()

    def _finish(
        self,
        assistant: AssistantMessage,
        outcome: Outcome | None,
        exit_code: int,
        stderr: str,
        stopped: bool,
    ) -> None:
        self._agent_running = False
        self._stopping = False
        self.query_one(ActivityBar).set_activity("")
        elapsed = max(0.0, time.monotonic() - self._started_at)
        fallback = outcome.provider_message if outcome is not None else ""
        assistant.finish(fallback)
        if self._last_assistant is None:
            self._last_assistant = assistant
        if stopped:
            self._notice(tr(self.language, "notice.stopped"), "warning")
        elif outcome is not None:
            if outcome.session_id:
                self.session_id = outcome.session_id
            self._tokens = outcome.token_total or self._tokens
            self._notice(self._result_text(outcome, elapsed), self._result_level(outcome))
            self._continue = True
        else:
            detail = stderr.strip().splitlines()[-1] if stderr.strip() else f"exit code {exit_code}"
            self._notice(tr(self.language, "notice.backend_error", error=detail), "error")
        if not assistant.answer_text().strip():
            assistant.remove()
            if self._last_assistant is assistant:
                self._last_assistant = None
        self._refresh_status()
        self._refresh_chrome()
        self.query_one("#composer", ChatInput).focus()

    def _result_text(self, outcome: Outcome, elapsed: float) -> str:
        if outcome.verified:
            head = tr(self.language, "result.verified")
        elif outcome.reason == "answer":
            head = tr(self.language, "result.answer")
        else:
            head = tr(self.language, "result.failed")
        parts = [
            head,
            tr(self.language, "result.steps", count=outcome.steps),
            tr(self.language, "result.files", count=len(outcome.changed_files)),
        ]
        passed, total = outcome.check_tally
        if total:
            parts.append(tr(self.language, "result.checks", passed=passed, total=total))
        tokens = outcome.token_total
        if tokens:
            parts.append(tr(self.language, "result.tokens", count=_token_text(tokens)))
        if elapsed:
            parts.append(tr(self.language, "result.duration", seconds=elapsed))
        lines = [" · ".join(parts)]
        if not outcome.verified and outcome.reason:
            lines.append(tr(self.language, "result.reason", reason=outcome.reason))
        if outcome.changed_files:
            shown = ", ".join(outcome.changed_files[:6])
            if len(outcome.changed_files) > 6:
                shown += f" (+{len(outcome.changed_files) - 6})"
            lines.append(tr(self.language, "result.changed", files=shown))
        return "\n".join(lines)

    @staticmethod
    def _result_level(outcome: Outcome) -> str:
        if outcome.verified or outcome.reason == "answer":
            return "success"
        if outcome.status in {"failed", "error"}:
            return "error"
        return "warning"

    # --------------------------------------------------------------- commands

    def _add_verification(self, argument: str) -> None:
        if not argument:
            current = (
                "\n".join(" ".join(command) for command in self._verification)
                or "(none)"
            )
            self._notice(current, "info")
            return
        try:
            command = tuple(shlex.split(argument))
        except ValueError as exc:
            self._notice(f"cannot parse command: {exc}", "error")
            return
        if not command:
            return
        self._verification = (*self._verification, command)
        self._notice(
            tr(self.language, "notice.verify_added", command=" ".join(command)), "info"
        )

    def _set_effort(self, argument: str) -> None:
        if not argument:
            self._notice(tr(self.language, "notice.effort", level=self.effort), "info")
            return
        if argument not in _EFFORT_LEVELS:
            self._notice(
                "effort must be one of: " + ", ".join(_EFFORT_LEVELS), "error"
            )
            return
        self.effort = argument
        _save_preference(effort_level=argument)
        self._notice(tr(self.language, "notice.effort", level=argument), "info")

    def _set_mode(self, argument: str) -> None:
        if not argument:
            self._notice(tr(self.language, "notice.mode", mode=self.mode), "info")
            return
        if argument not in _MODES:
            self._notice("mode must be one of: " + ", ".join(_MODES), "error")
            return
        self.mode = argument
        _save_preference(agent_mode=argument)
        self._notice(tr(self.language, "notice.mode", mode=argument), "info")

    def _set_language(self, argument: str) -> None:
        selected = normalise_language(argument)
        if selected is None:
            self._notice("language must be ru or en", "error")
            return
        self.language = selected
        _save_preference(language=selected)
        self.query_one("#composer", ChatInput).placeholder = tr(
            self.language, "composer.placeholder"
        )
        self.query_one(StatusBar).set_hint(tr(self.language, "status.hint"))
        self._refresh_status()
        self._notice(tr(self.language, "notice.language", language=selected), "info")

    def _status_text(self) -> str:
        model = self._model_label() or tr(self.language, "status.no_model")
        checks = "; ".join(" ".join(item) for item in self._verification) or "(none)"
        return "\n".join(
            [
                f"repository: {self.repository}",
                f"branch: {self._branch or '-'}",
                f"model: {model}",
                f"session: {self.session_id}",
                f"effort: {self.effort} · mode: {self.mode}",
                f"tokens: {self._tokens}",
                f"verification: {checks}",
            ]
        )

    def _resume(self, session_id: str) -> None:
        if not self._idle():
            return
        record = self.backend.session(session_id)
        if record is None:
            self._notice(f"session not found: {session_id}", "error")
            return
        repository = record.get("repository")
        try:
            same = isinstance(repository, str) and os.path.normcase(
                str(Path(repository).resolve())
            ) == os.path.normcase(str(self.repository))
        except OSError:
            same = False
        if not same:
            self._notice(
                tr(self.language, "notice.session_repo", repository=repository or "?"),
                "warning",
            )
            return
        self.session_id = session_id
        self._continue = True
        self._tokens = 0
        self.query_one(ChatLog).clear_messages()
        self._welcome()
        task = record.get("task")
        self._notice(
            tr(self.language, "notice.session_selected", id=session_id), "info"
        )
        if isinstance(task, str) and task:
            self._notice(f"task: {task}", "info")

    # ---------------------------------------------------------------- actions

    def action_help(self) -> None:
        self.push_screen(
            HelpScreen(
                tr(self.language, "help.title"), tr(self.language, "help.body")
            )
        )

    def action_models(self) -> None:
        if not self._idle():
            return
        try:
            models = self.backend.models()
        except BackendError as exc:
            self._notice(str(exc), "error")
            return
        if not models:
            self._notice(tr(self.language, "notice.models_empty"), "warning")
            return
        options: list[tuple[str, str]] = []
        for item in models:
            provider = str(item.get("provider_id") or "?")
            model = str(item.get("model_id") or "?")
            key = f"{provider}/{model}"
            label = key + ("  · selected" if item.get("selected") else "")
            window = item.get("context_window")
            if isinstance(window, int) and window > 0:
                label += f" · {_token_text(window)} ctx"
            options.append((key, label))
        self.push_screen(
            ListScreen(
                tr(self.language, "models.title"),
                options,
                tr(self.language, "models.hint"),
            ),
            self._model_chosen,
        )

    def _model_chosen(self, value: str | None) -> None:
        if not value or "/" not in value or not self._idle():
            return
        provider, model = value.split("/", 1)
        try:
            self.backend.select_model(provider, model)
        except BackendError as exc:
            self._notice(str(exc), "error")
            return
        self._refresh_model()
        self._notice(
            tr(self.language, "models.selected", model=value), "success"
        )

    def action_sessions(self) -> None:
        if not self._idle():
            return
        try:
            sessions = self.backend.sessions()
        except BackendError as exc:
            self._notice(str(exc), "error")
            return
        options: list[tuple[str, str]] = []
        for item in sessions:
            repository = item.get("repository")
            try:
                same = isinstance(repository, str) and os.path.normcase(
                    str(Path(repository).resolve())
                ) == os.path.normcase(str(self.repository))
            except OSError:
                same = False
            if not same:
                continue
            session_id = str(item.get("session_id") or "")
            if not session_id:
                continue
            name = str(item.get("name") or session_id)
            status = str(item.get("status") or "?")
            updated = _clock(item.get("updated_at"))
            task = " ".join(str(item.get("task") or "").split())[:70]
            label = f"{name}  · {status}  · {updated}  · {task}"
            options.append((session_id, label))
        if not options:
            self._notice(tr(self.language, "notice.no_sessions"), "warning")
            return
        self.push_screen(
            ListScreen(
                tr(self.language, "sessions.title"),
                options,
                tr(self.language, "sessions.hint"),
            ),
            self._session_chosen,
        )

    def _session_chosen(self, value: str | None) -> None:
        if value:
            self._resume(value)

    def action_new_session(self) -> None:
        if not self._idle():
            return
        self._last_assistant = None
        self.session_id = self._new_session_id()
        self._continue = False
        self._tokens = 0
        self.query_one(ChatLog).clear_messages()
        self._welcome()
        self._notice(tr(self.language, "notice.new_session", id=self.session_id), "info")
        self._refresh_chrome()

    def action_mcp(self) -> None:
        if not self._idle():
            return
        self.push_screen(
            McpScreen(self.backend, self.language, self.session_id),
            self._mcp_closed,
        )

    def _mcp_closed(self, result: str | None) -> None:
        if result:
            self._notice(result, "info")

    def action_provider(self) -> None:
        if not self._idle():
            return
        self.push_screen(
            ProviderSetupScreen(self.backend, self.language), self._provider_done
        )

    def _provider_done(self, result: dict[str, Any] | None) -> None:
        if not result:
            return
        self._refresh_model()
        selected = result.get("selected_model") or result.get("model") or {}
        model = ""
        if isinstance(selected, dict):
            model = f"{selected.get('provider_id', '')}/{selected.get('model_id', '')}".strip("/")
        self._notice(
            tr(self.language, "provider.saved", model=model or "?"), "success"
        )

    def action_clear(self) -> None:
        if not self._idle():
            return
        self._last_assistant = None
        self.query_one(ChatLog).clear_messages()
        self._notice(tr(self.language, "notice.clear"), "info")

    def action_stop(self) -> None:
        if not self._agent_running:
            return
        self._stopping = True
        self.query_one(StatusBar).set_state(
            tr(self.language, "status.stopping"), "warning"
        )
        self.run_worker(self.backend.stop, thread=True, name="agent-stop")

    def action_interrupt(self) -> None:
        if self._agent_running:
            self.action_stop()
        else:
            self.exit(0)

    async def action_quit(self) -> None:
        if self._agent_running:
            self.backend.stop()
        self.exit(0)

    def action_show_reasoning(self) -> None:
        assistant = self._last_assistant
        if assistant is None or not assistant.reasoning_text():
            self._notice("No reasoning recorded for the last turn.", "info")
            return
        self.push_screen(
            TextViewerScreen(
                tr(self.language, "reasoning.title"), assistant.reasoning_text()
            )
        )

    # ----------------------------------------------------------------- chrome

    def _refresh_model(self) -> None:
        try:
            self._model = self.backend.selected_model()
        except BackendError:
            self._model = None

    def _model_label(self) -> str:
        if self._model is None:
            return ""
        provider = self._model.get("provider_id")
        model = self._model.get("model_id")
        if not isinstance(provider, str) or not isinstance(model, str):
            return ""
        return f"{provider}/{model}"

    def _refresh_chrome(self) -> None:
        self.query_one(HeaderBar).update_state(
            repository=self.repository.name or str(self.repository),
            branch=self._branch,
            model=self._model_label(),
            session=self.session_id[:12],
            tokens=self._tokens,
            running=self._agent_running,
        )

    def _refresh_status(self) -> None:
        bar = self.query_one(StatusBar)
        if self._stopping:
            bar.set_state(tr(self.language, "status.stopping"), "warning")
        elif self._agent_running:
            elapsed = max(0, int(time.monotonic() - self._started_at))
            bar.set_state(
                f"{tr(self.language, 'status.running')} · {elapsed}s", "warning"
            )
        else:
            bar.set_state(tr(self.language, "status.idle"), "success")
        bar.set_hint(tr(self.language, "status.hint"))

    def _idle(self) -> bool:
        """Refuse a surface that would move the session under a live run."""

        if self._agent_running:
            self._notice(tr(self.language, "notice.already_running"), "warning")
            return False
        return True

    def _notice(self, text: str, level: str = "info") -> None:
        if not text:
            return
        self.query_one(ChatLog).add(Notice(text, level))

    @staticmethod
    def _new_session_id() -> str:
        return f"tui-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _save_preference(**changes: object) -> None:
    try:
        prefs.save_preferences(**changes)
    except OSError:
        # A read-only config directory must not take a running chat down.
        pass


def _git_branch(repository: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=2.0,
            creationflags=windowless_flags(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _detect_verification(repository: Path) -> tuple[tuple[str, ...], ...]:
    """The same bounded discovery the runtime performs, without its web import."""

    commands: list[tuple[str, ...]] = []
    package = repository / "package.json"
    if package.is_file():
        try:
            payload = json.loads(package.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = None
        scripts = payload.get("scripts") if isinstance(payload, dict) else None
        if isinstance(scripts, dict):
            for name in ("test", "verify", "typecheck", "lint", "build"):
                raw = scripts.get(name)
                if not isinstance(raw, str):
                    continue
                if any(marker in raw for marker in ("--fix", "--write", " deploy", " publish")):
                    continue
                command = ("npm", "test") if name == "test" else ("npm", "run", name)
                if command not in commands:
                    commands.append(command)
    if (repository / "pyproject.toml").is_file() and (repository / "tests").is_dir():
        commands.append(("python", "-m", "pytest", "-q"))
    if (repository / "Cargo.toml").is_file():
        commands.append(("cargo", "test"))
    if (repository / "go.mod").is_file():
        commands.append(("go", "test", "./..."))
    return tuple(commands) or (("git", "diff", "--check"),)


def _clock(value: Any) -> str:
    if not isinstance(value, (int, float)):
        return "?"
    try:
        return time.strftime("%m-%d %H:%M", time.localtime(float(value)))
    except (OverflowError, OSError, ValueError):
        return "?"


def _token_text(value: int) -> str:
    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"{value / 1_000:.1f}k"
    return str(value)