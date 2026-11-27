"""`karox` command line entry point.

Imports stay inside the functions: `karox --version` must not pay for the agent
stack (startup budget 300 ms, checked in CI by scripts/perf_check.py).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from collections.abc import Callable, Coroutine, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from karox6.core.events import Event
    from karox6.tools.base import ApprovalRequest

EXIT_CODES = {"completed": 0, "error": 1, "limit": 3, "loop": 4, "cancelled": 130}
SIGTERM_EXIT = 143  # 128 + SIGTERM


class _Interrupts:
    """Ctrl+C (SIGINT) for a chat, taken before its first frame is painted.

    Python's own handler raises KeyboardInterrupt wherever the main thread happens to be,
    and while a chat starts that is inside the imports. Raised in an `exec` of source text
    (the standard library builds namedtuples and dataclasses that way) it makes the
    interpreter end by the signal although the chat caught it and returned 0; raised in a
    callback it is printed and dropped, and the key is lost; raised in an extension
    module's import it can come out as another error. This handler does not raise there:
    until the event loop runs it only counts the interrupt, and the loop's first step
    ends the start. From then on it cancels the main task, as asyncio's handler does
    (asyncio.run puts that one over Python's default handler only, so not over this
    one), and like it raises KeyboardInterrupt when asked again.
    """

    def __init__(self) -> None:
        import signal

        self.seen = 0
        self._cancel: Callable[[], None] | None = None
        self._previous: Any = signal.getsignal(signal.SIGINT)
        self._installed = False
        if self._previous is None:  # set by other code than Python's: theirs to handle
            return
        if self._previous is signal.SIG_IGN and not (sys.stdin.isatty() and sys.stdout.isatty()):
            return  # started to ignore it and no keyboard of its own: it stays ignored
        try:
            signal.signal(signal.SIGINT, self._on_sigint)
        except ValueError:  # not the main thread
            return
        self._installed = True

    def _on_sigint(self, _signum: int, _frame: Any) -> None:
        self.seen += 1
        if self._cancel is None:
            return
        if self.seen > 1:
            raise KeyboardInterrupt
        self._cancel()

    def attach(self, cancel: Callable[[], None]) -> bool:
        """The event loop runs: an interrupt calls ``cancel`` from now on. True when one
        has come already, and the start ends here."""
        self._cancel = cancel
        return self.seen > 0

    def restore(self) -> None:
        if self._installed:
            import signal

            self._installed = False
            signal.signal(signal.SIGINT, self._previous)


def run_async[T](main_coro: Coroutine[Any, Any, T], interrupts: _Interrupts | None = None) -> T:
    """asyncio.run, where SIGTERM (POSIX) stops the program the way Ctrl+C does: the main
    task is cancelled, so `finally` blocks run, child process trees are killed and the
    session is recorded. MCP clients and process managers stop a server with SIGTERM;
    left to Python's default the process just vanished and its commands kept running.
    On Windows CTRL_BREAK (SIGBREAK) is handled the same way: it is how `karox mcp
    --supervise` stops its server, and Python's default ends the process without cleanup
    (a Tailscale route would outlive it). With ``interrupts`` (a chat) Ctrl+C is theirs:
    it cancels the main task and ends as KeyboardInterrupt, as with asyncio's handler."""
    import asyncio
    import signal

    terminated = False

    async def guarded() -> T:
        loop = asyncio.get_running_loop()
        task = asyncio.current_task()

        def on_sigterm() -> None:
            nonlocal terminated
            if terminated:  # asked again while cleaning up: stop now (commands go first
                os._exit(SIGTERM_EXIT)  # in the cleanup, so they are already stopped)
            terminated = True
            if task is not None:
                task.cancel()

        if interrupts is not None:

            def on_interrupt() -> None:
                if task is not None:
                    task.cancel()
                with contextlib.suppress(RuntimeError):  # the loop has just been closed
                    loop.call_soon_threadsafe(lambda: None)  # wake a loop that waits for I/O

            if interrupts.attach(on_interrupt):  # Ctrl+C came while the chat was imported
                main_coro.close()
                raise asyncio.CancelledError

        installed = False
        previous: Any = None
        if sys.platform != "win32":
            try:
                loop.add_signal_handler(signal.SIGTERM, on_sigterm)
                installed = True
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        else:

            def on_sigbreak(_signum: int, _frame: Any) -> None:
                on_sigterm()
                loop.call_soon_threadsafe(lambda: None)  # wake a loop that waits for I/O

            with contextlib.suppress(ValueError):  # not the main thread
                previous = signal.signal(signal.SIGBREAK, on_sigbreak)
        try:
            return await main_coro
        finally:
            if installed:
                loop.remove_signal_handler(signal.SIGTERM)
            if sys.platform == "win32" and previous is not None:
                signal.signal(signal.SIGBREAK, previous)

    try:
        result = asyncio.run(guarded())
    except asyncio.CancelledError:
        if terminated:
            raise SystemExit(SIGTERM_EXIT) from None
        if interrupts is not None and interrupts.seen:
            raise KeyboardInterrupt from None
        raise
    if terminated:
        raise SystemExit(SIGTERM_EXIT)
    return result


