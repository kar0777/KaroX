"""The vendored karo-core engine as KaroX's default agent runtime.

``karox agent run`` used to reach only the v5 :class:`AgentKernel`; the
measured karo-core loop sat unused in ``karox.engine``. This module is the
integration point that was missing: it runs the engine's own
:class:`~karox.engine.agents.default.DefaultAgent` as the default native
agent, while keeping every KaroX product boundary that the engine never
had:

- the model is reached only through :mod:`karox.provider_adapters`
  (``Provider.complete``), never through the engine's litellm path, so
  credentials, routing, retries and pricing stay in one audited place;
- every engine shell action passes ``policy.py`` (capability) and
  ``risk_engine.py``/``action_policy.py`` (consequence) before execution,
  and the global command invariants (no push, no publish, no remote
  transport, no auth) are enforced on the raw command string;
- the session store keeps its lease, checksums, transcript and usage, and
  the harness runs the user-approved verification commands itself after
  the engine submits, because the engine can only submit -- it cannot
  prove -- a workspace;
- the NDJSON event protocol (``step_started`` ... ``finished``) is emitted
  unchanged, so the terminal client and TUI keep working without knowing
  which loop executed the task.

The old kernel remains available behind an explicit ``--legacy-loop``
flag; it is not the default and is not renamed.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import yaml

from .agent import (
    AgentEvent,
    AgentEventKind,
    AgentLimits,
    AgentObserver,
    AgentReport,
)
from .agent_modes import normalize_mode
from .command_guard import DeveloperCommandBlocked, validate_developer_command_argv
from .models import Capability, CoreCommand, Origin
from .policy import CapabilityPolicy, PolicyDenied
from .providers import ModelMessage, ModelRequest, ModelResponse, ProviderTool, ToolCall
from .risk_mapping import action_for_command
from .security import redact
from .sessions import MutationLease, SessionError, SessionRecord, SessionStore

# The engine prints a startup banner and loads a global dotenv on import. A
# product import must be silent and must not adopt another tool's config file.
os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

from .engine import __version__ as engine_version  # noqa: E402
from .engine import package_dir  # noqa: E402
from .engine.agents.default import DefaultAgent  # noqa: E402
from .engine.environments.local import LocalEnvironment, clip_output  # noqa: E402
from .engine.exceptions import FormatError, LimitsExceeded, Submitted, TimeExceeded  # noqa: E402
from .engine.models.litellm_model import salvage_fenced_command  # noqa: E402
from .engine.models.utils.actions_toolcall import (  # noqa: E402
    format_toolcall_observation_messages,
    parse_toolcall_actions,
)

#: The engine's own exit labels, mapped onto report reasons.
_REASON_BY_EXIT = {
    "Submitted": "submitted",
    "LimitsExceeded": "step_limit",
    "TimeExceeded": "wall_time_limit",
    "RepeatedFormatError": "format_errors_exhausted",
}

#: KaroX global invariants that must hold even inside a shell string. The
#: command guard validates parsed argv; the engine's actions arrive as raw
#: shell lines where ``git push`` can hide behind ``&&``. These patterns are
#: the string-level backstop for exactly the classes the guard blocks.
_STRING_BLOCKED: Tuple[Tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"\bgit\s+(?:-[A-Za-z-]+\s+)*push\b"),
        "git push is not part of the agent workflow; it requires the owner's "
        "one-shot approval outside the agent loop",
    ),
    (
        re.compile(r"\b(?:npm|pnpm|yarn|bun)\s+publish\b"),
        "package publishing is a publishing action and stays behind an "
        "explicit owner decision",
    ),
    (
        re.compile(r"\btwine\s+(?:upload|register)\b"),
        "package publishing is a publishing action and stays behind an "
        "explicit owner decision",
    ),
    (
        re.compile(r"\b(?:ssh|scp|sftp|sshpass|rsync\s+-e)\b"),
        "remote transport commands are blocked; use an approved network tool",
    ),
)

_BASH_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "The bash command to execute",
        }
    },
    "required": ["command"],
}

#: Output redirection, ``tee`` and ``cd`` are the three ways a one-line shell
#: command escapes the repository working directory. Each regex captures the
#: single token that names the destination or directory.
_REDIRECT_TARGET = re.compile(r"(?:>>|>|1>|2>)\s*(\"[^\"]+\"|'[^']+'|[^\s;&|<>]+)")
_CD_TARGET = re.compile(r"(?:^|&&|\|\||;|\|)\s*cd\s+(\"[^\"]+\"|'[^']+'|[^\s;&|<>]+)")
_TEE_TARGET = re.compile(r"\btee\s+(?:-a\s+)?(\"[^\"]+\"|'[^']+'|[^\s;&|<>]+)")
_WRITE_COMMAND_PREFIXES = (">>", ">", "tee ")


class EngineUsage:
    """Cumulative provider usage for one engine run, in report vocabulary."""

    def __init__(self) -> None:
        self.requests = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.total_tokens = 0
        self.costs: Dict[str, float] = {}

    def add(self, response: ModelResponse) -> None:
        self.requests += 1
        usage = response.usage or {}
        self.input_tokens += int(usage.get("input_tokens", 0) or 0)
        self.output_tokens += int(usage.get("output_tokens", 0) or 0)
        self.cache_read_tokens += int(usage.get("cache_read_tokens", 0) or 0)
        self.cache_write_tokens += int(usage.get("cache_write_tokens", 0) or 0)
        self.total_tokens += int(usage.get("total_tokens", 0) or 0)
        if response.cost is not None:
            currency = response.currency or "USD"
            self.costs[currency] = round(
                float(self.costs.get(currency, 0.0)) + float(response.cost), 6
            )

    def to_dict(self) -> Dict[str, Any]:
        value: Dict[str, Any] = {
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
        }
        if self.costs:
            value["costs"] = dict(self.costs)
        return value


class _FunctionFragment:
    """litellm-shaped tool-call fragment for the engine's parser."""

    def __init__(self, name: str, arguments: str) -> None:
        self.name = name
        self.arguments = arguments


