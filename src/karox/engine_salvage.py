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

The same holds for a tool call the model wrote in its native markup: r4
eng-task-queue -- right after the rewrite the model asked for its own probe as
``<｜DSML｜ invoke name="checks_run">`` text four times, the gateway parsed
none of it, and three format errors later the markup itself was accepted as
the final answer: the probe that printed the shared nested payload never ran
and the hidden isolation test failed. Across r2b-r4 that leak cost 17 extra
turns, and beside a call the gateway did parse it silently dropped the second
call of the batch. A complete invocation of a known tool is unambiguous, so it
is executed as the call it names.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Mapping, Optional, Tuple

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


#: DeepSeek's DSML tags (``<｜DSML｜ invoke name="x">``) as the gateway leaks
#: them, the full-width bars sometimes ASCII, the prefix sometimes dropped.
_BAR = "[|\uff5c]"
_DSML = rf"(?:{_BAR}\s*DSML\s*{_BAR}\s*)?"
_INVOKE_RE = re.compile(
    rf"<{_DSML}invoke\s+name\s*=\s*\"([^\"<>]{{1,200}})\"\s*>(.*?)</{_DSML}invoke\s*>",
    re.DOTALL,
)
_PARAMETER_RE = re.compile(
    rf"<{_DSML}parameter\s+name\s*=\s*\"([^\"<>]{{1,200}})\""
    rf"(?:\s+string\s*=\s*\"(true|false)\")?\s*>(.*?)</{_DSML}parameter\s*>",
    re.DOTALL,
)
_WRAPPER_RE = re.compile(rf"</?{_DSML}(?:function_calls|tool_calls|calls)\s*>")
#: Hermes/Qwen form: ``<tool_call>{"name": ..., "arguments": {...}}</tool_call>``.
_HERMES_RE = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)
#: Text carrying any of these is an attempted call, never a prose answer.
MARKUP_CALL_MARKERS = ("\uff5cDSML\uff5c", "|DSML|", "<invoke name=", "<tool_call>")


def salvage_markup_tool_calls(
    content: str,
    known_names: "set[str] | frozenset[str]",
    schemas: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> Tuple[List[Tuple[str, Dict[str, Any]]], str]:
    """Return ``([(name, arguments), ...], remaining_text)`` for leaked calls.

    Only complete invocations of a known tool are taken. A quoted example
    naming no real tool, or markup cut off mid-call, stays text, so the
    format-error path still sees it.
    """
    text = content or ""
    if "invoke" not in text and "<tool_call>" not in text:
        return [], text
    found: List[Tuple[int, int, str, Dict[str, Any]]] = []
    for match in _INVOKE_RE.finditer(text):
        name = match.group(1).strip()
        if name not in known_names:
            continue
        arguments: Dict[str, Any] = {}
        for parameter in _PARAMETER_RE.finditer(match.group(2)):
            key = parameter.group(1).strip()
            arguments[key] = _parameter_value(
                parameter.group(3), parameter.group(2), _declared_type(schemas, name, key)
            )
        found.append((match.start(), match.end(), name, arguments))
    for match in _HERMES_RE.finditer(text):
        try:
            payload = json.loads(match.group(1))
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        name = payload.get("name")
        arguments = payload.get("arguments", payload.get("parameters", {}))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except ValueError:
                continue
        if not isinstance(name, str) or name not in known_names:
            continue
        if not isinstance(arguments, dict):
            continue
        found.append((match.start(), match.end(), name, arguments))
    if not found:
        return [], text
    found.sort(key=lambda item: item[0])
    calls: List[Tuple[str, Dict[str, Any]]] = []
    pieces: List[str] = []
    cursor = 0
    for start, end, name, arguments in found:
        if start < cursor:
            continue
        pieces.append(text[cursor:start])
        cursor = end
        calls.append((name, arguments))
    pieces.append(text[cursor:])
    return calls, _WRAPPER_RE.sub("", "".join(pieces)).strip()


def _declared_type(
    schemas: Optional[Mapping[str, Mapping[str, Any]]], tool: str, key: str
) -> Optional[str]:
    try:
        declared = (schemas or {})[tool]["properties"][key].get("type")
    except (KeyError, TypeError, AttributeError):
        return None
    return declared if isinstance(declared, str) else None


def _parameter_value(raw: str, string_flag: Optional[str], declared: Optional[str]) -> Any:
    """``string="true"`` is the text itself; ``string="false"`` is JSON."""
    if string_flag == "true" or (string_flag is None and declared == "string"):
        return raw
    try:
        return json.loads(raw)
    except ValueError:
        return raw