def _parser(settings: dict[str, Any] | None = None) -> argparse.ArgumentParser:
    from karox6 import __version__

    parser = argparse.ArgumentParser(prog="karox", description="KaroX 6 coding agent")
    parser.add_argument("--version", action="version", version=f"karox {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run one task headless (text or JSON-lines output)")
    run.add_argument("task", nargs="*", help="task text; '-' or nothing reads stdin")
    _add_session_args(run, settings)
    run.add_argument("--cwd", type=Path, default=None, help="workspace directory")
    run.add_argument("--json", action="store_true", help="JSON-lines event stream on stdout")
    run.add_argument("--stream", action="store_true", help="include text/reasoning deltas")
    run.add_argument("--resume", default=None, help="session id or 'last'")
    run.add_argument(
        "--approve-dangerous",
        choices=["ask", "deny", "allow"],
        default=None,
        help="destructive commands: ask (TTY default), deny (non-interactive default), allow",
    )

    chat = sub.add_parser("chat", help="interactive chat in the terminal (default on a TTY)")
    _add_session_args(chat, settings)
    chat.add_argument("--cwd", type=Path, default=None, help="workspace directory")
    chat.add_argument("--resume", default=None, help="session id or 'last'")
    chat.add_argument(
        "-c",
        "--continue",
        dest="resume",
        action="store_const",
        const="last",
        help="continue this project's last session (same as --resume last)",
    )
    chat.add_argument(
        "--fork",
        default=None,
        metavar="ID|last",
        help="start a new session as a copy of a saved one (the original stays as it is)",
    )
    chat.add_argument(
        "--no-undo",
        action="store_true",
        help="do not record the workspace around each turn (/undo then restores no files)",
    )
    chat.add_argument(
        "--approve-dangerous",
        choices=["ask", "deny", "allow"],
        default="ask",
        help="destructive commands: ask (default), deny, allow",
    )
    chat.add_argument("--no-color", action="store_true", help="plain output without ANSI colors")
    chat.add_argument(
        "--theme",
        choices=["auto", "mid", "dark", "light", "ansi"],
        default=argparse.SUPPRESS,  # not None: that would shadow config.toml's `theme`
        help="colours: auto (default), dark, light, mid (any background), ansi (16 colours)",
    )
    chat.add_argument(
        "--inline",
        action="store_true",
        help="run in inline scrollback mode instead of the full-screen TUI",
    )

    mcp = sub.add_parser(
        "mcp", help="serve KaroX to MCP clients (stdio, or Streamable HTTP with --http)"
    )
    _add_session_args(mcp, settings)
    mcp.add_argument(
        "--http",
        default=None,
        metavar="[HOST:]PORT",
        help="serve Streamable HTTP instead of stdio (host defaults to 127.0.0.1)",
    )
    mcp.add_argument(
        "--direct",
        action="store_true",
        help="serve KaroX's file and shell tools to the client's own model (no --model, no "
        "second model; read, write, edit, grep, glob, the shell and background jobs)",
    )
    mcp.add_argument(
        "--no-screenshot",
        action="store_true",
        help="with --direct: leave out the screenshot tool (the client cannot see the screen)",
    )
    mcp.add_argument(
        "--root",
        type=Path,
        default=None,
        help="project root: runs (or --direct tools) work here or below (default: current "
        "directory)",
    )
    mcp.add_argument("--any-cwd", action="store_true", help="let clients run in any directory")
    mcp.add_argument(
        "--token-env",
        default="KAROX_MCP_TOKEN",
        help="environment variable with the HTTP access token (if unset: a random token "
        "kept in <home>/mcp/http-token, so the connector URL survives restarts)",
    )
    mcp.add_argument(
        "--new-token",
        action="store_true",
        help="replace the kept HTTP access token (the old connector URL stops working)",
    )
    mcp.add_argument(
        "--allow-origin",
        action="append",
        default=[],
        help="extra browser origin allowed to call the HTTP endpoint",
    )
    mcp.add_argument(
        "--approve-dangerous",
        choices=["ask", "deny", "allow"],
        default="ask",
        help="destructive commands: ask the client (karox_approve; with --direct the call is "
        "refused until repeated with confirmed: true), deny, or allow",
    )
    mcp.add_argument("--approval-timeout", type=float, default=600.0, help="seconds, then deny")
    mcp.add_argument(
        "--no-redact",
        action="store_true",
        help="send tool output as it is (by default likely secrets are masked: keys, tokens, "
        "passwords, values of secret environment variables)",
    )
    mcp.add_argument(
        "--tunnel",
        choices=("tailscale", "tailnet"),
        default=None,
        help="with --http: an HTTPS address through Tailscale; 'tailscale' is public (Funnel: "
        "ChatGPT, Claude and other hosted connectors), 'tailnet' only your devices (Serve)",
    )
    mcp.add_argument(
        "--supervise",
        action="store_true",
        help="with --http: run the server as a child process and start it again when it "
        "exits unexpectedly (waits 1 s, doubling up to 60 s); Ctrl+C stops both",
    )

    update = sub.add_parser(
        "update", help="install the newest KaroX from its git branch into this environment"
    )
    update.add_argument(
        "--check", action="store_true", help="only say whether a newer version is there"
    )
    update.add_argument(
        "--force", action="store_true", help="build and install even when already up to date"
    )
    update.add_argument("--repo", default=None, help="git repository to update from")
    update.add_argument("--branch", default=None, help="branch of that repository")

    debug = sub.add_parser("debug", help="diagnostics for developers")
    debug_sub = debug.add_subparsers(dest="debug_command", required=True)
    debug_sub.add_parser("rss", help="load the headless stack and print resident memory (MiB)")
    return parser