class _ToolCallFragment:
    def __init__(self, call: ToolCall) -> None:
        self.id = call.call_id
        self.function = _FunctionFragment(call.name, call.raw_arguments)


class ProviderEngineModel:
    """The engine's Model protocol on top of KaroX provider adapters.

    The engine's measured parsing behavior (native tool calls, the
    single-fenced-command salvage, the format-error contract that persists
    the response and cost) is reused verbatim from the engine package; only
    the transport is replaced.
    """

    def __init__(
        self,
        provider: Any,
        model: str,
        *,
        format_error_template: str,
        observation_template: str,
        usage: EngineUsage,
        max_output_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        deadline_seconds: float = 300.0,
        on_event: Optional[AgentObserver] = None,
    ) -> None:
        self._provider = provider
        self._model = model
        self._format_error_template = format_error_template
        self._observation_template = observation_template
        self._usage = usage
        self._max_output_tokens = max_output_tokens
        self._reasoning_effort = reasoning_effort
        self._deadline_seconds = deadline_seconds
        self._on_event = on_event
        self._bash_tool = ProviderTool(
            name="bash",
            description="Execute a bash command",
            input_schema=_BASH_TOOL_PARAMETERS,
        )

    # -- engine Model protocol -------------------------------------------

    def query(self, messages: List[dict], **kwargs: Any) -> dict:
        del kwargs
        prepared = [
            message
            for message in (_request_message(item) for item in messages)
            if message is not None
        ]
        request = ModelRequest(
            model=self._model,
            messages=tuple(prepared),
            tools=(self._bash_tool,),
            max_output_tokens=self._max_output_tokens,
            reasoning_effort=self._reasoning_effort,
            deadline_seconds=self._deadline_seconds,
        )
        try:
            response = self._provider.complete(request)
        except Exception as exc:
            raise self._format_error(f"provider request failed: {exc}") from exc
        self._usage.add(response)
        tool_calls = [_ToolCallFragment(call) for call in response.tool_calls]
        if tool_calls:
            try:
                actions = parse_toolcall_actions(
                    tool_calls,
                    format_error_template=self._format_error_template,
                    template_kwargs={"finish_reason": response.finish_reason},
                )
            except FormatError as exc:
                self._persist_on_format_error(exc, response)
                raise
        else:
            command = salvage_fenced_command(response.content or "")
            if command is None:
                exc = self._format_error(
                    "No tool calls found in the response. Every response MUST "
                    "include exactly one bash tool call."
                )
                self._persist_on_format_error(exc, response)
                raise exc
            actions = [{"command": command}]
        message = {
            "role": "assistant",
            "content": response.content,
            "tool_calls": [
                {
                    "id": call.call_id,
                    "type": "function",
                    "function": {"name": call.name, "arguments": call.raw_arguments},
                }
                for call in response.tool_calls
            ],
            "extra": {
                "actions": actions,
                "cost": float(response.cost or 0.0),
                "timestamp": time.time(),
            },
        }
        if isinstance(response.content, str) and response.content.strip():
            self._emit(
                AgentEvent(
                    AgentEventKind.TEXT_DELTA,
                    step=self._usage.requests,
                    text_delta=response.content,
                )
            )
        return message

    def format_message(self, **kwargs: Any) -> dict:
        message = {"role": kwargs["role"], "content": kwargs["content"]}
        extra = kwargs.get("extra")
        if isinstance(extra, dict):
            message["extra"] = extra
        return message

    def format_observation_messages(
        self, message: dict, outputs: List[dict], template_vars: Optional[dict] = None
    ) -> List[dict]:
        return format_toolcall_observation_messages(
            actions=message.get("extra", {}).get("actions", []),
            outputs=outputs,
            observation_template=self._observation_template,
            template_vars=template_vars,
        )

    def get_template_vars(self, **kwargs: Any) -> Dict[str, Any]:
        return {"model_name": self._model, **kwargs}

    def serialize(self) -> dict:
        return {
            "info": {
                "config": {
                    "model": {"model_name": self._model, "transport": "karox-provider"},
                    "model_type": f"{self.__class__.__module__}.{self.__class__.__name__}",
                }
            }
        }

    def _emit(self, event: AgentEvent) -> None:
        if self._on_event is None:
            return
        with contextlib.suppress(Exception):
            self._on_event(event)

    # -- internals ---------------------------------------------------------

    def _format_error(self, text: str) -> FormatError:
        return FormatError(
            {
                "role": "user",
                "content": text,
                "extra": {"interrupt_type": "FormatError"},
            }
        )

    def _persist_on_format_error(self, error: FormatError, response: ModelResponse) -> None:
        # Engine contract: the billed response and its cost must survive a
        # format error, or a repeated-error budget cannot account for it.
        if not error.messages:
            return
        extra = error.messages[0].setdefault("extra", {})
        extra.update(
            {
                "cost": float(response.cost or 0.0),
                "response": {
                    "choices": [
                        {
                            "finish_reason": response.finish_reason,
                            "message": {"content": response.content},
                        }
                    ],
                    "usage": dict(response.usage or {}),
                },
            }
        )


