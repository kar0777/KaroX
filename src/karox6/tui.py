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
from collections.abc import Awaitable, Callable, Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import TYPE_CHECKING, Any, Protocol

from karox6 import __version__
from karox6.core import events as ev
from karox6.core.digest import brief, is_tool_failure, outcome

if TYPE_CHECKING:
    from types import TracebackType

    from prompt_toolkit import PromptSession
    from prompt_toolkit.completion import Completer
    from prompt_toolkit.history import History
    from prompt_toolkit.input import Input
    from prompt_toolkit.key_binding import KeyBindings
    from prompt_toolkit.output import Output

    from karox6.bridge import Bridge
    from karox6.core.config import SessionConfig
    from karox6.core.session import Session
    from karox6.tools.base import ApprovalRequest, Tool


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


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    """Current chat metadata displayed by the full-screen status bar."""

    model: str
    provider: str
    workspace_name: str
    branch: str
    approval_mode: str
    running: bool
    activity: str
    total_tokens: int
    context_used: int
    context_window: int
    context_pct: int
    session_id: str | None


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
        "/setup",
        "/bridge",
    )
)
DIFF_CAP = 40_000
_CLEAR_SCREEN = "\x1b[2J\x1b[H"
MENTION_MAX_BYTES = 64 * 1024
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
    CommandSpec("/model", "/model [NAME]", "show or change the model", "Model & Setup"),
    CommandSpec(
        "/reasoning", "/reasoning [show|hide]", "show or hide model reasoning", "Model & Setup"
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

_REASONING_ARGS: tuple[tuple[str, str], ...] = (
    ("show", "display model reasoning as it streams"),
    ("hide", "hide model reasoning output"),
)

_HELP_GROUPS = ("Session", "Workspace & Context", "Model & Setup", "Tools & Bridge")


_HELP_SHORTCUTS: tuple[tuple[str, str], ...] = (
    ("@path", "mention a workspace file to attach it to your message"),
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


_CODES = {"dim": "2", "bold": "1", "red": "31", "green": "32", "yellow": "33", "cyan": "36"}


class Palette:
    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled

    def __call__(self, style: str, text: str) -> str:
        if not self.enabled or not text:
            return text
        return f"\x1b[{_CODES[style]}m{text}\x1b[0m"


def _glyphs(encoding: str | None) -> dict[str, str]:
    fancy = {"tool": "●", "ok": "✓", "fail": "✗", "prompt": "› ", "more": "… ", "dot": " · "}
    try:
        "".join(fancy.values()).encode(encoding or "ascii")
    except (LookupError, UnicodeEncodeError):
        return {"tool": "*", "ok": "ok", "fail": "x", "prompt": "> ", "more": ". ", "dot": " | "}
    try:
        "".join(_BOX_FANCY).encode(encoding or "ascii")
        fancy["box"] = "fancy"
    except (LookupError, UnicodeEncodeError):
        fancy["box"] = "ascii"
    return fancy


_BOX_FANCY = ("╭", "╮", "╰", "╯", "─", "│")
_BOX_ASCII = ("+", "+", "+", "+", "-", "|")
_ANSI = re.compile("\x1b\\[[0-9;]*m")
_TERMINAL_CONTROL = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"
    r"|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)"
    r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]"
)


def _plain_text(text: str) -> str:
    # Keep newlines and tabs. Model and tool output must never move the terminal cursor.
    return _TERMINAL_CONTROL.sub("", text)


def _git_branch(workspace: Path) -> str:
    """The checked-out branch read from .git/HEAD (no git process: it runs before the first
    prompt); empty when the project is not a git checkout or HEAD is detached."""
    for folder in (workspace, *workspace.parents):
        head = folder / ".git"
        if head.is_file():  # a worktree: "gitdir: <path>"
            try:
                target = head.read_text(encoding="utf-8").split(":", 1)[1].strip()
            except (OSError, IndexError, UnicodeDecodeError):
                return ""
            head = Path(target) if Path(target).is_absolute() else folder / target
        if head.is_dir():
            try:
                ref = (head / "HEAD").read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                return ""
            return ref.removeprefix("ref: refs/heads/") if ref.startswith("ref: ") else ""
    return ""