def _add_session_args(p: argparse.ArgumentParser, settings: dict[str, Any] | None) -> None:
    """Model, provider and session options shared by `run`, `chat` and `mcp`."""
    p.add_argument(
        "--profile",
        default=None,
        help="use [profiles.NAME] of the config file (<KAROX_HOME>/config.toml)",
    )
    p.add_argument("-m", "--model", default=os.environ.get("KAROX_MODEL"))
    p.add_argument(
        "--provider",
        default=os.environ.get("KAROX_PROVIDER"),
        help="openai-chat | openai-responses | anthropic | gemini (default: anthropic for "
        "claude-* models on Anthropic's API, else openai-chat)",
    )
    p.add_argument("--base-url", default=os.environ.get("KAROX_BASE_URL"))
    p.add_argument(
        "--api-key-env",
        default="KAROX_API_KEY",
        help="name of the environment variable holding the API key (never pass keys as args)",
    )
    p.add_argument("--home", type=Path, default=None, help="KaroX state dir (sessions)")
    p.add_argument(
        "--max-steps",
        type=int,
        default=None,
        help="model calls per turn (default: no limit)",
    )
    p.add_argument(
        "--wrap-up-steps",
        type=int,
        default=None,
        help="ask the model to wrap up after this many steps, and for its final answer 30 "
        "steps later (default: never)",
    )
    p.add_argument("--max-seconds", type=float, default=None)
    p.add_argument(
        "--deadline-notice",
        type=int,
        default=None,
        help="with --max-seconds: tell the model this many seconds before the limit to "
        "make sure its deliverables exist (default: never)",
    )
    p.add_argument("--max-cost", type=float, default=None, help="USD, needs --price-*")
    p.add_argument(
        "--context-window",
        type=int,
        default=None,
        help="tokens the served model accepts (default: unknown, learned from a context error)",
    )
    p.add_argument(
        "--context-limit",
        type=int,
        default=400_000,
        help="working context budget in tokens; history is compacted near the smaller of this "
        "and --context-window (default: 400000)",
    )
    p.add_argument("--max-output-tokens", type=int, default=None)
    p.add_argument("--compact-at", type=int, default=None, help="tokens")
    p.add_argument("--reasoning-effort", default=None)
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--price-input", type=float, default=None, help="USD per 1M tokens")
    p.add_argument("--price-output", type=float, default=None)
    p.add_argument("--price-cached", type=float, default=None)
    p.add_argument("--apply-patch", action="store_true", help="offer the apply_patch tool")
    p.add_argument(
        "--no-snapshot",
        action="store_true",
        help="do not copy changed files to <session>/submissions at the end of the run",
    )
    p.add_argument("--replay-reasoning", choices=["turn", "all", "none"], default="turn")
    p.add_argument("--system-prompt-file", type=Path, default=None)
    p.add_argument(
        "--append-system-prompt",
        default=None,
        help="text added after the built-in system prompt",
    )
    p.add_argument("--extra-body", default=None, help="JSON object merged into requests")
    p.add_argument("--no-sse", action="store_true", help="disable streaming HTTP responses")
    p.add_argument("--request-timeout", type=float, default=900.0)
    p.add_argument(
        "--mcp-config",
        type=Path,
        default=None,
        help="MCP servers to use as tools (JSON with 'mcpServers'; default <home>/mcp.json)",
    )
    p.add_argument("--no-mcp", action="store_true", help="do not load MCP servers")
    p.add_argument(
        "--subagents",
        action="store_true",
        help="offer the agent tool: delegate investigations to child sessions (experimental)",
    )
    p.add_argument(
        "--review",
        action="store_true",
        help="before finishing, an independent reviewer checks the change against the task "
        "and the agent fixes what it reproduced (experimental)",
    )
    p.add_argument(
        "--code-mode",
        action="store_true",
        help="offer run_code: one Python program makes several tool calls (experimental)",
    )
    p.add_argument(
        "--copy-notes",
        action="store_true",
        help="after an edit, list other places that still hold the replaced code (experimental)",
    )
    p.add_argument(
        "--command-wait-cap",
        type=int,
        default=None,
        metavar="SECONDS",
        help="wait at most this long for a foreground command; it then keeps running as a "
        "background job (experimental; default: the tool's own maximum)",
    )
    p.add_argument(
        "--finish-gate",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="check changes once before finishing in a git repo (default: off)",
    )
    p.add_argument(
        "--progress-guard",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="inject a wrap-up notice after prolonged steps without progress (default: on)",
    )
    if settings:
        p.set_defaults(**settings)


def safe_streams() -> None:
    """Piped output is UTF-8 (JSON Lines are UTF-8 by definition; on Windows a redirected
    stream would otherwise use the ANSI code page, e.g. cp1251), and no stream ever raises
    on a character its encoding lacks: one emoji from the model used to end a run."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            if stream.isatty():
                reconfigure(errors="replace")
            else:
                reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass


def read_piped_text() -> str:
    """A task piped on stdin: UTF-8 (BOM allowed) as other tools write it, else the
    locale's code page. sys.stdin alone would decode UTF-8 as cp1251 on Windows."""
    buffer = getattr(sys.stdin, "buffer", None)
    if buffer is None:
        return sys.stdin.read()
    data: bytes = buffer.read()
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        import locale

        return data.decode(locale.getpreferredencoding(False), errors="replace")


def main(argv: Sequence[str] | None = None) -> int:
    from karox6.settings import SettingsError, load_settings, requested_profile

    safe_streams()
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        settings = load_settings(requested_profile(argv))
    except SettingsError as exc:
        sys.stderr.write(f"karox: {exc}\n")
        return 2
    parser = _parser(settings)
    if not argv and sys.stdin.isatty() and sys.stdout.isatty():
        argv = ["chat"]
    args = parser.parse_args(argv)
    if hasattr(args, "provider") and not args.provider:
        args.provider = default_provider(args.model, args.base_url)
    if args.command == "run":
        return _run(args, parser)
    if args.command == "chat":
        return _chat(args, parser)
    if args.command == "mcp":
        return _mcp(args, parser, argv)
    if args.command == "update":
        return _update(args, settings)
    if args.command == "debug" and args.debug_command == "rss":
        return _debug_rss()
    parser.print_help()
    return 0


