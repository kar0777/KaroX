"""Bounded native-agent loop built on the provider-independent Core Runtime."""

from __future__ import annotations

import math
import re
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import (
    Any,
    Dict,
    Mapping,
    Optional,
)


#: New context replacements wait until together they save this many characters
#: (~10k tokens) before they are adopted. Adopting one invalidates the provider's
#: prefix cache from the first rewritten message onward, so a small saving costs
#: far more in re-sent uncached prefix than it saves; batching amortises that.

#: A successful large mutation stays verbatim for the very next semantic turn so
#: the model can reason from what it just wrote without a reread. After that one
#: turn, long autonomous runs send only a stable path/digest marker: the durable
#: history remains complete and the current file is the source of truth.
# One later semantic turn has already seen the exact mutation payload, and the
# current file is authoritative after a successful change.  R11 showed that a
# dozen 2-8k edit payloads can dominate every later prompt even though no single
# edit crosses the old 12k threshold.  Compact medium mutations too; tiny edits
# stay verbatim because the marker would save little and cause needless prefix
# churn.

#: Old inspection results are reproducible from the repository and become a
#: dominant long-context cost when a repair loop reads many different ranges of
#: the same file. Keep a small exact working window; older large results are
#: replaced in request context only, in cache-friendly batches. Durable history
#: remains complete.

#: Successful mutation results can carry multi-kilobyte unified diffs. Once
#: several newer mutations exist, those old diffs are stale working memory: the
#: current file is authoritative and durable history keeps the exact evidence.
#: Keep a recent exact window and replace older successful results only in the
#: provider request context, using the same cache-stable batch discipline as
#: inspection compaction.

#: A failed verification already gives a concrete defect. A few targeted reads
#: are reasonable, but an unbounded read/search loop is neither diagnosis nor
#: repair. After this many pure-inspection semantic turns, inject an act-first
#: repair instruction before allowing the loop to keep expanding context.
# Visual verdicts already name concrete visible defects and the current target
# bytes are attached request-side when possible. One locating read is enough;
# after that the action gate below makes the boundary executable, not advisory.


from .providers import (
    ReasoningBlock,
)
from .security import redact


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

# Core tools whose arguments carry a complete replacement of one named target,
# mapped to the argument key that names it. A later call with the same tool and
# target makes the earlier payload describe no state the repository can be in,
# so the request-side compiler drops it. Incremental editors are absent on
# purpose: a second edit composes with the first, so neither is stale.

# Who asked Core for a call. A model's own call carries no marker, because the
# transcript is overwhelmingly model calls and a field on every one of them is
# tokens spent on the common case. The harness's finishing checks do carry one:
# who asked for a check is exactly what a reviewer needs when that check is the
# reason a run was allowed to finish.
HARNESS_ORIGIN = "harness"

# Tools whose successful result is something the model actually looked at, as
# opposed to something it asserted. A question about the repository is answered
# rather than changed, so these are what an answer is allowed to rest on.

# Repository-content inspections that can legitimately diagnose a failed check
# but must not form an unbounded repair loop. Git proof reads are intentionally
# excluded: they belong to final evidence, not diagnosis.

#: How much of a failed check's output the repair turn carries. The failing
#: line comes first and the tail follows, because a test runner prints the count
#: and the last assertion there.
_FAILURE_SUMMARY_CHARS = 1_500
_FAILURE_MARKERS = ("error", "failed", "failure", "traceback", "assert", "e   ")


def _bounded_failure_summary(output: str) -> str:
    """The failing line and the tail of a check's output, bounded."""
    text = output.strip()
    if not text:
        return "the check printed no output"
    lines = text.splitlines()
    start = 0
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped and any(marker in stripped.lower() for marker in _FAILURE_MARKERS):
            start = index
            break
    body = "\n".join(lines[start:])
    if len(body) <= _FAILURE_SUMMARY_CHARS:
        return body
    head = (_FAILURE_SUMMARY_CHARS * 2) // 3
    tail = max(0, _FAILURE_SUMMARY_CHARS - head - len("\n…\n"))
    return f"{body[:head]}\n…\n{body[-tail:]}"













