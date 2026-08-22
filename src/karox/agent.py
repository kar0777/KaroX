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

from .agent_modes import normalize_mode
from .cache_scheduler import CacheAwareScheduler, CacheDecision
from .core import CoreRuntime, ToolDefinition
from .context_compiler import (
    CompiledContext,
    ContextCompiler,
    ContextItem,
    Verdict,
    continuity_lines,
)
from .cost_intelligence import (
    ContextTierDecision,
    CostGovernor,
    CostLedger,
    ReadCache,
    StablePrefixCache,
    ToolSchemaDeduplicator,
)
from .models import CoreCommand, CoreResult, Origin, OriginKind
from .provider_pricing import PricingRegistry
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
    ReasoningBlock,
    ToolCall,
    accumulate_response,
)
from .security import redact, redact_content
from .sessions import MutationLease, SessionRecord, SessionStore
from .tool_universe import (
    UniverseSelection,
    discovery_note,
    family_of,
    select_families,
)
from .usage_analytics import merge_response_usage, usage_event_from_response


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
durable.

Write everything the user reads in the language of their task. These instructions
and KaroX's own follow-up prompts are in English whatever that language is, so do
not take them as a request to switch: a task written in Russian is answered in
Russian."""

REPAIR_PROMPT = """KaroX cannot verify completion yet. Continue using tools.
After the latest real file change, run a successful check, then request both
git_status and git_diff. A narrative answer is not verification."""

ANSWER_PROMPT = """KaroX recorded no repository change for this task. If the task
only needed an answer, give the answer now and name the tool results it rests on.
If it needed a change, make that change with repo_edit_file or repo_write_file,
then run a check followed by git_status and git_diff."""

# A native coding agent must not spend a second provider turn asking for
# repository evidence when the user clearly did not ask about the repository at
# all. Keep this intentionally conservative: only self-contained greetings,
# thanks and tiny social prompts are admitted. Anything with extra task text
# falls back to the normal evidence-backed answer contract.
_SMALL_TALK_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"^(?:привет(?:ик)?|здравствуй(?:те)?|хай|hello|hi|hey)[\s!,.?…👋🙂😊]*$",
        r"^(?:доброе\s+(?:утро|день|вечер)|good\s+(?:morning|afternoon|evening))[\s!,.?…🙂😊]*$",
        r"^(?:спасибо|благодарю|thanks|thank\s+you)[\s!,.?…🙂😊]*$",
        r"^(?:как\s+дела|how\s+are\s+you|кто\s+ты|who\s+are\s+you|что\s+ты\s+умеешь|what\s+can\s+you\s+do)[\s!,.?…]*$",
    )
)


def _is_small_talk_task(task: str) -> bool:
    normalized = re.sub(r"\s+", " ", str(task).strip())
    if not normalized or len(normalized) > 80:
        return False
    return any(pattern.fullmatch(normalized) is not None for pattern in _SMALL_TALK_PATTERNS)


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
    # Present only when the model's context was rewritten to fit its window,
    # with the token counts either side. The same rule: if it shaped the run,
    # the run says so.
    compaction: Optional[Dict[str, Any]] = None
    # The stance the run was started with (--mode / the TUI\'s /mode). None is
    # the legacy default: a run created without the flag behaves as Build and
    # claims nothing beyond that.
    mode: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return dict(redact(asdict(self)))


def _joined(items: List[str], *, limit: int = 200, separator: str = ", ") -> str:
    """Join a list for a summary, saying so when it did not all fit.

    Truncating silently is the failure this product's own rules forbid: a summary
    that listed forty files out of ninety read as complete. The cap is high enough
    that reaching it is unusual, and reaching it is now visible.
    """

    if len(items) <= limit:
        return separator.join(items)
    shown = separator.join(items[:limit])
    return f"{shown} (and {len(items) - limit} more not listed here)"


def _summary_paths(data: Any) -> List[str]:
    """Every repository path a recorded result touched, not just the first."""

    if not isinstance(data, Mapping):
        return []
    found: List[str] = []
    single = data.get("path")
    if isinstance(single, str) and single:
        found.append(single)
    for key in ("paths", "files", "changed_files", "matches", "entries"):
        value = data.get(key)
        if not isinstance(value, list):
            continue
        for item in value:
            if isinstance(item, str) and item:
                found.append(item)
            elif isinstance(item, Mapping):
                nested = item.get("path")
                if isinstance(nested, str) and nested:
                    found.append(nested)
    return found


def _summary_evidence_ids(result: Any) -> List[str]:
    """The evidence IDs a recorded result produced."""

    if not isinstance(result, Mapping):
        return []
    records = result.get("evidence")
    if not isinstance(records, list):
        return []
    found: List[str] = []
    for item in records:
        if not isinstance(item, Mapping):
            continue
        identifier = item.get("evidence_id") or item.get("id")
        if isinstance(identifier, str) and identifier:
            found.append(identifier)
    return found


def _reasoning_blocks(entry: Mapping[str, Any]) -> tuple[ReasoningBlock, ...]:
    """Rebuild an assistant turn's deliberation from what was persisted.

    A stored block that no longer describes something this transport can return
    is dropped here rather than at the wire, so a record written by an older
    version -- or corrupted -- degrades to a turn with no thinking instead of a
    request the provider refuses.
    """

    stored = entry.get("reasoning_blocks")
    if not isinstance(stored, list):
        return ()
    blocks: list[ReasoningBlock] = []
    for item in stored:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        if kind not in {"thinking", "redacted_thinking"}:
            continue
        blocks.append(
            ReasoningBlock(
                kind=kind,
                text=str(item.get("text") or ""),
                data=str(item.get("data") or ""),
                signature=str(item.get("signature") or ""),
                replayable=item.get("replayable") is not False,
            )
        )
    return tuple(blocks)


class AgentEventKind(str, Enum):
    """What a watcher can be told while a run is still in progress."""

    STEP_STARTED = "step_started"
    TEXT_DELTA = "text_delta"
    # The model's own deliberation, on its own channel. Merging it into
    # TEXT_DELTA would present private reasoning as the answer.
    REASONING_DELTA = "reasoning_delta"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    # The middle of the conversation was replaced by a summary to fit the window.
    COMPACTED = "compacted"
    STEP_FINISHED = "step_finished"
    FINISHED = "finished"
    # Normalized product events: providers and adapters never construct UI
    # presentation; the kernel emits typed facts and every renderer derives
    # its own view from them.
    PHASE_CHANGED = "phase_changed"
    WARNING = "warning"
    ERROR = "error"
    FILE_READ = "file_read"
    FILE_EDITED = "file_edited"
    TEST_STARTED = "test_started"
    TEST_FINISHED = "test_finished"
    ARTIFACT_CREATED = "artifact_created"


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
    # Normalized event fields: the session phase, a repository-relative file
    # path, and a short safe detail line. Free text is redacted at the emit
    # site like every other field on this channel.
    phase: Optional[str] = None
    path: Optional[str] = None
    detail: Optional[str] = None


AgentObserver = Callable[[AgentEvent], None]


_READ_TOOL_NAMES = frozenset({"repo.read_file", "repo.read_lines"})
_EDIT_TOOL_NAMES = frozenset({"repo.edit_file", "repo.write_file"})
_TEST_TOOL_NAMES = frozenset({"checks.run", "checks.run_affected", "tests.run"})


def _derived_tool_events(
    core_name: str, result: Any
) -> list[tuple[AgentEventKind, Dict[str, Any]]]:
    """Normalized file/test facts derived from one finished tool call.

    Pure derivation from the CoreResult: a read that succeeded is FILE_READ,
    an edit that actually changed a file is FILE_EDITED, and a check-family
    call is TEST_FINISHED with its exit code. Nothing is invented -- a result
    without the expected fields derives nothing.
    """

    data = getattr(result, "data", None)
    if not isinstance(data, dict):
        return []
    ok = bool(getattr(result, "ok", False))
    events: list[tuple[AgentEventKind, Dict[str, Any]]] = []
    path = data.get("path")
    if core_name in _READ_TOOL_NAMES and ok and isinstance(path, str) and path:
        events.append((AgentEventKind.FILE_READ, {"path": path}))
    elif (
        core_name in _EDIT_TOOL_NAMES
        and ok
        and data.get("changed") is True
        and isinstance(path, str)
        and path
    ):
        events.append((AgentEventKind.FILE_EDITED, {"path": path}))
    elif core_name in _TEST_TOOL_NAMES:
        exit_code = data.get("exit_code")
        detail = (
            f"exit {exit_code}"
            if isinstance(exit_code, int) and not isinstance(exit_code, bool)
            else None
        )
        events.append((AgentEventKind.TEST_FINISHED, {"ok": ok, "detail": detail}))
    return events


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
        max_output_tokens: Optional[int] = None,
        reasoning_effort: Optional[str] = None,
        economy_mode: bool = False,
        mode: Optional[str] = None,
        pricing_registry: Optional[PricingRegistry] = None,
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
        self._compactions = 0
        self._last_compaction: Optional[Dict[str, Any]] = None
        # Validated once here rather than on the first request, so a bad value
        # fails before a session is leased and a repository is touched.
        if reasoning_effort is not None and reasoning_effort not in REASONING_EFFORTS:
            raise ValueError(
                "reasoning effort must be one of " + ", ".join(sorted(REASONING_EFFORTS))
            )
        self.reasoning_effort = reasoning_effort
        # Economy is deliberately orthogonal to model choice and reasoning.
        # It may remove transport/context duplication, but it must not silently
        # choose a weaker model, lower effort, or shrink the quality ceilings.
        self.economy_mode = bool(economy_mode)
        # The agent stance (Build/Plan/Ideate). Validated here so a bad value
        # fails before a session is leased; the kernel only records it for the
        # report -- the caller owns the prompt delta and the grant policy.
        self.mode = None if mode is None else normalize_mode(mode)
        # The model's own output ceiling, sent on every request. Leaving it unset
        # here made it something only the routed layer could supply, so a model
        # registered without one -- or any direct endpoint -- was capped by an
        # adapter constant instead, and a long answer was cut off mid-sentence
        # with nothing but a finish_reason to show for it.
        if max_output_tokens is not None and (
            not isinstance(max_output_tokens, int)
            or isinstance(max_output_tokens, bool)
            or max_output_tokens <= 0
        ):
            raise ValueError("agent output ceiling must be a positive integer")
        self.max_output_tokens = max_output_tokens
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
        self._permitted_tool_pairs = tuple(
            (alias, core_name)
            for alias, core_name in aliases.items()
            if self._may_call(definitions[core_name])
        )
        self._provider_tools = tuple(
            self._provider_tool(alias, definitions[core_name])
            for alias, core_name in self._permitted_tool_pairs
        )
        # -- quality economy (P0.4) ---------------------------------------
        # Same model, same reasoning, same verification: these primitives
        # measure and remove infrastructure duplication only. Each keeps a
        # counter the session usage view and tests can read back, so every
        # economy claim stays provable from this process instead of assumed.
        self._cost_ledger = CostLedger()
        self._prefix_cache = StablePrefixCache()
        # Cache-aware scheduling (Part 2): capability starts UNKNOWN and is
        # confirmed only by provider-reported cached tokens; money appears
        # only when the pricing registry knows every rate involved. The
        # scheduler is advisory: it never blocks a step or drops the key.
        self._pricing_registry = pricing_registry
        self._cache_scheduler = CacheAwareScheduler(
            pricing=(
                None
                if pricing_registry is None
                else pricing_registry.lookup(provider.provider_name, model)
            )
        )
        self._cache_decision: Optional[CacheDecision] = None
        self._cache_pricing_model = model
        # Long-context guard (Part 2): advisory tier classification from the
        # same pricing record the scheduler follows. It never blocks and
        # never trims evidence itself; it makes the premium visible and
        # names the reductions worth attempting. Quality precedes price.
        self._context_tier: Optional[ContextTierDecision] = None
        self._schema_dedup = ToolSchemaDeduplicator()
        self._read_cache = ReadCache()
        # Shadow mode: the governor observes and warns; it never blocks a run
        # and never downgrades the model. Enforcement stays a user decision.
        self._cost_governor = CostGovernor(shadow_mode=True)
        # Typed context IR (Part 2): decisions are measured on every request;
        # rewriting is applied only in economy mode and only through lossless
        # reference / supersession markers. Quality precedes reduction.
        self._context_compiler = ContextCompiler()
        self._context_compilation: CompiledContext | None = None
        self._economy_stale_chars_pending = 0
        # -- deferred tool universe (Part 2) --------------------------------
        # Family selection is measured on every run and applied only in
        # economy mode. Resolution always covers the full permitted set, so
        # an omitted tool called by exact name still executes, and its family
        # is advertised from the next step: deferral can never strand a run.
        self._universe_selection: Optional[UniverseSelection] = None
        self._expanded_families: set[str] = set()
        self._tool_schemas_included = 0
        self._tool_schemas_omitted = 0
        self._tool_schema_bytes_included = 0
        self._tool_schema_bytes_avoided = 0
        self._universe_note = ""
        self._refresh_tool_advertisement()
        self._prefix_stable_steps = 0
        self._prefix_total_steps = 0
        # ToolVM lane of the same measurement: one response that carries N
        # tool calls executes them in one model turn where classic
        # one-call-per-turn execution would have paid N turns.
        self._batched_turns_avoided = 0
        # Reasoning continuity: statements the compaction summary carried
        # verbatim so the model does not re-derive its own conclusions.
        self._continuity_statements_carried = 0
        self._continuity_chars_carried = 0

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

    # -- deferred tool universe ---------------------------------------------

    def _select_tool_universe(self, task_text: object) -> None:
        """Decide once per run which tool families this task gets to see.

        Selection is a pure function of the task text, so re-running the same
        task keeps the same advertisement and the same prompt-cache prefix.
        Expansion state resets here: a family another task demanded must not
        leak into this one.
        """

        self._universe_selection = select_families(
            task_text if isinstance(task_text, str) else ""
        )
        self._expanded_families = set()
        self._refresh_tool_advertisement()

    def _active_families(self) -> Optional[frozenset[str]]:
        selection = self._universe_selection
        if selection is None:
            return None
        return frozenset(selection.families) | frozenset(self._expanded_families)

    def _note_tool_demand(self, core_name: str) -> None:
        """Widen the advertised universe when the model proves it needs more.

        The omitted call itself already executed through full resolution; the
        only thing that changes is that from the next step the family's
        schemas are attached, so later calls are well-formed instead of
        guessed from the discovery note.
        """

        if not self.economy_mode:
            return
        families = self._active_families()
        if families is None:
            return
        family = family_of(core_name)
        if family in families:
            return
        self._expanded_families.add(family)
        self._refresh_tool_advertisement()

    def _refresh_tool_advertisement(self) -> None:
        """Recompute the advertised tool tuple and its measured economy.

        Deterministic by construction: permitted pairs keep their sorted
        order, so an equal selection always renders a byte-identical schema
        payload and the prefix cache key stays stable across the run. The
        omitted bytes are measured on every refresh even when deferral is not
        applied, which is what makes an OFF vs ON comparison honest.
        """

        pairs = self._permitted_tool_pairs
        tools = self._provider_tools
        families = self._active_families()
        if families is None:
            included = list(range(len(pairs)))
        else:
            included = [
                index
                for index, (_alias, core_name) in enumerate(pairs)
                if family_of(core_name) in families
            ]
        included_set = set(included)
        omitted = [index for index in range(len(pairs)) if index not in included_set]

        def payload(indexes: List[int]) -> List[Dict[str, Any]]:
            return [
                {
                    "name": tools[index].name,
                    "description": tools[index].description,
                    "parameters": tools[index].input_schema,
                }
                for index in indexes
            ]

        included_payload = payload(included)
        self._tool_schemas_included = len(included)
        self._tool_schemas_omitted = len(omitted)
        self._tool_schema_bytes_included = len(
            json.dumps(
                included_payload, ensure_ascii=False, sort_keys=True
            ).encode("utf-8")
        )
        self._tool_schema_bytes_avoided = (
            len(
                json.dumps(
                    payload(omitted), ensure_ascii=False, sort_keys=True
                ).encode("utf-8")
            )
            if omitted
            else 0
        )
        applied = self.economy_mode and families is not None
        if applied and omitted:
            omitted_names: Dict[str, List[str]] = {}
            for index in omitted:
                alias, core_name = pairs[index]
                omitted_names.setdefault(family_of(core_name), []).append(alias)
            self._universe_note = discovery_note(omitted_names)
        else:
            self._universe_note = ""
        active_payload = included_payload if applied else payload(
            list(range(len(pairs)))
        )
        self._advertised_provider_tools = (
            tuple(tools[index] for index in included) if applied else tools
        )
        rendered = json.dumps(active_payload, ensure_ascii=False, sort_keys=True)
        self._tool_schema_bytes_advertised = len(rendered.encode("utf-8"))
        # A fresh deduplicator per refresh: the previous advertisement's seen
        # set must not make the new unique-bytes measurement collapse to zero.
        self._schema_dedup = ToolSchemaDeduplicator()
        self._tool_schema_bytes_unique = len(
            json.dumps(
                self._schema_dedup.deduplicate(active_payload),
                ensure_ascii=False,
                sort_keys=True,
            ).encode("utf-8")
        )
        self._tool_schema_digest = hashlib.sha256(
            rendered.encode("utf-8")
        ).hexdigest()

    def _request_system_prompt(self) -> str:
        """The system prompt one request actually carries.

        The discovery note rides on the request, never on the stored history:
        durable history keeps the plain SYSTEM_PROMPT contract, and a run
        whose universe never changes keeps a byte-identical prefix.
        """

        if self._universe_note:
            return self.system_prompt + "\n" + self._universe_note
        return self.system_prompt

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
            self._select_tool_universe(record.task)
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
                previous_prefix_key = self._prefix_cache.last_key
                prefix_key = self._prefix_cache.compute_key(
                    system_prompt=self._request_system_prompt(),
                    tool_schemas=self._tool_schema_digest,
                    session_id=session_id,
                )
                self._prefix_total_steps += 1
                if (
                    previous_prefix_key is not None
                    and previous_prefix_key.key == prefix_key.key
                ):
                    self._prefix_stable_steps += 1
                messages = tuple(self._request_messages(record.provider_history))
                # The dynamic tail is everything except the cacheable prefix;
                # measuring it per step keeps prefix churn distinguishable
                # from a genuinely growing transcript on the usage surface.
                self._cache_decision = self._cache_scheduler.decide(
                    prefix_key=prefix_key,
                    previous_key=previous_prefix_key,
                    dynamic_chars=sum(
                        len(message.content or "")
                        for message in messages
                        if message.role != "system"
                    ),
                )
                self._context_tier = self._cost_governor.evaluate_context(
                    estimated_input_tokens=int(
                        sum(len(message.content or "") for message in messages)
                        / float(self.context.chars_per_token)
                    ),
                    pricing=self._cache_scheduler.pricing,
                )
                request = ModelRequest(
                    model=self.model,
                    messages=messages,
                    tools=self._advertised_provider_tools,
                    deadline_seconds=min(remaining, 3600.0),
                    # Every step resends the whole transcript, so the stable
                    # prefix (system prompt + tool schemas) plus the session id
                    # is exactly the right cache key: an unchanged prefix keeps
                    # billing at cache-read rate, and a changed prefix stops
                    # advertising a cache entry the provider no longer holds.
                    cache_key=prefix_key.key,
                    max_output_tokens=self.max_output_tokens,
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
                if len(response.tool_calls) > 1:
                    self._batched_turns_avoided += len(response.tool_calls) - 1
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

    @staticmethod
    def _reasoning_entries(response: ModelResponse) -> List[Dict[str, Any]]:
        """Store the model's deliberation in the form the provider will accept back.

        This is content transport, not display: the same distinction file content
        already relies on. Known secret values KaroX holds are still removed, but
        nothing is pattern-rewritten or clamped, because a signed block that no
        longer matches what was signed is rejected. If a removal did change the
        bytes, the block is kept for the record and marked unreplayable rather
        than sent and refused.
        """

        entries: List[Dict[str, Any]] = []
        for block in response.reasoning_blocks:
            text = str(redact_content(block.text))
            data = str(redact_content(block.data))
            entries.append(
                {
                    "kind": block.kind,
                    "text": text,
                    "data": data,
                    "signature": block.signature,
                    "replayable": text == block.text and data == block.data,
                }
            )
        return entries

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
        reasoning_blocks = self._reasoning_entries(response)
        usage_event = usage_event_from_response(
            session_id=session_id,
            step=self._step,
            response=response,
        )
        reused_chars = int(getattr(self, "_economy_reused_chars_pending", 0))
        if reused_chars > 0:
            usage_event["economy_reused_chars"] = reused_chars
            usage_event["economy_reused_estimated_tokens"] = int(
                reused_chars / float(self.context.chars_per_token)
            )
        # Quality-economy accounting (P0.4): proxy metrics measured in this
        # process. Token savings are never invented here; provider-native
        # cached-token counts arrive in the usage payload when they exist.
        self._cost_ledger.record(
            session_id=session_id,
            step=self._step,
            provider=str(response.selected_provider or self.provider.provider_name),
            model=str(response.selected_model or self.model),
            input_tokens=int(usage_event.get("prompt_tokens", 0)),
            output_tokens=int(usage_event.get("completion_tokens", 0)),
            cache_read_tokens=int(usage_event.get("cache_read_tokens", 0)),
            cache_write_tokens=int(usage_event.get("cache_write_tokens", 0)),
            cost_usd=float(usage_event.get("cost", 0.0) or 0.0),
            timestamp=float(usage_event["timestamp"]),
        )
        usage_event["economy_tool_schema_bytes_advertised"] = (
            self._tool_schema_bytes_advertised
        )
        usage_event["economy_tool_schema_bytes_unique"] = (
            self._tool_schema_bytes_unique
        )
        usage_event["economy_prefix_stable_steps"] = self._prefix_stable_steps
        usage_event["economy_prefix_total_steps"] = self._prefix_total_steps
        # Cache-aware scheduler: feed provider-reported cached counts back as
        # the only accepted capability evidence, then surface measured
        # counters. The estimated saving appears only when the registry
        # knows both the input and cached-input rates; otherwise the field
        # is absent, which the usage surface renders as UNAVAILABLE.
        routed_model = str(response.selected_model or self.model)
        if routed_model != self._cache_pricing_model:
            self._cache_pricing_model = routed_model
            registry = self._pricing_registry
            self._cache_scheduler.set_pricing(
                None
                if registry is None
                else registry.lookup(
                    str(response.selected_provider or self.provider.provider_name),
                    routed_model,
                )
            )
        self._cache_scheduler.observe_usage(
            cache_read_tokens=int(usage_event.get("cache_read_tokens", 0)),
            cache_write_tokens=int(usage_event.get("cache_write_tokens", 0)),
        )
        cache_decision = self._cache_decision
        if cache_decision is not None:
            usage_event["economy_cache_verdict"] = cache_decision.verdict
            usage_event["economy_cache_prefix_sha"] = cache_decision.prefix_sha
            usage_event["economy_cache_invalidations"] = (
                self._cache_scheduler.invalidations
            )
            usage_event["economy_cache_capability"] = (
                self._cache_scheduler.capability.label()
            )
            usage_event["economy_cache_read_tokens_total"] = (
                self._cache_scheduler.cache_read_tokens_total
            )
            usage_event["economy_cache_write_tokens_total"] = (
                self._cache_scheduler.cache_write_tokens_total
            )
            cache_saving = self._cache_scheduler.estimated_reuse_saving_usd()
            if cache_saving is not None:
                usage_event["economy_cache_saving_estimated_usd"] = cache_saving
        context_tier = self._context_tier
        if context_tier is not None:
            usage_event["economy_context_tier"] = context_tier.tier
            if context_tier.threshold_tokens is not None:
                usage_event["economy_context_tier_threshold"] = (
                    context_tier.threshold_tokens
                )
                usage_event["economy_context_tier_estimated_input"] = (
                    context_tier.estimated_input_tokens
                )
            if context_tier.tier in (
                ContextTierDecision.TIER_APPROACHING,
                ContextTierDecision.TIER_LONG,
            ):
                usage_event["economy_context_tier_reason"] = context_tier.reason
        usage_event["economy_batched_turns_avoided"] = self._batched_turns_avoided
        if self._continuity_statements_carried:
            usage_event["economy_continuity_statements"] = (
                self._continuity_statements_carried
            )
            usage_event["economy_continuity_chars"] = (
                self._continuity_chars_carried
            )
        selection = self._universe_selection
        if selection is not None:
            # Measured on every run; "applied" separates shadow measurement
            # from the economy-mode advertisement that actually omits bytes.
            usage_event["economy_tool_universe_applied"] = bool(self.economy_mode)
            usage_event["economy_tool_groups_selected"] = ",".join(
                selection.families
            )
            usage_event["economy_tool_schemas_included"] = (
                self._tool_schemas_included
            )
            usage_event["economy_tool_schemas_omitted"] = (
                self._tool_schemas_omitted
            )
            usage_event["economy_tool_schema_bytes_included"] = (
                self._tool_schema_bytes_included
            )
            usage_event["economy_tool_schema_bytes_avoided"] = (
                self._tool_schema_bytes_avoided
            )
            if self._expanded_families:
                usage_event["economy_tool_groups_expanded"] = ",".join(
                    sorted(self._expanded_families)
                )
        usage_event["economy_read_cache_hits"] = self._read_cache.hits
        usage_event["economy_read_cache_misses"] = self._read_cache.misses
        compiled = self._context_compilation
        if compiled is not None:
            usage_event["economy_context_items"] = compiled.stats.items_total
            usage_event["economy_context_included"] = compiled.stats.included
            usage_event["economy_context_referenced"] = compiled.stats.referenced
            usage_event["economy_context_elided_stale"] = (
                compiled.stats.elided_stale
            )
            usage_event["economy_context_chars_in"] = compiled.stats.chars_in
            usage_event["economy_context_chars_out"] = compiled.stats.chars_out
            usage_event["economy_context_applied"] = bool(self.economy_mode)
        stale_chars = int(getattr(self, "_economy_stale_chars_pending", 0))
        if stale_chars > 0:
            usage_event["economy_stale_chars"] = stale_chars
            usage_event["economy_stale_estimated_tokens"] = int(
                stale_chars / float(self.context.chars_per_token)
            )
        decision = self._cost_governor.evaluate(
            current_cost_usd=self._cost_ledger.total_cost(session_id)
        )
        if decision.warning:
            # Advisory only: the governor runs in shadow mode, never blocks a
            # run, and never downgrades the model. The warning is recorded so
            # the session usage view can surface it.
            usage_event["economy_budget_warning"] = decision.warning

        def update(record: SessionRecord) -> None:
            if route_audit is not None:
                record.provider_history.append(dict(redact(route_audit)))
            stored = dict(redact(entry))
            if reasoning_blocks:
                # Attached after the display-redaction pass, never through it. That
                # pass rewrites secret-shaped text and clamps long strings, which is
                # right for anything shown to a human and fatal here: a signature is
                # base64url and can contain a token-shaped run, and a rewritten
                # block is one the provider refuses.
                stored["reasoning_blocks"] = reasoning_blocks
            record.provider_history.append(stored)
            # Root-agent and read-only subagent responses share one accounting
            # implementation so Recursive Context can never become invisible
            # spend in the session Usage view.
            record.usage = merge_response_usage(
                record.usage,
                response,
                event=usage_event,
                model_fallback=self.model,
            )

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
            "uncached_cost": response.uncached_cost,
            "cache_savings": response.cache_savings,
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
            self._emit(
                AgentEventKind.WARNING,
                tool=display,
                call_id=call.call_id,
                reason="repeated_action",
                detail="identical call with unchanged results",
            )
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
        # Deferred universe discovery: an omitted family the model reached for
        # by exact name gets its schemas attached from the next step.
        self._note_tool_demand(core_name)
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
        if core_name in _TEST_TOOL_NAMES:
            self._emit(
                AgentEventKind.TEST_STARTED, tool=display, call_id=call.call_id
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
        for derived_kind, derived_fields in _derived_tool_events(core_name, result):
            self._emit(
                derived_kind, tool=display, call_id=call.call_id, **derived_fields
            )
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
        if core_name in {"repo.read_file", "repo.read_lines"} and result.ok:
            read_data = result.data if isinstance(result.data, dict) else {}
            read_path = read_data.get("path")
            read_size = read_data.get("bytes")
            read_digest = read_data.get("content_sha256") or read_data.get("sha256")
            if (
                isinstance(read_path, str)
                and isinstance(read_size, int)
                and isinstance(read_digest, str)
                and read_digest
            ):
                # The content digest is the fingerprint: Core reads return no
                # mtime, and identical bytes are exactly what makes a re-read
                # redundant. A hit means the transcript already carries this
                # content, which the request-side reuse pass then elides.
                self._read_cache.check(
                    read_path, float(int(read_digest[:12], 16)), read_size
                )
        content = json.dumps(
            result_value,
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
        self._emit(
            AgentEventKind.ERROR,
            tool=self._tool_aliases.get(call.name, call.name),
            call_id=call.call_id,
            reason=error_type,
            detail=str(redact(message))[:400],
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
                reasoning_blocks=_reasoning_blocks(entry) if role == "assistant" else (),
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
        messages: List[ModelMessage] = []
        for message in self._messages(self._compact(list(history))):
            if not replaced and message.role == "system":
                replaced = True
                messages.append(ModelMessage("system", self._request_system_prompt()))
            elif message.role == "tool" and message.content is not None:
                messages.append(
                    ModelMessage(
                        role="tool",
                        content=self._clip(
                            message.content, self.context.max_tool_result_chars
                        ),
                        tool_call_id=message.tool_call_id,
                    )
                )
            else:
                messages.append(message)
        # Typed context IR: every retained item receives an explicit
        # INCLUDE / REFERENCE / ELIDE verdict with a recorded reason. The
        # decisions are measured on every request; the rewrite below is
        # applied only in economy mode. Economy never alters the selected
        # model, reasoning effort, or quality limits, and every replaced
        # item stays reachable through the marker that replaced it.
        calls: Dict[str, tuple[str, str]] = {}
        for message in messages:
            for call in message.tool_calls:
                calls[call.call_id] = (call.name, call.raw_arguments)
        items: List[ContextItem] = []
        for index, message in enumerate(messages):
            tool_name: str | None = None
            tool_arguments: str | None = None
            if message.role == "tool" and message.tool_call_id:
                known = calls.get(message.tool_call_id)
                if known is not None:
                    tool_name, tool_arguments = known
            items.append(
                ContextItem(
                    index=index,
                    role=message.role,
                    content=message.content
                    if isinstance(message.content, str)
                    else None,
                    tool_call_id=message.tool_call_id,
                    tool_name=tool_name,
                    tool_arguments=tool_arguments,
                )
            )
        compiled = self._context_compiler.compile(items)
        self._context_compilation = compiled
        self._economy_reused_chars_pending = 0
        self._economy_stale_chars_pending = 0
        if not self.economy_mode:
            return messages
        rewritten: List[ModelMessage] = []
        for message, decision in zip(messages, compiled.decisions):
            if (
                message.role == "tool"
                and decision.replacement is not None
                and decision.verdict is not Verdict.INCLUDE
            ):
                if decision.verdict is Verdict.ELIDE_STALE:
                    self._economy_stale_chars_pending += decision.chars_saved
                else:
                    self._economy_reused_chars_pending += decision.chars_saved
                rewritten.append(
                    ModelMessage(
                        role="tool",
                        content=decision.replacement,
                        tool_call_id=message.tool_call_id,
                    )
                )
            else:
                rewritten.append(message)
        return rewritten

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
        before = self._estimated_tokens(history)
        if before <= ceiling:
            return list(history)
        keep = min(self.context.keep_recent_groups, len(groups))
        for cut in range(1, len(groups) - keep + 1):
            digest = self._context_summary(groups[:cut])
            kept = [entry for group in groups[cut:] for entry in group]
            candidate = head + [digest] + kept
            after = self._estimated_tokens(candidate)
            if after <= ceiling:
                self._record_compaction(before, after, cut, fitted=True)
                return candidate
        # Even the floor of recent turns exceeds the ceiling. Keep that floor:
        # clipping in _request_messages still applies, and discarding the most
        # recent evidence would be worse than sending a large request.
        older = groups[:-keep]
        if not older:
            return list(history)
        result = (
            head
            + [self._context_summary(older)]
            + [entry for group in groups[-keep:] for entry in group]
        )
        self._record_compaction(
            before, self._estimated_tokens(result), len(older), fitted=False
        )
        return result

    def _record_compaction(
        self, before: int, after: int, turns: int, *, fitted: bool
    ) -> None:
        """Say that the model's context was rewritten, and by how much.

        Compaction used to be entirely invisible: nothing in the report, the
        stream or the record said the middle of the conversation had been
        replaced, so a run that went wrong afterwards could not be explained.
        ``fitted`` is False when even the floor of recent turns is over the
        ceiling -- an honest 'this is still too big' rather than a silent pass.
        """

        self._compactions += 1
        # The counts are spelled ``*_tokens`` because that is the one spelling the
        # credential-name redactor recognises as a counter; anything else
        # containing "token" comes out of the JSON report as [REDACTED].
        self._last_compaction = {
            "count": self._compactions,
            "before_tokens": before,
            "after_tokens": after,
            "turns_summarized": turns,
            "within_ceiling": fitted,
            "ceiling": self.context.token_ceiling,
        }
        self._emit(
            AgentEventKind.COMPACTED,
            summary=(
                f"summarized {turns} earlier turn(s): ~{before} tokens to ~{after}"
                + ("" if fitted else ", still over the ceiling")
            ),
            ok=fitted,
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
            # Replayed deliberation is sent in full and cannot be clipped without
            # invalidating its signature, so it has to be counted or a long
            # thinking phase would push the request past the window while this
            # estimate said it fitted.
            for block in _reasoning_blocks(entry):
                characters += len(block.text) + len(block.data)
        return int(characters / float(self.context.chars_per_token)) + 1

    def _context_summary(
        self, groups: List[List[Dict[str, Any]]]
    ) -> Dict[str, Any]:
        steps = 0
        tools: Counter[str] = Counter()
        paths: List[str] = []
        failures: List[str] = []
        evidence: List[str] = []
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
                for path in _summary_paths(data):
                    if path not in paths:
                        paths.append(path)
                # The evidence IDs are the whole point of this product's
                # verification story. A summary that dropped them left the model
                # unable to name what it had already proved, and the design
                # comment above claims exactly that cannot happen.
                for identifier in _summary_evidence_ids(result):
                    if identifier not in evidence:
                        evidence.append(identifier)
        used = ", ".join(f"{name} x{count}" for name, count in sorted(tools.items()))
        lines = [
            "KaroX summarized the earliest turns of this session to stay inside "
            "the model context window. The summary below is computed from the "
            "recorded tool results, not from any narration.",
            f"Turns summarized: {steps}.",
            f"Tools used: {used or 'none'}.",
        ]
        if paths:
            lines.append("Files involved: " + _joined(paths) + ".")
        if evidence:
            lines.append("Evidence already recorded: " + _joined(evidence) + ".")
        if failures:
            lines.append("Failures: " + _joined(failures, separator="; ") + ".")
        carried, continuity = continuity_lines(groups)
        if carried:
            # The model's own words, clearly fenced off from tool evidence:
            # continuity must never let narration masquerade as proof.
            lines.append(
                "Your own earlier conclusions, quoted verbatim from the "
                "summarized turns (model narration, not verified evidence):"
            )
            lines.extend("- " + statement for statement in carried)
        if continuity.reasoning_blocks_dropped:
            lines.append(
                f"{continuity.reasoning_blocks_dropped} provider reasoning "
                "blocks were dropped with the summarized turns; the quoted "
                "conclusions above are the only narration that survives."
            )
        self._continuity_statements_carried += continuity.statements_carried
        self._continuity_chars_carried += continuity.statement_chars
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
        self._emit(AgentEventKind.PHASE_CHANGED, phase=phase)

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
            compaction=dict(self._last_compaction) if self._last_compaction else None,
            mode=self.mode,
        )

    def _completed(self, record: SessionRecord) -> bool:
        """True when this run reached either terminal outcome KaroX recognises."""
        return self._verification(record) or self._answer_complete(record)

    def _answer_complete(self, record: SessionRecord) -> bool:
        """True when a read-only task has produced a trustworthy final answer.

        Repository questions still require at least one successful inspection:
        narrative is not evidence. A tiny, explicitly conversational task such
        as ``привет`` is different -- there is no repository fact to inspect, so
        forcing an ``answer_prompt`` would spend a second provider call only to
        make the model explain that no repository change was needed.
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
        if _is_small_talk_task(record.task):
            return True
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
