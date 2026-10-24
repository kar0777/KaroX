"""The durable requirement ledger.

A requirement is what the user asked for, recorded as structured state the
runtime can check -- not as the model's restatement of it, and not as a chat
turn that compaction may drop. The ledger is admitted when a task is accepted
and when a continuation adds a user turn, and it lives on the session record,
so it survives compaction, fallback, reload and retry by construction. The
original user turns themselves are preserved untouched beside the ledger (the
session keeps every prior task and every continuation message), so admission
never destroys the only copy of the user's words.

Admission is lossless: the lines around a numbered list (a preamble like
"No network.", a tail like "Never upload data.") and a numbered item's
indented continuation lines are requirements the same as the items are.
Numbered lines inside a fenced code block or a block quote are data the user
was quoting, not orders, and are never admitted.

Runtime output never becomes a requirement: tool results, visual-review
prompts, and continuation bookkeeping do not arrive through the two admission
points, and a model answer has no path that writes here.

IDs are immutable and admitted in order (R1, R2, ...). A requirement is never
renumbered or rewritten. A continuation appends new IDs; an EXPLICIT,
unambiguous correction ("замени требование про красный фон на синий фон")
supersedes the old entry -- it stays in the ledger with its ID and history but
stops being an active requirement. An ambiguous correction never silently
drops anything.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

#: A numbered item in the user's task: "1. Do X", "2) Do Y", indented or not.
#: Bullets are deliberately not requirements -- prose bullets describe, numbered
#: items commit.
_NUMBERED_ITEM = re.compile(r"^[ \t]*(\d+)[.)][ \t]+(.+)$")

#: A fenced code block opener. Numbered lines inside a fence are quoted data.
_FENCE_OPEN = re.compile(r"^[ \t]*(`{3,}|~{3,})")

#: A replacement instruction: verb + "требование/пункт" + reference + "на/— new".
_REPLACE = re.compile(
    r"^\s*(?:замени(?:ть|те)?|замена|вместо)\s+(?:требова\w+|пункт\w*)\s*"
    r"(?:про\s+|о\s+|об\s+)?(?P<ref>.+?)\s*(?:—|--|->|на)\s+(?P<new>.+?)\s*$",
    re.IGNORECASE,
)
_REPLACE_EN = re.compile(
    r"^\s*replace\s+(?:the\s+)?requirement\s+(?:about\s+|that\s+|for\s+)?"
    r"(?P<ref>.+?)\s+(?:with|by)\s+(?P<new>.+?)\s*$",
    re.IGNORECASE,
)

#: A withdrawal instruction: verb + optional "требование" keyword + reference.
#: The keyword is optional: "убери красный" is as much a correction as
#: "убери требование про красный фон" -- what makes it a correction is that
#: its reference actually matches a live requirement.
_WITHDRAW = re.compile(
    r"^\s*(?:отмен\w+|убери(?:те)?|удали(?:те)?|withdraw|remove|drop)\s+"
    r"(?:(?:требова\w+|пункт\w*|the\s+requirement)\s*)?"
    r"(?:(?:про|о|об|about|that|for)\s+)?(?P<ref>.+?)\s*$",
    re.IGNORECASE,
)

#: Small function words that must not drive a stem match between a correction
#: and the requirement it references.
_STOP_TOKENS = frozenset({"про", "the", "and", "для", "это", "that", "with"})

#: The visual review answers this for a requirement the frame cannot show: it
#: stays covered by a line without being graded as if the frame had shown it.
NOT_VISIBLE = "NOT VISIBLE IN FRAME"

STATUS_ACTIVE = "active"
STATUS_SUPERSEDED = "superseded"
STATUS_WITHDRAWN = "withdrawn"

# A repository file becomes requirement authority only when the user's task
# explicitly delegates work to it. Merely mentioning README.md must never turn
# arbitrary repository prose into instructions.
_DELEGATED_SOURCE = re.compile(
    r"(?:\b(?:read|follow|use|implement|according\s+to|requirements?\s+in)\b"
    r"|(?:прочита\w*|следу\w*|выполн\w*|реализ\w*|согласно|требован\w*\s+в))",
    re.IGNORECASE,
)
_SOURCE_FILE = re.compile(
    r"(?:`(?P<tick>[A-Za-z0-9_.][A-Za-z0-9_./\\ -]*\.(?:md|txt|rst|adoc))`"
    r"|(?P<plain>[A-Za-z0-9_.][A-Za-z0-9_./\\-]*\.(?:md|txt|rst|adoc)))",
    re.IGNORECASE,
)
_MAX_DELEGATED_SOURCE_BYTES = 512 * 1024
# Exact delegated specs up to this total size may ride in the first model
# request. Larger sources stay file-backed and are read normally; never truncate
# a spec and pretend the model saw all of it.
_MAX_DELEGATED_PREFETCH_BYTES = 96 * 1024


def canonical(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _is_active(entry: Dict[str, Any]) -> bool:
    return str(entry.get("status", STATUS_ACTIVE)) == STATUS_ACTIVE


def _active_entries(entries: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [entry for entry in entries if _is_active(entry)]


def _significant_tokens(text: str) -> set:
    tokens = set()
    for word in re.findall(r"[0-9a-zа-яё]+", text.lower()):
        if len(word) < 3 or word in _STOP_TOKENS:
            continue
        tokens.add(word[: min(len(word), 5)])
    return tokens


def _references_entry(reference: str, text: str) -> bool:
    """Whether a correction phrase refers to a requirement's text.

    Matching is token-prefix stemming in both directions, so inflected Russian
    ("красного фона") still finds "фон красный" without a real stemmer, and a
    match requires every significant token of the poorer side to appear.
    """

    reference_tokens = _significant_tokens(reference)
    text_tokens = _significant_tokens(text)
    if not reference_tokens or not text_tokens:
        return False
    return reference_tokens <= text_tokens or text_tokens <= reference_tokens


def _code_free_lines(task: str) -> List[str]:
    """The task's lines with quoted material removed.

    Excluded, because a numbered line there is data the user was quoting and
    never an order: lines inside ``` or ~~~ fences (a fence closes with a run
    of the same character at least as long as its opener, so a short close
    cannot end a long fence), block-quoted lines, and numbered lines indented
    four spaces or more -- an indented code block in the pasted source.
    Shallower indents stay list nesting, and an unnumbered deep indent can
    still continue the item above it.
    """

    lines: List[str] = []
    fence: Optional[str] = None
    fence_len = 0
    for line in task.splitlines():
        stripped = line.strip()
        opener = _FENCE_OPEN.match(stripped)
        if fence is None:
            if opener is not None:
                fence = opener.group(1)[:1]
                fence_len = len(opener.group(1))
                continue
        else:
            if len(stripped) >= fence_len and set(stripped) == {fence}:
                fence = None
            continue
        item = _NUMBERED_ITEM.match(line)
        if item is not None and line[:1] in (" ", "\t") and len(line) - len(line.lstrip()) >= 4:
            continue
        if stripped.startswith(">"):
            continue
        lines.append(line)
    return lines


def requirement_texts(task: str) -> List[str]:
    """The user's requirements, in the words the user wrote -- losslessly.

    With at least two numbered items the list is structured: each item is a
    requirement, an indented line following an item continues that item, and
    the non-empty prose before the first item and after the last one are
    requirements in their own right (contiguous prose lines form one). A
    single numbered item with no other content loses just its number; a task
    that numbers nothing is one requirement: itself.
    """

    blocks = _code_free_lines(task)
    segments: List[Tuple[str, List[str]]] = []  # ("item"|"prose", lines)
    current_prose: List[str] = []

    def close_prose() -> None:
        if current_prose:
            segments.append(("prose", list(current_prose)))
            current_prose.clear()

    for line in blocks:
        match = _NUMBERED_ITEM.match(line)
        if match is not None:
            close_prose()
            segments.append(("item", [match.group(2)]))
            continue
        if segments and segments[-1][0] == "item" and line[:1] in (" ", "\t"):
            # An indented line belongs to the requirement above it.
            segments[-1][1].append(line.strip())
            continue
        if line.strip():
            current_prose.append(line.strip())
            continue
        close_prose()
    close_prose()

    if segments:
        # Document order: the user's preamble precedes the items that follow
        # it, and IDs are admitted in the order the user wrote them.
        texts = [canonical(" ".join(lines)) for _, lines in segments]
        return [text for text in texts if text]
    whole = [canonical(line) for line in blocks if canonical(line)]
    return [" ".join(whole)] if whole else []


def delegated_source_paths(task: str) -> List[str]:
    """Return repository-relative spec files the user explicitly delegated to.

    This is an authority boundary, not generic file-mention extraction. A file
    is eligible only when it appears on a code-free task line that also contains
    an explicit delegation verb ("Read TASK.md", "follow spec.md", ...).
    Absolute paths and traversal are rejected here; the repository boundary is
    checked again when the file is opened.
    """

    found: List[str] = []
    for line in _code_free_lines(task):
        if not _DELEGATED_SOURCE.search(line):
            continue
        for match in _SOURCE_FILE.finditer(line):
            raw = (match.group("tick") or match.group("plain") or "").strip()
            raw = raw.replace("\\", "/")
            path = Path(raw)
            if not raw or path.is_absolute() or ".." in path.parts:
                continue
            normalized = path.as_posix()
            if normalized not in found:
                found.append(normalized)
    return found


def delegated_source_context(task: str, repository: Path) -> str:
    """Exact small delegated specs for the first model request.

    This removes a pure orchestration round (model -> read TASK.md -> model)
    without changing authority: only paths admitted by delegated_source_paths()
    can appear here. A source is included whole or not at all.
    """

    root = repository.expanduser().resolve()
    remaining = _MAX_DELEGATED_PREFETCH_BYTES
    sections: List[str] = []
    for relative in delegated_source_paths(task):
        try:
            candidate = (root / relative).resolve(strict=True)
            candidate.relative_to(root)
            if not candidate.is_file():
                continue
            size = candidate.stat().st_size
            if size <= 0 or size > remaining or size > _MAX_DELEGATED_SOURCE_BYTES:
                continue
            payload = candidate.read_bytes()
            text = payload.decode("utf-8")
        except (OSError, UnicodeError, ValueError):
            continue
        digest = hashlib.sha256(payload).hexdigest()
        sections.append(
            "Runtime-prefetched delegated source "
            f"{relative} (sha256={digest}; exact full contents):\n"
            f"--- BEGIN {relative} ---\n{text}\n--- END {relative} ---"
        )
        remaining -= size
    if not sections:
        return ""
    return (
        "\n\nThe runtime already read the explicitly delegated source file(s) below. "
        "Use these exact contents directly; do not spend a tool call rereading a "
        "listed path unless you have evidence the file changed.\n\n"
        + "\n\n".join(sections)
    )


def seed_with_delegated_sources(task: str, repository: Path) -> List[Dict[str, Any]]:
    """Seed from the user task plus explicitly delegated repository specs."""

    entries = seed(task)
    root = repository.expanduser().resolve()
    delegated = delegated_source_paths(task)
    for relative in delegated:
        # The wrapper instruction ("Read TASK.md and implement it") stays in the
        # durable contract, but it is not itself a screenshot requirement. Mark
        # it explicitly so visual review can grade the delegated requirements
        # rather than getting stuck on a meta-instruction no frame can show.
        for entry in entries:
            if (
                entry.get("source") == "task"
                and relative.lower() in str(entry.get("text", "")).lower()
            ):
                entry["delegates_source"] = relative
        try:
            candidate = (root / relative).resolve(strict=True)
            candidate.relative_to(root)
            if (
                not candidate.is_file()
                or candidate.stat().st_size > _MAX_DELEGATED_SOURCE_BYTES
            ):
                continue
            payload = candidate.read_bytes()
            text = payload.decode("utf-8")
        except (OSError, UnicodeError, ValueError):
            continue
        admitted = admit(
            entries,
            requirement_texts(text),
            source=f"file:{relative}",
        )
        digest = hashlib.sha256(payload).hexdigest()
        for entry in admitted:
            entry["source_path"] = relative
            entry["source_sha256"] = digest
    return entries


def ledger_digest(entries: Sequence[Dict[str, Any]]) -> str:
    """A stable digest of the ACTIVE admitted IDs and texts.

    A visual grade is made against one version of the ledger; this digest is
    what binds the grade to that version, so a requirement added -- or a
    correction that retired one -- after the grade invalidates it instead of
    inheriting it. Superseded entries keep their history but bind no grade.
    """

    payload = "\n".join(
        f"{entry.get('id', '')} {entry.get('text', '')}"
        for entry in _active_entries(entries)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def expected_ids(entries: Sequence[Dict[str, Any]]) -> List[str]:
    return [
        str(entry.get("id", ""))
        for entry in _active_entries(entries)
        if entry.get("id")
    ]


def admit(
    entries: List[Dict[str, Any]],
    texts: Iterable[str],
    *,
    source: str,
    added_at: float = 0.0,
) -> List[Dict[str, Any]]:
    """Append new requirements with the next free IDs; duplicates are ignored.

    Returns the entries newly admitted (possibly none), while mutating
    ``entries`` in place. The existing entries are never touched: a requirement
    the user already asked for keeps its ID and its text. Deduplication reads
    the ACTIVE set only -- a text the user withdrew (or superseded) and then
    explicitly asks for again is a live ask, and is re-admitted under a new
    ID instead of being silently swallowed by its own history. New entries
    are active; superseding is a separate, explicit act.
    """

    known = {
        str(entry.get("text", ""))
        for entry in entries
        if _is_active(entry)
    }
    next_id = 1 + max(
        (int(str(entry.get("id", "")).removeprefix("R") or 0) for entry in entries),
        default=0,
    )
    admitted: List[Dict[str, Any]] = []
    for text in texts:
        text = canonical(text)
        if not text or text in known:
            continue
        entry = {
            "id": f"R{next_id}",
            "text": text,
            "source": source,
            "added_at": float(added_at or time.time()),
            "status": STATUS_ACTIVE,
        }
        entries.append(entry)
        known.add(text)
        admitted.append(entry)
        next_id += 1
    return admitted


def _supersede(
    entry: Dict[str, Any],
    *,
    superseded_by: Optional[str],
    at: float,
) -> None:
    entry["status"] = STATUS_SUPERSEDED if superseded_by else STATUS_WITHDRAWN
    entry["superseded_by"] = superseded_by
    entry["superseded_at"] = float(at or time.time())


def _apply_instruction(
    entries: List[Dict[str, Any]],
    instruction: str,
    *,
    source: str,
    added_at: float,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]], bool]:
    """Apply one correction instruction.

    Returns (admitted, superseded, ambiguous, admit_line_instead). A verb with
    NO matching requirement is not a correction at all -- "убери мусор" asks
    for new work -- so the line is admitted as a requirement rather than
    swallowed. An ambiguous reference (>1 matches) neither supersedes nor
    admits: the instruction is reported and nothing is dropped or guessed.
    """

    match = _REPLACE.match(instruction) or _REPLACE_EN.match(instruction)
    superseded: List[Dict[str, Any]] = []
    ambiguous: List[Dict[str, Any]] = []
    if match is not None:
        reference, replacement = match.group("ref"), match.group("new")
        targets = [
            entry
            for entry in _active_entries(entries)
            if _references_entry(reference, str(entry.get("text", "")))
        ]
        if len(targets) > 1:
            ambiguous.append(
                {
                    "reason": "ambiguous_reference",
                    "reference": canonical(reference),
                    "matches": len(targets),
                }
            )
            return [], [], ambiguous, False
        if not targets:
            # Nothing to replace: the line is a new ask in instruction
            # clothing, and dropping it would lose the user's words.
            return [], [], [], True
        new_entries = admit(entries, [replacement], source=source, added_at=added_at)
        _supersede(targets[0], superseded_by=new_entries[0]["id"] if new_entries else None, at=added_at)
        superseded.append(targets[0])
        return new_entries, superseded, [], False

    match = _WITHDRAW.match(instruction)
    if match is not None:
        reference = match.group("ref")
        targets = [
            entry
            for entry in _active_entries(entries)
            if _references_entry(reference, str(entry.get("text", "")))
        ]
        if len(targets) > 1:
            ambiguous.append(
                {
                    "reason": "ambiguous_reference",
                    "reference": canonical(reference),
                    "matches": len(targets),
                }
            )
            return [], [], ambiguous, False
        if not targets:
            return [], [], [], True
        _supersede(targets[0], superseded_by=None, at=added_at)
        superseded.append(targets[0])
        return [], superseded, [], False

    # An instruction-shaped verb with nothing to act on is neither a
    # correction nor a standing requirement; it is ignored rather than
    # admitted, and reported so the caller can see it was seen.
    return [], [], [{"reason": "unrecognized_instruction", "reference": canonical(instruction), "matches": 0}], False


def apply_continuation(
    entries: List[Dict[str, Any]],
    task_text: str,
    *,
    source: str = "continuation",
    added_at: float = 0.0,
) -> Dict[str, Any]:
    """Admit a continuation's new requirements and apply explicit corrections.

    Lines that read as a correction instruction ("замени требование про красный
    фон на синий фон", "убери требование про воду") are never admitted as
    requirements themselves; they act on the active set instead, and only when
    the reference matches exactly one active requirement. An instruction verb
    with no matching requirement is a new ask and IS admitted; an ambiguous
    one admits nothing and drops nothing. Everything else goes through the
    normal lossless admission.
    """

    instruction_lines: List[str] = []
    plain_lines: List[str] = []
    # Instruction detection runs on the CODE-FREE lines only: a correction
    # quoted inside a fenced example or a block quote is the user showing the
    # model a message shape, not the user correcting the ledger. Leaving this
    # unfiltered let a quoted "замени требование ..." in an example fence
    # mutate the active set.
    for line in _code_free_lines(task_text):
        if (
            _REPLACE.match(line)
            or _REPLACE_EN.match(line)
            or _WITHDRAW.match(line)
        ):
            instruction_lines.append(line)
        else:
            plain_lines.append(line)

    admitted = admit(
        entries, requirement_texts("\n".join(plain_lines)), source=source, added_at=added_at
    )
    superseded: List[Dict[str, Any]] = []
    ambiguous: List[Dict[str, Any]] = []
    deferred_lines: List[str] = []
    for line in instruction_lines:
        line_admitted, line_superseded, line_ambiguous, admit_instead = _apply_instruction(
            entries, line, source=source, added_at=added_at or time.time()
        )
        admitted.extend(line_admitted)
        superseded.extend(line_superseded)
        ambiguous.extend(line_ambiguous)
        if admit_instead:
            deferred_lines.append(line)
    if deferred_lines:
        admitted.extend(
            admit(entries, deferred_lines, source=source, added_at=added_at)
        )
    return {"admitted": admitted, "superseded": superseded, "ambiguous": ambiguous}


def seed(task: str) -> List[Dict[str, Any]]:
    """The ledger for a task being admitted for the first time."""

    entries: List[Dict[str, Any]] = []
    admit(entries, requirement_texts(task), source="task")
    return entries


def review_listing(entries: Sequence[Dict[str, Any]]) -> str:
    """The ACTIVE requirements as the visual review sees them, one per line."""

    return "\n".join(
        f"{entry['id']}. {entry['text']}" for entry in _active_entries(entries)
    )
