"""Atomic multi-edit engine behind ``repo.edit_file``.

The model asks for several exact replacements in one call; the engine either
applies all of them or none. Every edit is searched in the ORIGINAL text
independently, so the edits compose the way the caller wrote them, not in the
order the model happened to list them. The only fuzziness allowed is a
normalized retry: when an exact match is missing, the search repeats with
CRLF folded to LF and trailing whitespace dropped per line, and a unique
normalized match is applied to the original lines with their own line endings
preserved. A last, equally strict retry tolerates a block quoted with a
uniformly wrong indentation (the most common way a model misquotes code): it
must match exactly one place line for line after a single consistent indent
shift, and the replacement is shifted back by that same amount.

A no-op entry inside a batch that also carries real changes is skipped, not
fatal: it cannot change the file, and failing the whole batch for it only cost
the model a retry. A batch made only of no-ops still fails.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Sequence

__all__ = ["EditOp", "EditFailure", "apply_edits"]

UNIQUE = "unique"
ALL = "all"

_NOT_FOUND = "not_found"
_AMBIGUOUS = "ambiguous"
_NOOP = "noop"
_OVERLAP = "overlap"

_CANDIDATE_LIMIT = 3
_CANDIDATE_MIN_RATIO = 0.6
_FRAGMENT_LIMIT = 120


@dataclass(frozen=True)
class EditOp:
    """One exact replacement. ``occurrence`` is "unique", "all" or a 1-based index."""

    old: str
    new: str
    occurrence: str | int = UNIQUE


@dataclass(frozen=True)
class EditFailure:
    """Why one edit of an ``edits[]`` batch could not be applied."""

    index: int
    reason: str
    hint: str
    candidates: list[tuple[int, str]] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return {
            "index": self.index,
            "reason": self.reason,
            "hint": self.hint,
            "candidates": [
                {"line": line, "fragment": fragment} for line, fragment in self.candidates
            ],
        }


@dataclass(frozen=True)
class _Span:
    """One located occurrence in the original text, in character offsets."""

    start: int
    end: int
    line: int
    # Replacement text already adapted to the file (indent-tolerant matches).
    new_override: str | None = None


def apply_edits(text: str, edits: Sequence[EditOp]) -> tuple[str, list[EditFailure]]:
    """Apply every edit, or none of them.

    Returns the rewritten text and the list of failures. A non-empty failure
    list means the original text is returned unchanged: callers never see a
    half-applied batch.
    """

    failures: list[EditFailure] = []
    spans: dict[int, list[_Span]] = {}
    real = [op for op in edits if op.old != op.new]
    for index, op in enumerate(edits):
        if op.old == op.new and real:
            # Harmless inside a batch with real edits: it cannot change bytes.
            continue
        if op.old == op.new:
            failures.append(
                EditFailure(
                    index=index,
                    reason=_NOOP,
                    hint="old and new are identical; nothing to replace",
                )
            )
            continue
        located, failure = _locate(text, index, op)
        if failure is not None:
            failures.append(failure)
            continue
        spans[index] = located
    failures.extend(_overlap_failures(spans))
    if failures:
        return text, failures
    # Applying from the end keeps every remaining offset valid without a
    # running offset adjustment.
    ordered = sorted(
        ((span, index) for index, group in spans.items() for span in group),
        key=lambda item: item[0].start,
        reverse=True,
    )
    result = text
    for span, index in ordered:
        op = edits[index]
        replacement = (
            span.new_override
            if span.new_override is not None
            else _replacement(text, span, op)
        )
        result = result[: span.start] + replacement + result[span.end :]
    return result, []


def _locate(text: str, index: int, op: EditOp) -> tuple[list[_Span], EditFailure | None]:
    """Find the occurrences one edit names, or say precisely why it could not."""
    if not op.old:
        return [], EditFailure(
            index=index,
            reason=_NOT_FOUND,
            hint="old must not be empty",
        )
    spans = _exact_spans(text, op.old)
    if not spans:
        normalized_start, normalized_end = _normalized_unique_span(text, op.old)
        if normalized_start is None or normalized_end is None:
            shifted = _reindented_unique_span(text, op)
            if shifted is not None:
                return [shifted], None
            return [], _not_found_failure(text, index, op)
        span = _Span(
            start=normalized_start,
            end=normalized_end,
            line=text.count("\n", 0, normalized_start) + 1,
        )
        return [span], None
    if op.occurrence == ALL:
        return spans, None
    if isinstance(op.occurrence, bool) or not isinstance(op.occurrence, int):
        # Not "all" and not an index: treated as "unique".
        if len(spans) == 1:
            return spans, None
        if len(spans) == 0:
            return [], _not_found_failure(text, index, op)
        return [], _ambiguous_failure(text, index, spans)
    wanted = op.occurrence
    if wanted < 1 or wanted > len(spans):
        return [], EditFailure(
            index=index,
            reason=_NOT_FOUND,
            hint=(
                f"requested occurrence {wanted}, but the file has "
                f"{len(spans)} occurrence(s) of the exact string"
            ),
        )
    return [spans[wanted - 1]], None


def _not_found_failure(text: str, index: int, op: EditOp) -> EditFailure:
    hint = (
        "exact string not found; the normalized retry (line endings and "
        "trailing whitespace folded) also has no unique match"
    )
    block = _closest_block(text, op.old)
    if block is not None:
        line, quoted = block
        # The file's own text, so the next call can copy it instead of
        # re-reading the whole file.
        hint += f"; closest block in the file starts at line {line}:\n{quoted}"
    return EditFailure(
        index=index,
        reason=_NOT_FOUND,
        hint=hint,
        candidates=_nearest_lines(text, op.old),
    )


def _ambiguous_failure(text: str, index: int, spans: list[_Span]) -> EditFailure:
    return EditFailure(
        index=index,
        reason=_AMBIGUOUS,
        hint=(
            f"found {len(spans)} occurrences; add surrounding context to make "
            'it unique, or pass occurrence=<n> / "all"'
        ),
        candidates=[(span.line, _fragment(text, span)) for span in spans[:_CANDIDATE_LIMIT]],
    )


def _overlap_failures(spans: dict[int, list[_Span]]) -> list[EditFailure]:
    """Every edit whose range intersects another edit's range fails."""
    failures: list[EditFailure] = []
    indexes = sorted(spans)
    for position, first in enumerate(indexes):
        for second in indexes[position + 1 :]:
            if any(
                span_a.start < span_b.end and span_b.start < span_a.end
                for span_a in spans[first]
                for span_b in spans[second]
            ):
                failures.append(
                    EditFailure(
                        index=first,
                        reason=_OVERLAP,
                        hint=f"range overlaps edit {second}; edits must be disjoint",
                    )
                )
                failures.append(
                    EditFailure(
                        index=second,
                        reason=_OVERLAP,
                        hint=f"range overlaps edit {first}; edits must be disjoint",
                    )
                )
    return failures


