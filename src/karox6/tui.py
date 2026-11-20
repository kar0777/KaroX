"""Interactive terminal UI: an inline chat on the shared core (M3).

The chat streams into the terminal's normal scrollback (no full-screen takeover), so
output stays searchable and copyable after the session ends. Line editing and history
come from prompt_toolkit when the ``tui`` extra is installed; without it the chat falls
back to plain ``input()``. All I/O goes through two injected callables, so the app is
tested without a terminal.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from karox6 import __version__
from karox6.bridge import Bridge, BridgeError
from karox6.core import events as ev
from karox6.core.digest import brief, outcome
from karox6.core.session import Session, SessionConfig
from karox6.core.store import recent_sessions, resume_kwargs
from karox6.tools.base import ApprovalRequest, Tool

ReadLine = Callable[[str], Awaitable[str]]  # raises EOFError at end of input
Write = Callable[[str], None]

APPROVAL_MODES = ("ask", "allow", "deny")
DIFF_CAP = 40_000
HELP = """Commands:
  /help                     this help
  /diff                     workspace changes (git diff and untracked files)
  /status                   session, model, tokens and cost
  /new                      start a fresh session
  /sessions                 recent sessions
  /resume [ID|last]         continue a saved session
  /approve ask|allow|deny   dangerous commands: ask, run, or refuse (now: {mode})
  /reasoning                show or hide the model's reasoning
  /bridge [agent]           connect ChatGPT (web), Claude etc.: start the MCP server with a
                            Tailscale Funnel link (default: the chat model works through
                            KaroX's tools; agent: they hand tasks to KaroX's own model)
  /bridge url | log | stop  the secret no-auth link, recent requests, stop (also on exit)
  /quit                     leave (Ctrl+D works too)
