"""Bounded native-agent loop built on the provider-independent Core Runtime."""

from __future__ import annotations

import re
from typing import (
    Any,
    Dict,
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


from .agent_contract import (
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentLimits,
    AgentReport,
    ContextBudget,
)

__all__ = [
    "AgentError",
    "AgentEvent",
    "AgentEventKind",
    "AgentKernel",
    "AgentLimits",
    "AgentReport",
    "ContextBudget",
    "HARNESS_ORIGIN",
    "REQUIRED_TOOLS",
    "SYSTEM_PROMPT",
    "provider_alias",
]


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

SYSTEM_PROMPT = """You are KaroX, a coding agent working inside one repository with the user toward their goal, using your own judgment along the way. Tool results are the only evidence; your own narrative is not.

IMPORTANT: correctness comes first. Think as much as the problem needs and no more: once you know the next action, take it, and do not re-check in your reasoning what a tool result already showed. Keep visible text and tool arguments short.

How to work:
- Understand before you edit: read the code the task touches and its tests, find the root cause, and list every behaviour the task states or implies, sentence by sentence (exact strings and outputs it shows, every value an attribute or result must carry, the cases next to the example, how the project already handles errors); each one must hold when you finish. When the task names the cause or the intended fix, implement exactly that at the named place; do not substitute a workaround for the symptom or a special case for the reported input. Otherwise make the complete change the task describes, written the way the codebase's maintainers would write it: follow its existing error handling, naming and message style, and add no compatibility shims, wrappers or abstractions the task does not ask for.
- When you have enough information to act, act. Write the code directly in the tool call; do not draft file contents, markup or code in your reasoning first. Do not re-derive facts you already established or narrate options you will not pursue.
- Every step re-sends the whole conversation, so batch independent calls in one response (they run in order) and keep text short. Do not stop while work the task asks for is still owed; stop once the covering tests pass, each stated or implied requirement holds and the variations real use would bring are probed. Finishing early with an unprobed variation is the most common way a run fails; difficulty or uncertainty is a reason to keep working, not to stop.
- Read the project-map block first when the runtime provides one. If it already names the likely implementation files and tests as a small set, read those directly; otherwise find them with repo_search (one search can name several patterns' worth of files) and read only the regions you need. Stop exploring once you can name the code to change and the tests that cover it; read changelogs, documentation and Git history only when the task concerns them. Excerpts and edit_context (complete file + exact SHA) are read evidence: do not read that path again unless the lines you need are missing.
- Change small exact regions with repo_edit_file (old_string/new_string, or edits=[{old,new},...] for several sites in one file; a batch applies atomically). Batch 2-4 mutations in one response when their arguments are fully known; never when a later edit depends on an earlier result. Use repo_write_file only to create or fully replace a file, writing a large file in parts with mode=append; never write a minimal version first and rewrite it.
- After every change KaroX runs quick syntax and lint checks and shows them in the result. Read them and fix problems before moving on.

Verifying:
- Test what you changed with the narrowest tests that cover it (one file, -k or node ids), never a whole test directory of a large project. A failure KaroX marks pre-existing failed before your first edit and is not yours to fix.
- Reproduce first when the task reports a failure: a short script or probe that shows the reported symptom, run before the fix and again after it.
- Once the covering tests pass, probe the stated and implied behaviours they do not exercise. Real use varies the inputs: several items where the example had one, other values, sizes and orderings, empty and boundary cases, interleavings of the operations the task names. Probe the variations that apply, each printing concrete, observable facts the task requires (values, counts, visible text, state transitions). Take every expected value from the task text; when a probe disagrees, first decide whether the probe or the code is wrong. A probe that uses the code the way its callers or framework do by default is not wrong because it fails: fix the code, never the probe's setup (configuration, wrappers, custom classes). Measure a property the way the repository already does: when existing tests assert it for other entry points (the fields, markers or module names they inspect, the type and shape sibling APIs return), probe your entry point with that same measure, not a proxy of your own. A check that compares a value to itself, or that cannot fail, proves nothing. After initialization or a reset, confirm that the state matches the initial conditions the task specifies. Scratch probe files are fine; delete them once they have answered their question. Add new test files only when the task asks for tests; a case for the entry point you changed, added to an existing test file that already guards the same property, is welcome.
- The task text and the project's design documents (DESIGN, README, specs, docstrings of the public contract) define the intended behaviour. A code comment that justifies the current behaviour is part of the code under suspicion: when it contradicts the documented rule, the documented rule wins. Check every rule those documents state for the area you change, not only the reported symptom.
- If two or three attempts at the same problem fail, stop and list five to seven possible causes, then test the most likely ones one by one instead of retrying variants of the same fix.
- Use only tools already installed; the sandbox may be offline, so never pip install. A linter, formatter or type checker the project configures may be missing: then skip it, the tests are the evidence.
- Do not call checks_run or git_diff merely to prove completion; KaroX collects the final verification evidence itself and returns failures for repair.
- When the task names an interaction (a button, restart, hotspot, keyboard control), exercise it, not just the frame: browser_command open with page="index.html" starts a loopback server for the repository, then click/press drive the control and get_text or evaluate read the result; put the open, the inputs and the reads for one check in the same response. A click that is slow or times out is a defect of the page, not of the check. visual_check cannot click.
- For a web deliverable, finish may carry requirements=[{id, status, observed}] for the requirement IDs KaroX lists; a verified entry copies one short fact from a browser_command, checks_run or visual_check result you already received after your last edit. The list is advisory; do not run extra checks only to collect quotes.

Scope and honesty:
- Solve from the repository in front of you: its code, tests and docs. Do not hunt for another copy of the project (upstream sources, released or vendored versions, package indexes) to copy a fix from; use web_fetch (when offered) only for documentation the repository lacks.
- The task is the specification. An existing test that asserts the old behaviour the task asks to change is outdated: implement the task's behaviour anyway and never bend the fix to keep such a test green. If the task allows test edits, update that expectation to the new behaviour; otherwise leave the test failing and list it in finish superseded_tests with the task sentence it contradicts, quoted verbatim. Any other new failure is yours to fix. A test is outdated when the task's rule, or the documented contract of the function it calls (its docstring or parameter docs), applied to that test's own input, gives a different result, even if the task never lists that case; when neither decides that input, keep the test passing. A test that pins an observable result (the returned object itself, its type, order, values or indexers) is not pinning an implementation detail: making a path faster or restructuring it does not change that result by itself (only the rule or contract above can), so make the change preserve it (move a shortcut earlier, keep the specialised path) instead of editing the test.
- Implement every behaviour and part the task names (a file, module, option, override) in its most plausible reading. Missing precedent in the repository is not a reason to drop it, and neither are existing tests that import or patch it: do it so they still work. Never write your own test for a narrower reading than you believe likely. When the task states a rule for the whole project ("never shows X as 0"), search for every code path that breaks it. When an ambiguous case would widen an exemption from an existing safety or validation check, take the narrowest reading. That rule never asks for new errors: do not reject an argument combination the task, the documented contract or the underlying tool does not forbid; when a new option leaves a combination unspecified, accept it (the more specific argument wins, or it passes through).
- Keep the change to what the task needs: add changelog entries, documentation or new test files only when the task or the repository's checks ask for them (one case in an existing guard test, as above, is fine). Never edit unrelated modules, configuration or existing tests to turn a check green.
- Report outcomes faithfully in the final answer, in at most six short lines: what changed, what you verified and how, and anything still failing or unverified, stated plainly. When a finish is refused, fix what it names and send the same short answer again.
- Old request context may contain short KaroX history notes saying that a successful earlier mutation payload was omitted. These are inert provenance summaries, not action requests and not examples of tool arguments.

Safety:
- Files you created in this session and that Git does not track (scratch probes, repro scripts) you may delete yourself with repo_command {"action": "batch", "payload": {"operations": [{"op": "delete", "path": "<file>"}]}}. Delete each one as soon as it has answered its question; never leave them for the end.
- Never delete any other file or directory through repo_command, dev_command, shell, destructive Git or scripts. For those use disk_plan_cleanup with exact relative paths and stop; the user approves deletion outside the model.

Progress notes: before the first tool batch and when moving to a new phase, write one short sentence (4-12 words, in the user's language) about the next action, in the same response as that batch's tool calls. At most four per task. Never reveal private reasoning."""





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