def _box(title: str, rows: list[tuple[str, str]], fancy: bool) -> str:
    """A framed panel: label column, then values (ANSI colors do not count as width)."""
    tl, tr, bl, br, h, v = _BOX_FANCY if fancy else _BOX_ASCII
    wide = max((len(k) for k, _ in rows), default=0)
    lines = [(f"{k:<{wide}}  {val}" if wide else val) for k, val in rows]
    seen = [len(_ANSI.sub("", ln)) for ln in lines]
    shown = len(_ANSI.sub("", title))
    width = max([*seen, shown + 2]) + 2
    out = [tl + h + f" {title} " + h * (width - shown - 3) + tr]
    out += [f"{v} {ln}{' ' * (width - n - 1)}{v}" for ln, n in zip(lines, seen, strict=True)]
    out.append(bl + h * width + br)
    return "\n".join(out)


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


class _PromptInterrupt(KeyboardInterrupt):
    def __init__(self, empty: bool) -> None:
        self.empty = empty


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
    ) -> None:
        if approval_mode not in APPROVAL_MODES:
            raise ValueError(f"approval mode must be one of {APPROVAL_MODES}")
        self.config = config
        self.provider = provider
        self.read_line = read_line
        self._read_answer = read_answer or read_line
        self.write = write
        self.approval_mode = approval_mode
        self.paint = Palette(color)
        self.glyph = _glyphs(encoding)
        self.extra_tools = list(extra_tools or [])
        self.show_reasoning = False
        self._stream: str | None = None  # "text" | "reasoning" while a delta stream is open
        self._streamed_text = False
        self._stream_newline = False
        self._initialize = initialize
        self._before_turn = before_turn
        self._session_task: asyncio.Task[Session] | None = None
        self._display_factory = display_factory
        self._display: TurnView | None = None
        self._branch: str | None = None
        self._turn_started = 0.0
        self._phase = ""
        self._active_tools: dict[str, ev.ToolStarted] = {}
        self._had_tools = False
        self.session: Session
        if initialize is None:
            self.session = self._new_session(**(session_kwargs or {}))
        self._bridge_instance: Bridge | None = None
        self.bridge_agent_args = list(bridge_agent_args or [])
        self.bridge_env = dict(bridge_env or {})
        self._read_secret = read_secret or self._read_answer
        self._question_lock = asyncio.Lock()
        self._turn_running = False

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

    def _new_session(self, **kwargs: Any) -> Session:
        from karox6.core.session import Session

        return Session(
            self.config,
            self.provider,
            approver=self._approve,
            extra_tools=self.extra_tools,
            **kwargs,
        )

    async def run(self) -> int:
        self.write(self._banner())
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
        if self._session_task is not None:
            with contextlib.suppress(Exception):
                await self._session_task
            self._session_task = None
        if self._bridge_instance is not None and self._bridge_instance.running():
            self.write("stopping the bridge...\n")
            await asyncio.to_thread(self._bridge_instance.stop)

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
            window = cfg.effective_window(s._learned_window)
            pct = min(round(used / window * 100), 100) if window else 0
            session_id: str | None = s.session_id
        else:
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
        dot = self.glyph["dot"]
        project = _plain_text(str(cfg.workspace))
        branch = self._branch_name()
        if branch:
            project += self.paint("dim", f"{dot}{_plain_text(branch)}")
        rows = [
            ("model", _plain_text(cfg.model) + self.paint("dim", f"{dot}{cfg.provider_name}")),
            ("project", project),
            ("approvals", f"{self.approval_mode}" + self.paint("dim", "  (/approve to change)")),
            ("", ""),
            ("", self.paint("dim", f"/ commands{dot}@ attach a file{dot}Alt+Enter new line")),
            ("", self.paint("dim", f"Esc stops the agent{dot}/help{dot}Ctrl+D quits")),
        ]
        fancy = self.glyph.get("box", "ascii") == "fancy"
        card = _box(self.paint("bold", f"KaroX {__version__}"), rows, fancy)
        return card + "\n" + self.paint("dim", "Type a task to start.") + "\n"

    # -- turns ----------------------------------------------------------------------

    async def turn(self, text: str) -> str:
        """Run one user turn with interruptible terminal output."""
        phase = "Preparing session" if self._initialize is not None else "Model thinking"
        return await self._run_with_display(lambda: self._consume(text), phase)

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
                    self.write(self.paint("yellow", "[stopped]") + "\n")
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

    def _progress(self) -> str:
        elapsed = time.monotonic() - self._turn_started
        spinner = "|/-\\"[int(elapsed * 8) % 4]
        phase = self._phase
        if self._active_tools:
            names = (
                " ".join(_plain_text(call.name).split())[:80]
                for call in self._active_tools.values()
            )
            phase = "Running " + ", ".join(dict.fromkeys(names))
        return f"{spinner} {elapsed:.1f}s  {phase}"

    async def _consume(self, text: str) -> str:
        await self._ensure_session()
        if self._before_turn is not None:
            await self._before_turn(self)
            self._before_turn = None
        if _MENTION_RE.search(text):
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
        async with contextlib.aclosing(self.session.run(text)) as events:
            async for event in events:
                if isinstance(event, ev.RunFinished):
                    status = event.status
                    if status == "cancelled":
                        continue  # the turn prints its stop marker after cleanup
                self.render(event)
                count += 1
                if count % 32 == 0:
                    await asyncio.sleep(0)
        return status

    def render(self, event: ev.Event) -> None:
        p, g = self.paint, self.glyph
        if isinstance(event, ev.TextDelta):
            self._open("text")
            self._phase = "Model responding"
            self._streamed_text = True
            text = _plain_text(event.text)
            self.write(text)
            if text:
                self._stream_newline = text.endswith("\n")
            return
        if isinstance(event, ev.ReasoningDelta):
            self._phase = "Model thinking"
            if self.show_reasoning:
                self._open("reasoning")
                text = _plain_text(event.text)
                self.write(p("dim", text))
                if text:
                    self._stream_newline = text.endswith("\n")
            return
        if isinstance(event, ev.AssistantMessage):
            if event.text and not self._streamed_text:
                self._open("text")
                self.write(_plain_text(event.text).rstrip() + "\n")
                self._stream_newline = True
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
            error = _plain_text(event.error)[:120]
            self.write(
                p(
                    "dim",
                    f"  retry {event.attempt} in {event.delay_s:.0f}s: {error}",
                )
                + "\n"
            )
        elif isinstance(event, ev.Notice):
            self.write(p("yellow", f"  [{event.kind}] {_plain_text(event.text)[:200]}") + "\n")
        elif isinstance(event, ev.Compacted):
            self.write(
                p(
                    "dim",
                    f"  context compacted ({event.mode}): "
                    f"{event.tokens_before} -> {event.tokens_after} tokens",
                )
                + "\n"
            )
        elif isinstance(event, ev.ErrorEvent):
            self.write(p("red", f"{g['fail']} error: {_plain_text(event.message)}") + "\n")
        elif isinstance(event, ev.RunFinished):
            self._phase = "Finishing"
            self.write(self._footer(event) + "\n")

    def _render_tool_finished(self, event: ev.ToolFinished) -> None:
        start = self._active_tools.pop(event.call_id, None)
        self._phase = "Model thinking"
        self._had_tools = True
        p, g = self.paint, self.glyph
        name = " ".join(_plain_text(event.name).split())[:80]
        target = _plain_text(brief(start.arguments)) if start is not None else ""
        pad = "    " if "/" in name else ""  # a subagent's call
        head = f"{pad}{p('cyan', g['tool'])} {p('bold', name)}" + (f" {target}" if target else "")
        failed = is_tool_failure(event)
        mark = p("red", g["fail"]) if failed else p("green", g["ok"])
        stats = (
            _edit_diff_stats(event.name, start.arguments)
            if start is not None and not failed
            else None
        )
        if stats is None:
            summary = _plain_text(outcome(event)).replace(" · ", g["dot"])
            detail = p("red" if failed else "dim", summary)
        else:
            detail = self._paint_diff_stats(*stats)
        took = p("dim", f"({event.duration_ms / 1000:.1f}s)")
        self.write(f"{head}  {mark}" + (f" {detail}" if detail else "") + f" {took}\n")

    def _paint_diff_stats(self, added: int, removed: int) -> str:
        return f"{self.paint('green', f'+{added}')} {self.paint('red', f'-{removed}')}"

    def _footer(self, event: ev.RunFinished) -> str:
        usage = event.usage
        parts = [
            f"{event.steps} steps",
            f"{event.elapsed_s:.0f}s",
            f"{_k(usage.get('input_tokens', 0))} in / {_k(usage.get('output_tokens', 0))} out",
        ]
        if event.cost_usd is not None:
            parts.append(f"${event.cost_usd:.4f}")
        if event.files_changed:
            parts.append(f"{len(event.files_changed)} files changed (/diff)")
        color = "green" if event.status == "completed" else "yellow"
        head = self.paint(color, f"[{event.status}]")
        reason = (
            f" {_plain_text(event.reason)}" if event.reason and event.status != "completed" else ""
        )
        return f"{head}{reason} " + self.paint("dim", self.glyph["dot"].join(parts))

    def _open(self, kind: str) -> None:
        if self._stream != kind:
            self._end_stream()
            if kind == "text" and self._had_tools:
                self.write("\n")
                self._had_tools = False
            self._stream = kind
            if kind == "reasoning":
                self.write(self.paint("dim", "thinking: "))

    def _end_stream(self) -> None:
        if self._stream is not None:
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
            self.write(f"new session {self.session.session_id}\n")
        elif name == "/sessions":
            from karox6.core.store import recent_sessions

            rows = recent_sessions(self.config.home, workspace=self.config.workspace)
            if not rows:
                self.write("no saved sessions\n")
            for session_id, title in rows:
                mark = "*" if session_id == self.session.session_id else " "
                self.write(f"{mark} {session_id}  {self.paint('dim', title)}\n")
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
                count = len(self.session.messages)
                self.write(f"resumed {self.session.session_id} ({count} messages)\n")
        elif name == "/approve":
            if arg in APPROVAL_MODES:
                self.approval_mode = arg
            self.write(f"dangerous commands: {self.approval_mode}\n")
        elif name == "/key":
            await self._set_key()
        elif name == "/model":
            if arg and arg != self.config.model:
                self.config.model = arg
                self.session.forget_model_state(provider_changed=False)
            self.write(f"model: {self.config.model}\n")
        elif name == "/setup":
            await self._setup()
        elif name == "/bridge":
            await self._bridge(arg)
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
            self.write(f"unknown command {name}; /help lists commands\n")
        return None

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
        where = save_key(self.config.home, key)
        if hasattr(self.provider, "api_key"):
            self.provider.api_key = key
        if self.bridge_env:
            self.bridge_env = {name: key for name in self.bridge_env}
        self.write(f"key saved (encrypted for your user) in {where}; in use now\n")

    async def _setup(self) -> None:
        from karox6.providers import make_provider
        from karox6.setup import run_setup

        try:
            choice = await run_setup(
                self.read_answer, self.read_secret, self.write, self.config.home
            )
        except (EOFError, KeyboardInterrupt):
            self.write("setup cancelled\n")
            return
        if choice is None:
            return
        key = choice.key or getattr(self.provider, "api_key", None)
        old = self.provider
        self.provider = make_provider(choice.provider, base_url=choice.base_url, api_key=key)
        self.session.provider = self.provider
        switched = choice.provider != self.config.provider_name
        self.session.forget_model_state(provider_changed=switched)
        self.config.model = choice.model
        self.config.provider_name = choice.provider
        with contextlib.suppress(Exception):
            await old.aclose()
        self.write(f"now using {choice.model}\n")

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
        used = self.session.context_tokens()
        window = self.config.effective_window(self.session._learned_window)
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
            return self.paint("green", line)
        if line.startswith("-"):
            return self.paint("red", line)
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


