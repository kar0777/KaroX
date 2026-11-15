"""Code mode (M5, opt-in with ``--code-mode``): one Python program makes many tool calls.

``run_code`` runs a Python 3 program in the workspace with the project's own interpreter.
Inside it, ``tools.<name>(...)`` calls any other tool of the session (shell, read, grep,
edit, MCP tools...) through a loopback socket and returns the tool's text, so a step that
would take several model round trips (read three files, grep and read the hits, run a
command and parse it) costs one. Nested calls go through the same tool code as direct
calls: the destructive-command guard and approvals, file tracking and output limits all
apply. A failing nested call raises ``ToolError`` in the program; uncaught, it fails the
whole program (no silent truncation, lessons of KaroX 5's code mode). The model sees only
what the program prints, plus an exit-code marker.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import secrets
import shutil
import sys
import time
from collections.abc import Callable
from typing import Any

from karox6.core import events as ev
from karox6.tools.base import Tool, ToolArgsError, ToolContext, ToolResult, parse_arguments
from karox6.tools.proc import ProcessRun
from karox6.tools.shell import shell_env

DEFAULT_TIMEOUT_MS = 300_000
MAX_TIMEOUT_MS = 1_200_000
NOT_NESTED = frozenset({"run_code", "agent"})

# Runs in the project's interpreter, so: standard library only, Python 3.8 syntax.
RUNNER = r'''
import builtins, json, os, runpy, socket, sys, threading

class ToolError(Exception):
    """A tool called from the program failed; str(error) is the tool's message."""

class _Tools:
    def __init__(self, port, token, params):
        self._port, self._token, self._params = port, token, params
        self._file, self._n, self._lock = None, 0, threading.Lock()

    def __dir__(self):
        return sorted(self._params)

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        def call(*args, **kwargs):
            return self._call(name, args, kwargs)
        call.__name__ = name
        return call

    def _call(self, name, args, kwargs):
        if name not in self._params:
            known = ", ".join(sorted(self._params))
            raise ToolError("unknown tool %r; available: %s" % (name, known))
        order = self._params[name]
        if len(args) > len(order):
            raise ToolError(
                "%s takes at most %d positional arguments (%s)"
                % (name, len(order), ", ".join(order))
            )
        for key, value in zip(order, args):
            if key in kwargs:
                raise ToolError("%s got two values for %r" % (name, key))
            kwargs[key] = value
        with self._lock:
            if self._file is None:
                sock = socket.create_connection(("127.0.0.1", self._port))
                self._file = sock.makefile("rwb")
                self._send({"token": self._token})
            self._n += 1
            self._send({"id": self._n, "tool": name, "args": kwargs})
            line = self._file.readline()
        if not line:
            raise ToolError("the connection to KaroX closed")
        reply = json.loads(line.decode("utf-8"))
        if reply.get("error"):
            raise ToolError(reply.get("output", "tool failed"))
        return reply.get("output", "")

    def _send(self, obj):
        self._file.write((json.dumps(obj, default=str) + "\n").encode("utf-8"))
        self._file.flush()

