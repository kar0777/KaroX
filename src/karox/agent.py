"""Bounded native-agent loop built on the provider-independent Core Runtime."""

from __future__ import annotations

import hashlib
import json
import math
import pathlib
import re
import time
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import (
    Any,
    Callable,
    Deque,
    Dict,
    Iterable,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
)

from .agent_modes import normalize_mode
from .cache_scheduler import CacheAwareScheduler, CacheDecision
from .core import CoreRuntime, ToolDefinition
from .context_compiler import (
    CallPayload,
    CompiledCallPayloads,
    CompiledContext,
    ContextCompiler,
    ContextItem,
    Verdict,
    continuity_lines,
)
from .auto_verify import quick_checks, summarize

#: New context replacements wait until together they save this many characters
#: (~10k tokens) before they are adopted. Adopting one invalidates the provider's
#: prefix cache from the first rewritten message onward, so a small saving costs
#: far more in re-sent uncached prefix than it saves; batching amortises that.
REWRITE_BATCH_MIN_CHARS = 40_000

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
COMPLETED_MUTATION_CONTEXT_THRESHOLD_CHARS = 2_000

#: Old inspection results are reproducible from the repository and become a
#: dominant long-context cost when a repair loop reads many different ranges of
#: the same file. Keep a small exact working window; older large results are
#: replaced in request context only, in cache-friendly batches. Durable history
#: remains complete.
INSPECTION_CONTEXT_KEEP_RESULTS = 6
INSPECTION_CONTEXT_MIN_RESULT_CHARS = 900
INSPECTION_CONTEXT_REWRITE_BATCH_MIN_CHARS = 24_000

#: Successful mutation results can carry multi-kilobyte unified diffs. Once
#: several newer mutations exist, those old diffs are stale working memory: the
#: current file is authoritative and durable history keeps the exact evidence.
#: Keep a recent exact window and replace older successful results only in the
#: provider request context, using the same cache-stable batch discipline as
#: inspection compaction.
MUTATION_RESULT_CONTEXT_KEEP_RESULTS = 6
MUTATION_RESULT_CONTEXT_MIN_CHARS = 1_000
MUTATION_RESULT_REWRITE_BATCH_MIN_CHARS = 24_000

#: A failed verification already gives a concrete defect. A few targeted reads
#: are reasonable, but an unbounded read/search loop is neither diagnosis nor
#: repair. After this many pure-inspection semantic turns, inject an act-first
#: repair instruction before allowing the loop to keep expanding context.
FAILED_CHECK_INSPECTION_ROUNDS = 1
# Visual verdicts already name concrete visible defects and the current target
# bytes are attached request-side when possible. One locating read is enough;
# after that the action gate below makes the boundary executable, not advisory.
VISUAL_REPAIR_INSPECTION_ROUNDS = 1

REPAIR_ACTION_GATE_KINDS: frozenset[str] = frozenset(
    {
        "failed_check_action",
        "failed_check_output_recovery",
        "visual_repair_action",
        "visual_repair_output_recovery",
        "mutation_mistake_recovery",
        "repair_action_gate",
    }
)

