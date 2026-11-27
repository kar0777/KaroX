"""Interactive terminal UI: an inline chat on the shared core (M3).

The chat streams into the terminal's normal scrollback (no full-screen takeover), so
output stays searchable and copyable after the session ends. Line editing and history
come from prompt_toolkit (a dependency); if it cannot be imported the chat falls
back to plain ``input()``. All I/O goes through two injected callables, so the app is
tested without a terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import stat
import sys
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any, Protocol

from karox6 import __version__
from karox6.chat_features import (
    INIT_PROMPT,
    McpState,
    closest_command,
    diff_preview,
    find_images,
    last_code_block,
    mcp_report,
    picture_chip,
    store_image,
)
from karox6.core import events as ev
from karox6.core.digest import brief, clip, explain_error, is_tool_failure, outcome
from karox6.tools.jobs import incomplete_utf8_tail
from karox6.tui_markdown import Markdown
from karox6.tui_text import (
    Palette,
    StatusSnapshot,
    _box,
    _git_branch,
    _glyphs,
    _k,
    _plain_text,
    banner_text,
    clip_cells,
)

if TYPE_CHECKING:
    from types import TracebackType

    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer
    from prompt_toolkit.history import History
    from prompt_toolkit.input import Input
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.output import Output

    from karox6.bridge import Bridge
    from karox6.core.checkpoint import Checkpoints
    from karox6.core.config import SessionConfig
    from karox6.core.messages import Message, ToolCall
    from karox6.core.session import Session
    from karox6.tools.base import ApprovalRequest, Tool
    from karox6.tui_theme import Theme


class TurnView(Protocol):
    """Live turn presentation used by ChatApp._run_with_display."""

    async def __aenter__(self) -> TurnView: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    def write(self, text: str) -> None: ...

    def pause(self) -> contextlib.AbstractAsyncContextManager[None]: ...


ReadLine = Callable[[str], Awaitable[str]]  # raises EOFError at end of input
Write = Callable[[str], None]
DisplayFactory = Callable[[Write, Callable[[], str], Callable[[], None]], "TurnView | None"]

APPROVAL_MODES = ("ask", "allow", "deny")
_SESSION_COMMANDS = frozenset(
    (
        "/diff",
        "/status",
        "/cost",
        "/compact",
        "/new",
        "/sessions",
        "/resume",
        "/key",
        "/model",
        "/models",
        "/provider",
        "/effort",
        "/setup",
        "/bridge",
        "/undo",
        "/redo",
        "/fork",
        "/mcp",
        "/paste",
        "/export",
    )
)
DIFF_CAP = 40_000
_EXIT_LINE = re.compile(r"^\[exit code:\s*-?\d+")
_EXIT_CODE = re.compile(r"^\[exit code:\s*(-?\d+)", re.MULTILINE)
_CLEAR_SCREEN = "\x1b[2J\x1b[H"
MENTION_MAX_BYTES = 64 * 1024
REPLAY_TURNS = 10  # exchanges a resumed session shows
MODELS_SHOWN = 60  # /models lines
MENTION_TOTAL_BYTES = 128 * 1024


@dataclass(frozen=True, slots=True)
class CommandSpec:
    name: str
    usage: str
    summary: str
    group: str


COMMANDS: tuple[CommandSpec, ...] = (
    CommandSpec("/new", "/new", "start a fresh session", "Session"),
    CommandSpec("/clear", "/clear", "clear the screen (keeps current session)", "Session"),
    CommandSpec("/sessions", "/sessions", "list recent sessions for this workspace", "Session"),
    CommandSpec("/resume", "/resume [ID|last]", "continue a saved session", "Session"),
    CommandSpec(
        "/fork", "/fork", "branch off: a copy of this conversation to try another way", "Session"
    ),
    CommandSpec(
        "/undo", "/undo", "take back the last turn: its file changes and messages", "Session"
    ),
    CommandSpec("/redo", "/redo", "put back the turn that /undo took away", "Session"),
    CommandSpec("/quit", "/quit", "leave the chat (/exit, /q, Ctrl+D)", "Session"),
    CommandSpec(
        "/diff",
        "/diff",
        "coloured workspace changes (git diff and untracked)",
        "Workspace & Context",
    ),
    CommandSpec(
        "/status", "/status", "session, model, tokens, context fill and cost", "Workspace & Context"
    ),
    CommandSpec(
        "/cost", "/cost", "detailed token breakdown and session cost", "Workspace & Context"
    ),
    CommandSpec(
        "/compact",
        "/compact",
        "compact conversation history to free context",
        "Workspace & Context",
    ),
    CommandSpec(
        "/init",
        "/init [FOCUS]",
        "write AGENTS.md: this project's commands, layout and conventions",
        "Workspace & Context",
    ),
    CommandSpec(
        "/paste",
        "/paste",
        "attach the picture in the clipboard to your next message (Alt+V, Ctrl+V)",
        "Workspace & Context",
    ),
    CommandSpec(
        "/copy",
        "/copy [N|code]",
        "copy the last answer to the clipboard (N-th back, or its last code block)",
        "Workspace & Context",
    ),
    CommandSpec(
        "/export",
        "/export [FILE]",
        "save the conversation as Markdown (default: karox-<session>.md here)",
        "Workspace & Context",
    ),
    CommandSpec(
        "/model",
        "/model [NAME|NUMBER]",
        "the model and the provider's list; switch to another",
        "Model & Setup",
    ),
    CommandSpec("/models", "/models [TEXT]", "the models this provider offers", "Model & Setup"),
    CommandSpec(
        "/provider",
        "/provider [NAME]",
        "list the saved providers or switch to one",
        "Model & Setup",
    ),
    CommandSpec(
        "/reasoning", "/reasoning [show|hide]", "show or hide model reasoning", "Model & Setup"
    ),
    CommandSpec(
        "/effort",
        "/effort [low|medium|high|xhigh|max|off]",
        "show or change how hard the model reasons",
        "Model & Setup",
    ),
    CommandSpec("/key", "/key", "paste the API key (hidden; saved encrypted)", "Model & Setup"),
    CommandSpec("/setup", "/setup", "choose provider, model and key again", "Model & Setup"),
    CommandSpec(
        "/approve",
        "/approve ask|allow|deny",
        "dangerous commands: ask, allow, or deny",
        "Tools & Bridge",
    ),
    CommandSpec(
        "/bridge",
        "/bridge [agent|url|log|stop]",
        "connect ChatGPT/Claude via Tailscale Funnel MCP bridge",
        "Tools & Bridge",
    ),
    CommandSpec(
        "/mcp", "/mcp [SERVER]", "MCP servers: state, tools, how to add one", "Tools & Bridge"
    ),
    CommandSpec(
        "/update",
        "/update [check|force]",
        "install the newest KaroX (restart to use it)",
        "Model & Setup",
    ),
    CommandSpec("/help", "/help", "show commands and keyboard shortcuts", "Tools & Bridge"),
)

_COMMAND_ALIASES: dict[str, str] = {
    "/exit": "leave the chat (same as /quit)",
    "/q": "leave the chat (same as /quit)",
}

_APPROVE_ARGS: tuple[tuple[str, str], ...] = (
    ("ask", "ask before running dangerous commands"),
    ("allow", "run dangerous commands without asking"),
    ("deny", "refuse dangerous commands automatically"),
)

_BRIDGE_ARGS: tuple[tuple[str, str], ...] = (
    ("direct", "let the client model use KaroX tools"),
    ("agent", "hand tasks to KaroX's own model"),
    ("url", "show the secret no-auth connector link"),
    ("log", "show recent bridge requests"),
    ("stop", "stop the bridge and remove its Tailscale route"),
)

_UPDATE_ARGS: tuple[tuple[str, str], ...] = (
    ("check", "only say whether a newer version is there"),
    ("force", "build and install even when up to date"),
)

_EFFORT_ARGS: tuple[tuple[str, str], ...] = (
    ("low", "quick answers, little reasoning"),
    ("medium", "balanced"),
    ("high", "think harder (slower, more tokens)"),
    ("xhigh", "harder still, where the model has it"),
    ("max", "the most the model offers"),
    ("off", "the provider's default"),
)
_REASONING_ARGS: tuple[tuple[str, str], ...] = (
    ("show", "display model reasoning as it streams"),
    ("hide", "hide model reasoning output"),
)

_HELP_GROUPS = ("Session", "Workspace & Context", "Model & Setup", "Tools & Bridge")


_HELP_SHORTCUTS: tuple[tuple[str, str], ...] = (
    ("@path", "mention a workspace file to attach it to your message"),
    ("@shot.png / Ctrl+V", "attach a picture: its path, or the one in the clipboard (Alt+V)"),
    ("mouse drag", "select conversation text; letting go copies it (see /copy)"),
    ("Enter / Alt+Enter", "send message / insert newline (Ctrl+J or trailing \\ also work)"),
    ("Tab", "complete /commands, arguments and @file paths"),
    ("Ctrl+R", "search persistent input history"),
    ("PgUp / PgDn / End", "scroll conversation (mouse wheel too; End / Ctrl+End jumps to bottom)"),
    ("Esc or Ctrl+C", "stop the running turn"),
    ("Ctrl+D", "exit chat at an empty prompt"),
)


def _format_help(paint: Palette, mode: str) -> str:
    width = max(len(c.usage) for c in COMMANDS) + 2
    sections: list[str] = []
    for group in _HELP_GROUPS:
        rows = [paint("bold", group)]
        for cmd in COMMANDS:
            if cmd.group != group:
                continue
            note = f"{cmd.summary} (now: {mode})" if cmd.name == "/approve" else cmd.summary
            rows.append(f"  {paint('cyan', f'{cmd.usage:<{width}}')}{note}")
        sections.append("\n".join(rows))
    shortcuts = [paint("bold", "Input & Shortcuts")]
    for key, desc in _HELP_SHORTCUTS:
        shortcuts.append(f"  {paint('cyan', f'{key:<{width}}')}{desc}")
    sections.append("\n".join(shortcuts))
    return "\n\n".join(sections)


_MENTION_RE = re.compile(r'(?:^|(?<=[\s(\[{]))@(?:"([^"]+)"|([^\s"]+))')
_MENTION_PREFIX_RE = re.compile(r'(?:^|(?<=[\s(\[{]))@(?:"([^"]*)|([^\s"]*))$')
_MENTION_BINARY_PROBE = 8_000
_TRAILING_PUNCT = ",.;:)]}?!"


@dataclass(slots=True)
class MentionResult:
    text: str
    attached: list[tuple[str, Path, int, bool]]  # (rel_path, abs_path, bytes, truncated)
    warnings: list[str]


def _format_bytes(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _resolve_mention(raw: str, root: Path, *, quoted: bool) -> tuple[Path | None, str | None]:
    """Return (path, warning) for a raw ``@path`` token; (None, None) to ignore."""
    candidates = [raw]
    stripped = raw.rstrip(_TRAILING_PUNCT)
    if not quoted and stripped and stripped != raw:
        candidates.append(stripped)
    for cand in candidates:
        norm = cand.replace("\\", "/")
        path = Path(norm)
        if PureWindowsPath(norm).anchor or ".." in path.parts or (os.name == "nt" and ":" in norm):
            return None, f"skipped @{cand} (outside workspace)"
        try:
            resolved = (root / path).resolve()
            if not resolved.is_relative_to(root):
                return None, f"skipped @{cand} (outside workspace)"
            if resolved.exists():
                return resolved, None
        except (OSError, ValueError) as exc:
            return None, f"skipped @{cand} ({exc})"
    return None, None


def expand_file_mentions(text: str, workspace: Path) -> MentionResult:
    """Attach referenced workspace files (``@path`` or ``@"path with spaces"``) to ``text``."""
    from html import escape

    root = workspace.resolve()
    seen: set[str] = set()
    blocks: list[str] = []
    attached: list[tuple[str, Path, int, bool]] = []
    warnings: list[str] = []
    remaining = MENTION_TOTAL_BYTES

    for match in _MENTION_RE.finditer(text):
        quoted = match.group(1)
        raw = quoted if quoted is not None else match.group(2)
        resolved, warning = _resolve_mention(raw, root, quoted=quoted is not None)
        if warning:
            warnings.append(warning)
            continue
        if resolved is None:
            continue
        rel = resolved.relative_to(root).as_posix()
        key = os.path.normcase(rel)
        if key in seen:
            continue
        seen.add(key)
        if remaining <= 0:
            warnings.append(f"skipped @{rel} (attachment budget reached)")
            continue
        cap = min(MENTION_MAX_BYTES, remaining)
        try:
            info = resolved.stat()
            if stat.S_ISDIR(info.st_mode):
                warnings.append(f"skipped @{rel} (directory)")
                continue
            if not stat.S_ISREG(info.st_mode):
                warnings.append(f"skipped @{rel} (not a regular file)")
                continue
            with resolved.open("rb") as stream:
                size = os.fstat(stream.fileno()).st_size
                data = stream.read(max(cap + 1, _MENTION_BINARY_PROBE))
        except OSError as exc:
            warnings.append(f"skipped @{rel} ({exc})")
            continue
        if b"\0" in data[:_MENTION_BINARY_PROBE]:
            warnings.append(f"skipped @{rel} (binary file)")
            continue
        size = max(size, len(data))
        truncated = len(data) > cap
        chunk = data[:cap]
        if truncated:  # do not cut a UTF-8 character in two (it would show as U+FFFD)
            chunk = chunk[: len(chunk) - incomplete_utf8_tail(chunk)]
        remaining -= len(chunk)
        included = len(chunk)
        if chunk.startswith(b"\xef\xbb\xbf"):
            chunk = chunk[3:]
        body = chunk.decode("utf-8", errors="replace").replace("\r\n", "\n")
        note = (
            f"\n... [truncated to {_format_bytes(included)} of {_format_bytes(size)}; "
            "use read tool for the rest]"
            if truncated
            else ""
        )
        path_attr = escape(rel, quote=True)
        blocks.append(f'<attached_file path="{path_attr}">\n{body}{note}\n</attached_file>')
        attached.append((rel, resolved, size, truncated))

    if not blocks:
        return MentionResult(text=text, attached=[], warnings=warnings)
    return MentionResult(
        text=text.rstrip() + "\n\n" + "\n\n".join(blocks),
        attached=attached,
        warnings=warnings,
    )


def _edit_diff_stats(name: str, arguments: str) -> tuple[int, int] | None:
    """Return (added_lines, removed_lines) for edit/apply_patch calls when parseable."""
    base = name.rsplit("/", 1)[-1]
    try:
        data = json.loads(arguments)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if base == "edit":
        old = data.get("old_string")
        new = data.get("new_string")
        if isinstance(old, str) and isinstance(new, str):
            removed = len(old.splitlines()) if old else 0
            added = len(new.splitlines()) if new else 0
            return added, removed
    if base == "apply_patch":
        patch = data.get("patch") or data.get("input")
        if isinstance(patch, str):
            added = sum(
                1 for ln in patch.splitlines() if ln.startswith("+") and not ln.startswith("+++")
            )
            removed = sum(
                1 for ln in patch.splitlines() if ln.startswith("-") and not ln.startswith("---")
            )
            return added, removed
    return None


def _diff_summary(diff_text: str) -> tuple[int, int, int]:
    """Return (files_changed, added_lines, removed_lines) from a unified git diff."""
    files = 0
    added = 0
    removed = 0
    in_hunk = False
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            files += 1
            in_hunk = False
        elif line.startswith("@@ "):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            added += 1
        elif in_hunk and line.startswith("-"):
            removed += 1
    return files, added, removed


@dataclass(slots=True)
class _TurnRecord:
    """One finished turn, as /undo needs it."""

    prompt: str
    anchor: Any  # the conversation's last message before the turn (None: it was empty)
    before: str | None  # workspace snapshot before the turn (None: not recorded)
    after: asyncio.Future[str | None] | None  # and after it (taken in the background)
    session_id: str


UNDO_DEPTH = 50  # turns /undo can step back through
SNAPSHOT_WAIT_S = 5.0  # a turn waits this long for its "before" snapshot, then starts
SNAPSHOT_AFTER_WAIT_S = 3.0  # and this long, when it ends, for the snapshot of its result


class _PromptInterrupt(KeyboardInterrupt):
    def __init__(self, empty: bool) -> None:
        self.empty = empty


def _default_copy_text(text: str) -> str:
    """How ``/copy`` copies when no clipboard writer was injected: the system
    clipboard; OSC 52 is sent to stdout only when it is a terminal."""
    from karox6.clipboard import copy_text

    def emit(seq: str) -> None:
        sys.stdout.write(seq)
        sys.stdout.flush()

    return copy_text(text, emit_osc52=emit if sys.stdout.isatty() else None)


class ChatApp:
    def __init__(
        self,
        *,
        config: SessionConfig,
        provider: Any,
        read_line: ReadLine,
        write: Write,
        read_answer: ReadLine | None = None,
        approval_mode: str = "ask",
        color: bool = False,
        encoding: str | None = "utf-8",
        extra_tools: list[Tool] | None = None,
        session_kwargs: dict[str, Any] | None = None,
        bridge_agent_args: list[str] | None = None,
        bridge_env: dict[str, str] | None = None,
        read_secret: ReadLine | None = None,
        initialize: Callable[[ChatApp], Awaitable[dict[str, Any]]] | None = None,
        before_turn: Callable[[ChatApp], Awaitable[None]] | None = None,
        display_factory: DisplayFactory | None = None,
        columns: Callable[[], int] | None = None,
        snapshots: bool = False,
        prefill: Callable[[str], None] | None = None,
        theme: Theme | None = None,
        legacy_glyphs: bool = False,
        write_block: Callable[[str, str], None] | None = None,
        live: Callable[[str], None] | None = None,
        copy_to_clipboard: Callable[[str], str] | None = None,
    ) -> None:
        if approval_mode not in APPROVAL_MODES:
            raise ValueError(f"approval mode must be one of {APPROVAL_MODES}")
        self.config = config
        self.provider = provider
        self.read_line = read_line
        self._read_answer = read_answer or read_line
        self.write = write
        self.approval_mode = approval_mode
        self.paint = Palette(color, theme)
        self.glyph = _glyphs(encoding, legacy_glyphs)
        self._write_block = write_block  # (collapsed, expanded): Ctrl+O switches them
        self._live = live  # shows the line of the answer still being streamed
        self._copy_text = copy_to_clipboard or _default_copy_text
        self._md = Markdown(self.paint, self.glyph, columns)
        self._last_turn_s: float | None = None  # how long the last turn took
        self.extra_tools = list(extra_tools or [])
        self.show_reasoning = False
        self._stream: str | None = None  # "text" | "reasoning" while a delta stream is open
        self._streamed_text = False
        self._stream_newline = False
        self._text_before_tools = False  # the model wrote text: a gap before tool lines
        self._initialize = initialize
        self._before_turn = before_turn
        self._session_task: asyncio.Task[Session] | None = None
        self._display_factory = display_factory
        self._columns = columns  # terminal width in cells, for text that must not wrap
        self._display: TurnView | None = None
        self._branch: str | None = None
        self._turn_started = 0.0
        self._phase = ""
        self._active_tools: dict[str, ev.ToolStarted] = {}
        self._had_tools = False
        self._error_shown = False  # this turn's error line is on screen
        self.mcp: McpState | None = None  # set when the MCP servers were connected
        self.snapshots = snapshots  # record the workspace around each turn (for /undo)
        self._prefill = prefill  # puts text into the input line (an undone message)
        self._fork_source: dict[str, Any] | None = None  # --fork: history to start from
        self.replay_on_start = False  # --resume: show the saved conversation at the start
        self.key_name = ""  # the saved key of this provider (credentials-<name>)
        # The full-screen list picker (title, items, current) -> choice or None; without
        # it /model and /provider print numbered lists.
        self.picker: Callable[..., Awaitable[str | None]] | None = None
        self._model_list: tuple[Any, list[str] | None] | None = None  # server, its models
        self._shown_models: list[str] = []  # what a number in /model picks from
        self._model_notes: dict[str, str] = {}  # model -> "1M · pictures" (from the list)
        self._checkpoints: Checkpoints | None = None
        self._turns: list[_TurnRecord] = []  # what /undo can take back, oldest first
        self._undone: list[tuple[_TurnRecord, list[Message]]] = []  # what /redo can put back
        self._pending_images: list[Path] = []  # pictures waiting for the next message
        self._next_images: list[Path] | None = None  # set: a sent message's own pictures
        # Pictures a command attached (/paste, or a sent command's draft pictures): they go
        # with the next message whatever its draft holds.
        self._carried: list[Path] = []
        self._warmup: asyncio.Future[str | None] | None = None
        self.session: Session
        if initialize is None:
            self.session = self._new_session(**(session_kwargs or {}))
        self._bridge_instance: Bridge | None = None
        self.bridge_agent_args = list(bridge_agent_args or [])
        self.bridge_env = dict(bridge_env or {})
        self._read_secret = read_secret or self._read_answer
        self._question_lock = asyncio.Lock()
        self._turn_running = False
        self.update_source: Any = None  # karox6.update.Source (None: the default)
        self.update_note = ""  # "6.0.0.dev41" when the background check found it
        self._updating = False

    # -- lifecycle ------------------------------------------------------------------

    @property
    def bridge(self) -> Bridge:
        from karox6.bridge import Bridge

        if self._bridge_instance is None:
            self._bridge_instance = Bridge(self.config.home)
        return self._bridge_instance

    async def _ensure_session(self) -> None:
        if self._initialize is not None:
            if self._session_task is None:
                kwargs = await self._initialize(self)
                self._session_task = asyncio.create_task(
                    asyncio.to_thread(self._new_session, **kwargs)
                )
            try:
                self.session = await asyncio.shield(self._session_task)
            except Exception:
                self._session_task = None
                raise
            self._session_task = None
            self._initialize = None
            if self._fork_source is not None:
                source, self._fork_source = self._fork_source, None
                self.session.take_history(
                    source["messages"], source["usage"], source["cost_usd"], source["session_id"]
                )
                self.write(
                    f"forked from {source['session_id']}: {len(self.session.messages)} messages, "
                    f"now in {self.session.session_id}\n"
                )

    def _new_session(self, **kwargs: Any) -> Session:
        from karox6.core.session import Session

        return Session(
            self.config,
            self.provider,
            approver=self._approve,
            extra_tools=self.extra_tools,
            **kwargs,
        )

    async def run(self, *, banner: bool = True) -> int:
        """The chat loop. ``banner=False``: the caller has already shown the welcome card
        (the full-screen app puts it into its very first frame)."""
        if banner:
            self.write(self._banner())
        if self.replay_on_start:
            try:
                await self._ensure_session()
            except (ValueError, FileNotFoundError) as exc:  # an unknown id: said once here
                self.write(self.paint("red", str(exc)) + "\n")
            else:
                self.replay_history()
        if self.snapshots:
            # The first snapshot reads the whole project: start it while the user types.
            self._warmup = asyncio.ensure_future(self._snapshot())
        exit_armed = False
        while True:
            try:
                text = (await self._read_prompt()).strip()
            except EOFError:
                break
            except KeyboardInterrupt as exc:
                empty = not isinstance(exc, _PromptInterrupt) or exc.empty
                if empty and exit_armed:
                    break
                exit_armed = empty
                if empty:
                    self.write("Press Ctrl+C again to exit.\n")
                continue
            exit_armed = False
            if self._next_images is not None and (not text or text.startswith("/")):
                # A command takes no pictures: they go with the next message.
                self._carried += self._next_images
                self._next_images = None
            if not text:
                continue
            if text.startswith("/"):
                if await self.command(text) == "quit":
                    break
                continue
            exit_armed = await self.turn(text) == "cancelled"
        await self.close()
        self.write("\n")
        return 0

    async def close(self) -> None:
        """Stop what the chat started beside it (the bridge and its Tailscale route)."""
        if self._updating:
            from karox6 import update

            update.abort()  # kill the update's clone/pip instead of hanging on its timeouts
        if self._session_task is not None:
            with contextlib.suppress(Exception):
                await self._session_task
            self._session_task = None
        if self._bridge_instance is not None and self._bridge_instance.running():
            self.write("stopping the bridge...\n")
            await asyncio.to_thread(self._bridge_instance.stop)
        if self._checkpoints is not None:
            self._checkpoints.abort()  # a snapshot still being taken must not delay the exit

    async def _read_prompt(self) -> str:
        lines: list[str] = []
        while True:
            try:
                line = await self.read_line(self.glyph["more"] if lines else self.glyph["prompt"])
            except KeyboardInterrupt as exc:
                if lines:
                    raise _PromptInterrupt(empty=False) from exc
                raise
            if line.endswith("\\"):
                lines.append(line[:-1])
                continue
            lines.append(line)
            return "\n".join(lines)

    def _branch_name(self) -> str:
        if self._branch is None:
            self._branch = _git_branch(Path(self.config.workspace))
        return self._branch

    def status_snapshot(self) -> StatusSnapshot:
        """Current state for the full-screen status bar (never forces session init)."""
        cfg = self.config
        ws = Path(cfg.workspace)
        has_session = self._initialize is None and hasattr(self, "session")
        if has_session:
            s = self.session
            u = s.usage
            total_tokens = u.input_tokens + u.output_tokens
            used = s.context_tokens() if s.messages else 0
            window = cfg.effective_window(s.window_hint())
            pct = min(round(used / window * 100), 100) if window else 0
            session_id: str | None = s.session_id
            cost = s.cost_usd if cfg.price else None
        else:
            cost = None
            total_tokens = 0
            used = 0
            window = cfg.effective_window(None)
            pct = 0
            session_id = None
        return StatusSnapshot(
            model=cfg.model,
            provider=cfg.provider_name,
            workspace_name=ws.name or str(ws),
            branch=self._branch_name(),
            approval_mode=self.approval_mode,
            running=self._turn_running,
            activity=self._progress() if self._turn_running else "",
            total_tokens=total_tokens,
            context_used=used,
            context_window=window,
            context_pct=pct,
            session_id=session_id,
            cost_usd=cost,
            turn_s=(time.monotonic() - self._turn_started)
            if self._turn_running
            else self._last_turn_s,
            update=self.update_note,
        )

    def exit_summary(self) -> str:
        """One-line summary printed to the normal screen after leaving full-screen TUI."""
        if self._initialize is None and hasattr(self, "session"):
            sid = self.session.session_id
            count = len(self.session.messages)
            if count:
                return f"Session {sid} ({count} messages) — resume: karox6 chat --resume {sid}"
            return f"Session {sid} (empty) — resume previous: karox6 chat --resume last"
        return "KaroX exited — resume previous: karox6 chat --resume last"

    def _banner(self) -> str:
        """The welcome card: what runs where, and the keys a new user needs first."""
        cfg = self.config
        return banner_text(
            version=__version__,
            model=cfg.model,
            provider=cfg.provider_name,
            workspace=str(cfg.workspace),
            branch=self._branch_name(),
            approval_mode=self.approval_mode,
            paint=self.paint,
            glyph=self.glyph,
            max_width=self._columns() if self._columns is not None else None,
        )

    # -- turns ----------------------------------------------------------------------

    async def turn(self, text: str, *, label: str | None = None) -> str:
        """Run one user turn with interruptible terminal output. ``label`` is what the
        turn is called in /undo when ``text`` is a built-in prompt (``/init``)."""
        phase = "Preparing session" if self._initialize is not None else "Model thinking"
        return await self._run_with_display(lambda: self._consume(text, label), phase)

    async def _run_with_display(self, action: Callable[[], Awaitable[str]], phase: str) -> str:
        self._turn_started = time.monotonic()
        self._phase = phase
        self._active_tools.clear()
        self._had_tools = False
        self._streamed_text = False
        ready = asyncio.Event()

        async def consume() -> str:
            await ready.wait()
            return await action()

        worker = asyncio.create_task(consume())

        def interrupt() -> None:
            # Repeated keys during cleanup must not cancel the cleanup itself.
            if not worker.done() and not worker.cancelling():
                self._phase = "Stopping"
                self._active_tools.clear()
                worker.cancel()

        write = self.write
        restore = _trap_sigint(interrupt)
        self._turn_running = True
        try:
            display = (
                self._display_factory(write, self._progress, interrupt)
                if self._display_factory
                else None
            )
            async with display if display is not None else contextlib.nullcontext():
                if display is None:
                    self.write(f"  {self._phase}...\n")
                else:
                    self._display = display
                    self.write = display.write
                ready.set()
                try:
                    return await asyncio.shield(worker)
                except asyncio.CancelledError:
                    current = asyncio.current_task()
                    if current is not None and current.cancelling():
                        current.uncancel()
                    interrupt()
                    with contextlib.suppress(asyncio.CancelledError):
                        await worker
                    self._end_stream()
                    self.write(self.paint("warning", f"{self.glyph['warn']} stopped") + "\n")
                    return "cancelled"
        finally:
            self._turn_running = False
            self._display = None
            self.write = write
            try:
                if not worker.done():
                    interrupt()
                    with contextlib.suppress(asyncio.CancelledError):
                        await worker
            finally:
                restore()

    def activity(self) -> tuple[str, float]:
        """What the running turn is doing now, and for how long it has run."""
        elapsed = time.monotonic() - self._turn_started
        phase = self._phase
        if self._active_tools:
            calls = list(self._active_tools.values())
            names = (" ".join(_plain_text(call.name).split())[:80] for call in calls)
            phase = "Running " + ", ".join(dict.fromkeys(names))
            if len(calls) == 1:
                target = _plain_text(brief(calls[0].arguments, 60))
                phase += f" {target}" if target else ""
        return phase, elapsed

    def _progress(self) -> str:
        phase, elapsed = self.activity()
        spin = self.glyph.get("spin", "|/-\\")
        spinner = spin[int(elapsed * 10) % len(spin)]
        return f"{spinner} {elapsed:.1f}s  {phase}"

    async def _consume(self, text: str, label: str | None = None) -> str:
        await self._ensure_session()
        if self._before_turn is not None:
            await self._before_turn(self)
            self._before_turn = None
        prompt = label or text
        images: list[dict[str, str]] = []
        if label is None:
            text, images = self._attach_images(text)
        if label is None and _MENTION_RE.search(text):
            mention = await asyncio.to_thread(expand_file_mentions, text, self.session.workspace)
            for warning in mention.warnings:
                self.write(self.paint("yellow", f"  {_plain_text(warning)}") + "\n")
            items = []
            for rel, path, size, truncated in mention.attached:
                if not truncated:
                    self.session.files.observe(path)
                size_label = f"{_format_bytes(size)}{', truncated' if truncated else ''}"
                items.append(f"{_plain_text(rel)} ({size_label})")
            if items:
                self.write(self.paint("dim", "  attached: " + ", ".join(items)) + "\n")
            text = mention.text
        self._phase = "Model thinking"
        status = "error"
        count = 0
        record = await self._record_start(prompt)
        recorded = False
        try:
            async with contextlib.aclosing(self.session.run(text, images)) as events:
                async for event in events:
                    if isinstance(event, ev.RunFinished):
                        status = event.status
                        # Before the closing line is printed: "[completed]" on screen
                        # means the turn is over, so a Ctrl+C after it is for the prompt.
                        self._phase = "Finishing"
                        recorded = True
                        await self._record_end(record)
                        if status == "cancelled":
                            continue  # the turn prints its stop marker after cleanup
                    self.render(event)
                    count += 1
                    if count % 32 == 0:
                        await asyncio.sleep(0)
        finally:
            if not recorded:  # stopped or failed part-way: recorded in the background
                self._record_close(record)
        return status

    # -- pictures -------------------------------------------------------------------

    def _attach_images(self, text: str) -> tuple[str, list[dict[str, str]]]:
        """Pictures for this message: the ones waiting from /paste, then the ones its
        text names. Each is copied into the session; the text keeps an [image N] mark."""
        if self._next_images is not None:  # its own pictures: the draft's stay put
            pending, self._next_images = self._next_images, None
        else:
            pending, self._pending_images = self._pending_images, []
        pending, self._carried = [*self._carried, *pending], []
        found = find_images(text, self.session.workspace, already=len(pending))
        for warning in found.warnings:
            self.write(self.paint("yellow", f"  {_plain_text(warning)}") + "\n")
        images: list[dict[str, str]] = []
        labels: list[str] = []
        base = sum(len(m.images) for m in self.session.messages)
        for path in [*pending, *found.paths]:
            try:
                labels.append(picture_chip(path, len(labels) + 1))
                images.append(store_image(self.session.store.dir, path, base + len(images) + 1))
            except OSError as exc:
                labels.pop()
                self.write(self.paint("yellow", f"  skipped {path.name} ({exc})") + "\n")
        if labels:
            self.write(self.paint("dim", "  " + " ".join(labels)) + "\n")
        return found.text, images

    async def paste_image(self, *, to_draft: bool = True) -> None:
        """Alt+V / Ctrl+V (``to_draft``) or ``/paste``: keep the clipboard's picture for
        the next message."""
        from karox6.clipboard import ClipboardError, grab_image, in_daemon_thread

        await self._ensure_session()
        number = len(self._pending_images) + len(self._carried) + 1
        stamp = time.strftime("%H%M%S")
        target = (
            self.session.store.dir
            / "images"
            / f"clipboard-{stamp}-{number}-{uuid.uuid4().hex[:8]}.png"
        )
        try:
            path = await in_daemon_thread(grab_image, target)
        except ClipboardError as exc:
            self.write(self.paint("yellow", f"  {exc}") + "\n")
            return
        except OSError as exc:
            self.write(self.paint("red", f"  cannot read the clipboard: {exc}") + "\n")
            return
        (self._pending_images if to_draft else self._carried).append(path)
        if self.session.model().vision is False:
            self.write(
                self.paint("yellow", f"  {self.config.model} takes no pictures: ")
                + self.paint("yellow", "it gets a note instead (models.json in the KaroX home)")
                + "\n"
            )
        self.write(
            self.paint("dim", f"  {picture_chip(path, number)} — goes with your ")
            + self.paint("dim", "next message (/paste or Ctrl+V adds another)")
            + "\n"
        )

    # -- clipboard ---------------------------------------------------------------------

    def images_for_next(self, images: list[Path]) -> None:
        """The line read next was sent with these pictures: it takes them instead of
        the ones in the draft being typed now."""
        self._next_images = list(images)

    @property
    def pending_images(self) -> list[Path]:
        """The pictures waiting to go with the next message (the UI shows their chips)."""
        return self._pending_images

    @property
    def carried_images(self) -> list[Path]:
        """Pictures a command attached; they go with the next message too."""
        return self._carried

    async def _copy(self, arg: str) -> None:
        """``/copy``, ``/copy code``, ``/copy N``: an answer or its last code block
        onto the system clipboard (drag-selecting text copies any piece of it)."""
        from karox6.clipboard import ClipboardError, in_daemon_thread

        session = getattr(self, "session", None)
        answers = [
            m.content.rstrip()
            for m in (session.messages if session is not None else [])
            if m.role == "assistant" and m.content.strip()
        ]
        low = arg.lower()
        if low == "code":
            if not answers:
                self.write("nothing to copy yet\n")
                return
            text = last_code_block(answers[-1])
            if text is None:
                self.write("the last answer has no code block\n")
                return
            what = "code block"
        else:
            if low and not (low.isascii() and low.isdigit() and len(low) <= 9):
                self.write("usage: /copy, /copy code or /copy N\n")
                return
            n = int(low) if low else 1
            if n < 1 or n > len(answers):
                if answers:
                    self.write(f"there is no answer {n} (only {len(answers)} so far)\n")
                else:
                    self.write("nothing to copy yet\n")
                return
            text = answers[-n]
            what = f"answer {n}" if n > 1 else "answer"
        try:
            how = await in_daemon_thread(self._copy_text, text)
        except ClipboardError as exc:
            self.write(self.paint("yellow", f"  {exc}") + "\n")
            return
        via = " via OSC 52" if how == "osc52" else ""
        self.write(self.paint("dim", f"  copied {len(text)} chars ({what}){via}") + "\n")

    # -- undo -----------------------------------------------------------------------

    def _store(self) -> Checkpoints:
        from karox6.core.checkpoint import Checkpoints

        if self._checkpoints is None:
            self._checkpoints = Checkpoints(self.config.home, Path(self.config.workspace))
        return self._checkpoints

    async def _snapshot(self, wait: float | None = None) -> str | None:
        """The workspace recorded now; None when snapshots are off or unavailable, or
        when ``wait`` seconds were not enough (a first snapshot of a huge project): the
        recording then goes on in its thread and the next one is quick."""
        if not self.snapshots:
            return None
        task = asyncio.ensure_future(asyncio.to_thread(self._store().snapshot))
        if wait is None:
            return await task
        try:
            return await asyncio.wait_for(asyncio.shield(task), wait)
        except TimeoutError:
            return None

    async def _record_start(self, prompt: str) -> _TurnRecord:
        messages = self.session.messages
        before = await self._snapshot(SNAPSHOT_WAIT_S)
        return _TurnRecord(
            prompt=prompt,
            anchor=messages[-1] if messages else None,
            before=before,
            after=None,
            session_id=self.session.session_id,
        )

    def _record_close(self, record: _TurnRecord) -> None:
        """The turn is over (finished, failed or stopped): it can be undone. The snapshot
        of its result starts now, in a thread."""
        self._turns.append(record)
        del self._turns[:-UNDO_DEPTH]
        self._undone.clear()  # a new turn: nothing left to redo
        if record.before is not None:
            record.after = asyncio.ensure_future(asyncio.to_thread(self._store().snapshot))

    async def _record_end(self, record: _TurnRecord) -> None:
        """Close the turn's record and wait for the snapshot of its result, so that what
        the user changes next is never taken for the turn's work; only a recording slower
        than SNAPSHOT_AFTER_WAIT_S (a huge project) is left to finish in the background."""
        self._record_close(record)
        if record.after is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(record.after), SNAPSHOT_AFTER_WAIT_S)

    def replay_history(self, turns: int = REPLAY_TURNS) -> None:
        """A resumed session: its last ``turns`` exchanges as they looked when they ran
        (messages, answers, one line per tool call), so the user sees where it stopped."""
        p, g = self.paint, self.glyph
        messages = [m for m in self.session.messages if m.role != "system"]
        asked = [i for i, m in enumerate(messages) if m.role == "user" and not m.meta.get("kind")]
        if not asked:
            return
        start = asked[-turns] if len(asked) > turns else 0
        if start:
            self.write(p("muted", f"… {start} earlier messages not shown") + "\n")
        calls: dict[str, ToolCall] = {}
        for m in messages[start:]:
            if m.role == "user":
                if m.meta.get("kind") in (None, "compaction"):
                    lines = _plain_text(m.content).split("\n")
                    if m.meta.get("kind"):
                        lines = ["(summary of the earlier conversation)"]
                    self._end_stream()
                    head = p("accent+bold", g["prompt"]) + p("user", lines[0])
                    rest = [p("muted", g["more"]) + p("user", line) for line in lines[1:]]
                    self.write("\n".join([head, *rest]) + "\n")
            elif m.role == "assistant":
                if m.content.strip():
                    self.render(ev.AssistantMessage(step=0, text=m.content))
                for call in m.tool_calls:
                    calls[call.id] = call
                    self.render(ev.ToolStarted(call.id, call.name, call.arguments))
            elif m.role == "tool":
                made = calls.pop(m.tool_call_id or "", None)
                name = made.name if made is not None else (m.name or "tool")
                # The exit code is not stored with the message: its marker line tells it,
                # so a failed command shows as failed again.
                codes = _EXIT_CODE.findall(m.content)
                meta = {"exit_code": int(codes[-1])} if codes else {}
                finished = ev.ToolFinished(
                    m.tool_call_id or "", name, m.is_error, m.content, -1, meta=meta
                )
                self.render(finished)
        self._end_stream()
        self._active_tools.clear()  # calls the history has no result for
        self._had_tools = False

    def _export(self, arg: str) -> None:
        from karox6.chat_features import conversation_markdown

        session = self.session
        if not any(m.role == "user" for m in session.messages):
            self.write("nothing to export yet\n")
            return
        target = Path(arg).expanduser() if arg else Path(f"karox-{session.session_id}.md")
        if not target.is_absolute():
            target = Path(self.config.workspace) / target
        title = f"KaroX session {session.session_id} ({self.config.model})"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(conversation_markdown(session.messages, title), encoding="utf-8")
        except OSError as exc:
            self.write(self.paint("red", f"cannot write {target}: {exc}") + "\n")
            return
        self.write(f"exported to {_plain_text(str(target))}\n")

    def _forget_turns(self) -> None:
        self._turns.clear()
        self._undone.clear()

    def _rewind_to(self, record: _TurnRecord) -> list[Message] | None:
        """Drop the turn's messages; None when its start is no longer in the history
        (compaction replaced it)."""
        messages = self.session.messages
        if record.anchor is None:
            return self.session.rewind(0)
        for index in range(len(messages) - 1, -1, -1):
            if messages[index] is record.anchor:
                return self.session.rewind(index + 1)
        return None

    async def _move_files(self, record: _TurnRecord, *, back: bool) -> str:
        """Move the turn's files back (undo) or forward (redo); a line for the user."""
        after = await record.after if record.after is not None else None
        if record.before is None or after is None:
            why = self._store().disabled if self.snapshots else "snapshots are off (--no-undo)"
            return f"files were not recorded ({why or 'no snapshot'}): only the messages moved"
        source, target = (after, record.before) if back else (record.before, after)
        current = await self._snapshot()
        if current is None:
            return f"files left as they are ({self._store().disabled})"
        from karox6.core.checkpoint import CheckpointError

        try:
            changed, skipped = await asyncio.to_thread(self._store().move, source, target, current)
        except (CheckpointError, OSError) as exc:
            return f"files left as they are ({exc})"
        if not changed and not skipped:
            return "no files to change"
        parts = []
        if changed:
            shown = ", ".join(_plain_text(p) for p in changed[:6])
            more = f" and {len(changed) - 6} more" if len(changed) > 6 else ""
            verb = "restored" if back else "re-applied"
            plural = "s" if len(changed) != 1 else ""
            parts.append(f"{verb} {len(changed)} file{plural}: {shown}{more}")
        if skipped:
            shown = ", ".join(_plain_text(p) for p in skipped[:6])
            parts.append(f"left alone (changed since): {shown}")
        return "; ".join(parts)

    async def _undo(self) -> None:
        if not self._turns:
            self.write("nothing to undo (only turns of this chat can be taken back)\n")
            return
        record = self._turns.pop()
        files = await self._move_files(record, back=True)
        dropped = self._rewind_to(record)
        title = clip(_plain_text(record.prompt), 70)
        self.write(f"undid: {self.paint('bold', title)}\n")
        self.write(self.paint("dim", f"  {files}") + "\n")
        if dropped is None:
            # The turn's start was summarised away: the model must still learn of it.
            self.session.note_for_model(
                "undo",
                "[The user undid the last turn: the files it changed are back to their state "
                "before it. Do not assume those changes exist.]",
            )
            self.write(self.paint("dim", "  the conversation was compacted since: kept") + "\n")
        self._undone.append((record, dropped or []))
        if self._prefill is not None and dropped is not None:
            self._prefill(record.prompt)
            hint = "  your message is back in the input line; /redo puts the turn back"
        else:
            hint = "  /redo puts the turn back"
        self.write(self.paint("dim", hint) + "\n")

    async def _redo(self) -> None:
        if not self._undone:
            self.write("nothing to redo\n")
            return
        record, dropped = self._undone.pop()
        files = await self._move_files(record, back=False)
        self.session.extend(dropped)
        self._turns.append(record)
        title = clip(_plain_text(record.prompt), 70)
        self.write(f"redid: {self.paint('bold', title)}\n")
        self.write(self.paint("dim", f"  {files}") + "\n")

    def _fork(self) -> None:
        old = self.session
        if not old.messages:
            self.write("nothing to fork yet: the conversation is empty\n")
            return
        fresh = self._new_session()
        fresh.take_history(old.messages, old.usage, old.cost_usd, old.session_id)
        self.session = fresh
        self._forget_turns()
        self.write(
            f"forked: now in {fresh.session_id}, a copy of {old.session_id} "
            f"({len(fresh.messages)} messages)\n"
            + self.paint("dim", f"  the original is untouched: /resume {old.session_id}")
            + "\n"
        )

    def render(self, event: ev.Event) -> None:
        p, g = self.paint, self.glyph
        if isinstance(event, ev.TextDelta):
            self._open("text")
            self._phase = "Model responding"
            self._streamed_text = True
            text = _plain_text(event.text)
            done = self._md.feed(text)
            if done:
                self.write(done)
                self._stream_newline = done.endswith("\n")
            if self._live is not None:
                self._live(self._md.partial())
            return
        if isinstance(event, ev.ReasoningDelta):
            self._phase = "Model thinking"
            if self.show_reasoning:
                self._open("reasoning")
                text = _plain_text(event.text)
                self.write(p("muted+italic", text))
                if text:
                    self._stream_newline = text.endswith("\n")
            return
        if isinstance(event, ev.AssistantMessage):
            if event.text and not self._streamed_text:
                self._open("text")
                text = self._md.feed(_plain_text(event.text).rstrip() + "\n") + self._md.flush()
                self.write(text)
                self._stream_newline = text.endswith("\n")
            self._streamed_text = False
            self._end_stream()
            return
        if isinstance(
            event, ev.UsageUpdate | ev.RunStarted | ev.ApprovalRequested | ev.ApprovalResolved
        ):
            return
        self._end_stream()
        if isinstance(event, ev.ToolStarted):
            self._active_tools[event.call_id] = event
        elif isinstance(event, ev.ToolFinished):
            self._render_tool_finished(event)
        elif isinstance(event, ev.Retry):
            self._streamed_text = False
            self._phase = f"Retrying model (attempt {event.attempt})"
            error = clip(_plain_text(explain_error(event.error)[0]), 140)
            self.write(
                p("warning", f"  {g['warn']} retry {event.attempt}")
                + p("muted", f" in {event.delay_s:.0f}s: {error}")
                + "\n"
            )
        elif isinstance(event, ev.Notice):
            self.write(p("warning", f"  [{event.kind}] {_plain_text(event.text)[:200]}") + "\n")
        elif isinstance(event, ev.Compacted):
            self.write(
                p(
                    "muted",
                    f"  context compacted ({event.mode}): "
                    f"{event.tokens_before} -> {event.tokens_after} tokens",
                )
                + "\n"
            )
        elif isinstance(event, ev.ErrorEvent):
            line, hint = explain_error(event.message)
            self.write(p("error", f"{g['fail']} error: {_plain_text(line)}") + "\n")
            if hint:
                self.write(p("accent", f"  {g['arrow']} ") + p("muted", hint) + "\n")
            self._error_shown = True
        elif isinstance(event, ev.RunFinished):
            self._phase = "Finishing"
            self._last_turn_s = event.elapsed_s
            self.write(self._footer(event) + "\n")
            self._error_shown = False

    def _render_tool_finished(self, event: ev.ToolFinished) -> None:
        if self._text_before_tools:
            self.write("\n")  # the model's words, then a gap, then what it ran
            self._text_before_tools = False
        start = self._active_tools.pop(event.call_id, None)
        self._phase = "Model thinking"
        self._had_tools = True
        p, g = self.paint, self.glyph
        name = " ".join(_plain_text(event.name).split())[:80]
        target = _plain_text(brief(start.arguments)) if start is not None else ""
        pad = "    " if "/" in name else ""  # a subagent's call
        failed = is_tool_failure(event)
        bullet = p("error" if failed else "success", g["tool"])
        # The tool line steps back (muted name and target): the model's own words stay
        # the brightest text on screen; the bullet keeps the success / failure colour.
        head = f"{pad}{bullet} {p('muted+bold', name)}" + (
            f" {p('muted', target)}" if target else ""
        )
        mark = p("error", g["fail"]) if failed else p("success", g["ok"])
        stats = (
            _edit_diff_stats(event.name, start.arguments)
            if start is not None and not failed
            else None
        )
        if stats is None:
            summary = _plain_text(outcome(event)).replace(" · ", g["dot"])
            detail = p("error" if failed else "muted", summary)
        else:
            detail = self._paint_diff_stats(*stats)
        # A replayed call (duration -1) took no time now: no duration shown.
        took = (
            p("muted", f"{g['dot']}{event.duration_ms / 1000:.1f}s")
            if event.duration_ms >= 0
            else ""
        )
        line = f"{head}  {mark}" + (f" {detail}" if detail else "") + f"{took}\n"
        short = [line]
        full = [line]
        if start is not None and not failed:
            # What the edit changed, right under its line (the whole change: /diff).
            colors = {"-": "error+del_bg", "+": "success+add_bg", "…": "muted"}
            # Tinted lines run to the edge, as a diff viewer draws them: "erase to the end
            # of the line" paints the rest in the current background.
            tinted = bool(p.enabled and p.theme.sgr("add_bg"))

            def row(kind: str, text: str) -> str:
                fill = "\x1b[K" if tinted and kind != "…" else ""
                return f"{pad}  {p(colors[kind], text + fill)}\n"

            short += [row(*r) for r in diff_preview(event.name, start.arguments, event.meta)]
            full += [row(*r) for r in diff_preview(event.name, start.arguments, event.meta, 400)]
        output = self._tool_output_lines(event)
        if output and len(full) == 1:
            shown = output[-200:]
            lead = f"{pad}  {p('subtle', g['result'])}"
            rest = f"{pad}    "
            if len(output) > len(shown):
                full.append(lead + p("muted", f"… {len(output) - len(shown)} earlier lines") + "\n")
                lead = rest
            for n, text in enumerate(shown):
                full.append((lead if n == 0 else rest) + p("muted", text) + "\n")
            if self._write_block is not None:
                count = f"{len(output)} line{'s' if len(output) != 1 else ''}"
                short.append(f"{pad}  " + p("subtle", f"… {count} (ctrl+o to expand)") + "\n")
        if self._write_block is not None and full != short:
            self._write_block("".join(short), "".join(full))
        else:
            self.write("".join(short))

    def _tool_output_lines(self, event: ev.ToolFinished) -> list[str]:
        """The output worth expanding under a tool's line: a command's output, or the
        rest of an error. Reads, searches and listings stay one line."""
        base = event.name.rsplit("/", 1)[-1]
        shell = base in ("bash", "shell", "powershell", "pwsh", "job_output")
        if not shell and not is_tool_failure(event):
            return []
        # clip_cells, not digest.clip: that one squeezes whitespace for one-line
        # summaries and flattened the indentation of code and diffs here.
        lines = [
            clip_cells(_plain_text(line.expandtabs(4)).rstrip(), 400)
            for line in event.output.strip("\n").splitlines()
        ]
        lines = [line for line in lines if line.strip() and not _EXIT_LINE.match(line)]
        return lines if len(lines) > 1 else []  # one line: the summary shows it

    def _paint_diff_stats(self, added: int, removed: int) -> str:
        return f"{self.paint('green', f'+{added}')} {self.paint('red', f'-{removed}')}"

    def _footer(self, event: ev.RunFinished) -> str:
        # this turn's numbers: the session's totals made a later turn look like it had
        # changed files it never touched (and spent every earlier turn's tokens)
        usage = event.run_usage or event.usage
        files = event.files_changed if event.run_files is None else event.run_files
        g = self.glyph
        parts = [
            f"{event.steps} steps",
            f"{event.elapsed_s:.0f}s",
            f"{_k(usage.get('input_tokens', 0))} in / {_k(usage.get('output_tokens', 0))} out",
        ]
        cost = event.run_cost_usd if event.run_usage else event.cost_usd
        if cost is not None:
            parts.append(f"${cost:.4f}")
        if files:
            count = len(files)
            outside = sum(1 for f in files if Path(f).is_absolute())
            text = f"{count} file{'s' if count != 1 else ''} changed"
            if outside:
                text += f", {outside} outside the workspace"
            parts.append(f"{text} (/diff)")
        done = event.status == "completed"
        head = self.paint(
            "success" if done else "warning", f"{g['ok'] if done else g['warn']} {event.status}"
        )
        reason = ""
        if event.reason and not done and not self._error_shown:
            # (an error was just printed in full above the footer: not twice)
            reason = f" {clip(_plain_text(explain_error(event.reason)[0]), 160)}"
        return f"{head}{reason}" + self.paint("muted", g["dot"] + g["dot"].join(parts).strip())

    def _open(self, kind: str) -> None:
        if self._stream != kind:
            self._end_stream()
            if kind == "text" and self._had_tools:
                self.write("\n")
                self._had_tools = False
            self._stream = kind
            if kind == "reasoning":
                self.write(self.paint("muted+italic", "thinking: "))

    def _end_stream(self) -> None:
        if self._stream is not None:
            if self._stream == "text":
                self._text_before_tools = True
                rest = self._md.flush()
                if self._live is not None:
                    self._live("")
                if rest:
                    self.write(rest)
                    self._stream_newline = rest.endswith("\n")
            if not self._stream_newline:
                self.write("\n")
            self._stream = None
            self._stream_newline = False

    # -- approvals ------------------------------------------------------------------

    async def _read_question(self, reader: ReadLine, prompt: str) -> str:
        try:
            if self._display is None:
                return await reader(prompt)
            async with self._display.pause():
                return await reader(prompt)
        except KeyboardInterrupt:
            if not self._turn_running:
                raise
            self._phase = "Stopping"
            self._active_tools.clear()
            raise asyncio.CancelledError from None

    async def read_answer(self, prompt: str) -> str:
        return await self._read_question(self._read_answer, prompt)

    async def read_secret(self, prompt: str) -> str:
        return await self._read_question(self._read_secret, prompt)

    async def _approve(self, request: ApprovalRequest) -> bool:
        async with self._question_lock:
            return await self._ask_approval(request)

    async def _ask_approval(self, request: ApprovalRequest) -> bool:
        if self.approval_mode == "allow":
            return True
        self._end_stream()
        if self.approval_mode == "deny":
            self.write(
                self.paint("yellow", f"  refused: {request.summary[:160]}")
                + self.paint("dim", " (/approve ask to be asked)")
                + "\n"
            )
            return False
        self.write(
            self.paint("yellow", f"! {_plain_text(request.reason)}")
            + f"\n  rule: {_plain_text(request.rule)}\n  command: {_plain_text(request.summary)}\n"
        )
        try:
            answer = (await self.read_answer("  Run it? [y/N/a = always] ")).strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if answer in ("a", "always"):
            self.approval_mode = "allow"
            return True
        return answer in ("y", "yes")

    # -- commands -------------------------------------------------------------------

    async def command(self, line: str) -> str | None:
        name, _, rest = line.partition(" ")
        arg = rest.strip()
        name = name.lower()
        if name in _SESSION_COMMANDS:
            await self._ensure_session()
        if name in ("/quit", "/exit", "/q"):
            return "quit"
        if name == "/help":
            self.write(_format_help(self.paint, self.approval_mode) + "\n")
        elif name == "/clear":
            self._clear_screen()
        elif name == "/diff":
            await self._show_diff()
        elif name == "/status":
            self._show_status()
        elif name == "/cost":
            self._show_cost()
        elif name == "/compact":
            await self._compact()
        elif name == "/new":
            self.session = self._new_session()
            self._forget_turns()
            self.write(f"new session {self.session.session_id}\n")
        elif name == "/undo":
            await self._undo()
        elif name == "/redo":
            await self._redo()
        elif name == "/fork":
            self._fork()
        elif name == "/init":
            focus = f"\n\nPay special attention to: {arg}" if arg else ""
            await self.turn(INIT_PROMPT + focus, label="/init" + (f" {arg}" if arg else ""))
        elif name == "/paste":
            await self.paste_image(to_draft=False)
        elif name == "/copy":
            await self._copy(arg)
        elif name == "/export":
            self._export(arg)
        elif name == "/mcp":
            self.write(mcp_report(self.mcp, self.paint, self.glyph, arg))
        elif name == "/sessions":
            from karox6.core.store import recent_sessions

            rows = recent_sessions(self.config.home, workspace=self.config.workspace)
            if not rows:
                self.write("no saved sessions\n")
            for session_id, title in rows:
                mark = "*" if session_id == self.session.session_id else " "
                # A title is the start of a saved message: none of its control
                # sequences may reach the terminal.
                self.write(f"{mark} {session_id}  {self.paint('dim', _plain_text(title))}\n")
        elif name == "/resume":
            from karox6.core.store import resume_kwargs

            try:
                kwargs = resume_kwargs(
                    self.config.home,
                    arg or "last",
                    exclude=self.session.session_id,
                    workspace=self.config.workspace,
                )
            except (ValueError, FileNotFoundError) as exc:
                self.write(self.paint("red", str(exc)) + "\n")
            else:
                self.session = self._new_session(**kwargs)
                self._forget_turns()
                count = len(self.session.messages)
                self.replay_history()
                self.write(f"resumed {self.session.session_id} ({count} messages)\n")
        elif name == "/approve":
            if arg in APPROVAL_MODES:
                self.approval_mode = arg
            self.write(f"dangerous commands: {self.approval_mode}\n")
        elif name == "/key":
            await self._set_key()
        elif name == "/model":
            await self._model(arg)
        elif name == "/models":
            await self._models(arg)
        elif name == "/provider":
            await self._provider(arg)
        elif name == "/setup":
            await self._setup()
        elif name == "/bridge":
            await self._bridge(arg)
        elif name == "/update":
            await self._update(arg.lower())
        elif name == "/effort":
            low = arg.lower()
            if low and low not in {value for value, _ in _EFFORT_ARGS} | {"default", "none"}:
                choices = "|".join(value for value, _ in _EFFORT_ARGS)
                self.write(self.paint("yellow", f"usage: /effort [{choices}]") + "\n")
            else:
                if low:  # the session reads the config at its next request
                    effort = None if low in ("off", "default", "none") else low
                    self.config.reasoning_effort = effort
                now = self.config.reasoning_effort or "provider default"
                self.write(f"reasoning effort: {now}\n")
        elif name == "/reasoning":
            low = arg.lower()
            if low in ("on", "show", "true", "1"):
                self.show_reasoning = True
            elif low in ("off", "hide", "false", "0"):
                self.show_reasoning = False
            else:
                self.show_reasoning = not self.show_reasoning
            self.write(f"reasoning {'shown' if self.show_reasoning else 'hidden'}\n")
        else:
            known = [spec.name for spec in COMMANDS] + list(_COMMAND_ALIASES)
            near = closest_command(name, known)
            meant = f"did you mean {self.paint('cyan', near)}? " if near else ""
            self.write(f"unknown command {name}; {meant}/help lists commands\n")
        return None

    async def _update(self, arg: str) -> None:
        """/update: what `karox6 update` does, with its lines in the conversation."""
        from concurrent.futures import ThreadPoolExecutor

        from karox6 import update

        if self._updating:
            self.write("an update is already running\n")
            return
        if arg not in ("", "check", "force"):
            self.write(
                self.paint("red", f"unknown /update argument {arg!r}")
                + self.paint("dim", " — /update, /update check or /update force")
                + "\n"
            )
            return
        loop = asyncio.get_running_loop()

        def out(line: str) -> None:
            # Updater lines carry remote bytes (commit subjects, git/pip stderr): strip
            # terminal controls so a hostile repo cannot write CSI/OSC into the chat.
            text = self.paint("dim", f"  {_plain_text(line)}") + "\n"
            loop.call_soon_threadsafe(self.write, text)

        source = self.update_source or update.Source.from_settings()
        self._updating = True
        try:
            # Its own one-thread pool: run_update is minutes of subprocess work and
            # asyncio.to_thread's threads are non-daemon, so a quit would hang on them.
            # close() calls update.abort() to kill the clone/pip it is sitting inside.
            with ThreadPoolExecutor(1, thread_name_prefix="karox6-update") as pool:
                result = await loop.run_in_executor(
                    pool,
                    lambda: update.run_update(
                        source, out, force=arg == "force", check_only=arg == "check"
                    ),
                )
        except update.UpdateError as exc:
            self.write(
                self.paint("red", f"{self.glyph['fail']} update failed: {_plain_text(str(exc))}")
                + "\n"
            )
            return
        finally:
            self._updating = False
        lines = update.describe(result)
        color = "green" if result.status in ("updated", "current") else "yellow"
        self.write(self.paint(color, lines[0]) + "\n")
        for line in lines[1:]:
            self.write(self.paint("dim", line) + "\n")
        if result.status == "updated":
            self.update_note = ""
            self.write(
                self.paint("bold", f"restart KaroX to use {_plain_text(result.new_version)}")
                + self.paint("dim", " (/quit, then karox6; --continue resumes this session)")
                + "\n"
            )
        elif result.status == "available":
            self.write(self.paint("dim", "/update installs it") + "\n")

    def notice_update(self, version: str) -> None:
        """The background check found a newer version: one line, never during a turn."""
        version = _plain_text(version)  # parsed out of the remote repo's __init__.py
        self.update_note = version
        if not self._turn_running:
            self.write(self.paint("cyan", f"version {version} is available: /update") + "\n")

    def _clear_screen(self) -> None:
        self._end_stream()
        # Clear only the visible screen (not scrollback) and keep the active session.
        self.write(_CLEAR_SCREEN + self._banner())
        if self._initialize is None and self.session.messages:
            self.write(
                self.paint(
                    "dim",
                    f"session {self.session.session_id} kept "
                    f"({len(self.session.messages)} messages); /new starts fresh",
                )
                + "\n"
            )

    async def _compact(self) -> None:
        if len(self.session.messages) < 3:
            self.write("nothing to compact yet\n")
            return
        await self._run_with_display(self._compact_history, "compacting context")

    async def _compact_history(self) -> str:
        from karox6.providers.base import ProviderError

        try:
            events = await self.session.compact(emit=self.render)
        except ProviderError as exc:
            reason = _plain_text(str(exc))
            message = f"{self.glyph['fail']} compaction failed: {reason}"
            self.write(self.paint("red", message) + "\n")
            return "error"
        if not events:
            self.write("nothing to compact\n")
        return "completed"

    async def _set_key(self) -> None:
        from karox6.credentials import save_key

        try:
            key = (
                await self.read_secret("API key (hidden; Enter keeps the current one): ")
            ).strip()
        except (EOFError, KeyboardInterrupt):
            self.write("key unchanged\n")
            return
        if not key:
            self.write("key unchanged\n")
            return
        where = save_key(self.config.home, key, self.key_name)
        if hasattr(self.provider, "api_key"):
            self.provider.api_key = key
        if self.bridge_env:
            self.bridge_env = {name: key for name in self.bridge_env}
        self.write(f"key saved (encrypted for your user) in {where}; in use now\n")

    async def _setup(self) -> None:
        from karox6.setup import run_setup

        try:
            choice = await run_setup(
                self.read_answer,
                self.read_secret,
                self.write,
                self.config.home,
                picker=self.picker,
            )
        except (EOFError, KeyboardInterrupt):
            self.write("setup cancelled\n")
            return
        if choice is None:
            return
        await self._use(choice.provider, choice.base_url, choice.model, choice.key, choice.name)

    async def _use(
        self, kind: str, base_url: str, model: str, key: str | None, key_name: str
    ) -> None:
        """Talk to another provider (or model) from the next request on."""
        from karox6.providers import make_provider

        old = self.provider
        self.provider = make_provider(kind, base_url=base_url or None, api_key=key)
        self.session.provider = self.provider
        switched = kind != self.config.provider_name or base_url != getattr(
            old, "base_url", base_url
        )
        self.session.forget_model_state(provider_changed=switched)
        self.config.model = model
        self.config.provider_name = kind
        self.key_name = key_name
        # The bridge agent talks to this provider now (never the old server with the new
        # key, nor the new server with the old one).
        self.bridge_agent_args = ["--provider", kind, "--model", model]
        if base_url:
            self.bridge_agent_args += ["--base-url", base_url]
        self.bridge_env = {}
        if key:
            self.bridge_agent_args += ["--api-key-env", "KAROX_BRIDGE_API_KEY"]
            self.bridge_env = {"KAROX_BRIDGE_API_KEY": key}
        with contextlib.suppress(Exception):
            await old.aclose()
        self.write(f"now using {model}\n")

    async def _provider(self, arg: str) -> None:
        from karox6.credentials import load_key
        from karox6.settings import SettingsError, save_provider, saved_providers

        saved = saved_providers()
        if not arg and self.picker is not None:
            labels = {
                f"{n}: {t['model']} ({t.get('base_url') or t.get('provider', '')})": n
                for n, t in saved.items()
            }
            new = "+ another provider (setup)"
            here = getattr(self.provider, "base_url", "")
            current = next(
                (lbl for lbl, n in labels.items() if saved[n].get("base_url") == here), None
            )
            picked = await self.picker("Provider", [*labels, new], current)
            if picked is None:
                return
            if picked == new:
                await self._setup()
                return
            arg = labels.get(picked, "")
        if not arg:
            if not saved:
                self.write("no saved providers yet: /setup adds one\n")
                return
            here = getattr(self.provider, "base_url", "")
            for name, row in saved.items():
                mark = "*" if row.get("base_url") == here else " "
                where = row.get("base_url") or row.get("provider", "")
                self.write(f" {mark} {name}: {row['model']} ({where})\n")
            self.write(self.paint("dim", "  /provider NAME switches; /setup adds one") + "\n")
            return
        table = saved.get(arg)
        if table is None:
            names = ", ".join(saved) or "none yet"
            line = f"no saved provider {arg!r} (saved: {names})"
            self.write(self.paint("yellow", line) + "\n")
            return
        # A profile written by hand has no saved_key: its own name, never the unnamed key
        # (that one belongs to whatever an older setup saved, maybe another server).
        key_name = str(table.get("saved_key") or arg)
        env = table.get("api_key_env")
        key = os.environ.get(env) if isinstance(env, str) and env else None
        key = key or load_key(self.config.home, key_name)
        kind = str(table.get("provider") or "openai-chat")
        await self._use(kind, str(table.get("base_url") or ""), str(table["model"]), key, key_name)
        try:
            save_provider(arg, table)  # the next start uses it too
        except SettingsError as exc:
            self.write(self.paint("yellow", f"not saved as the default: {exc}") + "\n")
        if key is None:
            self.write(self.paint("yellow", "  no key saved for it: /key adds one") + "\n")

    async def _provider_models(self) -> list[str] | None:
        """The current provider's chat models (cached per server); None when it has no list."""
        from karox6.providers.catalog import CatalogError, fetch

        where = (self.config.provider_name, getattr(self.provider, "base_url", None))
        if not where[1]:
            return None  # not a server KaroX talks to over HTTP (a scripted test provider)
        if self._model_list is not None and self._model_list[0] == where:
            return self._model_list[1]  # also a failure: asked once per server
        found: list[str] | None
        key = getattr(self.provider, "api_key", None)
        try:
            found, self._model_notes = await fetch(where[0], where[1], key, self.config.home)
            # What the list said (window, pictures) is in models.json now: read it again.
            self.session.reread_model_facts()
        except CatalogError as exc:
            self.write(self.paint("yellow", f"no model list: {_plain_text(str(exc))}") + "\n")
            found = None
        self._model_list = (where, found)
        return found

    def _show_models(self, models: list[str], hint: str) -> None:
        from karox6.providers.catalog import columns

        shown = models[:MODELS_SHOWN]
        width = self._columns() if self._columns else 100
        for line in columns(shown, width=width, mark=self.config.model):
            self.write(line + "\n")
        more = ""
        if len(models) > len(shown):
            more = f"{len(models) - len(shown)} more (/models TEXT filters); "
        self.write(self.paint("dim", f"  {more}{hint}") + "\n")
        self._shown_models = shown

    async def _models(self, arg: str) -> None:
        from karox6.providers.catalog import pick

        models = await self._provider_models()
        if models is None:
            return
        if arg:
            chosen, matches = pick(models, arg)
            models = [chosen] if chosen else matches
        if not models:
            self.write(f"no model matches {arg!r}\n")
            return
        self._show_models(models, "/model NUMBER or NAME switches")

    async def _model(self, arg: str) -> None:
        from karox6.providers.catalog import pick

        if not arg:
            models = await self._provider_models()
            if models and self.picker is not None:
                picked = await self.picker(
                    "Model", models, self.config.model, notes=self._model_notes
                )
                if picked is not None:
                    self._switch_model(picked)
                return
            self.write(f"model: {self.config.model}\n")
            self.write(self.paint("dim", f"  {self.session.model().describe()}") + "\n")
            if models:
                self._show_models(models, "/model NUMBER or NAME (or part of one) switches")
            return
        chosen: str | None = None
        if arg.isdigit() and self._shown_models and 1 <= int(arg) <= len(self._shown_models):
            chosen = self._shown_models[int(arg) - 1]
        else:
            models = await self._provider_models() or []
            found, matches = pick(models, arg) if models and not arg.isdigit() else (None, [])
            if found is None and len(matches) == 1:
                found = matches[0]
            if found is None and len(matches) > 1:
                self._show_models(matches, "/model NUMBER picks one of these")
                return
            chosen = found or arg
            if found is None and models:
                note = f"  {arg} is not in the provider's list; using it as typed"
                self.write(self.paint("yellow", note) + "\n")
        self._switch_model(chosen)

    def _switch_model(self, chosen: str) -> None:
        args = self.bridge_agent_args
        if "--model" in args and args.index("--model") + 1 < len(args):
            args[args.index("--model") + 1] = chosen  # the bridge agent uses it too
        if chosen != self.config.model:
            self.config.model = chosen
            self.session.forget_model_state(provider_changed=False)
        self.write(f"model: {self.config.model}\n")
        self.write(self.paint("dim", f"  {self.session.model().describe()}") + "\n")

    async def _bridge(self, arg: str) -> None:
        from karox6.bridge import BridgeError

        b = self.bridge
        if arg == "stop":
            if not b.running():
                self.write("the bridge is not running\n")
                return
            await asyncio.to_thread(b.stop)
            self.write("bridge stopped (its Tailscale route is removed)\n")
            return
        if arg in ("url", "log"):
            if not b.running():
                self.write("the bridge is not running: /bridge starts it\n")
            elif arg == "url":
                self.write(
                    "Secret link for clients set to 'No authentication' (anyone with it can "
                    "use KaroX here):\n  " + self.paint("cyan", b.url or "") + "\n"
                )
            else:
                notes = b.activity()
                self.write(
                    "\n".join(f"  {n}" for n in notes) + "\n" if notes else "  no requests yet\n"
                )
            return
        if b.running() and b.url:
            self._bridge_card()
            return
        if arg not in ("", "direct", "agent"):
            self.write("usage: /bridge [agent] | /bridge url | /bridge log | /bridge stop\n")
            return
        mode = "agent" if arg == "agent" else "direct"
        if mode == "agent" and not self.bridge_agent_args:
            self.write(self.paint("red", "agent mode needs the chat's provider settings") + "\n")
            return
        self.write(self.paint("dim", "starting the bridge (Tailscale Funnel)...") + "\n")
        try:
            await asyncio.to_thread(
                b.start,
                mode=mode,
                root=self.session.workspace,
                agent_args=self.bridge_agent_args,
                env=self.bridge_env,
            )
        except (BridgeError, OSError) as exc:
            self.write(self.paint("red", str(exc)) + "\n")
            return
        self._bridge_card()

    def _bridge_card(self) -> None:
        b = self.bridge
        pin = b.pin or ""
        pin = f"{pin[:4]}-{pin[4:]}" if len(pin) == 8 else pin
        how = (
            "ChatGPT's model uses KaroX tools"
            if b.mode == "direct"
            else (f"tasks go to {self.config.model}")
        )
        rows: list[tuple[str, str]] = [
            (
                "",
                self.paint("green", self.glyph["tool"] + " online")
                + f"  {b.where} · {b.mode} ({how})",
            ),
            ("", ""),
            ("URL", self.paint("cyan", b.oauth_url or "")),
            ("Auth", "OAuth"),
            ("Code", self.paint("bold", pin) + self.paint("dim", "  type it on the KaroX page")),
            ("", ""),
            ("ChatGPT", "Settings > Apps > Advanced: Developer mode > Create app:"),
            ("", "paste the URL, choose OAuth, Create; approve with the code"),
            ("Claude", "Settings > Connectors > Add custom connector: the URL"),
            ("", ""),
            ("Project", str(b.root)),
            ("", self.paint("dim", "/bridge url secret no-auth link · /bridge log · /bridge stop")),
        ]
        self.write(_box("KaroX bridge", rows, self.glyph.get("box", "ascii") == "fancy") + "\n")

    def _context_summary(self, *, with_threshold: bool = False) -> str:
        used = self.session.context_tokens() if self.session.messages else 0
        window = self.config.effective_window(self.session.window_hint())
        pct = min(round(used / window * 100), 100) if window else 0
        base = f"{_k(used)} / {_k(window)} ({pct}%)"
        if with_threshold:
            return f"{base}, compacts at {_k(self.session._threshold())}"
        return base

    def _show_status(self) -> None:
        s = self.session
        usage = s.usage.to_dict()
        lines = [
            f"session    {s.session_id}",
            f"model      {self.config.model} ({self.config.provider_name})",
            f"knows      {s.model().describe()}",
            f"workspace  {s.workspace}",
            f"messages   {len(s.messages)}",
            f"context    {self._context_summary()}",
            f"tokens     {usage.get('input_tokens', 0)} in "
            f"({usage.get('cached_tokens', 0)} cached) / "
            f"{usage.get('output_tokens', 0)} out",
            f"dangerous  {self.approval_mode}",
        ]
        if self.config.price:
            lines.append(f"cost       ${s.cost_usd:.4f}")
        bridge = self._bridge_instance
        if bridge is not None and bridge.running():
            lines.append(f"bridge     {bridge.mode} {bridge.url}")
        self.write("\n".join(lines) + "\n")

    def _show_cost(self) -> None:
        s = self.session
        u = s.usage
        total = u.input_tokens + u.output_tokens
        lines = [
            f"model      {self.config.model} ({self.config.provider_name})",
            f"requests   {u.requests}",
            f"input      {u.input_tokens:,} ({u.cached_tokens:,} cached)",
            f"output     {u.output_tokens:,} ({u.reasoning_tokens:,} reasoning)",
            f"total      {total:,} tokens",
            f"context    {self._context_summary(with_threshold=True)}",
        ]
        if self.config.price:
            lines.append(f"cost       ${s.cost_usd:.4f}")
        else:
            lines.append(
                "cost       "
                + self.paint("dim", "not configured (set --price-input / --price-output)")
            )
        self.write("\n".join(lines) + "\n")

    async def _show_diff(self) -> None:
        from karox6.mcp.runs import git_diff

        workspace = self.session.workspace
        text, untracked = await asyncio.to_thread(git_diff, workspace)
        if text is None:
            changed = await asyncio.to_thread(self.session.files.changed_files)
            self.write("not a git repository; files changed this session:\n")
            for path in changed or ["(none)"]:
                self.write(f"  {_plain_text(path)}\n")
            return
        if not text.strip() and not untracked:
            self.write("no changes\n")
            return
        if text.strip():
            files, added, removed = _diff_summary(text)
            label = f"{files} file{'s' if files != 1 else ''} changed"
            self.write(f"{self.paint('dim', label)}  {self._paint_diff_stats(added, removed)}\n")
        shown = text[:DIFF_CAP]
        in_hunk = False
        for line in shown.splitlines():
            if line.startswith("diff --git "):
                in_hunk = False
            elif line.startswith("@@ "):
                in_hunk = True
            self.write(self._diff_line(_plain_text(line), in_hunk=in_hunk) + "\n")
        if len(text) > DIFF_CAP:
            self.write(self.paint("dim", f"... {len(text) - DIFF_CAP} more characters") + "\n")
        if untracked:
            self.write(self.paint("bold", "untracked:") + "\n")
            for path in untracked[:50]:
                self.write(self.paint("green", f"  {_plain_text(path)}") + "\n")

    def _diff_line(self, line: str, *, in_hunk: bool) -> str:
        if not in_hunk and line.startswith(("+++ ", "--- ", "diff --git ", "index ")):
            return self.paint("bold", line)
        if line.startswith("+"):
            return self.paint("success+add_bg", line)
        if line.startswith("-"):
            return self.paint("error+del_bg", line)
        if line.startswith("@@"):
            return self.paint("cyan", line)
        return line