def _exact_spans(text: str, old: str) -> list[_Span]:
    spans: list[_Span] = []
    position = text.find(old)
    while position != -1:
        spans.append(
            _Span(
                start=position,
                end=position + len(old),
                line=text.count("\n", 0, position) + 1,
            )
        )
        position = text.find(old, position + len(old))
    return spans


def _normalized_unique_span(text: str, old: str) -> tuple[int | None, int | None]:
    """Locate ``old`` after folding line endings and trailing whitespace.

    Returns the original-text offsets of the match, or ``(None, None)`` when
    the normalized search is missing or ambiguous.
    """

    normalized, spans = _normalize(text)
    wanted, _ = _normalize(old)
    if not wanted:
        return None, None
    first = normalized.find(wanted)
    if first == -1:
        return None, None
    if normalized.find(wanted, first + 1) != -1:
        return None, None
    # Spans hold absolute original offsets, so the match end is the last
    # normalized character's own end, not its start plus a length.
    return spans[first].start, spans[first + len(wanted) - 1].end


def _normalize(text: str) -> tuple[str, list[_Span]]:
    """The folded text plus, per normalized character, its original extent.

    CRLF and bare CR fold to LF, and trailing whitespace is dropped from every
    line, so a block quoted from a CRLF file can be located from an LF copy.
    The spans keep the mapping back to the original bytes: each normalized
    character records where it starts in the original text and how many
    characters it covers there.
    """

    normalized: list[str] = []
    spans: list[_Span] = []
    length = len(text)
    position = 0
    while position < length:
        newline = text.find("\n", position)
        if newline == -1:
            line_end = length
            ending_len = 0
        else:
            line_end = newline + 1
            ending_len = 2 if newline > position and text[newline - 1] == "\r" else 1
        content_end = line_end - ending_len
        content = text[position:content_end].rstrip()
        for offset, character in enumerate(content):
            normalized.append(character)
            spans.append(_Span(start=position + offset, end=position + offset + 1, line=0))
        if ending_len:
            normalized.append("\n")
            ending_start = line_end - ending_len
            spans.append(_Span(start=ending_start, end=line_end, line=0))
        position = line_end
    return "".join(normalized), spans