def _k(n: int) -> str:
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


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


def _match_workspace_files(files: list[str], fragment: str, limit: int = 25) -> list[str]:
    needle = fragment.replace("\\", "/").lower()
    if not needle:
        return files[:limit]
    prefix: list[str] = []
    base_prefix: list[str] = []
    substr: list[str] = []
    for rel in files:
        low = rel.lower()
        base = low.rsplit("/", 1)[-1]
        if low.startswith(needle):
            prefix.append(rel)
        elif base.startswith(needle):
            base_prefix.append(rel)
        elif needle in low:
            substr.append(rel)
    return (prefix + base_prefix + substr)[:limit]


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
                for spec in COMMANDS:
                    if spec.name.startswith(prefix):
                        yield Completion(
                            spec.name,
                            start_position=-len(before),
                            display=spec.name,
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
            elif cmd == "/resume":
                choices = [("last", "most recent saved session")]
                if home is not None:
                    from karox6.core.store import recent_sessions

                    for sid, title in recent_sessions(home, limit=15, workspace=ws):
                        choices.append((sid, title or "saved session"))
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
        state = buf.complete_state
        if state is not None and state.current_completion is not None:
            buf.apply_completion(state.current_completion)
            return
        if buf.document.text_before_cursor.endswith("\\") and not buf.document.text_after_cursor:
            buf.delete_before_cursor(1)
            buf.insert_text("\n")
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