def _request_message(entry: Mapping[str, Any]) -> Optional[ModelMessage]:
    role = str(entry.get("role") or "")
    content = entry.get("content")
    if content is not None and not isinstance(content, str):
        content = str(content)
    if role == "tool":
        tool_call_id = entry.get("tool_call_id")
        if isinstance(tool_call_id, str) and tool_call_id:
            return ModelMessage(role="tool", content=content, tool_call_id=tool_call_id)
        # An observation that lost its tool-call anchor degrades to user text
        # rather than being dropped: dropping it would hide evidence.
        return ModelMessage(role="user", content=content)
    if role == "assistant":
        calls = entry.get("tool_calls")
        tool_calls: Tuple[ToolCall, ...] = ()
        if isinstance(calls, list):
            parsed: List[ToolCall] = []
            for call in calls:
                if not isinstance(call, Mapping):
                    continue
                function = call.get("function")
                if not isinstance(function, Mapping):
                    continue
                call_id = str(call.get("id") or "")
                name = str(function.get("name") or "")
                arguments = function.get("arguments")
                raw_arguments = (
                    arguments
                    if isinstance(arguments, str)
                    else (
                        _json_dumps_compact(arguments)
                        if arguments is not None
                        else "{}"
                    )
                )
                if call_id and name:
                    parsed.append(
                        ToolCall(call_id=call_id, name=name, raw_arguments=raw_arguments)
                    )
            tool_calls = tuple(parsed)
        return ModelMessage(role="assistant", content=content, tool_calls=tool_calls)
    if role in {"system", "user"}:
        return ModelMessage(role=role, content=content)
    return None


