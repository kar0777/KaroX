"""One-line digests of agent events, shared by the chat UI and the MCP status log."""

from __future__ import annotations

import json
import re

from karox6.core import events as ev

_SHELL_TOOLS = frozenset({"bash", "shell", "powershell", "pwsh", "job_output"})
_GREP_MATCH_LINE = re.compile(r"^Line \d+:")
_EXIT_MARKER = re.compile(r"^\[exit code:\s*-?\d+")


def brief(arguments: str, limit: int = 120) -> str:
    """The telling part of a tool call's JSON arguments (command, path, pattern...)."""
    try:
        value = json.loads(arguments)
    except json.JSONDecodeError:
        value = arguments
    if isinstance(value, dict):
        if isinstance(value.get("file_paths"), list) and value["file_paths"]:
            text = ", ".join(str(p) for p in value["file_paths"])
        else:
            for key in ("command", "file_path", "path", "pattern", "job_id", "description", "code"):
                if key in value:
                    text = str(value[key])
                    break
            else:
                text = json.dumps(value, ensure_ascii=False)
    else:
        text = str(value)
    return clip(text, limit)


def is_tool_failure(event: ev.ToolFinished) -> bool:
    """True when a tool call errored or a shell command exited non-zero."""
    if event.is_error:
        return True
    code = event.meta.get("exit_code")
    return isinstance(code, int) and code != 0


def outcome(event: ev.ToolFinished, limit: int = 140) -> str:
    """One line about a finished tool: exit code, read/search size, or first/last line."""
    name = event.name.rsplit("/", 1)[-1]  # "agent-1/read" -> "read"
    lines = [ln.strip() for ln in event.output.strip().splitlines() if ln.strip()]
    meta = event.meta
    if name in _SHELL_TOOLS:
        if meta.get("timed_out") and meta.get("job_id"):
            return clip(f"background job {meta['job_id']}", limit)
        content = [ln for ln in lines if not _EXIT_MARKER.match(ln)]
        last = content[-1] if content else ""
        code = meta.get("exit_code")
        if isinstance(code, int):
            summary = f"exit {code} · {last}" if last else f"exit {code}"
            return clip(summary, limit)
        if event.is_error:
            return clip(last or (lines[-1] if lines else ""), limit)
        return last[:limit]
    if not lines:
        return ""
    if event.is_error:
        return lines[0][:limit]
    if name == "read":
        span = meta.get("lines")
        total = meta.get("total_lines")
        if isinstance(span, list) and len(span) == 2 and isinstance(total, int):
            if total == 0:
                return "empty file"
            start, end = span
            if start == 1 and end >= total:
                return f"{total} lines"
            return f"lines {start}-{end} of {total}"
        if "<type>directory</type>" in event.output:
            in_entries = False
            count = 0
            for ln in lines:
                if ln == "<entries>":
                    in_entries = True
                elif ln == "</entries>":
                    break
                elif in_entries and not ln.startswith("("):
                    count += 1
            return f"{count} entries"
        return f"{len(lines)} lines"
    if name == "glob":
        if lines[0] == "No files found.":
            return "0 files"
        count = sum(1 for ln in lines if not ln.startswith("("))
        return f"{count} file" if count == 1 else f"{count} files"
    if name == "grep":
        if lines[0] == "No matches found.":
            return "0 matches"
        matches = sum(1 for ln in lines if _GREP_MATCH_LINE.match(ln))
        if matches:
            return f"{matches} match" if matches == 1 else f"{matches} matches"
        return f"{len(lines)} line" if len(lines) == 1 else f"{len(lines)} lines"
    return lines[0][:limit]


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
        mark = "✗" if is_tool_failure(event) else "✓"
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