Ctrl+C stops the running turn. End a line with \\ to continue on the next line."""

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


def _box(title: str, rows: list[tuple[str, str]], fancy: bool) -> str:
    """A framed panel: label column, then values (ANSI colors do not count as width)."""
    tl, tr, bl, br, h, v = _BOX_FANCY if fancy else _BOX_ASCII
    wide = max((len(k) for k, _ in rows), default=0)
    lines = [(f"{k:<{wide}}  {val}" if wide else val) for k, val in rows]
    seen = [len(_ANSI.sub("", ln)) for ln in lines]
    width = max([*seen, len(title) + 2]) + 2
    out = [tl + h + f" {title} " + h * (width - len(title) - 3) + tr]
    out += [f"{v} {ln}{' ' * (width - n - 1)}{v}" for ln, n in zip(lines, seen, strict=True)]
    out.append(bl + h * width + br)
    return "\n".join(out)


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
    ) -> None:
        if approval_mode not in APPROVAL_MODES:
            raise ValueError(f"approval mode must be one of {APPROVAL_MODES}")
        self.config = config
        self.provider = provider
        self.read_line = read_line
        self.read_answer = read_answer or read_line
        self.write = write
        self.approval_mode = approval_mode
        self.paint = Palette(color)
        self.glyph = _glyphs(encoding)
        self.extra_tools = list(extra_tools or [])
        self.show_reasoning = False
        self._stream: str | None = None  # "text" | "reasoning" while a delta stream is open
        self._streamed_text = False
        self.session = self._new_session(**(session_kwargs or {}))
        self.bridge = Bridge(config.home)
        self.bridge_agent_args = list(bridge_agent_args or [])
        self.bridge_env = dict(bridge_env or {})

    # -- lifecycle ------------------------------------------------------------------

    def _new_session(self, **kwargs: Any) -> Session:
        return Session(
            self.config,
            self.provider,
            approver=self._approve,
            extra_tools=self.extra_tools,
            **kwargs,
        )

    async def run(self) -> int:
        self.write(self._banner())
        while True:
            try:
                text = (await self._read_prompt()).strip()
            except EOFError:
                break
            except KeyboardInterrupt:
                continue
            if not text:
                continue
            if text.startswith("/"):
                if await self.command(text) == "quit":
                    break
                continue
            await self.turn(text)
        await self.close()
        self.write("\n")
        return 0

    async def close(self) -> None:
        """Stop what the chat started beside it (the bridge and its Tailscale route)."""
        if self.bridge.running():
            self.write("stopping the bridge...\n")
            await asyncio.to_thread(self.bridge.stop)

    async def _read_prompt(self) -> str:
        lines: list[str] = []
        while True:
            line = await self.read_line(self.glyph["more"] if lines else self.glyph["prompt"])
            if line.endswith("\\"):
                lines.append(line[:-1])
                continue
            lines.append(line)
            return "\n".join(lines)

    def _banner(self) -> str:
        cfg = self.config
        head = self.paint("bold", f"KaroX {__version__}")
        return (
            f"{head}{self.glyph['dot']}{cfg.model}{self.glyph['dot']}{cfg.workspace}\n"
            + self.paint("dim", "Type a task; /help lists commands.")
            + "\n"
        )

    # -- turns ----------------------------------------------------------------------

    async def turn(self, text: str) -> str:
        """Run one user turn; Ctrl+C (cancellation of the caller) stops only this turn."""
        status = "error"
        worker = asyncio.create_task(self._consume(text))
        restore = _trap_sigint(worker)
        try:
            status = await worker
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                current.uncancel()
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            self._end_stream()
            self.write(self.paint("yellow", "[stopped]") + "\n")
            status = "cancelled"
        finally:
            restore()
        return status

    async def _consume(self, text: str) -> str:
        status = "error"
        async for event in self.session.run(text):
            self.render(event)
            if isinstance(event, ev.RunFinished):
                status = event.status
        return status

    def render(self, event: ev.Event) -> None:
        p, g = self.paint, self.glyph
        if isinstance(event, ev.TextDelta):
            self._open("text")
            self._streamed_text = True
            self.write(event.text)
            return
        if isinstance(event, ev.ReasoningDelta):
            if self.show_reasoning:
                self._open("reasoning")
                self.write(p("dim", event.text))
            return
        if isinstance(event, ev.AssistantMessage):
            if event.text and not self._streamed_text:
                self._end_stream()
                self.write(event.text.rstrip() + "\n")
            self._streamed_text = False
            self._end_stream()
            return
        self._end_stream()
        if isinstance(event, ev.ToolStarted):
            pad = "    " if "/" in event.name else ""  # a subagent's call, under its agent line
            self.write(
                f"{pad}{p('cyan', g['tool'])} {p('bold', event.name)} {brief(event.arguments)}\n"
            )
        elif isinstance(event, ev.ToolFinished):
            pad = "    " if "/" in event.name else ""
            mark = p("red", g["fail"]) if event.is_error else p("green", g["ok"])
            secs = f"{event.duration_ms / 1000:.1f}s"
            self.write(f"{pad}  {mark} {p('dim', secs)} {p('dim', outcome(event))}\n")
        elif isinstance(event, ev.Retry):
            self.write(
                p("dim", f"  retry {event.attempt} in {event.delay_s:.0f}s: {event.error[:120]}")
                + "\n"
            )
        elif isinstance(event, ev.Notice):
            self.write(p("yellow", f"  [{event.kind}] {event.text[:200]}") + "\n")
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
            self.write(p("red", f"error: {event.message}") + "\n")
        elif isinstance(event, ev.RunFinished):
            self.write(self._footer(event) + "\n")

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
        reason = f" {event.reason}" if event.reason and event.status != "completed" else ""
        return f"{head}{reason} " + self.paint("dim", self.glyph["dot"].join(parts))

    def _open(self, kind: str) -> None:
        if self._stream != kind:
            self._end_stream()
            self._stream = kind
            if kind == "reasoning":
                self.write(self.paint("dim", "thinking: "))

    def _end_stream(self) -> None:
        if self._stream is not None:
            self.write("\n")
            self._stream = None

    # -- approvals ------------------------------------------------------------------

    async def _approve(self, request: ApprovalRequest) -> bool:
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
            self.paint("yellow", f"! {request.reason}")
            + f"\n  rule: {request.rule}\n  command: {request.summary}\n"
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
        if name in ("/quit", "/exit", "/q"):
            return "quit"
        if name == "/help":
            self.write(HELP.format(mode=self.approval_mode) + "\n")
        elif name == "/diff":
            await self._show_diff()
        elif name == "/status":
            self._show_status()
        elif name == "/new":
            self.session = self._new_session()
            self.write(f"new session {self.session.session_id}\n")
        elif name == "/sessions":
            rows = recent_sessions(self.config.home)
            if not rows:
                self.write("no saved sessions\n")
            for session_id, title in rows:
                mark = "*" if session_id == self.session.session_id else " "
                self.write(f"{mark} {session_id}  {self.paint('dim', title)}\n")
        elif name == "/resume":
            try:
                kwargs = resume_kwargs(
                    self.config.home, arg or "last", exclude=self.session.session_id
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
        elif name == "/bridge":
            await self._bridge(arg)
        elif name == "/reasoning":
            self.show_reasoning = not self.show_reasoning
            self.write(f"reasoning {'shown' if self.show_reasoning else 'hidden'}\n")
        else:
            self.write(f"unknown command {name}; /help lists commands\n")
        return None

    async def _bridge(self, arg: str) -> None:
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

    def _show_status(self) -> None:
        s = self.session
        usage = s.usage.to_dict()
        lines = [
            f"session    {s.session_id}",
            f"model      {self.config.model} ({self.config.provider_name})",
            f"workspace  {s.workspace}",
            f"messages   {len(s.messages)}",
            f"tokens     {usage.get('input_tokens', 0)} in "
            f"({usage.get('cached_input_tokens', 0)} cached) / "
            f"{usage.get('output_tokens', 0)} out",
            f"dangerous  {self.approval_mode}",
        ]
        if self.config.price:
            lines.append(f"cost       ${s.cost_usd:.4f}")
        if self.bridge.running():
            lines.append(f"bridge     {self.bridge.mode} {self.bridge.url}")
        self.write("\n".join(lines) + "\n")

    async def _show_diff(self) -> None:
        from karox6.mcp.runs import git_diff

        workspace = self.session.workspace
        text, untracked = await asyncio.to_thread(git_diff, workspace)
        if text is None:
            changed = await asyncio.to_thread(self.session.files.changed_files)
            self.write("not a git repository; files changed this session:\n")
            for path in changed or ["(none)"]:
                self.write(f"  {path}\n")
            return
        if not text.strip() and not untracked:
            self.write("no changes\n")
            return
        shown = text[:DIFF_CAP]
        for line in shown.splitlines():
            self.write(self._diff_line(line) + "\n")
        if len(text) > DIFF_CAP:
            self.write(self.paint("dim", f"... {len(text) - DIFF_CAP} more characters") + "\n")
        if untracked:
            self.write(self.paint("bold", "untracked:") + "\n")
            for path in untracked[:50]:
                self.write(self.paint("green", f"  {path}") + "\n")

    def _diff_line(self, line: str) -> str:
        if line.startswith(("+++", "---", "diff --git", "index ")):
            return self.paint("bold", line)
        if line.startswith("+"):
            return self.paint("green", line)
        if line.startswith("-"):
            return self.paint("red", line)
        if line.startswith("@@"):
            return self.paint("cyan", line)
        return line


def _trap_sigint(worker: asyncio.Task[Any]) -> Callable[[], None]:
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
        loop.add_signal_handler(signal.SIGINT, worker.cancel)
    except (NotImplementedError, RuntimeError, ValueError):
        pass  # Windows: no loop signal handlers; a plain handler below
    else:

        def restore_posix() -> None:
            loop.remove_signal_handler(signal.SIGINT)
            signal.signal(signal.SIGINT, previous)

        return restore_posix

    def on_sigint(_signum: int, _frame: Any) -> None:
        loop.call_soon_threadsafe(worker.cancel)

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


def terminal_io(history: Path | None) -> tuple[ReadLine, ReadLine]:
    """Prompt readers: prompt_toolkit (history, editing) if installed, else input()."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return _plain_read, _plain_read  # piped input: no line editor
    try:
        from prompt_toolkit import PromptSession
        from prompt_toolkit.history import FileHistory, InMemoryHistory
        from prompt_toolkit.patch_stdout import patch_stdout
    except ImportError:
        return _plain_read, _plain_read

    store: Any = InMemoryHistory()
    if history is not None:
        with contextlib.suppress(OSError):
            history.parent.mkdir(parents=True, exist_ok=True)
            store = FileHistory(str(history))
    main: PromptSession[str] = PromptSession(history=store)
    answers: PromptSession[str] = PromptSession()

    async def read_line(prompt: str) -> str:
        return await main.prompt_async(prompt)

    async def read_answer(prompt: str) -> str:
        # Tool events of parallel calls may print while the question is open.
        with patch_stdout(raw=True):
            return await answers.prompt_async(prompt)

    return read_line, read_answer


async def _plain_read(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


def color_enabled(stream: Any = None) -> bool:
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR"):
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def stdout_write(text: str) -> None:
    sys.stdout.write(text)
    sys.stdout.flush()