def _trap_sigint(interrupt: Callable[[], None]) -> Callable[[], None]:
    """While a turn runs, Ctrl+C cancels that turn only, every time.

    asyncio.run cancels the main task on the first SIGINT but raises KeyboardInterrupt on
    the second, which would end the whole chat on the second stopped turn.
    """
    import signal
    import threading

    if threading.current_thread() is not threading.main_thread():
        return lambda: None
    loop = asyncio.get_running_loop()
    previous = signal.getsignal(signal.SIGINT)
    try:
        # POSIX: the loop's wakeup fd wakes it whichever thread receives the signal.
        loop.add_signal_handler(signal.SIGINT, interrupt)
    except (NotImplementedError, RuntimeError, ValueError):
        pass  # Windows: no loop signal handlers; a plain handler below
    else:

        def restore_posix() -> None:
            loop.remove_signal_handler(signal.SIGINT)
            signal.signal(signal.SIGINT, previous)

        return restore_posix

    def on_sigint(_signum: int, _frame: Any) -> None:
        loop.call_soon_threadsafe(interrupt)

    try:
        signal.signal(signal.SIGINT, on_sigint)
    except (ValueError, OSError):
        return lambda: None

    def restore() -> None:
        signal.signal(signal.SIGINT, previous)

    return restore