def _update(args: argparse.Namespace, settings: dict[str, Any]) -> int:
    from karox6 import update
    from karox6.tui_text import _plain_text

    source = update.Source.from_settings(settings, args.repo, args.branch)

    def out(line: str) -> None:
        # progress/subject lines carry bytes the remote repo wrote — never raw to a tty
        sys.stdout.write(_plain_text(line) + "\n")
        sys.stdout.flush()

    try:
        result = update.run_update(source, out, force=args.force, check_only=args.check)
    except update.UpdateError as exc:
        sys.stderr.write(f"karox: update failed: {_plain_text(str(exc))}\n")
        return 1
    for line in update.describe(result):
        out(line)
    if result.status == "available" and args.check:
        out("run `karox6 update` to install it")
    return 0


def _debug_rss() -> int:
    """Import everything a headless run imports, then report RSS."""
    import karox6.stack  # noqa: F401  (side effect: imports the headless stack)
    from karox6.sysinfo import rss_mb

    sys.stdout.write(f"{rss_mb():.1f}\n")
    return 0


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:

    task = " ".join(args.task).strip()
    if task in ("", "-"):
        if sys.stdin.isatty() and task == "":
            parser.error("give a task, or pipe it on stdin")
        task = read_piped_text().strip()
    if not task:
        parser.error("empty task")
    if not args.model:
        parser.error("--model is required (or set KAROX_MODEL)")
    try:
        return run_async(_run_async(args, task))
    except KeyboardInterrupt:
        return EXIT_CODES["cancelled"]
    except (ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"karox: {exc}\n")
        return 2


async def _run_async(args: argparse.Namespace, task: str) -> int:
    from karox6.core.session import Session
    from karox6.core.store import default_home, resume_kwargs

    home = (args.home or default_home()).expanduser().resolve()
    workspace = (args.cwd or Path.cwd()).expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"workspace {workspace} is not a directory")
    config = _session_config(args, workspace, home, stream_deltas=args.stream or not args.json)
    # The key leaves the environment before any MCP server starts: they inherit it.
    provider = _provider(args)
    mcp_handles: list[Any] = []
    status = "error"
    try:
        mcp_tools, mcp_handles = await _mcp_tools(args, home)
        approver = _approver(args.approve_dangerous, json_mode=args.json)
        session_kwargs = (
            resume_kwargs(home, args.resume, workspace=workspace) if args.resume else {}
        )
        session = Session(
            config, provider, approver=approver, extra_tools=mcp_tools, **session_kwargs
        )
        render = _JsonRenderer() if args.json else _TextRenderer()
        async for event in session.run(task):
            render(event)
            if event.type == "run_finished":
                status = getattr(event, "status", "error")
    finally:
        await provider.aclose()
        await _close_mcp(mcp_handles)
    return EXIT_CODES.get(status, 1)


