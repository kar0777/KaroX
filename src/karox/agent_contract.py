"""Stable vocabulary shared by the agent kernel, the CLI and the TUI.

The report, the event stream, the limits and the context budget are the
contract between the loop and everything that watches it; keeping them in one
small module means a reader can learn the surface without reading the kernel.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, Optional

from .security import redact


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


#: Named context budgets a run can choose between. The number is the *window*
#: the loop budgets against, before ``utilization`` takes its share; ``full``
#: means the model's own advertised window and is the only tier that is not a
#: number, because a policy cannot know what a model advertises.
#:
#: The tiers exist because one number cannot serve every model. A 1M-token model
#: can hold a session whole, but every turn resends that prefix, so a budget that
#: large is paid for on every step whether or not the task needed it. 272k is the
#: small tier: more than four times the largest session measured in this
#: repository (a sixty-minute run with six full rewrites of one file reached
#: ~134k tokens before draft elision), so compaction stays rare, while the prefix
#: stays inside what a subscription endpoint will actually bill comfortably.

#: What an unknown window resolves to. A model that advertises nothing gets a
#: budget rather than a claim: this is the small tier, and a provider that
#: rejects the request size teaches the loop the real one instead of failing.












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
    #: Where ``max_input_tokens`` came from, so the loop can be honest about
    #: whether it knows the model's window or merely chose a budget:
    #: ``advertised`` (the registry states the model's window), ``tier`` (the run
    #: was given one), ``fallback`` (nobody stated one, so this is the default),
    #: ``unstated`` (nothing was resolved; older callers and tests).
    window_source: str = "unstated"

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
        """True only when the window is a fact about the model, not a choice.

        A run that budgets 272k against a model nobody described does not know
        the model's window; it picked a number. Saying otherwise would let a
        guess be reported as a capability.
        """

        if self.window_source == "advertised":
            return True
        if self.window_source == "unstated":
            return self.max_input_tokens is not None
        return False

    def with_window(self, window: Optional[int], *, source: str) -> "ContextBudget":
        """Return this budget against another window, for a learned one.

        A provider that rejects a request for its size has just told the loop
        something no registry said. The budget is frozen, so adapting means
        replacing it rather than mutating it -- and the source travels with the
        number so the reason survives into the report.
        """

        return ContextBudget(
            max_input_tokens=window,
            fallback_input_tokens=self.fallback_input_tokens,
            utilization=self.utilization,
            keep_recent_groups=self.keep_recent_groups,
            max_tool_result_chars=self.max_tool_result_chars,
            chars_per_token=self.chars_per_token,
            window_source=source,
        )


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
    #: Set when the run ended worse than a state it had already proved, and the
    #: files were put back to that state before reporting.
    restored: Optional[str] = None
    #: The completion-floor reasons still open when the run stopped. A user
    #: should not have to open the session to learn why a run is not verified.
    unmet: tuple[str, ...] = ()
    #: The parsed failure map of the last approved check that failed, when one
    #: did. It names exact file:line and test ids; the check result carries the
    #: same shape, so a reader can act on the report without a second lookup.
    failure_map: Optional[Dict[str, Any]] = None
    # The stance the run was started with (--mode / the TUI\'s /mode). None is
    # the legacy default: a run created without the flag behaves as Build and
    # claims nothing beyond that.
    mode: Optional[str] = None
    # Names the decision loop that produced this report: the engine-backed
    # kernel stamps the vendored karo-core identity here, the legacy v5 loop
    # leaves it None. The report is the in-band proof of which loop ran; the
    # engine's trajectory artifact is the durable one.
    engine: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return dict(redact(asdict(self)))



class AgentEventKind(str, Enum):
    """What a watcher can be told while a run is still in progress."""

    STEP_STARTED = "step_started"
    TEXT_DELTA = "text_delta"
    # The model's own deliberation, on its own channel. Merging it into
    # TEXT_DELTA would present private reasoning as the answer.
    REASONING_DELTA = "reasoning_delta"
    # Provider-labeled high-level reasoning summary. Unlike REASONING_DELTA,
    # this channel is explicitly safe for public progress UI.
    REASONING_SUMMARY_DELTA = "reasoning_summary_delta"
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
    # Provider-labeled high-level reasoning summary: the channel the provider
    # adapter streams and the TUI renders. It is its own field, not `summary`,
    # because `summary` belongs to tool/compaction outcomes.
    reasoning_summary_delta: Optional[str] = None
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