# -- terminal wiring ------------------------------------------------------------------


def terminal_display(
    write: Write, status: Callable[[], str], interrupt: Callable[[], None]
) -> TurnView | None:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        from karox6.tui_terminal import TurnDisplay
    except ImportError:
        return None
    return TurnDisplay(write, status, interrupt)


class _WorkspaceFiles:
    """Short-lived cache of workspace files for responsive ``@path`` completion."""

    def __init__(self, workspace: Path, ttl_s: float = 2.0) -> None:
        self.workspace = workspace
        self.ttl_s = ttl_s
        self._files: list[str] = []
        self._expires = 0.0

    def list(self) -> list[str]:
        now = time.monotonic()
        if now >= self._expires:
            from karox6.tools.search import _list_files

            try:
                self._files = sorted(_list_files(self.workspace, include_ignored=False))
            except OSError:
                self._files = []
            self._expires = time.monotonic() + self.ttl_s
        return self._files


def _rank_commands(prefix: str) -> list[CommandSpec]:
    """Commands for what is typed after "/": names that start with it, then names that
    hold it, then those whose description has a word starting with it."""
    needle = prefix.removeprefix("/")
    if not needle:
        return list(COMMANDS)
    starts = [c for c in COMMANDS if c.name.startswith(prefix)]
    inside = [c for c in COMMANDS if c not in starts and needle in c.name]
    described = [
        c
        for c in COMMANDS
        if c not in starts
        and c not in inside
        and len(needle) >= 3
        and any(word.startswith(needle) for word in re.findall(r"\w+", c.summary.lower()))
    ]
    return starts + inside + described