def _chat(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    # No model yet: the chat starts with its setup (provider, model, key) instead.
    if not args.model and not (sys.stdin.isatty() and sys.stdout.isatty()):
        parser.error("--model is required (or set KAROX_MODEL, or run `karox6` in a terminal)")
    interrupts = _Interrupts()  # before the frame: Ctrl+C must not raise inside the imports
    try:
        splash = _first_frame(args)  # on screen before asyncio and prompt_toolkit are imported
        try:
            return run_async(_chat_async(args, splash), interrupts)
        except KeyboardInterrupt:
            return 0
        except (ValueError, FileNotFoundError) as exc:
            if splash is not None:
                splash.close()
            sys.stderr.write(f"karox: {exc}\n")
            return 2
        finally:
            if splash is not None:
                splash.close()
    finally:
        interrupts.restore()


def _initial_window(args: argparse.Namespace) -> int:
    """The context budget shown before the session exists (as SessionConfig derives it)."""
    known = [v for v in (args.context_window, args.context_limit) if v]
    return min(known) if known else 400_000


def _first_frame(args: argparse.Namespace) -> Any:
    """Paint the full-screen chat's first frame right away (see karox6.splash); None when
    the chat will not start with that frame."""
    if not args.model or args.no_color or getattr(args, "inline", False):
        return None
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    workspace = (args.cwd or Path.cwd()).expanduser().resolve()
    if not workspace.is_dir():
        return None
    from karox6 import __version__, splash

    theme, legacy = _look(args, True)
    return splash.show(
        version=__version__,
        model=args.model,
        provider=args.provider,
        workspace=workspace,
        approval_mode=args.approve_dangerous,
        context_window=_initial_window(args),
        theme=theme,
        legacy_glyphs=legacy,
    )


def _look(args: argparse.Namespace, color: bool) -> tuple[Any, bool]:
    """The chat's theme (karox6.tui_theme) for this terminal, and whether its glyphs must
    stay within what a classic Windows console font draws."""
    from karox6.tui_text import legacy_console
    from karox6.tui_theme import Theme, color_depth, theme_name

    depth = color_depth() if color else 0
    return Theme(theme_name(getattr(args, "theme", None)), depth), legacy_console()


async def _chat_async(args: argparse.Namespace, splash: Any = None) -> int:
    from karox6.chat_features import McpState
    from karox6.core.config import SessionConfig
    from karox6.settings import default_home
    from karox6.tui import (
        ChatApp,
        color_enabled,
        secret_reader,
        stdout_write,
        terminal_display,
        terminal_io,
    )
    from karox6.tui_text import _plain_text

    home = (args.home or default_home()).expanduser().resolve()
    workspace = (args.cwd or Path.cwd()).expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"workspace {workspace} is not a directory")

    is_tty = sys.stdin.isatty() and sys.stdout.isatty()
    use_fullscreen = is_tty and not getattr(args, "inline", False)
    color = not args.no_color and color_enabled()
    encoding = getattr(sys.stdout, "encoding", None)
    theme, legacy = _look(args, color)
    ui: Any = None
    if use_fullscreen:
        try:
            from karox6.tui_app import FullScreenTui
        except ImportError:
            use_fullscreen = False
            sys.stderr.write(
                "KaroX: prompt_toolkit is missing, so the chat runs in inline plain mode. "
                "Reinstall KaroX with its dependencies for the full-screen TUI.\n"
            )
        else:
            ui = FullScreenTui(
                workspace=workspace,
                home=home,
                history=home / "chat_history",
                color=color,
                encoding=encoding,
                model=args.model or "",
                provider=args.provider,
                approval_mode=args.approve_dangerous,
                splash=splash,
                theme=theme,
                legacy_glyphs=legacy,
            )

    if ui is None:
        if splash is not None:
            splash.close()
        read_line, read_answer = terminal_io(home / "chat_history", workspace=workspace, home=home)

        async def read_secret(prompt: str) -> str:
            return await secret_reader()(prompt)

        write_fn = stdout_write
        display_factory = terminal_display
    else:
        read_line = ui.read_line
        read_answer = ui.read_answer
        read_secret = ui.read_secret
        write_fn = ui.write
        display_factory = ui.turn_display

    chose_model = not args.model
    mcp_handles: list[Any] | None = None

    async def initialize(app: ChatApp) -> dict[str, Any]:
        from karox6.core.store import resume_kwargs

        nonlocal mcp_handles
        app.config = _session_config(args, workspace, home, stream_deltas=True)
        session_kwargs = (
            resume_kwargs(home, args.resume, workspace=workspace) if args.resume else {}
        )
        if args.fork and not args.resume:
            # A new session that starts with the saved one's conversation.
            app._fork_source = resume_kwargs(home, args.fork, workspace=workspace)
        if app.provider is None:
            # Drop the key from the environment before starting any MCP server.
            app.provider = _provider(args)
            app.bridge_agent_args = ["--provider", args.provider, "--model", args.model]
            if args.base_url:
                app.bridge_agent_args += ["--base-url", args.base_url]
            key = getattr(app.provider, "api_key", None)
            if key:
                app.bridge_agent_args += ["--api-key-env", "KAROX_BRIDGE_API_KEY"]
                app.bridge_env = {"KAROX_BRIDGE_API_KEY": key}
        if mcp_handles is None:
            failures: dict[str, str] = {}

            def warn(message: str) -> None:
                server = getattr(message, "server", None)  # mcp.client.ServerSkipped
                if server is not None:
                    failures[server] = str(getattr(message, "reason", ""))
                app.write(_plain_text(message))

            app.extra_tools, mcp_handles = await _mcp_tools(args, home, warn=warn)
            app.mcp = McpState(
                path=None if args.no_mcp else (args.mcp_config or home / "mcp.json"),
                handles=mcp_handles,
                failures=failures,
            )
        return session_kwargs

    async def check_key(app: ChatApp) -> None:
        if (
            chose_model
            or getattr(app.provider, "api_key", None)
            or _local_url(getattr(app.provider, "base_url", args.base_url))
            or not (sys.stdin.isatty() and sys.stdout.isatty())
        ):
            return
        from karox6.credentials import save_key

        app.write("No API key for this model yet.\n")
        try:
            typed = (await app.read_secret("API key (hidden; Enter to skip): ")).strip()
        except EOFError:
            import asyncio

            raise asyncio.CancelledError from None
        if typed:
            save_key(home, typed, getattr(args, "saved_key", None) or "")
            app.provider.api_key = typed
            app.bridge_agent_args += ["--api-key-env", "KAROX_BRIDGE_API_KEY"]
            app.bridge_env = {"KAROX_BRIDGE_API_KEY": typed}
            app.write("key saved (encrypted for your user); /key changes it\n")

    def make_app() -> ChatApp:
        import shutil

        created = ChatApp(
            config=SessionConfig(
                model=args.model,
                provider_name=args.provider,
                workspace=workspace,
                home=home,
                context_window=args.context_window,
                context_limit=args.context_limit,
            ),
            provider=None,
            read_line=read_line,
            read_answer=read_answer,
            write=write_fn,
            approval_mode=args.approve_dangerous,
            color=color,
            encoding=encoding,
            read_secret=read_secret,
            initialize=initialize,
            before_turn=check_key,
            display_factory=display_factory,
            columns=ui.columns if ui is not None else lambda: shutil.get_terminal_size().columns,
            snapshots=not args.no_undo,
            prefill=ui.prefill if ui is not None else None,
            theme=theme,
            legacy_glyphs=legacy,
            write_block=ui.write_block if ui is not None else None,
            live=ui.set_live if ui is not None else None,
            copy_to_clipboard=ui.copy_to_clipboard if ui is not None else None,
        )
        if ui is not None:
            ui.attach_app(created)
        created.replay_on_start = bool(args.resume)
        created.key_name = getattr(args, "saved_key", None) or ""
        return created

    app: ChatApp | None = None
    summary: str | None = None
    banner = True
    if ui is not None and not chose_model:
        # The welcome card is part of the very first frame (it used to pop in a frame
        # later), which is also what the start-up frame has already painted.
        app = make_app()
        ui.write(app._banner())
        banner = False
    try:
        async with ui if ui is not None else contextlib.nullcontext():
            if chose_model:
                from karox6.setup import run_setup

                try:
                    choice = await run_setup(read_answer, read_secret, write_fn, home)
                except (EOFError, KeyboardInterrupt):
                    return 2
                if choice is None:
                    return 2
                args.provider, args.base_url, args.model = (
                    choice.provider,
                    choice.base_url,
                    choice.model,
                )
                args.saved_key = choice.name
            if app is None:
                app = make_app()
            _start_update_check(args, home, app)
            try:
                return await app.run(banner=banner)
            finally:
                if ui is not None:
                    summary = app.exit_summary()
                with contextlib.suppress(Exception):
                    await app.close()
    finally:
        try:
            if app is not None and app.provider is not None:
                await app.provider.aclose()
        finally:
            await _close_mcp(mcp_handles or [])
        if splash is not None:
            splash.close()  # the app never drew: back to the user's screen first
        if summary:
            stdout_write(summary + "\n")