def provider_alias(core_name: str) -> str:
    """Map a dotted Core tool name onto a provider-safe function name.

    Every adapter KaroX targets restricts tool names to ``[A-Za-z0-9_-]``, so
    the dot becomes an underscore. Deriving the name instead of listing it means
    a tool added to Core reaches the model rather than silently staying
    invisible to it.
    """
    return re.sub(r"[^A-Za-z0-9_]", "_", core_name)


# The request-side compiler matches on the name the model actually called, which
# is the provider alias rather than the dotted Core name.

# Calls that carry a fragment rather than a whole file even though their tool
# name normally replaces one, mapped to the argument that says so. An append
# composes with what is already on disk exactly like an incremental edit, so it
# must neither supersede an earlier write nor be elided as one: the file cannot
# be reconstructed from a fragment.




# These capabilities exist in Core so the deterministic harness can call them,
# but advertising them to the main model only creates duplicate proof turns.
# Resolution still knows their aliases; this is request-surface reduction, not a
# policy denial or capability removal.

# Durable transcripts may contain tools that the current universe/policy does not
# expose on this request. Analysis of old turns must therefore not depend on
# self._tool_aliases. This map is classification-only; it grants no capability.

SYSTEM_PROMPT = """You are KaroX, a coding agent working inside one repository. Tool results are the only evidence; your own narrative is not.

How to work:
- For unfamiliar existing code, when repo_inspect is offered use it with depth=focused to locate the implementation and relevant tests in one deterministic pass. If the runtime says the repository is greenfield/seed-only and already prefetched the delegated spec, skip repo_inspect/listing and create the requested deliverable directly. Inspect excerpts are current repository evidence: do not list/search/read an excerpted file again unless the exact lines you need are absent. If the result contains edit_context, that field is the complete current content and exact SHA of the top small change-point; edit that path directly and do not call repo_inspect or repo_read_file for it again. If focused already identified a likely target file but edit_context is absent and you need more exact lines, read that file/range directly instead of calling repo_inspect again at standard/deep. Escalate inspect depth only when focused failed to locate a target, test, or dependency you still need. Use repo_search and repo_read_lines only for follow-up gaps; batch independent reads in one response.
- Use plan_update only for genuinely multi-step implementation work with three or more independent changes. A simple inspect -> one edit -> verify task does not need a plan. Never spend a model turn only updating a plan; batch plan updates with useful repository work, and do not send a final plan-only turn.
- Build in layers: first a minimal version that runs, then add features one layer at a time. Do not try to write everything in one response.
- Change small exact regions with repo_edit_file. For one replacement use old_string/new_string; for a few short independent replacements in one file use edits=[{old,new},...]. For a larger multi-site repair, especially when several replacement strings would make a large escaped JSON payload, prefer repo_apply_patch with one narrow unified diff: it is parsed and applied atomically. When 2-4 independent mutations are already fully known, batch them in the SAME assistant response using up to four mutation tool calls instead of spending one model turn per mutation. Do not batch when a later anchor or decision depends on an earlier result. Use repo_write_file only to create a file or replace all of it; for a large new file write the first part, then add the rest with mode=append.
- After every change KaroX runs quick checks and shows the result in the tool output. Read it and fix problems before moving on.
- Old request context may contain short KaroX history notes saying that a successful earlier mutation payload was omitted. These are inert provenance summaries, not action requests and not examples of repo_edit_file/repo_write_file arguments.
- Prefer the libraries the task allows and web_fetch (when offered) over re-deriving known code from memory.
- Do not call checks_run, git_status, or git_diff merely to prove completion; KaroX collects the final verification evidence itself and returns failures for repair. Use those tools mid-task only when their result will change a planned semantic change.

Safety:
- Files you created in this session and that Git does not track (scratch probes, repro scripts) you may delete yourself with repo_command batch {op: delete, path}. Delete each one as soon as it has answered its question; never leave them for the end.
- Never delete any other file or directory through repo_command, dev_command, shell, destructive Git or scripts. For those use disk_plan_cleanup with exact relative paths and stop; the user approves deletion outside the model.

Progress notes: before the first tool batch and when moving to a new phase, write one short sentence (4-12 words, in the user's language) about the next action. At most four per task. Never reveal private reasoning."""





