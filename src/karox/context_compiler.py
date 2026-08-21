"""Typed model-facing context IR: the Context Compiler.

Every retained request item receives an explicit verdict with a recorded
reason before the request leaves this process:

``INCLUDE``
    The item travels to the model unchanged.
``REFERENCE``
    The item's exact bytes already travel earlier in the same request, so a
    lossless marker points at the first occurrence instead of repeating them.
``ELIDE_DUPLICATE``
    Reserved for items whose bytes are dropped entirely because a retained
    copy exists. The current rules always prefer ``REFERENCE`` so the model
    keeps an explicit pointer; nothing emits this verdict yet.
``ELIDE_STALE``
    A newer result for the same tool call identity (same tool, same
    arguments) travels later in the request with different content. The old
    bytes are misleading, so a supersession marker points forward to the
    fresh result.
``ELIDE_IRRELEVANT``
    Reserved for relevance-driven exclusion. No deterministic relevance
    signal exists at this layer yet, so nothing emits this verdict; the
    verdict exists so diagnostics and future rules share one vocabulary.

Quality precedes reduction. An item may only leave the request when it is an
exact duplicate, provably superseded, or losslessly reachable through the
marker that replaces it. Dialogue turns (system, user, assistant) are always
included. The compiler is deterministic: the same input sequence always
produces byte-identical decisions, so prefix caching never loses reuse to
ordering churn.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional, Sequence, Tuple

REFERENCE_THRESHOLD_CHARS = 1_000
"""Below this size a marker saves nothing worth the indirection."""

_REUSE_MARKER = "identical_tool_result_reused"
_STALE_MARKER = "superseded_tool_result"


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
    """The verdict for one item, with the reason on the record."""

    index: int
    verdict: Verdict
    reason: str
    replacement: Optional[str] = None
    chars_saved: int = 0


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


def _reuse_marker(first_call_id: str) -> str:
    return json.dumps(
        {"karox": _REUSE_MARKER, "same_as_tool_call_id": first_call_id},
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

    def compile(self, items: Sequence[ContextItem]) -> CompiledContext:
        latest_by_identity: Dict[Tuple[str, str], ContextItem] = {}
        for item in items:
            if (
                item.role == "tool"
                and isinstance(item.content, str)
                and item.tool_name
                and item.tool_arguments is not None
            ):
                latest_by_identity[(item.tool_name, item.tool_arguments)] = item

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
                (item.tool_name, item.tool_arguments)
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
    "CompilationStats",
    "CompiledContext",
    "ContextCompiler",
    "ContextItem",
    "Decision",
    "Verdict",
]
