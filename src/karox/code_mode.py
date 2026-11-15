"""Code mode: one tool that runs a Python script calling the other tools.

Every response a model sends is a full round trip: the provider re-reads the
whole conversation before the next step is decided. A chain such as "search,
read the two hits, run the covering test, read the failing lines" costs four
of them when each call needs its own response, and every intermediate result
stays in the context afterwards. Claude Code's code mode (2.1.323), Codex's
code-mode host and DeepSeek Harness's programmatic tool calling remove that
cost by letting the model write a short program that makes the calls, keeps
intermediate values in variables and returns only what it needs to read.

KaroX's version is plain Python, which every model writes well, offered next
to the ordinary tools rather than instead of them. Scripts run in
:mod:`karox.code_mode_worker`, a separate interpreter with an empty
environment and no file, process or network access of its own; each tool call
a script makes comes back here and runs through the same environment path as a
direct call (policy, risk engine, lease, audit, persisted evidence). Variables
persist between scripts of one run.
"""

from __future__ import annotations

import json
import os
import pathlib
import queue
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from .providers import ProviderTool

CODE_TOOL_NAME = "code_run"
#: Who asked for a tool call made from inside a script.
SCRIPT_REQUESTED_BY = "script"
#: Tool calls one script may make. A loop that needs more is better split.
MAX_CALLS_PER_SCRIPT = 40
#: Longest a script may run, nested tool calls included.
MAX_SCRIPT_SECONDS = 900.0
#: What the model sees of one script's output, head and tail kept.
OUTPUT_CHARS = 12_000
#: Structured result fields handed to a script, per call.
DATA_CHARS = 60_000
_WORKER = pathlib.Path(__file__).with_name("code_mode_worker.py")
#: Tools a script cannot call: finish ends the run and must be its own call.
_NOT_FROM_SCRIPTS = frozenset({CODE_TOOL_NAME, "finish"})


def enabled() -> bool:
    """Code mode is on unless ``KAROX_CODE_MODE=0`` (ablation runs)."""

    return os.environ.get("KAROX_CODE_MODE", "1").strip() != "0"