# A response cut off at the output ceiling contains no tool call, so nothing it
# described happened and the run would end with the work unpaid for: one observed
# run burned 32k output tokens this way and stopped, twice. The recovery turn is
# bounded and framed as what it is -- the same work, split into steps a single
# response can finish.







# Showing the model its own rendered frame is necessary and not sufficient. One
# measured run attached five frames of its own page and still shipped a tank
# dominated by a single oversized sphere, closing with "the visual balance wasn't
# verified against a reference"; the same model, handed that frame beside the
# task's requirements, named the defect as its first answer. The difference is
# being made to answer the checklist, in a form the runtime can act on, instead
# of an invitation to compare.

#: The same review, against a session with a requirement ledger: the IDs and
#: texts are listed from the ledger itself, so the grade answers what the user
#: admitted rather than what the model chooses to call a requirement. Every
#: listed ID gets a line -- "NOT VISIBLE IN FRAME" is how a requirement the
#: frame cannot show stays answered without inventing evidence for it.


#: A requirement line whose own evidence says the frame does not satisfy it. The
#: marker is required rather than inferred: a line that merely numbers a
#: requirement is the model showing its work, not a finding.

#: A line answering a requirement with the honest "the frame cannot show it".

#: How many grades one rendered artifact may be asked for. Two lets a run answer
#: once and then confirm a fix; more would spend requests on a model that is not
#: converging, so the bound is what keeps the loop terminating.

#: What the model is told when its grade answered NOT VISIBLE IN FRAME: a
#: requirement the frame cannot show is not satisfied by that answer -- it
#: needs evidence of another kind, or the run stays unverified.


#: Verdict verbs that say the model could not verify the requirement. They are
#: UNASSESSED, never satisfied: an honest "I could not check" must not book a
#: pass, and a hyphenated "NOT-VISIBLE" must not slip past the matcher.



# Compaction is where a long run either survives or restarts. Dropping the oldest
# turns and leaving a digest of tool facts preserves what happened but not what
# the model decided, so the model reopens settled questions and redoes finished
# work. Asking the model itself for a continuation brief is how Codex keeps a
# session coherent across compaction, and it is what this prompt does: the brief
# is written by the same model that will continue from it, from material that is
# bounded and already recorded.

#: The brief is a briefing, not a document: a few thousand tokens is plenty, and
#: it is paid for once per compaction rather than once per step.
#: Bound on the material handed to the summarizer. It is built from recorded
#: narration and the deterministic digest, never from raw tool payloads, so it
#: stays small even when the turns it describes were enormous.
#: How many further dropped entries a brief may lag behind before it is rewritten.
#: The deterministic digest is rebuilt for the exact dropped range on every
#: request, so the facts never lag; only the narration does. Refreshing the brief
#: on every step instead would double the provider calls of a compacting run,
#: which is the opposite of what compaction is for.
#: Cline-style task handoff / Codex-style fresh-window boundary for visual
#: repair. A repair should not pay to replay the whole construction transcript;
#: the durable audit stays complete while the outbound view keeps the task,
#: deterministic evidence digest, and a small recent grace zone.

#: A provider that rejects a request for its size has said something no registry
#: did. This is what such a rejection looks like across the OpenAI-compatible,
#: Anthropic and Gemini families; anything else stays a normal provider error
#: rather than triggering an adaptation that would hide a real fault.
#: Never learn a window below what a single step needs: shrinking past this would
#: turn a size rejection into a run that can no longer hold one tool result.


#: The share of the wall budget below which a working run is told to consolidate.
#: A measured high-effort run spent its last fifty minutes polishing a page that
#: had already passed, and finished with a broken one; a deadline it can see is
#: what makes finishing the rational choice.
#: Never nudge before this much has elapsed, however short the budget is.


