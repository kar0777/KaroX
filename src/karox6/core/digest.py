"""One-line digests of agent events, shared by the chat UI and the MCP status log."""

from __future__ import annotations

import json

from karox6.core import events as ev


def brief(arguments: str, limit: int = 120) -> str:
    """The telling part of a tool call's JSON arguments (command, path, pattern...)."""
    try:
        value = json.loads(arguments)
    except json.JSONDecodeError:
        value = arguments
    if isinstance(value, dict):
        for key in ("command", "file_path", "path", "pattern", "job_id", "description"):
            if key in value:
                text = str(value[key])
                break
        else:
            text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    return clip(text, limit)


def outcome(event: ev.ToolFinished, limit: int = 140) -> str:
    """One line about a finished tool: a shell's last line, a read's size, else line one."""
    lines = [ln.strip() for ln in event.output.strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    name = event.name.rsplit("/", 1)[-1]  # "agent-1/read" -> "read"
    if name == "read" and not event.is_error:
        return f"{len(lines)} lines"
    pick = lines[-1] if name in ("bash", "shell", "powershell", "pwsh", "job_output") else lines[0]
    return pick[:limit]


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


def event_line(event: ev.Event, started: dict[str, str]) -> str | None:
    """A log line for an MCP client, or None for events not worth a line.

    ``started`` maps call ids to argument digests (filled from tool_started events), so
    the finished line can say what ran.
    """
    if isinstance(event, ev.ToolStarted):
        started[event.call_id] = brief(event.arguments, 100)
        return None
    if isinstance(event, ev.ToolFinished):
        mark = "✗" if event.is_error else "✓"
        what = started.pop(event.call_id, "")
        result = outcome(event, 160)
        took = f" ({event.duration_ms / 1000:.1f}s)" if event.duration_ms >= 1000 else ""
        return f"{mark} {event.name} {what}".rstrip() + (f" → {result}" if result else "") + took
    if isinstance(event, ev.AssistantMessage):
        return f"says: {clip(event.text, 400)}" if event.text.strip() else None
    if isinstance(event, ev.ApprovalRequested):
        return f"approval needed ({event.request_id}): {clip(event.summary, 200)} — {event.reason}"
    if isinstance(event, ev.Notice):
        return f"note to the agent: {event.kind}"
    if isinstance(event, ev.Compacted):
        return f"context compacted: {event.tokens_before} → {event.tokens_after} tokens"
    if isinstance(event, ev.Retry):
        return f"model retry {event.attempt} in {event.delay_s:.0f}s: {clip(event.error, 120)}"
    if isinstance(event, ev.ErrorEvent):
        return f"error: {clip(event.message, 300)}"
    if isinstance(event, ev.RunFinished):
        return f"finished: {event.status}" + (f" — {event.reason}" if event.reason else "")
    if isinstance(event, ev.RunStarted):
        return f"started (session {event.session_id})"
    return None