def provider_tool() -> ProviderTool:
    return ProviderTool(
        CODE_TOOL_NAME,
        (
            "Run a Python script that calls your tools, in one step. Every other tool is a function here "
            "with the same arguments, by keyword (the required ones may also go first, in order): "
            "repo_read_lines('a.py', start=40, count=60), repo_search(query='def parse', path='src'), "
            "checks_run(argv=['python', '-m', 'pytest', '-q', 'tests/test_a.py']). A call returns a str, the "
            "text the tool would show you, with .ok, .exit_code and .data (the tool's structured result); a "
            "failed call returns .ok False and does not raise. Only what the script prints, or its final "
            "expression, reaches you, followed by one line per call: print just what you need to read. Use it "
            "when the next calls need no judgment in between: read several files, search and read the hits, "
            "run a probe and then the covering tests, make the same edit in many places. Variables persist "
            "between scripts. The script has no file, process or network access of its own (standard library "
            "only): it reaches the repository only through the tools, each checked as if you called it. "
            "finish is never called from a script."
        ),
        {
            "type": "object",
            "properties": {
                "code": {
                    "type": "string",
                    "description": "Python source. Top-level statements run in order; the value of a final "
                    "expression is shown like a print.",
                }
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    )


def tool_specs(tools: Sequence[ProviderTool]) -> List[Dict[str, Any]]:
    """Name and required parameters of each tool a script may call."""

    specs: List[Dict[str, Any]] = []
    for tool in tools:
        if tool.name in _NOT_FROM_SCRIPTS:
            continue
        schema = tool.input_schema if isinstance(tool.input_schema, Mapping) else {}
        required = schema.get("required") if isinstance(schema.get("required"), list) else []
        specs.append({"name": tool.name, "required": [str(item) for item in required]})
    return specs


def json_safe(value: Any, budget: int = DATA_CHARS) -> Any:
    """A JSON-encodable copy of a tool's data, long strings shortened."""

    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return {}
    if len(text) <= budget:
        return json.loads(text)
    limit = max(200, budget // 8)

    def shrink(item: Any) -> Any:
        if isinstance(item, str) and len(item) > limit:
            return item[: limit // 2] + f"\n... [{len(item) - limit} chars omitted] ...\n" + item[-limit // 2 :]
        if isinstance(item, list):
            return [shrink(entry) for entry in item[:200]]
        if isinstance(item, dict):
            return {str(key): shrink(entry) for key, entry in item.items()}
        return item

    return shrink(json.loads(text))


@dataclass
class CallRecord:
    name: str
    ok: bool
    summary: str


@dataclass
class ScriptOutcome:
    output: str = ""
    value: Optional[str] = None
    error: Optional[str] = None
    dropped: int = 0
    calls: List[CallRecord] = field(default_factory=list)
    restarted: bool = False

    @property
    def ok(self) -> bool:
        return self.error is None


#: (name, arguments) -> (ok, text, data, summary)
CallTool = Callable[[str, Dict[str, Any]], Tuple[bool, str, Any, str]]


class CodeModeWorker:
    """One worker interpreter for a run, started on first use."""

    def __init__(self, python: Optional[str] = None) -> None:
        self._python = python or sys.executable
        self._process: Optional[subprocess.Popen[bytes]] = None
        self._lines: "queue.Queue[Optional[bytes]]" = queue.Queue()
        self._scratch: Optional[tempfile.TemporaryDirectory[str]] = None
        self._evals = 0
        self._lost_state = False

    # -- lifecycle -----------------------------------------------------------

    def _environment(self) -> Dict[str, str]:
        keep = {"PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        for name in ("SYSTEMROOT", "WINDIR", "LANG", "LC_ALL"):
            value = os.environ.get(name)
            if value:
                keep[name] = value
        return keep

    def _start(self, deadline: float) -> None:
        self._scratch = tempfile.TemporaryDirectory(prefix="karox-code-")
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self._process = subprocess.Popen(
            [self._python, "-I", "-S", "-u", str(_WORKER)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=self._scratch.name,
            env=self._environment(),
            creationflags=flags,
        )
        self._lines = queue.Queue()
        stdout = self._process.stdout
        lines = self._lines

        def pump() -> None:
            try:
                assert stdout is not None
                for line in iter(stdout.readline, b""):
                    lines.put(line)
            except (OSError, ValueError):
                pass
            lines.put(None)

        threading.Thread(target=pump, name="karox-code-mode", daemon=True).start()
        ready = self._next(deadline)
        if not ready or ready.get("t") != "ready":
            self.close()
            raise RuntimeError("the code_run worker did not start")

    def close(self) -> None:
        process, self._process = self._process, None
        if process is not None:
            # Kill first: a writer thread blocked on a full pipe holds the
            # stdin buffer's lock, so closing stdin first would wait forever.
            try:
                process.kill()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except (OSError, ValueError):
                pass
        scratch, self._scratch = self._scratch, None
        if scratch is not None:
            try:
                scratch.cleanup()
            except OSError:
                pass

    @property
    def alive(self) -> bool:
        return self._process is not None and self._process.poll() is None

    # -- protocol ------------------------------------------------------------

    def _send(self, message: Mapping[str, Any], deadline: float) -> None:
        """Write one message, or give the worker up when it stops reading.

        A script that never returns to its loop (or forged a ``done`` and kept
        running) leaves the pipe unread; a write larger than the pipe buffer
        would then block KaroX itself past every deadline.
        """

        process = self._process
        if process is None or process.stdin is None:
            raise BrokenPipeError("worker is not running")
        stdin = process.stdin
        data = (json.dumps(message, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        failed: List[BaseException] = []

        def write() -> None:
            try:
                stdin.write(data)
                stdin.flush()
            except (OSError, ValueError) as exc:
                failed.append(exc)

        writer = threading.Thread(target=write, name="karox-code-mode-write", daemon=True)
        writer.start()
        writer.join(max(0.1, deadline - time.monotonic()))
        if writer.is_alive():
            self.close()
            raise BrokenPipeError("the script's interpreter stopped reading")
        if failed:
            raise BrokenPipeError(str(failed[0]))

    def _next(self, deadline: float) -> Optional[Dict[str, Any]]:
        """The worker's next message; None when it exited or time ran out."""

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            try:
                line = self._lines.get(timeout=min(remaining, 1.0))
            except queue.Empty:
                continue
            if line is None:
                return None
            try:
                value = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(value, dict):
                return value

    def run(
        self,
        code: str,
        tools: Sequence[Mapping[str, Any]],
        call_tool: CallTool,
        *,
        seconds: float,
        max_calls: int = MAX_CALLS_PER_SCRIPT,
    ) -> ScriptOutcome:
        outcome = ScriptOutcome()
        deadline = time.monotonic() + max(1.0, min(float(seconds), MAX_SCRIPT_SECONDS))
        if not self.alive:
            if self._evals:
                self._lost_state = True
            self.close()
            try:
                self._start(min(deadline, time.monotonic() + 30.0))
            except (OSError, RuntimeError) as exc:
                outcome.error = f"code_run is unavailable: {exc}"
                return outcome
        outcome.restarted, self._lost_state = self._lost_state, False
        self._evals += 1
        parts: List[str] = []
        try:
            self._send({"t": "eval", "id": self._evals, "code": code, "tools": list(tools)}, deadline)
            while True:
                message = self._next(deadline)
                if message is None:
                    seconds_used = int(max(1.0, min(float(seconds), MAX_SCRIPT_SECONDS)))
                    reason = (
                        f"stopped after {seconds_used}s (time limit); variables from earlier scripts are gone"
                        if self.alive
                        else "the script's interpreter exited; variables from earlier scripts are gone"
                    )
                    outcome.error = reason
                    self.close()
                    self._lost_state = False
                    break
                kind = message.get("t")
                if kind == "call":
                    parts.append(str(message.get("out") or ""))
                    name = str(message.get("name") or "")
                    arguments = message.get("args")
                    if len(outcome.calls) >= max_calls:
                        self._send({"t": "abort", "reason": f"more than {max_calls} tool calls in one script"}, deadline)
                        continue
                    if not isinstance(arguments, dict):
                        arguments = {}
                    ok, text, data, summary = call_tool(name, arguments)
                    outcome.calls.append(CallRecord(name, ok, summary))
                    self._send({"t": "ret", "ok": ok, "text": text, "data": json_safe(data)}, deadline)
                    continue
                if kind == "done":
                    parts.append(str(message.get("out") or ""))
                    outcome.dropped = int(message.get("dropped") or 0)
                    value = message.get("value")
                    outcome.value = str(value) if value is not None else None
                    error = message.get("error")
                    outcome.error = str(error) if error else None
                    break
        except (BrokenPipeError, OSError) as exc:
            outcome.error = f"the script's interpreter stopped ({type(exc).__name__}); variables are gone"
            self.close()
        outcome.output = "".join(parts)
        return outcome


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head, tail = int(limit * 0.6), int(limit * 0.4)
    return (
        f"{text[:head]}\n... [{len(text) - head - tail} chars of script output dropped; print less or slice] ...\n"
        f"{text[-tail:]}"
    )


def render(outcome: ScriptOutcome, notices: Sequence[str] = (), *, limit: int = OUTPUT_CHARS) -> str:
    """The one observation the model gets for a script."""

    sections: List[str] = []
    if outcome.restarted:
        sections.append("[the code_run interpreter restarted: variables from earlier scripts are gone]")
    body = outcome.output.rstrip("\n")
    if outcome.value is not None:
        body = f"{body}\n{outcome.value}" if body else outcome.value
    if outcome.dropped:
        body += f"\n[{outcome.dropped} more chars were printed and not kept]"
    if body:
        sections.append(_clip(body, limit))
    if outcome.error:
        sections.append(f"Error: {outcome.error}" if not outcome.error.startswith("Traceback") else outcome.error)
    if outcome.calls:
        lines = [f"[{len(outcome.calls)} tool call{'s' if len(outcome.calls) != 1 else ''}]"]
        for index, call in enumerate(outcome.calls[:MAX_CALLS_PER_SCRIPT], 1):
            lines.append(f"{index}. {call.name}: {call.summary}" if call.ok else f"{index}. {call.name}: FAILED {call.summary}")
        sections.append("\n".join(lines))
        if not body and not outcome.error:
            sections.append("Nothing was printed: print or return what you need to read.")
    elif not body and not outcome.error:
        sections.append("(no output)")
    unique: List[str] = []
    for notice in notices:
        if notice and notice not in unique:
            unique.append(notice)
    sections.extend(unique)
    return "\n\n".join(sections)
