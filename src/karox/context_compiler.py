"""Typed model-facing context IR: the Context Compiler.

Every retained request item receives an explicit verdict with a recorded
reason before the request leaves this process:

``INCLUDE``
    The item travels to the model unchanged.
``REFERENCE``
    The item's exact bytes already travel earlier in the same request, so a
    lossless marker points at the first occurrence instead of repeating them.
``ELIDE_DUPLICATE``
    An item whose bytes are dropped entirely because a retained copy exists.
    Nothing emits this for a tool result yet -- a result keeps an explicit
    pointer instead -- but a superseded call payload whose later copy is
    byte-identical does emit it, because there the marker *is* the pointer.
``ELIDE_STALE``
    A newer result for the same tool call identity (same tool, same
    arguments) travels later in the request with different content. The old
    bytes are misleading, so a supersession marker points forward to the
    fresh result. The same verdict covers a full-content write payload that a
    later call to the same target replaced in full.
``ELIDE_IRRELEVANT``
    Reserved for relevance-driven exclusion. No deterministic relevance
    signal exists at this layer yet, so nothing emits this verdict; the
    verdict exists so diagnostics and future rules share one vocabulary.

Quality precedes reduction. An item may only leave the request when it is an
exact duplicate, provably superseded, or losslessly reachable through the
marker that replaces it. Dialogue turns (system, user, assistant) are always
included; only the *arguments of a replaced call* may be shortened, never a
turn. The compiler is deterministic: the same input sequence always produces
byte-identical decisions, so prefix caching never loses reuse to ordering
churn.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

REFERENCE_THRESHOLD_CHARS = 1_000
"""Below this size a marker saves nothing worth the indirection."""

SUPERSEDED_CALL_THRESHOLD_CHARS = 2_000
"""Below this size a superseded write payload is not worth a marker either.

A full-content write is the largest payload a request ever carries, and a
generative task rewrites the same file several times, so this rule is where the
request shrinks the most. It also applies to the call, not the result: a
superseded result is worthless either way, while a superseded write payload is
tens of thousands of characters the model paid to produce and never reads again.
"""

_REUSE_MARKER = "identical_tool_result_reused"
_STALE_MARKER = "superseded_tool_result"
_SUPERSEDED_CALL_MARKER = "superseded_tool_call"


class Verdict(str, Enum):
    """What the compiler decided to do with one context item."""

    INCLUDE = "include"
    REFERENCE = "reference"
    ELIDE_DUPLICATE = "elide_duplicate"
    ELIDE_STALE = "elide_stale"
    ELIDE_IRRELEVANT = "elide_irrelevant"


@dataclass(frozen=True)
class ContextItem:
    """One typed, model-facing request item.

    ``tool_name`` and ``tool_arguments`` describe the call that produced a
    tool result; they are the identity used for supersession. Items without
    a full identity are never marked stale, because two results can only be
    ordered when they provably answer the same question.
    """

    index: int
    role: str
    content: Optional[str]
    tool_call_id: Optional[str] = None
    tool_name: Optional[str] = None
    tool_arguments: Optional[str] = None

    @property
    def chars(self) -> int:
        return len(self.content) if isinstance(self.content, str) else 0

    @property
    def sha256(self) -> Optional[str]:
        if not isinstance(self.content, str):
            return None
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Decision:
    """The verdict for one item, with the reason on the record.

    ``call_id`` is set only for a decision about one tool call's arguments;
    the result-item rules address an item by position and leave it empty.
    """

    index: int
    verdict: Verdict
    reason: str
    replacement: Optional[str] = None
    chars_saved: int = 0
    call_id: str = ""


@dataclass(frozen=True)
class CompilationStats:
    """Deterministic accounting for one compiled request."""

    items_total: int
    included: int
    referenced: int
    elided_duplicate: int
    elided_stale: int
    elided_irrelevant: int
    chars_in: int
    chars_out: int

    @property
    def chars_saved(self) -> int:
        return max(0, self.chars_in - self.chars_out)


@dataclass(frozen=True)
class CompiledContext:
    """Decisions plus stats for one request; input order is preserved."""

    decisions: Tuple[Decision, ...]
    stats: CompilationStats

    def explain(self) -> Tuple[str, ...]:
        """Compact per-decision lines for every non-INCLUDE item."""

        lines: List[str] = []
        for decision in self.decisions:
            if decision.verdict is Verdict.INCLUDE:
                continue
            lines.append(
                f"item={decision.index} verdict={decision.verdict.value} "
                f"reason={decision.reason} saved={decision.chars_saved}"
            )
        return tuple(lines)


# -- reasoning continuity ledger ---------------------------------------------

CONTINUITY_MAX_STATEMENTS = 8
"""Most recent statements carried through one compaction; older ones age out."""

CONTINUITY_STATEMENT_CHARS = 160
"""One carried statement stays one bounded line; details live in history."""


@dataclass(frozen=True)
class ContinuityStats:
    """Deterministic accounting for one compaction's carried narration."""

    statements_carried: int
    statement_chars: int
    reasoning_blocks_dropped: int
    replayable_blocks_dropped: int