def _json_dumps_compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class _EventAgent(DefaultAgent):
    """DefaultAgent that reports each step on the NDJSON event channel."""

    def __init__(self, *args: Any, on_event: Optional[AgentObserver] = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._event_observer = on_event

    def step(self) -> list[dict]:
        self._emit_event(
            AgentEvent(AgentEventKind.STEP_STARTED, step=self.n_calls + 1)
        )
        try:
            return super().step()
        finally:
            self._emit_event(
                AgentEvent(AgentEventKind.STEP_FINISHED, step=self.n_calls)
            )

    def _emit_event(self, event: AgentEvent) -> None:
        if self._event_observer is None:
            return
        with contextlib.suppress(Exception):
            self._event_observer(event)


@dataclass
class _GateVerdict:
    allowed: bool
    reason: str = ""
    risk: str = ""


class GuardedEngineEnvironment(LocalEnvironment):
    """The engine's local environment with KaroX's execution boundaries.

    The engine executes one raw shell line per action. Before that line
    reaches a shell it must pass the capability policy, the global command
    invariants (checked on the parsed argv *and* the raw string, because a
    blocked action can hide behind ``&&``), and the consequence decision
    engine. Anything destructive, external or outside the task's intent
    fails closed: the engine receives a refusal observation it can act on,
    never a silent pass.

    ``in_band_commands`` is the typed-tool seam: a callable that may claim a
    command line (for example the in-band ``mcp`` protocol) and answer it
    with an observation directly, applying its own policy, lease and
    idempotency gates the way the MCP adapter does. A claimed command never
    reaches the shell; an unclaimed one passes the full gate above.
    """

    def __init__(
        self,
        *,
        policy: CapabilityPolicy,
        origin: Origin,
        session_id: str,
        repository: Path,
        task: str,
        decisions: Any,
        bypass_mode: bool,
        mode: Optional[str],
        on_event: Optional[AgentObserver],
        command_timeout: int,
        output_char_limit: int,
        in_band_commands: Optional[
            Callable[[str, int], Optional[Dict[str, Any]]]
        ] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(timeout=command_timeout, output_char_limit=output_char_limit, **kwargs)
        self._policy = policy
        self._origin = origin
        self._session_id = session_id
        self._repository = repository
        self._task = task
        self._decisions = decisions
        self._bypass_mode = bypass_mode
        self._mode = None if mode is None else normalize_mode(mode)
        self._on_event = on_event
        self._in_band = in_band_commands
        self.refusals: List[Dict[str, Any]] = []

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = str(action.get("command") or "")
        started = time.monotonic()
        self._emit(
            AgentEvent(
                AgentEventKind.TOOL_STARTED,
                step=0,
                tool="engine.bash",
                call_id=str(action.get("tool_call_id") or "") or None,
                summary=clip_output(command, 200),
            )
        )
        claimed: Optional[Dict[str, Any]] = None
        if self._in_band is not None:
            claimed = self._in_band(command, int(timeout or self.config.timeout))
        if claimed is not None:
            # Normalize to the engine's observation contract: the engine's
            # observation template reads output.exception_info, so an
            # in-band handler that omits it must not reach the template.
            output = {
                "output": str(claimed.get("output") or ""),
                "returncode": int(claimed.get("returncode", -1)),
                "exception_info": str(claimed.get("exception_info") or ""),
            }
        else:
            verdict = self._gate(command)
            if verdict.allowed:
                output = self._run_guarded(command, timeout or self.config.timeout)
            else:
                output = {
                    "output": (
                        f"karox refused to run this command: {verdict.reason}\n"
                        "Adjust the command to stay inside the repository and the "
                        "task, or finish with COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT."
                    ),
                    "returncode": 126,
                    "exception_info": "",
                    "extra": {"refused": True, "risk": verdict.risk},
                }
                self.refusals.append(
                    {"command": command, "reason": verdict.reason, "risk": verdict.risk}
                )
        output["output"] = clip_output(output["output"], self.config.output_char_limit)
        # The finish marker raises Submitted from _check_finished, so the
        # tool_finished event is emitted first: a watcher must see every
        # tool_started paired, including the one that ends the run.
        self._emit(
            AgentEvent(
                AgentEventKind.TOOL_FINISHED,
                step=0,
                tool="engine.bash",
                call_id=str(action.get("tool_call_id") or "") or None,
                ok=output.get("returncode") == 0,
                summary=clip_output(command, 200),
                duration_seconds=round(time.monotonic() - started, 3),
            )
        )
        self._check_finished(output)
        return output

    # -- gating -------------------------------------------------------------

    def _gate(self, command: str) -> _GateVerdict:
        if not command.strip():
            return _GateVerdict(False, "the command is empty")
        # Capability first: an engine action is a process run, the authority
        # every workspace_write session already holds. The profile ceiling
        # stays untouched -- the consequence engine, the string-level command
        # invariants and repository confinement bound what it may do.
        try:
            self._policy.require(self._origin, Capability.PROCESS_RUN)
        except PolicyDenied as exc:
            return _GateVerdict(False, str(exc), risk="capability")
        # Global invariants, on the raw string: a blocked action may hide
        # behind pipes, redirects or command separators.
        for pattern, reason in _STRING_BLOCKED:
            if pattern.search(command):
                return _GateVerdict(False, reason, risk="invariant")
        # Repository confinement: redirection targets, tee destinations and
        # ``cd`` hops are the ways a shell line writes or works outside the
        # repository. Anything that resolves outside is refused.
        escape = _confinement_escape(command, self._repository)
        if escape is not None:
            return _GateVerdict(
                False,
                f"the command escapes the repository ({escape}); keep every "
                "redirect, tee destination and cd inside it",
                risk="confinement",
            )
        if self._mode in {"plan", "ideate"} and _writes_anything(command):
            return _GateVerdict(
                False,
                f"the session is in {self._mode} mode, which does not mutate "
                "the repository",
                risk="observation",
            )
        # The same invariants on the parsed argv, where command_guard sees
        # switches and subcommands precisely.
        argv = _parse_argv(command)
        if argv is not None:
            try:
                validate_developer_command_argv(argv)
            except DeveloperCommandBlocked as exc:
                return _GateVerdict(False, str(exc), risk="invariant")
        # Consequence: what happens if this actually runs.
        synthetic = CoreCommand(
            name="dev.command",
            arguments={"argv": list(argv) if argv is not None else [command]},
            session_id=self._session_id,
            origin=self._origin,
            deadline_seconds=120.0,
        )
        try:
            risk_action = action_for_command(
                synthetic, repository=self._repository
            )
            decision = self._decisions.decide(
                risk_action, user_intent=self._task, bypass_mode=self._bypass_mode
            )
        except Exception as exc:  # fail closed on any mapping failure
            return _GateVerdict(
                False, f"the command could not be risk-classified: {exc}", risk="error"
            )
        if decision.hard_blocked:
            return _GateVerdict(
                False,
                "this action crosses a hard safety boundary: "
                + "; ".join(decision.reasons),
                risk=decision.consequence.value,
            )
        if decision.requires_confirmation:
            return _GateVerdict(
                False,
                "this action needs an exact one-shot user approval, which an "
                "autonomous engine run cannot grant: "
                + "; ".join(decision.reasons),
                risk=decision.consequence.value,
            )
        return _GateVerdict(True, risk=decision.consequence.value)

    # -- execution -----------------------------------------------------------

    def _run_guarded(self, command: str, timeout: int) -> Dict[str, Any]:
        try:
            result = _run_shell(command, str(self._repository), os.environ, timeout)
            return {
                "output": result.stdout,
                "returncode": result.returncode,
                "exception_info": "",
            }
        except subprocess.TimeoutExpired as exc:
            output = exc.output
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            return {
                "output": output or "",
                "returncode": -1,
                "exception_info": f"command timed out after {timeout}s",
            }
        except Exception as exc:
            return {
                "output": "",
                "returncode": -1,
                "exception_info": f"An error occurred while executing the command: {exc}",
            }

    def _emit(self, event: AgentEvent) -> None:
        observer = self._on_event
        if observer is None:
            return
        with contextlib.suppress(Exception):
            observer(event)


def _run_shell(command: str, cwd: str, env: Mapping[str, str], timeout: int):
    """The engine's own subprocess semantics, unchanged.

    Same shell, same merged stderr, same process-group kill on timeout; the
    measured engine behavior is preserved and only the gating is new.
    """

    process = subprocess.Popen(
        command,
        shell=True,
        text=True,
        cwd=cwd,
        env=dict(env),
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=os.name == "posix",
    )
    try:
        stdout, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            import signal

            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        stdout, _ = process.communicate()
        raise subprocess.TimeoutExpired(command, timeout, output=stdout)
    return subprocess.CompletedProcess(command, process.returncode, stdout=stdout)


def _parse_argv(command: str) -> Optional[List[str]]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return None


def _strip_token_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'}:
        return token[1:-1]
    return token


def _confinement_escape(command: str, repository: Path) -> Optional[str]:
    """The first redirect/tee/cd target that resolves outside the repository.

    Best-effort by design: a shell line can hide writes in inline code, and
    that opacity is exactly what the consequence engine labels opaque and
    the capability policy bounds. What this check removes is the *cheap*
    escape -- one ``>`` or ``cd`` token naming an outside location -- which
    a repository-scoped agent has no legitimate reason to use.
    """

    targets: List[Tuple[str, str]] = []
    targets.extend(
        ("redirect", _strip_token_quotes(match.group(1)))
        for match in _REDIRECT_TARGET.finditer(command)
    )
    targets.extend(
        ("tee", _strip_token_quotes(match.group(1)))
        for match in _TEE_TARGET.finditer(command)
    )
    targets.extend(
        ("cd", _strip_token_quotes(match.group(1)))
        for match in _CD_TARGET.finditer(command)
    )
    for kind, raw in targets:
        if not raw or raw.startswith("&"):
            continue
        candidate = Path(raw)
        if not candidate.is_absolute() and not candidate.drive:
            candidate = repository / candidate
        try:
            resolved = candidate.resolve(strict=False)
            resolved.relative_to(repository)
        except (OSError, ValueError):
            return f"{kind} -> {raw}"
    return None


def _writes_anything(command: str) -> bool:
    """Whether a shell line writes files (plan/ideate refuse mutations)."""

    if any(marker in command for marker in (">>", ">")):
        return True
    if _TEE_TARGET.search(command):
        return True
    argv = _parse_argv(command)
    if argv is None:
        # An unparseable line in a no-mutation mode is refused rather than
        # guessed at: fail closed is the mode's contract.
        return True
    executable = argv[0].replace("\\", "/").rsplit("/", 1)[-1].casefold()
    if executable.endswith((".exe", ".cmd", ".bat")):
        executable = executable.rsplit(".", 1)[0]
    if executable in {
        "rm", "rmdir", "del", "erase", "remove-item", "touch", "mkdir", "md",
        "mv", "move", "ren", "rename", "tee",
    }:
        return True
    if executable == "git" and len(argv) > 1:
        return argv[1].casefold() in {
            "add", "apply", "checkout", "cherry-pick", "clean", "commit",
            "config", "init", "merge", "mv", "rebase", "reset", "restore",
            "revert", "rm", "stash", "switch", "tag",
        }
    if executable == "sed" and any(flag.startswith("-") and "i" in flag for flag in argv[1:]):
        return True
    if executable in {"pip", "pip3", "npm", "pnpm", "yarn", "uv"} and any(
        token.casefold() in {"install", "add", "remove", "uninstall", "publish"}
        for token in argv[1:]
    ):
        return True
    return False


@dataclass(frozen=True)
class EngineAgentReport(AgentReport):
    """The agent report plus the identity of the loop that produced it.

    ``engine`` is the proof the report was executed by the vendored
    karo-core loop rather than the legacy v5 kernel: module, version and
    the trajectory the engine itself wrote.
    """

    engine: Optional[Dict[str, Any]] = None


class EngineKernel:
    """Runs one native-agent session through the vendored karo-core engine."""

    def __init__(
        self,
        *,
        provider: Any,
        model: str,
        core: Any,
        sessions: SessionStore,
        origin: Origin,
        limits: AgentLimits = AgentLimits(),
        policy: Optional[CapabilityPolicy] = None,
        action_decisions: Optional[Any] = None,
        project_context: Optional[Mapping[str, Any]] = None,
        prompt_suffix: str = "",
        verification_commands: Sequence[Sequence[str]] = (),
        require_change: bool = False,
        max_output_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        mode: Optional[str] = None,
        bypass_mode: bool = False,
        context_window: Optional[int] = None,
        context_token_budget: int = 160_000,
        command_timeout: int = 300,
        output_char_limit: int = 50_000,
        in_band_commands: Optional[
            Callable[[str, int], Optional[Dict[str, Any]]]
        ] = None,
        on_event: Optional[AgentObserver] = None,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("agent model must be a non-empty string")
        self.provider = provider
        self.model = model
        self.core = core
        self.sessions = sessions
        self.origin = origin
        self.limits = limits
        self.policy = policy if policy is not None else getattr(core, "policy", None)
        if self.policy is None:
            raise ValueError("the engine kernel requires a capability policy")
        self.action_decisions = action_decisions
        self.project_context: Dict[str, Any] = dict(project_context or {})
        self.prompt_suffix = prompt_suffix
        self.verification_commands = tuple(tuple(item) for item in verification_commands)
        self.require_change = bool(require_change)
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.mode = None if mode is None else normalize_mode(mode)
        self.bypass_mode = bool(bypass_mode)
        self.context_window = context_window
        self.context_token_budget = context_token_budget
        self.command_timeout = command_timeout
        self.output_char_limit = output_char_limit
        self.in_band_commands = in_band_commands
        self._on_event = on_event
        self.usage = EngineUsage()
        self._engine_config = yaml.safe_load(
            (package_dir / "config" / "default.yaml").read_text(encoding="utf-8")
        )
        if not isinstance(self._engine_config, dict):
            raise ValueError("the engine's packaged default config is malformed")

    # -- public API -----------------------------------------------------------

    def run(self, session_id: str) -> AgentReport:
        lease_ttl = min(3600.0, max(5.0, float(self.limits.max_seconds) + 5.0))
        lease = self.sessions.acquire(
            session_id,
            f"engine-agent:{self.origin.identity}",
            ttl_seconds=lease_ttl,
        )
        try:
            record = self.sessions.load(session_id)
            self.sessions.validate_repository(record, self.core.repository)
            if record.revoked:
                raise SessionError("session access has been revoked")
            if record.status == "verified" and record.checks:
                return self._report(
                    record,
                    reason="already_verified",
                    provider_message=self._last_assistant_text(record),
                    engine_steps=0,
                )
            return self._execute(session_id, record, lease)
        finally:
            self.sessions.release(lease)

    # -- engine execution -------------------------------------------------

    def _execute(
        self, session_id: str, record: SessionRecord, lease: MutationLease
    ) -> AgentReport:
        self._set_phase(session_id, lease, "execution")
        environment = GuardedEngineEnvironment(
            policy=self.policy,
            origin=self.origin,
            session_id=session_id,
            repository=self.core.repository,
            task=record.task,
            decisions=self._decisions(),
            bypass_mode=self.bypass_mode,
            mode=self.mode,
            on_event=self._on_event,
            command_timeout=self.command_timeout,
            output_char_limit=self.output_char_limit,
            in_band_commands=self.in_band_commands,
        )
        engine_model = ProviderEngineModel(
            self.provider,
            self.model,
            format_error_template=str(self._model_config().get("format_error_template") or "{{ error }}"),
            observation_template=str(
                self._model_config().get("observation_template")
                or "<returncode>{{output.returncode}}</returncode>\n<output>\n{{output.output}}</output>"
            ),
            usage=self.usage,
            max_output_tokens=self.max_output_tokens,
            reasoning_effort=self.reasoning_effort,
            on_event=self._on_event,
        )
        agent = _EventAgent(
            engine_model,
            environment,
            on_event=self._on_event,
            step_limit=int(self.limits.max_steps),
            wall_time_limit_seconds=int(self.limits.max_seconds),
            cost_limit=0.0,
            context_token_budget=self.context_token_budget,
            output_path=self._trajectory_path(session_id),
            system_template=self._system_template(),
            instance_template=self._instance_template(),
        )
        status = "stopped"
        reason = "stopped"
        exit_extra: Dict[str, Any] = {}
        try:
            exit_extra = agent.run(task=record.task) or {}
            reason = _REASON_BY_EXIT.get(
                str(exit_extra.get("exit_status") or ""), "stopped"
            )
            if reason != "stopped":
                status = "submitted"
        except Submitted as exc:
            reason = "submitted"
            status = "submitted"
            exit_extra = dict(exc.messages[0].get("extra") or {}) if exc.messages else {}
        except TimeExceeded:
            reason = "wall_time_limit"
        except LimitsExceeded:
            reason = "step_limit"
        except Exception as exc:  # the CLI reports, never crashes, on engine errors
            reason = f"engine_error: {type(exc).__name__}"
            self._record_failure(session_id, lease, reason, str(exc))
        engine_steps = int(agent.n_calls)
        submission = str(
            (exit_extra.get("submission")
             or (agent.messages[-1].get("content") if agent.messages else "")
             or "")
        )
        self._persist_engine_history(session_id, lease, agent, environment)
        checks_passed, changed_files, git_state = self._collect_evidence(
            session_id, lease
        )
        verified = self._verified(checks_passed, changed_files)
        final_status = "verified" if verified else "stopped"
        final_phase = "verification" if verified else "execution"
        if not verified:
            reason = self._unverified_reason(checks_passed, changed_files, reason)
        summary = (
            f"Engine agent submitted: {reason}."
            if status == "submitted"
            else f"Engine agent stopped: {reason}."
        )
        saved = self._update_session(
            session_id,
            lease,
            lambda current: self._apply_final_state(
                current,
                final_status,
                final_phase,
                summary,
                changed_files,
                git_state,
            ),
        )
        self._emit(
            AgentEvent(
                AgentEventKind.FINISHED,
                step=engine_steps,
                reason=reason,
                status=final_status,
                ok=verified,
                usage=self.usage.to_dict(),
            )
        )
        return self._report(
            saved,
            reason=reason,
            provider_message=submission or None,
            engine_steps=engine_steps,
            changed_files=changed_files,
            git_state=git_state,
            trajectory=self._trajectory_path(session_id),
        )

    # -- finishing evidence -------------------------------------------------

    def _collect_evidence(
        self, session_id: str, lease: MutationLease
    ) -> Tuple[bool, List[str], Dict[str, Any]]:
        """Run the approved checks and read the resulting git state.

        The engine can only submit a workspace; the harness proves it. Each
        approved verification command runs through the Core (allowlist,
        timeouts, checker identity challenge, evidence records) and the git
        state is read through the Core's own git readers.
        """

        checks_passed = True
        ran_any = False
        for index, argv in enumerate(self.verification_commands):
            command = CoreCommand(
                name="checks.run",
                arguments={
                    "argv": list(argv),
                    "timeout_seconds": min(600.0, float(self.limits.max_seconds)),
                },
                session_id=session_id,
                origin=self.origin,
                idempotency_key=f"engine-check-{index}-{_argv_digest(argv)}",
                deadline_seconds=min(660.0, float(self.limits.max_seconds) + 60.0),
                user_intent=self._task_of(session_id),
            )
            try:
                result = self.core.execute(command, lease=lease)
            except Exception as exc:
                checks_passed = False
                ran_any = True
                self._record_failure(
                    session_id, lease, "check_failed", f"{type(exc).__name__}: {exc}"
                )
                continue
            ran_any = True
            if not result.ok:
                checks_passed = False
        if self.verification_commands and not ran_any:
            checks_passed = False
        changed_files, git_state = self._git_state(session_id)
        if not self.verification_commands:
            checks_passed = False
        return checks_passed, changed_files, git_state

    def _git_state(self, session_id: str) -> Tuple[List[str], Dict[str, Any]]:
        status = self._core_read("git.status", {}, session_id)
        diff = self._core_read("git.diff", {}, session_id)
        changed: List[str] = []
        git_state: Dict[str, Any] = {}
        if status is not None and status.ok:
            data = status.data if isinstance(status.data, dict) else {}
            git_state["status"] = {
                key: value for key, value in data.items() if key != "output"
            }
            for key in ("changed_files", "files", "modified", "untracked"):
                value = data.get(key)
                if isinstance(value, list):
                    changed.extend(str(item) for item in value if str(item))
        if diff is not None and diff.ok:
            data = diff.data if isinstance(diff.data, dict) else {}
            git_state["diff"] = {
                key: value
                for key, value in data.items()
                if key not in {"output", "patch"}
            }
            patch = data.get("patch") or data.get("output")
            if isinstance(patch, str) and patch.strip():
                git_state["diff"]["present"] = True
        return list(dict.fromkeys(changed)), git_state

    def _core_read(self, name: str, arguments: Dict[str, Any], session_id: str):
        command = CoreCommand(
            name=name,
            arguments=arguments,
            session_id=session_id,
            origin=self.origin,
            deadline_seconds=60.0,
        )
        try:
            return self.core.execute(command)
        except Exception:
            return None

    def _verified(self, checks_passed: bool, changed_files: Sequence[str]) -> bool:
        if not checks_passed:
            return False
        if self.require_change and not changed_files:
            return False
        return True

    def _unverified_reason(
        self, checks_passed: bool, changed_files: Sequence[str], fallback: str
    ) -> str:
        if not self.verification_commands:
            return "no_approved_check"
        if not checks_passed:
            return "check_failed"
        if self.require_change and not changed_files:
            return "no_change"
        return fallback

    # -- prompt construction -------------------------------------------------

    def _system_template(self) -> str:
        template = str(
            (self._engine_config.get("agent") or {}).get("system_template") or ""
        )
        if self.mode and self.mode != "build":
            from .agent_modes import mode_prompt_delta

            template = template + "\n\n" + mode_prompt_delta(self.mode)
        if self.prompt_suffix:
            template = template + "\n\n" + self.prompt_suffix
        return template

    def _instance_template(self) -> str:
        template = str(
            (self._engine_config.get("agent") or {}).get("instance_template") or ""
        )
        return template

    def _model_config(self) -> Mapping[str, Any]:
        model = self._engine_config.get("model")
        return model if isinstance(model, Mapping) else {}

    def _trajectory_path(self, session_id: str) -> Path:
        return self.sessions.session_dir(session_id) / "engine" / "trajectory.json"

    def _decisions(self) -> Any:
        if self.action_decisions is not None:
            return self.action_decisions
        decisions = getattr(self.core, "_action_decisions", None)
        if decisions is not None:
            return decisions
        from .action_policy import ActionDecisionEngine

        return ActionDecisionEngine()

    # -- session bookkeeping -------------------------------------------------

    def _set_phase(self, session_id: str, lease: MutationLease, phase: str) -> None:
        def update(record: SessionRecord) -> None:
            record.phase = phase
            record.status = "active"

        self._update_session(session_id, lease, update)
        self._emit(AgentEvent(AgentEventKind.PHASE_CHANGED, step=0, phase=phase))

    def _persist_engine_history(
        self,
        session_id: str,
        lease: MutationLease,
        agent: DefaultAgent,
        environment: GuardedEngineEnvironment,
    ) -> None:
        """Persist the engine transcript into the checksummed session record.

        The engine keeps its own trajectory file; the session store is what
        the product reads, so the assistant turns and observations are
        appended in the store's shape, redacted, once at the end of the run
        so a long run does not pay a session revision per step.
        """

        entries: List[Dict[str, Any]] = []
        for message in agent.messages:
            role = str(message.get("role") or "")
            if role not in {"assistant", "user", "tool"}:
                continue
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            entry: Dict[str, Any] = {
                "role": role,
                "content": redact(content),
                "kind": "engine_turn",
            }
            if role == "assistant":
                extra = message.get("extra")
                if isinstance(extra, dict) and isinstance(extra.get("cost"), (int, float)):
                    entry["cost"] = float(extra["cost"])
            entries.append(entry)
        refusals = [
            {"role": "provider_audit", "kind": "engine_refusal", **item}
            for item in environment.refusals
        ]

        def update(record: SessionRecord) -> None:
            record.provider_history.extend(entries)
            record.provider_history.extend(refusals)
            record.usage.update(self.usage.to_dict())

        self._update_session(session_id, lease, update)

    def _apply_final_state(
        self,
        record: SessionRecord,
        status: str,
        phase: str,
        summary: str,
        changed_files: Sequence[str],
        git_state: Mapping[str, Any],
    ) -> None:
        record.status = status
        record.phase = phase
        record.summary = summary
        for path in changed_files:
            if path not in record.changed_files:
                record.changed_files.append(path)
        if git_state:
            record.git_state.update(dict(git_state))
        record.usage.update(self.usage.to_dict())

    def _record_failure(
        self, session_id: str, lease: MutationLease, kind: str, message: str
    ) -> None:
        def update(record: SessionRecord) -> None:
            record.failures.append(
                {"kind": kind, "message": redact(str(message)), "engine": engine_version}
            )

        self._update_session(session_id, lease, update)

    def _update_session(
        self,
        session_id: str,
        lease: MutationLease,
        update: Callable[[SessionRecord], None],
    ) -> SessionRecord:
        record = self.sessions.load(session_id)
        update(record)
        return self.sessions.save(record, record.revision, lease)

    def _task_of(self, session_id: str) -> str:
        try:
            return str(self.sessions.load(session_id).task)
        except Exception:
            return ""

    # -- reporting -----------------------------------------------------------

    def _report(
        self,
        record: SessionRecord,
        *,
        reason: str,
        provider_message: Optional[str],
        engine_steps: int,
        changed_files: Optional[Sequence[str]] = None,
        git_state: Optional[Mapping[str, Any]] = None,
        trajectory: Optional[Path] = None,
    ) -> EngineAgentReport:
        engine_identity: Dict[str, Any] = {
            "name": "karo-core",
            "version": engine_version,
            "module": "karox.engine",
            "agent": "karox.engine.agents.default.DefaultAgent",
            "steps": engine_steps,
        }
        if trajectory is not None:
            engine_identity["trajectory"] = str(trajectory)
        project_context = dict(self.project_context)
        project_context["engine"] = dict(engine_identity)
        return EngineAgentReport(
            session_id=record.session_id,
            status=record.status,
            phase=record.phase,
            verified=record.status == "verified",
            reason=reason,
            steps=engine_steps,
            changed_files=tuple(changed_files if changed_files is not None else record.changed_files),
            checks=tuple(record.checks),
            git_state=dict(git_state if git_state is not None else record.git_state),
            evidence=tuple(record.evidence),
            usage=dict(record.usage),
            provider_message=provider_message,
            project_context=project_context,
            mode=self.mode,
            engine=engine_identity,
        )

    @staticmethod
    def _last_assistant_text(record: SessionRecord) -> Optional[str]:
        for entry in reversed(record.provider_history):
            if entry.get("role") == "assistant" and isinstance(
                entry.get("content"), str
            ):
                return str(redact(entry["content"]))
        return None

    def _emit(self, event: AgentEvent) -> None:
        if self._on_event is None:
            return
        with contextlib.suppress(Exception):
            self._on_event(event)


def _argv_digest(argv: Sequence[str]) -> str:
    blob = _json_dumps_compact(list(argv))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


__all__ = [
    "EngineAgentReport",
    "EngineKernel",
    "EngineUsage",
    "GuardedEngineEnvironment",
    "ProviderEngineModel",
    "engine_version",
]
