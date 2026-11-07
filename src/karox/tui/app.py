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

from .app_support import (  # noqa: F401
    EFFORT_LEVELS as EFFORT_LEVELS,
    MODES as MODES,
    LANGUAGES as LANGUAGES,
    _REFUSED_WHILE_RUNNING as _REFUSED_WHILE_RUNNING,
    RunState as RunState,
    _display_path as _display_path,
    _cost_text as _cost_text,
    _save_preference as _save_preference,
    _git_branch as _git_branch,
    _detect_verification as _detect_verification,
)
from .app_session import AppSessionMixin
from .app_commands import AppCommandsMixin
from .app_run import AppRunMixin

import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional

from rich.text import Text
from textual import events, on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.theme import Theme
from textual.widgets import Input, Static, TextArea

from . import commands as slash
from . import prefs
from .backend import CLIBackend
from .i18n import detect_language, normalise_language, tr
from .protocol import SessionState
from .widgets import (
    MUTED,
    AssistantMessage,
    ChatInput,
    ChatLog,
    InlineMenu,
    SecretPrompt,
    StatusLine,
    ToolCard,
    WelcomeCard,
    WorkingLine,
)

#: Commands that would move the session or the screen under a live run.




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


class KaroxApp(App[int], AppSessionMixin, AppCommandsMixin, AppRunMixin):
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
        #: A task submitted before any check was approved; it starts once one is.
        self._verify_task: str | None = None
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



    # ----------------------------------------------------------- background







    # ---------------------------------------------------------------- welcome




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
            "verify": self._verify_chosen,
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
        if event.kind == "verify" and self._verify_task:
            # The task is not lost: it goes back to the composer as typed.
            self.composer.set_text(self._verify_task)
            self._verify_task = None
            self._notice(tr(self.language, "notice.cancelled"), "info")
        elif event.kind != "commands":
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




















    # ------------------------------------------------------- model & provider















    # --------------------------------------------------------------- sessions





    # ---------------------------------------------------------------- skills



    # ------------------------------------------------------------------- MCP










    # -------------------------------------------------------------- settings



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


    # ------------------------------------------------------------------- run















    # ---------------------------------------------------------------- actions







    async def action_quit(self) -> None:
        if self._agent_running:
            self.backend.stop()
        self.exit(0)

    # ----------------------------------------------------------------- chrome





    def on_resize(self, event: events.Resize) -> None:
        del event  # the new size is read from self.size
        self._refresh_chrome()





    @staticmethod
    def _new_session_id() -> str:
        return f"tui-{int(time.time())}-{uuid.uuid4().hex[:6]}"