def _start_update_check(args: argparse.Namespace, home: Path, app: Any) -> None:
    """Look for a newer KaroX in the background (at most once a day; see karox6.update):
    a daemon thread, so neither the first frame nor the exit waits for the network. Off
    with ``update_check = false`` or KAROX_UPDATE_CHECK=0, and when KaroX runs from a
    source tree; KAROX_UPDATE_CHECK=1 forces it on either way."""
    import asyncio
    import threading

    from karox6 import update

    # /update installs from this source even when the background check below is off.
    settings = {k: getattr(args, k, None) for k in ("update_repo", "update_branch")}
    source = update.Source.from_settings(settings)
    app.update_source = source

    wanted = os.environ.get("KAROX_UPDATE_CHECK", "")
    if wanted == "0" or (
        wanted != "1" and (getattr(args, "update_check", True) is False or update.from_checkout())
    ):
        return
    loop = asyncio.get_running_loop()

    def check() -> None:
        with contextlib.suppress(Exception):
            found = update.check_cached(home, source)
            if found:
                loop.call_soon_threadsafe(app.notice_update, found)

    threading.Thread(target=check, name="karox-update-check", daemon=True).start()


def _session_config(
    args: argparse.Namespace, workspace: Path, home: Path, *, stream_deltas: bool
) -> Any:
    from karox6.core.config import SessionConfig
    from karox6.core.messages import Price

    price = None
    if args.price_input is not None and args.price_output is not None:
        price = Price(args.price_input, args.price_output, args.price_cached)
    elif _positive(args.max_cost) is not None:
        raise ValueError("--max-cost needs --price-input and --price-output")
    extra_body: dict[str, Any] = {}
    if args.extra_body:
        loaded = json.loads(args.extra_body)
        if not isinstance(loaded, dict):
            raise ValueError("--extra-body must be a JSON object")
        extra_body = loaded
    system_prompt = None
    if args.system_prompt_file:
        system_prompt = args.system_prompt_file.read_text(encoding="utf-8")
    return SessionConfig(
        model=args.model,
        provider_name=args.provider,
        workspace=workspace,
        home=home,
        max_steps=_positive(args.max_steps),  # 0 or less: no limit
        max_seconds=_positive(args.max_seconds),  # 0: no limit, as for --max-steps
        max_cost_usd=_positive(args.max_cost),
        max_output_tokens=args.max_output_tokens,
        context_window=args.context_window,
        context_limit=args.context_limit,
        compact_at_tokens=args.compact_at,
        temperature=args.temperature,
        reasoning_effort=args.reasoning_effort,
        price=price,
        apply_patch=args.apply_patch,
        replay_reasoning=args.replay_reasoning,
        snapshot_on_finish=not args.no_snapshot,
        stream_deltas=stream_deltas,
        extra_body=extra_body,
        system_prompt=system_prompt,
        system_append=args.append_system_prompt,
        subagents=args.subagents,
        review=args.review,
        finish_gate=args.finish_gate,
        progress_guard=args.progress_guard,
        code_mode=args.code_mode,
        copy_notes=args.copy_notes,
        command_wait_cap_s=_positive(args.command_wait_cap),
        wrap_up_steps=_positive(args.wrap_up_steps),
        deadline_notice_s=_positive(args.deadline_notice),
    )


def _local_url(url: str | None) -> bool:
    """A model server on this machine (Ollama, LM Studio, ...) usually needs no key."""
    host = urlsplit(url or "").hostname or ""
    return host in ("localhost", "127.0.0.1", "::1")


def _positive[N: (int, float)](value: N | None) -> N | None:
    return value if value is not None and value > 0 else None


def default_provider(model: str | None, base_url: str | None) -> str:
    """The native Messages API for Claude models on Anthropic's own endpoint: it keeps signed
    thinking blocks across turns (the OpenAI-compatible path drops them, which measured ~1.7x
    the output tokens per call). Everything else defaults to the OpenAI-compatible protocol."""
    from urllib.parse import urlsplit

    url = urlsplit(base_url or "")
    native = not base_url or (url.hostname == "api.anthropic.com" and url.path.rstrip("/") == "")
    if native and (model or "").lower().startswith("claude"):
        return "anthropic"
    return "openai-chat"


def _provider(args: argparse.Namespace) -> Any:
    from karox6.providers import make_provider

    # Read the key once, then drop it: the agent's shell commands (and their output in
    # transcripts) inherit this process's environment.
    api_key = os.environ.pop(args.api_key_env, None) if args.api_key_env else None
    if not api_key:  # the key the chat's setup saved (`/key`), if any
        from karox6.core.store import default_home
        from karox6.credentials import load_key

        home = (args.home or default_home()).expanduser()
        api_key = load_key(home, getattr(args, "saved_key", None) or "")
    return make_provider(
        args.provider or default_provider(args.model, args.base_url),
        base_url=args.base_url,
        api_key=api_key,
        timeout_s=args.request_timeout,
        stream=not args.no_sse,
    )


def _mcp(args: argparse.Namespace, parser: argparse.ArgumentParser, argv: list[str]) -> int:
    from karox6.mcp.tunnel import TunnelError

    if not args.model and not args.direct:
        parser.error("--model is required (or set KAROX_MODEL), unless --direct")
    if args.supervise:
        if args.http is None:
            parser.error("--supervise needs --http PORT")
        try:
            return _supervise(argv)
        except OSError as exc:
            sys.stderr.write(f"karox mcp: {exc}\n")
            return 2
    try:
        run_async(_mcp_async(args))
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError, TunnelError) as exc:
        sys.stderr.write(f"karox mcp: {exc}\n")
        return 2
    return 0