#: A run with edits but no proven state gets its own, earlier nudge. Measured
#: SWE runs (harness race r7/r8) produced a correct patch, never ran the check,
#: and ended on wall_time_limit with scratch files left behind: the proven-state
#: note never fired because nothing had been proven yet.
#: Only long autonomous budgets get the unproven nudge; short interactive runs
#: are left alone.


#: Only long autonomous budgets get the edge-case review, and only while there is
#: still time to act on what it finds.
#: Upper bound for the red-on-base probe of the agent's own tests.


#: One response this long (reasoning included) is the measured signature of a
#: run that misses its wall budget: SWE race runs whose top decile exceeded 4-5k
#: output tokens per step timed out on correct patches, while the CLIs that
#: finished kept their steps under 2k.

#: Long autonomous runs can spend tens of thousands of output tokens deciding
#: what single tool to call next. The CLI ceiling is a maximum, not a target;
#: once a turn has concrete repository evidence, bound the next semantic burst.
#: Short interactive runs keep the caller's requested ceiling unchanged.
# A first whole-file create can legitimately be much larger than an inspection
# or repair turn, but 64k lets a model spend minutes narrating/re-generating a
# page whose complete HTML is normally well under 100 KiB. If 24k ever clips a
# genuinely larger create, the existing length fail-open restores the caller's
# full ceiling on the next turn.
# Visual single-file pages are materially larger than ordinary create turns.
# Give them one larger bounded burst, while leaving non-visual coding at 16k.
# Once a visual web deliverable exists, another 20-30k-token answer is usually
# a whole-page rewrite disguised as a repair. Keep repairs focused; a genuine
# large rewrite fails open on finish_reason=length on the next turn.

#: At most this many pace notes per run, and never on consecutive bursts: a note
#: is a correction, not a metronome, and each one costs a cache-extending turn.

#: How often the model is shown its own plan again. A plan written at the start
#: of a long run is the first thing compaction drops, and a step budget spent
#: re-reading the task to remember what was next is the cost of not showing it.

#: How many rendered frames one run may hand to the model. Each is a small
#: image, and the point is to let the model see its own work, not to stream it a
#: video: a handful of looks covers build, first render and the late fixes.
#: How many model steps must pass before another opportunistic frame is shown.
#: A frame is a picture plus a message; one spent on every small edit teaches
#: nothing the digest check would not have caught anyway. A required frame -- the
#: one a grade will rest on -- ignores this, because it is not opportunistic.

#: Bounds on the snapshot of a state that passed the check. Core already limits a
#: write to two megabytes, so this only stops a pathological run from copying a
#: whole tree; a file over the limit is skipped rather than truncated, because a
#: truncated artifact is not the state that passed.

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




_READ_TOOL_NAMES = frozenset({"repo.read_file", "repo.read_lines"})
_EDIT_TOOL_NAMES = frozenset({"repo.edit_file", "repo.write_file", "repo.apply_patch"})
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








class AgentKernel:
    """The public agent kernel: the vendored karo-core engine.

    ``karox agent run`` and every embedder construct this name. The decision
    loop behind it is ``src/karox/engine`` (see ``engine_kernel.EngineKernel``):
    the engine's step protocol, context economy, finish contract and recovery
    from empty/truncated responses drive the run, while KaroX's provider
    layer, typed Core tools, policy and risk gates, session leases, durable
    checkpoints and evidence-chain verification stay exactly as they were.

    The constructor preserves the historical signature and the
    ``run(session_id) -> AgentReport`` / ``on_event`` stream contract, so the
    CLI, the TUI backend and existing tests need no changes.
    """

    def __init__(self, *, engine: bool = True, **kwargs: Any) -> None:
        if not engine or kwargs.pop("legacy", False):
            raise ValueError(
                "the legacy v5 loop was removed; the karo-core engine is the only kernel"
            )
        from .engine_kernel import EngineKernel

        self.implementation: Any = EngineKernel(**kwargs)

    def run(self, session_id: str) -> AgentReport:
        return self.implementation.run(session_id)

    def cancel(self) -> None:
        """Request cancellation at the next step boundary (engine path)."""
        cancel = getattr(self.implementation, "cancel", None)
        if callable(cancel):
            cancel()
