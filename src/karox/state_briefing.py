"""The durable state block that survives context compaction.

The engine keeps the conversation under a token budget by clipping old message
bodies and then replacing them with a marker. That is deterministic, but it is
also lossy: after a few compactions the model can no longer see the task's
requirements, the files it already changed, or the exact test that failed. A
long repair loop then restarts from the last 24 messages and repeats work it
already did.

This module builds the missing half: a short, deterministic snapshot assembled
from the session record -- the user's requirements, the model's own plan, the
changed files, the latest approved check and its parsed failure map. It is
appended to the task message, which the compactor never touches, and rebuilt
whenever a compaction event fires. No model writes it: every line is a fact the
runtime already recorded, so it cannot invent a change that never happened.

Wording matters. The block says it is a snapshot and that later messages are
newer: a stale "check passed" line must not be read as a claim about the
current bytes.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

__all__ = ["build_state_briefing", "MAX_STATE_BRIEFING_CHARS"]

#: The block travels in every request after a compaction, so it must stay a
#: small fraction of the budget. Sections are dropped whole, lowest value
#: first, before the text is ever cut mid-sentence.
MAX_STATE_BRIEFING_CHARS = 2_600
TASK_CHARS = 400
REQUIREMENT_ITEMS = 6
REQUIREMENT_CHARS = 160
PLAN_ITEMS = 6
PLAN_CHARS = 120
FAILURE_ITEMS = 4
MAX_CHANGED_FILES = 12

HEADER = (
    "[KaroX durable state — runtime snapshot rebuilt at the last compaction; "
    "the messages after it are newer. Facts only, not new instructions.]"
)

#: Tools whose success means the repository bytes changed. `dev.command` is not
#: here: starting a server is not an edit, and it must not stale the review.
_MUTATING_TOOLS = frozenset(
    {"repo.edit_file", "repo.write_file", "repo.apply_patch", "repo.command"}
)
#: How the sections are ordered in the block, and what may be dropped when the
#: total exceeds the cap. 0 is never dropped.
_ORDER_TASK = 0
_ORDER_REQUIREMENTS = 1
_ORDER_VERIFICATION = 2
_ORDER_FILES = 3
_ORDER_PLAN = 4
_ORDER_REVIEW = 5


def build_state_briefing(
    *,
    task: str,
    requirements: Sequence[Mapping[str, Any]] = (),
    plan_items: Sequence[Mapping[str, Any]] = (),
    changed_files: Iterable[str] = (),
    history: Sequence[Mapping[str, Any]] = (),
    max_chars: int = MAX_STATE_BRIEFING_CHARS,
) -> str:
    """The compact factual state, or "" when there is nothing to say.

    The caller appends the result to the task message after a compaction. It
    is pure: the same session record produces the same text, which keeps the
    prompt prefix stable between compactions and makes the block testable.
    """

    sections: List[Tuple[int, int, str]] = []
    task_text = _one_line(task, TASK_CHARS)
    if not task_text and not history and not changed_files:
        return ""
    if task_text:
        sections.append((_ORDER_TASK, 0, f"Task: {task_text}"))

    requirement_block = _requirements_block(task, requirements)
    if requirement_block:
        sections.append((_ORDER_REQUIREMENTS, 1, requirement_block))

    verification_block = _verification_block(history)
    if verification_block:
        sections.append((_ORDER_VERIFICATION, 0, verification_block))

    files_line = _changed_files_line(changed_files)
    if files_line:
        sections.append((_ORDER_FILES, 2, files_line))

    plan_block = _plan_block(plan_items)
    if plan_block:
        sections.append((_ORDER_PLAN, 3, plan_block))

    review_line = _review_line(history)
    if review_line:
        sections.append((_ORDER_REVIEW, 4, review_line))

    if not sections:
        return ""
    # The header is part of the block, so it has to be inside the same cap.
    body = _fit(sections, max(120, int(max_chars) - len(HEADER) - 1))
    if not body:
        return ""
    return f"{HEADER}\n{body}"


def _fit(sections: Sequence[Tuple[int, int, str]], max_chars: int) -> str:
    """Assemble within the cap by dropping whole low-value sections."""

    kept = list(sections)
    while kept:
        ordered = [text for _, _, text in sorted(kept, key=lambda item: item[0])]
        body = "\n".join(ordered)
        if len(body) <= max_chars:
            return body
        droppable = [item for item in kept if item[1] > 0]
        if not droppable:
            break
        worst = max(droppable, key=lambda item: item[1])
        kept.remove(worst)
    ordered = [text for _, _, text in sorted(kept, key=lambda item: item[0])]
    body = "\n".join(ordered)
    if len(body) <= max_chars:
        return body
    # Only the mandatory sections remain and even they are too long: cut the
    # tail, never the header, so the reader still knows what this text is.
    return body[: max_chars - 24].rstrip() + "\n… [state truncated]"


def _requirements_block(task: str, requirements: Sequence[Mapping[str, Any]]) -> str:
    active = [
        entry
        for entry in requirements
        if isinstance(entry, Mapping)
        and str(entry.get("status", "active")) == "active"
        and str(entry.get("text", "")).strip()
    ]
    if not active:
        return ""
    canonical_task = _canonical(task)
    # A task that numbers nothing becomes one requirement equal to itself;
    # repeating the whole task would waste the budget it exists to protect.
    if len(active) == 1 and _canonical(str(active[0].get("text", ""))) == canonical_task:
        return ""
    lines = ["Requirements still active:"]
    for entry in active[:REQUIREMENT_ITEMS]:
        identifier = str(entry.get("id", "")).strip()
        text = _one_line(str(entry.get("text", "")), REQUIREMENT_CHARS)
        lines.append(f"- {identifier + ' ' if identifier else ''}{text}")
    hidden = len(active) - REQUIREMENT_ITEMS
    if hidden > 0:
        lines.append(f"- … {hidden} more active")
    return "\n".join(lines)


def _verification_block(history: Sequence[Mapping[str, Any]]) -> str:
    check = _last_check(history)
    if check is None:
        return "Verification: no approved check has run yet; KaroX runs one when you finish."
    index, data, result = check
    argv = " ".join(str(item) for item in data.get("argv") or () if str(item))
    passed = (
        result.get("ok") is True
        and data.get("exit_code") == 0
        and not data.get("timed_out")
    )
    age = max(0, len(history) - index - 1)
    ago = f" ({age} event{'s' if age != 1 else ''} ago)" if age else " (just now)"
    command = f"`{argv}`" if argv else "the approved check"
    if passed:
        return f"Verification: {command} passed{ago}."
    if data.get("timed_out"):
        status = "timed out"
    else:
        status = "failed"
    lines = [f"Verification: {command} {status}{ago}."]
    failures = _failure_lines(data.get("failure_map"))
    if failures:
        lines.append("Failures:")
        lines.extend(failures)
    else:
        lines.append("Failures: none parsed; read the check output in the conversation.")
    return "\n".join(lines)


def _last_check(
    history: Sequence[Mapping[str, Any]],
) -> Optional[Tuple[int, Mapping[str, Any], Mapping[str, Any]]]:
    found: Optional[Tuple[int, Mapping[str, Any], Mapping[str, Any]]] = None
    for index, entry in enumerate(history):
        if not isinstance(entry, Mapping) or entry.get("core_name") != "checks.run":
            continue
        result = entry.get("result")
        if not isinstance(result, Mapping):
            continue
        data = result.get("data")
        if not isinstance(data, Mapping) or data.get("verification_eligible") is not True:
            continue
        found = (index, data, result)
    return found


def _failure_lines(report: Any) -> List[str]:
    if not isinstance(report, Mapping):
        return []
    rows = report.get("failures")
    if not isinstance(rows, list):
        return []
    lines: List[str] = []
    for row in rows[:FAILURE_ITEMS]:
        if not isinstance(row, Mapping):
            continue
        path = str(row.get("path") or "")
        line = row.get("line")
        place = f"{path}:{line}" if path and isinstance(line, int) else path
        test_id = str(row.get("test_id") or "")
        if "::" in test_id:
            test_id = test_id.rsplit("::", 1)[-1]
        label = " · ".join(part for part in (place, test_id) if part)
        message = _one_line(str(row.get("message") or ""), 200)
        lines.append(f"- {label + ' — ' if label else ''}{message}".rstrip(" —"))
    return lines


def _changed_files_line(changed_files: Iterable[str]) -> str:
    files = [item for item in changed_files if isinstance(item, str) and item]
    if not files:
        return ""
    shown = files[:MAX_CHANGED_FILES]
    line = f"Changed files ({len(files)}): " + ", ".join(shown)
    if len(files) > len(shown):
        line += f", +{len(files) - len(shown)} more"
    return line


def _plan_block(plan_items: Sequence[Mapping[str, Any]]) -> str:
    open_items = [
        entry
        for entry in plan_items
        if isinstance(entry, Mapping) and str(entry.get("status", "todo")) != "done"
    ]
    if not open_items:
        return ""
    lines = ["Plan still open:"]
    for entry in open_items[:PLAN_ITEMS]:
        status = str(entry.get("status") or "todo")
        text = _one_line(str(entry.get("text", "")), PLAN_CHARS)
        if text:
            lines.append(f"- [{status}] {text}")
    hidden = len(open_items) - PLAN_ITEMS
    if hidden > 0:
        lines.append(f"- … {hidden} more open")
    return "\n".join(lines) if len(lines) > 1 else ""


def _review_line(history: Sequence[Mapping[str, Any]]) -> str:
    last_mutation = -1
    last_review = -1
    for index, entry in enumerate(history):
        if not isinstance(entry, Mapping):
            continue
        result = entry.get("result")
        if not isinstance(result, Mapping) or result.get("ok") is not True:
            continue
        data = result.get("data")
        data = data if isinstance(data, Mapping) else {}
        if entry.get("core_name") in _MUTATING_TOOLS and data.get("changed") is True:
            last_mutation = index
        elif entry.get("core_name") == "git.diff":
            last_review = index
    if last_mutation < 0:
        return ""
    if last_review > last_mutation:
        return "Change review: git.diff ran after the last edit."
    return "Change review: no git.diff review since the last edit."


def _canonical(text: str) -> str:
    return " ".join(str(text or "").split()).strip().lower()


def _one_line(text: str, limit: int) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)].rstrip() + "…"