_port = int(os.environ.pop("KAROX_CODE_PORT"))
_token = os.environ.pop("KAROX_CODE_TOKEN")
_params = json.loads(os.environ.pop("KAROX_CODE_TOOLS"))
builtins.tools = _Tools(_port, _token, _params)
builtins.ToolError = ToolError
_program = sys.argv[1]
sys.argv = [_program]
sys.path[0] = ""  # like `python -c`: project modules import from the working directory
runpy.run_path(_program, run_name="__main__")
'''


class RunCodeTool:
    name = "run_code"
    description = (
        "Run a Python 3 program that calls the other tools as functions, to do in one step "
        "what would otherwise take several: read or grep several files, act on search "
        "results, run commands and inspect their output, apply a series of edits. Inside "
        "the program `tools.<tool>(...)` calls a tool with its usual arguments (keywords, or "
        "positionally in the order of its required parameters), e.g. "
        "`tools.bash(command='pytest -q tests/x.py')`, `tools.read(file_path='a.py')`, "
        "`tools.grep(pattern='def f', path='src')`, `tools.edit(file_path=..., "
        "old_string=..., new_string=...)`, and returns the tool's text output. A failing "
        "tool call raises ToolError (catch it to carry on); an uncaught error fails the "
        "program. The program runs in the working directory with the project's Python and "
        "full standard library. Only what it prints comes back to you, so print exactly what "
        "you need. For a single tool call, call the tool directly instead."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "A few words saying what the program does.",
            },
            "code": {"type": "string", "description": "The Python 3 program."},
            "timeout": {
                "type": "integer",
                "description": f"Time limit in ms for the whole program (default "
                f"{DEFAULT_TIMEOUT_MS}, max {MAX_TIMEOUT_MS}).",
            },
        },
        "required": ["code"],
    }
    read_only = False

    def __init__(self, lookup: Callable[[], dict[str, Tool]]) -> None:
        self._lookup = lookup  # the session's tools, read when the program runs
        self._runs = 0

    def _tools(self) -> dict[str, Tool]:
        return {n: t for n, t in self._lookup().items() if n not in NOT_NESTED}

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        code = str(args.get("code") or "")
        if not code.strip():
            return ToolResult("Error: code is empty.", is_error=True)
        timeout_ms = min(max(int(args.get("timeout") or DEFAULT_TIMEOUT_MS), 1000), MAX_TIMEOUT_MS)
        tools = self._tools()
        self._runs += 1
        folder = ctx.session_dir / "code"
        folder.mkdir(parents=True, exist_ok=True)
        stem = f"run-{self._runs:03d}"
        program = folder / f"{stem}.py"
        program.write_text(code, encoding="utf-8")
        runner = folder / "karox_runner.py"
        if not runner.is_file():
            runner.write_text(RUNNER, encoding="utf-8")

        token = secrets.token_hex(16)
        nested = _NestedCalls(tools, ctx, token)
        server = await asyncio.start_server(nested.serve, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        env = shell_env(ctx.env or None)
        env.update(
            KAROX_CODE_PORT=str(port),
            KAROX_CODE_TOKEN=token,
            KAROX_CODE_TOOLS=json.dumps({n: _param_order(t) for n, t in tools.items()}),
            PYTHONIOENCODING="utf-8",
            PYTHONUTF8="1",
        )
        python = find_python(env)
        started = time.monotonic()
        run: ProcessRun | None = None
        try:
            run = await ProcessRun.start(
                [python, "-u", str(runner), str(program)],
                cwd=ctx.workspace,
                env=env,
                log_path=folder / f"{stem}.log",
                command=f"run_code {stem}",
            )
            finished = await run.wait(timeout_ms / 1000)
            if not finished:
                await run.kill()
        except BaseException:
            if run is not None:
                await asyncio.shield(run.kill())
            raise
        finally:
            await nested.stop()
            server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 2.0)
        text, _complete = run.output_text()
        text = text.rstrip()
        took = time.monotonic() - started
        calls = f"{nested.count} tool call{'s' if nested.count != 1 else ''}"
        if not finished:
            marker = f"[timed out after {timeout_ms / 1000:.0f}s; {calls}]"
            return ToolResult(f"{text}\n{marker}".strip(), is_error=True)
        code_rc = run.returncode
        marker = f"[exit code: {code_rc}; {calls}; {took:.1f}s]"
        body = text or "(the program printed nothing)"
        return ToolResult(
            f"{body}\n{marker}", is_error=code_rc != 0, meta={"program": str(program)}
        )


class _NestedCalls:
    """Serves one program's tool calls: token first, then one JSON request per line."""

    def __init__(self, tools: dict[str, Tool], ctx: ToolContext, token: str) -> None:
        self.tools = tools
        self.ctx = ctx
        self.token = token
        self.count = 0
        self._handlers: set[asyncio.Task[Any]] = set()

    async def stop(self) -> None:
        """The program is gone: abandon calls still running for it."""
        for task in list(self._handlers):
            task.cancel()
        for task in list(self._handlers):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._handlers.add(task)
        try:
            first = await reader.readline()
            try:
                hello = json.loads(first)
            except ValueError:
                return
            if not isinstance(hello, dict) or not hmac.compare_digest(
                str(hello.get("token", "")), self.token
            ):
                return
            while line := await reader.readline():
                try:
                    request = json.loads(line)
                    name = str(request["tool"])
                    call_args = request.get("args") or {}
                except (ValueError, KeyError, TypeError):
                    break
                output, failed = await self.call(name, call_args)
                reply = {"id": request.get("id"), "output": output, "error": failed}
                writer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        finally:
            if task is not None:
                self._handlers.discard(task)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def call(self, name: str, args: Any) -> tuple[str, bool]:
        tool = self.tools.get(name)
        self.count += 1
        call_id = f"code-{secrets.token_hex(4)}"
        raw = json.dumps(args if isinstance(args, dict) else {}, ensure_ascii=False, default=str)
        self.ctx.emit(ev.ToolStarted(call_id=call_id, name=f"run_code/{name}", arguments=raw))
        started = time.monotonic()
        if tool is None:
            output, failed = f"unknown tool {name!r}; available: {', '.join(self.tools)}", True
        else:
            try:
                parsed = parse_arguments(raw, tool.parameters)
                result = await tool.run(parsed, self.ctx)
                output, failed = result.content, result.is_error
            except ToolArgsError as exc:
                output, failed = f"{name}: {exc}", True
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                output, failed = f"{name} failed: {type(exc).__name__}: {exc}", True
        self.ctx.emit(
            ev.ToolFinished(
                call_id=call_id,
                name=f"run_code/{name}",
                is_error=failed,
                output=output,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )
        return output, failed


def _param_order(tool: Tool) -> list[str]:
    """Positional order for ``tools.x(a, b)``: required parameters, then the rest."""
    props = list((tool.parameters.get("properties") or {}).keys())
    required = [p for p in tool.parameters.get("required") or [] if p in props]
    return required + [p for p in props if p not in required]


def find_python(env: dict[str, str]) -> str:
    """The project's interpreter (first on PATH), not necessarily KaroX's own."""
    from karox6.core.prompt import _answers_version

    names = ("python", "py", "python3") if sys.platform == "win32" else ("python3", "python")
    for name in names:
        path = shutil.which(name, path=env.get("PATH") or env.get("Path"))
        if not path:
            continue
        if "\\windowsapps\\" in path.lower().replace("/", "\\") and not _answers_version(path):
            continue  # the Microsoft Store placeholder
        return path
    return sys.executable