def _supervise(argv: list[str]) -> int:
    """Run `karox mcp` with these arguments as a child and restart it when it dies. The
    environment passes through unchanged: the child reads (and drops) the token itself."""
    from karox6.mcp.supervise import Supervisor, without_flag

    first = [sys.executable, "-m", "karox6", *without_flag(argv, "--supervise")]
    # A restart keeps the token the first start made instead of replacing it again.
    again = without_flag(first, "--new-token")
    return Supervisor(first, restart_command=again).run()


async def _mcp_async(args: argparse.Namespace) -> None:
    import asyncio

    from karox6.core.store import default_home
    from karox6.mcp.direct import DirectTools
    from karox6.mcp.oauth import OAuth
    from karox6.mcp.protocol import McpServer
    from karox6.mcp.redact import Redactor
    from karox6.mcp.runs import RunManager
    from karox6.mcp.transport import DEFAULT_ORIGINS, HttpMcp, serve_stdio
    from karox6.mcp.tunnel import Tunnel

    home = (args.home or default_home()).expanduser().resolve()
    root = (args.root or Path.cwd()).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"root {root} is not a directory")
    if args.tunnel and args.http is None:
        raise ValueError("--tunnel needs --http PORT (the tunnel forwards to it)")
    # Taken now: the key and token variables leave the environment below, but their
    # values can still turn up in files and command output.
    redactor = None if args.no_redact else Redactor.from_env()
    if redactor is not None and args.api_key_env and os.environ.get(args.api_key_env):
        redactor.add(os.environ[args.api_key_env], f"${args.api_key_env}")
    tunnel: Tunnel | None = None
    keeper: asyncio.Task[None] | None = None
    provider: Any = None
    mcp_handles: list[Any] = []
    backend: RunManager | DirectTools
    if args.direct:
        # No model to call, but the key still leaves the environment: shell output goes
        # back to the client.
        if args.api_key_env:
            os.environ.pop(args.api_key_env, None)
        backend = DirectTools(
            root,
            home=home,
            any_path=args.any_cwd,
            approval_mode=args.approve_dangerous,
            screenshot=not args.no_screenshot,
        )
        server = McpServer(direct=backend, redactor=redactor)
    else:
        provider = _provider(args)
        mcp_tools, mcp_handles = await _mcp_tools(args, home)
        backend = RunManager(
            lambda ws: _session_config(args, ws, home, stream_deltas=False),
            lambda: provider,
            root=root,
            any_workspace=args.any_cwd,
            approval_mode=args.approve_dangerous,
            approval_timeout_s=args.approval_timeout,
            extra_tools=mcp_tools,
        )
        server = McpServer(backend, redactor=redactor)
    try:
        if args.http is None:
            await serve_stdio(server)
            return
        host, _, port = args.http.rpartition(":")
        host = host or "127.0.0.1"
        token, kept = _http_token(args, home)
        if redactor is not None:
            redactor.add(token, "KaroX access token")
        origins = [*DEFAULT_ORIGINS, *args.allow_origin]

        def note(line: str) -> None:
            sys.stderr.write(f"karox mcp: {line}\n")
            sys.stderr.flush()

        # OAuth sign-in for clients that require it (ChatGPT's connector form defaults to
        # it); the token-in-URL access keeps working beside it.
        oauth = OAuth(home / "mcp" / "oauth.json", on_event=lambda s: note(f"sign-in: {s}"))
        http = HttpMcp(server, token=token, allowed_origins=origins, oauth=oauth, access_log=note)
        listener = await http.start(host, int(port))
        bound = listener.sockets[0].getsockname()[1]
        shown = host if host not in ("0.0.0.0", "::") else "127.0.0.1"
        # A token the user chose is not echoed (stderr often ends up in logs).
        path_token = token if kept else f"<${args.token_env}>"
        async with listener:
            if args.tunnel:
                tunnel = await _open_tunnel(bound, host, public=args.tunnel == "tailscale")
                keeper = asyncio.ensure_future(_keep_tunnel(tunnel))
                # published under a path (port 443 shared with another site): links the
                # server hands out must carry it
                http.mount = oauth.prefix = urlsplit(tunnel.url).path.rstrip("/")
            where = "public internet" if tunnel and tunnel.public else "your tailnet"
            mode = "  direct tools" if args.direct else ""
            sys.stderr.write(
                f"KaroX MCP (Streamable HTTP) on http://{shown}:{bound}/mcp  root={root}{mode}\n"
                + (
                    f"  Connector URL ({where}, Tailscale): {tunnel.url}/mcp/{path_token}\n"
                    f"  OAuth URL: {tunnel.url}/mcp\n"
                    f"  Approval code: {oauth.pin}\n"
                    "  Paste the connector URL into ChatGPT or Claude with no authentication,\n"
                    "  or the OAuth URL with OAuth and type the approval code when asked.\n"
                    if tunnel
                    else "  URL for clients that only take a URL (ChatGPT connector, behind an\n"
                    f"  HTTPS tunnel): http://{shown}:{bound}/mcp/{path_token}\n"
                )
                + "  or send 'Authorization: Bearer <token>' to /mcp. Keep the token secret.\n"
                + (
                    f"  The token is kept in {kept} and stays the same across restarts;\n"
                    "  --new-token replaces it (the old URL then stops working).\n"
                    if kept
                    else ""
                )
            )
            sys.stderr.flush()
            await asyncio.Event().wait()
    finally:
        if keeper is not None:
            keeper.cancel()
            await asyncio.wait([keeper])
        if tunnel is not None:  # waits for a keep still running in its thread
            await asyncio.to_thread(tunnel.stop)
        await backend.shutdown()
        if provider is not None:
            await provider.aclose()
        await _close_mcp(mcp_handles)