def continuity_lines(
    groups: Sequence[Sequence[Mapping[str, Any]]],
) -> Tuple[Tuple[str, ...], ContinuityStats]:
    """Carry the model's own dropped conclusions through a compaction.

    Compaction preserves tool facts but used to discard every conclusion the
    model had already stated, so after a long session the model re-derived
    decisions it had written down turns earlier. This ledger quotes the
    first line of each dropped assistant turn verbatim -- extraction, never
    paraphrase, so the mechanism stays deterministic and cannot invent a
    conclusion the model did not state. Callers must label the result as the
    model's own narration; it is not tool evidence and never merges with it.
    """

    statements: List[str] = []
    blocks = 0
    replayable = 0
    turn = 0
    for group in groups:
        for entry in group:
            if entry.get("role") != "assistant":
                continue
            turn += 1
            stored_blocks = entry.get("reasoning_blocks")
            if isinstance(stored_blocks, list):
                blocks += len(stored_blocks)
                replayable += sum(
                    1
                    for block in stored_blocks
                    if isinstance(block, Mapping)
                    and block.get("replayable") is True
                )
            content = entry.get("content")
            if not isinstance(content, str):
                continue
            first_line = next(
                (line.strip() for line in content.splitlines() if line.strip()),
                "",
            )
            if not first_line:
                continue
            if len(first_line) > CONTINUITY_STATEMENT_CHARS:
                first_line = first_line[: CONTINUITY_STATEMENT_CHARS - 3] + "..."
            statements.append(f"turn {turn}: {first_line}")
    kept = statements[-CONTINUITY_MAX_STATEMENTS:]
    stats = ContinuityStats(
        statements_carried=len(kept),
        statement_chars=sum(len(statement) for statement in kept),
        reasoning_blocks_dropped=blocks,
        replayable_blocks_dropped=replayable,
    )
    return tuple(kept), stats