def _subsequence(needle: str, text: str) -> bool:
    """Every character of ``needle`` occurs in ``text`` in order ("tuiap" in "tui_app")."""
    it = iter(text)
    return all(ch in it for ch in needle)


def _match_workspace_files(files: list[str], fragment: str, limit: int = 25) -> list[str]:
    needle = fragment.replace("\\", "/").lower()
    if not needle:
        return files[:limit]
    prefix: list[str] = []
    base_prefix: list[str] = []
    substr: list[str] = []
    fuzzy: list[str] = []
    for rel in files:
        low = rel.lower()
        base = low.rsplit("/", 1)[-1]
        if low.startswith(needle):
            prefix.append(rel)
        elif base.startswith(needle):
            base_prefix.append(rel)
        elif needle in low:
            substr.append(rel)
        elif len(needle) >= 3 and len(fuzzy) < limit and _subsequence(needle, low):
            fuzzy.append(rel)
    return (prefix + base_prefix + substr + fuzzy)[:limit]


def build_completer(*, workspace: Path | None = None, home: Path | None = None) -> Completer:
    """Inline prompt_toolkit completer for ``/commands``, command arguments and ``@files``."""
    from prompt_toolkit.completion import CompleteEvent, Completer, Completion
    from prompt_toolkit.document import Document

    ws = workspace or Path.cwd()
    file_cache = _WorkspaceFiles(ws)

    class _ChatCompleter(Completer):
        def get_completions(
            self, document: Document, complete_event: CompleteEvent
        ) -> Iterator[Completion]:
            before = document.text_before_cursor
            if "\n" not in before and before.startswith("/"):
                yield from self._complete_command(before)
                return
            yield from self._complete_mention(before)

        def _complete_command(self, before: str) -> Iterator[Completion]:
            if " " not in before:
                prefix = before.lower()
                for spec in _rank_commands(prefix):
                    yield Completion(
                        spec.name,
                        start_position=-len(before),
                        display=spec.usage,
                        display_meta=spec.summary,
                    )
                for alias, desc in _COMMAND_ALIASES.items():
                    if alias.startswith(prefix) and prefix != "/":
                        yield Completion(
                            alias,
                            start_position=-len(before),
                            display=alias,
                            display_meta=desc,
                        )
                return
            cmd, _, rest = before.partition(" ")
            cmd = cmd.lower()
            if " " in rest.lstrip():
                return
            arg = rest.lstrip()
            low = arg.lower()
            choices: list[tuple[str, str]] = []
            if cmd == "/approve":
                choices = list(_APPROVE_ARGS)
            elif cmd == "/bridge":
                choices = list(_BRIDGE_ARGS)
            elif cmd == "/reasoning":
                choices = list(_REASONING_ARGS)
            elif cmd == "/effort":
                choices = list(_EFFORT_ARGS)
            elif cmd == "/update":
                choices = list(_UPDATE_ARGS)
            elif cmd == "/copy":
                choices = [("code", "the last code block of the answer")]
            elif cmd == "/resume":
                choices = [("last", "most recent saved session")]
                if home is not None:
                    from karox6.core.store import recent_sessions

                    for sid, title in recent_sessions(home, limit=15, workspace=ws):
                        choices.append((sid, _plain_text(title) or "saved session"))
            for value, desc in choices:
                if value.lower().startswith(low):
                    yield Completion(
                        value,
                        start_position=-len(arg),
                        display=value,
                        display_meta=desc,
                    )

        def _complete_mention(self, before: str) -> Iterator[Completion]:
            match = _MENTION_PREFIX_RE.search(before)
            if not match:
                return
            quoted = match.group(1) is not None
            fragment = match.group(1) if quoted else (match.group(2) or "")
            token_len = len(fragment) + (2 if quoted else 1)  # includes @ and optional "
            root = ws.resolve()
            for rel in _match_workspace_files(file_cache.list(), fragment):
                if '"' in rel or "\n" in rel or "\r" in rel:
                    continue
                resolved, _ = _resolve_mention(rel, root, quoted=True)
                if resolved is None:
                    continue
                inserted = f'@"{rel}"' if quoted or any(c.isspace() for c in rel) else f"@{rel}"
                meta = ""
                with contextlib.suppress(OSError):
                    meta = _format_bytes(resolved.stat().st_size)
                yield Completion(
                    inserted,
                    start_position=-token_len,
                    display=f"@{rel}",
                    display_meta=meta,
                )

    return _ChatCompleter()


