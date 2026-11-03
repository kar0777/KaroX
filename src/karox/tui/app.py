"""The KaroX terminal client: a transcript, a composer and one status line.

It reads like a CLI session. Text typed into the composer goes to the agent; a
line that starts with ``/`` is a command, and typing ``/`` opens the command
list right above the composer. Pickers -- model, session, provider, MCP server
-- open in the same place and are filtered by typing, and command output is
printed into the transcript the way a CLI prints it. Only the two multi-field
forms (a custom endpoint and an MCP server) are screens of their own.

The app never imports the agent runtime and never parses human progress text:
every fact comes from a ``--json`` CLI surface or a typed stream event, so the
whole client can be exercised against a recorded stream. Every CLI call runs in
a worker thread, so a slow provider or disk never freezes the screen.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.theme import Theme
from textual.widgets import Input, Static, TextArea

from ..detached_process import windowless_flags
from . import commands as slash
from . import prefs
from .backend import AgentRequest, CLIBackend
from .i18n import detect_language, normalise_language, plural, tr
from .modals import McpAddScreen, ProviderSetupScreen
from .protocol import Event, Outcome, SessionState, tool_category
from .tooltext import duration_text, plan_rows, plan_step, short_tokens, working_text
from .views import (
    ADD_SERVER_KEY,
    CUSTOM_PROVIDER_KEY,
    catalog_items,
    doctor_text,
    help_text,
    lines_text,
    mcp_items,
    model_items,
    same_repository,
    session_items,
    session_state_lines,
    skill_text,
    status_text,
    token_text,
)
from .widgets import (
    MUTED,
    AssistantMessage,
    ChatInput,
    ChatLog,
    InlineMenu,
    MenuItem,
    Notice,
    SecretPrompt,
    StatusLine,
    TextBlock,
    ToolCard,
    UserMessage,
    WelcomeCard,
    WorkingLine,
    status_segments,
    welcome_text,
)

EFFORT_LEVELS = ("auto", "low", "medium", "high", "extra-high", "ultra")
MODES = ("build", "plan", "ideate")
LANGUAGES = (("ru", "Русский"), ("en", "English"))
#: Commands that would move the session or the screen under a live run.
_REFUSED_WHILE_RUNNING = frozenset({"connect", "model", "sessions", "new", "mcp", "clear"})


@dataclass
class RunState:
    """Live facts about the current or latest run, for the status line.

    Every field is derived from a typed stream event or the final report --
    nothing here is inferred from human-readable text.
    """

    state: str = "idle"  # idle | running | stopping | done | failed | stopped
    step: int = 0
    phase: str = ""
    started_at: float = 0.0
    prompt_tokens: int = 0
    #: Tokens this run has spent so far, for the working line.
    tokens: int = 0
    context_fraction: float | None = None
    reads: int = 0
    edits: int = 0
    tests_ok: int = 0
    tests_failed: int = 0
    mcp_calls: int = 0
    skill_calls: int = 0
    inspections: int = 0
    files: list[str] = field(default_factory=list)

    def elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        return max(0.0, time.monotonic() - self.started_at)


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
        Binding("ctrl+n", "new_session", "New session", show=False),
        Binding("ctrl+r", "show_reasoning", "Reasoning", show=False),
        Binding("ctrl+o", "toggle_verbose", "Tool details", show=False),
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
        self._resume_requested = bool(session_id)
        self.session_id = session_id or self._new_session_id()
        self._continue = False
        self._model: dict[str, Any] | None = None
        self._model_loaded = False
        self._branch = ""
        self._tokens = 0
        self._agent_running = False
        self._stopping = False
        self.run_state = RunState()
        self.checkpoint: SessionState | None = None
        # Detection is a suggestion shown to the user, never approval: the CLI
        # treats --verification-command as an explicit human decision. Only
        # commands the user adds with /verify travel to the child.
        self._detected_verification: tuple[tuple[str, ...], ...] = (
            _detect_verification(self.repository)
        )
        self._verification: tuple[tuple[str, ...], ...] = ()
        effort = stored.get("effort_level")
        self.effort = effort if effort in EFFORT_LEVELS else "auto"
        mode = stored.get("agent_mode")
        self.mode = mode if mode in MODES else "build"
        self._cards: dict[str, ToolCard] = {}
        # The answer of one turn is a sequence of segments interleaved with
        # tool lines in the order things happened; ``_segment`` is the open one.
        self._segment: AssistantMessage | None = None
        self._segments: list[AssistantMessage] = []
        self._last_reasoning = ""
        self._welcome_card: WelcomeCard | None = None
        self._menu_context: dict[str, Any] = {}
        self._catalog: dict[str, dict[str, Any]] = {}
        self._prompt_handler: Callable[[str], None] | None = None
        self._connect_flow: dict[str, Any] | None = None
        # What the working line says: the calls still running (call id ->
        # tool, target), the model's current phase, the plan's next item, a
        # provider retry, and tasks typed while the agent was busy.
        self._running_tools: dict[str, tuple[str, str]] = {}
        self._phase_key = "working.thinking"
        self._plan_rows: list[tuple[str, str]] = []
        self._retry_note = ""
        self._queue: list[str] = []
        self._lookups = 0
        self._lookup_text = ""
        #: Ctrl+O: every tool line shows its whole preview and raw facts.
        self._verbose = False
        #: MCP servers chosen before the session exists: server -> scope
        #: ("all" | "read"), attached by the first run's ``--mcp-server``.
        self._pending_mcp: dict[str, str] = {}
        self._mcp_payload: dict[str, Any] = {}
        self.register_theme(
            Theme(
                name="karox",
                primary="#d4b676",
                secondary="#8a7e6a",
                accent="#d4b676",
                warning="#c6a56b",
                error="#e0a3a3",
                success="#8aab7e",
                foreground="#e5e5e5",
                background="#0e0c08",
                surface="#1a1712",
                panel="#1a1712",
                dark=True,
            )
        )
        self.theme = "karox"

    def compose(self) -> ComposeResult:
        yield ChatLog(id="chat")
        yield WorkingLine(id="working")
        yield InlineMenu(id="menu")
        with Horizontal(id="composer-box"):
            yield Static(">", id="composer-prompt", markup=False)
            yield ChatInput(
                id="composer",
                placeholder=tr(self.language, "composer.placeholder"),
                soft_wrap=True,
            )
        yield Static("", id="prompt-label")
        yield SecretPrompt(id="secret")
        yield StatusLine(id="status")
        yield Static("", id="shortcuts", markup=False)

    # --------------------------------------------------------------- lifecycle

    @property
    def composer(self) -> ChatInput:
        return self.query_one("#composer", ChatInput)

    @property
    def menu(self) -> InlineMenu:
        return self.query_one("#menu", InlineMenu)

    @property
    def chat(self) -> ChatLog:
        return self.query_one("#chat", ChatLog)

    @property
    def working(self) -> WorkingLine:
        return self.query_one("#working", WorkingLine)

    def on_mount(self) -> None:
        composer = self.composer
        composer.inline_menu = self.menu
        composer.scroller = self.chat
        self.query_one("#secret", SecretPrompt).display = False
        self.query_one("#prompt-label", Static).display = False
        self.query_one("#shortcuts", Static).display = False
        composer.focus()
        self._add_welcome()
        self._refresh_chrome()
        self._refresh_status()
        self.set_interval(1.0, self._tick)
        self._background(self._load_startup, self._startup_loaded)

    def _tick(self) -> None:
        if self._agent_running:
            self._refresh_working()

    def _load_startup(self) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
        record = self.backend.session(self.session_id) if self._resume_requested else None
        try:
            model = self.backend.selected_model()
        except Exception:  # noqa: BLE001 - "no model" is a state, not a crash
            model = None
        return record, model, _git_branch(self.repository)

    def _startup_loaded(
        self, loaded: tuple[dict[str, Any] | None, dict[str, Any] | None, str]
    ) -> None:
        record, model, branch = loaded
        self._model = model
        self._model_loaded = True
        self._branch = branch
        if record is not None:
            repository = record.get("repository")
            if isinstance(repository, str) and repository and not same_repository(
                repository, self.repository
            ):
                self.session_id = self._new_session_id()
                self._continue = False
                self._notice(
                    tr(self.language, "notice.session_repo", repository=repository), "warning"
                )
            else:
                self._continue = True
                self.checkpoint = SessionState.from_record(record)
        self._refresh_welcome()
        self._refresh_chrome()
        self._refresh_status()

    # ----------------------------------------------------------- background

    def _background(
        self,
        work: Callable[[], Any],
        done: Callable[[Any], None],
        *,
        activity: str = "",
        failed: Callable[[str], None] | None = None,
    ) -> None:
        """Run one CLI call in a thread and hand its result back to the screen."""

        if activity:
            self._lookup_started(activity)

        def runner() -> None:
            try:
                result = work()
            except Exception as exc:  # noqa: BLE001 - every failure becomes a notice
                message = str(exc).strip() or type(exc).__name__
                self._from_thread(self._background_failed, bool(activity), failed, message)
                return
            self._from_thread(self._background_done, bool(activity), done, result)

        self.run_worker(runner, thread=True, group="lookup", exit_on_error=False)

    def _from_thread(self, callback: Callable[..., Any], *arguments: Any) -> None:
        try:
            self.call_from_thread(callback, *arguments)
        except RuntimeError:
            # The app is shutting down; there is no screen left to update.
            pass

    def _background_done(self, had_activity: bool, done: Callable[[Any], None], result: Any) -> None:
        if had_activity:
            self._lookup_finished()
        done(result)

    def _background_failed(
        self, had_activity: bool, failed: Callable[[str], None] | None, message: str
    ) -> None:
        if had_activity:
            self._lookup_finished()
        if failed is not None:
            failed(message)
        else:
            self._notice(message, "error")

    def _lookup_started(self, text: str) -> None:
        # A slow CLI lookup (doctor, an MCP probe, the model list) gets the
        # same dots as the agent, so the screen never looks frozen.
        self._lookups += 1
        self._lookup_text = text
        self._refresh_working()

    def _lookup_finished(self) -> None:
        self._lookups = max(0, self._lookups - 1)
        if not self._lookups:
            self._lookup_text = ""
        self._refresh_working()

    # ---------------------------------------------------------------- welcome

    def _add_welcome(self) -> None:
        self._welcome_card = self.chat.add(
            WelcomeCard(
                tr(self.language, "welcome.title"),
                tr(self.language, "welcome.body"),
                facts=self._welcome_facts(),
            )
        )

    def _refresh_welcome(self) -> None:
        card = self._welcome_card
        if card is None or not card.is_attached:
            return
        card.update(
            welcome_text(
                tr(self.language, "welcome.title"),
                tr(self.language, "welcome.body"),
                self._welcome_facts(),
            )
        )

    def _welcome_facts(self) -> list[str]:
        lang = self.language
        path = _display_path(self.repository)
        if self._branch:
            path = f"{path} ({self._branch})"
        facts = [tr(lang, "welcome.cwd", path=path)]
        model = self._model_label() or tr(
            lang, "status.no_model" if self._model_loaded else "status.loading"
        )
        facts.append(tr(lang, "welcome.model", model=model))
        if self._verification:
            commands = "; ".join(" ".join(item) for item in self._verification)
            facts.append(tr(self.language, "welcome.facts.verification", commands=commands))
        elif self._detected_verification:
            commands = "; ".join(" ".join(item) for item in self._detected_verification)
            facts.append(tr(self.language, "welcome.facts.suggested", commands=commands))
        return facts

    # ------------------------------------------------------------------ input

    @on(TextArea.Changed, "#composer")
    def composer_changed(self, event: TextArea.Changed) -> None:
        del event
        composer = self.composer
        text = str(composer.text)
        if text:
            self._show_shortcuts(False)
        menu = self.menu
        if (
            text.startswith("/")
            and not any(character.isspace() for character in text)
            and text != composer.recalled
        ):
            self._show_palette(text[1:])
        elif menu.kind == "commands":
            menu.close()
        elif menu.active:
            menu.filter(text.strip())

    def _show_palette(self, query: str) -> None:
        items = [
            MenuItem(
                command.name,
                f"/{command.name}" + (f" {command.argument}" if command.argument else ""),
                tr(self.language, command.description),
                search=" ".join(command.aliases),
            )
            for command in slash.match(query)
        ]
        self._menu_context = {}
        self.menu.open(
            "commands",
            "",
            items,
            hint=tr(self.language, "menu.palette_hint"),
            empty=tr(self.language, "menu.empty"),
            filtered=False,
        )

    def _open_picker(
        self,
        kind: str,
        title: str,
        items: list[MenuItem],
        *,
        current: str | None = None,
        query: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        self._menu_context = dict(context or {})
        self.menu.open(
            kind,
            title,
            items,
            hint=tr(self.language, "menu.picker_hint"),
            empty=tr(self.language, "menu.empty"),
            current=current,
            query=query,
        )
        composer = self.composer
        if str(composer.text) != query:
            composer.set_text(query)
        if composer.display:
            composer.focus()

    @on(InlineMenu.Chosen)
    def menu_chosen(self, event: InlineMenu.Chosen) -> None:
        event.stop()
        kind, key = event.kind, event.key
        context = self._menu_context
        self._menu_context = {}
        self.menu.close()
        if kind == "commands":
            command = slash.resolve(key)
            if command is None:
                return
            if event.complete or command.requires_argument:
                self.composer.set_text(f"/{command.name} ")
                return
            self.composer.set_text("")
            self.composer.push_history(f"/{command.name}")
            self._run_command(command, "")
            return
        self.composer.set_text("")
        handlers: dict[str, Callable[[str, dict[str, Any]], None]] = {
            "model": self._model_chosen,
            "session": self._session_chosen,
            "connect": self._provider_chosen,
            "mcp": self._mcp_server_chosen,
            "mcp-action": self._mcp_action_chosen,
            "mcp-confirm": self._mcp_confirmed,
            "skills": self._skill_chosen,
            "effort": lambda value, _: self._set_effort(value),
            "mode": lambda value, _: self._set_mode(value),
            "lang": lambda value, _: self._set_language(value),
        }
        handler = handlers.get(kind)
        if handler is not None:
            handler(key, context)

    @on(InlineMenu.Cancelled)
    def menu_cancelled(self, event: InlineMenu.Cancelled) -> None:
        event.stop()
        self._menu_context = {}
        if event.kind != "commands":
            self.composer.set_text("")

    @on(ChatInput.Submitted)
    def submitted(self, event: ChatInput.Submitted) -> None:
        value = event.value.strip()
        if not value:
            return
        if self.menu.active:
            self.menu.close()
        self.composer.push_history(value)
        if value.startswith("/"):
            self._command(value)
        else:
            self._start_task(value)

    # -------------------------------------------------------- one-line prompt

    def _ask(
        self,
        placeholder: str,
        handler: Callable[[str], None],
        *,
        password: bool = True,
        value: str = "",
    ) -> None:
        """Swap the composer for a one-line prompt (masked for a key)."""

        prompt = self.query_one("#secret", SecretPrompt)
        self.menu.close()
        prompt.password = password
        prompt.placeholder = placeholder
        prompt.value = value
        self._prompt_handler = handler
        # The placeholder disappears with the first key, so the question stays
        # on a line of its own above the entry.
        label = self.query_one("#prompt-label", Static)
        label.update(Text(placeholder, style=MUTED))
        label.display = True
        self._show_shortcuts(False)
        self.composer.display = False
        self.query_one("#composer-box", Horizontal).display = False
        prompt.display = True
        prompt.focus()

    def _close_prompt(self) -> None:
        prompt = self.query_one("#secret", SecretPrompt)
        prompt.value = ""
        prompt.display = False
        self.query_one("#prompt-label", Static).display = False
        self._prompt_handler = None
        self.query_one("#composer-box", Horizontal).display = True
        composer = self.composer
        composer.display = True
        composer.focus()

    @on(Input.Submitted, "#secret")
    def prompt_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        value = event.value
        handler = self._prompt_handler
        self._close_prompt()
        if handler is not None:
            handler(value)

    @on(SecretPrompt.Cancelled)
    def prompt_cancelled(self, event: SecretPrompt.Cancelled) -> None:
        event.stop()
        self._cancel_prompt()

    def _cancel_prompt(self) -> None:
        self._close_prompt()
        self._connect_flow = None
        self._notice(tr(self.language, "notice.cancelled"), "info")

    @property
    def _prompt_open(self) -> bool:
        return bool(self.query_one("#secret", SecretPrompt).display)

    # --------------------------------------------------------------- commands

    def _command(self, value: str) -> None:
        head, _, rest = value[1:].partition(" ")
        command = slash.resolve(head)
        if command is None:
            self.chat.add(UserMessage(value, command=True))
            self._notice(
                tr(self.language, "notice.unknown_command", command=head.strip() or "?"),
                "warning",
            )
            return
        self._run_command(command, rest.strip())

    def _run_command(self, command: slash.SlashCommand, argument: str) -> None:
        if command.name != "clear":
            # Echo the command like a shell keeps the prompt line on screen, so
            # the printed output below it reads as the answer to something.
            self.chat.add(UserMessage(f"/{command.name} {argument}".strip(), command=True))
        if command.name in _REFUSED_WHILE_RUNNING and not self._idle():
            return
        handler = getattr(self, "_cmd_" + command.name.replace("-", "_"))
        handler(argument)

    def _cmd_help(self, argument: str) -> None:
        del argument
        self._print_block(tr(self.language, "help.title"), help_text(self.language))

    def _cmd_status(self, argument: str) -> None:
        del argument
        state = self.run_state
        self._print_block(
            tr(self.language, "info.title"),
            status_text(
                lang=self.language,
                repository=self.repository,
                branch=self._branch,
                model=self._model_label(),
                session_id=self.session_id,
                effort=self.effort,
                mode=self.mode,
                tokens=self._tokens,
                verification=self._verification,
                suggested=self._detected_verification,
                counters={
                    "reads": state.reads,
                    "edits": state.edits,
                    "ok": state.tests_ok,
                    "bad": state.tests_failed,
                    "mcp": state.mcp_calls,
                    "skills": state.skill_calls,
                },
                checkpoint_at=(
                    self.checkpoint.updated_at if self.checkpoint is not None else None
                ),
            ),
        )

    def _cmd_session(self, argument: str) -> None:
        """The durable record behind this window, printed as a block.

        Read-only, so it stays available while a run is live; the record shown
        is the last checkpoint the kernel saved.
        """

        del argument
        session_id = self.session_id

        def done(record: dict[str, Any] | None) -> None:
            state = SessionState.from_record(record) if record is not None else None
            if state is not None:
                self.checkpoint = state
            self._print_block(
                tr(self.language, "session.title"),
                lines_text(session_state_lines(state, self.language)),
            )

        self._background(lambda: self.backend.session(session_id), done)

    def _cmd_doctor(self, argument: str) -> None:
        del argument

        def done(report: dict[str, Any]) -> None:
            text, _level = doctor_text(report if isinstance(report, dict) else {})
            self._print_block(tr(self.language, "doctor.title"), text)

        self._background(
            self.backend.doctor, done, activity=tr(self.language, "doctor.running")
        )

    def _cmd_reasoning(self, argument: str) -> None:
        del argument
        reasoning = self._last_reasoning or "\n\n".join(
            segment.reasoning_text() for segment in self._segments if segment.reasoning_text()
        )
        if not reasoning.strip():
            self._notice(tr(self.language, "notice.no_reasoning"), "info")
            return
        self._print_block(tr(self.language, "reasoning.title"), Text(reasoning.strip(), style=MUTED))

    def _cmd_verify(self, argument: str) -> None:
        if not argument:
            if self._verification:
                commands = "; ".join(" ".join(item) for item in self._verification)
                self._notice(tr(self.language, "notice.verify_list", commands=commands), "info")
            else:
                text = tr(self.language, "notice.verify_none")
                if self._detected_verification:
                    commands = "; ".join(" ".join(item) for item in self._detected_verification)
                    text += "\n" + tr(self.language, "welcome.facts.suggested", commands=commands)
                self._notice(text, "info")
            return
        try:
            command = tuple(shlex.split(argument))
        except ValueError as exc:
            self._notice(tr(self.language, "notice.verify_parse", error=str(exc)), "error")
            return
        if not command:
            return
        if command not in self._verification:
            self._verification = (*self._verification, command)
        self._notice(
            tr(self.language, "notice.verify_added", command=" ".join(command)), "success"
        )
        self._refresh_welcome()

    def _cmd_unverify(self, argument: str) -> None:
        del argument
        self._verification = ()
        self._notice(tr(self.language, "notice.verify_cleared"), "info")
        self._refresh_welcome()

    def _cmd_effort(self, argument: str) -> None:
        if argument:
            self._set_effort(argument)
            return
        items = [MenuItem(level, level, marked=level == self.effort) for level in EFFORT_LEVELS]
        self._open_picker("effort", tr(self.language, "effort.title"), items, current=self.effort)

    def _cmd_mode(self, argument: str) -> None:
        if argument:
            self._set_mode(argument)
            return
        items = [
            MenuItem(mode, mode, tr(self.language, f"mode.{mode}"), marked=mode == self.mode)
            for mode in MODES
        ]
        self._open_picker("mode", tr(self.language, "mode.title"), items, current=self.mode)

    def _cmd_lang(self, argument: str) -> None:
        if argument:
            self._set_language(argument)
            return
        items = [
            MenuItem(code, name, code, marked=code == self.language) for code, name in LANGUAGES
        ]
        self._open_picker("lang", tr(self.language, "lang.title"), items, current=self.language)

    def _cmd_new(self, argument: str) -> None:
        del argument
        self.action_new_session()

    def _cmd_clear(self, argument: str) -> None:
        del argument
        self.action_clear()

    def _cmd_stop(self, argument: str) -> None:
        del argument
        self.action_stop()

    def _cmd_quit(self, argument: str) -> None:
        del argument
        if self._agent_running:
            self.backend.stop()
        self.exit(0)

    # ------------------------------------------------------- model & provider

    def _cmd_model(self, argument: str) -> None:
        if "/" in argument and " " not in argument:
            self._select_model(argument)
            return
        self._show_models(query=argument)

    def _show_models(self, *, query: str = "", current: str | None = None) -> None:
        def done(models: list[dict[str, Any]]) -> None:
            if not models:
                self._notice(tr(self.language, "notice.models_empty"), "warning")
                return
            items, selected = model_items(models, self.language)
            self._open_picker(
                "model",
                tr(self.language, "models.title"),
                items,
                current=current or selected,
                query=query,
            )

        self._background(self.backend.models, done, activity=tr(self.language, "status.loading"))

    def _model_chosen(self, key: str, context: dict[str, Any]) -> None:
        del context
        self._select_model(key)

    def _select_model(self, key: str) -> None:
        if not self._idle():
            return
        provider, _, model = key.partition("/")
        if not provider or not model:
            return

        def work() -> dict[str, Any] | None:
            self.backend.select_model(provider, model)
            return self.backend.selected_model()

        def done(selected: dict[str, Any] | None) -> None:
            self._model = selected
            self._model_loaded = True
            self._refresh_welcome()
            self._refresh_chrome()
            self._notice(tr(self.language, "models.selected", model=key), "success")

        self._background(work, done)

    def _reload_model(self) -> None:
        def done(selected: dict[str, Any] | None) -> None:
            self._model = selected
            self._model_loaded = True
            self._refresh_welcome()
            self._refresh_chrome()

        self._background(self.backend.selected_model, done)

    def _cmd_connect(self, argument: str) -> None:
        wanted = argument.strip()

        def done(rows: list[dict[str, Any]]) -> None:
            self._catalog = {
                str(row.get("provider_id")): row for row in rows if row.get("provider_id")
            }
            if wanted:
                exact = next(
                    (row for key, row in self._catalog.items() if key.lower() == wanted.lower()),
                    None,
                )
                if exact is not None:
                    self._begin_connect(exact)
                    return
            self._open_picker(
                "connect",
                tr(self.language, "connect.title"),
                catalog_items(rows, self.language),
                query=wanted,
            )

        self._background(
            self.backend.provider_catalog,
            done,
            activity=tr(self.language, "status.loading"),
            failed=lambda error: self._notice(
                tr(self.language, "notice.catalog_failed", error=error), "error"
            ),
        )

    def _provider_chosen(self, key: str, context: dict[str, Any]) -> None:
        del context
        if key == CUSTOM_PROVIDER_KEY:
            self.push_screen(
                ProviderSetupScreen(self.backend, self.language), self._provider_saved
            )
            return
        row = self._catalog.get(key)
        if row is not None:
            self._begin_connect(row)

    def _begin_connect(self, row: dict[str, Any]) -> None:
        if not self._idle():
            return
        name = str(row.get("display_name") or row.get("provider_id") or "?")
        env = [str(item) for item in row.get("env") or [] if item]
        variables = [str(item) for item in row.get("url_variables") or [] if item]
        self._connect_flow = {
            "row": row,
            "name": name,
            "key": "",
            "pending": variables,
            "values": [],
        }
        if not env:
            self._connect_next()
            return
        if row.get("connected_as"):
            placeholder = tr(self.language, "secret.placeholder_saved", provider=name)
        else:
            placeholder = tr(self.language, "secret.placeholder", provider=name, env=env[0])
        self._ask(placeholder, self._connect_key_entered, password=True)

    def _connect_key_entered(self, value: str) -> None:
        flow = self._connect_flow
        if flow is None:
            return
        key = value.strip()
        if not key and not flow["row"].get("connected_as"):
            self._connect_flow = None
            self._notice(tr(self.language, "notice.key_empty"), "warning")
            return
        flow["key"] = key
        self._connect_next()

    def _connect_next(self) -> None:
        flow = self._connect_flow
        if flow is None:
            return
        if flow["pending"]:
            name = flow["pending"][0]
            self._ask(
                tr(self.language, "secret.variable", name=name, provider=flow["name"]),
                self._connect_variable_entered,
                password=False,
                value=os.environ.get(name, ""),
            )
            return
        self._connect_flow = None
        self._run_connect(flow)

    def _connect_variable_entered(self, value: str) -> None:
        flow = self._connect_flow
        if flow is None or not flow["pending"]:
            return
        name = flow["pending"].pop(0)
        if not value.strip():
            self._connect_flow = None
            self._notice(tr(self.language, "notice.variable_empty", name=name), "warning")
            return
        flow["values"].append((name, value.strip()))
        self._connect_next()

    def _run_connect(self, flow: dict[str, Any]) -> None:
        row = flow["row"]
        name = str(flow["name"])
        catalog_id = str(row.get("provider_id") or "")
        registry_id = str(row.get("connected_as") or "")
        api_key = str(flow.pop("key", "") or "")
        variables = tuple(flow["values"])

        def work() -> dict[str, Any]:
            return self.backend.provider_connect(
                catalog_id, api_key=api_key, provider_id=registry_id, variables=variables
            )

        self._background(
            work,
            lambda payload: self._connected(name, payload),
            activity=tr(self.language, "activity.connecting", provider=name),
            failed=lambda error: self._notice(
                tr(self.language, "notice.connect_failed", provider=name, error=error), "error"
            ),
        )

    def _connected(self, name: str, payload: dict[str, Any]) -> None:
        provider_id = str(payload.get("provider_id") or "")
        model = str(payload.get("selected_model") or "")
        self._notice(
            tr(
                self.language,
                "notice.connected",
                provider=name,
                id=provider_id,
                models=payload.get("models_installed", 0),
                priced=payload.get("priced_models", 0),
            ),
            "success",
        )
        if payload.get("activated"):
            self._notice(
                tr(self.language, "models.selected", model=f"{provider_id}/{model}"), "success"
            )
            self._reload_model()
            return
        self._notice(tr(self.language, "notice.connect_pick_model", id=provider_id), "info")
        self._show_models(
            query=f"{provider_id}/", current=f"{provider_id}/{model}" if model else None
        )

    def _provider_saved(self, result: dict[str, Any] | None) -> None:
        if not result:
            return
        selected = result.get("selected_model") or result.get("model") or {}
        model = ""
        if isinstance(selected, dict):
            model = f"{selected.get('provider_id', '')}/{selected.get('model_id', '')}".strip("/")
        self._notice(tr(self.language, "notice.provider_saved", model=model or "?"), "success")
        self._reload_model()

    # --------------------------------------------------------------- sessions

    def _cmd_sessions(self, argument: str) -> None:
        if argument:
            self._resume(argument)
            return

        def done(rows: list[dict[str, Any]]) -> None:
            items = session_items(rows, self.repository, self.session_id, self.language)
            if not items:
                self._notice(tr(self.language, "notice.no_sessions"), "warning")
                return
            self._open_picker(
                "session",
                tr(self.language, "sessions.title"),
                items,
                current=self.session_id,
            )

        self._background(self.backend.sessions, done, activity=tr(self.language, "status.loading"))

    def _session_chosen(self, key: str, context: dict[str, Any]) -> None:
        del context
        self._resume(key)

    def _resume(self, session_id: str) -> None:
        if not self._idle():
            return
        self._background(
            lambda: self.backend.session(session_id),
            lambda record: self._resumed(session_id, record),
        )

    def _resumed(self, session_id: str, record: dict[str, Any] | None) -> None:
        if self._agent_running:
            self._notice(tr(self.language, "notice.already_running"), "warning")
            return
        if record is None:
            self._notice(tr(self.language, "notice.session_missing", id=session_id), "error")
            return
        repository = record.get("repository")
        if not same_repository(repository, self.repository):
            self._notice(
                tr(self.language, "notice.session_repo", repository=repository or "?"),
                "warning",
            )
            return
        self.session_id = session_id
        self._continue = True
        self._tokens = 0
        self.run_state = RunState()
        self.checkpoint = SessionState.from_record(record)
        self._reset_turn()
        self.chat.clear_messages()
        self._add_welcome()
        self._notice(tr(self.language, "notice.session_selected", id=session_id), "info")
        task = record.get("task")
        if isinstance(task, str) and task.strip():
            self._notice(
                tr(self.language, "notice.session_task", task=" ".join(task.split())[:200]),
                "info",
            )
        self._refresh_status()
        self._refresh_chrome()

    # ---------------------------------------------------------------- skills

    def _cmd_skills(self, argument: str) -> None:
        del argument

        def done(skills: list[dict[str, Any]]) -> None:
            items: list[MenuItem] = []
            for item in skills:
                name = str(item.get("name") or "")
                if not name:
                    continue
                description = " ".join(str(item.get("description") or "").split())
                if len(description) > 70:
                    description = description[:69] + "…"
                items.append(MenuItem(name, name, description))
            if not items:
                self._notice(tr(self.language, "notice.skills_empty"), "warning")
                return
            self._open_picker("skills", tr(self.language, "skills.title"), items)

        self._background(
            self.backend.skill_list, done, activity=tr(self.language, "status.loading")
        )

    def _skill_chosen(self, key: str, context: dict[str, Any]) -> None:
        del context

        def done(skill: dict[str, Any] | None) -> None:
            if skill is None:
                self._notice(tr(self.language, "notice.skill_failed", name=key), "error")
                return
            self._print_block(
                tr(self.language, "skill.detail_title", name=key),
                skill_text(skill, self.language),
            )

        self._background(lambda: self.backend.skill_show(key), done)

    # ------------------------------------------------------------------- MCP

    def _cmd_mcp(self, argument: str) -> None:
        if argument.strip().lower() == "add":
            self._open_mcp_add()
            return
        session_id = self.session_id

        def done(payload: dict[str, Any]) -> None:
            self._mcp_payload = payload if isinstance(payload, dict) else {}
            self._open_picker(
                "mcp",
                tr(self.language, "mcp.title"),
                mcp_items(self._mcp_payload, self.language, pending=self._pending_mcp),
            )

        self._background(
            lambda: self.backend.mcp_status(session_id),
            done,
            activity=tr(self.language, "status.loading"),
        )

    def _open_mcp_add(self) -> None:
        self.push_screen(McpAddScreen(self.backend, self.language), self._mcp_added)

    def _mcp_added(self, server: str | None) -> None:
        if server:
            self._notice(tr(self.language, "notice.mcp_added", server=server), "success")

    def _mcp_in_session(self, server: str) -> bool:
        if server in self._pending_mcp:
            return True
        entries = self._mcp_payload.get("servers", [])
        for entry in entries if isinstance(entries, list) else []:
            if isinstance(entry, dict) and entry.get("server_id") == server:
                selection = entry.get("selection")
                state = selection.get("state") if isinstance(selection, dict) else None
                return bool(state) and state != "not_selected"
        return False

    def _mcp_server_chosen(self, key: str, context: dict[str, Any]) -> None:
        del context
        if key == ADD_SERVER_KEY:
            self._open_mcp_add()
            return
        lang = self.language
        items = [
            MenuItem("probe", tr(lang, "mcp.action.probe")),
            MenuItem("select", tr(lang, "mcp.action.select"), tr(lang, "mcp.action.select_detail")),
            MenuItem(
                "select_read",
                tr(lang, "mcp.action.select_read"),
                tr(lang, "mcp.action.select_read_detail"),
            ),
        ]
        if self._mcp_in_session(key):
            items.append(MenuItem("deselect", tr(lang, "mcp.action.deselect")))
        items.append(MenuItem("remove", tr(lang, "mcp.action.remove")))
        self._open_picker(
            "mcp-action",
            tr(lang, "mcp.actions_title", server=key),
            items,
            context={"server": key},
        )

    def _mcp_action_chosen(self, key: str, context: dict[str, Any]) -> None:
        server = str(context.get("server") or "")
        if not server:
            return
        lang = self.language
        if key == "probe":
            self._background(
                lambda: self.backend.mcp_probe(server),
                lambda payload: self._notice(
                    tr(
                        lang,
                        "notice.mcp_probe_ok",
                        server=server,
                        count=payload.get("tool_count", 0) if isinstance(payload, dict) else 0,
                    ),
                    "success",
                ),
                activity=tr(lang, "notice.mcp_probe", server=server),
                failed=lambda error: self._notice(
                    tr(lang, "notice.mcp_probe_failed", server=server, error=error),
                    "error",
                ),
            )
        elif key in {"select", "select_read"}:
            self._mcp_select(server, "all" if key == "select" else "read")
        elif key == "deselect":
            self._mcp_deselect(server)
        elif key == "remove":
            items = [
                MenuItem("no", tr(lang, "mcp.confirm_no")),
                MenuItem("yes", tr(lang, "mcp.confirm_yes")),
            ]
            self._open_picker(
                "mcp-confirm",
                tr(lang, "mcp.confirm_remove", server=server),
                items,
                context={"server": server},
            )

    def _mcp_select(self, server: str, scope: str) -> None:
        """Attach a server to this window's session: every tool, or read-only.

        The choice is the user's explicit approval, so the chosen tools are
        stored as ``allow`` -- a stored ``ask`` would hide them from the agent,
        since nothing can answer a question in the middle of a run. Before the
        first run there is no session to store it in: the server is checked
        now and attached by that run's ``--mcp-server``.
        """

        lang = self.language
        session_id = self.session_id

        def work() -> dict[str, Any]:
            if self.backend.session(session_id) is None:
                discovered = self.backend.mcp_tools(server)
                allowed = [
                    item
                    for item in discovered
                    if scope == "all" or item.get("read_only") is True
                ]
                return {"pending": True, "count": len(allowed)}
            payload = self.backend.mcp_select(server, session_id, scope)
            selection = payload.get("selection") if isinstance(payload, dict) else None
            stored = selection.get("tools") if isinstance(selection, dict) else None
            permissions = list(stored.values()) if isinstance(stored, dict) else []
            allowed_count = len(
                [
                    item
                    for item in permissions
                    if isinstance(item, dict) and item.get("permission") == "allow"
                ]
            )
            return {"pending": False, "count": allowed_count}

        def done(result: dict[str, Any]) -> None:
            count = int(result.get("count") or 0)
            if result.get("pending"):
                self._pending_mcp[server] = scope
                self._notice(
                    tr(lang, "notice.mcp_pending", server=server, count=count), "success"
                )
                return
            self._pending_mcp.pop(server, None)
            self._notice(tr(lang, "notice.mcp_selected", server=server, count=count), "success")

        self._background(
            work,
            done,
            activity=tr(lang, "notice.mcp_selecting", server=server),
            failed=lambda error: self._notice(
                tr(lang, "notice.mcp_apply_failed", server=server, error=error), "error"
            ),
        )

    def _mcp_deselect(self, server: str) -> None:
        lang = self.language
        if self._pending_mcp.pop(server, None) is not None:
            self._notice(tr(lang, "notice.mcp_deselected", server=server), "info")
            return
        session_id = self.session_id
        self._background(
            lambda: self.backend.mcp_deselect(server, session_id),
            lambda _: self._notice(tr(lang, "notice.mcp_deselected", server=server), "info"),
        )

    def _mcp_confirmed(self, key: str, context: dict[str, Any]) -> None:
        server = str(context.get("server") or "")
        if key != "yes" or not server:
            self._notice(tr(self.language, "notice.cancelled"), "info")
            return
        self._background(
            lambda: self.backend.mcp_remove(server),
            lambda _: self._notice(
                tr(self.language, "notice.mcp_removed", server=server), "success"
            ),
        )

    # -------------------------------------------------------------- settings

    def _set_effort(self, argument: str) -> None:
        level = argument.strip().lower()
        if level not in EFFORT_LEVELS:
            self._notice(
                tr(self.language, "notice.effort_invalid", levels=", ".join(EFFORT_LEVELS)),
                "error",
            )
            return
        self.effort = level
        _save_preference(effort_level=level)
        self._notice(tr(self.language, "notice.effort", level=level), "info")
        self._refresh_chrome()

    def _set_mode(self, argument: str, *, quiet: bool = False) -> None:
        mode = argument.strip().lower()
        if mode not in MODES:
            self._notice(
                tr(self.language, "notice.mode_invalid", modes=", ".join(MODES)), "error"
            )
            return
        self.mode = mode
        _save_preference(agent_mode=mode)
        if not quiet:
            self._notice(tr(self.language, "notice.mode", mode=mode), "info")
        self._refresh_chrome()

    @on(ChatInput.ModeCycled)
    def mode_cycled(self, event: ChatInput.ModeCycled) -> None:
        # Shift+Tab: the footer says the new mode; a transcript line per press
        # would only push the conversation up.
        event.stop()
        index = MODES.index(self.mode) if self.mode in MODES else 0
        self._set_mode(MODES[(index + 1) % len(MODES)], quiet=True)

    @on(ChatInput.ShortcutsToggled)
    def shortcuts_toggled(self, event: ChatInput.ShortcutsToggled) -> None:
        event.stop()
        panel = self.query_one("#shortcuts", Static)
        self._show_shortcuts(not panel.display)

    def _show_shortcuts(self, visible: bool) -> None:
        panel = self.query_one("#shortcuts", Static)
        if visible:
            panel.update(Text(tr(self.language, "shortcuts.text"), style=MUTED))
        panel.display = visible

    def _set_language(self, argument: str) -> None:
        selected = normalise_language(argument)
        if selected is None:
            self._notice(tr(self.language, "notice.language_invalid"), "error")
            return
        self.language = selected
        _save_preference(language=selected)
        self.composer.placeholder = tr(self.language, "composer.placeholder")
        self._refresh_status()
        self._refresh_chrome()
        self._notice(tr(self.language, "notice.language", language=selected), "info")

    # ------------------------------------------------------------------- run

    def _start_task(self, task: str) -> None:
        if self._agent_running:
            # Typed while the agent works: it runs next, in order, the way a
            # shell keeps type-ahead. The working line shows the queue.
            self._queue.append(task)
            self._refresh_working()
            return
        if self._model is None:
            try:
                self._model = self.backend.selected_model()
            except Exception:  # noqa: BLE001 - reported as "no model" below
                self._model = None
            self._model_loaded = True
        if self._model is None:
            self._notice(tr(self.language, "notice.no_model"), "warning")
            return
        self._show_shortcuts(False)
        self.chat.add(UserMessage(task))
        self._reset_turn()
        self._agent_running = True
        self._stopping = False
        self.run_state = RunState(state="running", started_at=time.monotonic())
        self._refresh_status()
        # Let the user line finish mounting before the first event can arrive:
        # a fast run could otherwise paint into widgets that are still queued.
        self.call_after_refresh(self._launch_run, task)

    def _reset_turn(self) -> None:
        self._segment = None
        self._segments = []
        self._cards.clear()
        self._running_tools.clear()
        self._phase_key = "working.thinking"
        self._plan_rows = []
        self._retry_note = ""
        self._last_reasoning = ""

    def _launch_run(self, task: str) -> None:
        self.run_worker(
            lambda: self._stream(task),
            thread=True,
            group="agent",
            name="agent-run",
            exit_on_error=False,
        )

    def _stream(self, task: str) -> None:
        request = AgentRequest(
            repository=self.repository,
            task=task,
            session_id=self.session_id,
            continue_task=self._continue,
            verification=self._verification,
            effort=None if self.effort == "auto" else self.effort,
            mode=None if self.mode == "build" else self.mode,
            mcp_servers=tuple(sorted(self._pending_mcp.items())),
        )

        def on_event(event: Event) -> None:
            self._from_thread(self._apply_event, event)

        try:
            result = self.backend.stream_agent(request, on_event)
        except Exception as exc:  # noqa: BLE001 - a UI must survive any bridge failure
            self._from_thread(self._finish, None, -1, str(exc), False)
            return
        self._from_thread(
            self._finish, result.outcome, result.exit_code, result.stderr, result.stopped
        )

    def _open_segment(self) -> AssistantMessage:
        if self._segment is None:
            self._segment = self.chat.add(AssistantMessage(self.language))
            self._segments.append(self._segment)
        return self._segment

    def _close_segment(self) -> None:
        segment, self._segment = self._segment, None
        if segment is not None:
            segment.finish()

    def _apply_event(self, event: Event) -> None:
        """Fold one typed stream event into the transcript and the working line.

        The transcript gets the durable lines -- answer text, one line per tool
        call with its result, notices that matter later. Everything that is
        only true right now (which call is running, a provider retry, what
        the plan says is next) goes to the working line and disappears with
        the run.
        """

        state = self.run_state
        kind = event.kind
        lang = self.language
        if kind == "text_delta":
            if event.text:
                self._open_segment().append_text(event.text)
                self._phase_key = "working.writing"
        elif kind in {"reasoning_delta", "reasoning_summary_delta"}:
            text = event.reasoning if kind == "reasoning_delta" else event.reasoning_summary
            if text:
                self._open_segment().append_reasoning(
                    text, summary=kind == "reasoning_summary_delta"
                )
                self._phase_key = "working.reasoning"
        elif kind == "step_started":
            # A new model turn starts a new stretch of text: without the break
            # "working on it" and the next step's "done" render as one word.
            self._close_segment()
            state.step = max(state.step, event.step)
            self._phase_key = "working.thinking"
            self._retry_note = ""
        elif kind == "step_finished":
            self._absorb_usage(event.usage)
        elif kind == "tool_started":
            self._close_segment()
            card = ToolCard(
                event.tool,
                event.call_id,
                tool_category(event.tool),
                language=lang,
                detail=event.detail,
                expanded=self._verbose,
            )
            self.chat.add(card)
            key = event.call_id or f"anonymous-{id(card)}"
            self._cards[key] = card
            self._running_tools[key] = (event.tool, event.detail)
            if event.tool == "plan.update":
                rows = plan_rows(event.detail)
                if rows:
                    self._plan_rows = rows
            self._retry_note = ""
        elif kind == "tool_finished":
            self._close_segment()
            category = tool_category(event.tool)
            key = event.call_id or next(
                (
                    name
                    for name, (tool, _detail) in reversed(list(self._running_tools.items()))
                    if tool == event.tool and name.startswith("anonymous-")
                ),
                "",
            )
            finished = self._cards.get(key) if key else None
            if finished is None:
                finished = ToolCard(
                    event.tool, event.call_id, category, language=lang, expanded=self._verbose
                )
                self.chat.add(finished)
                if event.call_id:
                    self._cards[event.call_id] = finished
            finished.finish(event.ok, event.summary, event.duration_seconds, preview=event.detail)
            self._running_tools.pop(key, None)
            self._count_tool(category)
            self._phase_key = "working.thinking"
        elif kind == "phase_changed":
            state.phase = event.phase
        elif kind == "compacted":
            self._close_segment()
            self._notice(tr(lang, "notice.compacted", summary=event.summary or "…"), "info")
        elif kind == "warning":
            warned = self._cards.get(event.call_id) if event.call_id else None
            if event.reason == "provider_retry":
                self._retry_note = event.detail or event.reason
            elif warned is not None:
                warned.note_error(event.detail or event.reason)
            else:
                self._close_segment()
                self._notice(self._warning_text(event.reason, event.detail), "warning")
        elif kind == "error":
            failed = self._cards.get(event.call_id) if event.call_id else None
            if failed is not None:
                # A refused or failed call: the reason belongs on its own line.
                failed.note_error(event.detail or event.reason)
            else:
                self._close_segment()
                self._notice(f"{event.reason}: {event.detail}".strip(": "), "error")
        elif kind == "file_edited":
            if event.path and event.path not in state.files:
                state.files.append(event.path)
        elif kind == "test_finished":
            if event.ok:
                state.tests_ok += 1
            else:
                state.tests_failed += 1
        if event.usage and kind != "step_finished":
            self._absorb_usage(event.usage)
        self._refresh_working()

    def _warning_text(self, reason: str, detail: str) -> str:
        """A kernel warning in the reader's words; unknown reasons stay raw."""

        key = f"warn.{reason}"
        text = tr(self.language, key, detail=detail)
        if text == key:
            return f"{reason}: {detail}".strip(": ")
        return text

    def _count_tool(self, category: str) -> None:
        state = self.run_state
        if category == "read":
            state.reads += 1
        elif category == "edit":
            state.edits += 1
        elif category == "mcp":
            state.mcp_calls += 1
        elif category == "skill":
            state.skill_calls += 1
        elif category == "inspect":
            state.inspections += 1

    def _absorb_usage(self, usage: dict[str, int]) -> None:
        if not usage:
            return
        state = self.run_state
        total = usage.get("total_tokens")
        if isinstance(total, int) and total > 0:
            self._tokens += total
            state.tokens += total
        prompt = usage.get("prompt_tokens") or usage.get("input_tokens")
        if isinstance(prompt, int) and prompt > 0:
            state.prompt_tokens = prompt
            window = 0
            if isinstance(self._model, dict):
                raw = self._model.get("context_window")
                if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
                    window = raw
            if window:
                state.context_fraction = prompt / float(window)
        self._refresh_chrome()

    def _finish(
        self,
        outcome: Outcome | None,
        exit_code: int,
        stderr: str,
        stopped: bool,
    ) -> None:
        self._agent_running = False
        self._stopping = False
        elapsed = self.run_state.elapsed()
        self._close_segment()
        # A call the run never reported back on (a stop in the middle of a
        # command) must not keep blinking as if it were still running.
        for key in list(self._running_tools):
            card = self._cards.get(key)
            if card is not None and card.ok is None:
                card.interrupt()
        self._running_tools.clear()
        self._retry_note = ""
        answered = any(segment.answer_text().strip() for segment in self._segments)
        if not answered and not stopped and outcome is not None and outcome.provider_message.strip():
            # The report's final message is shown only when nothing streamed;
            # otherwise it would repeat the answer the reader already has.
            self._open_segment().append_text(outcome.provider_message)
            self._close_segment()
        for segment in list(self._segments):
            if segment.is_blank():
                self._segments.remove(segment)
                try:
                    segment.remove()
                except Exception:  # noqa: BLE001 - an unmounted segment has nothing to remove
                    pass
        self._last_reasoning = "\n\n".join(
            segment.reasoning_text() for segment in self._segments if segment.reasoning_text()
        )
        if stopped:
            self.run_state.state = "stopped"
            self._result_notice(tr(self.language, "notice.stopped"), "warning")
            self._refresh_checkpoint()
        elif outcome is not None:
            if outcome.session_id:
                self.session_id = outcome.session_id
            self._tokens = outcome.token_total or self._tokens
            self.run_state.state = "failed" if outcome.status in {"failed", "error"} else "done"
            self._result_notice(self._result_text(outcome, elapsed), self._result_level(outcome))
            self._continue = True
            # The run created the session and stored the servers it was
            # asked to attach; from here on /mcp edits the session directly.
            self._pending_mcp.clear()
            self._refresh_checkpoint()
        else:
            self.run_state.state = "failed"
            lines = [line for line in stderr.strip().splitlines() if line.strip()]
            detail = lines[-1] if lines else f"exit code {exit_code}"
            self._result_notice(tr(self.language, "notice.backend_error", error=detail), "error")
        self._refresh_status()
        if self._queue:
            if outcome is not None and not stopped:
                self.call_after_refresh(self._start_task, self._queue.pop(0))
            else:
                # Stopped or broken: hand the type-ahead back instead of
                # running it on top of a turn that did not finish.
                self.composer.set_text("\n".join(self._queue))
                self._queue.clear()
                self._refresh_working()
        if self.composer.display:
            self.composer.focus()

    def _refresh_checkpoint(self) -> None:
        """Re-read the durable record so the saved-state fact is never guessed."""

        session_id = self.session_id

        def done(record: dict[str, Any] | None) -> None:
            if session_id != self.session_id:
                return
            self.checkpoint = SessionState.from_record(record) if record is not None else None
            self._refresh_status()

        self._background(lambda: self.backend.session(session_id), done, failed=lambda _: None)

    def _result_text(self, outcome: Outcome, elapsed: float) -> str:
        lang = self.language
        if outcome.verified:
            head = tr(lang, "result.verified")
        elif outcome.reason == "answer":
            head = tr(lang, "result.answer")
        else:
            head = tr(lang, "result.failed")
        parts = [head]
        if outcome.steps:
            parts.append(plural(lang, outcome.steps, "step"))
        if outcome.changed_files:
            parts.append(plural(lang, len(outcome.changed_files), "file"))
        passed, total = outcome.check_tally
        if total:
            parts.append(tr(lang, "result.checks", passed=passed, total=total))
        if outcome.token_total:
            parts.append(tr(lang, "result.tokens", count=token_text(outcome.token_total)))
        if outcome.costs:
            parts.append(tr(lang, "result.cost", cost=_cost_text(outcome.costs)))
        if elapsed:
            parts.append(tr(lang, "result.duration", seconds=elapsed))
        lines = [" · ".join(parts)]
        if not outcome.verified and outcome.reason and outcome.reason != "answer":
            lines.append(tr(lang, "result.reason", reason=outcome.reason))
        unmet = outcome.raw.get("unmet")
        if isinstance(unmet, list) and unmet:
            shown = "; ".join(str(item) for item in unmet[:3])
            if len(unmet) > 3:
                shown += f" (+{len(unmet) - 3})"
            lines.append(tr(lang, "result.unmet", items=shown))
        failure_map = outcome.raw.get("failure_map")
        if isinstance(failure_map, dict):
            from ..check_report import render_failure_map

            rendered = render_failure_map(failure_map)
            if rendered:
                lines.extend(rendered.splitlines()[:5])
        if outcome.changed_files:
            shown = ", ".join(outcome.changed_files[:6])
            if len(outcome.changed_files) > 6:
                shown += f" (+{len(outcome.changed_files) - 6})"
            lines.append(tr(lang, "result.changed", files=shown))
        return "\n".join(lines)

    @staticmethod
    def _result_level(outcome: Outcome) -> str:
        if outcome.verified or outcome.reason == "answer":
            return "success"
        if outcome.status in {"failed", "error"}:
            return "error"
        return "warning"

    # ---------------------------------------------------------------- actions

    def action_new_session(self) -> None:
        if not self._idle():
            return
        self.session_id = self._new_session_id()
        self._continue = False
        self._tokens = 0
        self.checkpoint = None
        self.run_state = RunState()
        self._reset_turn()
        self.chat.clear_messages()
        self._add_welcome()
        self._notice(tr(self.language, "notice.new_session", id=self.session_id), "info")
        self._refresh_chrome()
        self._refresh_status()

    def action_clear(self) -> None:
        if not self._idle():
            return
        self._reset_turn()
        self._welcome_card = None
        self.chat.clear_messages()
        self._notice(tr(self.language, "notice.clear"), "info")

    def action_show_reasoning(self) -> None:
        self._cmd_reasoning("")

    def action_stop(self) -> None:
        if self.menu.active:
            self.menu.cancel()
            return
        if self.query_one("#shortcuts", Static).display:
            self._show_shortcuts(False)
            return
        if not self._agent_running or self._stopping:
            return
        self._stopping = True
        self.run_state.state = "stopping"
        self._refresh_status()
        self.run_worker(self.backend.stop, thread=True, group="stop", name="agent-stop")

    def action_toggle_verbose(self) -> None:
        """Ctrl+O: every tool line, past and future, shows all it has."""

        self._verbose = not self._verbose
        for card in self.chat.query(ToolCard):
            card.set_expanded(self._verbose)
        self._refresh_chrome()

    def action_interrupt(self) -> None:
        """Ctrl+C: close what is open, else stop the run, else clear, else quit."""

        if len(self.screen_stack) > 1:
            self.pop_screen()
            return
        if self._prompt_open:
            self._cancel_prompt()
            return
        if self._agent_running:
            self.action_stop()
            return
        if self.menu.active:
            self.menu.cancel()
            return
        if str(self.composer.text):
            self.composer.set_text("")
            return
        self.exit(0)

    async def action_quit(self) -> None:
        if self._agent_running:
            self.backend.stop()
        self.exit(0)

    # ----------------------------------------------------------------- chrome

    def _model_label(self) -> str:
        if self._model is None:
            return ""
        provider = self._model.get("provider_id")
        model = self._model.get("model_id")
        if not isinstance(provider, str) or not isinstance(model, str):
            return ""
        return f"{provider}/{model}"

    def _refresh_chrome(self) -> None:
        """The footer: a hint on the left, the window's facts on the right."""

        lang = self.language
        width = self.size.width or 100
        if self.mode != "build":
            hint, level = tr(lang, "footer.mode", mode=self.mode), "mode"
        else:
            hint, level = tr(lang, "footer.hint"), ""
        if self._verbose:
            hint += " · " + tr(lang, "footer.verbose")
        model = self._model_label()
        if not model and not self._model_loaded:
            model = tr(lang, "status.loading")
        status = self.query_one(StatusLine)
        status.set_hint(hint, level)
        status.set_facts(
            status_segments(
                model=model,
                repository="",
                tokens=self._tokens,
                context_fraction=self.run_state.context_fraction,
                effort=self.effort,
                # The mode is the hint's business; stating it twice is noise.
                mode="build",
                width=max(24, width - 8 - len(hint)),
                language=lang,
            )
        )

    def _refresh_status(self) -> None:
        self._refresh_working()
        self._refresh_chrome()

    def _refresh_working(self) -> None:
        """KaroX's dots and one line saying what is happening right now."""

        line = self.working
        lang = self.language
        if not self._agent_running:
            line.set_working(self._lookup_text)
            return
        state = self.run_state
        if self._stopping:
            text = tr(lang, "working.stopping")
        elif self._running_tools:
            tool, detail = list(self._running_tools.values())[-1]
            text = working_text(tool, detail, lang)
        else:
            text = tr(lang, self._phase_key)
        elapsed = duration_text(state.elapsed(), lang)
        if state.tokens:
            facts = tr(lang, "working.facts", elapsed=elapsed, tokens=short_tokens(state.tokens))
        else:
            facts = tr(lang, "working.facts_quiet", elapsed=elapsed)
        below: list[str] = []
        if self._retry_note:
            below.append(tr(lang, "working.retry", detail=self._retry_note))
        position, total, upcoming = plan_step(self._plan_rows)
        if upcoming:
            below.append(tr(lang, "working.next", item=upcoming, index=position, total=total))
        if self._queue:
            first = " ".join(self._queue[0].split())
            if len(first) > 60:
                first = first[:59] + "…"
            if len(self._queue) > 1:
                below.append(
                    tr(lang, "working.queued_more", text=first, count=len(self._queue) - 1)
                )
            else:
                below.append(tr(lang, "working.queued", text=first))
        line.set_working(
            text,
            facts,
            "\n".join(below),
            "warning" if self._stopping or self._retry_note else "",
        )

    def on_resize(self, event: events.Resize) -> None:
        del event  # the new size is read from self.size
        self._refresh_chrome()

    def _idle(self) -> bool:
        """Refuse a command that would move the session under a live run."""

        if self._agent_running:
            self._notice(tr(self.language, "notice.already_running"), "warning")
            return False
        return True

    def _notice(self, text: str, level: str = "info") -> None:
        if text:
            self.chat.add(Notice(text, level))

    def _result_notice(self, text: str, level: str) -> None:
        # The end of a turn stands one row apart from the last tool line.
        if text:
            notice = Notice(text, level)
            notice.add_class("run-result")
            self.chat.add(notice)

    def _print_block(self, title: str, body: Text | str) -> None:
        # Not ``_print``: Textual's App._print(text, stderr) receives every
        # print() made while the app runs, so shadowing it crashed the TUI.
        self.chat.add(TextBlock(title, body))

    @staticmethod
    def _new_session_id() -> str:
        return f"tui-{int(time.time())}-{uuid.uuid4().hex[:6]}"


