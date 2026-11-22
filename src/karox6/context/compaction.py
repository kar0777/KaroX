"""Context compaction: prune old tool output first, summarize only if still too large.

Shape and texts follow DSH's compaction-basic / tool-result-pruner (MIT, see
THIRD_PARTY_NOTICES.md). KaroX additions: the original task message is kept verbatim,
and the checkpoint lists the files changed so far (from the single change tracker).
"""

from __future__ import annotations

from dataclasses import replace

from karox6.core.messages import IMAGE_TOKENS, Message

PRUNE_THRESHOLD = 8192
PRUNE_HEAD = 4096
PRUNE_TAIL = 1024
PRUNE_MARKER = "\n\n[... tool result middle pruned ...]\n\n"

COMPACTION_INSTRUCTION = """You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context.

Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write "(none)" for an empty section — never drop a section.

## Primary Request and Intent
- [the user's original and evolving goals; quote verbatim where the exact wording matters]

## Key Technical Concepts
- [technologies, frameworks, patterns, and conventions in play]

## Files and Code
- [exact path: why it matters, key changes or snippets]

## Errors and Fixes
- [error: how it was resolved, plus any related user feedback]

## Pending Jobs
- [explicitly requested work not yet completed]

## Current Work
- [precisely what was in progress at this checkpoint]

## Next Step
- [the single next action, directly in line with the most recent request, or "(none)"]

## Critical Context
- [decisions and their rationale, constraints, user preferences, open hypotheses, data needed to continue]

Rules:
- Write concise English engineering prose. Preserve exact file paths, commands, error strings, identifiers, numeric values, function signatures, and syntax fragments.
- Capture user feedback and explicit instructions faithfully, especially corrections.
- Do NOT mention this summarization request or that the context was compacted.
- Output only the checkpoint text: do not call any tool or take any other action.
- If the conversation already contains a <compacted-summary> block, it is a PRIOR checkpoint. Do not copy it forward verbatim: preserve still-true facts, drop stale ones, and merge newer information into a single consolidated summary under the same structure."""  # noqa: E501

CHECKPOINT_PREAMBLE = (
    "This is an automatically generated checkpoint condensing an earlier span of the "
    "conversation to free up context. Treat the captured context as established background and "
    "build on it without restating it. Continue the task directly from the messages that follow, "
    "without acknowledging this checkpoint."
)


def estimate_tokens(messages: list[Message], system: str = "") -> int:
    """Rough token count (chars/3.5): only used to decide when to compact."""
    chars = len(system)
    for m in messages:
        chars += len(m.content) + len(m.reasoning or "") + 16
        chars += int(IMAGE_TOKENS * 3.5) * len(m.images)
        for call in m.tool_calls:
            chars += len(call.arguments) + len(call.name) + 16
    return int(chars / 3.5)


def prune_tool_results(messages: list[Message], keep_last: int = 6) -> int:
    """Cut the middle out of big tool results except the newest ``keep_last`` messages.

    Returns the number of characters removed. Mutates ``messages`` in place.
    """
    removed = 0
    limit = max(len(messages) - keep_last, 0)
    for i in range(limit):
        m = messages[i]
        if m.role != "tool" or len(m.content) <= PRUNE_THRESHOLD:
            continue
        new = m.content[:PRUNE_HEAD] + PRUNE_MARKER + m.content[-PRUNE_TAIL:]
        removed += len(m.content) - len(new)
        messages[i] = replace(m, content=new, meta={**m.meta, "pruned": True})
    return removed


def split_for_summary(
    messages: list[Message], retain_tokens: int
) -> tuple[list[Message], list[Message]]:
    """(head to summarize, tail to keep verbatim). Never splits a tool call from its result."""
    if not messages:
        return [], []
    acc = 0
    start = len(messages)
    for i in range(len(messages) - 1, -1, -1):
        acc += estimate_tokens([messages[i]])
        if acc > retain_tokens:
            break
        start = i
    # The newest message alone over the budget: keep it (and the call it answers) anyway;
    # dropping the step the model is on would leave it with no recent context.
    start = min(start, len(messages) - 1)
    # Move the boundary back while it would orphan tool results.
    while start < len(messages) and messages[start].role == "tool":
        start -= 1
    start = max(start, 1)
    return messages[:start], messages[start:]


def checkpoint_message(summary: str, files_changed: list[str]) -> Message:
    files = "\n".join(f"- {p}" for p in files_changed) or "- (none)"
    text = (
        f"{CHECKPOINT_PREAMBLE}\n\n<compacted-summary>\n{summary.strip()}\n</compacted-summary>\n\n"
        f"Files changed in the workspace so far:\n{files}"
    )
    return Message("user", text, meta={"kind": "compaction"})