def _replacement(text: str, span: _Span, op: EditOp) -> str:
    """The new content for one located span, with the original line endings."""
    covered = text[span.start : span.end]
    if "\r\n" in covered:
        ending = "\r\n"
    elif "\n" in covered:
        ending = "\n"
    elif "\r" in covered:
        ending = "\r"
    else:
        # No line ending inside the covered region: a single-line replacement
        # keeps the new content verbatim.
        return op.new
    pieces = [piece[:-1] if piece.endswith("\r") else piece for piece in op.new.split("\n")]
    joined = ending.join(pieces)
    # The join already carries a trailing ending whenever ``new`` ends with a
    # newline; appending again would silently add a blank line. Only a covered
    # region that ended lines while ``new`` does not needs the ending restored.
    if covered.endswith(("\r\n", "\n", "\r")) and not joined.endswith(ending):
        return joined + ending
    return joined


def _fragment(text: str, span: _Span) -> str:
    """A bounded view of the matched region, for failure reports."""
    line_start = text.rfind("\n", 0, span.start) + 1
    line_end = text.find("\n", span.end)
    if line_end == -1:
        line_end = len(text)
    fragment = text[line_start:line_end].strip()
    return fragment[:_FRAGMENT_LIMIT]


def _nearest_lines(text: str, old: str) -> list[tuple[int, str]]:
    """The three lines most similar to the first line of ``old``."""
    first_line = old.split("\n", 1)[0].strip()
    if not first_line:
        return []
    matcher = difflib.SequenceMatcher(None, first_line, "")
    scored: list[tuple[float, int, str]] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped:
            continue
        matcher.set_seq2(stripped)
        ratio = matcher.ratio()
        if ratio >= _CANDIDATE_MIN_RATIO:
            scored.append((ratio, line_number, stripped[:_FRAGMENT_LIMIT]))
    scored.sort(reverse=True)
    return [(line_number, fragment) for _, line_number, fragment in scored[:_CANDIDATE_LIMIT]]


_BLOCK_MIN_RATIO = 0.6
_BLOCK_QUOTE_LIMIT = 800
_BLOCK_MAX_LINES = 40