def _display_path(path: Path) -> str:
    """The folder the way a shell prompt shows it: under ``~`` when it can."""

    try:
        home = Path.home().resolve()
        relative = path.relative_to(home)
    except (OSError, RuntimeError, ValueError):
        return str(path)
    return "~" if not relative.parts else str(Path("~") / relative)


def _cost_text(costs: dict[str, float]) -> str:
    def amount(value: float) -> str:
        return f"{value:.4f}" if value < 0.01 else f"{value:.2f}"

    return " · ".join(
        f"${amount(value)}" if currency.upper() == "USD" else f"{currency} {amount(value)}"
        for currency, value in sorted(costs.items())
    )


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
            stdin=subprocess.DEVNULL,
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
    has_pytest_tests = bool(
        list(repository.glob("test_*.py"))
        or list((repository / "tests").glob("test_*.py"))
    )
    if has_pytest_tests or (
        (repository / "pyproject.toml").is_file() and (repository / "tests").is_dir()
    ):
        commands.append(("python", "-m", "pytest", "-q"))
    if (repository / "Cargo.toml").is_file():
        commands.append(("cargo", "test"))
    if (repository / "go.mod").is_file():
        commands.append(("go", "test", "./..."))
    return tuple(commands) or (("git", "diff", "--check"),)