def pasted_line_end(event: Any) -> bool:
    """True when this Enter is a line end inside pasted text: more keys arrived in the
    same read, so nobody typed them one by one. Terminals without bracketed paste send a
    paste as plain keys, and each of its line ends would otherwise send a message. The
    line feed of a CR LF pair is dropped, so one line end stays one."""
    from prompt_toolkit.keys import Keys

    queue = event.app.key_processor.input_queue
    waiting = [press for press in queue if press.key not in (Keys.CPRResponse, Keys.SIGINT)]
    if not waiting:
        return False
    if queue and queue[0].key == Keys.ControlJ:
        queue.popleft()
    return True


def build_key_bindings() -> KeyBindings:
    """Inline chat bindings: Enter submits, Alt+Enter / Ctrl+J insert a newline."""
    from prompt_toolkit.filters import is_searching
    from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent

    keys = KeyBindings()

    @keys.add("c-c", eager=True)
    @keys.add("<sigint>", eager=True)
    def interrupt(event: KeyPressEvent) -> None:
        event.app.exit(
            exception=_PromptInterrupt(empty=not event.current_buffer.text),
            style="class:aborting",
        )

    @keys.add("escape", "enter", filter=~is_searching)
    def _alt_enter(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    @keys.add("c-j", filter=~is_searching)
    def _ctrl_j(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    @keys.add("enter", filter=~is_searching)
    def _enter(event: KeyPressEvent) -> None:
        buf = event.current_buffer
        if buf.document.text_before_cursor.endswith("\\") and not buf.document.text_after_cursor:
            buf.delete_before_cursor(1)
            buf.insert_text("\n")
            return
        if pasted_line_end(event):
            buf.insert_text("\n")
            return
        state = buf.complete_state
        if state is not None and state.current_completion is not None:
            buf.apply_completion(state.current_completion)
            return
        buf.validate_and_handle()

    return keys


def build_history(history: Path | History | None = None) -> History:
    """Persistent file-backed history (loaded in a thread) or in-memory history."""
    from prompt_toolkit.history import FileHistory, InMemoryHistory, ThreadedHistory

    class ChatHistory(FileHistory):
        def load_history_strings(self) -> Iterable[str]:
            with contextlib.suppress(OSError):
                yield from super().load_history_strings()

        def store_string(self, string: str) -> None:
            with contextlib.suppress(OSError):
                Path(os.fsdecode(self.filename)).parent.mkdir(parents=True, exist_ok=True)
                super().store_string(string)

    if isinstance(history, Path):
        return ThreadedHistory(ChatHistory(history))
    return history if history is not None else InMemoryHistory()


def build_prompt_session(
    *,
    history: Path | History | None = None,
    workspace: Path | None = None,
    home: Path | None = None,
    encoding: str | None = None,
    input: Input | None = None,
    output: Output | None = None,
) -> PromptSession[str]:
    """Create a configured inline PromptSession (testable with PipeInput + DummyOutput)."""
    from prompt_toolkit import PromptSession

    store = build_history(history)
    more = _glyphs(encoding or getattr(sys.stdout, "encoding", None))["more"]
    resolved_home = home or (history.parent if isinstance(history, Path) else None)
    return PromptSession(
        history=store,
        multiline=True,
        prompt_continuation=lambda _w, _n, _wrap: more,
        key_bindings=build_key_bindings(),
        completer=build_completer(workspace=workspace, home=resolved_home),
        complete_while_typing=True,
        complete_in_thread=True,
        reserve_space_for_menu=6,
        input=input,
        output=output,
    )


def terminal_io(
    history: Path | None, *, workspace: Path | None = None, home: Path | None = None
) -> tuple[ReadLine, ReadLine]:
    """Prompt readers: prompt_toolkit (history, editing) if installed, else input()."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _plain_read, _plain_read  # piped input: no line editor
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.patch_stdout import patch_stdout
    except ImportError:
        # Never degrade silently: without it there is no completion, history or live status.
        sys.stderr.write(
            "KaroX: prompt_toolkit is missing, so the chat runs in plain mode (no completion, "
            "history or live status). Reinstall KaroX with its dependencies to fix it.\n"
        )
        return _plain_read, _plain_read

    main = build_prompt_session(history=history, workspace=workspace, home=home)
    from karox6.tui_input import coalescing_input

    merged = coalescing_input(main.app.input)
    if merged is not main.app.input:
        main.app.input = merged
        main.app.ttimeoutlen = 0.12  # a merged paste is handed over this soon after its end
    answers: PromptSession[str] | None = None

    async def read_line(prompt: str) -> str:
        return await main.prompt_async(prompt)

    async def read_answer(prompt: str) -> str:
        from karox6.tui_terminal import question_bindings

        nonlocal answers
        if answers is None:
            answers = PromptSession(key_bindings=question_bindings())
            answers.app.ttimeoutlen = 0.12
        # Tool events of parallel calls may print while the question is open.
        with patch_stdout(raw=True):
            return await answers.prompt_async(prompt, handle_sigint=False)

    return read_line, read_answer


def secret_reader() -> ReadLine:
    """A prompt that hides what is typed (API keys)."""
    if sys.stdin.isatty() and sys.stdout.isatty():
        try:
            from prompt_toolkit import PromptSession

            from karox6.tui_terminal import question_bindings

            session: PromptSession[str] = PromptSession(key_bindings=question_bindings())
            session.app.ttimeoutlen = 0.12

            async def hidden(prompt: str) -> str:
                return await session.prompt_async(prompt, is_password=True, handle_sigint=False)

            return hidden
        except ImportError:
            pass

    async def fallback(prompt: str) -> str:
        import getpass

        return await asyncio.to_thread(getpass.getpass, prompt)

    return fallback


async def _plain_read(prompt: str) -> str:
    return await asyncio.to_thread(_plain_input, prompt)


def _plain_input(prompt: str) -> str:
    stream = getattr(sys.stdin, "buffer", None)
    if sys.stdin.isatty() or stream is None:
        return input(prompt)
    stdout_write(prompt)
    data: bytes = stream.readline()
    if not data:
        raise EOFError
    try:
        line = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        line = data.decode(sys.stdin.encoding or "utf-8", errors="replace")
    return line.removesuffix("\n").removesuffix("\r")


def color_enabled(stream: Any = None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def stdout_write(text: str) -> None:
    if not sys.stdout.isatty():
        text = _plain_text(text)
    elif sys.platform == "win32" and "\x1b" in text:
        # Win32Output sets native attributes in conhost and negotiates VT mode in
        # Windows Terminal. Direct writes of SGR codes may appear literally.
        if sys.stdout is sys.__stdout__:
            try:
                from prompt_toolkit import print_formatted_text
                from prompt_toolkit.application import get_app_session
                from prompt_toolkit.formatted_text import ANSI
            except ImportError:
                pass
            else:
                if text.startswith(_CLEAR_SCREEN):
                    output = get_app_session().output
                    output.erase_screen()
                    output.cursor_goto(0, 0)
                    output.flush()
                    text = text.removeprefix(_CLEAR_SCREEN)
                print_formatted_text(ANSI(text), end="")
                return
        text = _plain_text(text)
    sys.stdout.write(text)
    sys.stdout.flush()