def _closest_block(text: str, old: str) -> tuple[int, str] | None:
    """The run of file lines most similar to a multi-line old block, verbatim."""
    wanted = [line.rstrip() for line in old.strip("\n").split("\n")]
    if len(wanted) < 2 or len(wanted) > _BLOCK_MAX_LINES:
        return None
    lines = text.splitlines()
    size = len(wanted)
    if len(lines) < size:
        return None
    first = wanted[0].strip()
    target = "\n".join(wanted)
    matcher = difflib.SequenceMatcher(None, "", target, autojunk=False)
    best: tuple[float, int] | None = None
    for start in range(0, len(lines) - size + 1):
        # Cheap prefilter: the window must resemble the first quoted line.
        if first and difflib.SequenceMatcher(
            None, first, lines[start].strip(), autojunk=False
        ).quick_ratio() < 0.5:
            continue
        window = "\n".join(line.rstrip() for line in lines[start : start + size])
        matcher.set_seq1(window)
        if matcher.real_quick_ratio() < _BLOCK_MIN_RATIO:
            continue
        ratio = matcher.ratio()
        if best is None or ratio > best[0]:
            best = (ratio, start)
    if best is None or best[0] < _BLOCK_MIN_RATIO:
        return None
    start = best[1]
    quoted = "\n".join(lines[start : start + size])
    return start + 1, quoted[:_BLOCK_QUOTE_LIMIT]


def _leading(line: str) -> str:
    return line[: len(line) - len(line.lstrip(" \t"))]


def _reindented_unique_span(text: str, op: EditOp) -> _Span | None:
    """Match the old block under one consistent indentation shift, uniquely.

    Every non-blank quoted line must equal its file line after removing or
    adding the same leading-whitespace prefix, blank lines must stay blank,
    and exactly one place in the file may match. The replacement gets the
    same shift. Anything less certain is left to the not-found report.
    """
    old, new = op.old, op.new
    if old.endswith("\n") and new.endswith("\n"):
        old, new = old[:-1], new[:-1]
    elif old.endswith("\n") or "\n" not in old.strip("\n"):
        return None
    old_lines = [line.rstrip() for line in old.replace("\r\n", "\n").split("\n")]
    if not old_lines[0].strip() or not old_lines[-1].strip():
        return None
    file_lines = text.splitlines(keepends=True)
    contents = [line.rstrip("\r\n").rstrip() for line in file_lines]
    size = len(old_lines)
    matches: list[tuple[int, tuple[str, str]]] = []
    for start in range(0, len(contents) - size + 1):
        shift: tuple[str, str] | None = None
        for offset, quoted in enumerate(old_lines):
            actual = contents[start + offset]
            if not quoted.strip() or not actual.strip():
                if quoted.strip() or actual.strip():
                    shift = None
                    break
                continue
            if quoted.lstrip(" \t") != actual.lstrip(" \t"):
                shift = None
                break
            have, want = _leading(actual), _leading(quoted)
            if have.endswith(want):
                candidate = ("+", have[: len(have) - len(want)])
            elif want.endswith(have):
                candidate = ("-", want[: len(want) - len(have)])
            else:
                shift = None
                break
            if shift is None:
                shift = candidate
            elif shift != candidate:
                shift = None
                break
        else:
            if shift is not None and shift[1]:
                matches.append((start, shift))
        if len(matches) > 1:
            return None
    if len(matches) != 1:
        return None
    start, (direction, prefix) = matches[0]
    shifted: list[str] = []
    for line in new.replace("\r\n", "\n").split("\n"):
        if not line.strip():
            shifted.append("")
        elif direction == "+":
            shifted.append(prefix + line)
        elif line.startswith(prefix):
            shifted.append(line[len(prefix) :])
        else:
            return None
    offset_start = sum(len(line) for line in file_lines[:start])
    last = file_lines[start + size - 1]
    ending = "\r\n" if last.endswith("\r\n") else "\n"
    offset_end = (
        sum(len(line) for line in file_lines[: start + size])
        - (len(last) - len(last.rstrip("\r\n")))
    )
    return _Span(
        start=offset_start,
        end=offset_end,
        line=start + 1,
        new_override=ending.join(shifted),
    )
