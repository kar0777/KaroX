"""The environment side of code mode: run a script, route its tool calls.

A script's tool call takes exactly the path a direct call takes
(:meth:`CoreToolEnvironment._execute`), with ``requested_by="script"``: the
same policy and risk checks, lease, idempotency key, persisted result and
evidence. Two things differ. The guidance notices those calls would have
carried are collected and shown once under the script's output, and the
persisted entries name the ``code_run`` call they ran inside (``nested_in``),
so a resumed transcript does not replay them as calls the model never sent.
"""

from __future__ import annotations

import itertools
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Tuple

from . import code_mode
from .agent import AgentEventKind
from .code_mode import CODE_TOOL_NAME, SCRIPT_REQUESTED_BY
from .security import redact

if TYPE_CHECKING:
    from .engine_kernel import EngineKernel


class EngineCodeModeMixin:
    """Methods composed into CoreToolEnvironment."""

    _kernel: "EngineKernel"
    _aliases: Dict[str, str]
    #: Set while a script runs: notices of its calls, and the last call's facts.
    _script_notices: Optional[List[str]] = None
    _script_last: Optional[Tuple[Any, str]] = None
    _code_worker: Optional[code_mode.CodeModeWorker] = None
    #: Kernel-clock time the running script must end by; its calls end by it too.
    _script_deadline: Optional[float] = None
    #: The script's last code edit not yet followed by a check it ran.
    _script_edit: Any = None

    def _script_seconds(self) -> float:
        remaining = float(self._kernel.remaining_seconds())
        reserve = float(getattr(self._kernel, "_finish_reserve_seconds", 0.0) or 0.0)
        # Never past the run's own deadline, however little is left.
        return min(max(1.0, remaining), max(5.0, min(code_mode.MAX_SCRIPT_SECONDS, remaining - reserve - 10.0)))

    def _code_run(self, action: Mapping[str, Any]) -> Dict[str, Any]:
        arguments = action.get("arguments")
        code = arguments.get("code") if isinstance(arguments, Mapping) else None
        if not isinstance(code, str) or not code.strip():
            return self._refusal(  # type: ignore[attr-defined]
                action,
                "malformed_arguments",
                'code_run needs {"code": "<Python source>"}.',
            )
        call_id = str(action.get("tool_call_id", "") or "")
        parent = call_id or f"script-{int(getattr(self, '_action_seq', 0))}"
        worker = self._code_worker
        if worker is None:
            worker = code_mode.CodeModeWorker()
            self._code_worker = worker
        counter = itertools.count(1)
        notices: List[str] = []
        kernel = self._kernel
        started = kernel.monotonic()
        kernel._emit(
            AgentEventKind.TOOL_STARTED,
            tool="code.run",
            call_id=call_id or None,
            detail=code.strip().splitlines()[0][:160],
        )

        def call(name: str, call_arguments: Dict[str, Any]) -> Tuple[bool, str, Any, str]:
            if name in {CODE_TOOL_NAME, "finish"}:
                return (
                    False,
                    f"{name} cannot be called from a script; call it as its own tool call",
                    {},
                    "not callable from a script",
                )
            # Argument spellings are normalized in _execute, as for a direct call.
            nested = {
                "name": name,
                "arguments": call_arguments,
                "tool_call_id": f"{parent}~{next(counter)}",
            }
            self._script_last = None
            try:
                output = self._execute(nested, requested_by=SCRIPT_REQUESTED_BY)  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - a failed call is a result
                message = f"{type(exc).__name__}: {redact(str(exc))}"
                return False, message, {}, type(exc).__name__
            text = str(output.get("output") or "")
            ok = output.get("returncode") == 0
            last = self._script_last
            data: Any = last[0] if last is not None else {}
            summary = last[1] if last is not None and last[1] else _first_line(text)
            return ok, text, data, summary

        self._script_notices = notices
        self._script_edit = None
        kernel._script_parent_call = parent
        seconds = self._script_seconds()
        self._script_deadline = kernel.monotonic() + seconds
        try:
            outcome = worker.run(
                code,
                code_mode.tool_specs(kernel.script_tools()),
                call,
                seconds=seconds,
            )
        finally:
            self._script_notices = None
            self._script_last = None
            self._script_deadline = None
            kernel._script_parent_call = None
            edit, self._script_edit = self._script_edit, None
        if edit is not None:
            # Once per script, after it: the tests of the code it changed.
            try:
                self._attach_affected_tests(edit)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - advisory only
                pass
            line = edit.data.get("affected_tests_line") if isinstance(edit.data, dict) else None
            if isinstance(line, str) and line.strip():
                notices.append(line.strip())
        text = code_mode.render(outcome, notices)
        kernel._emit(
            AgentEventKind.TOOL_FINISHED,
            tool="code.run",
            call_id=call_id or None,
            ok=outcome.ok,
            summary=f"{len(outcome.calls)} tool calls" + ("" if outcome.ok else " · error"),
            duration_seconds=round(max(0.0, kernel.monotonic() - started), 3),
        )
        return {
            "output": text,
            "observation": text,
            "returncode": 0 if outcome.ok else 1,
            "exception_info": "" if outcome.ok else "script_error",
            # Identical scripts with identical results are the no-progress
            # signal; the call lines already carry each nested outcome.
            "progress_key": text,
        }

    def close_code_mode(self) -> None:
        worker, self._code_worker = self._code_worker, None
        if worker is not None:
            worker.close()


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:160]
    return ""
