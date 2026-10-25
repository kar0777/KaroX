"""Salvage of a shell command the model wrote into a fence instead of a tool call.

Port 5 of the KaroX task loop. Evidence: jsonargparse-707 v3 -- after 27 real
steps the model answered "Now I understand the bug... Let me implement the fix"
with the patch script in a ```mswea_bash_command fence and no tool call. Three
such responses in a row ended the run with the patch never applied. One
bash-ish fence is unambiguous, so it is executed; two fences, or a fence in
another language, remain a format error.

This lived in the vendored litellm model; the product never uses that
transport (KaroX speaks providers.py), so the helper moved here and the
litellm dependency left the runtime closure with it.
"""

from __future__ import annotations

import re

_FENCED_COMMAND_RE = re.compile(
    r"```[ \t]*(?:mswea_bash_command|bash|sh|shell)[ \t]*\r?\n(.*?)```",
    re.DOTALL,
)
#: Open fence without a closing one: sqlglot-4935 v4h -- the response stops at
#: `</parameter>` mid-command (the provider cuts the model's native tool-call
#: text), finish_reason is still "stop". The command is unambiguous, so the
#: remainder up to the leaked tag is executed instead of killing the run.
_FENCE_OPEN_RE = re.compile(
    r"```[ \t]*(?:mswea_bash_command|bash|sh|shell)[ \t]*\r?\n", re.DOTALL
)


def salvage_fenced_command(content: str) -> str | None:
    """Return the command from exactly one bash fence, else None."""
    text = content or ""
    commands = [m.strip() for m in _FENCED_COMMAND_RE.findall(text)]
    commands = [c for c in commands if c]
    if len(commands) == 1:
        return commands[0]
    if commands:
        return None
    opens = list(_FENCE_OPEN_RE.finditer(text))
    if len(opens) != 1:
        return None
    tail = text[opens[0].end() :]
    for leak in ("</parameter>", "</invoke>", "</tool_call>"):
        tail = tail.split(leak)[0]
    tail = tail.replace("```", "").strip()
    return tail or None