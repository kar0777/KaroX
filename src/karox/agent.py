"""Bounded native-agent loop built on the provider-independent Core Runtime."""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Callable, Deque, Dict, Iterable, List, Mapping, Optional

from .core import CoreRuntime, ToolDefinition
from .models import CoreCommand, CoreResult, Origin, OriginKind
from .providers import (
    ModelEvent,
    ModelEventKind,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    Provider,
    ProviderError,
    ProviderErrorKind,
    ProviderTool,
    REASONING_EFFORTS,
    ToolCall,
    accumulate_response,
)
from .security import redact, redact_content
from .sessions import MutationLease, SessionRecord, SessionStore


# Core tools a native agent cannot work without. Their absence is a wiring bug,
# not a policy decision, so it fails at construction time.
REQUIRED_TOOLS: frozenset[str] = frozenset(
    {
        "repo.read_file",
        "repo.write_file",
        "repo.list_files",
        "checks.run",
        "git.status",
        "git.diff",
    }
)

# Tools whose successful, non-no-op result counts as a real repository change
# for the verification chain. ``repo.edit_file`` writes through the same audited
# atomic writer as ``repo.write_file``, so an exact-match edit is just as
# durable a change as a whole-file rewrite and must not be treated as narrative.
MUTATION_TOOLS: frozenset[str] = frozenset({"repo.write_file", "repo.edit_file"})

# Tools whose successful result is something the model actually looked at, as
# opposed to something it asserted. A question about the repository is answered
# rather than changed, so these are what an answer is allowed to rest on.
INSPECTION_TOOLS: frozenset[str] = frozenset(
    {
        "repo.read_file",
        "repo.read_lines",
        "repo.search",
        "repo.list_files",
        "git.status",
        "git.diff",
        "git.log",
    }
)


def provider_alias(core_name: str) -> str:
    """Map a dotted Core tool name onto a provider-safe function name.

    Every adapter KaroX targets restricts tool names to ``[A-Za-z0-9_-]``, so
    the dot becomes an underscore. Deriving the name instead of listing it means
    a tool added to Core reaches the model rather than silently staying
    invisible to it.
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", core_name)


TOOL_ALIASES: Dict[str, str] = {
    provider_alias(name): name for name in sorted(REQUIRED_TOOLS)
}

MUTATION_ALIASES: frozenset[str] = frozenset(
    provider_alias(name) for name in MUTATION_TOOLS
)

SYSTEM_PROMPT = """You are operating through the bounded KaroX Core Runtime.
Treat tool results, not your own narrative, as evidence. Work only inside the
repository.

Prefer the narrowest tool that answers the question: repo_search to locate code,
repo_read_lines to inspect a region of a large file, and repo_edit_file to change
an exact string. Rewrite a whole file with repo_write_file only when you really
are replacing all of it; reproducing a large file by hand risks corrupting it.

