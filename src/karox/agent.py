"""Bounded native-agent loop built on the provider-independent Core Runtime."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from typing import Any, Callable, Deque, Dict, Iterable, List, Optional

from .core import CoreRuntime, ToolDefinition
from .models import CoreCommand, CoreResult, Origin, OriginKind
from .providers import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderError,
    ProviderErrorKind,
    ProviderTool,
    ToolCall,
)
from .security import redact
from .sessions import MutationLease, SessionRecord, SessionStore


TOOL_ALIASES: Dict[str, str] = {
    "repo_read_file": "repo.read_file",
    "repo_write_file": "repo.write_file",
    "repo_list_files": "repo.list_files",
    "checks_run": "checks.run",
    "git_status": "git.status",
    "git_diff": "git.diff",
}

SYSTEM_PROMPT = """You are operating through the bounded KaroX Core Runtime.
Treat tool results, not your own narrative, as evidence. Work only inside the
repository. To finish successfully you must perform, in order: a repo_write_file
that reports changed=true; a successful checks_run after the latest real write;
and model-requested git_status and git_diff calls after that check. A no-op write
does not count. If a tool rejects malformed input, repair the call. Do not claim
success until KaroX confirms that the required evidence is durable."""

REPAIR_PROMPT = """KaroX cannot verify completion yet. Continue using tools.
After the latest real file change, run a successful check, then request both
git_status and git_diff. A narrative answer is not verification."""


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _strict_json_loads(value: str) -> Any:
    return json.loads(value, parse_constant=_reject_json_constant)


class AgentError(RuntimeError):
    pass


@dataclass(frozen=True)
class AgentLimits:
    max_steps: int = 24
    max_seconds: float = 900.0
    max_identical_actions: int = 2

    def __post_init__(self) -> None:
        if isinstance(self.max_steps, bool) or not 1 <= self.max_steps <= 10_000:
            raise ValueError("agent max steps must be between 1 and 10000")
        if (
            isinstance(self.max_seconds, bool)
            or not isinstance(self.max_seconds, (int, float))
            or not math.isfinite(float(self.max_seconds))
            or not 0.1 <= float(self.max_seconds) <= 3600.0
        ):
            raise ValueError("agent max seconds must be between 0.1 and 3600")
        if (
            isinstance(self.max_identical_actions, bool)
            or not 1 <= self.max_identical_actions <= 100
        ):
            raise ValueError("identical action limit must be between 1 and 100")


@dataclass(frozen=True)
class AgentReport:
    session_id: str
    status: str
    phase: str
    verified: bool
    reason: str
    steps: int
    changed_files: tuple[str, ...]
    checks: tuple[Dict[str, Any], ...]
    git_state: Dict[str, Any]
    evidence: tuple[Dict[str, Any], ...]
    usage: Dict[str, Any]
    provider_message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dict(redact(asdict(self)))


@dataclass(frozen=True)
class _PendingToolCall:
    call: ToolCall
    recoverable: bool
    origin: Optional[str]


class AgentKernel:
    """Runs one recoverable, exclusively leased native-agent session."""

    def __init__(
        self,
        *,
        provider: Provider,
        model: str,
        core: CoreRuntime,
        sessions: SessionStore,
        origin: Origin,
        limits: AgentLimits = AgentLimits(),
        system_prompt: str = SYSTEM_PROMPT,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(model, str) or not model.strip():
            raise ValueError("agent model must be a non-empty string")
        if not isinstance(system_prompt, str) or not system_prompt.strip():
            raise ValueError("agent system prompt must be a non-empty string")
        self.provider = provider
        self.model = model
        self.core = core
        self.sessions = sessions
        self.origin = origin
        self.limits = limits
        self.system_prompt = system_prompt
        self.monotonic = monotonic
        definitions = {item.name: item for item in core.tools()}
        missing = set(TOOL_ALIASES.values()).difference(definitions)
        if missing:
            raise AgentError(f"Core is missing required tools: {sorted(missing)}")
        self._definitions = definitions
        aliases = dict(TOOL_ALIASES)
        for core_name in sorted(definitions):
            if not core_name.startswith("mcp."):
                continue
            alias = re.sub(r"[^A-Za-z0-9_]", "_", core_name)
            current = aliases.get(alias)
            if current is not None and current != core_name:
                raise AgentError(
                    f"provider tool alias collision: {alias} maps to {current} and {core_name}"
                )
            aliases[alias] = core_name
        if len(set(aliases.values())) != len(aliases):
            raise AgentError("multiple provider aliases map to the same Core tool")
        self._tool_aliases = aliases
        self._provider_tools = tuple(
            self._provider_tool(alias, definitions[core_name])
            for alias, core_name in aliases.items()
        )

    @staticmethod
    def _provider_tool(alias: str, definition: ToolDefinition) -> ProviderTool:
        return ProviderTool(alias, definition.description, definition.input_schema)

    def run(self, session_id: str) -> AgentReport:
        started = self.monotonic()
        deadline = started + float(self.limits.max_seconds)
        lease_ttl = min(3600.0, max(5.0, float(self.limits.max_seconds) + 5.0))
        lease = self.sessions.acquire(
            session_id,
            f"native-agent:{self.origin.identity}",
            ttl_seconds=lease_ttl,
        )
        provider_message: Optional[str] = None
        reason = "stopped"
        try:
            record = self.sessions.load(session_id)
            self.sessions.validate_repository(record, self.core.repository)
            if record.revoked:
                raise AgentError("session access has been revoked")
            if record.status == "verified" and self._verification(record):
                return self._report(
                    record,
                    reason="already_verified",
                    provider_message=self._last_provider_message(record),
                )
            self._initialize_history(record, lease)
            action_counts = self._completed_action_counts(
                self.sessions.load(session_id).provider_history
            )
            repeated = self._recover_pending(
                session_id, lease, deadline, action_counts
            )
            if repeated:
                return self._finish(
                    session_id, lease, "stopped", "execution", "repeated_action"
                )

            while True:
                record = self.sessions.load(session_id)
                steps = self._step_count(record.provider_history)
                if steps >= self.limits.max_steps:
                    reason = "step_limit"
                    return self._finish(
                        session_id, lease, "stopped", record.phase, reason
                    )
                remaining = deadline - self.monotonic()
                if remaining < 0.1:
                    reason = "wall_time_limit"
                    return self._finish(
                        session_id, lease, "stopped", record.phase, reason
                    )
                self.sessions.heartbeat(lease, ttl_seconds=lease_ttl)
                request = ModelRequest(
                    model=self.model,
                    messages=tuple(self._request_messages(record.provider_history)),
                    tools=self._provider_tools,
                    deadline_seconds=min(remaining, 3600.0),
                )
                try:
                    response = self.provider.complete(request)
                except ProviderError as exc:
                    self._record_provider_failure(session_id, lease, exc, steps + 1)
                    if exc.kind is ProviderErrorKind.BUDGET_EXCEEDED:
                        return self._finish(
                            session_id,
                            lease,
                            "stopped",
                            "budget",
                            "budget_exceeded",
                        )
                    return self._finish(
                        session_id,
                        lease,
                        "failed",
                        "provider",
                        f"provider_error:{exc.kind.value}",
                    )
                if self.monotonic() >= deadline:
                    return self._finish(
                        session_id,
                        lease,
                        "stopped",
                        record.phase,
                        "wall_time_limit",
                    )
                self._persist_assistant(session_id, lease, response)
                provider_message = (
                    str(redact(response.content)) if response.content is not None else None
                )
                if response.budget_exceeded:
                    for call in response.tool_calls:
                        self._persist_tool_error(
                            session_id,
                            lease,
                            call,
                            "budget_exceeded",
                            "tool execution was blocked after the provider response "
                            "crossed a configured budget",
                        )
                    suffix = response.budget_reason or "budget"
                    return self._finish(
                        session_id,
                        lease,
                        "stopped",
                        "budget",
                        f"budget_exceeded:{suffix}",
                        provider_message,
                    )
                repeated = False
                for call in response.tool_calls:
                    if self.monotonic() >= deadline:
                        return self._finish(
                            session_id,
                            lease,
                            "stopped",
                            "execution",
                            "wall_time_limit",
                            provider_message,
                        )
                    repeated = self._execute_call(
                        session_id, lease, call, deadline, action_counts
                    ) or repeated
                    self.sessions.heartbeat(lease, ttl_seconds=lease_ttl)
                if repeated:
                    return self._finish(
                        session_id,
                        lease,
                        "stopped",
                        "execution",
                        "repeated_action",
                        provider_message,
                    )
                if response.tool_calls:
                    self._set_phase(session_id, lease, "verification")
                    continue
                record = self.sessions.load(session_id)
                if self._verification(record):
                    return self._finish(
                        session_id,
                        lease,
                        "verified",
                        "completed",
                        "verified",
                        provider_message,
                    )
                self._append_message(
                    session_id,
                    lease,
                    {"role": "user", "content": REPAIR_PROMPT},
                    phase="verification",
                )
        finally:
            self.sessions.release(lease)

    def _initialize_history(
        self, record: SessionRecord, lease: MutationLease
    ) -> None:
        if record.provider_history:
            self._set_phase(record.session_id, lease, "execution")
            return

        def update(current: SessionRecord) -> None:
            if not current.provider_history:
                current.provider_history.extend(
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": current.task},
                    ]
                )
            current.status = "active"
            current.phase = "execution"

        self._update_session(record.session_id, lease, update)

    def _persist_assistant(
        self, session_id: str, lease: MutationLease, response: ModelResponse
    ) -> None:
        tool_calls = []
        for item in response.tool_calls:
            safe_arguments, recoverable = self._safe_arguments(item.raw_arguments)
            tool_calls.append(
                {
                    "call_id": item.call_id,
                    "name": item.name,
                    "raw_arguments": safe_arguments,
                    "arguments_sha256": hashlib.sha256(
                        item.raw_arguments.encode("utf-8")
                    ).hexdigest(),
                    "recoverable": recoverable,
                }
            )
        entry = {
            "role": "assistant",
            "origin": self.origin.key,
            "content": redact(response.content) if response.content is not None else None,
            "tool_calls": tool_calls,
            "provider": self.provider.provider_name,
            "model": self.model,
            "finish_reason": response.finish_reason,
            "response_id": response.response_id,
            "transport_attempts": response.transport_attempts,
            "usage": dict(response.usage),
        }
        route_audit = self._route_audit(response)

        def update(record: SessionRecord) -> None:
            if route_audit is not None:
                record.provider_history.append(dict(redact(route_audit)))
            record.provider_history.append(dict(redact(entry)))
            aggregate = dict(record.usage)
            aggregate["requests"] = int(aggregate.get("requests", 0)) + 1
            aggregate["transport_attempts"] = int(
                aggregate.get("transport_attempts", 0)
            ) + response.transport_attempts
            for name, count in response.usage.items():
                aggregate[name] = int(aggregate.get(name, 0)) + count
            if response.currency is not None and response.cumulative_cost is not None:
                costs = aggregate.get("costs")
                if not isinstance(costs, dict):
                    costs = {}
                costs = dict(costs)
                costs[response.currency] = response.cumulative_cost
                aggregate["costs"] = costs
            record.usage = aggregate

        self._update_session(session_id, lease, update)

    @staticmethod
    def _route_audit(response: ModelResponse) -> Optional[Dict[str, Any]]:
        if not (
            response.route_attempts
            or response.selected_provider is not None
            or response.selected_model is not None
            or response.cost is not None
            or response.cumulative_usage
            or response.cumulative_cost is not None
            or response.budget_exceeded
        ):
            return None
        return {
            "role": "provider_audit",
            "kind": "route",
            "route_attempts": [dict(item) for item in response.route_attempts],
            "selected_provider": response.selected_provider,
            "selected_model": response.selected_model,
            "cost": response.cost,
            "currency": response.currency,
            "pricing_version": response.pricing_version,
            "cumulative_usage": dict(response.cumulative_usage),
            "cumulative_cost": response.cumulative_cost,
            "budget_exceeded": response.budget_exceeded,
            "budget_reason": response.budget_reason,
        }

    @staticmethod
    def _safe_arguments(raw: str) -> tuple[str, bool]:
        try:
            value = _strict_json_loads(raw)
        except (TypeError, ValueError, json.JSONDecodeError):
            safe = str(redact(raw))
            return safe, safe == raw
        safe_value = redact(value)
        return (
            json.dumps(safe_value, ensure_ascii=False, sort_keys=True),
            safe_value == value,
        )

    def _execute_call(
        self,
        session_id: str,
        lease: MutationLease,
        call: ToolCall,
        deadline: float,
        action_counts: Counter[str],
    ) -> bool:
        signature = self._action_signature(call)
        if action_counts[signature] >= self.limits.max_identical_actions:
            self._persist_tool_error(
                session_id,
                lease,
                call,
                "repeated_action",
                "identical action limit reached",
            )
            action_counts[signature] += 1
            return True
        action_counts[signature] += 1
        core_name = self._tool_aliases.get(call.name)
        if core_name is None:
            self._persist_tool_error(
                session_id, lease, call, "unknown_tool", "tool is not available"
            )
            return False
        try:
            arguments = _strict_json_loads(call.raw_arguments)
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._persist_tool_error(
                session_id, lease, call, "malformed_arguments", str(exc)
            )
            return False
        remaining = deadline - self.monotonic()
        if remaining < 0.1:
            self._persist_tool_error(
                session_id, lease, call, "wall_time_limit", "agent deadline expired"
            )
            return False
        definition = self._definitions[core_name]
        identity = f"{session_id}\0{call.call_id}\0{core_name}".encode("utf-8")
        digest = hashlib.sha256(identity).hexdigest()
        command = CoreCommand(
            name=core_name,
            arguments=arguments,
            session_id=session_id,
            origin=self.origin,
            correlation_id=f"agent-{digest[:32]}",
            idempotency_key=f"agent-{digest}" if definition.mutates else None,
            deadline_seconds=min(3600.0, max(0.1, remaining)),
        )
        try:
            result = self.core.execute(command, lease=lease if definition.mutates else None)
        except Exception as exc:
            self._persist_tool_error(
                session_id,
                lease,
                call,
                type(exc).__name__,
                str(redact(str(exc))),
                core_name=core_name,
            )
            return False
        self._persist_tool_result(session_id, lease, call, core_name, result)
        return False

    def _persist_tool_result(
        self,
        session_id: str,
        lease: MutationLease,
        call: ToolCall,
        core_name: str,
        result: CoreResult,
    ) -> None:
        result_value = result.to_dict()
        content = json.dumps(
            {"ok": result.ok, "command": core_name, "result": result_value},
            ensure_ascii=False,
            sort_keys=True,
        )
        entry = {
            "role": "tool",
            "origin": self.origin.key,
            "content": redact(content),
            "tool_call_id": call.call_id,
            "tool_name": call.name,
            "core_name": core_name,
            "result": redact(result_value),
        }

        def update(record: SessionRecord) -> None:
            record.provider_history.append(entry)
            if not result.mutation:
                known = {
                    item.get("evidence_id")
                    for item in record.evidence
                    if isinstance(item, dict)
                }
                for item in result.evidence:
                    if item.evidence_id not in known:
                        record.evidence.append(item.to_dict())
            if core_name in {"git.status", "git.diff"}:
                key = "status" if core_name == "git.status" else "diff"
                record.git_state[key] = {
                    "correlation_id": result.correlation_id,
                    "ok": result.ok,
                    "exit_code": result.data.get("exit_code"),
                    "timed_out": result.data.get("timed_out", False),
                    "sha256": result.data.get("sha256"),
                    "stdout": result.data.get("stdout", ""),
                }

        self._update_session(session_id, lease, update)

    def _persist_tool_error(
        self,
        session_id: str,
        lease: MutationLease,
        call: ToolCall,
        error_type: str,
        message: str,
        *,
        core_name: Optional[str] = None,
    ) -> None:
        payload = {
            "ok": False,
            "error": {
                "type": error_type,
                "message": str(redact(message))[:4000],
            },
        }
        self._append_message(
            session_id,
            lease,
            {
                "role": "tool",
                "origin": self.origin.key,
                "content": json.dumps(payload, ensure_ascii=False, sort_keys=True),
                "tool_call_id": call.call_id,
                "tool_name": call.name,
                "core_name": core_name,
                "error": payload["error"],
            },
        )

    def _recover_pending(
        self,
        session_id: str,
        lease: MutationLease,
        deadline: float,
        action_counts: Counter[str],
    ) -> bool:
        history = self.sessions.load(session_id).provider_history
        repeated = False
        for pending in self._pending_calls(history):
            call = pending.call
            if (
                pending.origin is not None
                and pending.origin != self.origin.key
            ) or (
                pending.origin is None
                and self.origin.kind is not OriginKind.NATIVE_AGENT
            ):
                self._persist_tool_error(
                    session_id,
                    lease,
                    call,
                    "origin_mismatch",
                    "persisted tool call belongs to a different or unknown origin; "
                    "refusing replay",
                )
                continue
            if not pending.recoverable:
                self._persist_tool_error(
                    session_id,
                    lease,
                    call,
                    "unrecoverable_arguments",
                    "persisted arguments were redacted; refusing replay",
                )
                continue
            repeated = self._execute_call(
                session_id, lease, call, deadline, action_counts
            ) or repeated
        return repeated

    @staticmethod
    def _pending_calls(history: List[Dict[str, Any]]) -> List[_PendingToolCall]:
        pending: List[Optional[_PendingToolCall]] = []
        pending_by_id: Dict[str, Deque[int]] = defaultdict(deque)
        for entry in history:
            if entry.get("role") == "assistant":
                raw_origin = entry.get("origin")
                origin = (
                    raw_origin
                    if isinstance(raw_origin, str) and raw_origin.strip()
                    else None
                )
                for raw in entry.get("tool_calls", []):
                    call_id = str(raw.get("call_id", ""))
                    pending.append(
                        _PendingToolCall(
                            call=ToolCall(
                                call_id=call_id,
                                name=str(raw.get("name", "")),
                                raw_arguments=str(raw.get("raw_arguments", "")),
                            ),
                            # Older records did not persist enough information to
                            # prove that redaction left the executable input intact.
                            recoverable=raw.get("recoverable") is True,
                            origin=origin,
                        ),
                    )
                    pending_by_id[call_id].append(len(pending) - 1)
            elif entry.get("role") == "tool":
                call_id = str(entry.get("tool_call_id", ""))
                queued = pending_by_id[call_id]
                if queued:
                    pending[queued.popleft()] = None
        return [item for item in pending if item is not None]

    @classmethod
    def _completed_action_counts(
        cls, history: List[Dict[str, Any]]
    ) -> Counter[str]:
        counts: Counter[str] = Counter()
        pending_by_id: Dict[str, List[ToolCall]] = {}
        for entry in history:
            if entry.get("role") == "assistant":
                for raw in entry.get("tool_calls", []):
                    call = ToolCall(
                        call_id=str(raw.get("call_id", "")),
                        name=str(raw.get("name", "")),
                        raw_arguments=str(raw.get("raw_arguments", "")),
                    )
                    pending_by_id.setdefault(call.call_id, []).append(call)
            elif entry.get("role") == "tool":
                call_id = str(entry.get("tool_call_id", ""))
                queued = pending_by_id.get(call_id, [])
                if queued:
                    counts[cls._action_signature(queued.pop(0))] += 1
        return counts

    @staticmethod
    def _action_signature(call: ToolCall) -> str:
        try:
            value = _strict_json_loads(call.raw_arguments)
            normalized = json.dumps(
                value, allow_nan=False, ensure_ascii=False, sort_keys=True
            )
        except (TypeError, ValueError, json.JSONDecodeError):
            normalized = call.raw_arguments
        return hashlib.sha256(f"{call.name}\0{normalized}".encode("utf-8")).hexdigest()

    @staticmethod
    def _messages(history: Iterable[Dict[str, Any]]) -> Iterable[ModelMessage]:
        entries = list(history)
        tool_entries: Dict[str, Deque[tuple[int, Dict[str, Any]]]] = defaultdict(deque)
        for index, entry in enumerate(entries):
            if entry.get("role") == "tool":
                tool_entries[str(entry.get("tool_call_id", ""))].append(
                    (index, entry)
                )
        consumed: set[int] = set()
        for index, entry in enumerate(entries):
            role = entry.get("role")
            if role == "tool":
                continue
            if role not in {"system", "user", "assistant"}:
                continue
            calls = tuple(
                ToolCall(
                    call_id=str(item.get("call_id", "")),
                    name=str(item.get("name", "")),
                    raw_arguments=str(item.get("raw_arguments", "")),
                )
                for item in entry.get("tool_calls", [])
            )
            yield ModelMessage(
                role=str(role or ""),
                content=entry.get("content"),
                tool_calls=calls,
                tool_call_id=entry.get("tool_call_id"),
            )
            if role != "assistant":
                continue
            for call in calls:
                queue = tool_entries[call.call_id]
                while queue and (queue[0][0] <= index or queue[0][0] in consumed):
                    queue.popleft()
                if not queue:
                    continue
                tool_index, tool_entry = queue.popleft()
                consumed.add(tool_index)
                yield ModelMessage(
                    role="tool",
                    content=tool_entry.get("content"),
                    tool_call_id=call.call_id,
                )

    def _request_messages(
        self, history: Iterable[Dict[str, Any]]
    ) -> Iterable[ModelMessage]:
        replaced = False
        for message in self._messages(history):
            if not replaced and message.role == "system":
                replaced = True
                yield ModelMessage("system", self.system_prompt)
            else:
                yield message

    @staticmethod
    def _step_count(history: List[Dict[str, Any]]) -> int:
        return sum(
            1
            for entry in history
            if entry.get("role") == "assistant" and entry.get("provider")
        )

    @staticmethod
    def _verification(record: SessionRecord) -> bool:
        write: Optional[Dict[str, Any]] = None
        check: Optional[Dict[str, Any]] = None
        status: Optional[Dict[str, Any]] = None
        diff: Optional[Dict[str, Any]] = None
        for entry in record.provider_history:
            if entry.get("role") != "tool" or not isinstance(entry.get("result"), dict):
                continue
            result = entry["result"]
            if result.get("ok") is not True:
                continue
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            core_name = entry.get("core_name")
            if core_name == "repo.write_file" and data.get("changed") is True:
                write, check, status, diff = result, None, None, None
            elif core_name == "checks.run" and write is not None:
                check, status, diff = result, None, None
            elif core_name == "git.status" and check is not None:
                status = result
            elif core_name == "git.diff" and check is not None:
                diff = result
        selected = (write, check, status, diff)
        if any(item is None for item in selected):
            return False
        persisted_ids = {
            item.get("evidence_id")
            for item in record.evidence
            if isinstance(item, dict)
        }
        for result in selected:
            assert result is not None
            evidence = result.get("evidence")
            if not isinstance(evidence, list) or not evidence:
                return False
            if any(item.get("evidence_id") not in persisted_ids for item in evidence):
                return False
        return True

    def _record_provider_failure(
        self,
        session_id: str,
        lease: MutationLease,
        error: ProviderError,
        step: int,
    ) -> None:
        route_audit: Optional[Dict[str, Any]] = None
        if error.route_attempts:
            route_audit = {
                "role": "provider_audit",
                "kind": "route_failure",
                "route_attempts": [dict(item) for item in error.route_attempts],
                "error_kind": error.kind.value,
                "step": step,
            }

        def update(record: SessionRecord) -> None:
            if route_audit is not None:
                record.provider_history.append(dict(redact(route_audit)))
            record.failures.append(
                {
                    "kind": "provider",
                    "error_kind": error.kind.value,
                    "message": error.safe_message,
                    "status_code": error.status_code,
                    "step": step,
                }
            )

        self._update_session(session_id, lease, update)

    def _append_message(
        self,
        session_id: str,
        lease: MutationLease,
        entry: Dict[str, Any],
        *,
        phase: Optional[str] = None,
    ) -> None:
        def update(record: SessionRecord) -> None:
            record.provider_history.append(dict(redact(entry)))
            if phase is not None:
                record.phase = phase

        self._update_session(session_id, lease, update)

    def _set_phase(
        self, session_id: str, lease: MutationLease, phase: str
    ) -> None:
        def update(record: SessionRecord) -> None:
            record.phase = phase
            record.status = "active"

        self._update_session(session_id, lease, update)

    def _finish(
        self,
        session_id: str,
        lease: MutationLease,
        status: str,
        phase: str,
        reason: str,
        provider_message: Optional[str] = None,
    ) -> AgentReport:
        def update(record: SessionRecord) -> None:
            record.status = status
            record.phase = phase
            if status == "verified":
                record.summary = "Native agent completed with required local evidence."
            else:
                record.summary = f"Native agent stopped: {reason}."

        record = self._update_session(session_id, lease, update)
        verified = status == "verified" and self._verification(record)
        if status == "verified" and not verified:
            raise AgentError("session verification changed before final report")
        return self._report(
            record,
            reason=reason,
            provider_message=provider_message,
        )

    def _report(
        self,
        record: SessionRecord,
        *,
        reason: str,
        provider_message: Optional[str] = None,
    ) -> AgentReport:
        return AgentReport(
            session_id=record.session_id,
            status=record.status,
            phase=record.phase,
            verified=record.status == "verified" and self._verification(record),
            reason=reason,
            steps=self._step_count(record.provider_history),
            changed_files=tuple(record.changed_files),
            checks=tuple(record.checks),
            git_state=dict(record.git_state),
            evidence=tuple(record.evidence),
            usage=dict(record.usage),
            provider_message=provider_message,
        )

    @staticmethod
    def _last_provider_message(record: SessionRecord) -> Optional[str]:
        for entry in reversed(record.provider_history):
            if entry.get("role") == "assistant" and entry.get("content") is not None:
                return str(redact(entry["content"]))
        return None

    def _update_session(
        self,
        session_id: str,
        lease: MutationLease,
        update: Callable[[SessionRecord], None],
    ) -> SessionRecord:
        record = self.sessions.load(session_id)
        update(record)
        return self.sessions.save(record, record.revision, lease)
