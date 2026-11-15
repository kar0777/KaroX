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


def _parser() -> argparse.ArgumentParser:
    from karox6 import __version__

    parser = argparse.ArgumentParser(prog="karox", description="KaroX 6 coding agent")
    parser.add_argument("--version", action="version", version=f"karox {__version__}")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run one task headless (text or JSON-lines output)")
    run.add_argument("task", nargs="*", help="task text; '-' or nothing reads stdin")
    run.add_argument("-m", "--model", default=os.environ.get("KAROX_MODEL"))
    run.add_argument(
        "--provider",
        default=os.environ.get("KAROX_PROVIDER", "openai-chat"),
        help="openai-chat | openai-responses | anthropic | gemini",
    )
    run.add_argument("--base-url", default=os.environ.get("KAROX_BASE_URL"))
    run.add_argument(
        "--api-key-env",
        default="KAROX_API_KEY",
        help="name of the environment variable holding the API key (never pass keys as args)",
    )
    run.add_argument("--cwd", type=Path, default=None, help="workspace directory")
    run.add_argument("--home", type=Path, default=None, help="KaroX state dir (sessions)")
    run.add_argument("--json", action="store_true", help="JSON-lines event stream on stdout")
    run.add_argument("--stream", action="store_true", help="include text/reasoning deltas")
    run.add_argument("--resume", default=None, help="session id or 'last'")
    run.add_argument("--max-steps", type=int, default=None)
    run.add_argument("--max-seconds", type=float, default=None)
    run.add_argument("--max-cost", type=float, default=None, help="USD, needs --price-*")
    run.add_argument("--context-window", type=int, default=128_000)
    run.add_argument("--max-output-tokens", type=int, default=None)
    run.add_argument("--compact-at", type=int, default=None, help="tokens")
    run.add_argument("--reasoning-effort", default=None)
    run.add_argument("--temperature", type=float, default=None)
    run.add_argument("--price-input", type=float, default=None, help="USD per 1M tokens")
    run.add_argument("--price-output", type=float, default=None)
    run.add_argument("--price-cached", type=float, default=None)
    run.add_argument("--apply-patch", action="store_true", help="offer the apply_patch tool")
    run.add_argument(
        "--no-snapshot",
        action="store_true",
        help="do not copy changed files to <session>/submissions at the end of the run",
    )
    run.add_argument("--replay-reasoning", choices=["turn", "all", "none"], default="turn")
    run.add_argument(
        "--approve-dangerous",
        choices=["ask", "deny", "allow"],
        default=None,
        help="destructive commands: ask (TTY default), deny (non-interactive default), allow",
    )
    run.add_argument("--system-prompt-file", type=Path, default=None)
    run.add_argument("--extra-body", default=None, help="JSON object merged into requests")
    run.add_argument("--no-sse", action="store_true", help="disable streaming HTTP responses")
    run.add_argument("--request-timeout", type=float, default=900.0)

    debug = sub.add_parser("debug", help="diagnostics for developers")
    debug_sub = debug.add_subparsers(dest="debug_command", required=True)
    debug_sub.add_parser("rss", help="load the headless stack and print resident memory (MiB)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "run":
        return _run(args, parser)
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
    from karox6.core.messages import Price
    from karox6.core.session import Session, SessionConfig
    from karox6.core.store import SessionStore, default_home
    from karox6.providers import make_provider

    home = (args.home or default_home()).expanduser().resolve()
    workspace = (args.cwd or Path.cwd()).expanduser().resolve()
    if not workspace.is_dir():
        raise ValueError(f"workspace {workspace} is not a directory")
    api_key = os.environ.get(args.api_key_env) if args.api_key_env else None
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
    config = SessionConfig(
        model=args.model,
        provider_name=args.provider,
        workspace=workspace,
        home=home,
        max_steps=args.max_steps,
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
        stream_deltas=args.stream or not args.json,
        extra_body=extra_body,
        system_prompt=system_prompt,
    )
    provider = make_provider(
        args.provider,
        base_url=args.base_url,
        api_key=api_key,
        timeout_s=args.request_timeout,
        stream=not args.no_sse,
    )
    approver = _approver(args.approve_dangerous, json_mode=args.json)
    session_kwargs: dict[str, Any] = {}
    if args.resume:
        session_id = SessionStore.latest(home) if args.resume == "last" else args.resume
        if not session_id:
            raise ValueError(f"no session to resume under {home}")
        loaded = SessionStore.load(home, session_id)
        session_kwargs = {
            "session_id": session_id,
            "messages": loaded.messages,
            "usage": loaded.usage,
            "cost_usd": loaded.cost_usd,
        }
    session = Session(config, provider, approver=approver, **session_kwargs)
    render = _JsonRenderer() if args.json else _TextRenderer()
    status = "error"
    try:
        async for event in session.run(task):
            render(event)
            if event.type == "run_finished":
                status = getattr(event, "status", "error")
    finally:
        await provider.aclose()
    return EXIT_CODES.get(status, 1)


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