async def _open_tunnel(port: int, host: str, *, public: bool) -> Any:
    """open_tailscale in a thread; a Ctrl+C meanwhile waits for it and removes the route
    it may still have created, so no public route outlives KaroX."""
    import asyncio

    from karox6.mcp.tunnel import open_tailscale

    opening = asyncio.ensure_future(
        asyncio.to_thread(open_tailscale, port, public=public, local_host=host)
    )
    try:
        return await asyncio.shield(opening)
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            late = await asyncio.wait_for(asyncio.shield(opening), 90)
            await asyncio.to_thread(late.stop)
        raise


async def _keep_tunnel(tunnel: Any, interval: float = 30.0) -> None:
    """Put the tunnel route back whenever it vanished (checked every ``interval`` s). A
    message is printed once, not every round, while the same problem persists."""
    import asyncio

    last: str | None = None
    while True:
        await asyncio.sleep(interval)
        line = await asyncio.to_thread(tunnel.keep)
        if line and line != last:
            sys.stderr.write(f"karox mcp: {line}\n")
            sys.stderr.flush()
        last = line


def _http_token(args: argparse.Namespace, home: Path) -> tuple[str, Path | None]:
    """The HTTP access token, and the file it is kept in (None: it came from the
    environment and is neither saved nor printed)."""
    from karox6.mcp.transport import saved_token

    # Taken out of the environment, so the agent's commands cannot read it.
    given = os.environ.pop(args.token_env, None) if args.token_env else None
    if given:
        return given, None
    path = home / "mcp" / "http-token"
    return saved_token(path, renew=args.new_token), path


async def _mcp_tools(
    args: argparse.Namespace, home: Path, *, warn: Callable[[str], object] | None = None
) -> tuple[list[Any], list[Any]]:
    """Tools of the configured external MCP servers (none unless a config exists)."""
    if args.no_mcp:
        return [], []
    path = args.mcp_config or home / "mcp.json"
    if args.mcp_config is None and not path.is_file():
        return [], []
    from karox6.mcp.client import load_mcp_tools

    tools, handles = await load_mcp_tools(path, warn=warn)
    return list(tools), list(handles)


async def _close_mcp(handles: list[Any]) -> None:
    if handles:
        from karox6.mcp.client import close_all

        await close_all(handles)


def _approver(mode: str | None, *, json_mode: bool) -> Any:
    from karox6.tools.base import deny_all

    if mode is None:
        mode = "ask" if (sys.stdin.isatty() and not json_mode) else "deny"
    if mode == "deny":
        return deny_all
    if mode == "allow":

        async def allow(_request: ApprovalRequest) -> bool:
            return True

        return allow

    async def ask(request: ApprovalRequest) -> bool:
        import asyncio

        sys.stderr.write(
            f"\n[karox] {request.reason}\n  rule: {request.rule}\n  command: {request.summary}\n"
            "  Run it? [y/N] "
        )
        sys.stderr.flush()
        answer = await asyncio.to_thread(sys.stdin.readline)
        return answer.strip().lower() in ("y", "yes")

    return ask


class _JsonRenderer:
    def __call__(self, event: Event) -> None:
        sys.stdout.write(json.dumps(event.to_dict(), ensure_ascii=False, default=str) + "\n")
        sys.stdout.flush()


class _TextRenderer:
    """Plain headless output: assistant text on stdout, tool activity on stderr."""

    def __init__(self) -> None:
        self._open_text = False
        self._streamed = False

    def __call__(self, event: Event) -> None:
        data = event.to_dict()
        kind = event.type
        if kind == "text_delta":
            sys.stdout.write(data["text"])
            sys.stdout.flush()
            self._open_text = True
            self._streamed = True
            return
        if kind == "assistant_message":
            # Without SSE (--no-sse) no deltas arrive: the whole reply comes here.
            if not self._streamed and data["text"]:
                sys.stdout.write(data["text"].rstrip("\n") + "\n")
                sys.stdout.flush()
            self._streamed = False
        elif kind == "retry":
            # A failed partial stream is replayed in full by the retry.
            self._streamed = False
        if self._open_text and kind != "reasoning_delta":
            sys.stdout.write("\n")
            # Flushed before the stderr line below: with both streams in one pipe
            # (`2>&1 | ...`) the tool line was glued to the end of the reply.
            sys.stdout.flush()
            self._open_text = False
        if kind == "tool_started":
            sys.stderr.write(f"> {data['name']} {_brief(data['arguments'])}\n")
        elif kind == "tool_finished":
            mark = "x" if data["is_error"] else "ok"
            first = data["output"].strip().splitlines()[-1:] if data["output"].strip() else [""]
            sys.stderr.write(f"  [{mark} {data['duration_ms']} ms] {first[0][:160]}\n")
        elif kind in ("notice", "retry", "error", "compacted", "approval_requested"):
            sys.stderr.write(f"[{kind}] {json.dumps(data, ensure_ascii=False)[:300]}\n")
        elif kind == "run_finished":
            cost = data.get("cost_usd")
            cost_text = f", ${cost:.4f}" if isinstance(cost, float) else ""
            usage = data["usage"]
            sys.stderr.write(
                f"\n[{data['status']}] {data['reason'] or 'done'}: {data['steps']} steps, "
                f"{data['elapsed_s']}s, {usage['input_tokens']} in / {usage['output_tokens']} out"
                f"{cost_text}; session {data['session_id']}\n"
            )
        sys.stderr.flush()


def _brief(arguments: str, limit: int = 160) -> str:
    try:
        value = json.loads(arguments)
    except json.JSONDecodeError:
        value = arguments
    if isinstance(value, dict):
        for key in ("command", "file_path", "pattern", "job_id"):
            if key in value:
                text = str(value[key])
                break
        else:
            text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