from . import completion_gate
from . import base_probe
from .cost_intelligence import (
    ContextTierDecision,
    CostGovernor,
    CostLedger,
    ReadCache,
    StablePrefixCache,
    ToolSchemaDeduplicator,
    event_cost_usd,
)
from .models import CoreCommand, CoreResult, EvidenceRecord, Origin, OriginKind
from .provider_pricing import PricingRegistry
from .providers import (
    ImageAttachment,
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
from .test_output import condense_test_output
from .tool_universe import (
    UniverseSelection,
    discovery_note,
    family_of,
    select_families,
)
from .usage_analytics import USAGE_EVENTS_KEY, merge_response_usage, usage_event_from_response
from .semantic_shadow import (
    ShadowDecision,
    ShadowSnapshot,
    audit_history_delta,
    classify_before_model,
    request_char_counts,
    summarize_shadow_usage,
)
from .web_check import default_pages, screenshot
from . import requirements as requirements_ledger


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
MUTATION_TOOLS: frozenset[str] = frozenset(
    {"repo.write_file", "repo.edit_file", "repo.apply_patch"}
)

# Core tools whose arguments carry a complete replacement of one named target,
# mapped to the argument key that names it. A later call with the same tool and
# target makes the earlier payload describe no state the repository can be in,
# so the request-side compiler drops it. Incremental editors are absent on
# purpose: a second edit composes with the first, so neither is stale.
FULL_REPLACEMENT_TOOLS: Dict[str, str] = {"repo.write_file": "path"}

# Who asked Core for a call. A model's own call carries no marker, because the
# transcript is overwhelmingly model calls and a field on every one of them is
# tokens spent on the common case. The harness's finishing checks do carry one:
# who asked for a check is exactly what a reviewer needs when that check is the
# reason a run was allowed to finish.
HARNESS_ORIGIN = "harness"
HARNESS_REQUESTED_BY = "harness_final"

# Tools whose successful result is something the model actually looked at, as
# opposed to something it asserted. A question about the repository is answered
# rather than changed, so these are what an answer is allowed to rest on.
INSPECTION_TOOLS: frozenset[str] = frozenset(
    {
        "repo.read_file",
        "repo.read_lines",
        "repo.inspect",
        "repo.search",
        "repo.list_files",
        "disk.scan",
        "disk.plan_cleanup",
        "git.status",
        "git.diff",
        "git.log",
    }
)

# Repository-content inspections that can legitimately diagnose a failed check
# but must not form an unbounded repair loop. Git proof reads are intentionally
# excluded: they belong to final evidence, not diagnosis.
REPAIR_INSPECTION_TOOLS: frozenset[str] = frozenset(
    {
        "repo.read_file",
        "repo.read_lines",
        "repo.inspect",
        "repo.search",
        "repo.list_files",
    }
)

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


def _render_plan_note(items: object) -> Optional[str]:
    """The model's own plan as a short checklist, or nothing if there is none."""
    if not isinstance(items, list) or not items:
        return None
    lines: List[str] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        label = str(item.get("text") or "").strip()
        if not label:
            continue
        lines.append(f"- [{'x' if item.get('status') == 'done' else ' '}] {label}")
    if not lines:
        return None
    return "Current plan:\n" + "\n".join(lines)


#: When a task's own words say the work is a three.js scene. Deliberately the
#: plan's rule rather than a cleverer one: a false positive costs the notes'
#: tokens for one run, while a false negative ships an unlit scene that every
#: other gate in this product passes.
_THREE_DIMENSIONAL_TASK = re.compile(r"three(\.js)?", re.IGNORECASE)
DOMAIN_NOTES_HEADING = "## Domain notes"


def domain_notes(task: object) -> str:
    """The domain notes this task calls for, or "" when it calls for none.

    Read from the installed package so the notes travel with the wheel; a
    missing file is not an error, because a task that needs no notes is the
    common case and an installation without the data directory still runs.
    """
    if not isinstance(task, str) or _THREE_DIMENSIONAL_TASK.search(task) is None:
        return ""
    try:
        from importlib import resources

        note = resources.files("karox.skill_notes").joinpath("threejs_scene.md").read_text(
            encoding="utf-8"
        )
    except (ModuleNotFoundError, FileNotFoundError, OSError):
        return ""
    text = note.strip()
    if not text:
        return ""
    return f"\n\n{DOMAIN_NOTES_HEADING}\n{text}"


_GREENFIELD_META_FILES = frozenset({".gitignore", ".gitattributes", ".editorconfig"})

GREENFIELD_VISUAL_FIRST_ACTION_NOTE = """
Runtime first-action contract for this greenfield visual build:
- Your first assistant response must contain a real repo_write_file tool call. Do not spend the first turn planning the whole deliverable.
- Create only a small, complete, runnable scaffold and set more_semantic_changes=true. The first turn's goal is observable structure, not completeness.
- For a single-file HTML deliverable, keep the document syntactically valid after every step and leave a stable insertion marker inside the main script before its closing tag.
- Add later layers with targeted repo_edit_file replacements at that marker. Do not append JavaScript after closing script/html tags.
- Do not implement fish, plants, decoration, bubbles, or other secondary features in the first write. Add one coherent layer per later mutation.
- Emit tool calls through the tool interface; never print XML/pseudo-tool markup as prose.
""".strip()


def greenfield_delegated_note(
    task: object,
    repository: pathlib.Path,
    *,
    visual_web: bool = False,
) -> str:
    """Tell the model when deterministic inspection already proves no code exists.

    Conservative by design: any non-metadata directory or any file that is not
    an explicitly delegated source disables the hint. Existing repositories keep
    the normal inspect-first behavior.
    """

    if not isinstance(task, str):
        return ""
    delegated = set(requirements_ledger.delegated_source_paths(task))
    if not delegated:
        return ""
    try:
        root = repository.expanduser().resolve()
        entries = list(root.iterdir())
    except OSError:
        return ""

    seen_source = False
    for path in entries:
        name = path.name
        if name in {".git", ".karox", "__pycache__"} or name in _GREENFIELD_META_FILES:
            continue
        if path.is_dir():
            return ""
        if not path.is_file():
            return ""
        relative = path.relative_to(root).as_posix()
        if relative not in delegated:
            return ""
        seen_source = True
    if not seen_source:
        return ""
    note = (
        "\n\nRuntime repository note: this is a greenfield/seed-only repository. "
        "Apart from Git/metadata, it contains only the delegated source file(s) "
        "already prefetched above; there is no existing implementation to inspect. "
        "Create the requested deliverable directly instead of calling "
        "repo_inspect or repo_list_files."
    )
    if visual_web:
        note += "\n\n" + GREENFIELD_VISUAL_FIRST_ACTION_NOTE
    return note


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
FULL_REPLACEMENT_ALIASES: Dict[str, str] = {
    provider_alias(name): key for name, key in FULL_REPLACEMENT_TOOLS.items()
}

# Calls that carry a fragment rather than a whole file even though their tool
# name normally replaces one, mapped to the argument that says so. An append
# composes with what is already on disk exactly like an incremental edit, so it
# must neither supersede an earlier write nor be elided as one: the file cannot
# be reconstructed from a fragment.
INCREMENTAL_ALIASES: Dict[str, tuple[str, str]] = {
    provider_alias("repo.write_file"): ("mode", "append")
}


TOOL_ALIASES: Dict[str, str] = {
    provider_alias(name): name for name in sorted(REQUIRED_TOOLS)
}

MUTATION_ALIASES: frozenset[str] = frozenset(
    provider_alias(name) for name in MUTATION_TOOLS
)

# These capabilities exist in Core so the deterministic harness can call them,
# but advertising them to the main model only creates duplicate proof turns.
# Resolution still knows their aliases; this is request-surface reduction, not a
# policy denial or capability removal.
HARNESS_ONLY_TOOLS: frozenset[str] = frozenset({"visual.check"})

# Durable transcripts may contain tools that the current universe/policy does not
# expose on this request. Analysis of old turns must therefore not depend on
# self._tool_aliases. This map is classification-only; it grants no capability.
HISTORICAL_TOOL_ALIASES: Dict[str, str] = {
    provider_alias(name): name
    for name in sorted(
        INSPECTION_TOOLS
        | MUTATION_TOOLS
        | frozenset({"checks.run", "visual.check"})
    )
}

SYSTEM_PROMPT = """You are KaroX, a coding agent working inside one repository. Tool results are the only evidence; your own narrative is not.

How to work:
- For unfamiliar existing code, when repo_inspect is offered use it with depth=focused to locate the implementation and relevant tests in one deterministic pass. If the runtime says the repository is greenfield/seed-only and already prefetched the delegated spec, skip repo_inspect/listing and create the requested deliverable directly. Inspect excerpts are current repository evidence: do not list/search/read an excerpted file again unless the exact lines you need are absent. If the result contains edit_context, that field is the complete current content and exact SHA of the top small change-point; edit that path directly and do not call repo_inspect or repo_read_file for it again. If focused already identified a likely target file but edit_context is absent and you need more exact lines, read that file/range directly instead of calling repo_inspect again at standard/deep. Escalate inspect depth only when focused failed to locate a target, test, or dependency you still need. Use repo_search and repo_read_lines only for follow-up gaps; batch independent reads in one response.
- Use plan_update only for genuinely multi-step implementation work with three or more independent changes. A simple inspect -> one edit -> verify task does not need a plan. Never spend a model turn only updating a plan; batch plan updates with useful repository work, and do not send a final plan-only turn.
- Build in layers: first a minimal version that runs, then add features one layer at a time. Do not try to write everything in one response.
- Change small exact regions with repo_edit_file. For one replacement use old_string/new_string; for a few short independent replacements in one file use edits=[{old,new},...]. For a larger multi-site repair, especially when several replacement strings would make a large escaped JSON payload, prefer repo_apply_patch with one narrow unified diff: it is parsed and applied atomically. When 2-4 independent mutations are already fully known, batch them in the SAME assistant response using up to four mutation tool calls instead of spending one model turn per mutation. Do not batch when a later anchor or decision depends on an earlier result. Use repo_write_file only to create a file or replace all of it; for a large new file write the first part, then add the rest with mode=append.
- After every change KaroX runs quick checks and shows the result in the tool output. Read it and fix problems before moving on.
- Old request context may contain short KaroX history notes saying that a successful earlier mutation payload was omitted. These are inert provenance summaries, not action requests and not examples of repo_edit_file/repo_write_file arguments.
- Prefer the libraries the task allows and web_fetch (when offered) over re-deriving known code from memory.
- Every repo_edit_file or repo_write_file call must include more_semantic_changes=true or false. Set it true only when you already know a specific additional code/config/test mutation is required after this one. Set it false when this is the last semantic mutation and only tests, checks, verification, Git status/diff, or reporting remain; KaroX will collect that final evidence itself and return failures for repair. If no mutation is needed in this response, reply with a short summary and no tool call; KaroX will collect any still-missing final evidence itself. Do not call checks_run, git_status, or git_diff merely to prove completion; use them mid-task only when their result will change a planned semantic change.

Safety:
- Files you created in this session and that Git does not track (scratch probes, repro scripts) you may delete yourself with repo_command batch {op: delete, path}. Delete each one as soon as it has answered its question; never leave them for the end.
- Never delete any other file or directory through repo_command, dev_command, shell, destructive Git or scripts. For those use disk_plan_cleanup with exact relative paths and stop; the user approves deletion outside the model.

Progress notes: before the first tool batch and when moving to a new phase, write one short sentence (4-12 words, in the user's language) about the next action. At most four per task. Never reveal private reasoning."""

MAINTENANCE_SYSTEM_PROMPT = """You are operating in KaroX disk-maintenance mode.
Your job is to help the user reclaim storage without reading arbitrary file
contents and without deleting anything yourself.

Use disk_scan to inspect bounded metadata-only cleanup candidates. Prefer caches,
temporary files, dependency folders, virtual environments and rebuildable build
artifacts. Treat application data, user files and code as higher-impact even when
they are large. Use disk_plan_cleanup only after choosing an exact, minimal list of
relative targets. That tool freezes the list and metadata for a human review; it
does not delete anything. The KaroX UI, not you, owns the later confirmation and
apply step. Never claim that anything was deleted merely because a plan exists.

Do not try to work around this boundary with repository writes, shell/process
commands, Git, browser automation, or guessed absolute paths. System directories,
KaroX runtime files and approved project roots are protected. A project root is
not cleanup waste; rebuildable children such as node_modules may be candidates.

After creating a plan, give a concise impact summary and stop so the user can
approve or cancel it. Mention only the useful facts: approximate size/count,
applications or code projects affected, and whether user/application data is
included. Write in the language of the user's task. Keep public work notes short
and never expose private chain-of-thought."""

REPAIR_PROMPT = """KaroX ran the task's checks after your last change and they failed:
{failure_summary}
Fix the cause with one targeted repo_edit_file or a narrow repo_apply_patch, then finish again. KaroX re-runs the checks itself."""

VISUAL_REPAIR_PROMPT = """KaroX rendered the current deliverable and the isolated visual reviewer found concrete visible defects:
{failure_summary}
Do not finish on the strength of a technical/runtime PASS. Fix ALL listed visible defects in this single semantic repair turn. Preserve task requirements that already looked correct in earlier renders: do not flatten or replace working 3D geometry, camera, glass, water, plants, lighting, or animation merely to fix a local defect. Prefer one narrow repo_apply_patch for a larger multi-site repair, or repo_edit_file for short exact replacements; otherwise emit up to four independent mutations in the SAME response. Do not spend one model turn per defect. After the edits, stop: KaroX will rerun the render and visual review itself."""

ANSWER_PROMPT = """KaroX recorded no repository change for this task. If the task only needed an answer, give it now and name the tool results it rests on. If it needed a change, make it with repo_edit_file or repo_write_file and then finish; KaroX runs the checks."""

# A response cut off at the output ceiling contains no tool call, so nothing it
# described happened and the run would end with the work unpaid for: one observed
# run burned 32k output tokens this way and stopped, twice. The recovery turn is
# bounded and framed as what it is -- the same work, split into steps a single
# response can finish.
OUTPUT_LIMIT_PROMPT = """Your previous response was cut off by the output limit before it contained a tool call, so nothing happened. Continue in smaller pieces and act before reasoning further: make a tool call in this response. For a large new file, write one complete valid scaffold first (including its closing syntax), then add bounded sections with repo_write_file mode=append or repo_edit_file. Do not draft the whole deliverable in hidden/visible reasoning before the tool call, and do not replace a larger incomplete file with a smaller incomplete rewrite."""

FAILED_CHECK_OUTPUT_RECOVERY_PROMPT = """Your diagnostic repository context for the failed check is already attached, but your repair response was cut off before any action. Do not inspect or explain further: make one targeted repo_edit_file/repo_apply_patch/repo_write_file mutation in this response that addresses the active check failure. If the current bytes already contain the fix, rerun the approved check instead. This is the final no-mutation recovery for this failed-check episode."""

VISUAL_REPAIR_OUTPUT_RECOVERY_PROMPT = """Your visual-repair response was cut off before it emitted a mutation. KaroX is attaching the same authoritative current file bytes again in this request. Do not read, search, inspect, explain, or redesign the whole page. Use the attached current bytes and the existing visual defects to emit ONE bounded mutation now: use repo_apply_patch for a larger multi-site fix or repo_edit_file for a short exact fix. Then stop so KaroX can rerun render and visual review. This is the only extra no-mutation retry for this visual-repair identity."""

REPAIR_ACTION_GATE_PROMPT = """KaroX blocked that repository inspection because the active repair context already contains the authoritative current bytes/evidence you need. Do not read/search/inspect again in the next response. Emit one targeted repo_edit_file/repo_apply_patch/repo_write_file mutation now, or rerun the approved check if no repository change is required. Keep the mutation bounded and stop after acting so KaroX can verify it."""

MUTATION_MISTAKE_THRESHOLD = 2
MUTATION_MISTAKE_RECOVERY_PROMPT = """KaroX observed {count} mutation attempts on {target} fail since the last successful repository change:
{failures}
Change strategy now instead of repeating another large edit/patch from memory. First decide whether this mutation is actually required by the task or an active check. If it is optional cleanup and the required behavior is already implemented, stop editing and finish so KaroX can run the approved acceptance check. If it is required: for malformed/schema arguments, correct the tool call directly without another read; for anchor/conflict errors, use at most ONE narrow read/search to capture the exact current bytes, then make ONE small repo_edit_file anchored to those bytes. Do not escalate to a larger repo_command/apply_patch unless that bounded evidence proves it is necessary."""

TOOL_PROTOCOL_PROMPT = """Your previous response printed something that looked like a tool invocation as ordinary text, so that pseudo-call did not execute. Do not finish or repeat the XML/tool syntax in prose. Repeat only the intended next concrete action now using an actual tool call through the tool interface, then continue from the real tool result."""

EMPTY_RESPONSE_PROMPT = """Your previous response arrived empty: no text and no
tool call, so nothing happened. Continue the task with the next concrete step --
the narrowest tool call that makes progress -- and keep the answer short."""

# Showing the model its own rendered frame is necessary and not sufficient. One
# measured run attached five frames of its own page and still shipped a tank
# dominated by a single oversized sphere, closing with "the visual balance wasn't
# verified against a reference"; the same model, handed that frame beside the
# task's requirements, named the defect as its first answer. The difference is
# being made to answer the checklist, in a form the runtime can act on, instead
# of an invitation to compare.
VISUAL_REVIEW_PROMPT = """The frame above is the render KaroX just measured of the
page this run produced.

Go through the task's requirements one at a time and grade each against what the
frame actually shows. Answer with one line per requirement, in this exact form:

R1. <the requirement in a few words>: <what in the frame satisfies it>
R2. <the requirement in a few words>: UNMET - <what is visible that is wrong> -> fix: <the concrete change that would satisfy it>

For a fix, give the number or the parameter to change ("swim amplitude 0.63 ->
0.06, frequency 27 -> 4", "band width 40% of body -> 12%, keep the base orange
between them", "lamp intensity 1.2 -> 4 with distance 12"), never a general
exhortation. When a well-known implementation already does what the requirement
asks, name the file and its source (for example
three@0.160.0/examples/jsm/objects/Water.js on unpkg) so the fix can read it with
web_fetch instead of inventing one.

Every requirement the frame can show gets a line. End with exactly one last line:
VISUAL: OK
and write it only when no line above says UNMET. Otherwise that last line is
VISUAL: VIOLATIONS.

Judge only what the frame shows: a requirement the frame cannot show is left out,
and "the code implements it" is not evidence that it is visible."""

#: The same review, against a session with a requirement ledger: the IDs and
#: texts are listed from the ledger itself, so the grade answers what the user
#: admitted rather than what the model chooses to call a requirement. Every
#: listed ID gets a line -- "NOT VISIBLE IN FRAME" is how a requirement the
#: frame cannot show stays answered without inventing evidence for it.
VISUAL_REVIEW_LEDGER_PROMPT = VISUAL_REVIEW_PROMPT.replace(
    "Go through the task's requirements one at a time and grade each against what the\n"
    "frame actually shows. Answer with one line per requirement, in this exact form:",
    "Grade every requirement below, by its ledger ID, against what the frame\n"
    "actually shows. The requirements are exactly these:\n"
    "\n"
    "{requirements}\n"
    "\n"
    "Answer with one line per listed requirement, in this exact form:",
).replace(
    "Every requirement the frame can show gets a line. End with exactly one last line:",
    "Every listed requirement gets a line, under its own ID. A requirement the\n"
    "frame cannot show at all still gets its line, judged NOT VISIBLE IN FRAME\n"
    "instead of OK. End with exactly one last line:",
).replace(
    'Judge only what the frame shows: a requirement the frame cannot show is left out,\n'
    'and "the code implements it" is not evidence that it is visible.',
    'Judge only what the frame shows: "the code implements it" is not evidence\n'
    'that a requirement is visible, and a requirement the frame cannot show is\n'
    "NOT VISIBLE IN FRAME, not OK.",
)

VISUAL_REVIEW_FIX_PROMPT = """Your own review of the render named requirements the
page does not satisfy, each with the fix that would satisfy it. Apply those fixes
in the file -- read any source the fixes name with web_fetch rather than
re-deriving it -- then run the approved check again: the corrected page is
measured and shown to you for grading again."""

#: A requirement line whose own evidence says the frame does not satisfy it. The
#: marker is required rather than inferred: a line that merely numbers a
#: requirement is the model showing its work, not a finding.
_UNMET_LINE = re.compile(r"^R?\s*\d+[.)]?\s?.*\bUNMET\b", re.IGNORECASE)

#: A line answering a requirement with the honest "the frame cannot show it".
_NOT_VISIBLE_LINE = re.compile(
    r"^R?\s*\d+[.)]?\s?.*\bNOT VISIBLE\b", re.IGNORECASE
)

#: How many grades one rendered artifact may be asked for. Two lets a run answer
#: once and then confirm a fix; more would spend requests on a model that is not
#: converging, so the bound is what keeps the loop terminating.
VISUAL_REVIEWS_PER_RENDER = 2

#: What the model is told when its grade answered NOT VISIBLE IN FRAME: a
#: requirement the frame cannot show is not satisfied by that answer -- it
#: needs evidence of another kind, or the run stays unverified.
VISUAL_REVIEW_UNASSESSED_PROMPT = (
    "Your grade answered NOT VISIBLE IN FRAME for: {requirements}. That is not a\n"
    "satisfied answer -- the frame neither shows nor refutes them. Either make each\n"
    "of them visible in the page where it legitimately can be shown, or demonstrate\n"
    "it by evidence the frame cannot carry (the approved check's own output, a test,\n"
    "the exact source), and say which in your final message. A run whose\n"
    "requirements are only ever answered NOT VISIBLE IN FRAME does not finish\n"
    "verified."
)


#: Verdict verbs that say the model could not verify the requirement. They are
#: UNASSESSED, never satisfied: an honest "I could not check" must not book a
#: pass, and a hyphenated "NOT-VISIBLE" must not slip past the matcher.
_UNCERTAIN_STATUS = re.compile(
    r"^(?:CANNOT|CAN[’']?T|UNABLE|PARTIAL|UNCLEAR|UNKNOWN|UNSURE|MAYBE|"
    r"NOT[- ]?VISIBLE|NOT[- ]?VERIFIABLE)",
    re.IGNORECASE,
)


def classify_visual_verdict(
    text: Optional[str], expected_ids: Optional[Sequence[str]] = None
) -> Optional[Dict[str, Any]]:
    """Read the grade the review demanded, as structured evidence.

    Returns ``None`` when no grade is present in the required form (an empty
    answer, a cut-off one, an unknown status, an ID set that does not match
    the ledger version that was asked). Otherwise a dict: ``clean`` -- the run
    may count this artifact as visually accepted; ``satisfied``/``unmet``/
    ``unassessed`` -- the requirement IDs in each state. "NOT VISIBLE IN
    FRAME" is an honest answer about a frame that cannot show a requirement,
    and it is recorded as UNASSESSED, never as satisfied: invisibility is not
    evidence of a requirement met. A global "VISUAL: OK" cannot outvote its
    own per-requirement lines either way.
    """

    lines = []
    for raw in (text or "").splitlines():
        line = re.sub(r"^\s*[-*+]\s+", "", raw.strip())
        line = line.replace("**", "").replace("__", "").replace("\x60", "").strip()
        if line:
            lines.append(line)
    if not lines:
        return None
    verdicts = [
        re.fullmatch(r"VISUAL:\s*(OK|VIOLATIONS)\.?", line, re.IGNORECASE)
        for line in lines
    ]
    if sum(match is not None for match in verdicts) != 1 or verdicts[-1] is None:
        return None
    global_ok = verdicts[-1].group(1).upper() == "OK"
    satisfied: List[str] = []
    unmet: List[str] = []
    unassessed: List[str] = []
    for line in lines[:-1]:
        match = re.fullmatch(
            r"(?:R)?(\d+)[.)]\s+([^:]+):\s*(\S.*)", line, re.IGNORECASE
        )
        if match is None:
            return None
        requirement_id = f"R{match.group(1)}"
        status = match.group(3).strip().upper()
        # The demanded statuses lead the evidence ("UNMET -- the fix", "NOT
        # VISIBLE IN FRAME"). Prose evidence that merely MENTIONS unmet ("no
        # requirement is UNMET") is an affirmative answer, not a finding, and
        # an uncertain verb ("CANNOT VERIFY", "PARTIAL") is an honest
        # UNASSESSED rather than a pass.
        if _UNCERTAIN_STATUS.match(status):
            unassessed.append(requirement_id)
        elif status.startswith("UNMET"):
            unmet.append(requirement_id)
        else:
            satisfied.append(requirement_id)
    all_ids = satisfied + unmet + unassessed
    # Findings take precedence over coverage: a grade that names an UNMET
    # requirement (or a global VIOLATIONS) is a violation even when it also
    # skipped IDs the ledger asked about -- a named unmet requirement is a
    # real finding, and demanding a well-formed grade before hearing one
    # would bury it. Only a grade with no finding must cover exactly the
    # ledger version it was shown: a clean-looking subset is not a pass, it
    # is no grade at all.
    violation = bool(unmet) or not global_ok
    if not violation:
        if expected_ids:
            if len(all_ids) != len(set(all_ids)):
                return None
            if set(all_ids) != {item.upper() for item in expected_ids}:
                return None
        else:
            numbers = [int(item[1:]) for item in all_ids]
            if not numbers or numbers != list(range(1, len(numbers) + 1)):
                return None
    clean = not violation and not unassessed
    return {
        "clean": clean,
        "satisfied": satisfied,
        "unmet": unmet,
        "unassessed": unassessed,
    }

# Compaction is where a long run either survives or restarts. Dropping the oldest
# turns and leaving a digest of tool facts preserves what happened but not what
# the model decided, so the model reopens settled questions and redoes finished
# work. Asking the model itself for a continuation brief is how Codex keeps a
# session coherent across compaction, and it is what this prompt does: the brief
# is written by the same model that will continue from it, from material that is
# bounded and already recorded.
COMPACTION_SUMMARY_PROMPT = """KaroX is about to drop the earliest turns of this
session to stay inside the model context window, and you will continue the task
with your own brief in their place. Write that brief now, for yourself.

Use only the material below. It contains the task, the recorded tool facts, and
your own earlier statements; it does not contain the full text of the dropped
turns, so do not claim to remember anything it does not show.

Write these sections, in this order, as compactly as the facts allow:
1. Objective: the task, and every constraint in it that must not be broken.
2. Done: what is finished, with file paths and evidence ids exactly as recorded.
3. Decisions: choices already made and the reason, so they are not reopened.
4. Open: what is still unverified, broken, or half-written.
5. Next: the single next action to take.

Do not describe the act of summarizing, do not apologize, and do not invent a
fact that is not in the material. If something is unknown, say so.

Always keep: the task goal and acceptance criteria; every file path created or changed with its latest sha256; the latest check results; open plan items."""

#: The brief is a briefing, not a document: a few thousand tokens is plenty, and
#: it is paid for once per compaction rather than once per step.
COMPACTION_SUMMARY_MAX_TOKENS = 3_000
#: Bound on the material handed to the summarizer. It is built from recorded
#: narration and the deterministic digest, never from raw tool payloads, so it
#: stays small even when the turns it describes were enormous.
COMPACTION_MATERIAL_CHARS = 16_000
COMPACTION_NARRATION_CHARS = 1_200
#: How many further dropped entries a brief may lag behind before it is rewritten.
#: The deterministic digest is rebuilt for the exact dropped range on every
#: request, so the facts never lag; only the narration does. Refreshing the brief
#: on every step instead would double the provider calls of a compacting run,
#: which is the opposite of what compaction is for.
COMPACTION_BRIEF_REFRESH_ENTRIES = 8
#: Cline-style task handoff / Codex-style fresh-window boundary for visual
#: repair. A repair should not pay to replay the whole construction transcript;
#: the durable audit stays complete while the outbound view keeps the task,
#: deterministic evidence digest, and a small recent grace zone.
VISUAL_REPAIR_EPOCH_MIN_TOKENS = 32_000
VISUAL_REPAIR_EPOCH_KEEP_GROUPS = 5
VISUAL_REPAIR_SNAPSHOT_MAX_CHARS = 64_000

#: A provider that rejects a request for its size has said something no registry
#: did. This is what such a rejection looks like across the OpenAI-compatible,
#: Anthropic and Gemini families; anything else stays a normal provider error
#: rather than triggering an adaptation that would hide a real fault.
_CONTEXT_REJECTION = re.compile(
    r"context length|context_length|maximum context|context window|"
    r"too many tokens|too long|reduce the length|prompt is too long|"
    r"input is too long|token limit|maximum tokens",
    re.IGNORECASE,
)
#: Never learn a window below what a single step needs: shrinking past this would
#: turn a size rejection into a run that can no longer hold one tool result.
MIN_LEARNED_CONTEXT_WINDOW = 32_000

DEADLINE_NOTE = """About %d minutes remain of this run's budget, and a state
that passed the approved check already exists (step %d). Stop adding
improvements: run the approved check and the git reads now, so the run can end
verified. If a change is still necessary, keep it small enough to re-verify in
the time that is left, because the end of the budget is a hard stop."""

#: The share of the wall budget below which a working run is told to consolidate.
#: A measured high-effort run spent its last fifty minutes polishing a page that
#: had already passed, and finished with a broken one; a deadline it can see is
#: what makes finishing the rational choice.
DEADLINE_NOTE_SHARE = 0.2
#: Never nudge before this much has elapsed, however short the budget is.
DEADLINE_NOTE_MIN_SECONDS = 120.0

DEADLINE_NOTE_UNPROVEN = """About %d minutes remain of this run's budget and no
change has passed the approved check yet. Converge now: keep the smallest fix
that addresses the task, run the most focused approved check that exercises it,
delete scratch files this session created, and finish. Do not start new
exploration or refactors; the end of the budget is a hard stop and unverified
work is reported as unverified."""

#: A run with edits but no proven state gets its own, earlier nudge. Measured
#: SWE runs (harness race r7/r8) produced a correct patch, never ran the check,
#: and ended on wall_time_limit with scratch files left behind: the proven-state
#: note never fired because nothing had been proven yet.
DEADLINE_NOTE_UNPROVEN_SHARE = 0.25
DEADLINE_NOTE_UNPROVEN_MIN_SECONDS = 180.0
#: Only long autonomous budgets get the unproven nudge; short interactive runs
#: are left alone.
DEADLINE_NOTE_UNPROVEN_MIN_BUDGET = 600.0

EDGE_CASE_REVIEW = """Before this run ends, check the actual requirements, not just
green tests. A new test that passes on your patch but fails on the unchanged base
shows that it detects a change; it does NOT show that it covers every requirement.
List each stated rule and example with its expected value, then check independent
variants of the rule (for example nested inheritance, None/empty values and a
second call). For each, name an existing test or run the smallest focused probe.
If the probe misses a case, fix the code and rerun it. Run your regression test
on the unchanged base only in a separate clean checkout or snapshot, never by
resetting or stashing the working tree: report red/green only if it actually
happened. Do not treat a red/green test alone as proof of all requirements.
A related case you noticed while working that follows from the same stated rule
is in scope: fix and test it instead of reporting it as an out-of-scope quirk.
Delete scratch probes after use; if time is short, report untested cases honestly
instead of asserting they were verified."""

#: Only long autonomous budgets get the edge-case review, and only while there is
#: still time to act on what it finds.
EDGE_REVIEW_MIN_BUDGET = 600.0
EDGE_REVIEW_MIN_REMAINING = 240.0
#: Upper bound for the red-on-base probe of the agent's own tests.
BASE_PROBE_MAX_SECONDS = 120.0

PACE_NOTE = """Your last response spent about %d output tokens before acting.
Long deliberation is the main reason runs end on the time limit. From here on:
take the next concrete step after a short thought -- read the exact lines, make
the smallest edit, run the narrowest check -- and let tool results, not more
reasoning, settle open questions. Delete each scratch probe as soon as it has
answered its question."""

#: One response this long (reasoning included) is the measured signature of a
#: run that misses its wall budget: SWE race runs whose top decile exceeded 4-5k
#: output tokens per step timed out on correct patches, while the CLIs that
#: finished kept their steps under 2k.
PACE_NOTE_OUTPUT_TOKENS = 6_000

#: Long autonomous runs can spend tens of thousands of output tokens deciding
#: what single tool to call next. The CLI ceiling is a maximum, not a target;
#: once a turn has concrete repository evidence, bound the next semantic burst.
#: Short interactive runs keep the caller's requested ceiling unchanged.
ADAPTIVE_OUTPUT_MIN_BUDGET_SECONDS = 600.0
ADAPTIVE_OUTPUT_AFTER_INSPECTION = 16_000
# A first whole-file create can legitimately be much larger than an inspection
# or repair turn, but 64k lets a model spend minutes narrating/re-generating a
# page whose complete HTML is normally well under 100 KiB. If 24k ever clips a
# genuinely larger create, the existing length fail-open restores the caller's
# full ceiling on the next turn.
ADAPTIVE_OUTPUT_LARGE_CREATE = 24_000
# Visual single-file pages are materially larger than ordinary create turns.
# Give them one larger bounded burst, while leaving non-visual coding at 16k.
ADAPTIVE_OUTPUT_VISUAL_CREATE = 8_000
ADAPTIVE_OUTPUT_LIMIT_RECOVERY = 16_000
ADAPTIVE_OUTPUT_VISUAL_LIMIT_RECOVERY = 12_000
ADAPTIVE_OUTPUT_AFTER_MUTATION = 16_000
# Once a visual web deliverable exists, another 20-30k-token answer is usually
# a whole-page rewrite disguised as a repair. Keep repairs focused; a genuine
# large rewrite fails open on finish_reason=length on the next turn.
ADAPTIVE_OUTPUT_VISUAL_REPAIR = 8_000
ADAPTIVE_OUTPUT_VISUAL_ACTION_RECOVERY = 8_000
ADAPTIVE_OUTPUT_VERIFICATION = 2_000
ADAPTIVE_OUTPUT_INSPECTION_TOOLS: frozenset[str] = frozenset(
    {"repo.inspect", "repo.read_lines", "repo.search"}
)
ADAPTIVE_OUTPUT_CREATE_CONTEXT_TOOLS: frozenset[str] = frozenset(
    {"repo.read_file", "repo.list_files", "skill.load", "plan.update", "web.fetch"}
)

#: At most this many pace notes per run, and never on consecutive bursts: a note
#: is a correction, not a metronome, and each one costs a cache-extending turn.
PACE_NOTE_MAX = 3
PACE_NOTE_MIN_GAP_STEPS = 4

#: How often the model is shown its own plan again. A plan written at the start
#: of a long run is the first thing compaction drops, and a step budget spent
#: re-reading the task to remember what was next is the cost of not showing it.
PLAN_NOTE_EVERY_STEPS = 6

#: How many rendered frames one run may hand to the model. Each is a small
#: image, and the point is to let the model see its own work, not to stream it a
#: video: a handful of looks covers build, first render and the late fixes.
MAX_RENDER_CAPTURES = 24
#: How many model steps must pass before another opportunistic frame is shown.
#: A frame is a picture plus a message; one spent on every small edit teaches
#: nothing the digest check would not have caught anyway. A required frame -- the
#: one a grade will rest on -- ignores this, because it is not opportunistic.
RENDER_CAPTURE_MIN_STEPS = 3

#: Bounds on the snapshot of a state that passed the check. Core already limits a
#: write to two megabytes, so this only stops a pathological run from copying a
#: whole tree; a file over the limit is skipped rather than truncated, because a
#: truncated artifact is not the state that passed.
_MAX_SNAPSHOT_FILES = 50
_MAX_SNAPSHOT_BYTES = 2_000_000

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


def _tool_argument_json_loads(value: str) -> Any:
    """Parse one tool-argument object, salvaging only known inert wire tails.

    Some OpenAI-compatible routes occasionally append rendered tool markup or a
    KaroX request-context provenance note *after* a complete JSON argument
    object.  Re-paying a model turn for that transport artifact is unnecessary,
    but accepting arbitrary trailing bytes would be unsafe.  Decode exactly one
    complete JSON value and ignore a suffix only when it starts with one of the
    two known inert renderings.  Truncated JSON and every other suffix still fail
    closed through the original JSONDecodeError.
    """

    try:
        return _strict_json_loads(value)
    except json.JSONDecodeError as original:
        stripped = value.lstrip()
        decoder = json.JSONDecoder(parse_constant=_reject_json_constant)
        try:
            parsed, end = decoder.raw_decode(stripped)
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
            raise original
        tail = stripped[end:].lstrip()
        if tail.startswith("<tool_call") or tail.startswith("[KaroX history note:"):
            return parsed
        raise original


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
CONTEXT_TIERS: Dict[str, Optional[int]] = {
    "auto": None,
    "full": 0,
    "1m": 1_000_000,
    "272k": 272_000,
    "120k": 120_000,
}

#: What an unknown window resolves to. A model that advertises nothing gets a
#: budget rather than a claim: this is the small tier, and a provider that
#: rejects the request size teaches the loop the real one instead of failing.
CONTEXT_TIER_UNKNOWN_WINDOW = 272_000


def resolve_context_window(
    tier: str, advertised: Optional[int]
) -> tuple[Optional[int], str]:
    """Resolve a context tier into the window this run budgets against.

    Returns the window and where it came from, because the difference matters:
    ``advertised`` is a fact about the model, ``tier`` is a choice the user made,
    and ``fallback`` is neither -- it is the loop being honest that nobody said.
    ``full`` resolves to the advertised window so that "keep everything the model
    can take" is expressible, and an unknown window still resolves to the small
    tier rather than to nothing.
    """

    if tier not in CONTEXT_TIERS:
        raise ValueError(
            f"unknown context tier {tier!r}; choose one of "
            + ", ".join(sorted(CONTEXT_TIERS))
        )
    if tier == "full":
        if advertised is not None:
            return advertised, "advertised"
        return CONTEXT_TIER_UNKNOWN_WINDOW, "fallback"
    configured = CONTEXT_TIERS[tier]
    if tier == "auto":
        if advertised is None:
            return CONTEXT_TIER_UNKNOWN_WINDOW, "fallback"
        # Never budget above what the model advertises: that would plan a
        # request the provider refuses rather than one compaction can prevent.
        return min(advertised, CONTEXT_TIERS["1m"] or advertised), "advertised"
    assert configured is not None
    if advertised is not None and configured > advertised:
        return advertised, "advertised"
    return configured, "tier"


@dataclass(frozen=True)
class _CompactionPlan:
    """What one compaction would drop, keep and cost.

    Shared by the loop (which has to brief the model before the request is
    assembled) and the request builder (which applies it), so both halves agree
    on exactly which turns are being replaced.
    """

    head: List[Dict[str, Any]]
    dropped: List[List[Dict[str, Any]]]
    kept: List[Dict[str, Any]]
    cut: int
    before: int
    fitted: bool
    ceiling: int
    #: How many recent turns the plan managed to keep verbatim. Below
    #: ``keep_recent_groups`` when the preferred floor could not fit, which is
    #: recorded because a run that compacted harder than usual should say so.
    kept_groups: int = 0


class _Unplanned:
    """The caller did not work out a plan, so this call has to.

    Distinct from a plan of ``None``, which is a decision: "this history needs
    no compaction". Without the distinction the request builder could not tell
    the two apart and recomputed a decision the step loop had already made.
    """


_UNPLANNED = _Unplanned()


def _history_sha256(entries: Iterable[Dict[str, Any]]) -> str:
    """Identify one range of transcript, so a stored brief can be trusted.

    The digest is what lets a brief be reused: equal digests mean the turns it
    was written from have not changed, so paying for another brief would buy
    nothing. It is computed over the recorded entries only -- never over
    generated caches -- so it cannot drift for a reason the model cannot see.
    """

    canonical = json.dumps(
        list(entries), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


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


def _json_chars(value: object) -> int:
    """How many characters this result would cost in the transcript."""

    return len(json.dumps(value, ensure_ascii=False, sort_keys=True))


class LegacyAgentKernel:
    """Runs one recoverable, exclusively leased native-agent session.

    This is the v5 model/tool decision loop, kept importable for comparison
    runs and compatibility tests. It is not the default: ``AgentKernel``
    below routes to the vendored karo-core engine (``engine_kernel``), and
    selecting this class requires an explicit ``legacy=True``.
    """

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
        lossless_context_rewrite: bool = True,
        restore_on_regression: bool = True,
        render_feedback: bool = True,
        domain_notes_enabled: bool = True,
        repeat_read_suppression: bool = True,
        mode: Optional[str] = None,
        maintenance_mode: bool = False,
        request_images: tuple[ImageAttachment, ...] = (),
        pricing_registry: Optional[PricingRegistry] = None,
        on_event: Optional[AgentObserver] = None,
        semantic_shadow: bool = False,
        base_probe_enabled: bool = True,
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
        # Phase 0 research observer. It never suppresses a provider call or
        # executes its counterfactual transition; it only annotates usage events.
        self.semantic_shadow = bool(semantic_shadow)
        # Red-on-base probe of the agent's own tests during the edge-case review.
        self._base_probe_enabled = bool(base_probe_enabled)
        self._shadow_snapshot: Optional[ShadowSnapshot] = None
        self._shadow_decision: Optional[ShadowDecision] = None
        self._shadow_history_start = 0
        # Post-step shadow fields wait in memory until the next session write the
        # real control flow already needed. Phase 0 must never create a save (and
        # therefore a session revision) merely to persist research telemetry.
        self._shadow_pending_effect: Optional[tuple[int, str, Dict[str, Any]]] = None
        self._step = 0
        self._compactions = 0
        # The step number whose plan note is already in the history: a loop that
        # re-enters without a new model step must not repeat it.
        self._plan_noted_step = -1
        # The step at which the last pace note was added; -inf means never.
        self._pace_noted_step = -(10**9)
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
        # Exact duplicate/superseded tool-result rewriting is stronger than an
        # economy preference: it is a deterministic lossless transport cleanup.
        # Keep it on by default even when deferred tool-universe optimisation is
        # off, while retaining an explicit debug/benchmark escape hatch.
        self.lossless_context_rewrite = bool(lossless_context_rewrite)
        # The agent stance (Build/Plan/Ideate). Validated here so a bad value
        # fails before a session is leased; the kernel only records it for the
        # report -- the caller owns the prompt delta and the grant policy.
        self.mode = None if mode is None else normalize_mode(mode)
        self.maintenance_mode = bool(maintenance_mode)
        if self.maintenance_mode and self.require_change:
            raise ValueError("maintenance mode plans cleanup but never mutates directly")
        if not isinstance(request_images, tuple) or len(request_images) > 8:
            raise ValueError("agent request_images must be a tuple of at most 8 images")
        if not all(isinstance(item, ImageAttachment) for item in request_images):
            raise ValueError("agent request_images contains an invalid attachment")
        self.request_images = request_images
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
        # The per-request ceiling can be lower than the caller's hard maximum on
        # long autonomous runs. It is recorded with usage so benchmark reports can
        # distinguish model behaviour from a runtime-imposed economy bound.
        self._active_output_ceiling = max_output_tokens
        approved_checks = core.verification_commands
        if not approved_checks and not self.maintenance_mode:
            raise AgentError(
                "native agents require at least one user-approved verification command"
            )
        self.system_prompt = system_prompt
        if approved_checks:
            rendered_checks = json.dumps(
                [list(item) for item in sorted(approved_checks)], ensure_ascii=False
            )
            self.system_prompt += (
                "\nOnly these user-approved checks may be executed and used as "
                f"verification evidence: {rendered_checks}"
            )
            # An entry ending in "*" is a prefix rule. Unexplained, the model
            # sends the "*" through as a literal argument, and with no shell to
            # expand it the approved command fails every time.
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
        # Every permitted Core tool is resolvable. Most are also advertised to
        # the model; harness-only proof tools stay internal because the runtime
        # already schedules them deterministically. This must remain a tiny
        # explicit set, never a return to the old hardcoded subset that hid
        # repo.search/repo.edit_file/repo.read_lines/git tools from the agent.
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
            and core_name not in HARNESS_ONLY_TOOLS
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
        # Superseded call payloads: a full-content write a later call replaced.
        # The newest write per target is always kept; only the drafts before it
        # are shortened, and the marker says so.
        self._call_payload_compilation: CompiledCallPayloads | None = None
        self._economy_superseded_call_chars_pending = 0
        self._economy_completed_mutation_chars_pending = 0
        # Request-only inspection window. Adopted markers are sticky so the
        # provider prefix stays stable; new markers join only in large batches.
        self._inspection_rewrite_adopted: Dict[str, str] = {}
        self._inspection_rewrite_batches = 0
        self._economy_inspection_chars_pending = 0
        self._mutation_result_rewrite_adopted: Dict[str, str] = {}
        self._mutation_result_rewrite_batches = 0
        self._economy_mutation_result_chars_pending = 0
        # Cache-stable rewriting. A replacement changes bytes that earlier
        # requests already sent, so every newly adopted marker invalidates the
        # provider's prefix cache from that message onward. Measured on the
        # harness race (swer5-k1..k5): ~60% of Karo's uncached input tokens --
        # 8.5M of 14.1M -- were re-sent prefixes after a one-item rewrite that
        # saved a few thousand characters. Adopted replacements are therefore
        # sticky (applied on every later request, so the prefix stays byte-
        # identical) and new ones are adopted in batches: on the first request
        # of this kernel, when the request is being compacted anyway, or once
        # the pending savings are worth one cache rebuild.
        self._rewrite_adopted: Dict[str, str] = {}
        self._rewrite_primed = False
        self._rewrite_pending_chars = 0
        self._rewrite_batches = 0
        # Compaction state: the brief this request will carry (loaded from the
        # session, reused until the dropped range outgrows it), and a window the
        # loop learned from a provider that refused a request for its size.
        self._compaction_state: Dict[str, Any] = {}
        self._learned_context_window: Optional[int] = None
        self._context_shrink_used = False
        # The session revision the kernel last wrote or read, so a heartbeat can
        # record it without re-loading and re-verifying the whole session.
        self._known_revision: Optional[int] = None
        # How many times each path has been regenerated in full, plus the size
        # of the previous full write. The size catches a more specific failure:
        # replacing an already-incomplete page with an even smaller incomplete
        # page loses work while paying for another whole-file generation.
        self._full_writes_by_path: Dict[str, int] = {}
        self._last_full_write_bytes_by_path: Dict[str, int] = {}
        # Whether anything has actually changed in this session yet, which is what
        # makes an earlier passing check prove nothing about the work.
        self._changed_once = False
        # The state that passed the check, kept so a run cannot end worse than
        # one it already proved; ``False`` turns the guarantee off for callers
        # that must never touch the workspace at the end of a run.
        self._restore_on_regression = bool(restore_on_regression)
        # Showing the model its own rendered page: the difference between the run
        # that produced striped fish and the run that produced flat ones.
        self._render_feedback = bool(render_feedback)
        # The three.js notes on the task message exist so a run does not spend a
        # turn discovering them. At v7 they live in a skill that names them, and
        # a note injected into the first message is paid for on every later step.
        self._domain_notes_enabled = bool(domain_notes_enabled)
        # Reading the same lines of an unchanged file twice: the model's earlier
        # copy is still in the transcript, so the second read returns the fact
        # that nothing changed instead of the bytes again.
        self._repeat_read_suppression = bool(repeat_read_suppression)
        #: ``(path, start, count) -> (digest, step)`` for every read this run has
        #: made, so a repeat can be recognised without another trip to disk.
        self._read_ranges: Dict[tuple[str, Optional[int], Optional[int]], tuple[str, int]] = {}
        self._repeat_reads = 0
        #: Skill name -> the step whose request loaded its text, so a brief that
        #: drops that text can still say which recipe it was and when.
        self._loaded_skills: Dict[str, int] = {}
        self._captures = 0
        self._last_capture_digest = ""
        #: The step number of the last frame shown, so a burst of small edits
        #: does not pay for a picture each.
        self._last_capture_step = -RENDER_CAPTURE_MIN_STEPS
        self._pending_render_image: Optional[bytes] = None
        self._pending_render_page = ""
        self._screenshots_dropped = False
        # The frame the model has not yet graded, and how many times each
        # rendered artifact has been offered for grading.
        self._render_review_frame: Optional[bytes] = None
        self._render_review_page = ""
        self._render_review_digest = ""
        self._render_review_identity: Dict[str, Any] = {}
        self._reviewed_renders: Dict[str, int] = {}
        self._settled_renders: Set[str] = set()
        self._awaiting_visual_review = False
        # The requirement IDs the outstanding review was asked to cover, and the
        # ledger digest they came from. A grade answers the ledger it was shown,
        # so both travel with the review turn instead of being re-derived.
        self._pending_review_ids: List[str] = []
        self._pending_review_ledger = ""
        self._verified_snapshot: Dict[str, Any] = {}
        self._restored_from_step: Optional[int] = None
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
        # A discovery note is also part of the request.  Track it separately
        # from schema bytes so "avoided" never overstates the bytes the
        # provider actually did not receive.
        self._tool_discovery_note_bytes = 0
        self._tool_schema_bytes_saved_net = 0
        self._tool_universe_applied = False
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

    def _shadow_before_main_model(
        self,
        record: SessionRecord,
        request: ModelRequest,
    ) -> None:
        """Capture a prospective Phase 0 decision without changing the real step."""

        if not self.semantic_shadow:
            self._shadow_snapshot = None
            self._shadow_decision = None
            return
        try:
            active_ids = tuple(requirements_ledger.expected_ids(record.requirements))
            active = set(active_ids)
            requirement_bytes = sum(
                len(str(item.get("text") or "").encode("utf-8"))
                for item in record.requirements
                if str(item.get("id") or "") in active
            )
            plan_counts = Counter(
                str(item.get("status") or "")
                for item in record.plan_items
                if isinstance(item, dict)
            )
            request_chars, dynamic_chars = request_char_counts(request.messages)
            actual_tool_schema_bytes = len(
                json.dumps(
                    self._provider_tools_payload(request.tools),
                    ensure_ascii=False,
                    sort_keys=True,
                ).encode("utf-8")
            )
            runtime_transitions = tuple(
                name for name, _arguments in self._harness_final_calls(record)
            )
            snapshot = ShadowSnapshot(
                step=self._step,
                session_revision=record.revision,
                phase=str(record.phase),
                status=str(record.status),
                repo_fingerprint=str(record.repo_fingerprint),
                branch=str(record.branch),
                changed_files=tuple(sorted(str(path) for path in record.changed_files)),
                requirements_digest=(
                    requirements_ledger.ledger_digest(record.requirements)
                    if record.requirements
                    else ""
                ),
                active_requirement_ids=active_ids,
                active_requirement_bytes=requirement_bytes,
                provider_history_entries=len(record.provider_history),
                request_message_count=len(request.messages),
                request_chars=request_chars,
                request_dynamic_chars=dynamic_chars,
                advertised_tool_count=len(request.tools),
                advertised_tool_schema_bytes=actual_tool_schema_bytes,
                checks_count=len(record.checks),
                failures_count=len(record.failures),
                evidence_count=len(record.evidence),
                jobs_count=len(record.jobs),
                unfinished_actions_count=len(record.unfinished_actions),
                plan_todo_count=int(plan_counts.get("todo", 0)),
                plan_doing_count=int(plan_counts.get("doing", 0)),
                plan_done_count=int(plan_counts.get("done", 0)),
                verified_snapshot_present=bool(record.verified_snapshot),
                verification_complete=self._verification(record),
                answer_complete=self._answer_complete(record),
                awaiting_visual_review=bool(self._awaiting_visual_review),
                changed_once=bool(self._changed_once),
                require_change=bool(self.require_change),
                runtime_transitions=runtime_transitions,
            )
            self._shadow_snapshot = snapshot
            self._shadow_decision = classify_before_model(snapshot)
            self._shadow_history_start = len(record.provider_history)
        except Exception:
            # Research instrumentation must never be able to stop the real run.
            self._shadow_snapshot = None
            self._shadow_decision = None

    def _shadow_pre_usage_fields(self) -> Dict[str, Any]:
        if (
            not self.semantic_shadow
            or self._shadow_snapshot is None
            or self._shadow_decision is None
        ):
            return {}
        snapshot = self._shadow_snapshot
        fields = self._shadow_decision.usage_fields()
        fields.update(
            {
                "shadow_request_chars": snapshot.request_chars,
                "shadow_dynamic_chars": snapshot.request_dynamic_chars,
                "shadow_request_messages": snapshot.request_message_count,
                "shadow_advertised_tools": snapshot.advertised_tool_count,
                "shadow_tool_schema_bytes": snapshot.advertised_tool_schema_bytes,
                "shadow_active_requirements": len(snapshot.active_requirement_ids),
                "shadow_active_requirement_bytes": snapshot.active_requirement_bytes,
                "shadow_history_entries_before": snapshot.provider_history_entries,
            }
        )
        return fields

    def _shadow_finish_main_model(
        self,
        session_id: str,
        lease: MutationLease,
        response: ModelResponse,
    ) -> None:
        """Compute post-step shadow fields without creating a session save."""

        if (
            not self.semantic_shadow
            or self._shadow_snapshot is None
            or self._shadow_decision is None
        ):
            return
        snapshot = self._shadow_snapshot
        decision = self._shadow_decision
        try:
            current = self.sessions.load(session_id)
            delta = tuple(current.provider_history[self._shadow_history_start :])
            effect = audit_history_delta(
                snapshot=snapshot,
                new_entries=delta,
                finish_reason=response.finish_reason,
            )
            # Keep the effect in process until the real control flow performs its
            # next normal SessionRecord save. Persisting research telemetry must
            # not create an extra revision of authoritative task state.
            self._shadow_pending_effect = (
                self._step,
                decision.snapshot_sha256,
                effect.usage_fields(),
            )
        except Exception:
            # Shadow accounting is intentionally fail-open with respect to the run.
            self._shadow_pending_effect = None
        finally:
            self._shadow_snapshot = None
            self._shadow_decision = None

    def _shadow_apply_pending_usage(self, record: SessionRecord) -> None:
        """Fold pending shadow effect into an already-required session save."""

        pending = self._shadow_pending_effect
        if not self.semantic_shadow or pending is None:
            return
        step, snapshot_sha256, fields = pending
        raw_events = record.usage.get(USAGE_EVENTS_KEY)
        if not isinstance(raw_events, list):
            return
        for event in reversed(raw_events):
            if not isinstance(event, dict):
                continue
            if int(event.get("step", -1)) != step:
                continue
            if event.get("shadow_snapshot_sha256") != snapshot_sha256:
                continue
            event.update(fields)
            self._shadow_pending_effect = None
            return

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
            elif (
                event.kind is ModelEventKind.REASONING_SUMMARY_DELTA
                and event.reasoning_summary_delta
            ):
                self._emit(
                    AgentEventKind.REASONING_SUMMARY_DELTA,
                    summary=str(redact(event.reasoning_summary_delta)),
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
        candidate_note = ""
        if self.economy_mode and families is not None and omitted:
            omitted_names: Dict[str, List[str]] = {}
            for index in omitted:
                alias, core_name = pairs[index]
                omitted_names.setdefault(family_of(core_name), []).append(alias)
            candidate_note = discovery_note(omitted_names)

        # Deferral must reduce the actual request, not merely its schema
        # portion.  For a small conditional family the lossless discovery
        # note can be larger than the schemas it explains.  Keeping those
        # schemas attached preserves every capability while avoiding a token
        # regression.  The comparison uses UTF-8 bytes, the same unit used by
        # the provider-payload accounting below.
        # Include the separating newline and JSON escaping, just like the
        # serialized schema estimate. Subtract only the two string quotes.
        note_bytes = (
            len(json.dumps("\n" + candidate_note, ensure_ascii=False).encode("utf-8")) - 2
            if candidate_note else 0
        )
        gross_avoided = self._tool_schema_bytes_avoided
        applied = bool(
            self.economy_mode
            and families is not None
            and omitted
            and gross_avoided > note_bytes
        )
        self._universe_note = candidate_note if applied else ""
        self._tool_discovery_note_bytes = note_bytes if applied else 0
        self._tool_schema_bytes_saved_net = (
            gross_avoided - self._tool_discovery_note_bytes if applied else 0
        )
        self._tool_universe_applied = applied
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
        description = definition.description
        schema = definition.input_schema
        if alias in MUTATION_ALIASES:
            schema = dict(schema)
            properties = dict(schema.get("properties") or {})
            properties["more_semantic_changes"] = {
                "type": "boolean",
                "description": (
                    "Required continuation decision. Set true only when you already "
                    "know a specific additional code/config/test mutation is required "
                    "after this one. Set false when this is the last semantic mutation "
                    "and only tests, checks, verification, Git evidence or reporting "
                    "remain; KaroX collects that evidence itself and returns failures "
                    "for repair."
                ),
            }
            schema["properties"] = properties
            required = list(schema.get("required") or [])
            if "more_semantic_changes" not in required:
                required.append("more_semantic_changes")
            schema["required"] = required
            description += (
                " Always include more_semantic_changes=true or false. Use true only "
                "for a known later mutation; use false when only verification or "
                "reporting remains, so KaroX can finalize mechanically."
            )
        if alias in {
            provider_alias("checks.run"),
            provider_alias("git.status"),
            provider_alias("git.diff"),
        }:
            description += (
                " Native-agent finalization: do not call this merely to prove "
                "completion. When the implementation is ready, answer without a "
                "tool and KaroX will collect any missing final evidence itself. "
                "Use this tool mid-task only when its result will change your next action."
            )
        return ProviderTool(alias, description, schema)

    @staticmethod
    def _provider_tools_payload(
        tools: Sequence[ProviderTool],
    ) -> list[dict[str, Any]]:
        return [
            {
                "name": tool.name,
                "description": tool.description,
                "parameters": tool.input_schema,
            }
            for tool in tools
        ]

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
                self.sessions.heartbeat(
                    lease,
                    ttl_seconds=lease_ttl,
                    session_revision=record.revision,
                )
                self._known_revision = record.revision
                self._step = steps + 1
                self._emit(AgentEventKind.STEP_STARTED)
                request_tools = self._advertised_provider_tools
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
                # A brief for the turns this step is about to drop has to exist
                # before the request is assembled, and it has to be written
                # before the turns are gone: this is the one place both are true.
                # The plan it returns is handed to the request builder, so the
                # decision is made once and not recomputed from the same history.
                step_plan = self._prepare_compaction(
                    session_id, lease, record, deadline - self.monotonic()
                )
                plan_noted = self._note_the_plan(
                    session_id,
                    lease,
                    record,
                    steps,
                    compacting=step_plan is not None,
                )
                if plan_noted is not None:
                    record = plan_noted
                noted = self._note_the_deadline(
                    session_id, lease, record, deadline - self.monotonic()
                )
                if noted is not None:
                    record = noted
                paced = self._note_the_pace(session_id, lease, record, steps)
                if paced is not None:
                    record = paced
                messages = tuple(
                    self._request_messages(record.provider_history, plan=step_plan)
                )
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
                self._active_output_ceiling = self._effective_max_output_tokens(record)
                request = ModelRequest(
                    model=self.model,
                    messages=messages,
                    tools=request_tools,
                    deadline_seconds=min(remaining, 3600.0),
                    # Every step resends the whole transcript, so the stable
                    # prefix (system prompt + tool schemas) plus the session id
                    # is exactly the right cache key: an unchanged prefix keeps
                    # billing at cache-read rate, and a changed prefix stops
                    # advertising a cache entry the provider no longer holds.
                    cache_key=prefix_key.key,
                    max_output_tokens=self._active_output_ceiling,
                    reasoning_effort=self._effective_reasoning_effort(record),
                )
                self._shadow_before_main_model(record, request)
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
                    if self._shrink_context_after_rejection(
                        session_id, lease, exc, list(record.provider_history)
                    ):
                        # The provider just stated a limit no registry knew. Send
                        # this same step again against the smaller budget instead
                        # of ending a run that was making progress.
                        continue
                    if self._drop_render_feedback_after_rejection(session_id, exc):
                        # A model that cannot take images must lose the pictures,
                        # not the run: the capture is a feedback aid, and the run
                        # is the work.
                        continue
                    if (
                        exc.kind is ProviderErrorKind.TRANSPORT
                        and self.monotonic() >= deadline - 1.0
                    ):
                        # The request was cut by this run's own wall-clock budget
                        # (the adapter received min(remaining, ...) as its
                        # deadline), not by the provider. Reporting it as a
                        # provider failure sent the post-mortem after the gateway
                        # while the real cause was the time limit.
                        return self._finish(
                            session_id,
                            lease,
                            "stopped",
                            record.phase,
                            "wall_time_limit",
                            self._last_provider_message(record),
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
                    self._shadow_finish_main_model(session_id, lease, response)
                    suffix = response.budget_reason or "budget"
                    return self._finish(
                        session_id,
                        lease,
                        "stopped",
                        "budget",
                        f"budget_exceeded:{suffix}",
                        provider_message,
                    )
                if response.finish_reason == "length" and not response.tool_calls:
                    self._shadow_finish_main_model(session_id, lease, response)
                    if self._visual_repair_output_recovery_available(record):
                        self._emit(
                            AgentEventKind.WARNING,
                            detail="visual repair clipped before mutation; retrying once",
                        )
                        self._append_message(
                            session_id,
                            lease,
                            {
                                "role": "user",
                                "content": VISUAL_REPAIR_OUTPUT_RECOVERY_PROMPT,
                                "kind": "visual_repair_output_recovery",
                            },
                            phase="verification",
                        )
                        continue
                    # The provider cut the response off at the output ceiling: the
                    # model never got to emit its call, so the turn produced no
                    # work at all. Falling through reported this as the "no
                    # changes" stop -- the label for a model that decided to do
                    # nothing. One bounded recovery turn asks for the same work in
                    # pieces; if that reply is truncated too, the run ends on the
                    # limit by name instead of losing what was already spent.
                    if self._output_limit_recovery_available(
                        record.provider_history
                    ):
                        self._emit(
                            AgentEventKind.WARNING,
                            detail="response cut off at the output limit; asking for a smaller step",
                        )
                        self._append_message(
                            session_id,
                            lease,
                            {
                                "role": "user",
                                "content": OUTPUT_LIMIT_PROMPT,
                                "kind": "output_limit_prompt",
                            },
                            phase="execution",
                        )
                        continue
                    if self._failed_check_output_recovery_available(record):
                        self._emit(
                            AgentEventKind.WARNING,
                            detail=(
                                "repair response was cut off after a successful "
                                "failed-check diagnostic read; forcing one final action turn"
                            ),
                        )
                        self._append_message(
                            session_id,
                            lease,
                            {
                                "role": "user",
                                "content": FAILED_CHECK_OUTPUT_RECOVERY_PROMPT,
                                "kind": "failed_check_output_recovery",
                            },
                            phase="verification",
                        )
                        continue
                    return self._finish(
                        session_id,
                        lease,
                        "stopped",
                        "provider",
                        "output_token_limit",
                        provider_message,
                    )
                content_text = str(response.content or "")
                lowered_content = content_text.lower()
                unparsed_tool_markup = (
                    not response.tool_calls
                    and "<tool_call" in lowered_content
                    and (
                        "<function=" in lowered_content
                        or "<parameter=" in lowered_content
                        or "<arg_key>" in lowered_content
                    )
                )
                if unparsed_tool_markup:
                    self._shadow_finish_main_model(session_id, lease, response)
                    if self._prompt_count(
                        record.provider_history, "tool_protocol_prompt"
                    ) < 1:
                        self._emit(
                            AgentEventKind.WARNING,
                            detail=(
                                "provider returned tool-call markup as plain text; "
                                "asking for a real tool call"
                            ),
                        )
                        self._append_message(
                            session_id,
                            lease,
                            {
                                "role": "user",
                                "content": TOOL_PROTOCOL_PROMPT,
                                "kind": "tool_protocol_prompt",
                            },
                            phase="execution",
                        )
                        continue
                    return self._finish(
                        session_id,
                        lease,
                        "stopped",
                        "provider",
                        "malformed_tool_protocol",
                        provider_message,
                    )
                if not response.tool_calls and not content_text.strip():
                    self._shadow_finish_main_model(session_id, lease, response)
                    # An empty response is not a decision either: the model sent
                    # neither text nor a call, so the turn produced nothing. Left
                    # alone this reached the "no changes" stop through the answer
                    # nudge -- two wasted turns and a reason that named the wrong
                    # cause. One bounded recovery asks for the next concrete step.
                    if self._prompt_count(record.provider_history, "empty_response_prompt") < 1:
                        self._emit(
                            AgentEventKind.WARNING,
                            detail="the provider returned an empty response; asking for the next step",
                        )
                        self._append_message(
                            session_id,
                            lease,
                            {
                                "role": "user",
                                "content": EMPTY_RESPONSE_PROMPT,
                                "kind": "empty_response_prompt",
                            },
                            phase=record.phase,
                        )
                        continue
                    return self._finish(
                        session_id,
                        lease,
                        "stopped",
                        "provider",
                        "empty_response",
                        provider_message,
                    )
                repeated = False
                if len(response.tool_calls) > 1:
                    self._batched_turns_avoided += len(response.tool_calls) - 1

                # A batch of pure reads is independent work, so it runs
                # concurrently while every session write stays in the batch's own
                # order below. A batch containing any mutation keeps the
                # sequential path: the decision to change the repository must not
                # race the reads that informed it.
                batch_results: Dict[str, Any] = {}
                if self._is_inspection_batch(response.tool_calls):
                    inspection_record = self.sessions.load(session_id)
                    # Do not speculatively execute a concurrent read that the
                    # repair gate/argument-aware detector will reject moments
                    # later. Precomputation must preserve the same semantics as
                    # the sequential path, including read-loop suppression.
                    blocked = any(
                        self._repair_inspection_refusal(inspection_record, call)
                        is not None
                        for call in response.tool_calls
                    )
                    if not blocked:
                        batch_results = self._inspection_batch_results(
                            session_id, response.tool_calls, deadline
                        )
                finish_call_ids = self._finish_attempt_call_ids(response)
                final_check_call_ids = self._final_check_attempt_call_ids(response)
                finish_attempt_ran = False
                finish_attempt_verified = False
                final_check_verified = False
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
                        session_id,
                        lease,
                        call,
                        deadline,
                        ledger,
                        precomputed=batch_results.get(call.call_id),
                    ) or repeated
                    self.sessions.heartbeat(
                        lease,
                        ttl_seconds=lease_ttl,
                        session_revision=self._known_revision,
                    )
                mistake_record = self.sessions.load(session_id)
                mistake_state = self._mutation_mistake_state(
                    mistake_record.provider_history
                )
                if (
                    mistake_state is not None
                    and int(mistake_state.get("count") or 0)
                    >= MUTATION_MISTAKE_THRESHOLD
                    and int(mistake_state.get("nudges") or 0) < 1
                ):
                    failures = mistake_state.get("failures")
                    failure_text = "\n".join(
                        f"- {item}" for item in failures
                    ) if isinstance(failures, list) else "- mutation failed"
                    detail = MUTATION_MISTAKE_RECOVERY_PROMPT.format(
                        count=int(mistake_state["count"]),
                        target=str(mistake_state["target"]),
                        failures=failure_text,
                    )
                    self._append_message(
                        session_id,
                        lease,
                        {
                            "role": "user",
                            "kind": "mutation_mistake_recovery",
                            "content": detail,
                        },
                        phase="verification",
                    )
                    self._emit(
                        AgentEventKind.WARNING,
                        reason="mutation_strategy_change_required",
                        detail=str(mistake_state["target"]),
                    )
                    self._shadow_finish_main_model(session_id, lease, response)
                    self._emit(
                        AgentEventKind.STEP_FINISHED,
                        usage=dict(response.usage),
                        reason=response.finish_reason,
                    )
                    continue
                if self._all_repair_inspection_calls(response.tool_calls):
                    inspected_record = self.sessions.load(session_id)
                    visual_state = self._visual_repair_inspection_state(inspected_record)
                    if (
                        visual_state is not None
                        and visual_state[0] >= VISUAL_REPAIR_INSPECTION_ROUNDS
                        and visual_state[1] < 1
                    ):
                        rounds, _nudges, failure = visual_state
                        self._append_message(
                            session_id,
                            lease,
                            {
                                "role": "user",
                                "kind": "visual_repair_action",
                                "content": (
                                    "The visual reviewer already named concrete visible defects, "
                                    f"and you have spent {rounds} semantic turns locating anchors. "
                                    "Stop inspecting now. In your next response make one bounded "
                                    "mutation batch that addresses the listed defects. If 2-4 "
                                    "independent edits are known, emit them in the SAME response "
                                    "or use one repo_edit_file edits=[...] call. Do not request "
                                    "another read/search batch first.\n\n"
                                    f"Current visual repair evidence:\n{failure}"
                                ),
                            },
                            phase="verification",
                        )
                        self._emit(
                            AgentEventKind.WARNING,
                            reason="visual_repair_action_required",
                            detail=failure,
                        )
                        self._shadow_finish_main_model(session_id, lease, response)
                        self._emit(
                            AgentEventKind.STEP_FINISHED,
                            usage=dict(response.usage),
                            reason=response.finish_reason,
                        )
                        continue
                    failed_state = self._failed_check_inspection_state(
                        inspected_record
                    )
                    if (
                        failed_state is not None
                        and failed_state[0] >= FAILED_CHECK_INSPECTION_ROUNDS
                        and failed_state[1] < 1
                    ):
                        rounds, _nudges, failure = failed_state
                        self._append_message(
                            session_id,
                            lease,
                            {
                                "role": "user",
                                "kind": "failed_check_action",
                                "content": (
                                    "Verification is still failing and you have "
                                    f"already spent {rounds} semantic turns on "
                                    "read/search inspection since that failure. "
                                    "Stop broad inspection now. Make one targeted "
                                    "repo edit that addresses the concrete failure "
                                    "below, or rerun the approved check if the "
                                    "current bytes already fix it. Do not request "
                                    "another broad read/search batch first.\n\n"
                                    f"Current failed-check evidence:\n{failure}"
                                ),
                            },
                            phase="verification",
                        )
                        self._emit(
                            AgentEventKind.WARNING,
                            reason="failed_check_action_required",
                            detail=failure,
                        )
                        self._shadow_finish_main_model(
                            session_id, lease, response
                        )
                        self._emit(
                            AgentEventKind.STEP_FINISHED,
                            usage=dict(response.usage),
                            reason=response.finish_reason,
                        )
                        continue
                if finish_call_ids:
                    finish_record = self.sessions.load(session_id)
                    if self._finish_attempt_succeeded(finish_record, finish_call_ids):
                        self._run_harness_final_checks(
                            session_id, lease, deadline, ledger, finish_record
                        )
                        finish_record = self.sessions.load(session_id)
                        finish_attempt_ran = True
                        missing = completion_gate.floor(self.core, finish_record)
                        verified = self._verification(finish_record)
                        finish_attempt_verified = verified
                        if not verified:
                            failure = self._failure_summary(finish_record)
                            summary = (
                                "KaroX ran the requested finalization, but verification "
                                "is incomplete. Missing: "
                                + ("; ".join(missing) if missing else "unknown")
                                + (f". Failure: {failure}" if failure else "")
                                + ". Continue working; do not claim completion yet."
                            )
                            self._append_message(
                                session_id,
                                lease,
                                {
                                    "role": "user",
                                    "content": summary,
                                    "kind": "finish_attempt_result",
                                },
                                phase="verification",
                            )
                if final_check_call_ids:
                    check_record = self.sessions.load(session_id)
                    if self._final_check_attempt_succeeded(
                        check_record, final_check_call_ids
                    ):
                        self._run_harness_final_checks(
                            session_id, lease, deadline, ledger, check_record
                        )
                        check_record = self.sessions.load(session_id)
                        final_check_verified = self._verification(check_record)
                self._shadow_finish_main_model(session_id, lease, response)
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
                    # A step that ran the check may be the moment the artifact
                    # became provable. Snapshotting here is what lets a later
                    # regression be undone instead of shipped -- and it keys on the
                    # check itself, because the run this protects never completed
                    # the Git tail the reporting chain also wants.
                    if finish_attempt_ran or self._step_ran_the_check(response):
                        record = self.sessions.load(session_id)
                        passing = self._current_passing_check(record) is not None
                        # Keyed to the passing check, not to the full completion
                        # floor: this is a restore point, never a verdict. _finish
                        # re-runs the floor after any restore, so these bytes are
                        # never reported as verified on the strength of this snapshot.
                        if passing:
                            self._snapshot_verified_state(session_id, lease, record)
                            # Quality feedback is useful immediately, but final
                            # Git proof is not: the model may have run a green
                            # check in the middle of a larger change. Run only the
                            # isolated visual gate here and leave terminal proof
                            # to an explicit finish/stop.
                            visual_repair_prompted = self._run_early_visual_check(
                                session_id, lease, deadline, ledger, record
                            )
                            record = self.sessions.load(session_id)
                            if visual_repair_prompted:
                                continue
                        self._capture_render(record)
                        # Legacy in-transcript render feedback remains the
                        # fallback when no isolated visual checker is available.
                        if passing and self._start_visual_review(
                            session_id, lease, record
                        ):
                            continue
                    if finish_attempt_verified or final_check_verified:
                        record = self.sessions.load(session_id)
                        # Exact current-byte technical + vision proof is a commit
                        # point. Do not reopen semantic review or the legacy
                        # in-transcript visual grader after the isolated checker
                        # has already proved the delivered identity.
                        if self._current_visual_completion_proven(record):
                            return self._finish(
                                session_id,
                                lease,
                                "verified",
                                "completed",
                                "verified",
                                provider_message,
                            )
                        if self._request_edge_case_review(
                            session_id,
                            lease,
                            record,
                            deadline - self.monotonic(),
                        ):
                            continue
                        if self._start_visual_review(session_id, lease, record):
                            continue
                        return self._finish(
                            session_id,
                            lease,
                            "verified",
                            "completed",
                            "verified",
                            provider_message,
                        )
                    continue
                record = self.sessions.load(session_id)
                if not self._awaiting_visual_review:
                    # A current visual defect is a coding obligation, not a reason
                    # to repeat proof on unchanged bytes. Feed the compact verdict
                    # back before doing any terminal Git/check work.
                    if self._append_visual_repair_prompt(
                        session_id, lease, record
                    ):
                        continue
                    # The model stopped to answer. When it changed files and the
                    # evidence chain is incomplete, the harness collects the
                    # finishing evidence itself rather than spending a model turn
                    # asking for what it can run in a few hundred milliseconds.
                    self._run_harness_final_checks(
                        session_id, lease, deadline, ledger, record
                    )
                    record = self.sessions.load(session_id)
                    if self._append_visual_repair_prompt(
                        session_id, lease, record
                    ):
                        continue
                if self._awaiting_visual_review:
                    # This answer is the grade the run owes on its own render.
                    settled = self._settle_visual_review(
                        session_id, lease, record, provider_message
                    )
                    if settled is None:
                        continue
                    return settled
                if self._verification(record):
                    # Once the exact current identity has both authoritative
                    # browser/technical proof and isolated vision PASS, success
                    # is terminal. A new model turn can only waste budget or
                    # mutate bytes whose acceptance has already been proved.
                    if self._current_visual_completion_proven(record):
                        return self._finish(
                            session_id,
                            lease,
                            "verified",
                            "completed",
                            "verified",
                            provider_message,
                        )
                    if self._request_edge_case_review(
                        session_id, lease, record, deadline - self.monotonic()
                    ):
                        continue
                    if self._start_visual_review(session_id, lease, record):
                        continue
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
                owed = completion_gate.floor(self.core, record) if mutated else []
                legacy_visual_attempted = any(
                    isinstance(item, dict) and item.get("kind") == "render_review"
                    for item in record.evidence
                )
                current_visual = self._current_visual_check_data(record)
                isolated_visual_exhausted = False
                if current_visual is not None and current_visual.get("verdict") != "pass":
                    checks_left = current_visual.get("checks_left")
                    isolated_visual_exhausted = (
                        isinstance(checks_left, int) and checks_left <= 0
                    ) or (
                        self._prompt_count(record.provider_history, "visual_repair")
                        >= self.limits.max_repair_prompts
                    )
                current_identity = completion_gate.identity(self.core, record)
                current_render_reviews = sum(
                    1
                    for item in record.evidence
                    if isinstance(item, dict)
                    and item.get("kind") == "render_review"
                    and isinstance(item.get("metadata"), dict)
                    and item["metadata"].get("completion_identity") == current_identity
                )
                legacy_visual_exhausted = (
                    legacy_visual_attempted
                    and current_render_reviews >= VISUAL_REVIEWS_PER_RENDER
                )
                if (
                    mutated
                    and (legacy_visual_exhausted or isolated_visual_exhausted)
                    and completion_gate.visual_required(record)
                    and not completion_gate.visual_proven(self.core, record)
                ):
                    return self._finish(
                        session_id,
                        lease,
                        "stopped",
                        "verification",
                        "render_unverified",
                        provider_message,
                    )
                if completion_gate.only_unproducible(owed) and not (
                    legacy_visual_attempted and not legacy_visual_exhausted
                ):
                    # What is still owed is proof no approved command can
                    # produce. A repair turn cannot supply it and Core refuses any
                    # substitute the model might try, so the run ends UNVERIFIED
                    # and says which approval is missing.
                    detail = "; ".join(
                        item for item in owed
                        if item.startswith(completion_gate.NO_PRODUCER)
                    )
                    self._emit(
                        AgentEventKind.WARNING,
                        reason="no_approved_producer",
                        detail=detail,
                    )
                    return self._finish(
                        session_id,
                        lease,
                        "stopped",
                        "verification",
                        "unverified_no_producer",
                        provider_message,
                        detail=detail,
                    )
                if self._prompt_count(record.provider_history, kind) >= ceiling:
                    terminal_reason = (
                        "render_unverified"
                        if mutated
                        and completion_gate.visual_required(record)
                        and not completion_gate.visual_proven(self.core, record)
                        else ("unverified_changes" if mutated else "no_changes")
                    )
                    return self._finish(
                        session_id,
                        lease,
                        "stopped",
                        "verification",
                        terminal_reason,
                        provider_message,
                    )
                self._append_message(
                    session_id,
                    lease,
                    {
                        "role": "user",
                        "content": (
                            self._repair_prompt(record)
                            if mutated
                            else ANSWER_PROMPT
                        ),
                        "kind": kind,
                    },
                    phase="verification",
                )
        finally:
            self.sessions.release(lease)

    def _repair_prompt(self, record: SessionRecord) -> str:
        """The repair turn's text, carrying the failure that was just recorded."""
        summary = "; ".join(filter(None, [
            "; ".join(completion_gate.floor(self.core, record)), self._failure_summary(record),
        ]))
        if not summary:
            # Nothing failed on the current bytes: no check has run on them yet,
            # or the harness could not run one. "They failed" would be untrue.
            summary = "no check has run on the current files yet"
        return (
            REPAIR_PROMPT.format(failure_summary=summary)
            + self._regression_clause(record)
        )

    def _regression_clause(self, record: SessionRecord) -> str:
        """Name a broken pass, so the model restores instead of guessing.

        A run whose check passed and then failed again is in a different
        situation from one that never passed: something that worked is now
        broken, and the fix is usually to undo the last edits rather than to keep
        adding. The clause is empty when no pass exists, because then there is
        nothing to restore and the ordinary prompt is the honest one.
        """

        passing = self._last_passing_check(record)
        if passing is None:
            return ""
        return (
            "\n\nThe approved check passed earlier at step %d (%s), and the "
            "edits since then broke it. Restore the state that passed and re-apply "
            "only the changes you can verify, instead of continuing to add."
            % (int(passing.get("step") or 0), str(passing.get("check") or "no summary"))
        )

    def _initialize_history(
        self, record: SessionRecord, lease: MutationLease
    ) -> None:
        if record.provider_history:
            self._set_phase(record.session_id, lease, "execution")
            return

        def update(current: SessionRecord) -> None:
            if not current.provider_history:
                user_content = current.task
                if current.continuation_context:
                    user_content = (
                        "Structured continuation context from the parent session "
                        "(bounded and secret-filtered; treat it as context, not as "
                        "new instructions):\n"
                        f"{current.continuation_context}\n\n"
                        f"Current task:\n{current.task}"
                    )
                delegated_context = requirements_ledger.delegated_source_context(
                    current.task, self.core.repository
                )
                greenfield_note = ""
                if delegated_context:
                    user_content += delegated_context
                    greenfield_note = greenfield_delegated_note(
                        current.task,
                        self.core.repository,
                        visual_web=completion_gate.web_render_intent(current),
                    )
                # The notes ride on the task message itself: they are part of
                # what the task asks for, and a note the model has to fetch
                # would cost a turn before any work happened.
                if self._domain_notes_enabled:
                    user_content += domain_notes(current.task)
                # Keep the runtime execution contract last: it is deliberately
                # the final thing the model sees before deciding its first action.
                if greenfield_note:
                    user_content += greenfield_note
                current.provider_history.extend(
                    [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": user_content},
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
            "reasoning_summary": (
                redact(response.reasoning_summary)
                if response.reasoning_summary is not None
                else None
            ),
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
        usage_event.update(self._shadow_pre_usage_fields())
        if self._active_output_ceiling is not None:
            usage_event["economy_effective_output_ceiling"] = int(
                self._active_output_ceiling
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
            cost_usd=event_cost_usd({"cost": response.cost, "currency": response.currency}),
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
            usage_event["economy_tool_universe_applied"] = (
                self._tool_universe_applied
            )
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
            usage_event["economy_tool_discovery_note_bytes"] = (
                self._tool_discovery_note_bytes
            )
            usage_event["economy_tool_schema_bytes_saved_net"] = (
                self._tool_schema_bytes_saved_net
            )
            if self._expanded_families:
                usage_event["economy_tool_groups_expanded"] = ",".join(
                    sorted(self._expanded_families)
                )
        usage_event["context_rewrite_batches"] = self._rewrite_batches
        usage_event["context_rewrite_pending_chars"] = self._rewrite_pending_chars
        usage_event["inspection_context_rewrite_batches"] = (
            self._inspection_rewrite_batches
        )
        usage_event["economy_inspection_chars_elided"] = int(
            self._economy_inspection_chars_pending
        )
        usage_event["economy_read_cache_hits"] = self._read_cache.hits
        usage_event["economy_read_cache_misses"] = self._read_cache.misses
        # Which window this run budgeted against, and whether that is a fact
        # about the model or a choice. A run compacting at a tier it selected
        # must not read afterwards as if the model's window had been known.
        usage_event["economy_context_window_tokens"] = int(
            self.context.max_input_tokens or self.context.fallback_input_tokens
        )
        usage_event["economy_context_window_source"] = self.context.window_source
        usage_event["economy_context_ceiling_tokens"] = self.context.token_ceiling
        if self._learned_context_window:
            usage_event["economy_context_window_learned"] = int(
                self._learned_context_window
            )
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
            # Keep the legacy economy field truthful: deferred tool-universe and
            # other quality-sensitive reductions still require /economy. Exact
            # duplicate/superseded result references are a separate lossless
            # transport optimisation and are on by default.
            usage_event["economy_context_applied"] = bool(self.economy_mode)
            usage_event["lossless_context_rewrite_applied"] = bool(
                self.lossless_context_rewrite
            )
        stale_chars = int(getattr(self, "_economy_stale_chars_pending", 0))
        if stale_chars > 0:
            usage_event["economy_stale_chars"] = stale_chars
            usage_event["economy_stale_estimated_tokens"] = int(
                stale_chars / float(self.context.chars_per_token)
            )
        superseded_calls = self._call_payload_compilation
        if superseded_calls is not None:
            # Written even at zero so the session view can say MEASURED rather
            # than "unknown": the rule ran on this request either way.
            usage_event["economy_superseded_calls_elided"] = (
                superseded_calls.stats.payloads_elided
            )
            usage_event["economy_superseded_call_chars"] = (
                superseded_calls.stats.chars_saved
            )
        superseded_chars = int(
            getattr(self, "_economy_superseded_call_chars_pending", 0)
        )
        if superseded_chars > 0:
            # The model's own superseded drafts: measured in this process and
            # never presented as provider-reported savings.
            usage_event["economy_superseded_call_estimated_tokens"] = int(
                superseded_chars / float(self.context.chars_per_token)
            )
        completed_mutation_chars = int(
            getattr(self, "_economy_completed_mutation_chars_pending", 0)
        )
        if completed_mutation_chars > 0:
            usage_event["economy_completed_mutation_chars"] = completed_mutation_chars
            usage_event["economy_completed_mutation_estimated_tokens"] = int(
                completed_mutation_chars / float(self.context.chars_per_token)
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

    def _record_side_call(
        self,
        session_id: str,
        lease: MutationLease,
        response: ModelResponse,
        *,
        kind: str,
    ) -> None:
        """Account for a provider call the loop made for itself.

        A compaction brief is real spend on the same account as the steps, so it
        goes through the same ledger, the same usage merge and the same route
        audit. What it must *not* do is leave a turn in the transcript: the model
        never wrote that message as part of the task, and a fake assistant turn
        would be worse than no record at all.
        """

        usage_event = usage_event_from_response(
            session_id=session_id,
            step=self._step,
            response=response,
        )
        usage_event["kind"] = kind
        self._cost_ledger.record(
            session_id=session_id,
            step=self._step,
            provider=str(response.selected_provider or self.provider.provider_name),
            model=str(response.selected_model or self.model),
            input_tokens=int(usage_event.get("prompt_tokens", 0)),
            output_tokens=int(usage_event.get("completion_tokens", 0)),
            cache_read_tokens=int(usage_event.get("cache_read_tokens", 0)),
            cache_write_tokens=int(usage_event.get("cache_write_tokens", 0)),
            cost_usd=event_cost_usd({"cost": response.cost, "currency": response.currency}),
            timestamp=float(usage_event["timestamp"]),
        )

        decision = self._cost_governor.evaluate(
            current_cost_usd=self._cost_ledger.total_cost(session_id)
        )
        if decision.warning:
            usage_event["economy_budget_warning"] = decision.warning

        def update(record: SessionRecord) -> None:
            route_audit = self._route_audit(response)
            if route_audit is not None:
                record.provider_history.append(dict(redact(route_audit)))
            record.usage = merge_response_usage(
                record.usage,
                response,
                event=usage_event,
                model_fallback=self.model,
            )

        self._update_session(session_id, lease, update)

    def _drop_render_feedback_after_rejection(
        self, session_id: str, exc: ProviderError
    ) -> bool:
        """Give up the pictures, not the run, when a model refuses images.

        Vision is advertised inconsistently -- the endpoint measured here answers
        correctly about an image, the registry does not say so -- so the feature
        is tried and then withdrawn on the first refusal, once, with the run
        continuing exactly as it would have without it.
        """

        if self._screenshots_dropped or self._captures == 0:
            return False
        if exc.kind is not ProviderErrorKind.INVALID_REQUEST:
            return False
        self._screenshots_dropped = True
        self._render_feedback = False
        self._pending_render_image = None
        self._emit(
            AgentEventKind.WARNING,
            reason="render_feedback_dropped",
            detail=(
                "the endpoint refused a request carrying a rendered frame; "
                "continuing without showing the model its own page"
            ),
        )
        del session_id
        return True

    def _shrink_context_after_rejection(
        self,
        session_id: str,
        lease: MutationLease,
        exc: ProviderError,
        history: List[Dict[str, Any]],
    ) -> bool:
        """Learn the real window from a provider that refused the request size.

        A registry that does not state a model's window leaves the loop guessing,
        and the guess is what this recovers from: the provider has just said the
        request is too big, which is better information than any default. One
        bounded halving, recorded rather than silent, turns a dead run into a
        continued one -- and only when the refusal actually names a size, so a
        malformed request is never papered over as a context problem.
        """

        if self._context_shrink_used:
            return False
        if exc.kind is not ProviderErrorKind.INVALID_REQUEST:
            return False
        if _CONTEXT_REJECTION.search(str(exc)) is None:
            return False
        current = int(
            self.context.max_input_tokens or self.context.fallback_input_tokens
        )
        smaller = max(MIN_LEARNED_CONTEXT_WINDOW, current // 2)
        if smaller >= current:
            return False
        # Nothing to drop means nothing to retry: the same request would go out
        # again and be refused for the same reason.
        _, groups = self._split_history(list(history))
        if len(groups) <= self.context.keep_recent_groups:
            return False
        self._context_shrink_used = True
        self._learned_context_window = smaller
        self.context = self.context.with_window(smaller, source="learned")
        self._compaction_state = {}

        def update(record: SessionRecord) -> None:
            state = dict(record.context_compaction) if record.context_compaction else {}
            state["window_learned"] = smaller
            record.context_compaction = state

        self._update_session(session_id, lease, update)
        self._emit(
            AgentEventKind.WARNING,
            reason="context_window_learned",
            detail=(
                f"the provider refused a ~{current} token request; retrying with "
                f"a {smaller} token budget and a harder compaction"
            ),
        )
        return True

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
        *,
        requested_by: str = "model",
        precomputed: object = None,
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

        # Exact proof reuse must happen before the generic repeated-action guard.
        # A second approved check on unchanged bytes is not an error: it is an
        # idempotent query whose persisted PASS can be returned immediately. If
        # we refuse it as "repeated_action", the model spends another semantic
        # turn recovering from an error even though the repository is already
        # proven.
        early_core_name = self._tool_aliases.get(call.name)
        if early_core_name == "checks.run":
            try:
                early_arguments = _strict_json_loads(call.raw_arguments)
            except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
                early_arguments = None
            if isinstance(early_arguments, dict):
                identity = (
                    f"{session_id}\0{call.call_id}\0{early_core_name}".encode("utf-8")
                )
                early_digest = hashlib.sha256(identity).hexdigest()
                cached = self._cached_check_result(
                    self.sessions.load(session_id),
                    early_arguments,
                    correlation_id=f"agent-{early_digest[:32]}",
                )
                if cached is not None:
                    ledger.observe(call.name, signature)
                    self._persist_tool_result(
                        session_id,
                        lease,
                        call,
                        early_core_name,
                        cached,
                        requested_by=requested_by,
                    )
                    finished(True, "cached approved check PASS on unchanged bytes")
                    return False

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
        if requested_by == "model" and core_name in REPAIR_INSPECTION_TOOLS:
            refusal = self._repair_inspection_refusal(
                self.sessions.load(session_id), call
            )
            if refusal is not None:
                self._persist_tool_error(
                    session_id,
                    lease,
                    call,
                    "repair_inspection_blocked",
                    refusal,
                    core_name=core_name,
                )
                current = self.sessions.load(session_id)
                if self._pending_action_prompt_kind(
                    current.provider_history, {"repair_action_gate"}
                ) is None:
                    self._append_message(
                        session_id,
                        lease,
                        {
                            "role": "user",
                            "kind": "repair_action_gate",
                            "content": REPAIR_ACTION_GATE_PROMPT,
                        },
                        phase="verification",
                    )
                self._emit(
                    AgentEventKind.WARNING,
                    tool=display,
                    call_id=call.call_id,
                    reason="repair_inspection_blocked",
                    detail=refusal,
                )
                finished(False, "repair inspection blocked; act on current evidence")
                return False
        # Harness-only proof producers are intentionally absent from the model's
        # tool catalogue. A model can still hallucinate a hidden tool name after
        # seeing an earlier harness result in transcript; executing that call would
        # let it burn visual budget and even obtain a contradictory stochastic
        # verdict on unchanged bytes. Refuse it and let the harness decide whether
        # current proof is actually missing.
        if requested_by == "model" and core_name in HARNESS_ONLY_TOOLS:
            self._persist_tool_error(
                session_id,
                lease,
                call,
                "harness_only_tool",
                (
                    f"{core_name} is owned by the KaroX completion harness; "
                    "do not call it directly. Finish the coding turn and the "
                    "harness will run it once if current evidence is missing."
                ),
                core_name=core_name,
            )
            finished(False, "harness-only tool; finish and let KaroX run it")
            return False
        # Deferred universe discovery: an omitted family the model reached for
        # by exact name gets its schemas attached from the next step.
        self._note_tool_demand(core_name)
        finish_value: Optional[bool] = None
        try:
            arguments = _tool_argument_json_loads(call.raw_arguments)
            if not isinstance(arguments, dict):
                raise ValueError("tool arguments must be a JSON object")
            if core_name in MUTATION_TOOLS:
                has_more = "more_semantic_changes" in arguments
                has_legacy_finish = "finish_attempt" in arguments
                if has_more and has_legacy_finish:
                    raise ValueError(
                        "use more_semantic_changes or legacy finish_attempt, not both"
                    )
                if has_more:
                    more_changes = arguments.pop("more_semantic_changes")
                    if not isinstance(more_changes, bool):
                        raise ValueError("more_semantic_changes must be a boolean")
                    finish_value = not more_changes
                elif has_legacy_finish:
                    finish_value = arguments.pop("finish_attempt")
                    if not isinstance(finish_value, bool):
                        raise ValueError("finish_attempt must be a boolean")
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
        correlation_id = f"agent-{digest[:32]}"
        command = CoreCommand(
            name=core_name,
            arguments=arguments,
            session_id=session_id,
            origin=self.origin,
            correlation_id=correlation_id,
            idempotency_key=f"agent-{digest}" if definition.mutates else None,
            deadline_seconds=min(3600.0, max(0.1, remaining)),
        )
        if core_name in _TEST_TOOL_NAMES:
            self._emit(
                AgentEventKind.TEST_STARTED, tool=display, call_id=call.call_id
            )
        proof_before = (
            completion_gate.identity(self.core, self.sessions.load(session_id))
            if core_name in {"checks.run", "visual.check", "git.status", "git.diff"} else None
        )
        try:
            if isinstance(precomputed, BaseException):
                # A concurrent read that failed is reported the same way a
                # sequential one is: the call is the unit of failure either way.
                raise precomputed
            if isinstance(precomputed, CoreResult):
                result = precomputed
            else:
                result = self.core.execute(
                    command, lease=lease if definition.mutates else None
                )
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
        if proof_before is not None:
            proof_after = completion_gate.identity(self.core, self.sessions.load(session_id))
            proof = proof_before if proof_before == proof_after else {}
            result.data["completion_identity"] = proof
            result.data["completion_target"] = arguments.get("path", "")
            if not result.evidence:
                result.evidence.append(EvidenceRecord(
                    kind="completion_observation", summary=core_name,
                ))
            for item in result.evidence:
                item.metadata["completion_identity"] = proof
        self._attach_quick_checks(result)
        greenfield_advisory = self._delegated_source_greenfield_advisory(
            self.sessions.load(session_id), core_name, arguments, result
        )
        if greenfield_advisory:
            result.data.setdefault("next_action_advisory", greenfield_advisory)
        if (
            core_name in MUTATION_TOOLS
            and finish_value is False
            and result.ok
            and isinstance(result.data, dict)
            and result.data.get("changed") is True
        ):
            result.data["next_action_advisory"] = (
                "You said more semantic changes remain. Make the known "
                "code/config/test mutations next. If 2-4 independent mutations "
                "are already fully specified, batch them in the same assistant "
                "response (or one repo_edit_file edits=[...] call) instead of "
                "spending one model turn per mutation. If no semantic mutation is "
                "actually needed and only verification, Git evidence, or reporting "
                "remain, reply with no tool call; KaroX will collect the final "
                "evidence itself."
            )
        self._persist_tool_result(
            session_id, lease, call, core_name, result, requested_by=requested_by
        )
        finished(bool(result.ok), self._result_summary(result))
        for derived_kind, derived_fields in _derived_tool_events(core_name, result):
            self._emit(
                derived_kind, tool=display, call_id=call.call_id, **derived_fields
            )
        if core_name in MUTATION_TOOLS and result.ok and result.data.get("changed") is True:
            ledger.clear_after_change()
        return False

    def _delegated_source_greenfield_advisory(
        self,
        record: SessionRecord,
        core_name: str,
        arguments: Dict[str, Any],
        result: CoreResult,
    ) -> str:
        """Avoid inspect/list rounds after reading the only greenfield spec.

        The authoritative source was already admitted into the requirement
        ledger.  When it is also the only working file in the repository, a
        second repository-discovery call cannot reveal implementation code: there
        is none.  This advisory is deterministic and does not skip any evidence;
        the model may still inspect if the spec itself names another path.
        """

        if core_name != "repo.read_file" or not result.ok or record.changed_files:
            return ""
        relative = arguments.get("path")
        if not isinstance(relative, str) or not relative:
            return ""
        delegated = {
            str(entry.get("source_path"))
            for entry in record.requirements
            if isinstance(entry, dict)
            and entry.get("status", "active") == "active"
            and isinstance(entry.get("source_path"), str)
            and entry.get("source_path")
        }
        if relative.replace("\\", "/") not in delegated:
            return ""

        ignored_dirs = {".git", ".karox", "__pycache__", ".pytest_cache"}
        working: set[str] = set()
        try:
            for path in self.core.repository.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(self.core.repository)
                if any(part in ignored_dirs for part in rel.parts):
                    continue
                working.add(rel.as_posix())
        except OSError:
            return ""
        if not working or not working.issubset(delegated):
            return ""
        return (
            "This is a greenfield repository: apart from the explicitly delegated "
            "spec file(s), there are no implementation/test files to discover. "
            "Do not spend another semantic round on repo.inspect or repo.list_files "
            "unless the spec names a missing dependency; proceed to create now "
            "with the write tool instead of drafting the whole deliverable in reasoning. If the "
            "deliverable is large, create a complete syntactically valid scaffold "
            "first, including its closing syntax, then add bounded sections with "
            "append or targeted edits."
        )

    def _premature_check_advisory(
        self, core_name: str, result: CoreResult
    ) -> Optional[Dict[str, Any]]:
        """Say when a passing check was run before anything had changed.

        A measured run opened by running the approved check on an empty
        repository: it passed, checked nothing, and the model then made thirty
        edits without checking again -- because a green line it had already seen
        felt like coverage. The check is not wrong to pass; it is wrong for the
        model to bank it, so the result says so where the model reads it.
        """

        if core_name != "checks.run" or not result.ok:
            return None
        if self._changed_once:
            return None
        data = result.data if isinstance(result.data, dict) else {}
        return {
            "advisory": (
                "this check passed before any change in this session, so it "
                "proves nothing about the deliverable; run the approved check "
                "again after your change, and only then the git reads"
            ),
            "verification_eligible": bool(data.get("verification_eligible")),
        }

    def _rewrite_advisory(
        self, call: ToolCall, core_name: str, result: CoreResult
    ) -> Optional[Dict[str, Any]]:
        """Say when a write is the third full regeneration of one file.

        A measured run rewrote one 53 KB page six times, each rewrite a complete
        regeneration the model then paid for again on every later turn; Cline
        finished the same task in half the wall clock by editing. The decision is
        made at this call, so the count and the cost of the alternative belong in
        this result -- as a recorded fact, not a scolding.
        """

        if core_name != "repo.write_file" or not result.ok:
            return None
        data = result.data if isinstance(result.data, dict) else {}
        if data.get("changed") is not True:
            return None
        # An append is not a regeneration: it adds to what is there instead of
        # reproducing it, which is the behaviour this advice asks for.
        if data.get("mode") == "append":
            return None
        path = data.get("path")
        if not isinstance(path, str) or not path:
            return None
        written = int(data.get("bytes") or 0)
        previous = self._full_writes_by_path
        count = previous.get(path, 0) + 1
        previous[path] = count

        previous_bytes = self._last_full_write_bytes_by_path.get(path)
        self._last_full_write_bytes_by_path[path] = written
        quick_checks = data.get("quick_checks")
        incomplete_html = bool(
            pathlib.Path(path).suffix.lower() in {".html", ".htm"}
            and isinstance(quick_checks, list)
            and any(
                isinstance(check, dict)
                and check.get("name") == "web_check"
                and check.get("ok") is False
                and any(
                    marker in str(check.get("summary", "")).lower()
                    for marker in (
                        "closing </html>",
                        "complete html document",
                        "cut short",
                    )
                )
                for check in quick_checks
            )
        )
        shrank_incomplete = bool(
            incomplete_html
            and isinstance(previous_bytes, int)
            and previous_bytes > 0
            and written < previous_bytes * 0.9
        )
        if shrank_incomplete:
            return {
                "full_writes_of_this_path": count,
                "previous_full_write_bytes": previous_bytes,
                "bytes_regenerated_now": written,
                "bytes_lost_vs_previous_write": previous_bytes - written,
                "note": (
                    "this full rewrite made an already incomplete HTML file "
                    "substantially smaller while the quick check still says it is "
                    "cut short. Do not replace it with another smaller partial "
                    "document. Preserve the current work and complete the missing "
                    "tail with mode=append, or repair a specific region with "
                    "repo_edit_file; keep a complete closing </script></body></html> "
                    "in the scaffold."
                ),
            }
        if count < 3:
            return None
        return {
            "full_writes_of_this_path": count,
            "bytes_regenerated_now": written,
            "note": (
                "this is full regeneration %d of %s; repo_edit_file changes an "
                "exact string and costs a fraction of the same result, and every "
                "rewrite also stays in the conversation and is paid for again on "
                "every later turn"
            )
            % (count, path),
        }

    #: Read-only calls that may be batched: independent by construction, and
    #: they change nothing, so overlapping them cannot alter what the run did.
    _INSPECTION_BATCH_WORKERS = 4

    def _canonical_history_tool_name(self, name: str) -> str:
        """Resolve a provider alias without depending on the current tool universe."""
        return (
            self._tool_aliases.get(name)
            or HISTORICAL_TOOL_ALIASES.get(name)
            or name
        )

    def _all_inspection_calls(self, calls: Sequence[ToolCall]) -> bool:
        """True when a non-empty model turn asks only for read-only inspection."""
        if not calls:
            return False
        for call in calls:
            core_name = self._canonical_history_tool_name(call.name)
            if core_name not in INSPECTION_TOOLS:
                return False
        return True

    def _all_repair_inspection_calls(self, calls: Sequence[ToolCall]) -> bool:
        """True when a turn contains only repository diagnosis reads/searches."""
        if not calls:
            return False
        for call in calls:
            core_name = self._canonical_history_tool_name(call.name)
            if core_name not in REPAIR_INSPECTION_TOOLS:
                return False
        return True

    def _repair_action_gate_active(self, record: SessionRecord) -> Optional[str]:
        """Return the active act-now boundary, if reads must no longer execute."""
        history = record.provider_history
        gate_index = -1
        gate_kind: Optional[str] = None
        for index in range(len(history) - 1, -1, -1):
            entry = history[index]
            kind = entry.get("kind")
            if entry.get("role") == "user" and kind in REPAIR_ACTION_GATE_KINDS:
                gate_index = index
                gate_kind = str(kind)
                break
        if gate_index < 0:
            return None
        for entry in history[gate_index + 1 :]:
            if self._changed_the_repository(entry):
                return None
            if entry.get("role") == "tool" and entry.get("core_name") == "checks.run":
                result = entry.get("result")
                if isinstance(result, dict) and result.get("ok") is True:
                    return None
        return gate_kind

    def _successful_repair_inspection_calls(
        self, record: SessionRecord
    ) -> list[tuple[str, Dict[str, Any]]]:
        """Successful diagnosis calls since the last repository mutation."""
        history = record.provider_history
        progress_index = -1
        for index, entry in enumerate(history):
            if self._changed_the_repository(entry):
                progress_index = index
        successful_ids = {
            str(entry.get("tool_call_id") or "")
            for entry in history[progress_index + 1 :]
            if entry.get("role") == "tool"
            and entry.get("core_name") in REPAIR_INSPECTION_TOOLS
            and isinstance(entry.get("result"), dict)
            and entry["result"].get("ok") is True
        }
        calls: list[tuple[str, Dict[str, Any]]] = []
        for entry in history[progress_index + 1 :]:
            if entry.get("role") != "assistant":
                continue
            for raw in entry.get("tool_calls", []) or []:
                if not isinstance(raw, dict):
                    continue
                call_id = str(raw.get("call_id") or "")
                if call_id not in successful_ids:
                    continue
                core_name = self._canonical_history_tool_name(str(raw.get("name") or ""))
                if core_name not in REPAIR_INSPECTION_TOOLS:
                    continue
                try:
                    arguments = _strict_json_loads(str(raw.get("raw_arguments") or ""))
                except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
                    continue
                if isinstance(arguments, dict):
                    calls.append((core_name, arguments))
        return calls

    def _repair_inspection_refusal(
        self, record: SessionRecord, call: ToolCall
    ) -> Optional[str]:
        """Refuse repair reads that are gated or already answered on current bytes."""
        core_name = self._canonical_history_tool_name(call.name)
        if core_name not in REPAIR_INSPECTION_TOOLS:
            return None
        gate = self._repair_action_gate_active(record)
        if gate is not None:
            return (
                f"{gate} requires action now; authoritative repair context is already "
                "attached. Make a targeted mutation or rerun the approved check instead "
                "of another repository inspection."
            )
        history = record.provider_history
        if self._failed_check_prefetch(history):
            return (
                "the active failed-check request already includes authoritative current "
                "file bytes; act on that snapshot instead of inspecting the same bytes"
            )
        if self._visual_repair_prefetch(history):
            return (
                "the active visual-repair request already includes authoritative current "
                "file bytes; act on that snapshot instead of inspecting the same bytes"
            )
        # Argument-aware suppression is repair-specific. Outside an unresolved
        # failed-check/visual episode a repeated range may answer a new question,
        # so ordinary exploration remains governed by the existing action ledger.
        if (
            self._visual_repair_inspection_state(record) is None
            and self._failed_check_inspection_state(record) is None
        ):
            return None
        try:
            arguments = _strict_json_loads(call.raw_arguments)
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
            return None
        if not isinstance(arguments, dict):
            return None
        prior = self._successful_repair_inspection_calls(record)
        normalized = json.dumps(arguments, ensure_ascii=False, sort_keys=True)
        for previous_name, previous_arguments in prior:
            if previous_name == core_name and json.dumps(
                previous_arguments, ensure_ascii=False, sort_keys=True
            ) == normalized:
                return (
                    "the identical inspection already succeeded after the last repository "
                    "change; reuse that result instead of reading unchanged bytes again"
                )
        path = arguments.get("path")
        if not isinstance(path, str) or not path:
            return None
        prior_on_path = [
            (previous_name, previous_arguments)
            for previous_name, previous_arguments in prior
            if previous_arguments.get("path") == path
        ]
        if len(prior_on_path) >= 3:
            return (
                "this repair has already inspected the same path three times on "
                "unchanged bytes; act on the accumulated evidence instead"
            )
        if core_name == "repo.search":
            return None
        if core_name == "repo.read_file":
            if any(
                previous_name == "repo.read_file"
                and previous_arguments.get("path") == path
                for previous_name, previous_arguments in prior
            ):
                return (
                    "this whole file was already read successfully after the last "
                    "repository change; its bytes are unchanged"
                )
            return None
        if core_name != "repo.read_lines":
            return None
        raw_start = arguments.get("start", 1)
        raw_count = arguments.get("count", 1500)
        if isinstance(raw_start, bool) or not isinstance(raw_start, (int, float)):
            return None
        if isinstance(raw_count, bool) or not isinstance(raw_count, (int, float)):
            return None
        start = max(1, int(raw_start))
        end = start + max(1, int(raw_count)) - 1
        for previous_name, previous_arguments in prior:
            if previous_arguments.get("path") != path:
                continue
            if previous_name == "repo.read_file":
                return (
                    "the requested range is already contained in a successful whole-file "
                    "read on unchanged repository bytes"
                )
            if previous_name != "repo.read_lines":
                continue
            previous_start = previous_arguments.get("start", 1)
            previous_count = previous_arguments.get("count", 1500)
            if (
                isinstance(previous_start, bool)
                or not isinstance(previous_start, (int, float))
                or isinstance(previous_count, bool)
                or not isinstance(previous_count, (int, float))
            ):
                continue
            prior_start = max(1, int(previous_start))
            prior_end = prior_start + max(1, int(previous_count)) - 1
            if prior_start <= start and end <= prior_end:
                return (
                    "the requested line range is fully contained in a successful earlier "
                    "read on unchanged repository bytes"
                )
            overlap = max(0, min(end, prior_end) - max(start, prior_start) + 1)
            current_size = max(1, end - start + 1)
            prior_size = max(1, prior_end - prior_start + 1)
            if overlap * 2 >= min(current_size, prior_size):
                return (
                    "the requested line range substantially overlaps a successful earlier "
                    "read on unchanged repository bytes; use the existing evidence"
                )
        return None

    def _is_inspection_batch(self, calls: Sequence[ToolCall]) -> bool:
        """True when every call in a multi-read response may run concurrently."""
        return len(calls) >= 2 and self._all_inspection_calls(calls)

    def _visual_repair_inspection_state(
        self, record: SessionRecord
    ) -> Optional[tuple[int, int, str]]:
        """Inspection turns spent after the latest actionable visual verdict.

        The state starts at the most recent visual_repair prompt, is cleared by
        a successful mutation, and counts only semantic turns that did nothing
        but repository diagnosis. This gives visual repair its own tighter
        mistake/inspection budget instead of borrowing failed-check policy.
        """

        history = record.provider_history
        start: Optional[int] = None
        summary = ""
        for index in range(len(history) - 1, -1, -1):
            entry = history[index]
            if entry.get("role") == "user" and entry.get("kind") == "visual_repair":
                start = index
                summary = self._clip(str(entry.get("content") or ""), 1800)
                break
        if start is None:
            return None

        rounds = 0
        nudges = 0
        for entry in history[start + 1 :]:
            if entry.get("role") == "tool" and entry.get("core_name") in MUTATION_TOOLS:
                result = entry.get("result")
                if isinstance(result, dict) and result.get("ok") is True:
                    data = result.get("data")
                    if not isinstance(data, dict) or data.get("changed") is not False:
                        return None
            if entry.get("role") == "user" and entry.get("kind") == "visual_repair_action":
                nudges += 1
                continue
            if entry.get("role") != "assistant" or not entry.get("provider"):
                continue
            raw_calls = entry.get("tool_calls") or []
            if not raw_calls:
                continue
            names = [
                self._canonical_history_tool_name(str(call.get("name", "")))
                for call in raw_calls
                if isinstance(call, dict)
            ]
            if names and all(name in REPAIR_INSPECTION_TOOLS for name in names):
                rounds += 1
        return rounds, nudges, summary

    def _failed_check_inspection_state(
        self, record: SessionRecord
    ) -> Optional[tuple[int, int, str]]:
        """Return inspection rounds, prior nudges and the current failed-check fact.

        Only the latest verification attempt matters. A successful check clears
        the state, and a mutation after a failure counts as action already taken.
        """

        history = record.provider_history
        failed_index: Optional[int] = None
        summary = ""
        for index in range(len(history) - 1, -1, -1):
            entry = history[index]
            if entry.get("role") != "tool" or entry.get("core_name") != "checks.run":
                continue
            result = entry.get("result")
            if not isinstance(result, dict):
                return None
            if result.get("ok") is True:
                return None
            failed_index = index
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            stdout = str(data.get("stdout") or "")
            try:
                parsed = json.loads(stdout)
            except (TypeError, ValueError, json.JSONDecodeError):
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("problems"), list):
                problems = [
                    str(problem).strip()
                    for problem in parsed["problems"][:4]
                    if str(problem).strip()
                ]
                summary = "; ".join(problems)
            if not summary:
                summary = str(
                    data.get("summary")
                    or stdout
                    or result.get("message")
                    or "verification failed"
                ).strip()
            summary = self._clip(summary, 1800)
            break
        if failed_index is None:
            return None

        rounds = 0
        nudges = 0
        for entry in history[failed_index + 1 :]:
            if entry.get("role") == "tool" and entry.get("core_name") in MUTATION_TOOLS:
                result = entry.get("result")
                if isinstance(result, dict) and result.get("ok") is True:
                    return None
            if entry.get("role") == "user" and entry.get("kind") == "failed_check_action":
                nudges += 1
                continue
            if entry.get("role") != "assistant" or not entry.get("provider"):
                continue
            raw_calls = entry.get("tool_calls") or []
            if not raw_calls:
                continue
            names = [
                self._canonical_history_tool_name(str(call.get("name", "")))
                for call in raw_calls
                if isinstance(call, dict)
            ]
            if names and all(name in REPAIR_INSPECTION_TOOLS for name in names):
                rounds += 1
        return rounds, nudges, summary

    def _inspection_batch_results(
        self, session_id: str, calls: Sequence[ToolCall], deadline: float
    ) -> Dict[str, object]:
        """Run a batch of reads concurrently; persist nothing here.

        Only the Core call is overlapped: every session write, every event and
        every ledger update stays on the sequential path, applied in the order
        the model asked for. A call this pass cannot prepare is left alone, so
        the sequential pass reports it exactly as it would have without this
        path -- and executes nothing twice.
        """

        prepared: list[tuple[str, CoreCommand]] = []
        for call in calls:
            core_name = self._tool_aliases.get(call.name)
            if core_name is None or core_name not in INSPECTION_TOOLS:
                continue
            try:
                arguments = _strict_json_loads(call.raw_arguments)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(arguments, dict):
                continue
            remaining = deadline - self.monotonic()
            if remaining < 0.1:
                continue
            identity = f"{session_id}\0{call.call_id}\0{core_name}".encode("utf-8")
            digest = hashlib.sha256(identity).hexdigest()
            prepared.append(
                (
                    call.call_id,
                    CoreCommand(
                        name=core_name,
                        arguments=arguments,
                        session_id=session_id,
                        origin=self.origin,
                        correlation_id=f"agent-{digest[:32]}",
                        idempotency_key=None,
                        deadline_seconds=min(3600.0, max(0.1, remaining)),
                    ),
                )
            )
        if len(prepared) < 2:
            return {}
        results: Dict[str, object] = {}
        with ThreadPoolExecutor(
            max_workers=min(self._INSPECTION_BATCH_WORKERS, len(prepared)),
            thread_name_prefix="karox-read",
        ) as pool:
            futures = {
                pool.submit(self.core.execute, command): call_id
                for call_id, command in prepared
            }
            for future, call_id in futures.items():
                try:
                    results[call_id] = future.result()
                except Exception as exc:
                    results[call_id] = exc

        return results

    def _attach_quick_checks(self, result: CoreResult) -> None:
        """Run the harness's cheap checks on the file a successful change wrote.

        The verdict travels inside the same tool result the model is already
        reading, because the point is to catch the mistake in the turn that
        caused it -- a missing brace should cost no extra model turn. It is not
        evidence: a syntax check is not the user's acceptance criteria, and
        calling it proof of completion is the false success the evidence chain
        exists to refuse.
        """

        if not result.ok:
            return
        data = result.data if isinstance(result.data, dict) else None
        if data is None or data.get("changed") is not True:
            return
        paths: list[str] = []
        path = data.get("path")
        if isinstance(path, str) and path:
            paths.append(path)
        else:
            changed_files = data.get("changed_files")
            if isinstance(changed_files, list):
                paths.extend(
                    candidate
                    for candidate in changed_files
                    if isinstance(candidate, str) and candidate
                )
        if not paths:
            return
        try:
            checks = quick_checks(self.core.repository, paths)
        except Exception as exc:
            # A broken checker must never break the run it is helping.
            self._emit(
                AgentEventKind.WARNING,
                reason="quick_checks_failed",
                detail=type(exc).__name__,
            )
            return
        if not checks:
            return
        data["quick_checks"] = [check.to_dict() for check in checks]
        data["quick_checks_line"] = summarize(checks)

    def _persist_tool_result(
        self,
        session_id: str,
        lease: MutationLease,
        call: ToolCall,
        core_name: str,
        result: CoreResult,
        *,
        requested_by: str = "model",
    ) -> None:
        harness_call = requested_by == HARNESS_REQUESTED_BY
        if harness_call:
            # The author is recorded on the evidence itself, not only on the
            # result: this is what a reviewer reads when asking whether a run
            # earned its completion or was handed it.
            for item in result.evidence:
                item.metadata["requested_by"] = HARNESS_REQUESTED_BY
        result_value = result.to_dict()
        if harness_call:
            result_value["requested_by"] = HARNESS_REQUESTED_BY
        if (
            core_name in MUTATION_TOOLS
            and result.ok
            and (result.data if isinstance(result.data, dict) else {}).get("changed")
            is True
        ):
            self._changed_once = True
        advisory = self._rewrite_advisory(call, core_name, result)
        if advisory is not None:
            # Recorded on the result itself, so the model reads it in the same
            # breath as the fact that the write succeeded, and an auditor sees
            # the same line the model saw.
            result_value["rewrite_advisory"] = advisory
        premature = self._premature_check_advisory(core_name, result)
        if premature is not None:
            result_value["check_advisory"] = premature
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
        if self._repeat_read_suppression:
            self._suppress_repeated_read(core_name, result_value)
        if core_name == "skill.load" and result.ok:
            loaded = result_value.get("data")
            name = loaded.get("name") if isinstance(loaded, dict) else None
            if isinstance(name, str) and name:
                self._loaded_skills.setdefault(name, self._step)
        content = json.dumps(
            result_value,
            ensure_ascii=False,
            sort_keys=True,
        )
        # The quick checks are the one part of a change's result that is a
        # sentence rather than a document, so they close it in plain words. The
        # same line is already inside ``data``; repeating it here costs a few
        # dozen characters and spares the model a JSON lookup.
        summary_line = (
            result_value.get("data", {}).get("quick_checks_line")
            if isinstance(result_value.get("data"), dict)
            else None
        )
        if isinstance(summary_line, str) and summary_line:
            content = f"{content}\n{summary_line}"
        content = self._condensed_check_content(core_name, result_value, content)
        # A tool result is the model's working material for the rest of the run.
        # Pattern redaction here rewrote file content that the model then had to
        # reproduce exactly, so an edit anchor taken from a read could not match
        # the file on disk. Key-name redaction and known credential values are
        # still removed; Core has already applied the same rule at its boundary.
        entry = {
            "role": "tool",
            # A finishing check the harness made on its own names the harness as
            # its author, so a reviewer reading the transcript can tell which
            # calls the model chose to make.
            "origin": HARNESS_ORIGIN if harness_call else self.origin.key,
            "content": redact_content(content),
            "tool_call_id": call.call_id,
            "tool_name": call.name,
            "core_name": core_name,
            "result": redact_content(result_value),
        }

        def update(record: SessionRecord) -> None:
            record.provider_history.append(entry)
            # Core may have persisted mutating checks before the kernel stamped inputs.
            stamped = {item.evidence_id: item.to_dict() for item in result.evidence}
            record.evidence = [stamped.get(item.get("evidence_id"), item) for item in record.evidence]
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
        """True when this tool entry is a mutation that changed repository bytes."""
        core_name = entry.get("core_name")
        result = entry.get("result")
        if not isinstance(result, dict) or result.get("ok") is not True:
            return False
        data = result.get("data")
        if not isinstance(data, dict):
            return False
        if core_name in MUTATION_TOOLS:
            return data.get("changed") is True
        if core_name == "repo.command" and result.get("mutation") is True:
            changed_files = data.get("changed_files")
            return (
                data.get("dry_run") is not True
                and isinstance(changed_files, list)
                and bool(changed_files)
            )
        return False

    @staticmethod
    def _action_signature(call: ToolCall) -> str:
        try:
            value = _strict_json_loads(call.raw_arguments)
            if call.name in MUTATION_ALIASES and isinstance(value, dict):
                value = dict(value)
                value.pop("finish_attempt", None)
                value.pop("more_semantic_changes", None)
            normalized = json.dumps(
                value, allow_nan=False, ensure_ascii=False, sort_keys=True
            )
        except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
            # ``raw_arguments`` is model output: a deeply nested document raises
            # RecursionError from json.loads/json.dumps, which is not a ValueError
            # and would otherwise escape the run. The raw spelling is the safe
            # signature, the same fallback the context compiler already uses.
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

    @staticmethod
    def _completed_mutation_context_replacements(
        history: Sequence[Dict[str, Any]],
    ) -> Dict[str, str]:
        """Shorten old successful large writes only in request context.

        The durable transcript keeps the exact tool arguments for recovery,
        evidence and audit.  The first semantic turn after a mutation also sees
        them verbatim.  Only after that turn has happened can a later request
        replace the payload with a stable marker pointing at the current file.
        """

        calls: Dict[str, tuple[str, str]] = {}
        semantic_turns: List[int] = []
        for index, entry in enumerate(history):
            if entry.get("role") != "assistant":
                continue
            if entry.get("provider"):
                semantic_turns.append(index)
            for raw in entry.get("tool_calls", []):
                call_id = str(raw.get("call_id", ""))
                arguments = str(raw.get("raw_arguments", ""))
                if (
                    call_id
                    and len(arguments) >= COMPLETED_MUTATION_CONTEXT_THRESHOLD_CHARS
                ):
                    calls[call_id] = (str(raw.get("name", "")), arguments)

        replacements: Dict[str, str] = {}
        for index, entry in enumerate(history):
            if entry.get("role") != "tool":
                continue
            call_id = str(entry.get("tool_call_id", ""))
            known = calls.get(call_id)
            if known is None:
                continue
            core_name = str(entry.get("core_name", ""))
            if core_name not in MUTATION_TOOLS:
                continue
            result = entry.get("result")
            data = result.get("data") if isinstance(result, dict) else None
            if (
                not isinstance(result, dict)
                or result.get("ok") is not True
                or not isinstance(data, dict)
                or data.get("changed") is not True
            ):
                continue
            # Preserve one full-payload follow-up turn for local continuity.
            if not any(turn > index for turn in semantic_turns):
                continue
            tool_name, arguments = known
            path = data.get("path")
            if not isinstance(path, str) or not path:
                try:
                    parsed = _strict_json_loads(arguments)
                except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
                    parsed = None
                if isinstance(parsed, dict) and isinstance(parsed.get("path"), str):
                    path = parsed["path"]
            if not isinstance(path, str) or not path:
                continue
            marker = {
                "karox": "completed_mutation_payload_elided",
                "tool": tool_name,
                "path": path,
                "note": (
                    "this mutation completed successfully and one later semantic "
                    "turn already saw its full arguments; the current file is the "
                    "source of truth, so read only the needed range if more detail "
                    "is required"
                ),
            }
            sha = data.get("sha256")
            if isinstance(sha, str) and sha:
                marker["sha256"] = sha
            replacements[call_id] = json.dumps(
                marker, ensure_ascii=False, sort_keys=True
            )
        return replacements

    def _compact_old_inspection_results(
        self,
        messages: Sequence[ModelMessage],
        *,
        keep_results: int = INSPECTION_CONTEXT_KEEP_RESULTS,
    ) -> List[ModelMessage]:
        """Keep only a small exact window of reproducible inspection output.

        The durable transcript is untouched. Request context keeps the latest few
        read/search/inspect results verbatim and uses sticky provenance markers
        for older large results. New markers are adopted only in batches large
        enough to justify one provider-prefix cache rebuild.
        """

        self._economy_inspection_chars_pending = 0
        if (
            not self.lossless_context_rewrite
            or float(self.limits.max_seconds) < ADAPTIVE_OUTPUT_MIN_BUDGET_SECONDS
        ):
            return list(messages)

        call_info: Dict[str, tuple[str, str]] = {}
        for message in messages:
            if message.role != "assistant":
                continue
            for call in message.tool_calls:
                core_name = self._canonical_history_tool_name(call.name)
                call_info[call.call_id] = (core_name, call.raw_arguments)

        # Never compact a result from the latest assistant turn: that batch
        # has not yet had a semantic consumer. This matters when the model asks
        # for >KEEP_RESULTS independent reads in one call.
        latest_assistant_call_ids: set[str] = set()
        for message in reversed(messages):
            if message.role != "assistant":
                continue
            latest_assistant_call_ids = {call.call_id for call in message.tool_calls}
            break

        eligible: List[tuple[int, ModelMessage, str, str]] = []
        for index, message in enumerate(messages):
            if (
                message.role != "tool"
                or not message.tool_call_id
                or message.tool_call_id in latest_assistant_call_ids
                or not isinstance(message.content, str)
                or len(message.content) < INSPECTION_CONTEXT_MIN_RESULT_CHARS
            ):
                continue
            known = call_info.get(message.tool_call_id)
            if known is None or known[0] not in REPAIR_INSPECTION_TOOLS:
                continue
            eligible.append((index, message, known[0], known[1]))

        keep_results = max(1, int(keep_results))
        candidates = eligible[:-keep_results] if len(eligible) > keep_results else []
        pending: Dict[str, str] = {}
        pending_savings = 0
        for _index, message, core_name, raw_arguments in candidates:
            call_id = str(message.tool_call_id or "")
            if not call_id or call_id in self._inspection_rewrite_adopted:
                continue
            try:
                decoded = _strict_json_loads(raw_arguments)
            except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
                decoded = None
            locator: Dict[str, Any] = {}
            if isinstance(decoded, dict):
                for key in ("path", "query", "pattern", "start", "count", "max_results"):
                    value = decoded.get(key)
                    if isinstance(value, (str, int, float, bool)) and value != "":
                        locator[key] = value
            marker = json.dumps(
                {
                    "karox": "older_inspection_result_elided",
                    "tool": core_name,
                    "locator": locator,
                    "result_sha256": hashlib.sha256(
                        message.content.encode("utf-8", errors="replace")
                    ).hexdigest(),
                    "note": (
                        "this older inspection result is reproducible from the "
                        "current repository; reread only the needed range if it "
                        "becomes relevant again"
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            saving = len(message.content) - len(marker)
            if saving <= 0:
                continue
            pending[call_id] = marker
            pending_savings += saving

        if (
            pending
            and pending_savings >= INSPECTION_CONTEXT_REWRITE_BATCH_MIN_CHARS
        ):
            self._inspection_rewrite_adopted.update(pending)
            self._inspection_rewrite_batches += 1

        rewritten: List[ModelMessage] = []
        for message in messages:
            replacement = (
                self._inspection_rewrite_adopted.get(message.tool_call_id or "")
                if message.role == "tool"
                else None
            )
            if (
                replacement is not None
                and isinstance(message.content, str)
                and len(replacement) < len(message.content)
            ):
                self._economy_inspection_chars_pending += (
                    len(message.content) - len(replacement)
                )
                rewritten.append(
                    ModelMessage(
                        role="tool",
                        content=replacement,
                        tool_call_id=message.tool_call_id,
                    )
                )
            else:
                rewritten.append(message)
        return rewritten

    def _compact_superseded_repair_inspections(
        self,
        messages: Sequence[ModelMessage],
        history: Sequence[Dict[str, Any]],
    ) -> List[ModelMessage]:
        """Elide diagnostic outputs made stale by a later repository mutation."""
        if not self.lossless_context_rewrite:
            return list(messages)
        last_mutation = -1
        for index, entry in enumerate(history):
            if self._changed_the_repository(entry):
                last_mutation = index
        if last_mutation < 0:
            return list(messages)
        stale: Dict[str, str] = {}
        for entry in history[:last_mutation]:
            if entry.get("role") != "tool" or entry.get("core_name") not in REPAIR_INSPECTION_TOOLS:
                continue
            result = entry.get("result")
            if not isinstance(result, dict) or result.get("ok") is not True:
                continue
            call_id = str(entry.get("tool_call_id") or "")
            if not call_id:
                continue
            stale[call_id] = json.dumps(
                {
                    "karox": "superseded_repair_inspection",
                    "tool": entry.get("core_name"),
                    "note": (
                        "repository bytes changed after this diagnostic result; the "
                        "current repository and newer repair snapshot supersede it"
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        if not stale:
            return list(messages)
        rewritten: List[ModelMessage] = []
        for message in messages:
            replacement = (
                stale.get(message.tool_call_id or "")
                if message.role == "tool"
                else None
            )
            if (
                replacement is not None
                and isinstance(message.content, str)
                and len(replacement) < len(message.content)
            ):
                self._economy_inspection_chars_pending += (
                    len(message.content) - len(replacement)
                )
                rewritten.append(
                    ModelMessage(
                        role="tool",
                        content=replacement,
                        tool_call_id=message.tool_call_id,
                    )
                )
            else:
                rewritten.append(message)
        return rewritten

    def _authoritative_repair_prefetch(
        self,
        relative: str,
        expected_digest: str,
        *,
        flag: str,
        call_prefix: str,
    ) -> tuple[ModelMessage, ...]:
        """Attach exact current UTF-8 bytes as request-only repair context."""
        try:
            target = self.core.safe_path(relative)
            raw = target.read_bytes()
        except (OSError, ValueError):
            return ()
        if len(raw) > VISUAL_REPAIR_SNAPSHOT_MAX_CHARS or b"\x00" in raw:
            return ()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected_digest:
            return ()
        call_id = f"{call_prefix}-{digest[:16]}"
        result = {
            "ok": True,
            "command": "repo.read_file",
            "data": {
                "path": relative,
                "content": content,
                "bytes": len(raw),
                "content_sha256": digest,
                "sha256": digest,
                "truncated": False,
                "repair_prefetch": True,
                flag: True,
            },
        }
        return (
            ModelMessage(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        call_id=call_id,
                        name=provider_alias("repo.read_file"),
                        raw_arguments=json.dumps(
                            {"path": relative}, ensure_ascii=False, sort_keys=True
                        ),
                    ),
                ),
            ),
            ModelMessage(
                role="tool",
                tool_call_id=call_id,
                content=json.dumps(result, ensure_ascii=False, sort_keys=True),
            ),
        )

    @staticmethod
    def _identity_changed_paths(
        history: Sequence[Dict[str, Any]], files: Dict[str, Any]
    ) -> set[str]:
        """Paths successfully changed in history that belong to one identity."""
        changed: set[str] = set()
        for entry in history:
            if entry.get("role") != "tool":
                continue
            result = entry.get("result")
            if not isinstance(result, dict) or result.get("ok") is not True:
                continue
            data = result.get("data")
            if not isinstance(data, dict):
                continue
            path = data.get("path")
            if (
                entry.get("core_name") in MUTATION_TOOLS
                and data.get("changed") is True
                and isinstance(path, str)
                and path in files
            ):
                changed.add(path)
            for candidate in data.get("changed_files", []) or []:
                if isinstance(candidate, str) and candidate in files:
                    changed.add(candidate)
        return changed

    def _failed_check_prefetch(
        self, history: Sequence[Dict[str, Any]]
    ) -> tuple[ModelMessage, ...]:
        """Keep exact current bytes attached while one failed check is unresolved."""
        failed_index = -1
        failed: Dict[str, Any] = {}
        for index in range(len(history) - 1, -1, -1):
            entry = history[index]
            if entry.get("role") != "tool" or entry.get("core_name") != "checks.run":
                continue
            result = entry.get("result")
            if not isinstance(result, dict):
                return ()
            if result.get("ok") is True:
                return ()
            failed_index = index
            failed = result
            break
        if failed_index < 0:
            return ()
        # A successful real mutation after this failure invalidates the attached
        # bytes. Failed/no-op mutations and inspections do not: those are exactly
        # the turns for which retaining authoritative context saves rereads.
        for entry in history[failed_index + 1 :]:
            if self._changed_the_repository(entry):
                return ()
        data = failed.get("data") if isinstance(failed.get("data"), dict) else {}
        identity = data.get("completion_identity")
        files = identity.get("files") if isinstance(identity, dict) else None
        if not isinstance(files, dict) or not files:
            return ()
        relative = data.get("completion_target")
        if not isinstance(relative, str) or relative not in files:
            changed_paths = self._identity_changed_paths(history[: failed_index + 1], files)
            if len(changed_paths) != 1:
                return ()
            relative = next(iter(changed_paths))
        expected_digest = files.get(relative)
        if not isinstance(expected_digest, str):
            return ()
        return self._authoritative_repair_prefetch(
            relative,
            expected_digest,
            flag="failed_check_prefetch",
            call_prefix="karox-failed-check-prefetch",
        )

    def _visual_repair_prefetch(
        self, history: Sequence[Dict[str, Any]]
    ) -> tuple[ModelMessage, ...]:
        """Expose current bounded bytes once at a visual-repair boundary."""

        repair_index = -1
        repair: Dict[str, Any] = {}
        for index, entry in enumerate(history):
            if entry.get("role") == "user" and entry.get("kind") == "visual_repair":
                repair_index = index
                repair = entry
        if repair_index < 0:
            return ()
        # Keep authoritative current bytes attached throughout this repair
        # identity until repository bytes actually change. R17 showed why an
        # assistant/tool turn is not a useful consumption boundary: a clipped
        # repair recovered into one malformed mutation call, then immediately
        # fell back to read/search turns after the snapshot disappeared. Reads,
        # prose and failed/malformed mutations do not make the snapshot stale;
        # the on-disk digest check below still fails closed on any external edit.
        for entry in history[repair_index + 1 :]:
            if entry.get("role") != "tool":
                continue
            result = entry.get("result")
            if not isinstance(result, dict) or result.get("ok") is not True:
                continue
            data = result.get("data")
            core_name = entry.get("core_name")
            if (
                core_name in MUTATION_TOOLS
                and isinstance(data, dict)
                and data.get("changed") is True
            ):
                return ()
            if (
                core_name == "repo.command"
                and result.get("mutation") is True
                and isinstance(data, dict)
                and data.get("dry_run") is not True
            ):
                return ()
        identity = repair.get("completion_identity")
        files = identity.get("files") if isinstance(identity, dict) else None
        if not isinstance(files, dict) or not files:
            return ()
        relative = repair.get("repair_target")
        if not isinstance(relative, str) or relative not in files:
            changed_paths: set[str] = set()
            for entry in history:
                if entry.get("role") != "tool" or entry.get("core_name") not in MUTATION_TOOLS:
                    continue
                result = entry.get("result")
                data = result.get("data") if isinstance(result, dict) else None
                path = data.get("path") if isinstance(data, dict) else None
                if (
                    isinstance(result, dict)
                    and result.get("ok") is True
                    and isinstance(data, dict)
                    and data.get("changed") is True
                    and isinstance(path, str)
                    and path in files
                ):
                    changed_paths.add(path)
            if len(changed_paths) != 1:
                return ()
            relative = next(iter(changed_paths))
        expected_digest = files.get(relative)
        if not isinstance(expected_digest, str):
            return ()
        try:
            target = self.core.safe_path(relative)
            raw = target.read_bytes()
        except (OSError, ValueError):
            return ()
        if len(raw) > VISUAL_REPAIR_SNAPSHOT_MAX_CHARS or b"\x00" in raw:
            return ()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return ()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != expected_digest:
            return ()
        call_id = f"karox-repair-prefetch-{digest[:16]}"
        result = {
            "ok": True,
            "command": "repo.read_file",
            "data": {
                "path": relative,
                "content": content,
                "bytes": len(raw),
                "content_sha256": digest,
                "sha256": digest,
                "truncated": False,
                "repair_prefetch": True,
            },
        }
        return (
            ModelMessage(
                role="assistant",
                tool_calls=(
                    ToolCall(
                        call_id=call_id,
                        name=provider_alias("repo.read_file"),
                        raw_arguments=json.dumps(
                            {"path": relative}, ensure_ascii=False, sort_keys=True
                        ),
                    ),
                ),
            ),
            ModelMessage(
                role="tool",
                tool_call_id=call_id,
                content=json.dumps(result, ensure_ascii=False, sort_keys=True),
            ),
        )

    def _request_messages(
        self,
        history: Iterable[Dict[str, Any]],
        *,
        plan: "Optional[_CompactionPlan] | _Unplanned" = _UNPLANNED,
    ) -> Iterable[ModelMessage]:
        replaced = False
        raw_history = list(history)
        messages: List[ModelMessage] = []
        for message in self._messages(self._compact(raw_history, plan=plan)):
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

        self._economy_completed_mutation_chars_pending = 0
        if (
            self.lossless_context_rewrite
            and float(self.limits.max_seconds) >= ADAPTIVE_OUTPUT_MIN_BUDGET_SECONDS
        ):
            completed_mutations = self._completed_mutation_context_replacements(
                raw_history
            )
            if completed_mutations:
                compacted_messages: List[ModelMessage] = []
                for message in messages:
                    if message.role != "assistant" or not message.tool_calls:
                        compacted_messages.append(message)
                        continue
                    for call in message.tool_calls:
                        replacement = completed_mutations.get(call.call_id)
                        if replacement is not None and len(replacement) < len(
                            call.raw_arguments
                        ):
                            self._economy_completed_mutation_chars_pending += (
                                len(call.raw_arguments) - len(replacement)
                            )
                    compacted_messages.append(
                        self._with_superseded_calls(
                            message, completed_mutations
                        )
                    )
                messages = [
                    message
                    for message in compacted_messages
                    if not (
                        message.role == "tool"
                        and message.tool_call_id in completed_mutations
                    )
                ]

        failed_check_prefetch = self._failed_check_prefetch(raw_history)
        visual_repair_prefetch = self._visual_repair_prefetch(raw_history)
        repair_prefetch_active = bool(failed_check_prefetch or visual_repair_prefetch)
        messages = self._compact_old_inspection_results(
            messages,
            keep_results=2 if repair_prefetch_active else INSPECTION_CONTEXT_KEEP_RESULTS,
        )
        messages = self._compact_superseded_repair_inspections(
            messages, raw_history
        )

        # Request-only current-state handoff for repair turns. Keep each as a
        # valid assistant tool-call + tool-result pair so provider wire formats
        # see normal tool protocol while durable history remains unchanged.
        messages.extend(failed_check_prefetch)
        messages.extend(visual_repair_prefetch)

        # A frame stays attached while its grade is outstanding, not only in the
        # request that asks for the grade. A measured run answered the grade
        # several steps after being asked -- it kept working first -- then
        # retracted its own "VISUAL: OK" on the grounds that it had not been shown
        # the frame that turn and had graded from memory. It was right, and the
        # fix belongs here rather than in the prompt: the picture travels with
        # every request until the question is answered.
        frame = self._pending_render_image
        page_name = self._pending_render_page
        if frame is None and self._awaiting_visual_review:
            frame = self._render_review_frame
            page_name = self._render_review_page
        if self.request_images or frame is not None:
            for index in range(len(messages) - 1, -1, -1):
                message = messages[index]
                if message.role != "user":
                    continue
                images = tuple(message.images) + tuple(self.request_images)
                content = message.content
                if frame is not None:
                    # The frame the check just measured, with a line that says
                    # what it is. Request-only: the transcript keeps the page as
                    # the model wrote it, not as a picture of it.
                    images = images + (
                        ImageAttachment(mime="image/png", data=frame),
                    )
                    content = (
                        f"This is the current render of {page_name} at "
                        "1280x800, exactly as the approved check measured it. Compare "
                        "it with what the task asks for before deciding the work is "
                        "done.\n\n"
                    ) + (content or "")
                messages[index] = ModelMessage(
                    role="user",
                    content=content,
                    images=images,
                )
                break
        self._pending_render_image = None
        # Typed context IR: every retained item receives an explicit
        # INCLUDE / REFERENCE / ELIDE verdict with a recorded reason. Exact
        # duplicate and superseded tool results are losslessly referenced on
        # every normal run; this does not change model, effort, verification or
        # evidence and therefore does not need the more aggressive economy mode.
        # Replaced old mutation payloads are ordinary inert history notes; they
        # intentionally leave the tool-call ABI so the model cannot invoke them.
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
        call_payloads = self._call_payloads(messages)
        compiled_calls = self._context_compiler.compile_call_payloads(
            call_payloads,
            replacement_targets=FULL_REPLACEMENT_ALIASES,
            incremental_markers=INCREMENTAL_ALIASES,
        )
        self._call_payload_compilation = compiled_calls
        self._economy_superseded_call_chars_pending = 0
        if not self.lossless_context_rewrite:
            return messages
        result_replacements, superseded_calls = self._adopt_rewrites(
            messages,
            compiled,
            compiled_calls,
            compacting=not isinstance(plan, _Unplanned) and plan is not None,
        )
        # Applied, not merely detected: these counters are what actually left
        # the request, while the compilation's own stats above record what the
        # rules found even when a replacement is still waiting for its batch.
        self._economy_superseded_call_chars_pending = sum(
            decision.chars_saved
            for decision in compiled_calls.decisions
            if decision.call_id and decision.call_id in superseded_calls
        )
        rewritten: List[ModelMessage] = []
        for message, decision in zip(messages, compiled.decisions):
            if (
                message.role == "tool"
                and decision.replacement is not None
                and decision.verdict is not Verdict.INCLUDE
                and message.tool_call_id in result_replacements
            ):
                if decision.verdict is Verdict.ELIDE_STALE:
                    self._economy_stale_chars_pending += decision.chars_saved
                else:
                    self._economy_reused_chars_pending += decision.chars_saved
                rewritten.append(
                    ModelMessage(
                        role="tool",
                        content=result_replacements[message.tool_call_id],
                        tool_call_id=message.tool_call_id,
                    )
                )
            elif message.role == "assistant" and superseded_calls:
                rewritten.append(self._with_superseded_calls(message, superseded_calls))
            else:
                rewritten.append(message)
        if superseded_calls:
            rewritten = [
                message
                for message in rewritten
                if not (
                    message.role == "tool"
                    and message.tool_call_id in superseded_calls
                )
            ]
        return rewritten

    def _adopt_rewrites(
        self,
        messages: Sequence[ModelMessage],
        compiled: CompiledContext,
        compiled_calls: CompiledCallPayloads,
        *,
        compacting: bool,
    ) -> tuple[Dict[str, str], Dict[str, str]]:
        """Decide which replacements this request carries, keeping the prefix stable.

        Returns ``(tool_call_id -> result replacement, call_id -> argument
        replacement)``. Every replacement adopted by an earlier request is
        carried again with the exact same bytes. New candidates join only as a
        batch (see ``REWRITE_BATCH_MIN_CHARS``), so one cache rebuild pays for
        many replacements instead of one rebuild per replacement.
        """

        candidates: Dict[str, tuple[str, int]] = {}
        for message, decision in zip(messages, compiled.decisions):
            if (
                message.role == "tool"
                and message.tool_call_id
                and decision.replacement is not None
                and decision.verdict is not Verdict.INCLUDE
            ):
                candidates["r:" + message.tool_call_id] = (
                    decision.replacement,
                    decision.chars_saved,
                )
        for decision in compiled_calls.decisions:
            if decision.replacement is not None and decision.call_id:
                candidates["c:" + decision.call_id] = (
                    decision.replacement,
                    decision.chars_saved,
                )
        pending: Dict[str, tuple[str, int]] = {}
        for message, decision in zip(messages, compiled.decisions):
            key = "r:" + str(message.tool_call_id or "")
            if key not in candidates or key in self._rewrite_adopted:
                continue
            if decision.verdict is Verdict.REFERENCE:
                # "Same bytes as call X" is only true while X still shows its
                # bytes. A sticky replacement may already stand in for X, so a
                # reference to it would point at a marker, not at the content.
                target = decision.reason.partition(":")[2]
                if "r:" + target in self._rewrite_adopted or "r:" + target in pending:
                    continue
            pending[key] = candidates[key]
        for key, value in candidates.items():
            if key.startswith("c:") and key not in self._rewrite_adopted:
                # A superseded full-content write is the model's own draft:
                # usually tens of thousands of characters, so dropping it at
                # once always outweighs the one cache rebuild it costs.
                self._rewrite_adopted[key] = value[0]
        pending_chars = sum(saved for _, saved in pending.values())
        self._rewrite_pending_chars = pending_chars
        adopt = bool(pending) and (
            not self._rewrite_primed
            or compacting
            or pending_chars >= REWRITE_BATCH_MIN_CHARS
        )
        if adopt:
            for key, (replacement, _) in pending.items():
                self._rewrite_adopted[key] = replacement
            self._rewrite_pending_chars = 0
            if self._rewrite_primed:
                self._rewrite_batches += 1
            # Correctness over cache: a reference whose original was just
            # replaced would point at a marker. Drop it so the full result
            # travels again (a later batch may reference the surviving copy).
            for key, replacement in list(self._rewrite_adopted.items()):
                if not key.startswith("r:"):
                    continue
                try:
                    target = json.loads(replacement).get("same_as_tool_call_id")
                except (ValueError, AttributeError):
                    continue
                if target and "r:" + str(target) in self._rewrite_adopted:
                    del self._rewrite_adopted[key]
        self._rewrite_primed = True
        results: Dict[str, str] = {}
        calls: Dict[str, str] = {}
        for key, replacement in self._rewrite_adopted.items():
            # An adopted replacement only applies while its item is still in
            # the request; compaction may have dropped it, which is harmless.
            if key.startswith("r:"):
                results[key[2:]] = replacement
            else:
                calls[key[2:]] = replacement
        return results, calls

    @staticmethod
    def _call_payloads(messages: Sequence[ModelMessage]) -> List[CallPayload]:
        """Every tool call in the request, in the order the model emitted it."""

        payloads: List[CallPayload] = []
        for message in messages:
            for call in message.tool_calls:
                payloads.append(
                    CallPayload(
                        index=len(payloads),
                        call_id=call.call_id,
                        tool_name=call.name,
                        raw_arguments=call.raw_arguments,
                    )
                )
        return payloads

    @staticmethod
    def _with_superseded_calls(
        message: ModelMessage, replacements: Dict[str, str]
    ) -> ModelMessage:
        """Turn replaced historical calls into inert prose, never fake tools.

        R11 showed that a marker under the real mutation tool name poisons the
        argument schema. R13 showed that merely renaming it to a synthetic tool
        is still unsafe: the model may try to call that synthetic name. Removed
        calls therefore become compact assistant history notes; their paired tool
        results are removed by the request builder. Durable history is untouched.
        """

        kept: List[ToolCall] = []
        notes: List[str] = []
        for call in message.tool_calls:
            replacement = replacements.get(call.call_id)
            if replacement is None:
                kept.append(call)
                continue
            try:
                payload = json.loads(replacement)
            except (TypeError, ValueError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            kind = str(payload.get("karox") or "compacted_tool_payload")
            path = str(payload.get("path") or payload.get("target") or "").strip()
            sha = str(payload.get("sha256") or "").strip()
            details = [kind]
            if path:
                details.append(f"path={path}")
            if sha:
                details.append(f"sha256={sha}")
            notes.append(
                "[KaroX history note: "
                + "; ".join(details)
                + ". The earlier tool action already completed; its large payload "
                "was omitted from request context. This note is not a tool call.]"
            )
        content_parts = [str(message.content)] if message.content else []
        content_parts.extend(notes)
        return ModelMessage(
            role=message.role,
            content="\n".join(content_parts) or None,
            tool_calls=tuple(kept),
            tool_call_id=message.tool_call_id,
            reasoning_blocks=message.reasoning_blocks,
            images=message.images,
        )

    # -- context compaction ------------------------------------------------

    def _visual_repair_epoch_plan(
        self, history: List[Dict[str, Any]]
    ) -> Optional["_CompactionPlan"]:
        """Start one visual repair from a compact, current-state-oriented view.

        This is request-only. The durable transcript remains complete for audit
        and recovery, while the model stops replaying the entire construction
        history after a visual checker has already reduced the next objective to
        a bounded defect list.
        """

        if self._estimated_tokens(history) < VISUAL_REPAIR_EPOCH_MIN_TOKENS:
            return None
        repair_index = -1
        for index, entry in enumerate(history):
            if entry.get("role") == "user" and entry.get("kind") == "visual_repair":
                repair_index = index
        if repair_index < 0:
            return None
        # Only the first provider turn after the repair instruction gets the
        # fresh epoch. Once the model has answered, its new reasoning/tool work is
        # part of the current epoch and must not be repeatedly re-compacted.
        if any(
            entry.get("role") == "assistant" and entry.get("provider")
            for entry in history[repair_index + 1 :]
        ):
            return None

        head, groups = self._split_history(history)
        if len(groups) <= VISUAL_REPAIR_EPOCH_KEEP_GROUPS:
            return None
        keep = min(VISUAL_REPAIR_EPOCH_KEEP_GROUPS, len(groups))
        dropped = groups[:-keep]
        kept_groups = groups[-keep:]
        kept = [entry for group in kept_groups for entry in group]
        before = self._estimated_tokens(history)
        return _CompactionPlan(
            head=head,
            dropped=dropped,
            kept=kept,
            cut=len(dropped),
            before=before,
            fitted=True,
            ceiling=self.context.token_ceiling,
            kept_groups=keep,
        )

    def _compaction_plan(
        self, history: List[Dict[str, Any]]
    ) -> Optional["_CompactionPlan"]:
        """Decide what compaction would drop, without applying it.

        Planning is separate from applying because the model-written brief has to
        be produced *before* the request is assembled, by a call that needs a
        lease, a deadline and a session -- none of which belong in the request
        builder. One plan serves both halves, so the turns a brief describes are
        exactly the turns the request drops.
        """

        head, groups = self._split_history(history)
        if not groups:
            return None
        ceiling = self.context.token_ceiling
        if self._estimated_tokens(history) <= ceiling:
            return None
        head_chars = self._entry_chars(head)
        # Suffix totals instead of a walk per candidate: a cut is a prefix of the
        # groups, the estimate is additive in characters, and recomputing it for
        # every candidate made this plan O(groups x entries) on every step of a
        # session that is over its ceiling -- exactly the steady state it exists
        # to leave. The walk above already proved this one is needed.
        prefix_chars = [0]
        for group in groups:
            prefix_chars.append(prefix_chars[-1] + self._entry_chars(group))
        before = self._tokens_from_chars(prefix_chars[-1] + head_chars)

        def fits(cut: int) -> bool:
            kept_chars = prefix_chars[-1] - prefix_chars[cut]
            return self._tokens_from_chars(head_chars + kept_chars) <= ceiling

        # The recent floor is a preference, not a promise. Keeping eight turns
        # verbatim is right until those eight turns are themselves bigger than
        # the window, and then it is the reason a request is refused: shrinking
        # the floor is what lets a run bound itself without losing the task.
        keep = min(self.context.keep_recent_groups, len(groups))
        while keep > 1:
            for cut in range(1, len(groups) - keep + 1):
                if fits(cut):
                    return _CompactionPlan(
                        head=head,
                        dropped=groups[:cut],
                        kept=[entry for group in groups[cut:] for entry in group],
                        cut=cut,
                        before=before,
                        fitted=True,
                        ceiling=ceiling,
                        kept_groups=keep,
                    )
            keep -= 1
        older = groups[:-1] if len(groups) > 1 else []
        if not older:
            return None
        return _CompactionPlan(
            head=head,
            dropped=older,
            kept=list(groups[-1]),
            cut=len(older),
            before=before,
            fitted=False,
            ceiling=ceiling,
            kept_groups=1,
        )

    def _compact(
        self,
        history: List[Dict[str, Any]],
        *,
        plan: "Optional[_CompactionPlan] | _Unplanned" = _UNPLANNED,
    ) -> List[Dict[str, Any]]:
        """Replace the earliest turns with a brief the model can continue from.

        Whole turns are dropped, never half of one: an assistant message whose
        tool results are missing (or a tool result with no matching call) is
        rejected outright by the OpenAI, Anthropic and Gemini wire formats, so
        partial trimming would turn a large request into a broken one.

        The deterministic digest is always present, so a brief that failed, was
        never written, or went stale cannot lose the evidence ids a run rests on.
        ``plan`` is the decision the caller already made for this history, which
        is how a step that had to write a brief does not pay for the plan twice;
        a plan of ``None`` means "no compaction" and is honoured as such.
        """

        resolved = (
            self._compaction_plan(list(history)) if isinstance(plan, _Unplanned) else plan
        )
        if resolved is None:
            return list(history)
        digest = self._context_summary(resolved.dropped)
        compacted = resolved.head + [digest] + resolved.kept
        self._record_compaction(
            resolved.before,
            self._estimated_tokens(compacted),
            resolved.cut,
            fitted=resolved.fitted,
            kept_groups=resolved.kept_groups,
            preferred_groups=self.context.keep_recent_groups,
        )
        return compacted

    def _prepare_compaction(
        self,
        session_id: str,
        lease: MutationLease,
        record: SessionRecord,
        remaining: float,
    ) -> Optional["_CompactionPlan"]:
        """Make sure a usable brief exists for the turns this step will drop.

        Reused whenever the stored brief already covers the dropped range: a
        brief is paid for once, not once per step. When more turns have to go
        than the stored brief describes, the model is asked to fold the new
        material into its own earlier brief, which is what keeps a rolling
        summary from diluting into unrelated fragments.

        Returns the plan it settled on, so the request builder does not have to
        work it out a second time from the same history.
        """

        history = list(record.provider_history)
        repair_epoch = self._visual_repair_epoch_plan(history)
        if repair_epoch is not None:
            # A visual-repair handoff intentionally uses only the deterministic
            # digest plus recent turns. A stored model-written compaction brief
            # may describe an older objective and would re-introduce stale
            # construction narration into the fresh repair epoch.
            self._compaction_state = {}
            return repair_epoch

        plan = self._compaction_plan(history)
        stored = record.context_compaction if isinstance(record.context_compaction, dict) else {}
        if plan is None:
            self._compaction_state = dict(stored)
            return None
        covered = int(stored.get("covered_entries") or 0)
        dropped = len(plan.head) + sum(len(group) for group in plan.dropped)
        if stored.get("summary") and covered + COMPACTION_BRIEF_REFRESH_ENTRIES >= dropped:
            # The brief still describes the dropped range closely enough, and the
            # digest below it is rebuilt for the exact range anyway.
            self._compaction_state = dict(stored)
            return plan
        self._compaction_state = dict(stored)
        self._write_compaction_brief(
            session_id, lease, plan, stored, remaining, record.task
        )
        return plan

    def _write_compaction_brief(
        self,
        session_id: str,
        lease: MutationLease,
        plan: "_CompactionPlan",
        stored: Dict[str, Any],
        remaining: float,
        task: str,
    ) -> None:
        """Ask the model for a continuation brief, and persist whatever comes back.

        A failed briefing is never fatal: the deterministic digest still travels,
        the run keeps its evidence ids, and the record says which of the two the
        model is working from. That asymmetry is deliberate -- compaction must
        not be able to end a run that was making progress.
        """

        material = self._compaction_material(plan, task)
        previous = str(stored.get("summary") or "").strip()
        if previous:
            material = (
                "Your own earlier brief, covering turns before these:\n"
                f"{previous}\n\nNewly dropped turns:\n{material}"
            )
        request = ModelRequest(
            model=self.model,
            messages=(
                ModelMessage(
                    role="user",
                    content=f"{COMPACTION_SUMMARY_PROMPT}\n\n--- material ---\n{material}",
                ),
            ),
            deadline_seconds=max(1.0, min(remaining, 300.0)),
            max_output_tokens=min(
                int(self.max_output_tokens or COMPACTION_SUMMARY_MAX_TOKENS),
                COMPACTION_SUMMARY_MAX_TOKENS,
            ),
        )
        try:
            response = self._call_provider(request)
        except ProviderError as exc:
            self._record_provider_failure(session_id, lease, exc, self._step)
            self._compaction_state = dict(stored)
            self._emit(
                AgentEventKind.WARNING,
                reason="compaction_brief_failed",
                detail=f"deterministic digest used instead ({exc.kind.value})",
            )
            return
        brief = str(response.content or "").strip()
        self._record_side_call(session_id, lease, response, kind="compaction_brief")
        if not brief:
            self._emit(
                AgentEventKind.WARNING,
                reason="compaction_brief_empty",
                detail="deterministic digest used instead",
            )
            return
        dropped_entries = len(plan.head) + sum(len(group) for group in plan.dropped)
        state = {
            "summary": brief[:COMPACTION_MATERIAL_CHARS],
            "source": "model",
            "covered_entries": dropped_entries,
            "covered_sha256": _history_sha256(plan.head + [
                entry for group in plan.dropped for entry in group
            ]),
            "created_at": time.time(),
            "count": int(stored.get("count") or 0) + 1,
            "tokens_before": plan.before,
            "tokens_after": self._estimated_tokens(plan.head + plan.kept),
        }
        self._compaction_state = state

        def update(record: SessionRecord) -> None:
            record.context_compaction = dict(state)

        self._update_session(session_id, lease, update)

    def _suppress_repeated_read(
        self, core_name: str, result_value: Dict[str, Any]
    ) -> None:
        """Answer a repeated, unchanged read with the fact that it is unchanged.

        The prompt asks the model not to read the same lines twice; this is the
        harness holding up its end. A second read of the same window returns the
        same bytes, and those bytes are already in the transcript -- so the
        payload is replaced by one line naming the step that read them and the
        digest that still describes them, and the model can keep working from
        the copy it has.

        Replacements are per window, not per file: a file edited outside the
        window it was read from still answers the same question, and calling that
        a change would make the model re-read what it already knows.
        """

        if core_name not in {"repo.read_file", "repo.read_lines"}:
            return
        if result_value.get("ok") is not True:
            return
        data = result_value.get("data")
        if not isinstance(data, dict):
            return
        path = data.get("path")
        digest = data.get("content_sha256")
        if not isinstance(path, str) or not path or not isinstance(digest, str) or not digest:
            return
        start = data.get("start") if core_name == "repo.read_lines" else None
        count = data.get("count") if core_name == "repo.read_lines" else None
        key = (
            path,
            start if isinstance(start, int) else None,
            count if isinstance(count, int) else None,
        )
        previous = self._read_ranges.get(key)
        self._read_ranges[key] = (digest, self._step)
        if previous is None or previous[0] != digest:
            return
        step = previous[1]
        short = digest[:12]
        replaced = dict(data)
        replaced.pop("content", None)
        replaced.pop("lines", None)
        replaced["unchanged"] = True
        replaced["since_step"] = step
        replaced["note"] = (
            f"unchanged since your last read at step {step} (sha {short}); "
            "your earlier copy still holds"
        )
        # Suppression has to pay for itself. A note is a sentence and a ten-line
        # read of a small file is barely more, so a rule that always replaced the
        # payload would make short re-reads *more* expensive and would be the very
        # thing this exists to prevent.
        if _json_chars(replaced) >= _json_chars(data):
            return
        data.clear()
        data.update(replaced)
        self._repeat_reads += 1

    def _dropped_skill_notes(self, plan: "_CompactionPlan") -> List[str]:
        """Recipes whose text the brief is about to drop, named so the fact survives.

        The recipe text is exactly the kind of payload compaction exists to
        remove, and the fact that it was loaded is exactly the kind it must not
        remove: a model continuing from a brief where a recipe silently vanished
        would either work without following it or spend a step asking whether it
        had been applied. One line says which skill is gone and how to get it
        back.
        """

        notes: List[str] = []
        seen: Set[str] = set()
        for group in plan.dropped:
            for entry in group:
                if entry.get("role") != "tool" or entry.get("core_name") != "skill.load":
                    continue
                result = entry.get("result")
                data = result.get("data") if isinstance(result, dict) else None
                name = data.get("name") if isinstance(data, dict) else None
                if not isinstance(name, str) or not name or name in seen:
                    continue
                seen.add(name)
                step = self._loaded_skills.get(name)
                where = f"at step {step}" if isinstance(step, int) else "earlier"
                notes.append(f"- {name} was loaded {where} and its text is gone")
        return notes

    def _compaction_material(self, plan: "_CompactionPlan", task: str) -> str:
        """Build the bounded material a continuation brief is written from.

        Narration and recorded facts only. The raw tool payloads are what made
        compaction necessary in the first place, so summarising from them would
        recreate the problem inside the summariser's own request.
        """

        lines: List[str] = [f"Task: {task}"]
        digest = self._context_summary(plan.dropped)
        lines.append(str(digest.get("content") or ""))
        lines.append("What you said while working:")
        for group in plan.dropped:
            for entry in group:
                if entry.get("role") != "assistant":
                    continue
                content = entry.get("content")
                if not isinstance(content, str) or not content.strip():
                    continue
                text = content.strip()
                if len(text) > COMPACTION_NARRATION_CHARS:
                    text = text[: COMPACTION_NARRATION_CHARS - 3] + "..."
                lines.append(f"- {text}")
        notes = self._dropped_skill_notes(plan)
        if notes:
            lines.append("Skill recipes whose text is being dropped with these turns:")
            lines.extend(notes)
        material = "\n".join(lines)
        if len(material) > COMPACTION_MATERIAL_CHARS:
            material = (
                material[: COMPACTION_MATERIAL_CHARS - 200]
                + "\n... [material truncated for the summariser] ...\n"
                + material[-200:]
            )
        return material

    def _record_compaction(
        self,
        before: int,
        after: int,
        turns: int,
        *,
        fitted: bool,
        kept_groups: int = 0,
        preferred_groups: int = 0,
    ) -> None:
        """Say that the model's context was rewritten, and by how much.

        Compaction used to be entirely invisible: nothing in the report, the
        stream or the record said the middle of the conversation had been
        replaced, so a run that went wrong afterwards could not be explained.
        ``fitted`` is False when even one recent turn is over the ceiling -- an
        honest 'this is still too big' rather than a silent pass -- and a floor
        that had to shrink is recorded, because a run that compacted harder than
        its preferred policy says so.
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
        if preferred_groups and kept_groups and kept_groups < preferred_groups:
            self._last_compaction["kept_recent_turns"] = kept_groups
            self._last_compaction["preferred_recent_turns"] = preferred_groups
        self._emit(
            AgentEventKind.COMPACTED,
            summary=(
                f"summarized {turns} earlier turn(s): ~{before} tokens to ~{after}"
                + ("" if fitted else ", still over the ceiling")
                + (
                    f" (kept {kept_groups} recent turns, not the preferred "
                    f"{preferred_groups})"
                    if kept_groups and preferred_groups and kept_groups < preferred_groups
                    else ""
                )
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

        return self._tokens_from_chars(self._entry_chars(entries))

    def _tokens_from_chars(self, characters: int) -> int:
        """The one arithmetic that turns counted characters into an estimate."""

        return int(characters / float(self.context.chars_per_token)) + 1

    def _entry_chars(self, entries: Iterable[Dict[str, Any]]) -> int:
        """Count exactly the characters :meth:`_estimated_tokens` accounted for.

        Split out because it is additive: planning a compaction needs the
        character total of every suffix of the transcript, and recomputing the
        whole walk for each candidate cut is what made that plan quadratic. The
        per-entry rules must stay identical to the estimate's, so this is the
        only place they are written down.
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
        return characters

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
        brief = str((self._compaction_state or {}).get("summary") or "").strip()
        if brief:
            # The model's own continuation brief, stated as what it is. It is the
            # part that survives a compaction: the sections below are facts, and
            # this is the plan the model was working to when the turns went.
            lines.append(
                "Your own brief for these turns, written by you when they were "
                "dropped (continue from it; it is your narration, not evidence):"
            )
            lines.append(brief)
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
        if self.context.window_source == "tier":
            lines.append(
                "This run budgets the model context at the tier you selected, "
                f"not at the model's own window ({self.context.token_ceiling} "
                "tokens of ceiling here)."
            )
        elif not self.context.window_known:
            lines.append(
                "The active model does not advertise an input window, so a "
                "conservative default ceiling was applied."
            )
        lines.append(
            "The omitted text is gone from this request. Continue the task from "
            "the brief above -- do not restart it or redo work it records as "
            "finished -- and re-read any file or re-run any search you still need "
            "instead of relying on memory of it."
        )
        return {
            "role": "user",
            "content": "\n".join(lines),
            "kind": "context_summary",
            # Which kind of brief this request carries, so a run that went wrong
            # after a compaction can be explained without guessing.
            "brief_source": str(
                (self._compaction_state or {}).get("source") or "deterministic"
            ),
        }

    @staticmethod
    def _condensed_check_content(
        core_name: Optional[str], result_value: Dict[str, Any], content: str
    ) -> str:
        """The model-facing text of a test run, with progress noise removed.

        Only the transcript text changes; ``result_value`` (and the evidence
        built from its hashes and byte counts) stays exactly what ran.
        """

        if core_name not in _TEST_TOOL_NAMES:
            return content
        data = result_value.get("data")
        if not isinstance(data, dict):
            return content
        changed = False
        slim = dict(data)
        for key in ("stdout", "stderr"):
            condensed = condense_test_output(slim.get(key) or "")
            if condensed is not None:
                slim[key] = condensed["text"]
                slim[f"{key}_condensed_from_chars"] = condensed["original_chars"]
                changed = True
        if not changed:
            return content
        view = dict(result_value)
        view["data"] = slim
        rendered = json.dumps(view, ensure_ascii=False, sort_keys=True)
        tail = content.split("\n", 1)[1] if content.startswith("{") and "\n" in content else ""
        return f"{rendered}\n{tail}" if tail else rendered

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
    def _mutation_mistake_state(
        history: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Describe repeated failed mutation attempts since the last progress.

        Reads do not reset the streak: R14 spent many read turns between failed
        anchors while still pursuing the same bad mutation strategy. A successful
        real edit/write resets it. A successful apply_patch dry-run also resets it
        because the mutation strategy has finally been validated and the next call
        is expected to apply those exact bytes.
        """

        calls: Dict[str, tuple[str, Dict[str, Any]]] = {}
        for entry in history:
            if entry.get("role") != "assistant":
                continue
            for raw in entry.get("tool_calls", []):
                call_id = str(raw.get("call_id", ""))
                if not call_id:
                    continue
                name = str(raw.get("name", ""))
                core_name = HISTORICAL_TOOL_ALIASES.get(name, name)
                arguments: Dict[str, Any] = {}
                try:
                    parsed = _tool_argument_json_loads(str(raw.get("raw_arguments", "")))
                    if isinstance(parsed, dict):
                        arguments = parsed
                except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
                    pass
                calls[call_id] = (core_name, arguments)

        def attempt(
            entry: Dict[str, Any],
        ) -> tuple[bool, str, str, bool]:
            call_id = str(entry.get("tool_call_id", ""))
            known = calls.get(call_id)
            core_name = str(entry.get("core_name") or (known[0] if known else ""))
            arguments = known[1] if known else {}
            target = ""
            mutation_like = core_name in MUTATION_TOOLS
            if mutation_like:
                candidate = arguments.get("path")
                if isinstance(candidate, str) and candidate:
                    target = candidate
                if core_name == "repo.apply_patch" and not target:
                    patch = arguments.get("patch")
                    if isinstance(patch, str):
                        match = re.search(
                            r"^\+\+\+\s+(?:b/)?([^\t\r\n]+)",
                            patch,
                            re.MULTILINE,
                        )
                        if match is not None:
                            candidate = match.group(1).strip()
                            if candidate != "/dev/null":
                                target = candidate
            elif core_name == "repo.command" and arguments.get("action") == "apply_patch":
                mutation_like = True
                payload = arguments.get("payload")
                patch = payload.get("patch") if isinstance(payload, dict) else None
                if isinstance(patch, str):
                    match = re.search(
                        r"^\+\+\+\s+(?:b/)?([^\t\r\n]+)",
                        patch,
                        re.MULTILINE,
                    )
                    if match is not None:
                        candidate = match.group(1).strip()
                        if candidate != "/dev/null":
                            target = candidate
            if not mutation_like:
                return False, core_name, target, False

            result = entry.get("result")
            ok = isinstance(result, dict) and result.get("ok") is True
            reset = False
            if ok and core_name in MUTATION_TOOLS:
                data = result.get("data")
                reset = isinstance(data, dict) and data.get("changed") is True
                if not target and isinstance(data, dict):
                    candidate = data.get("path")
                    if isinstance(candidate, str):
                        target = candidate
            elif ok and core_name == "repo.command":
                # A successful dry-run proves the patch applies; a successful real
                # patch is repository progress. Either means the previous failed
                # mutation strategy has been corrected.
                reset = result.get("mutation") is True

            failed = not ok
            return True, core_name, target, reset or not failed

        reset_index = -1
        for index, entry in enumerate(history):
            if entry.get("role") != "tool":
                continue
            mutation_like, _core_name, _target, reset = attempt(entry)
            if mutation_like and reset:
                reset_index = index

        episode = history[reset_index + 1 :]
        nudges = sum(
            1
            for entry in episode
            if entry.get("role") == "user"
            and entry.get("kind") == "mutation_mistake_recovery"
        )
        failures: Dict[str, List[str]] = {}
        order: List[str] = []
        for entry in episode:
            if entry.get("role") != "tool":
                continue
            mutation_like, core_name, target, reset = attempt(entry)
            if not mutation_like or reset:
                continue
            key = target or f"<{core_name or 'mutation'}>"
            error = entry.get("error")
            result = entry.get("result")
            if not isinstance(error, dict) and isinstance(result, dict):
                maybe_error = result.get("error")
                if isinstance(maybe_error, dict):
                    error = maybe_error
            message = ""
            if isinstance(error, dict):
                message = str(error.get("message") or error.get("type") or "").strip()
            if not message and isinstance(result, dict):
                data = result.get("data")
                if isinstance(data, dict):
                    message = str(data.get("detail") or "").strip()
            if not message:
                message = "mutation returned no successful change"
            if key not in failures:
                failures[key] = []
                order.append(key)
            failures[key].append(f"{core_name or 'mutation'}: {message[:360]}")

        for key in reversed(order):
            items = failures[key]
            if len(items) >= MUTATION_MISTAKE_THRESHOLD:
                return {
                    "count": len(items),
                    "target": key,
                    "failures": items[-MUTATION_MISTAKE_THRESHOLD:],
                    "nudges": nudges,
                }
        return None

    @staticmethod
    def _output_limit_recovery_available(history: List[Dict[str, Any]]) -> bool:
        """Allow one tool-less truncation recovery per real progress epoch.

        Two clipped replies in a row still stop. A successful repository mutation
        after the previous recovery re-arms one bounded recovery because the model
        has demonstrably moved the task forward rather than looping.
        """

        last_prompt = -1
        for index, entry in enumerate(history):
            if entry.get("kind") == "output_limit_prompt":
                last_prompt = index
        if last_prompt < 0:
            return True
        for entry in history[last_prompt + 1 :]:
            if entry.get("role") != "tool" or entry.get("core_name") not in MUTATION_TOOLS:
                continue
            result = entry.get("result")
            if not isinstance(result, dict) or result.get("ok") is not True:
                continue
            data = result.get("data")
            if isinstance(data, dict) and data.get("changed") is True:
                return True
        return False

    def _failed_check_output_recovery_available(
        self, record: SessionRecord
    ) -> bool:
        """Allow one final act-now retry after a successful diagnostic read.

        A failed-check repair may legitimately need one exact read before the
        mutation. That read changes the model's information state even though it
        does not change repository bytes. R13 showed that treating the following
        clipped repair exactly like a second no-progress truncation stops a
        healthy repair path. This allowance is intentionally one-shot per active
        failed-check episode; a mutation or a new check starts a new episode.
        """

        state = self._failed_check_inspection_state(record)
        if state is None or state[0] < 1:
            return False
        history = record.provider_history
        failed_index = -1
        for index in range(len(history) - 1, -1, -1):
            entry = history[index]
            if entry.get("role") != "tool" or entry.get("core_name") != "checks.run":
                continue
            result = entry.get("result")
            if isinstance(result, dict) and result.get("ok") is False:
                failed_index = index
            break
        if failed_index < 0:
            return False
        episode = history[failed_index + 1 :]
        if any(
            entry.get("role") == "user"
            and entry.get("kind") == "failed_check_output_recovery"
            for entry in episode
        ):
            return False
        last_limit_prompt = -1
        for index, entry in enumerate(episode):
            if entry.get("role") == "user" and entry.get("kind") == "output_limit_prompt":
                last_limit_prompt = index
        if last_limit_prompt < 0:
            return False
        for entry in episode[last_limit_prompt + 1 :]:
            if entry.get("role") != "tool":
                continue
            if entry.get("core_name") not in REPAIR_INSPECTION_TOOLS:
                continue
            result = entry.get("result")
            if isinstance(result, dict) and result.get("ok") is True:
                return True
        return False

    @staticmethod
    def _visual_repair_output_recovery_available(record: SessionRecord) -> bool:
        """Allow one act-now retry for a clipped visual-repair identity."""

        history = record.provider_history
        visual_index = -1
        for index in range(len(history) - 1, -1, -1):
            entry = history[index]
            if entry.get("role") != "tool" or entry.get("core_name") != "visual.check":
                continue
            result = entry.get("result")
            data = result.get("data") if isinstance(result, dict) else None
            if not (
                isinstance(result, dict)
                and result.get("ok") is True
                and isinstance(data, dict)
                and data.get("verdict") == "needs_work"
            ):
                return False
            visual_index = index
            break
        if visual_index < 0:
            return False
        episode = history[visual_index + 1 :]
        if not any(
            entry.get("role") == "user" and entry.get("kind") == "visual_repair"
            for entry in episode
        ):
            return False
        if any(
            entry.get("role") == "user"
            and entry.get("kind") == "visual_repair_output_recovery"
            for entry in episode
        ):
            return False
        for entry in episode:
            if entry.get("role") != "tool":
                continue
            result = entry.get("result")
            if not isinstance(result, dict) or result.get("ok") is not True:
                continue
            data = result.get("data")
            if (
                entry.get("core_name") in MUTATION_TOOLS
                and isinstance(data, dict)
                and data.get("changed") is True
            ):
                return False
            if (
                entry.get("core_name") == "repo.command"
                and result.get("mutation") is True
                and isinstance(data, dict)
                and data.get("dry_run") is not True
            ):
                return False
        return True

    @staticmethod
    def _step_count(history: List[Dict[str, Any]]) -> int:
        return sum(
            1
            for entry in history
            if entry.get("role") == "assistant" and entry.get("provider")
        )

    def _last_passing_check(
        self, record: SessionRecord
    ) -> Optional[Dict[str, Any]]:
        """The last point at which an approved check passed on the current bytes.

        This is deliberately *not* the full reporting chain. The chain also wants
        the two Git reads, and a measured high-effort run passed its check at step
        4 without ever completing that tail -- so keying the artifact guarantee to
        the chain would have left the one run that needed protection unprotected.
        What makes a state provable is the check that ran after the last change and
        passed; the Git reads remain what makes the *report* verified.
        """

        write: Optional[Dict[str, Any]] = None
        steps = 0
        last: Optional[Dict[str, Any]] = None
        for entry in record.provider_history:
            if entry.get("role") == "assistant" and entry.get("provider"):
                steps += 1
            if entry.get("role") != "tool" or not isinstance(entry.get("result"), dict):
                continue
            result = entry["result"]
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            core_name = entry.get("core_name")
            ok = result.get("ok") is True
            if core_name in MUTATION_TOOLS:
                if ok and data.get("changed") is True:
                    write = result
                elif not ok:
                    write = None
                continue
            if core_name != "checks.run" or write is None:
                continue
            if not (ok and data.get("verification_eligible") is True):
                last = None
                continue
            summary = (result.get("data") or {}).get("summary")
            last = {"step": steps, "check": str(summary or "")[:200]}
        return last

    def _current_passing_check(
        self, record: SessionRecord
    ) -> Optional[Dict[str, Any]]:
        """The last approved PASS only if no mutation has happened since it.

        Historical PASS state is intentionally kept by _last_passing_check so
        regression recovery can name a restore point. Visual feedback and
        snapshots need the stricter question: did a check pass on the bytes that
        exist now?
        """

        write: Optional[Dict[str, Any]] = None
        steps = 0
        last: Optional[Dict[str, Any]] = None
        for entry in record.provider_history:
            if entry.get("role") == "assistant" and entry.get("provider"):
                steps += 1
            if entry.get("role") != "tool" or not isinstance(entry.get("result"), dict):
                continue
            result = entry["result"]
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            core_name = entry.get("core_name")
            ok = result.get("ok") is True
            if core_name in MUTATION_TOOLS:
                if ok and data.get("changed") is True:
                    write = result
                    last = None
                elif not ok:
                    write = None
                    last = None
                continue
            if core_name != "checks.run" or write is None:
                continue
            if not (ok and data.get("verification_eligible") is True):
                last = None
                continue
            summary = data.get("summary")
            last = {"step": steps, "check": str(summary or "")[:200]}
        return last

    def _cached_check_result(
        self,
        record: SessionRecord,
        arguments: Dict[str, Any],
        *,
        correlation_id: str,
    ) -> Optional[CoreResult]:
        """Reuse an exact PASS for the same bytes and argv without rerunning it."""

        argv = arguments.get("argv")
        if not isinstance(argv, list) or not argv or not all(
            isinstance(item, str) for item in argv
        ):
            return None
        # A legacy visual task uses repeated render checks as fresh frame
        # sampling for its in-transcript grader. Those renders are observably
        # time-dependent even when file bytes are identical, so replaying a cached
        # PASS would suppress the new frame. Modern isolated visual.check owns
        # sampling itself and ordinary test/lint commands remain cacheable.
        joined_argv = " ".join(argv).lower()
        if (
            completion_gate.visual_required(record)
            and "visual.check" not in self._definitions
            and ("web_check" in joined_argv or "--render" in argv)
        ):
            return None
        current_identity = completion_gate.identity(self.core, record)
        persisted_ids = {
            item.get("evidence_id")
            for item in record.evidence
            if isinstance(item, dict) and item.get("evidence_id")
        }
        for entry in reversed(record.provider_history):
            if entry.get("role") != "tool" or entry.get("core_name") != "checks.run":
                continue
            raw = entry.get("result")
            if not isinstance(raw, dict) or raw.get("ok") is not True:
                continue
            data = raw.get("data")
            if not isinstance(data, dict):
                continue
            if data.get("verification_eligible") is not True:
                continue
            if data.get("completion_identity") != current_identity:
                continue
            if data.get("argv") != argv:
                continue
            raw_evidence = raw.get("evidence")
            if not isinstance(raw_evidence, list) or not raw_evidence:
                continue
            try:
                evidence = [
                    EvidenceRecord.from_dict(item)
                    for item in raw_evidence
                    if isinstance(item, dict)
                ]
            except (TypeError, ValueError):
                continue
            if not evidence or any(
                item.evidence_id not in persisted_ids for item in evidence
            ):
                continue
            cached_data = {
                key: data[key]
                for key in (
                    "argv",
                    "verification_eligible",
                    "summary",
                    "exit_code",
                    "completion_identity",
                    "completion_target",
                )
                if key in data
            }
            cached_data["proof_cache_hit"] = True
            if completion_gate.browser_proof(data):
                cached_data["cached_browser_proof"] = True
            cached_data["next_action_advisory"] = (
                "These exact bytes already passed this exact approved check. "
                "Do not run it again unless a repository mutation changes the "
                "completion identity; finish or make the next semantic change."
            )
            return CoreResult(
                ok=True,
                command="checks.run",
                data=cached_data,
                correlation_id=correlation_id,
                mutation=False,
                evidence=evidence,
                idempotent_replay=True,
            )
        return None

    # -- the harness's own finishing checks ---------------------------------

    @staticmethod
    def _finishing_state(record: SessionRecord) -> tuple[bool, str]:
        """Where the evidence chain stands: is there a change, and does a check describe it?

        Returns ``(changed, check)`` where ``check`` is ``"none"`` when no
        approved check ran after the latest change, ``"pass"`` when the last one
        passed and ``"fail"`` when it ran and failed. It mirrors ``_verification``
        step for step, so the harness cannot act on a state the acceptance rule
        would not recognize.
        """

        changed = False
        check = "none"
        for entry in record.provider_history:
            if entry.get("role") != "tool" or not isinstance(entry.get("result"), dict):
                continue
            result = entry["result"]
            if result.get("requested_by") not in (None, HARNESS_REQUESTED_BY):
                continue
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            core_name = entry.get("core_name")
            ok = result.get("ok") is True
            if core_name in MUTATION_TOOLS:
                if ok and data.get("changed") is True:
                    changed, check = True, "none"
                elif not ok:
                    changed, check = False, "none"
            elif core_name == "checks.run" and changed:
                if ok and data.get("verification_eligible") is True:
                    check = "pass"
                elif data.get("verification_eligible") is True:
                    # An approved check ran on these bytes and failed. Re-running
                    # it unchanged would spend a whole test suite to learn what
                    # the record already says.
                    check = "fail"
                else:
                    # Not an approved check at all -- a refused argv, or one Core
                    # never got to run. Nothing the user agreed to has run, so the
                    # state is still "no check" rather than a failure.
                    check = "none"
        return changed, check

    def _harness_final_calls(
        self, record: SessionRecord
    ) -> tuple[tuple[str, Dict[str, Any]], ...]:
        """The finishing calls the harness owes this run, in order.

        The model no longer has to ask for the check and the two Git reads, so
        the harness asks for them itself the moment the model stops to answer.
        It asks only for what is actually missing: re-running a check that
        already describes the current bytes costs a whole test suite and proves
        nothing new, and running a check the model was told to fix first would
        spend the turn without changing anything the repair needs.
        """

        if self._verification(record):
            return ()
        changed, check = self._finishing_state(record)
        if not changed:
            return ()
        git_reads: tuple[tuple[str, Dict[str, Any]], ...] = (
            ("git.status", {}),
            ("git.diff", {}),
        )
        if completion_gate.docs_only(record):
            missing = completion_gate.floor(self.core, record)
            return () if any(reason.startswith("active failed check:") for reason in missing) else git_reads
        missing = completion_gate.floor(self.core, record)
        if any(reason.startswith("active failed check:") for reason in missing):
            return ()
        current_visual = self._current_visual_check_data(record)
        if current_visual is not None and current_visual.get("verdict") == "needs_work":
            # The current bytes were already judged and need code changes. Do
            # not spend another vision call or Git proof before those changes.
            return ()
        # A verdict is immutable for one completion identity. In particular,
        # never ask a stochastic vision model to judge unchanged bytes twice:
        # R10 produced PASS and NEEDS_WORK for the exact same SHA and spent the
        # final visual slot doing it.
        current_identity = completion_gate.identity(self.core, record)
        current_visual_is_exact = bool(
            current_visual is not None
            and current_visual.get("completion_identity") == current_identity
        )
        current_visual_usable = bool(
            current_visual_is_exact
            and current_visual.get("verdict") in {"pass", "needs_work"}
        )
        current_visual_retry_exhausted = bool(
            current_visual_is_exact
            and current_visual.get("verdict") == "unavailable"
            and self._visual_attempts_for_identity(record, current_identity) >= 2
        )
        # Visual acceptance is deterministic harness work once a page exists:
        # the expensive image is judged in visual.check's isolated context and
        # only its compact verdict enters the main transcript. Do not spend a
        # semantic model round merely asking the model to remember to call it.
        visual_calls: tuple[tuple[str, Dict[str, Any]], ...] = ()
        if (
            "missing visual evidence" in missing
            and "visual.check" in self._definitions
            and not current_visual_usable
            and not current_visual_retry_exhausted
        ):
            target = self._render_target(record)
            if target is not None:
                visual_calls = (("visual.check", {"path": target.name}),)
        required_calls = []
        for command in self.core.verification_command_order:
            joined = " ".join(command)
            # A check that failed on older bytes is owed a rerun, never a substitute.
            if f"missing current check: {joined}" in missing or f"unresolved failed check: {joined}" in missing:
                required_calls.append(("checks.run", {"argv": list(command)}))
        # The model's own focused checks (approved through a prefix rule) owe the
        # same rerun. Without it a run that fixed the bug after an early red
        # `pytest -k ...` ended UNVERIFIED because nobody ran that exact argv
        # again (swer5-k2 funcadl). Rerunning the identical argv on the current
        # bytes is the only thing that may discharge it; bounded to keep the
        # finishing step cheap.
        seen = {tuple(argv) for _name, arguments in required_calls for argv in [arguments["argv"]]}
        for argv in self._unresolved_model_checks(record, missing):
            if len(required_calls) >= 4:
                break
            if tuple(argv) not in seen:
                seen.add(tuple(argv))
                required_calls.append(("checks.run", {"argv": list(argv)}))
        if required_calls:
            return (*required_calls, *visual_calls, *git_reads)
        if check in {"pass", "fail"}:
            # The check already ran on these bytes. A failing one is the model's
            # turn to act on, not the harness's to repeat. A passing one can move
            # straight into the isolated visual gate without another model turn.
            return (*visual_calls, *git_reads) if check == "pass" else ()
        # The first check the user approved, in the order they gave it.
        commands = [
            list(item) for item in self.core.verification_command_order
        ] or [list(item) for item in sorted(self.core.verification_commands)]
        if not commands:
            return ()
        # An entry ending in "*" is a prefix rule; the literal prefix without the
        # marker matches it exactly, which is the only argv the harness can pick
        # without inventing arguments the user never approved.
        argv = [item for item in commands[0] if item != "*"]
        if not argv:
            return ()
        return (("checks.run", {"argv": argv}), *visual_calls, *git_reads)

    @staticmethod
    def _unresolved_model_checks(
        record: SessionRecord, missing: Sequence[str]
    ) -> list[list[str]]:
        """Exact argv of approved checks the floor still owes a rerun, oldest first."""

        owed = {
            reason[len("unresolved failed check: "):]
            for reason in missing
            if reason.startswith("unresolved failed check: ")
        }
        found: list[list[str]] = []
        for entry in record.provider_history:
            if entry.get("role") != "tool" or entry.get("core_name") != "checks.run":
                continue
            result = entry.get("result")
            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, dict) or data.get("verification_eligible") is not True:
                continue
            argv = data.get("argv")
            if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
                continue
            if " ".join(argv) in owed and argv not in found:
                found.append(list(argv))
        return found

    @staticmethod
    def _current_visual_check_data(record: SessionRecord) -> Optional[Dict[str, Any]]:
        """Latest isolated visual verdict on the bytes that exist now.

        A successful mutation invalidates every earlier visual verdict. This
        ordering rule is intentionally independent of completion_identity so a
        useful needs_work verdict is never lost merely because proof metadata was
        unavailable while the checker was running.
        """

        current: Optional[Dict[str, Any]] = None
        for entry in record.provider_history:
            if entry.get("role") != "tool":
                continue
            result = entry.get("result")
            if not isinstance(result, dict):
                continue
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            core_name = entry.get("core_name")
            if (
                core_name in MUTATION_TOOLS
                and result.get("ok") is True
                and data.get("changed") is True
            ):
                current = None
                continue
            if core_name == "visual.check" and result.get("ok") is True:
                current = dict(data)
        return current

    @staticmethod
    def _visual_attempts_for_identity(
        record: SessionRecord, identity: Dict[str, Any]
    ) -> int:
        return sum(
            1
            for entry in record.provider_history
            if entry.get("role") == "tool"
            and entry.get("core_name") == "visual.check"
            and isinstance(entry.get("result"), dict)
            and entry["result"].get("ok") is True
            and isinstance(entry["result"].get("data"), dict)
            and entry["result"]["data"].get("completion_identity") == identity
        )

    @staticmethod
    def _visual_failure_summary(data: Dict[str, Any]) -> str:
        pieces: List[str] = []
        issues = data.get("issues")
        if isinstance(issues, list):
            pieces.extend(str(item).strip() for item in issues if str(item).strip())
        for channel in ("page_errors", "console_errors"):
            values = data.get(channel)
            if isinstance(values, list):
                pieces.extend(str(item).strip() for item in values if str(item).strip())
        if pieces:
            return _bounded_failure_summary("Visual review: " + "; ".join(pieces[:5]))
        verdict = str(data.get("verdict") or "").strip()
        if verdict and verdict != "pass":
            return _bounded_failure_summary("Visual review: " + verdict)
        return ""

    def _append_visual_repair_prompt(
        self,
        session_id: str,
        lease: MutationLease,
        record: SessionRecord,
    ) -> bool:
        """Turn a current needs_work verdict into one bounded coding turn."""

        data = self._current_visual_check_data(record)
        # "unavailable" means the checker failed to produce a verdict, not that
        # the page has a code defect. Sending it to the coding model creates a
        # fake repair round that cannot fix the checker. Only actionable
        # needs_work findings become semantic repair prompts.
        if not data or data.get("verdict") != "needs_work":
            return False
        checks_left = data.get("checks_left")
        if isinstance(checks_left, int) and checks_left <= 0:
            return False
        identity = data.get("completion_identity")
        if not isinstance(identity, dict) or not identity:
            identity = completion_gate.identity(self.core, record)
        # Visual repair has its own bounded producer budget. A global
        # max_repair_prompts=2 made only the first two distinct NEEDS_WORK
        # revisions receive a repair handoff even when the visual checker still
        # had quality slots left (R13). Dedupe by the exact graded state instead:
        # one actionable prompt per completion identity, and a mutation naturally
        # re-arms the next visual repair epoch.
        if any(
            entry.get("role") == "user"
            and entry.get("kind") == "visual_repair"
            and entry.get("completion_identity") == identity
            for entry in record.provider_history
        ):
            return False
        summary = self._visual_failure_summary(data)
        if not summary:
            return False
        self._append_message(
            session_id,
            lease,
            {
                "role": "user",
                "content": VISUAL_REPAIR_PROMPT.format(failure_summary=summary),
                "kind": "visual_repair",
                "completion_identity": identity,
                "repair_target": data.get("completion_target"),
            },
            phase="verification",
        )
        self._emit(
            AgentEventKind.WARNING,
            reason="visual_repair_required",
            detail=summary,
        )
        return True

    def _run_early_visual_check(
        self,
        session_id: str,
        lease: MutationLease,
        deadline: float,
        ledger: _ActionLedger,
        record: SessionRecord,
    ) -> bool:
        """Grade a freshly passing visual page without ending the run.

        This is deliberately narrower than final proof: no Git reads and no
        terminal decision.  A model can run a green check mid-task, so finishing
        at that point would trade correctness for speed.  The isolated checker is
        still worth running immediately because its compact defects can guide the
        very next semantic turn.
        """

        if "visual.check" not in self._definitions:
            return False
        current_visual = self._current_visual_check_data(record)
        current_identity = completion_gate.identity(self.core, record)
        if current_visual is not None:
            verdict = current_visual.get("verdict")
            exact = current_visual.get("completion_identity") == current_identity
            if verdict == "needs_work":
                return self._append_visual_repair_prompt(session_id, lease, record)
            # A usable verdict on unchanged bytes is cached. An unavailable
            # checker result may retry once because no visual decision exists;
            # after two attempts the task fails closed instead of burning the
            # entire visual budget on one SHA.
            if exact and verdict == "pass":
                return False
            if (
                exact
                and verdict == "unavailable"
                and self._visual_attempts_for_identity(record, current_identity) >= 2
            ):
                return False
        missing = completion_gate.floor(self.core, record)
        if "missing visual evidence" not in missing:
            return False
        if any(
            reason.startswith("active failed check: visual.check")
            for reason in missing
        ):
            return False
        target = self._render_target(record)
        if target is None or self.monotonic() >= deadline:
            return False
        history = self.sessions.load(session_id).provider_history
        call = ToolCall(
            call_id=f"harness-visual-{len(history)}",
            name=provider_alias("visual.check"),
            raw_arguments=json.dumps(
                {"path": target.name}, ensure_ascii=False, sort_keys=True
            ),
        )
        self._execute_call(
            session_id,
            lease,
            call,
            deadline,
            ledger,
            requested_by=HARNESS_REQUESTED_BY,
        )
        updated = self.sessions.load(session_id)
        prompted = self._append_visual_repair_prompt(
            session_id, lease, updated
        )
        self.sessions.heartbeat(
            lease,
            ttl_seconds=min(
                3600.0, max(5.0, float(self.limits.max_seconds) + 5.0)
            ),
            session_revision=self._known_revision,
        )
        return prompted

    def _run_harness_final_checks(
        self,
        session_id: str,
        lease: MutationLease,
        deadline: float,
        ledger: _ActionLedger,
        record: SessionRecord,
    ) -> None:
        """Make the finishing calls the model stopped asking for.

        They go through the same ``_execute_call`` path as a model's request, so
        the ledger, the persisted records, the evidence and the audit line are
        identical -- only the author differs, and it is named on every one. The
        call ids are derived from how much history exists, so a second round of
        checks after a repair is a new call rather than a replay of the first.
        """

        calls = self._harness_final_calls(record)
        if not calls:
            return
        self._emit(
            AgentEventKind.WARNING,
            reason="harness_final_checks",
            detail=", ".join(name for name, _arguments in calls),
        )
        for index, (core_name, arguments) in enumerate(calls, start=1):
            if self.monotonic() >= deadline:
                return
            history = self.sessions.load(session_id).provider_history
            call = ToolCall(
                call_id=f"harness-{len(history)}-{index}",
                name=provider_alias(core_name),
                raw_arguments=json.dumps(
                    arguments, ensure_ascii=False, sort_keys=True
                ),
            )
            self._execute_call(
                session_id,
                lease,
                call,
                deadline,
                ledger,
                requested_by=HARNESS_REQUESTED_BY,
            )
            self.sessions.heartbeat(
                lease,
                ttl_seconds=min(3600.0, max(5.0, float(self.limits.max_seconds) + 5.0)),
                session_revision=self._known_revision,
            )
            if core_name in {"checks.run", "visual.check"}:
                # Do not keep spending deterministic proof work after a producer
                # has already found a defect. In particular, a failed browser
                # render should not launch the vision checker, and a needs-work
                # visual verdict should not be followed by Git reads that the next
                # edit will immediately stale.
                current = self.sessions.load(session_id)
                current_visual = self._current_visual_check_data(current)
                if (
                    core_name == "visual.check"
                    and current_visual is not None
                    and current_visual.get("verdict") != "pass"
                ):
                    break
                if any(
                    reason.startswith("active failed check:")
                    for reason in completion_gate.floor(self.core, current)
                ):
                    break

    def _failure_summary(self, record: SessionRecord) -> str:
        """The first error the check printed plus the tail of its output.

        A model told only "the check failed" re-runs the same command to find
        out why, at full price. The summary it actually needs is bounded, so the
        failing line is kept in front of the part that explains it. Visual
        checker findings get the same treatment: only the compact issues return
        to the coding model, never the screenshot or checker conversation.
        """

        current = completion_gate.identity(self.core, record)
        for entry in reversed(record.provider_history):
            if entry.get("role") != "tool" or entry.get("core_name") != "visual.check":
                continue
            result = entry.get("result")
            data = result.get("data") if isinstance(result, dict) else None
            if not isinstance(data, dict) or data.get("completion_identity") != current:
                continue
            if data.get("verdict") == "pass":
                break
            pieces: List[str] = []
            issues = data.get("issues")
            if isinstance(issues, list):
                pieces.extend(str(item).strip() for item in issues if str(item).strip())
            for channel in ("page_errors", "console_errors"):
                values = data.get(channel)
                if isinstance(values, list):
                    pieces.extend(str(item).strip() for item in values if str(item).strip())
            if pieces:
                return _bounded_failure_summary(
                    "Visual review: " + "; ".join(pieces[:5])
                )
            verdict = str(data.get("verdict") or "").strip()
            if verdict and verdict != "pass":
                return _bounded_failure_summary("Visual review: " + verdict)
            break

        output = ""
        for entry in reversed(record.provider_history):
            if entry.get("role") != "tool" or entry.get("core_name") != "checks.run":
                continue
            result = entry.get("result")
            if not isinstance(result, dict) or result.get("ok") is True:
                return ""
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            # Test runners split the story: pytest writes the failing assertion
            # to stdout and its progress dots to the terminal, while a compiler
            # writes the error to stderr. Both halves are the failure.
            output = "\n".join(
                part
                for part in (
                    str(data.get("stdout") or "").strip(),
                    str(data.get("stderr") or "").strip(),
                )
                if part
            )
            detail = data.get("detail")
            if isinstance(detail, str) and detail:
                output = f"{detail}\n{output}"
            break
        return _bounded_failure_summary(output)

    # -- the evidence chain -------------------------------------------------

    def _verification(self, record: SessionRecord) -> bool:
        return self._evidence_chain(record) and not completion_gate.floor(self.core, record)

    @staticmethod
    def _evidence_chain(record: SessionRecord) -> bool:
        write: Optional[Dict[str, Any]] = None
        check: Optional[Dict[str, Any]] = None
        status: Optional[Dict[str, Any]] = None
        diff: Optional[Dict[str, Any]] = None
        for entry in record.provider_history:
            if entry.get("role") != "tool" or not isinstance(entry.get("result"), dict):
                continue
            result = entry["result"]
            # Who asked for a call is recorded, and only the two known authors
            # are evidence: the model's own calls, and the harness's finishing
            # checks. An unknown author is treated the way an unverified write
            # is, because the chain exists to prove what happened, not to
            # collect anything that claims it did.
            if result.get("requested_by") not in (None, HARNESS_REQUESTED_BY):
                continue
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
        if completion_gate.docs_only(record):
            # No behavioral obligation for text; keep persisted change/review evidence.
            status = diff = None
            for entry in record.provider_history:
                result = entry.get("result") or {}
                if entry.get("core_name") in MUTATION_TOOLS:
                    status = diff = None
                elif result.get("ok") and result.get("requested_by") in (None, HARNESS_REQUESTED_BY):
                    if entry.get("core_name") == "git.status":
                        status = result
                    elif entry.get("core_name") == "git.diff":
                        diff = result
            selected = (write, status, diff)
        else:
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
        shadow_failure: Optional[Dict[str, Any]] = None
        if self.semantic_shadow and self._shadow_decision is not None:
            shadow_failure = self._shadow_pre_usage_fields()
            shadow_failure.update(
                {
                    "kind": "provider_failure",
                    "step": step,
                    "error_kind": error.kind.value,
                }
            )
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
            if shadow_failure is not None:
                raw = record.usage.get("shadow_provider_failures")
                failures = [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
                failures.append(dict(shadow_failure))
                record.usage["shadow_provider_failures"] = failures[-100:]
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
        self._shadow_snapshot = None
        self._shadow_decision = None

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

    def _finish_attempt_call_ids(self, response: ModelResponse) -> set[str]:
        """Return mutation calls that explicitly ask the harness to finalize."""

        requested: set[str] = set()
        for call in response.tool_calls:
            core_name = self._tool_aliases.get(call.name, call.name)
            if core_name not in MUTATION_TOOLS:
                continue
            try:
                value = _tool_argument_json_loads(call.raw_arguments)
            except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
                continue
            if not isinstance(value, dict):
                continue
            if value.get("more_semantic_changes") is False:
                requested.add(call.call_id)
                continue
            # Backward compatibility for scripted providers and older clients.
            if value.get("finish_attempt") is True:
                requested.add(call.call_id)
        return requested

    @staticmethod
    def _finish_attempt_succeeded(
        record: SessionRecord, call_ids: set[str]
    ) -> bool:
        if not call_ids:
            return False
        for entry in reversed(record.provider_history):
            if entry.get("role") != "tool":
                continue
            if str(entry.get("tool_call_id") or "") not in call_ids:
                continue
            result = entry.get("result")
            data = result.get("data") if isinstance(result, dict) else None
            if (
                isinstance(result, dict)
                and result.get("ok") is True
                and isinstance(data, dict)
                and data.get("changed") is True
            ):
                return True
        return False

    def _final_check_attempt_call_ids(self, response: ModelResponse) -> set[str]:
        """Return call ids when this response explicitly runs every final check."""

        if not self._changed_once or not response.tool_calls:
            return set()
        commands = [
            tuple(item) for item in self.core.verification_command_order
        ] or [tuple(item) for item in sorted(self.core.verification_commands)]
        if not commands or any("*" in command for command in commands):
            return set()

        seen: list[tuple[str, ...]] = []
        call_ids: set[str] = set()
        for call in response.tool_calls:
            if self._tool_aliases.get(call.name, call.name) != "checks.run":
                return set()
            try:
                arguments = _strict_json_loads(call.raw_arguments)
            except (TypeError, ValueError, json.JSONDecodeError, RecursionError):
                return set()
            if not isinstance(arguments, dict):
                return set()
            argv = arguments.get("argv")
            if (
                not isinstance(argv, list)
                or not argv
                or not all(isinstance(item, str) for item in argv)
            ):
                return set()
            seen.append(tuple(argv))
            call_ids.add(call.call_id)

        if len(seen) != len(commands) or set(seen) != set(commands):
            return set()
        return call_ids

    @staticmethod
    def _final_check_attempt_succeeded(
        record: SessionRecord, call_ids: set[str]
    ) -> bool:
        pending = set(call_ids)
        if not pending:
            return False
        for entry in reversed(record.provider_history):
            if entry.get("role") != "tool":
                continue
            call_id = str(entry.get("tool_call_id") or "")
            if call_id not in pending:
                continue
            if entry.get("core_name") != "checks.run":
                return False
            result = entry.get("result")
            data = result.get("data") if isinstance(result, dict) else None
            if (
                not isinstance(result, dict)
                or result.get("ok") is not True
                or not isinstance(data, dict)
                or data.get("verification_eligible") is not True
            ):
                return False
            pending.remove(call_id)
            if not pending:
                return True
        return False

    def _step_ran_the_check(self, response: ModelResponse) -> bool:
        """Whether this step could have made the artifact provable.

        Only a check can, so a step without one cannot change what the guarantee
        protects. Testing it first keeps the per-step cost to one walk of the
        history in the step that earns it, not in every step.
        """

        return any(
            self._tool_aliases.get(call.name, call.name) == "checks.run"
            for call in response.tool_calls
        )

    def _render_target(self, record: SessionRecord) -> Optional[pathlib.Path]:
        """Choose the page that represents the current visual deliverable.

        A stylesheet or script can change what a page looks like without changing
        the HTML bytes themselves. Prefer an explicitly changed page; otherwise a
        repository with exactly one root page has an unambiguous target. A prior
        target may be reused when it still exists. Multiple otherwise-unrelated
        pages are deliberately ambiguous rather than guessed.
        """

        pages = default_pages(self.core.repository)
        if not pages:
            return None
        changed = {pathlib.PurePosixPath(item).name for item in record.changed_files}
        changed_pages = [path for path in pages if path.name in changed]
        if len(changed_pages) == 1:
            return changed_pages[0]
        if self._render_review_page:
            previous = [path for path in pages if path.name == self._render_review_page]
            if len(previous) == 1:
                return previous[0]
        if len(pages) == 1:
            return pages[0]
        return None

    def _capture_render(self, record: SessionRecord, *, required: bool = False) -> None:
        """Show the model the page its own check just measured.

        The measured difference between two runs of one task with one model was
        not the prompt but the feedback: the run that looked at its rendered page
        produced clownfish with bands and eyes, and the run that only read a
        verdict produced flat orange fish. Cline did the looking; Karo did not.
        Bounded by a per-run count and by the artifact actually changing, so a
        picture is paid for when it can teach something.

        ``required`` bypasses that count. A duty-bound frame -- the one a grade
        will rest on -- cannot be skipped because eight opportunistic frames were
        already spent, which is exactly how a measured run finished ``verified``
        against a picture of a page it had since replaced: its own last message
        said no frame of the final change had ever reached it.
        """

        if not self._render_feedback:
            return
        if not required and self._captures >= MAX_RENDER_CAPTURES:
            return
        if (
            not required
            and self._last_capture_step >= 0
            and self._step - self._last_capture_step < RENDER_CAPTURE_MIN_STEPS
        ):
            return
        page = self._render_target(record)
        if page is None:
            return
        try:
            digest = hashlib.sha256(page.read_bytes()).hexdigest()
        except OSError:
            return
        inputs = completion_gate.identity(self.core, record)
        if digest == self._last_capture_digest and inputs == self._render_review_identity:
            return
        image = screenshot(page)
        if image is None:
            return
        self._pending_render_image = image
        self._pending_render_page = page.name
        self._last_capture_digest = digest
        # The frame is kept as well as queued: the review turn is asked for at
        # the end of the run, by which time the queued copy has been sent and
        # cleared, and re-rendering the page to ask a question about it would
        # both cost a browser launch and risk grading a different picture.
        self._render_review_frame = image
        self._render_review_page = page.name
        self._render_review_digest = digest
        self._render_review_identity = inputs
        self._captures += 1
        self._last_capture_step = self._step
        self._emit(
            AgentEventKind.ARTIFACT_CREATED,
            tool="render_feedback",
            summary=f"rendered {page.name} for the model to look at",
        )

    def _page_digest(self, record: SessionRecord) -> str:
        """The sha256 of the page this run delivers, or "" when it has none.

        The grade's own metadata carries a digest, and the reason is this: a
        verdict about a picture of a page that has since changed is not a verdict
        about the artifact. Comparing the two is what makes ``verified`` mean the
        bytes on disk were looked at.
        """

        page = self._render_target(record)
        if page is None:
            return ""
        try:
            return hashlib.sha256(page.read_bytes()).hexdigest()
        except OSError:
            return ""

    def _start_visual_review(
        self, session_id: str, lease: MutationLease, record: SessionRecord
    ) -> bool:
        """Ask the model to grade the frame its own check measured.

        Returns True when a review turn was issued. The turn is required rather
        than suggested: a page is a deliverable whose acceptance is a picture, and
        "the check passed" and "the picture is what the task asked for" are
        different facts that only the model can compare.

        The frame graded is always a picture of the page as it stands: when the
        bytes changed since the last capture, a fresh frame is taken first and the
        per-run capture budget does not apply to it. A grade of an older picture
        is what let a measured run report ``verified`` for a page nobody had seen.
        """

        if not self._render_feedback or not completion_gate.visual_required(record):
            return False
        # The isolated visual checker is the modern semantic grader. Once the
        # current completion identity already has both browser-backed technical
        # proof and a clean vision verdict, asking the main coding model to grade
        # the same frame again is not additional evidence; it is a new model turn
        # that can reopen a proven artifact. R16 reached exact proof and then lost
        # it through this legacy prompt. Keep the old in-transcript review only as
        # a fallback when that exact isolated proof is still absent.
        if self._current_visual_completion_proven(record):
            return False
        current = self._page_digest(record)
        if current and (current != self._render_review_digest or completion_gate.identity(self.core, record) != self._render_review_identity):
            # The deliverable moved on: grade what is on disk, not what was.
            self._capture_render(record, required=True)
            if current != self._render_review_digest:
                # A browser that cannot render means no frame; there is then
                # nothing to grade, and the run is honest about that rather than
                # grading the previous picture.
                return False
        if self._render_review_frame is None:
            return False
        digest = self._render_review_digest
        if self._settled_render_key(record, digest) in self._settled_renders:
            # A render that was graded clean is not graded again: the picture did
            # not change, and neither did the requirements it was graded against.
            return False
        reviews = self._reviewed_renders.get(digest, 0)
        if reviews >= VISUAL_REVIEWS_PER_RENDER:
            return False
        if self._awaiting_visual_review:
            # Showing the same outstanding frame again is not another review
            # attempt. Models are allowed to run a check or make a repair before
            # answering the grade; only an actual grade consumes the bounded
            # review budget. Re-arm the image and keep the original question.
            self._pending_render_image = self._render_review_frame
            self._pending_render_page = self._render_review_page
            return True
        self._pending_render_image = self._render_review_frame
        self._pending_render_page = self._render_review_page
        self._awaiting_visual_review = True
        entries = completion_gate.visual_review_entries(record)
        self._pending_review_ids = requirements_ledger.expected_ids(entries)
        self._pending_review_ledger = (
            requirements_ledger.ledger_digest(entries) if entries else ""
        )
        prompt = (
            VISUAL_REVIEW_LEDGER_PROMPT.format(
                requirements=requirements_ledger.review_listing(entries)
            )
            if entries
            else VISUAL_REVIEW_PROMPT
        )
        self._append_message(
            session_id,
            lease,
            {
                "role": "user",
                "content": prompt,
                "kind": "visual_review_prompt",
            },
            phase="verification",
        )
        return True

    def _settled_render_key(self, record: SessionRecord, digest: str) -> str:
        """The key a clean grade is settled under.

        A render graded against one set of requirements is not graded against
        another: a late user turn admits new IDs, and the old pass must not
        cover them. Binding the ledger digest into the key is what makes a
        settled grade survive only as long as the requirements it answered.
        """

        entries = list(record.requirements)
        ledger = requirements_ledger.ledger_digest(entries) if entries else ""
        inputs = completion_gate.identity(self.core, record)
        state = hashlib.sha256(json.dumps(inputs, sort_keys=True).encode()).hexdigest()
        return f"{digest}:{ledger}:{state}"

    @staticmethod
    def _visual_verdict(
        text: Optional[str], expected_ids: Optional[Sequence[str]] = None
    ) -> Optional[bool]:
        """Read the grade the review prompt demanded, or None if it is missing.

        The structured read is :func:`classify_visual_verdict`; this bool view
        is what the run loop needs (a grade arrived / it was clean). A grade
        whose own lines say UNMET or NOT VISIBLE is not clean even when its
        global VISUAL line says OK: asked for a single verdict, the same model
        once passed a frame whose one visible object was an oversized sphere,
        and it only named that sphere when it had to say, line by line, what
        in the frame showed each requirement.
        """

        classified = classify_visual_verdict(text, expected_ids)
        if classified is None:
            return None
        return classified["clean"]

    def _settle_visual_review(
        self,
        session_id: str,
        lease: MutationLease,
        record: SessionRecord,
        message: Optional[str],
    ) -> Optional[AgentReport]:
        """Record the grade and either finish or send the model back to work."""

        classified = classify_visual_verdict(
            message, self._pending_review_ids or None
        )
        clean = None if classified is None else classified["clean"]
        unassessed_ids = list(classified["unassessed"]) if classified else []
        self._awaiting_visual_review = False
        digest = self._render_review_digest
        # Count answered grades, not prompts. A model that uses the review turn
        # to run a tool has not disagreed with the frame yet and must not burn one
        # of the bounded review attempts.
        reviews = self._reviewed_renders.get(digest, 0) + 1
        self._reviewed_renders[digest] = reviews
        ledger = self._pending_review_ledger
        self._pending_review_ids = []
        self._pending_review_ledger = ""
        named = self._violation_lines(message)
        current = self._page_digest(record)
        if clean is True and (not current or current != digest or completion_gate.identity(self.core, record) != self._render_review_identity):
            # The grade is clean, and it is clean about a picture the page has
            # left behind. Recording it as a pass would be the same lie the
            # digest comparison exists to prevent, so it is recorded as what it
            # is -- a grade of an earlier render -- and the current bytes are
            # offered for grading in their turn.
            self._record_visual_evidence(
                session_id, lease, clean=None, violations=["the page changed after this grade"], ledger_digest=ledger
            )
            self._emit(
                AgentEventKind.WARNING,
                reason="render_review_stale",
                detail=(
                    "the render grade described an earlier revision of the page; "
                    "asking again about the delivered bytes"
                ),
            )
            return None
        self._record_visual_evidence(
            session_id,
            lease,
            clean=clean,
            violations=named,
            ledger_digest=ledger,
            unassessed=unassessed_ids,
        )
        if clean is True:
            self._settled_renders.add(self._settled_render_key(record, digest))
        verified = self._verification(self.sessions.load(session_id))
        if clean is True and verified:
            return self._finish(
                session_id, lease, "verified", "completed", "verified", message
            )
        reviews = self._reviewed_renders.get(digest, 0)
        if clean is None:
            detail = (
                "the render grade did not answer in the required form; the run "
                "keeps the check's own verdict"
            )
        else:
            detail = (
                "the render grade named requirements the page does not satisfy: "
                + "; ".join(named[:5])
            )
        if clean is not True:
            self._emit(
                AgentEventKind.WARNING, reason="render_review_unmet", detail=detail
            )
        # A grade that arrives before the run is finished -- which is now the
        # normal case, because the grade is asked for as soon as a render first
        # passes -- is a finding, not a terminal state: the work continues and
        # the new render is graded in its turn.
        if reviews < VISUAL_REVIEWS_PER_RENDER and clean is not True:
            if unassessed_ids and not named:
                content = VISUAL_REVIEW_UNASSESSED_PROMPT.format(
                    requirements=", ".join(unassessed_ids)
                )
            elif unassessed_ids:
                content = (
                    VISUAL_REVIEW_FIX_PROMPT
                    + "\n\n"
                    + VISUAL_REVIEW_UNASSESSED_PROMPT.format(
                        requirements=", ".join(unassessed_ids)
                    )
                )
            else:
                content = VISUAL_REVIEW_FIX_PROMPT
            self._append_message(
                session_id,
                lease,
                {
                    "role": "user",
                    "content": content,
                    "kind": "visual_review_fix_prompt",
                },
                phase="verification",
            )
            return None
        if verified:
            return self._finish(
                session_id, lease, "verified", "completed", "verified", message
            )
        return None

    @staticmethod
    def _violation_lines(message: Optional[str]) -> List[str]:
        """The requirements a grade said the render does not satisfy.

        A NOT VISIBLE IN FRAME line is not a violation -- the frame neither
        shows nor refutes the requirement -- but it is a finding the run must
        carry: a requirement left unassessed is not a requirement passed.
        """

        lines: List[str] = []
        for raw in (message or "").splitlines():
            text = raw.strip()
            if not text or text.upper().startswith("VISUAL:"):
                continue
            if (
                _UNMET_LINE.match(text)
                or _NOT_VISIBLE_LINE.match(text)
                or re.match(r"^(\d+[.)]|[-*])\s+", text)
            ):
                lines.append(text[:300])
        return lines

    def _record_visual_evidence(
        self,
        session_id: str,
        lease: MutationLease,
        *,
        clean: Optional[bool],
        violations: List[str],
        ledger_digest: str = "",
        unassessed: Optional[List[str]] = None,
    ) -> None:
        """Put the grade in the durable record, where a reader can find it.

        The verdict is the model's own judgement, so it is stored as its own
        evidence kind with the digest of the frame that was graded: an artifact
        changed after a grade has not been graded, and that is the property a
        later reader needs to see. The requirement ledger's digest travels with
        it, so a grade made against one set of requirements is identifiable as
        such even after the ledger grows. Requirements the grade answered
        NOT VISIBLE IN FRAME are recorded as unassessed: a reader must see
        that they were neither satisfied nor refuted by the frame.
        """

        summary = {
            True: "Render review found no unmet requirement",
            False: f"Render review named {len(violations) or 1} unmet requirement(s)",
            None: "Render review did not answer in the required form",
        }[clean]
        metadata: Dict[str, Any] = {
            "page": self._render_review_page,
            "artifact_sha256": self._render_review_digest,
            "completion_identity": self._render_review_identity,
            "requirements_digest": ledger_digest,
            "clean": clean,
            "unmet_requirements": violations[:10],
            "unassessed_requirements": list(unassessed or [])[:10],
        }
        evidence = EvidenceRecord(
            kind="render_review", summary=summary, metadata=metadata
        )

        def update(current: SessionRecord) -> None:
            current.evidence.append(evidence.to_dict())

        self._update_session(session_id, lease, update)

    def _note_the_plan(
        self,
        session_id: str,
        lease: MutationLease,
        record: SessionRecord,
        steps: int,
        *,
        compacting: bool = False,
    ) -> Optional[SessionRecord]:
        """Show the model its own plan every few steps, and before a compaction.

        The list is structured state, so it is not dropped when turns are; what
        is dropped is the model's memory that it wrote one. Handing it back is
        cheap and keeps a long run aimed at the item it was on.
        """

        text = _render_plan_note(record.plan_items)
        if text is None:
            return None
        due = compacting or (steps > 0 and steps % PLAN_NOTE_EVERY_STEPS == 0)
        if not due or self._plan_noted_step == steps:
            return None
        self._append_message(
            session_id,
            lease,
            {"role": "user", "content": text, "kind": "plan_note"},
            phase=record.phase,
        )
        self._plan_noted_step = steps
        return self.sessions.load(session_id)

    def _note_the_deadline(
        self,
        session_id: str,
        lease: MutationLease,
        record: SessionRecord,
        remaining: float,
    ) -> Optional[SessionRecord]:
        """Tell a still-working run that its budget is nearly spent.

        The model has no clock: a measured high-effort run kept improving a page
        that had already passed its check and finished with a broken one. Once a
        proven state exists and the budget is nearly gone, finishing is the only
        rational move, and saying so is the cheapest way to get it -- once per
        run, and only when there is something to consolidate.
        """

        if self.limits.max_seconds <= 0:
            return None
        note_after = max(
            DEADLINE_NOTE_MIN_SECONDS,
            float(self.limits.max_seconds) * DEADLINE_NOTE_SHARE,
        )
        if remaining > note_after:
            return None
        if self._prompt_count(record.provider_history, "deadline_note"):
            return None
        stored = dict(record.verified_snapshot) if record.verified_snapshot else {}
        if not stored and self._verified_snapshot:
            stored = dict(self._verified_snapshot)
        if not stored.get("files"):
            return self._note_the_unproven_deadline(session_id, lease, record, remaining)
        self._append_message(
            session_id,
            lease,
            {
                "role": "user",
                "content": DEADLINE_NOTE
                % (max(1, int(remaining // 60)), int(stored.get("step") or 0)),
                "kind": "deadline_note",
            },
            phase=record.phase,
        )
        self._emit(
            AgentEventKind.WARNING,
            reason="deadline_note",
            detail="told the model to consolidate onto the state that passed",
        )
        return self.sessions.load(session_id)

    @staticmethod
    def _last_output_tokens(history: List[Dict[str, Any]]) -> int:
        for entry in reversed(history):
            if entry.get("role") == "assistant" and entry.get("provider"):
                usage = entry.get("usage")
                if not isinstance(usage, dict):
                    return 0
                for key in ("completion_tokens", "output_tokens"):
                    value = usage.get(key)
                    if isinstance(value, int) and not isinstance(value, bool):
                        return value
                return 0
        return 0

    @staticmethod
    def _last_core_tool(history: List[Dict[str, Any]]) -> Optional[str]:
        """Return the latest executed core tool name, ignoring prose turns."""

        for entry in reversed(history):
            if entry.get("role") != "tool" and not entry.get("tool_name"):
                continue
            name = entry.get("core_name") or entry.get("tool_name")
            if isinstance(name, str) and name:
                return name
        return None

    @staticmethod
    def _last_model_finish_reason(history: List[Dict[str, Any]]) -> Optional[str]:
        for entry in reversed(history):
            if entry.get("role") == "assistant" and entry.get("provider"):
                reason = entry.get("finish_reason")
                return reason if isinstance(reason, str) and reason else None
        return None

    @staticmethod
    def _last_model_was_toolless_length(history: List[Dict[str, Any]]) -> bool:
        for entry in reversed(history):
            if entry.get("role") != "assistant" or not entry.get("provider"):
                continue
            return (
                entry.get("finish_reason") == "length"
                and not entry.get("tool_calls")
            )
        return False

    @staticmethod
    def _pending_action_prompt_kind(
        history: List[Dict[str, Any]], kinds: set[str]
    ) -> Optional[str]:
        """Return the newest actionable user prompt awaiting a model response."""

        prompt_index = -1
        prompt_kind: Optional[str] = None
        for index, entry in enumerate(history):
            kind = entry.get("kind")
            if entry.get("role") == "user" and isinstance(kind, str) and kind in kinds:
                prompt_index = index
                prompt_kind = kind
        if prompt_index < 0:
            return None
        if any(
            entry.get("role") == "assistant" and entry.get("provider")
            for entry in history[prompt_index + 1 :]
        ):
            return None
        return prompt_kind

    @staticmethod
    def _visual_repair_malformed_retry_pending(
        history: List[Dict[str, Any]],
    ) -> bool:
        """Keep one low-budget act retry after a malformed recovery mutation.

        The visual output-recovery prompt is normally consumed by one assistant
        turn. R17 showed a narrower failure mode: StepFun did act immediately,
        but its repo_edit_file JSON was truncated/malformed before Core could
        execute a mutation. That turn made zero repository progress, so the next
        request should preserve the same action discipline once -- not reopen
        high-effort inspection. A second assistant turn ends this special case;
        repeated failures then fall through to the generic mutation-mistake
        budget.
        """

        recovery_index = -1
        for index, entry in enumerate(history):
            if (
                entry.get("role") == "user"
                and entry.get("kind") == "visual_repair_output_recovery"
            ):
                recovery_index = index
        if recovery_index < 0:
            return False

        episode = history[recovery_index + 1 :]
        assistants = [
            entry
            for entry in episode
            if entry.get("role") == "assistant" and entry.get("provider")
        ]
        # Before the first recovery assistant turn, the ordinary
        # visual_repair_output_recovery prompt already supplies low/4k act mode.
        # This helper is specifically the *post-malformed* extension, so it only
        # becomes true after one assistant actually emitted a broken mutation.
        if not assistants:
            return False
        if len(assistants) != 1:
            return False

        calls: Dict[str, str] = {}
        for raw in assistants[0].get("tool_calls") or []:
            if not isinstance(raw, dict):
                continue
            call_id = str(raw.get("call_id") or "")
            name = str(raw.get("name") or "")
            core_name = HISTORICAL_TOOL_ALIASES.get(name, name)
            if call_id and core_name in MUTATION_TOOLS:
                calls[call_id] = core_name
        if not calls:
            return False

        malformed = False
        for entry in episode:
            if entry.get("role") != "tool":
                continue
            call_id = str(entry.get("tool_call_id") or "")
            if call_id not in calls:
                continue
            result = entry.get("result")
            if isinstance(result, dict) and result.get("ok") is True:
                data = result.get("data")
                if isinstance(data, dict) and data.get("changed") is True:
                    return False
            error = entry.get("error")
            if not isinstance(error, dict) and isinstance(result, dict):
                maybe_error = result.get("error")
                if isinstance(maybe_error, dict):
                    error = maybe_error
            if isinstance(error, dict) and error.get("type") == "malformed_arguments":
                malformed = True
            else:
                return False
        return malformed

    def _effective_reasoning_effort(self, record: SessionRecord) -> Optional[str]:
        """Use one lower-reasoning act turn after bounded recovery prompts.

        Claude/Codex-style plan→act separation is useful only at the boundary:
        the normal task keeps the user's requested effort, but once KaroX has
        already supplied concrete evidence and explicitly asked for one action,
        another max-effort deliberation is counterproductive. R15 measured a
        16k-token tool-less truncation followed by another 12k-token truncation
        on the recovery turn. The override lasts for exactly one provider turn.
        """

        configured = self.reasoning_effort
        act_kinds = {
            "output_limit_prompt",
            "failed_check_output_recovery",
            "visual_repair_output_recovery",
            "mutation_mistake_recovery",
            "failed_check_action",
            "visual_repair_action",
            "repair_action_gate",
            "tool_protocol_prompt",
            "empty_response_prompt",
        }
        active_kind = self._pending_action_prompt_kind(
            record.provider_history, act_kinds
        )
        if (
            active_kind is None
            and self._visual_repair_malformed_retry_pending(record.provider_history)
        ):
            active_kind = "visual_repair_output_recovery"
        if active_kind is None:
            return configured
        if configured is None:
            return None
        order = {"low": 0, "medium": 1, "high": 2, "xhigh": 3, "max": 4}
        if configured not in order:
            return configured
        target = "low" if active_kind == "visual_repair_output_recovery" else "medium"
        return configured if order[configured] <= order[target] else target

    def _effective_max_output_tokens(self, record: SessionRecord) -> Optional[int]:
        """Bound semantic bursts in long autonomous runs without shrinking quality.

        The caller's configured ceiling remains the hard maximum. Adaptive caps
        apply only on budgets long enough to be autonomous. Visual web work gets
        one bounded large-create allowance even on the first semantic turn; 24k
        tokens is enough for a substantial single-file page, while a real clipping
        event fails open on the next turn instead of silently sacrificing quality.
        """

        configured = self.max_output_tokens
        if configured is None:
            return None
        if float(self.limits.max_seconds) < ADAPTIVE_OUTPUT_MIN_BUDGET_SECONDS:
            return configured
        visual_web = completion_gate.web_render_intent(record)
        active_action_kind = self._pending_action_prompt_kind(
            record.provider_history,
            {
                "visual_repair_output_recovery",
                "visual_repair_action",
                "visual_repair",
                "failed_check_action",
                "mutation_mistake_recovery",
                "repair_action_gate",
            },
        )
        malformed_visual_retry = self._visual_repair_malformed_retry_pending(
            record.provider_history
        )
        if active_action_kind is None and malformed_visual_retry:
            active_action_kind = "visual_repair_output_recovery"
        if active_action_kind == "visual_repair_output_recovery":
            # Low reasoning provides the action discipline here; 4k proved
            # too small in R18 both for a multi-site edit (malformed/truncated JSON)
            # and for a later water-volume repair that clipped before any tool call.
            # Keep enough payload room to serialize one bounded visual mutation.
            recovery_cap = (
                ADAPTIVE_OUTPUT_VISUAL_REPAIR
                if malformed_visual_retry
                else ADAPTIVE_OUTPUT_VISUAL_ACTION_RECOVERY
            )
            return min(configured, recovery_cap)
        # A tool-less clipped turn applied no repository work. Keep its
        # recovery bounded: R8 showed that fail-opening a visual page straight to
        # the caller's 64k ceiling only buys more hidden planning, while the
        # bounded recovery immediately emitted a scaffold write.
        if self._last_model_was_toolless_length(record.provider_history):
            recovery = (
                ADAPTIVE_OUTPUT_VISUAL_LIMIT_RECOVERY
                if visual_web
                else ADAPTIVE_OUTPUT_LIMIT_RECOVERY
            )
            return min(configured, recovery)
        # Fail open only after a clipped turn that already carried useful work.
        # In that case the larger ceiling may be needed to finish a real mutation
        # rather than to repeat tool-less deliberation.
        if self._last_model_finish_reason(record.provider_history) == "length":
            return configured
        if self._awaiting_visual_review:
            return min(configured, ADAPTIVE_OUTPUT_VERIFICATION)

        # R15 measured a forced visual act turn hitting the 2k proof ceiling
        # exactly, before it could emit the multi-site edit it had already been
        # told to make. Keep inspection turns tiny, but once the harness says
        # "stop inspecting and act", give that single medium-reasoning turn the
        # normal visual-repair payload budget.
        if active_action_kind == "visual_repair_action":
            return min(configured, ADAPTIVE_OUTPUT_VISUAL_REPAIR)

        # A concrete failed check is stronger context than the kind of read the
        # model performed afterwards.  Without this priority, repo.read_file is
        # classified as create-context and reopens a 24k burst while the run is
        # already in repair mode -- exactly the loop measured on aquarium R6.
        if self._failed_check_inspection_state(record) is not None:
            return min(configured, ADAPTIVE_OUTPUT_VERIFICATION)

        visual_repair_state = self._visual_repair_inspection_state(record)
        if visual_repair_state is not None and visual_repair_state[0] > 0:
            return min(configured, ADAPTIVE_OUTPUT_VERIFICATION)

        if active_action_kind == "visual_repair":
            return min(configured, ADAPTIVE_OUTPUT_VISUAL_REPAIR)
        if active_action_kind in {
            "failed_check_action",
            "visual_repair_action",
            "mutation_mistake_recovery",
        }:
            return min(configured, ADAPTIVE_OUTPUT_VERIFICATION)

        # Once a concrete verification failure is active, diagnostic reads must
        # not reopen a large create/reasoning budget. The failure already names
        # the problem; subsequent inspection turns are act-first repair context.
        failed_check_state = self._failed_check_inspection_state(record)
        if failed_check_state is not None and failed_check_state[0] > 0:
            return min(configured, ADAPTIVE_OUTPUT_VERIFICATION)

        last_tool = self._last_core_tool(record.provider_history)
        if last_tool is None and visual_web:
            return min(configured, ADAPTIVE_OUTPUT_VISUAL_CREATE)
        if last_tool in {"checks.run", "visual.check", "git.status", "git.diff"}:
            return min(configured, ADAPTIVE_OUTPUT_VERIFICATION)
        if last_tool in MUTATION_TOOLS:
            if visual_web:
                return min(configured, ADAPTIVE_OUTPUT_VISUAL_REPAIR)
            return min(configured, ADAPTIVE_OUTPUT_AFTER_MUTATION)
        if last_tool in ADAPTIVE_OUTPUT_INSPECTION_TOOLS:
            if self._failed_check_inspection_state(record) is not None:
                return min(configured, ADAPTIVE_OUTPUT_VERIFICATION)
            return min(configured, ADAPTIVE_OUTPUT_AFTER_INSPECTION)
        if last_tool == "web.fetch" and not visual_web:
            # External context is inspection-like for ordinary coding: it should
            # inform the next action, not unlock another whole-file-size burst.
            return min(configured, ADAPTIVE_OUTPUT_AFTER_INSPECTION)
        if last_tool in ADAPTIVE_OUTPUT_CREATE_CONTEXT_TOOLS:
            return min(
                configured,
                ADAPTIVE_OUTPUT_VISUAL_CREATE if visual_web else ADAPTIVE_OUTPUT_LARGE_CREATE,
            )
        return configured

    def _note_the_pace(
        self,
        session_id: str,
        lease: MutationLease,
        record: SessionRecord,
        steps: int,
    ) -> Optional[SessionRecord]:
        """Tell a run whose last step deliberated at length to act sooner.

        Measured SWE runs produced correct patches and still ended on the wall
        budget: their longest responses were 2-3x the length of the CLIs that
        finished, and one hit the 32k output ceiling after 6.6 minutes. The note
        is bounded (a few per run, spaced apart) and only follows a response
        that actually acted, so it never interrupts a turn mid-protocol.
        """

        if steps <= 0:
            return None
        produced = self._last_output_tokens(record.provider_history)
        if produced < PACE_NOTE_OUTPUT_TOKENS:
            return None
        if self._prompt_count(record.provider_history, "pace_note") >= PACE_NOTE_MAX:
            return None
        if steps - self._pace_noted_step < PACE_NOTE_MIN_GAP_STEPS:
            return None
        self._append_message(
            session_id,
            lease,
            {"role": "user", "content": PACE_NOTE % produced, "kind": "pace_note"},
            phase=record.phase,
        )
        self._pace_noted_step = steps
        self._emit(
            AgentEventKind.WARNING,
            reason="pace_note",
            detail="told the model to act sooner after a long deliberation",
        )
        return self.sessions.load(session_id)

    def _current_visual_completion_proven(self, record: SessionRecord) -> bool:
        """Whether current visual bytes already have render and vision proof.

        The generic edge-case review is valuable for SWE fixes, but R16 showed it
        can destroy a finished visual artifact after exact current-byte browser
        and vision proof. Git status/diff remain enforced by _finish; this helper
        only decides whether another coding-review turn may mutate proven bytes.
        """

        if not completion_gate.web_render_intent(record):
            return False
        current = completion_gate.identity(self.core, record)
        if not current or not completion_gate.visual_proven(self.core, record):
            return False
        for entry in reversed(record.provider_history):
            if entry.get("role") != "tool" or entry.get("core_name") != "checks.run":
                continue
            result = entry.get("result")
            if not isinstance(result, dict) or result.get("ok") is not True:
                continue
            data = result.get("data")
            if not isinstance(data, dict):
                continue
            if data.get("verification_eligible") is not True:
                continue
            if data.get("completion_identity") != current:
                continue
            if completion_gate.browser_proof(data):
                return True
        return False

    def _request_edge_case_review(
        self,
        session_id: str,
        lease: MutationLease,
        record: SessionRecord,
        remaining: float,
    ) -> bool:
        """Ask once, before a long autonomous run ends verified, for the edge cases.

        Measured SWE runs (swer8 ninja-1427) ended VERIFIED on a fix whose own
        checks only replayed the reported symptom; the hidden test exercised the
        same rule one level deeper (a nested router inheriting the parent's
        auth=None) and failed. Green checks proved the reproduction, not the
        requirement. One bounded turn that makes the model map every stated
        example and the natural variants of the rule onto checks it actually ran
        is far cheaper than a wrong patch reported as proven. It never fires on
        short interactive budgets, on docs-only changes, twice, or when too little
        time is left to act on it.
        """

        if float(self.limits.max_seconds) < EDGE_REVIEW_MIN_BUDGET:
            return False
        if remaining < EDGE_REVIEW_MIN_REMAINING:
            return False
        if not record.changed_files or completion_gate.docs_only(record):
            return False
        if self._current_visual_completion_proven(record):
            return False
        if self._prompt_count(record.provider_history, "edge_case_review"):
            return False
        content = EDGE_CASE_REVIEW
        finding = self._probe_tests_on_base(record, remaining)
        if finding:
            content = EDGE_CASE_REVIEW + "\n\n" + finding
        self._append_message(
            session_id,
            lease,
            {"role": "user", "content": content, "kind": "edge_case_review"},
            phase="verification",
        )
        self._emit(
            AgentEventKind.WARNING,
            reason="edge_case_review",
            detail="asked the model to cover the task's edge cases before finishing",
        )
        return True

    def _probe_tests_on_base(self, record: SessionRecord, remaining: float) -> str:
        """Run the session's changed tests on a clean worktree of HEAD.

        Mechanical red-on-base check (Agentless' largest measured gain). It never
        touches the working tree, is bounded by the time left, and its only power
        is to add a fact to the review turn -- it cannot mark anything verified.
        """

        if not self._base_probe_enabled:
            return ""
        budget = min(BASE_PROBE_MAX_SECONDS, remaining - EDGE_REVIEW_MIN_REMAINING)
        if budget < 20.0:
            return ""
        repository = getattr(self.core, "repository", None)
        if repository is None:
            return ""
        try:
            result = base_probe.probe_tests_on_base(
                repository, list(record.changed_files), timeout_seconds=budget
            )
        except Exception:  # noqa: BLE001 - advisory only; never break a run
            return ""
        if result.status != "skipped":
            self._emit(
                AgentEventKind.WARNING,
                reason="base_probe",
                detail=f"{result.status} ({', '.join(result.tests)})",
            )
        return base_probe.describe(result)

    def _note_the_unproven_deadline(
        self,
        session_id: str,
        lease: MutationLease,
        record: SessionRecord,
        remaining: float,
    ) -> Optional[SessionRecord]:
        """Tell a long run with unverified edits to converge before the stop."""

        budget = float(self.limits.max_seconds)
        if budget < DEADLINE_NOTE_UNPROVEN_MIN_BUDGET:
            return None
        if not record.changed_files:
            return None
        note_after = max(
            DEADLINE_NOTE_UNPROVEN_MIN_SECONDS, budget * DEADLINE_NOTE_UNPROVEN_SHARE
        )
        if remaining > note_after:
            return None
        if self._prompt_count(record.provider_history, "deadline_note_unproven"):
            return None
        self._append_message(
            session_id,
            lease,
            {
                "role": "user",
                "content": DEADLINE_NOTE_UNPROVEN % max(1, int(remaining // 60)),
                "kind": "deadline_note_unproven",
            },
            phase=record.phase,
        )
        self._emit(
            AgentEventKind.WARNING,
            reason="deadline_note_unproven",
            detail="told the model to converge on a minimal verified fix",
        )
        return self.sessions.load(session_id)

    def _snapshot_verified_state(
        self, session_id: str, lease: MutationLease, record: SessionRecord
    ) -> None:
        """Keep the bytes of the state the evidence chain just proved.

        A measured high-effort run passed its check at step 4, kept polishing for
        another fifty minutes, and finished on an empty frame: the deliverable was
        worse than one the same run had already proved. Copying the changed files
        at the moment the chain holds is what makes the guarantee in
        :meth:`_restore_verified_state` possible, and it is bounded by the same
        limits Core already applies to a write.
        """

        if not self._restore_on_regression or not record.changed_files:
            return
        files = list(record.changed_files)[:_MAX_SNAPSHOT_FILES]
        directory = self.sessions.session_dir(session_id) / "verified"
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            return
        digests: Dict[str, str] = {}
        for relative in files:
            try:
                source = self.core.safe_path(relative)
            except Exception:
                continue
            if not source.is_file():
                continue
            try:
                payload = source.read_bytes()
            except OSError:
                continue
            if len(payload) > _MAX_SNAPSHOT_BYTES:
                continue
            target = directory / pathlib.PurePosixPath(str(relative))
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            except OSError:
                continue
            digests[str(relative)] = hashlib.sha256(payload).hexdigest()
        if not digests:
            return
        snapshot = {
            "step": self._step_count(record.provider_history),
            "files": digests,
            "checked_at": time.time(),
        }
        self._verified_snapshot = snapshot

        def update(current: SessionRecord) -> None:
            current.verified_snapshot = dict(snapshot)

        try:
            self._update_session(session_id, lease, update)
        except Exception:
            # A failed snapshot write must not fail a step that succeeded; the
            # in-memory copy still protects this run.
            return

    def _restore_verified_state(
        self, session_id: str, record: SessionRecord
    ) -> Optional[str]:
        """Put back the state that passed, when the run is ending worse than it.

        Returns a sentence naming the step and the files when a restore happened.
        Only files this run wrote are touched, only when the chain held earlier
        and does not hold now, and only from bytes this kernel itself recorded.
        """

        if not self._restore_on_regression:
            return None
        stored = dict(record.verified_snapshot) if record.verified_snapshot else {}
        if not stored:
            stored = dict(self._verified_snapshot) if self._verified_snapshot else {}
        files = stored.get("files")
        if not isinstance(files, dict) or not files:
            return None
        directory = self.sessions.session_dir(session_id) / "verified"
        restored: List[str] = []
        for relative, digest in files.items():
            source = directory / pathlib.PurePosixPath(str(relative))
            if not source.is_file():
                continue
            try:
                payload = source.read_bytes()
            except OSError:
                continue
            if hashlib.sha256(payload).hexdigest() != str(digest):
                continue
            try:
                target = self.core.safe_path(str(relative))
            except Exception:
                continue
            try:
                if target.is_file() and target.read_bytes() == payload:
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(payload)
            except OSError:
                continue
            restored.append(str(relative))
        if not restored:
            return None
        step = int(stored.get("step") or 0)
        self._restored_from_step = step
        return (
            "restored the state that passed at step %d: %s"
            % (step, ", ".join(sorted(restored)[:5]))
        )

    def _finish(
        self,
        session_id: str,
        lease: MutationLease,
        status: str,
        phase: str,
        reason: str,
        provider_message: Optional[str] = None,
        *,
        detail: Optional[str] = None,
    ) -> AgentReport:
        # A run that is ending unverified but once passed the check must not ship
        # what replaced that state. The restore happens before the final status is
        # written, so the report and the session both describe what the user will
        # actually find on disk.
        # Local check success cannot override missing, stale or rejected visual evidence.
        if status == "verified" and completion_gate.floor(self.core, self.sessions.load(session_id)):
            status, phase, reason = "stopped", "verification", "completion_unverified"
        if status == "verified":
            current = self.sessions.load(session_id)
            visual_required = completion_gate.visual_required(current)
            if visual_required and not completion_gate.visual_proven(self.core, current):
                status, phase, reason = "stopped", "stopped", "render_unverified"
        restore_note: Optional[str] = None
        if status != "verified":
            current = self.sessions.load(session_id)
            if self._completed(current):
                # It is verified after all: this path is the one that ends as an
                # answer or a stop after the chain already held.
                restore_note = None
            else:
                restore_note = self._restore_verified_state(session_id, current)
        if restore_note is not None:
            self._emit(
                AgentEventKind.WARNING,
                reason="regression_restored",
                detail=restore_note,
            )

        def update(record: SessionRecord) -> None:
            record.status = status
            record.phase = phase
            if status != "verified":
                record.summary = f"Native agent stopped: {reason}."
                if detail:
                    record.summary += " " + detail + "."
                if restore_note is not None:
                    record.summary += " " + restore_note + "."
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
            restored=restore_note,
        )

    def _report(
        self,
        record: SessionRecord,
        *,
        reason: str,
        provider_message: Optional[str] = None,
        restored: Optional[str] = None,
    ) -> AgentReport:
        # Every terminal path funnels through here, so this is the one place a
        # watcher can be told the run is over without the emit being forgotten
        # on some early return.
        report_project_context = dict(self.project_context)
        if self.semantic_shadow:
            try:
                report_project_context["semantic_shadow"] = summarize_shadow_usage(
                    record.usage if isinstance(record.usage, dict) else {}
                )
            except Exception:
                # Research reporting must not be able to fail a completed run.
                pass
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
            project_context=report_project_context,
            compaction=dict(self._last_compaction) if self._last_compaction else None,
            restored=restored,
            mode=self.mode,
        )

    def _completed(self, record: SessionRecord) -> bool:
        """True when this run reached either terminal outcome KaroX recognises."""
        visual_ok = (
            not completion_gate.visual_required(record)
            or completion_gate.visual_proven(self.core, record)
        )
        return (self._verification(record) and visual_ok) or self._answer_complete(record)

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
        self._shadow_apply_pending_usage(record)
        saved = self.sessions.save(record, record.revision, lease)
        # The kernel already knows the revision it just wrote, so a later
        # heartbeat does not have to read the session back to learn it.
        self._known_revision = saved.revision
        return saved


class AgentKernel:
    """The public agent kernel: the vendored karo-core engine by default.

    ``karox agent run`` and every embedder construct this name. Since B1 the
    decision loop behind it is ``src/karox/engine`` (see
    ``engine_kernel.EngineKernel``): the engine's step protocol, context
    economy, finish contract and recovery from empty/truncated responses
    drive the run, while KaroX's provider layer, typed Core tools, policy and
    risk gates, session leases, durable checkpoints and evidence-chain
    verification stay exactly as they were.

    The legacy v5 loop remains reachable for comparison runs:

    ``AgentKernel(..., legacy=True)``

    The engine path preserves this constructor's full signature and the
    ``run(session_id) -> AgentReport`` / ``on_event`` stream contract, so the
    CLI, the TUI backend and existing tests need no changes to move over.
    """

    def __init__(self, *, engine: bool = True, legacy: bool = False, **kwargs: Any) -> None:
        if legacy:
            engine = False
        if engine:
            from .engine_kernel import EngineKernel

            self.implementation: Any = EngineKernel(**kwargs)
        else:
            self.implementation = LegacyAgentKernel(**kwargs)

    def run(self, session_id: str) -> AgentReport:
        return self.implementation.run(session_id)

    def cancel(self) -> None:
        """Request cancellation at the next step boundary (engine path)."""
        cancel = getattr(self.implementation, "cancel", None)
        if callable(cancel):
            cancel()