def _reuse_marker(first_call_id: str) -> str:
    return json.dumps(
        {
            "karox": _REUSE_MARKER,
            "same_as_tool_call_id": first_call_id,
            # The exact bytes already exist earlier in the request. This tiny
            # deterministic hint converts a repeated observation into progress
            # guidance without forbidding further inspection when a genuinely
            # new question remains.
            "next": "act_or_change_query",
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _stale_marker(superseding_call_id: str) -> str:
    return json.dumps(
        {
            "karox": _STALE_MARKER,
            "superseded_by_tool_call_id": superseding_call_id,
        },
        ensure_ascii=False,
        sort_keys=True,
    )


def _tool_identity_arguments(raw_arguments: str) -> str:
    """Canonicalize valid object arguments for stale-result identity.

    Provider wire history retains the model's original JSON spelling.  Object
    key order is not semantic, though, and treating it as semantic can retain
    two large results for the same read solely because one call wrote
    ``path`` before ``start``.  Only valid JSON objects are normalized; every
    malformed or non-object value keeps its exact spelling so this economy
    layer cannot merge an ambiguous tool call.
    """

    try:
        decoded = json.loads(raw_arguments)
        if not isinstance(decoded, dict):
            return raw_arguments
        # Reject both non-standard constants and float overflow (1e400).
        # Normalizing either to Infinity would merge an invalid action with
        # another call and could discard the only successful read evidence.
        return json.dumps(decoded, ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError):
        return raw_arguments


# -- superseded call payloads -------------------------------------------------


@dataclass(frozen=True)
class CallPayload:
    """One tool call's arguments, exactly as they travel in the request.

    ``index`` is the call's position in request order, not the index of the
    assistant message that carries it: two calls written in one message still
    have a definite order, and supersession is only meaningful between two
    calls the model emitted at different points.
    """

    index: int
    call_id: str
    tool_name: str
    raw_arguments: str

    @property
    def chars(self) -> int:
        return len(self.raw_arguments)


@dataclass(frozen=True)
class CallPayloadStats:
    """Deterministic accounting for one compiled set of call payloads."""

    calls_total: int
    payloads_elided: int
    chars_in: int
    chars_out: int

    @property
    def chars_saved(self) -> int:
        return max(0, self.chars_in - self.chars_out)


@dataclass(frozen=True)
class CompiledCallPayloads:
    """One decision per payload, in payload order; input order is preserved."""

    decisions: Tuple[Decision, ...]
    stats: CallPayloadStats

    def explain(self) -> Tuple[str, ...]:
        """Compact per-decision lines for every replacement, for diagnostics."""

        return tuple(
            f"call={decision.index} verdict={decision.verdict.value} "
            f"reason={decision.reason} saved={decision.chars_saved}"
            for decision in self.decisions
            if decision.replacement is not None
        )

    def replacements(self) -> Dict[str, str]:
        """Call id to replacement arguments, for the messages being assembled."""

        return {
            decision.call_id: decision.replacement
            for decision in self.decisions
            if decision.replacement is not None and decision.call_id
        }


def _replacement_target(
    payload: CallPayload,
    replacement_targets: Mapping[str, str],
    incremental_markers: Mapping[str, Tuple[str, str]],
) -> Optional[str]:
    """Return the target a full-content write replaces, or ``None``.

    Only a call that *replaces a named target in full* can supersede another
    call. An incremental editor is deliberately absent from the mapping: a
    second edit composes with the first instead of replacing it, so neither
    payload makes the other stale. A call whose arguments declare it incremental
    -- ``repo_write_file`` with ``mode=append`` adds to the end of a file rather
    than replacing it -- is treated the same way even though its tool name is in
    the mapping, because the content it carries is a fragment the file cannot be
    reconstructed from. Ambiguous arguments -- malformed JSON, a non-object, a
    missing or non-text target -- are never a replacement, so this economy rule
    cannot merge two unrelated calls.
    """

    key = replacement_targets.get(payload.tool_name)
    if key is None:
        return None
    try:
        decoded = json.loads(payload.raw_arguments)
    except (TypeError, ValueError, RecursionError):
        return None
    if not isinstance(decoded, dict):
        return None
    marker = incremental_markers.get(payload.tool_name)
    if marker is not None and decoded.get(marker[0]) == marker[1]:
        return None
    target = decoded.get(key)
    if not isinstance(target, str) or not target:
        return None
    return target


def _superseded_call_marker(
    *, tool_name: str, target: str, superseding_call_id: str
) -> str:
    return json.dumps(
        {
            "karox": _SUPERSEDED_CALL_MARKER,
            "tool": tool_name,
            "target": target,
            "superseded_by_tool_call_id": superseding_call_id,
            # Stated plainly because it is true and the model must not assume
            # otherwise: the file holds the later content, and the earlier draft
            # is not retrievable from the transcript.
            "note": (
                "that later call replaced this file in full; read the file for "
                "its current content -- this earlier draft is not kept here"
            ),
        },
        ensure_ascii=False,
        sort_keys=True,
    )


class ContextCompiler:
    """Deterministically compile typed items into minimal sufficient output.

    The compiler never sees provider wire formats and never mutates its
    input; it returns one :class:`Decision` per item, in item order. Callers
    decide whether to apply replacements (economy mode) or only record the
    measurement. Nothing here depends on wall time, ordering randomness, or
    provider identity, so identical inputs always compile identically.
    """

    def __init__(
        self, reference_threshold_chars: int = REFERENCE_THRESHOLD_CHARS
    ) -> None:
        if reference_threshold_chars < 1:
            raise ValueError("reference threshold must be positive")
        self.reference_threshold_chars = int(reference_threshold_chars)

    def compile_call_payloads(
        self,
        payloads: Sequence[CallPayload],
        *,
        replacement_targets: Mapping[str, str],
        incremental_markers: Optional[Mapping[str, Tuple[str, str]]] = None,
        threshold_chars: int = SUPERSEDED_CALL_THRESHOLD_CHARS,
    ) -> CompiledCallPayloads:
        """Drop the arguments of a write a later call to the same target replaced.

        The model's own draft is usually the largest thing in the request: one
        generated page is tens of thousands of characters, and rewriting it is
        the normal way a task progresses. Every superseded draft then travels in
        every later request at full price without ever being read again.

        Only the newest write per target is kept; each older one is replaced by
        a marker naming the call that replaced it. The result is not a loss of
        working material -- the file on disk holds the newer content, and the
        marker says so -- but it is deliberately not lossless, so it is stated
        rather than implied. ``replacement_targets`` maps a provider tool name
        to the argument key naming the target it replaces in full; tools absent
        from that mapping, and calls whose target cannot be read unambiguously,
        always stay in the request. ``incremental_markers`` maps a provider tool
        name to an ``(argument key, value)`` pair that marks a call of that tool
        as incremental rather than a full replacement, so an append is kept for
        the same reason an edit is.
        """

        if threshold_chars < 1:
            raise ValueError("superseded call threshold must be positive")
        markers = incremental_markers if incremental_markers is not None else {}
        latest: Dict[Tuple[str, str], CallPayload] = {}
        targets: List[Optional[str]] = []
        for payload in payloads:
            target = _replacement_target(payload, replacement_targets, markers)
            targets.append(target)
            if target is not None:
                latest[(payload.tool_name, target)] = payload

        decisions: List[Decision] = []
        elided = 0
        chars_in = chars_out = 0
        for payload, target in zip(payloads, targets):
            chars_in += payload.chars
            decision = self._decide_call_payload(
                payload, target, latest, threshold_chars
            )
            decisions.append(decision)
            if decision.replacement is None:
                chars_out += payload.chars
            else:
                elided += 1
                chars_out += len(decision.replacement)
        return CompiledCallPayloads(
            decisions=tuple(decisions),
            stats=CallPayloadStats(
                calls_total=len(decisions),
                payloads_elided=elided,
                chars_in=chars_in,
                chars_out=chars_out,
            ),
        )

    @staticmethod
    def _decide_call_payload(
        payload: CallPayload,
        target: Optional[str],
        latest: Dict[Tuple[str, str], CallPayload],
        threshold_chars: int,
    ) -> Decision:
        if target is None:
            return Decision(
                payload.index, Verdict.INCLUDE, "not_a_full_replacement"
            )
        if payload.chars < threshold_chars:
            return Decision(
                payload.index, Verdict.INCLUDE, "below_supersession_threshold"
            )
        newer = latest.get((payload.tool_name, target))
        if (
            newer is None
            or newer.index <= payload.index
            or newer.call_id == payload.call_id
        ):
            # The newest write to this target is the one that describes the
            # file, so it always stays, whatever it cost.
            return Decision(payload.index, Verdict.INCLUDE, "newest_write")
        replacement = _superseded_call_marker(
            tool_name=payload.tool_name,
            target=target,
            superseding_call_id=newer.call_id,
        )
        if len(replacement) >= payload.chars:
            return Decision(payload.index, Verdict.INCLUDE, "replacement_not_smaller")
        identical = newer.raw_arguments == payload.raw_arguments
        return Decision(
            payload.index,
            Verdict.ELIDE_DUPLICATE if identical else Verdict.ELIDE_STALE,
            f"{'duplicate_of' if identical else 'superseded_by'}:{newer.call_id}",
            replacement=replacement,
            chars_saved=payload.chars - len(replacement),
            call_id=payload.call_id,
        )

    def compile(self, items: Sequence[ContextItem]) -> CompiledContext:
        latest_by_identity: Dict[Tuple[str, str], ContextItem] = {}
        for item in items:
            if (
                item.role == "tool"
                and isinstance(item.content, str)
                and item.tool_name
                and item.tool_arguments is not None
            ):
                latest_by_identity[
                    (item.tool_name, _tool_identity_arguments(item.tool_arguments))
                ] = item

        first_seen: Dict[str, ContextItem] = {}
        decisions: List[Decision] = []
        included = referenced = elided_stale = 0
        chars_in = chars_out = 0

        for item in items:
            chars_in += item.chars
            decision = self._decide(item, latest_by_identity, first_seen)
            decisions.append(decision)
            if decision.verdict is Verdict.INCLUDE:
                included += 1
                chars_out += item.chars
            elif decision.verdict is Verdict.REFERENCE:
                referenced += 1
                chars_out += len(decision.replacement or "")
            elif decision.verdict is Verdict.ELIDE_STALE:
                elided_stale += 1
                chars_out += len(decision.replacement or "")
            else:  # pragma: no cover - no rule emits these verdicts yet
                chars_out += item.chars

        stats = CompilationStats(
            items_total=len(decisions),
            included=included,
            referenced=referenced,
            elided_duplicate=0,
            elided_stale=elided_stale,
            elided_irrelevant=0,
            chars_in=chars_in,
            chars_out=chars_out,
        )
        return CompiledContext(decisions=tuple(decisions), stats=stats)

    def _decide(
        self,
        item: ContextItem,
        latest_by_identity: Dict[Tuple[str, str], ContextItem],
        first_seen: Dict[str, ContextItem],
    ) -> Decision:
        if item.role == "system":
            return Decision(item.index, Verdict.INCLUDE, "stable_prefix")
        if item.role != "tool":
            return Decision(item.index, Verdict.INCLUDE, "dialogue_turn")
        if not isinstance(item.content, str):
            return Decision(item.index, Verdict.INCLUDE, "no_content")

        content = item.content
        if item.chars < self.reference_threshold_chars:
            self._register(first_seen, item)
            return Decision(
                item.index, Verdict.INCLUDE, "below_reference_threshold"
            )

        # Supersession before duplication: a stale value that also happens
        # to repeat an old read must point forward at the truth, not
        # sideways at another stale copy.
        if item.tool_name and item.tool_arguments is not None:
            latest = latest_by_identity.get(
                (item.tool_name, _tool_identity_arguments(item.tool_arguments))
            )
            if (
                latest is not None
                and latest.index > item.index
                and latest.content != content
                and latest.tool_call_id
            ):
                replacement = _stale_marker(latest.tool_call_id)
                if len(replacement) < item.chars:
                    return Decision(
                        item.index,
                        Verdict.ELIDE_STALE,
                        f"superseded_by:{latest.tool_call_id}",
                        replacement=replacement,
                        chars_saved=item.chars - len(replacement),
                    )
                return Decision(
                    item.index, Verdict.INCLUDE, "replacement_not_smaller"
                )

        earlier = first_seen.get(content)
        if earlier is not None and earlier.tool_call_id:
            replacement = _reuse_marker(earlier.tool_call_id)
            if len(replacement) < item.chars:
                return Decision(
                    item.index,
                    Verdict.REFERENCE,
                    f"duplicate_of:{earlier.tool_call_id}",
                    replacement=replacement,
                    chars_saved=item.chars - len(replacement),
                )
            return Decision(
                item.index, Verdict.INCLUDE, "replacement_not_smaller"
            )

        self._register(first_seen, item)
        return Decision(item.index, Verdict.INCLUDE, "first_occurrence")

    @staticmethod
    def _register(first_seen: Dict[str, ContextItem], item: ContextItem) -> None:
        if isinstance(item.content, str) and item.content not in first_seen:
            first_seen[item.content] = item


__all__ = [
    "REFERENCE_THRESHOLD_CHARS",
    "SUPERSEDED_CALL_THRESHOLD_CHARS",
    "CallPayload",
    "CallPayloadStats",
    "CompilationStats",
    "CompiledCallPayloads",
    "CompiledContext",
    "ContextCompiler",
    "ContextItem",
    "Decision",
    "Verdict",
]
