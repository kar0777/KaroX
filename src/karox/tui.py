"""Optional interactive TUI shell for KaroX.

The TUI is a thin presentation layer over the already-working CLI backend.  It
contains *no* business logic: every slash command is translated into the same
``argparse`` arguments that ``karox.cli.main`` already handles, so the backend
stays fully usable without the TUI (line-mode, non-interactive, JSON, CI).

Rendering uses ``rich`` when it is importable and falls back to plain text
otherwise, so the TUI never hard-fails on a minimal install.
"""

from __future__ import annotations

import os
import shlex
import sys
from typing import Any, Callable, Dict, List, Optional, Sequence

try:  # optional dependency; the TUI degrades to plain text without it
    from rich.console import Console
    from rich.panel import Panel
    from rich.table import Table

    _HAS_RICH = True
except Exception:  # pragma: no cover - exercised only without rich installed
    _HAS_RICH = False
    Console = None  # type: ignore[assignment, misc]
    Panel = None  # type: ignore[assignment, misc]
    Table = None  # type: ignore[assignment, misc]


# Slash commands mapped to the underlying CLI argv they reproduce.  Each value
# is a function returning the full argv (minus the leading program name) so the
# shell can stay stateless about argument construction.
SLASH_COMMANDS: Dict[str, str] = {
    "/help": "show this help",
    "/provider": "manage API providers (add/list/show/test)",
    "/model": "manage models (list/inspect/select/test)",
    "/status": "show the current session status",
    "/plan": "show the session plan and remaining steps",
    "/diff": "show the current Git diff",
    "/tests": "show recorded checks",
    "/checkpoint": "show checkpoints",
    "/cost": "show usage and cost",
    "/context": "show session context summary",
    "/handoff": "emit a structured handoff document",
    "/bridge": "manage bridge profiles and credentials",
    "/permissions": "show session MCP permissions",
    "/doctor": "run diagnostics",
    "/exit": "leave the TUI",
}


def _render_help(output_stream: Any, out: Callable[[str], None]) -> None:
    if _HAS_RICH:
        table = Table(title="KaroX slash commands", show_header=True)
        table.add_column("Command", style="cyan", no_wrap=True)
        table.add_column("Description")
        for name, desc in SLASH_COMMANDS.items():
            table.add_row(name, desc)
        Console(file=output_stream).print(table)
    else:
        out("KaroX slash commands:")
        for name, desc in SLASH_COMMANDS.items():
            out(f"  {name:<14} {desc}")


def _slash_to_argv(line: str, session_id: Optional[str], repository: str) -> Optional[List[str]]:
    """Translate one slash command line into a CLI argv list.

    Returns ``None`` for unknown slash commands so the caller can report it.
    Bare ``/help`` and ``/exit`` are handled by the shell itself.
    """
    parts = shlex.split(line)
    if not parts:
        return None
    cmd = parts[0]
    rest = parts[1:]
    common: List[str] = []
    if session_id and cmd in {"/status", "/plan", "/diff", "/tests", "/checkpoint", "/cost",
                             "/context", "/handoff", "/permissions"}:
        common += ["--session-id", session_id, "--repository", repository]
    mapping: Dict[str, List[str]] = {
        "/provider": ["provider"] + (rest or ["list", "--json"]),
        "/model": ["model"] + (rest or ["list", "--json"]),
        "/status": ["session", "show", session_id or ""] if session_id else ["session", "list", "--json"],
        "/plan": ["session", "show", session_id or "", "--json"] if session_id else ["session", "list", "--json"],
        "/diff": ["session", "show", session_id or "", "--json"] if session_id else ["session", "list", "--json"],
        "/tests": ["session", "show", session_id or "", "--json"] if session_id else ["session", "list", "--json"],
        "/checkpoint": ["session", "show", session_id or "", "--json"] if session_id else ["session", "list", "--json"],
        "/cost": ["session", "show", session_id or "", "--json"] if session_id else ["session", "list", "--json"],
        "/context": ["session", "show", session_id or "", "--json"] if session_id else ["session", "list", "--json"],
        "/handoff": ["session", "handoff", session_id or "", "--repository", repository] if session_id else ["session", "list", "--json"],
        "/bridge": ["bridge"] + (rest or ["list", "--json"]),
        "/permissions": ["session", "show", session_id or "", "--json"] if session_id else ["session", "list", "--json"],
        "/doctor": ["doctor"],
    }
    if cmd in mapping:
        argv = mapping[cmd]
        return argv
    return None


def _run_cli(argv: Sequence[str], out: Callable[[str], None]) -> int:
    """Invoke the CLI backend with ``argv`` and capture its exit code/output.

    The TUI never reimplements handlers -- it calls the real entrypoint so the
    behavior is identical to non-interactive use.
    """
    from .cli import main

    # Capture stdout/stderr the backend writes, then relay it.
    import io
    from contextlib import redirect_stdout, redirect_stderr

    stdout_buf = io.StringIO()
    stderr_buf = io.StringIO()
    try:
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            code = main(list(argv))
    except SystemExit as exc:  # pragma: no cover - defensive
        code = int(exc.code) if exc.code is not None else 0
    for buffer in (stdout_buf, stderr_buf):
        text = buffer.getvalue()
        if text.strip():
            out(text.rstrip("\n"))
    return code


def run_tui(
    *,
    session_id: Optional[str] = None,
    repository: Optional[str] = None,
    input_stream=sys.stdin,
    output_stream=sys.stdout,
) -> int:
    """Run the interactive TUI loop.

    The loop reads lines, translates slash commands to CLI argv, and dispatches
    to the backend.  Plain text (non-slash) input is treated as a raw CLI argv
    for power users.  ``/exit`` or EOF ends the session.
    """
    repo = repository or os.getcwd()
    out = output_stream.write
    # When the input stream is the real stdin we keep readline-backed
    # ``input()`` for line editing; an injected stream (tests, pipes captured
    # by callers) is read directly so the parameter is actually honoured.
    interactive = input_stream is sys.stdin
    if _HAS_RICH:
        Console(file=output_stream).print(
            Panel("KaroX interactive shell — type /help for commands, /exit to quit.",
                  title="KaroX", border_style="cyan")
        )
    else:
        out("KaroX interactive shell — type /help for commands, /exit to quit.\n")

    while True:
        try:
            prompt = f"karox ({session_id or 'no-session'})> "
            if interactive:
                line = input(prompt)
            else:
                data = input_stream.readline()
                if data == "":
                    raise EOFError
                line = data.rstrip("\n")
        except EOFError:
            out("\n")
            return 0
        except KeyboardInterrupt:
            out("\n(interrupt — type /exit to quit)\n")
            continue
        line = line.strip()
        if not line:
            continue
        if line == "/exit":
            return 0
        if line == "/help":
            _render_help(output_stream, out)
            continue
        argv = _slash_to_argv(line, session_id, repo)
        if argv is None and line.startswith("/"):
            out(f"unknown slash command: {line.split()[0]} (try /help)\n")
            continue
        if argv is None:
            # Non-slash input: treat as raw CLI argv.
            try:
                argv = shlex.split(line)
            except ValueError as exc:
                out(f"cannot parse input: {exc}\n")
                continue
        if not argv:
            continue
        code = _run_cli(argv, out)
        if code != 0 and not line.startswith("/"):
            out(f"(exit code {code})\n")
    # unreachable