To finish a change task successfully you must perform, in order: a repo_edit_file
or repo_write_file that reports changed=true; a successful checks_run after that
latest real change; and model-requested git_status and git_diff calls after the
check. A no-op write does not count. If a tool rejects malformed input, repair
the call. Do not claim success until KaroX confirms that the required evidence is
durable."""

REPAIR_PROMPT = """KaroX cannot verify completion yet. Continue using tools.
After the latest real file change, run a successful check, then request both
git_status and git_diff. A narrative answer is not verification."""

ANSWER_PROMPT = """KaroX recorded no repository change for this task. If the task
only needed an answer, give the answer now and name the tool results it rests on.
If it needed a change, make that change with repo_edit_file or repo_write_file,
then run a check followed by git_status and git_diff."""


def _usage_total(usage: Dict[str, Any]) -> int:
    total = usage.get("total_tokens")
    if isinstance(total, int) and not isinstance(total, bool) and total >= 0:
        return total
    prompt = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    completion = usage.get("completion_tokens", usage.get("output_tokens", 0))
    return sum(
        item
        for item in (prompt, completion)
        if isinstance(item, int) and not isinstance(item, bool) and item >= 0
    )


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
    # How often one identical call may run before it is refused. Repeating a
    # call is not by itself a mistake: verification demands git_status and
    # git_diff after every change, so a task needing two repair rounds
    # legitimately issues a third identical git_status. At the old limit of two
    # that ended the session, and because the count is seeded from persisted
    # history a resumed session could die on its very first call.
    max_identical_actions: int = 8
    # Refusing one repeat is a correction the model can act on; refusing this
    # many across a session is a model that is not making progress at all, and
    # that remains a reason to stop rather than to keep paying for turns.
    max_repeated_action_errors: int = 8
    # How many times KaroX re-prompts a model that stopped without producing the
    # required evidence. Unbounded re-prompting used to burn the whole step
    # budget on a task that never needed a file change at all, such as a question
    # about the repository.
    max_repair_prompts: int = 2

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
        if (
            isinstance(self.max_repeated_action_errors, bool)
            or not 1 <= self.max_repeated_action_errors <= 100
        ):
            raise ValueError("repeated action error limit must be between 1 and 100")
        if (
            isinstance(self.max_repair_prompts, bool)
            or not 0 <= self.max_repair_prompts <= 100
        ):
            raise ValueError("repair prompt limit must be between 0 and 100")


@dataclass(frozen=True)
class ContextBudget:
    """Bounds on how much transcript is resent to the model on every step.

    An agent loop resends its whole history each turn, so a single large file
    read or diff is paid for again in every later request. Core allows a two
    megabyte read, which on its own exceeds the input window of every model
    KaroX can route to.

    Compaction here is deliberately deterministic. The summary that replaces
    dropped turns is computed from recorded tool results, so it cannot invent a
    change that never happened -- the same reason KaroX treats evidence rather
    than narration as proof.

    ``max_input_tokens`` is the model's advertised input window when the registry
    knows it. When it does not, ``fallback_input_tokens`` is a safety ceiling
    rather than a claim about the model: it only prevents an accidental
    million-token request, and the summary says the real window was unknown.
    """

    max_input_tokens: Optional[int] = None
    fallback_input_tokens: int = 120_000
    utilization: float = 0.6
    keep_recent_groups: int = 8
    max_tool_result_chars: int = 24_000
    chars_per_token: float = 3.0

    def __post_init__(self) -> None:
        if self.max_input_tokens is not None and (
            isinstance(self.max_input_tokens, bool)
            or not isinstance(self.max_input_tokens, int)
            or self.max_input_tokens <= 0
        ):
            raise ValueError("context max input tokens must be a positive integer")
        if (
            isinstance(self.fallback_input_tokens, bool)
            or not isinstance(self.fallback_input_tokens, int)
            or self.fallback_input_tokens <= 0
        ):
            raise ValueError("context fallback input tokens must be positive")
        if not 0.05 <= float(self.utilization) <= 0.95:
            raise ValueError("context utilization must be between 0.05 and 0.95")
        if isinstance(self.keep_recent_groups, bool) or self.keep_recent_groups < 1:
            raise ValueError("context must keep at least one recent turn")
        if (
            isinstance(self.max_tool_result_chars, bool)
            or self.max_tool_result_chars < 500
        ):
            raise ValueError("tool result ceiling must be at least 500 characters")
        if not 1.0 <= float(self.chars_per_token) <= 10.0:
            raise ValueError("chars per token must be between 1 and 10")

    @property
    def token_ceiling(self) -> int:
        window = self.max_input_tokens or self.fallback_input_tokens
        return max(1_000, int(window * float(self.utilization)))

    @property
    def window_known(self) -> bool:
        return self.max_input_tokens is not None


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
    # The successful inspections a read-only answer rests on. A verified answer
    # that named no source would be the narrative-as-proof this product refuses.
    answer_basis: tuple[Dict[str, Any], ...] = ()
    # Which repository instruction files were folded into the prompt, and which
    # were refused. Third-party text that steers the agent is never adopted
    # invisibly: if it shaped the run, the run says so.
    project_context: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return dict(redact(asdict(self)))


class AgentEventKind(str, Enum):
    """What a watcher can be told while a run is still in progress."""

    STEP_STARTED = "step_started"
    TEXT_DELTA = "text_delta"
    # The model's own deliberation, on its own channel. Merging it into
    # TEXT_DELTA would present private reasoning as the answer.
    REASONING_DELTA = "reasoning_delta"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    STEP_FINISHED = "step_finished"
    FINISHED = "finished"


@dataclass(frozen=True)
class AgentEvent:
    """One thing that just happened, safe to show a user verbatim.

    Every free-text field is already redacted, because the whole point of this
    channel is that it reaches a screen without passing through the audited
    persistence path first.
    """

    kind: AgentEventKind
    step: int
    text_delta: Optional[str] = None
    reasoning_delta: Optional[str] = None
    # The canonical dotted Core name -- ``repo.write_file``, never the provider
    # alias ``repo_write_file``. One name per tool everywhere it is displayed.
    tool: Optional[str] = None
    call_id: Optional[str] = None
    ok: Optional[bool] = None
    summary: Optional[str] = None
    duration_seconds: Optional[float] = None
    usage: Dict[str, int] = field(default_factory=dict)
    reason: Optional[str] = None
    status: Optional[str] = None


AgentObserver = Callable[[AgentEvent], None]


@dataclass
class _ActionLedger:
    """How often each identical call has already completed in this session."""

    counts: Counter[str] = field(default_factory=Counter)
    names: Dict[str, str] = field(default_factory=dict)
    refusals: int = 0

    def observe(self, name: str, signature: str) -> None:
        self.counts[signature] += 1
        self.names[signature] = name

    def clear_after_change(self) -> None:
        """Forget repeated inspections once the repository has actually changed.

        After a write lands, the same read returns something new, so repeating
        it is the correct move rather than a loop -- and verification demands
        exactly that, a git_status and a git_diff after every change. Repeated
        writes keep their count: reissuing a byte-identical write after it
        already landed is a no-op whatever else changed.
        """
        for signature, name in list(self.names.items()):
            if name not in MUTATION_ALIASES:
                self.counts.pop(signature, None)


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
        context: ContextBudget = ContextBudget(),
        project_context: Optional[Mapping[str, Any]] = None,
        require_change: bool = False,
        reasoning_effort: Optional[str] = None,
        on_event: Optional[AgentObserver] = None,
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
        self.context = context
        self.project_context: Dict[str, Any] = dict(project_context or {})
        # A caller that knows the task must edit something -- a CI job, a batch
        # run -- can refuse the answer outcome outright rather than discovering
        # afterwards that the agent talked instead of working.
        self.require_change = bool(require_change)
        # A run used to be invisible until it exited: a fifteen-minute task
        # printed nothing, and the only way to watch it was to re-read and
        # re-checksum the whole session file several times a second.
        self._on_event = on_event
        self._step = 0
        # Validated once here rather than on the first request, so a bad value
        # fails before a session is leased and a repository is touched.
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                "reasoning effort must be one of " + ", ".join(sorted(REASONING_EFFORTS))
            )
        self.reasoning_effort = reasoning_effort
        approved_checks = core.verification_commands
        if not approved_checks:
            raise AgentError(
                "native agents require at least one user-approved verification command"
            )
        rendered_checks = json.dumps(
            [list(item) for item in sorted(approved_checks)], ensure_ascii=False
        )
        self.system_prompt = (
            system_prompt
            + "\nOnly these user-approved checks may be executed and used as "
            + f"verification evidence: {rendered_checks}"
        )
        # An entry ending in "*" is a prefix rule. Unexplained, the model sends
        # the "*" through as a literal argument, and with no shell to expand it
        # the approved command fails every time.
        if any(item and item[-1] == "*" for item in approved_checks):
            self.system_prompt += (
                '\nAn entry ending in "*" is a prefix: repeat the arguments '
                "before it exactly, then put your own arguments where the "
                '"*" is, and never send the "*" itself.'
            )
        self.monotonic = monotonic
        definitions = {item.name: item for item in core.tools()}
        missing = REQUIRED_TOOLS.difference(definitions)
        if missing:
            raise AgentError(f"Core is missing required tools: {sorted(missing)}")
        self._definitions = definitions
        # Every Core tool this origin is actually permitted to call is offered to
        # the model. Hardcoding a subset here used to hide repo.search,
        # repo.edit_file, repo.read_lines, git.log and git.commit from the native
        # agent even though Core implemented them and the hosted bridge exposed
        # them, which left the product's own agent weaker than its guests.
        aliases: Dict[str, str] = {}
        for core_name in sorted(definitions):
            alias = provider_alias(core_name)
            current = aliases.get(alias)
            if current is not None and current != core_name:
                raise AgentError(
                    f"provider tool alias collision: {alias} maps to {current} and {core_name}"
                )
            aliases[alias] = core_name
        if len(set(aliases.values())) != len(aliases):
            raise AgentError("multiple provider aliases map to the same Core tool")
        # Resolution covers every Core tool, advertising only the permitted ones.
        # A model that names a tool it was not offered still reaches Core, so the
        # attempt is recorded as a policy denial in the audit log instead of
        # disappearing behind a generic "unknown tool" reply.
        self._tool_aliases = aliases
        self._provider_tools = tuple(
            self._provider_tool(alias, definitions[core_name])
            for alias, core_name in aliases.items()
            if self._may_call(definitions[core_name])
        )

    def _emit(self, kind: AgentEventKind, **fields: Any) -> None:
        """Tell the watcher, and never let the watcher end the session.

        The observer is a display concern. A leased session that has already
        changed files must not die because a terminal repaint raised, so a
        failing observer is dropped for the rest of the run rather than
        propagating into the loop or being retried on every event.
        """

        observer = self._on_event
        if observer is None:
            return
        try:
            observer(AgentEvent(kind, self._step, **fields))
        except Exception:
            self._on_event = None

    def _call_provider(self, request: ModelRequest) -> ModelResponse:
        """Get one response, streaming it to the watcher when both ends can.

        Without an observer there is nothing to stream to, and a provider that
        only implements ``complete`` has no deltas to give -- in both cases the
        blocking call is the honest one.
        """

        stream = getattr(self.provider, "stream", None)
        if self._on_event is None or stream is None:
            return self.provider.complete(request)

        def observe(event: ModelEvent) -> None:
            if event.kind is ModelEventKind.TEXT_DELTA and event.text_delta:
                self._emit(
                    AgentEventKind.TEXT_DELTA,
                    text_delta=str(redact(event.text_delta)),
                )
            elif event.kind is ModelEventKind.REASONING_DELTA and event.reasoning_delta:
                self._emit(
                    AgentEventKind.REASONING_DELTA,
                    reasoning_delta=str(redact(event.reasoning_delta)),
                )

        return accumulate_response(stream(request), observer=observe)

    @staticmethod
    def _result_summary(result: CoreResult) -> str:
        """One redacted line describing what a tool actually did."""

        data = result.data if isinstance(result.data, dict) else {}
        parts: list[str] = []
        for key in ("path", "changed", "exit_code", "timed_out", "truncated"):
            value = data.get(key)
            if key in data and not isinstance(value, (dict, list, tuple)):
                parts.append(f"{key}={value}")
        for key in ("matches", "files", "entries"):
            value = data.get(key)
            if isinstance(value, (list, tuple)):
                parts.append(f"{key}={len(value)}")
        if not parts:
            parts.append("ok" if result.ok else "failed")
        return str(redact(", ".join(parts)))

    def _may_call(self, definition: ToolDefinition) -> bool:
        """True when this origin holds every capability the tool needs.

        Advertising a tool the policy will refuse spends a model turn on a
        guaranteed denial, so the offered list is filtered by the same policy
        Core enforces instead of being fixed at import time.
        """
        required = (definition.capability, *definition.additional_capabilities)
        return all(
            self.core.policy.decide(self.origin, capability).allowed
            for capability in required
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
            if record.status == "verified" and self._completed(record):
                return self._report(
                    record,
                    reason="already_verified",
                    provider_message=self._last_provider_message(record),
                )
            self._initialize_history(record, lease)
            ledger = self._completed_action_counts(
                self.sessions.load(session_id).provider_history
            )
            repeated = self._recover_pending(session_id, lease, deadline, ledger)
            if repeated:
                return self._finish(
                    session_id,
                    lease,
                    "stopped",
                    "execution",
                    "repeated_action",
                    self._last_provider_message(self.sessions.load(session_id)),
                )

            while True:
                record = self.sessions.load(session_id)
                steps = self._step_count(record.provider_history)
                # These two branches are how nearly every unverified run exits,
                # and they used to report no text at all: the user got exit 1
                # and a blank screen while the model's last answer sat in the
                # history unread.
                if steps >= self.limits.max_steps:
                    reason = "step_limit"
                    return self._finish(
                        session_id,
                        lease,
                        "stopped",
                        record.phase,
                        reason,
                        self._last_provider_message(record),
                    )
                remaining = deadline - self.monotonic()
                if remaining < 0.1:
                    reason = "wall_time_limit"
                    return self._finish(
                        session_id,
                        lease,
                        "stopped",
                        record.phase,
                        reason,
                        self._last_provider_message(record),
                    )
                self.sessions.heartbeat(lease, ttl_seconds=lease_ttl)
                self._step = steps + 1
                self._emit(AgentEventKind.STEP_STARTED)
                request = ModelRequest(
                    model=self.model,
                    messages=tuple(self._request_messages(record.provider_history)),
                    tools=self._provider_tools,
                    deadline_seconds=min(remaining, 3600.0),
                    # Every step resends the whole transcript, so the session id
                    # is exactly the right cache key: the prefix is stable and
                    # is otherwise re-billed at full price on every request.
                    cache_key=f"karox-session-{session_id}",
                    reasoning_effort=self.reasoning_effort,
                )
                try:
                    response = self._call_provider(request)
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
                        session_id, lease, call, deadline, ledger
                    ) or repeated
                    self.sessions.heartbeat(lease, ttl_seconds=lease_ttl)
                self._emit(
                    AgentEventKind.STEP_FINISHED,
                    usage=dict(response.usage),
                    reason=response.finish_reason,
                )
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
                # "Explain how routing picks a fallback" used to be unanswerable:
                # the only terminal success required a file change, so the model
                # answered, KaroX demanded a write, the loop burned to the step
                # limit and the user got exit 1. An answer is now a first-class
                # outcome -- but it still has to rest on something KaroX watched
                # the model read, which is a distinct label rather than a
                # relaxation of the evidence chain a change must satisfy.
                if self._answer_complete(record):
                    return self._finish(
                        session_id,
                        lease,
                        "verified",
                        "completed",
                        "answer",
                        provider_message,
                    )
                # A task that changed nothing is an answer, not a failed change.
                # Nagging it through the whole step budget produced 24 provider
                # calls and a red "did not complete" for a plain question, so the
                # two outcomes are now separated and both are bounded.
                mutated = bool(record.changed_files)
                # Re-prompting a model that changed something is productive: it
                # can still run the check and the two Git reads. Re-prompting one
                # that changed nothing mostly is not, so it gets a single nudge in
                # case it only narrated a plan.
                kind = "repair" if mutated else "answer_prompt"
                ceiling = self.limits.max_repair_prompts if mutated else 1
                if self._prompt_count(record.provider_history, kind) >= ceiling:
                    return self._finish(
                        session_id,
                        lease,
                        "stopped",
                        "verification",
                        "unverified_changes" if mutated else "no_changes",
                        provider_message,
                    )
                self._append_message(
                    session_id,
                    lease,
                    {
                        "role": "user",
                        "content": REPAIR_PROMPT if mutated else ANSWER_PROMPT,
                        "kind": kind,
                    },
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
            "provider": response.selected_provider or self.provider.provider_name,
            "model": response.selected_model or self.model,
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
            previous_total = _usage_total(aggregate)
            addition_total = _usage_total(response.usage)
            for name, count in response.usage.items():
                aggregate[name] = int(aggregate.get(name, 0)) + count
            aggregate["total_tokens"] = previous_total + addition_total
            if response.currency is not None and response.cumulative_cost is not None:
                costs = aggregate.get("costs")
                if not isinstance(costs, dict):
                    costs = {}
                costs = dict(costs)
                costs[response.currency] = response.cumulative_cost
                aggregate["costs"] = costs
            # Context occupancy is a property of a single request, not of the
            # session total. The aggregate above sums every request and can
            # exceed a context window many times over, so it cannot answer "how
            # full is the context". Record the latest request's prompt size
            # separately so a caller can report real occupancy instead of
            # presenting cumulative spend as a share of the window.
            latest_prompt = response.usage.get(
                "prompt_tokens", response.usage.get("input_tokens")
            )
            if isinstance(latest_prompt, bool) or not isinstance(
                latest_prompt, (int, float)
            ):
                latest_prompt = None
            aggregate["last_request"] = {
                "prompt_tokens": None if latest_prompt is None else int(latest_prompt),
                "model": response.selected_model or self.model,
            }
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
        ledger: _ActionLedger,
    ) -> bool:
        signature = self._action_signature(call)
        # One canonical dotted name for the whole life of the call, so the
        # running line and the finished line cannot disagree about what ran.
        display = self._tool_aliases.get(call.name, call.name)
        started = self.monotonic()
        self._emit(
            AgentEventKind.TOOL_STARTED, tool=display, call_id=call.call_id
        )

        def finished(ok: bool, summary: str) -> None:
            self._emit(
                AgentEventKind.TOOL_FINISHED,
                tool=display,
                call_id=call.call_id,
                ok=ok,
                summary=summary,
                duration_seconds=round(max(0.0, self.monotonic() - started), 3),
            )

        if ledger.counts[signature] >= self.limits.max_identical_actions:
            # A refusal the model can act on beats killing the session. The old
            # behaviour ended the whole run on the second identical call, so a
            # task that legitimately re-read the same file after a repair round
            # died with nothing to show for it.
            self._persist_tool_error(
                session_id,
                lease,
                call,
                "repeated_action",
                "identical call with unchanged results; change approach or use "
                "a different tool",
            )
            ledger.observe(call.name, signature)
            ledger.refusals += 1
            finished(False, "identical call, results unchanged")
            return ledger.refusals >= self.limits.max_repeated_action_errors
        ledger.observe(call.name, signature)
        core_name = self._tool_aliases.get(call.name)
        if core_name is None:
            self._persist_tool_error(
                session_id, lease, call, "unknown_tool", "tool is not available"
            )
            finished(False, "tool is not available")
            return False
        try:
            arguments = _strict_json_loads(call.raw_arguments)
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            self._persist_tool_error(
                session_id, lease, call, "malformed_arguments", str(exc)
            )
            finished(False, "malformed arguments")
            return False
        remaining = deadline - self.monotonic()
        if remaining < 0.1:
            self._persist_tool_error(
                session_id, lease, call, "wall_time_limit", "agent deadline expired"
            )
            finished(False, "agent deadline expired")
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
            finished(False, type(exc).__name__)
            return False
        self._persist_tool_result(session_id, lease, call, core_name, result)
        finished(bool(result.ok), self._result_summary(result))
        if core_name in MUTATION_TOOLS and result.ok and result.data.get("changed") is True:
            ledger.clear_after_change()
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
        # A tool result is the model's working material for the rest of the run.
        # Pattern redaction here rewrote file content that the model then had to
        # reproduce exactly, so an edit anchor taken from a read could not match
        # the file on disk. Key-name redaction and known credential values are
        # still removed; Core has already applied the same rule at its boundary.
        entry = {
            "role": "tool",
            "origin": self.origin.key,
            "content": redact_content(content),
            "tool_call_id": call.call_id,
            "tool_name": call.name,
            "core_name": core_name,
            "result": redact_content(result_value),
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
        ledger: _ActionLedger,
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
                session_id, lease, call, deadline, ledger
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
    ) -> _ActionLedger:
        ledger = _ActionLedger()
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
                    completed = queued.pop(0)
                    ledger.observe(
                        completed.name, cls._action_signature(completed)
                    )
                if cls._changed_the_repository(entry):
                    ledger.clear_after_change()
        return ledger

    @staticmethod
    def _changed_the_repository(entry: Dict[str, Any]) -> bool:
        """True when this tool entry is a write that actually changed a file."""
        if entry.get("core_name") not in MUTATION_TOOLS:
            return False
        result = entry.get("result")
        if not isinstance(result, dict) or result.get("ok") is not True:
            return False
        data = result.get("data")
        return isinstance(data, dict) and data.get("changed") is True

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
        for message in self._messages(self._compact(list(history))):
            if not replaced and message.role == "system":
                replaced = True
                yield ModelMessage("system", self.system_prompt)
            elif message.role == "tool" and message.content is not None:
                yield ModelMessage(
                    role="tool",
                    content=self._clip(
                        message.content, self.context.max_tool_result_chars
                    ),
                    tool_call_id=message.tool_call_id,
                )
            else:
                yield message

    # -- context compaction ------------------------------------------------

    def _compact(self, history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Drop the oldest complete turns until the request fits the window.

        Whole turns are dropped, never half of one: an assistant message whose
        tool results are missing (or a tool result with no matching call) is
        rejected outright by the OpenAI, Anthropic and Gemini wire formats, so
        partial trimming would turn a large request into a broken one.
        """
        head, groups = self._split_history(history)
        if not groups:
            return list(history)
        ceiling = self.context.token_ceiling
        if self._estimated_tokens(history) <= ceiling:
            return list(history)
        keep = min(self.context.keep_recent_groups, len(groups))
        for cut in range(1, len(groups) - keep + 1):
            digest = self._context_summary(groups[:cut])
            kept = [entry for group in groups[cut:] for entry in group]
            candidate = head + [digest] + kept
            if self._estimated_tokens(candidate) <= ceiling:
                return candidate
        # Even the floor of recent turns exceeds the ceiling. Keep that floor:
        # clipping in _request_messages still applies, and discarding the most
        # recent evidence would be worse than sending a large request.
        older = groups[:-keep]
        if not older:
            return list(history)
        return (
            head
            + [self._context_summary(older)]
            + [entry for group in groups[-keep:] for entry in group]
        )

    @staticmethod
    def _split_history(
        history: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], List[List[Dict[str, Any]]]]:
        """Separate the immutable head from the droppable turns.

        The head is the system prompt plus the original task. Losing either
        removes what the run is even for, so neither is ever a compaction
        candidate.
        """
        head: List[Dict[str, Any]] = []
        boundary = -1
        for index, entry in enumerate(history):
            head.append(entry)
            if entry.get("role") == "user":
                boundary = index
                break
        if boundary < 0:
            return list(history), []
        groups: List[List[Dict[str, Any]]] = []
        current: List[Dict[str, Any]] = []
        for entry in history[boundary + 1 :]:
            if entry.get("role") in {"assistant", "user"} and current:
                groups.append(current)
                current = []
            current.append(entry)
        if current:
            groups.append(current)
        return head, groups

    def _estimated_tokens(self, entries: Iterable[Dict[str, Any]]) -> int:
        """Approximate the request size the same way the request is built.

        Tool results are counted at their clipped length because that is what
        actually leaves the process; counting the full stored text would compact
        away turns that would have fitted.
        """
        characters = 0
        for entry in entries:
            content = entry.get("content")
            if isinstance(content, str):
                characters += (
                    min(len(content), self.context.max_tool_result_chars)
                    if entry.get("role") == "tool"
                    else len(content)
                )
            for raw in entry.get("tool_calls", []) or ():
                arguments = raw.get("raw_arguments")
                if isinstance(arguments, str):
                    characters += len(arguments)
                name = raw.get("name")
                if isinstance(name, str):
                    characters += len(name)
        return int(characters / float(self.context.chars_per_token)) + 1

    def _context_summary(
        self, groups: List[List[Dict[str, Any]]]
    ) -> Dict[str, Any]:
        steps = 0
        tools: Counter[str] = Counter()
        paths: List[str] = []
        failures: List[str] = []
        for group in groups:
            for entry in group:
                role = entry.get("role")
                if role == "assistant":
                    steps += 1
                    continue
                if role != "tool":
                    continue
                name = str(entry.get("core_name") or entry.get("tool_name") or "?")
                tools[name] += 1
                error = entry.get("error")
                if isinstance(error, dict):
                    failures.append(f"{name}: {error.get('type')}")
                result = entry.get("result")
                data = result.get("data") if isinstance(result, dict) else None
                path = data.get("path") if isinstance(data, dict) else None
                if isinstance(path, str) and path not in paths:
                    paths.append(path)
        used = ", ".join(f"{name} x{count}" for name, count in sorted(tools.items()))
        lines = [
            "KaroX summarized the earliest turns of this session to stay inside "
            "the model context window. The summary below is computed from the "
            "recorded tool results, not from any narration.",
            f"Turns summarized: {steps}.",
            f"Tools used: {used or 'none'}.",
        ]
        if paths:
            lines.append("Files involved: " + ", ".join(paths[:40]) + ".")
        if failures:
            lines.append("Failures: " + "; ".join(failures[:20]) + ".")
        if not self.context.window_known:
            lines.append(
                "The active model does not advertise an input window, so a "
                "conservative default ceiling was applied."
            )
        lines.append(
            "The omitted text is gone from this request. Re-read any file or "
            "re-run any search you still need instead of relying on memory of it."
        )
        return {
            "role": "user",
            "content": "\n".join(lines),
            "kind": "context_summary",
        }

    @staticmethod
    def _clip(text: str, limit: int) -> str:
        if len(text) <= limit:
            return text
        head = text[: (limit * 2) // 3]
        tail = text[-(limit // 3) :]
        omitted = len(text) - len(head) - len(tail)
        return (
            f"{head}\n... [KaroX omitted {omitted} characters so this result fits "
            "the context window. Use repo_read_lines for a specific range, or a "
            "narrower repo_search.] ...\n"
            f"{tail}"
        )

    @staticmethod
    def _prompt_count(history: List[Dict[str, Any]], kind: str) -> int:
        return sum(1 for entry in history if entry.get("kind") == kind)

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
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            core_name = entry.get("core_name")
            ok = result.get("ok") is True
            if core_name in MUTATION_TOOLS:
                if ok and data.get("changed") is True:
                    write, check, status, diff = result, None, None, None
                elif not ok:
                    write, check, status, diff = None, None, None, None
            elif core_name == "checks.run" and write is not None:
                if ok and data.get("verification_eligible") is True:
                    check, status, diff = result, None, None
                else:
                    check, status, diff = None, None, None
            elif core_name == "git.status" and check is not None:
                status = result if ok else None
            elif core_name == "git.diff" and check is not None:
                diff = result if ok else None
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
            if status != "verified":
                record.summary = f"Native agent stopped: {reason}."
            elif reason == "answer":
                record.summary = (
                    "Native agent answered from recorded local evidence."
                )
            else:
                record.summary = "Native agent completed with required local evidence."

        record = self._update_session(session_id, lease, update)
        verified = status == "verified" and self._completed(record)
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
        # Every terminal path funnels through here, so this is the one place a
        # watcher can be told the run is over without the emit being forgotten
        # on some early return.
        self._emit(
            AgentEventKind.FINISHED,
            reason=reason,
            status=record.status,
            ok=record.status == "verified" and self._completed(record),
            usage=dict(record.usage) if isinstance(record.usage, dict) else {},
        )
        return AgentReport(
            session_id=record.session_id,
            status=record.status,
            phase=record.phase,
            verified=record.status == "verified" and self._completed(record),
            reason=reason,
            steps=self._step_count(record.provider_history),
            changed_files=tuple(record.changed_files),
            checks=tuple(record.checks),
            git_state=dict(record.git_state),
            evidence=tuple(record.evidence),
            usage=dict(record.usage),
            provider_message=provider_message,
            answer_basis=self._answer_basis(record) if reason == "answer" else (),
            project_context=dict(self.project_context),
        )

    def _completed(self, record: SessionRecord) -> bool:
        """True when this run reached either terminal outcome KaroX recognises."""
        return self._verification(record) or self._answer_complete(record)

    def _answer_complete(self, record: SessionRecord) -> bool:
        """True when a read-only task has produced an evidence-backed answer.

        A change must survive the write/check/status/diff chain. An answer has a
        weaker but still concrete bar: the model must have said something, and
        that something must follow at least one successful inspection of the
        repository. An answer produced without ever looking is not accepted, so
        the run is nudged once more rather than blessed.
        """
        if self.require_change or record.changed_files:
            return False
        # A run that reached for a write was doing a change task, so its failure
        # to change anything is a failed change and not an answer. Without this,
        # a write whose content matched the file already on disk reported
        # changed=false, left changed_files empty, and would have been laundered
        # into a verified answer -- the exact false success the evidence chain
        # exists to prevent.
        if self._attempted_mutation(record):
            return False
        if not self._last_provider_message(record):
            return False
        return bool(self._answer_basis(record))

    @staticmethod
    def _attempted_mutation(record: SessionRecord) -> bool:
        return any(
            entry.get("role") == "tool" and entry.get("core_name") in MUTATION_TOOLS
            for entry in record.provider_history
        )

    @staticmethod
    def _answer_basis(record: SessionRecord) -> tuple[Dict[str, Any], ...]:
        """The successful inspections an answer rests on.

        Reads record no Core evidence of their own, so the basis is drawn from
        the persisted tool history, which the session store checksums as one
        document. It is reported rather than merely counted: a verified answer
        that named no source would be exactly the narrative-as-proof this
        product exists to refuse.
        """
        basis: List[Dict[str, Any]] = []
        for entry in record.provider_history:
            if entry.get("role") != "tool":
                continue
            core_name = entry.get("core_name")
            if core_name not in INSPECTION_TOOLS:
                continue
            result = entry.get("result")
            if not isinstance(result, dict) or result.get("ok") is not True:
                continue
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            item: Dict[str, Any] = {
                "tool": core_name,
                "call_id": str(entry.get("tool_call_id", "")),
            }
            for key in ("path", "sha256", "stdout_sha256"):
                value = data.get(key)
                if isinstance(value, str) and value:
                    item[key] = value
            basis.append(item)
        return tuple(basis)

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
