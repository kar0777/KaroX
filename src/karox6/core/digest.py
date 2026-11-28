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
        if meta.get("blocked_rule"):
            return clip(f"not run (rule {meta['blocked_rule']})", limit)
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
    if name == "skill" and meta.get("skill"):
        return f"loaded {meta['skill']}"
    # the first line that says something: not a bare tag or bracket (`<skill ...>`, `{`)
    told = [ln for ln in lines if not _BARE_MARK.fullmatch(ln)]
    return (told or lines)[0][:limit]


_BARE_MARK = re.compile(r"</?[\w-]+(?:\s[^<>]*)?>|[\[\]{}()]+,?")


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


_ERROR_HEAD = re.compile(r"^([a-z_]+)(?: \(HTTP (\d+)\))?: (.*)$", re.S)
_JSON_MESSAGE = re.compile(r'"(?:message|detail|msg)"\s*:\s*"((?:[^"\\]|\\.)*)')
_EXC_REPR = re.compile(r"^[A-Za-z_.]+\((['\"])(.*)\1\)$", re.S)
_NOISE = re.compile(
    r"(?:litellm\.)?[A-Za-z]+Error: |OpenAIException - |\bReceived Model Group=.*$", re.S
)
_HTML_TAG = re.compile(r"<(?:script|style)\b.*?</(?:script|style)>|<[^>]+>", re.S | re.I)
_TITLES = {
    "auth": "the API key was refused",
    "quota": "the account is out of balance or allowance",
    "rate_limit": "rate limit",
    "context_window": "the conversation is larger than the model accepts",
    "timeout": "the model did not answer in time",
    "transport": "cannot reach the model server",
    "server": "the provider failed",
    "bad_request": "the provider rejected the request",
    "empty_response": "the model returned nothing",
    "internal": "a bug in KaroX",
}
_HINTS = {
    "auth": "/key sets a new key; /setup changes the provider",
    "quota": "waiting will not help: top up, or switch with /setup or /model",
    "rate_limit": "the provider kept refusing; try again later, or /model for another model",
    "context_window": "/compact shrinks the conversation; /new starts a fresh one",
    "timeout": "check the connection and send the message again",
    "transport": "check the connection and the server address (/setup)",
    "server": "try again in a moment; /model or /setup to switch",
    "empty_response": "send the message again or rephrase it; /model switches the model",
    "internal": "please report it with the text above",
}


def error_detail(text: str, limit: int = 220) -> str:
    """What a provider's error body says, without its JSON wrapping: the ``message`` of a
    JSON error (also when the body was cut short), the text inside an exception's repr,
    gateway prefixes dropped."""
    body = text.strip()
    found = ""
    if body.startswith("{"):
        try:
            data = json.loads(body)
        except ValueError:
            data = None
        if isinstance(data, dict):
            err = data.get("error", data)
            if isinstance(err, dict):
                found = str(err.get("message") or err.get("detail") or err.get("msg") or "")
            elif isinstance(err, str):
                found = err
            if not found and isinstance(data.get("detail"), str):
                found = data["detail"]
        if not found:
            match = _JSON_MESSAGE.search(body)
            if match:
                try:
                    found = json.loads('"' + match.group(1) + '"')
                except ValueError:
                    found = match.group(1)
    if not found:
        for prefix in ("connection failed: ", "request timed out: "):
            body = body.removeprefix(prefix)
        repr_match = _EXC_REPR.match(body)
        found = repr_match.group(2) if repr_match else body
    if "<" in found and ">" in found:  # an HTML error page of a proxy
        found = _HTML_TAG.sub(" ", found)
    found = _NOISE.sub("", found)
    return clip(found, limit)


def explain_error(message: str) -> tuple[str, str]:
    """(one readable line, what the user can do) for a run's error text, which has the
    form ``code (HTTP status): provider text``; other texts come back unchanged."""
    match = _ERROR_HEAD.match(message.strip())
    if not match or match.group(1) not in _TITLES:
        return clip(message, 300), ""
    code, status, rest = match.group(1), match.group(2), match.group(3)
    detail = error_detail(rest)
    lowered = detail.lower()
    title, hint = _TITLES[code], _HINTS.get(code, "")
    if code == "bad_request":
        if "image" in lowered or "vision" in lowered or "multimodal" in lowered:
            hint = "this model may not accept images; send the message without them"
        elif "model" in lowered and any(
            w in lowered for w in ("not found", "does not exist", "unknown", "invalid", "not exist")
        ):
            title, hint = "the model name was not accepted", "/model NAME sets another model"
        elif status == "404":
            title = "the server has no such endpoint or model"
            hint = "check the model name (/model) and the server address (/setup)"
    if code == "empty_response":
        detail = ""  # the title says it all
    where = f" (HTTP {status})" if status else ""
    line = f"{title}{where}" + (f": {detail}" if detail and detail.lower() != title else "")
    return line, hint
