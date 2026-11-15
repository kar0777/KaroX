"""`karox` command line entry point.

Imports stay inside the functions: `karox --version` must not pay for the agent
stack (startup budget 300 ms, checked in CI by scripts/perf_check.py).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from karox6.core.events import Event
    from karox6.tools.base import ApprovalRequest

EXIT_CODES = {"completed": 0, "error": 1, "limit": 3, "loop": 4, "cancelled": 130}


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
        "--approve-dangerous",
        choices=["ask", "deny", "allow"],
        default="ask",
        help="destructive commands: ask (default), deny, allow",
    )
    chat.add_argument("--no-color", action="store_true", help="plain output without ANSI colors")

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
        "--root",
        type=Path,
        default=None,
        help="project root: runs work here or below (default: current directory)",
    )
    mcp.add_argument("--any-cwd", action="store_true", help="let clients run in any directory")
    mcp.add_argument(
        "--token-env",
        default="KAROX_MCP_TOKEN",
        help="environment variable with the HTTP access token (random if unset)",
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
        help="destructive commands: ask the client (karox_approve), deny, or allow",
    )
    mcp.add_argument("--approval-timeout", type=float, default=600.0, help="seconds, then deny")

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
        default=os.environ.get("KAROX_PROVIDER", "openai-chat"),
        help="openai-chat | openai-responses | anthropic | gemini",
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
    p.add_argument("--max-cost", type=float, default=None, help="USD, needs --price-*")
    p.add_argument("--context-window", type=int, default=128_000)
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
    if settings:
        p.set_defaults(**settings)


def main(argv: Sequence[str] | None = None) -> int:
    from karox6.settings import SettingsError, load_settings, requested_profile

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
    if args.command == "run":
        return _run(args, parser)
    if args.command == "chat":
        return _chat(args, parser)
    if args.command == "mcp":
        return _mcp(args, parser)
    if args.command == "debug" and args.debug_command == "rss":
        return _debug_rss()
    parser.print_help()
    return 0


def _debug_rss() -> int:
    """Import everything a headless run imports, then report RSS."""
    import karox6.stack  # noqa: F401  (side effect: imports the headless stack)
    from karox6.sysinfo import rss_mb

    sys.stdout.write(f"{rss_mb():.1f}\n")
    return 0


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    import asyncio

    task = " ".join(args.task).strip()
    if task in ("", "-"):
        if sys.stdin.isatty() and task == "":
            parser.error("give a task, or pipe it on stdin")
        task = sys.stdin.read().strip()
    if not task:
        parser.error("empty task")
    if not args.model:
        parser.error("--model is required (or set KAROX_MODEL)")
    try:
        return asyncio.run(_run_async(args, task))
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
    mcp_tools, mcp_handles = await _mcp_tools(args, home)
    provider = _provider(args)
    approver = _approver(args.approve_dangerous, json_mode=args.json)
    session_kwargs = resume_kwargs(home, args.resume) if args.resume else {}
    session = Session(config, provider, approver=approver, extra_tools=mcp_tools, **session_kwargs)
    render = _JsonRenderer() if args.json else _TextRenderer()
    status = "error"
    try:
        async for event in session.run(task):
            render(event)
            if event.type == "run_finished":
                status = getattr(event, "status", "error")
    finally:
        await provider.aclose()
        await _close_mcp(mcp_handles)
    return EXIT_CODES.get(status, 1)


def _chat(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    import asyncio

    if not args.model:
        parser.error("--model is required (or set KAROX_MODEL)")
    try:
        return asyncio.run(_chat_async(args))
    except KeyboardInterrupt:
        return 0
    except (ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"karox: {exc}\n")
        return 2


async def _chat_async(args: argparse.Namespace) -> int:
    from karox6.core.store import default_home, resume_kwargs
    from karox6.tui import ChatApp, color_enabled, stdout_write, terminal_io

    home = (args.home or default_home()).expanduser().resolve()
    workspace = (args.cwd or Path.cwd()).expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"workspace {workspace} is not a directory")
    config = _session_config(args, workspace, home, stream_deltas=True)
    session_kwargs = resume_kwargs(home, args.resume) if args.resume else {}
    read_line, read_answer = terminal_io(home / "chat_history")
    mcp_tools, mcp_handles = await _mcp_tools(args, home)
    provider = _provider(args)
    try:
        app = ChatApp(
            config=config,
            provider=provider,
            read_line=read_line,
            read_answer=read_answer,
            write=stdout_write,
            approval_mode=args.approve_dangerous,
            color=not args.no_color and color_enabled(),
            encoding=getattr(sys.stdout, "encoding", None),
            extra_tools=mcp_tools,
            session_kwargs=session_kwargs,
        )
        return await app.run()
    finally:
        await provider.aclose()
        await _close_mcp(mcp_handles)


def _session_config(
    args: argparse.Namespace, workspace: Path, home: Path, *, stream_deltas: bool
) -> Any:
    from karox6.core.messages import Price
    from karox6.core.session import SessionConfig

    price = None
    if args.price_input is not None and args.price_output is not None:
        price = Price(args.price_input, args.price_output, args.price_cached)
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
        max_steps=args.max_steps or None,  # 0: no limit
        max_seconds=args.max_seconds,
        max_cost_usd=args.max_cost,
        max_output_tokens=args.max_output_tokens,
        context_window=args.context_window,
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
        wrap_up_steps=args.wrap_up_steps or None,
    )


def _provider(args: argparse.Namespace) -> Any:
    from karox6.providers import make_provider

    # Read the key once, then drop it: the agent's shell commands (and their output in
    # transcripts) inherit this process's environment.
    api_key = os.environ.pop(args.api_key_env, None) if args.api_key_env else None
    return make_provider(
        args.provider,
        base_url=args.base_url,
        api_key=api_key,
        timeout_s=args.request_timeout,
        stream=not args.no_sse,
    )


def _mcp(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    import asyncio

    if not args.model:
        parser.error("--model is required (or set KAROX_MODEL)")
    try:
        asyncio.run(_mcp_async(args))
    except KeyboardInterrupt:
        pass
    except (ValueError, OSError) as exc:
        sys.stderr.write(f"karox mcp: {exc}\n")
        return 2
    return 0


async def _mcp_async(args: argparse.Namespace) -> None:
    import asyncio
    import secrets

    from karox6.core.store import default_home
    from karox6.mcp.protocol import McpServer
    from karox6.mcp.runs import RunManager
    from karox6.mcp.transport import DEFAULT_ORIGINS, HttpMcp, serve_stdio

    home = (args.home or default_home()).expanduser().resolve()
    root = (args.root or Path.cwd()).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"root {root} is not a directory")
    mcp_tools, mcp_handles = await _mcp_tools(args, home)
    provider = _provider(args)
    runs = RunManager(
        lambda ws: _session_config(args, ws, home, stream_deltas=False),
        lambda: provider,
        root=root,
        any_workspace=args.any_cwd,
        approval_mode=args.approve_dangerous,
        approval_timeout_s=args.approval_timeout,
        extra_tools=mcp_tools,
    )
    server = McpServer(runs)
    try:
        if args.http is None:
            await serve_stdio(server)
            return
        host, _, port = args.http.rpartition(":")
        host = host or "127.0.0.1"
        # Taken out of the environment, so the agent's commands cannot read it.
        given = os.environ.pop(args.token_env, None) if args.token_env else None
        token = given or secrets.token_urlsafe(24)
        origins = [*DEFAULT_ORIGINS, *args.allow_origin]
        http = HttpMcp(server, token=token, allowed_origins=origins)
        listener = await http.start(host, int(port))
        bound = listener.sockets[0].getsockname()[1]
        shown = host if host not in ("0.0.0.0", "::") else "127.0.0.1"
        # A token the user chose is not echoed (stderr often ends up in logs).
        path_token = f"<${args.token_env}>" if given else token
        sys.stderr.write(
            f"KaroX MCP (Streamable HTTP) on http://{shown}:{bound}/mcp  root={root}\n"
            "  URL for clients that only take a URL (ChatGPT connector, behind an HTTPS\n"
            f"  tunnel): http://{shown}:{bound}/mcp/{path_token}\n"
            "  or send 'Authorization: Bearer <token>' to /mcp. Keep the token secret.\n"
        )
        sys.stderr.flush()
        async with listener:
            await asyncio.Event().wait()
    finally:
        await runs.shutdown()
        await provider.aclose()
        await _close_mcp(mcp_handles)


async def _mcp_tools(args: argparse.Namespace, home: Path) -> tuple[list[Any], list[Any]]:
    """Tools of the configured external MCP servers (none unless a config exists)."""
    if args.no_mcp:
        return [], []
    path = args.mcp_config or home / "mcp.json"
    if args.mcp_config is None and not path.is_file():
        return [], []
    from karox6.mcp.client import load_mcp_tools

    tools, handles = await load_mcp_tools(path)
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

    def __call__(self, event: Event) -> None:
        data = event.to_dict()
        kind = event.type
        if kind == "text_delta":
            sys.stdout.write(data["text"])
            sys.stdout.flush()
            self._open_text = True
            return
        if self._open_text and kind != "reasoning_delta":
            sys.stdout.write("\n")
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
