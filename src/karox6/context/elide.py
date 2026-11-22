"""Superseded-output elision on the wire.

Every request re-sends the whole conversation, so an old `read` of a file that was read
again (or rewritten) since, and the full text of an old `write`/`edit`, are paid for on every
later step although the model no longer needs them. This shrinks them to a short stub in the
request only: the stored history is never touched.

A provider that bills a cached prompt prefix at a fraction of the input price pays for every
rewrite of an old message by re-reading everything after it. So the history is rewritten in
batches, not step by step: only when the last request was big (REWRITE_PRESSURE_TOKENS) and
the rewrite would reclaim enough (REWRITE_BATCH_CHARS). Once a message is elided it stays
elided with the very same stub, so the prefix is identical between batches.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from karox6.core.messages import Message, ToolCall

#: Rewrite old history only when the last request carried at least this many input tokens...
REWRITE_PRESSURE_TOKENS = 96_000
#: ...and the rewrite reclaims at least this many characters.
REWRITE_BATCH_CHARS = 24_000
#: The newest messages are never touched: the model is working on them.
KEEP_RECENT_MESSAGES = 6
#: A string argument of a write/edit call longer than this is elidable.
ELIDE_ARGUMENT_CHARS = 2_000
#: The newest this-many large write/edit calls keep their arguments (a chunked write
#: continues from its own seam).
KEEP_RECENT_ARGUMENT_CALLS = 2
#: A read result shorter than this is not worth a stub.
MIN_READ_ELIDE_CHARS = 400

READ_TOOLS = frozenset({"read"})
WRITE_TOOLS = frozenset({"write", "edit", "apply_patch"})

_FOOTER = re.compile(
    r"\((?:End of file - (\d+) lines\.|Showing lines (\d+)-(\d+) of \d+\.[^)]*)\)\s*</content>\s*$"
)
_PATCH_PATH = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (.+?)\s*$", re.M)


def read_stub(path: str) -> str:
    return f"[older read of {path} elided: superseded by a later read/edit]"


@dataclass(slots=True)
class _Call:
    seq: int
    index: int  # index of the assistant message
    call: ToolCall
    name: str
    args: dict[str, Any] | None
    result: int | None  # index of its (unique) tool result


class WireElider:
    """Decides, once and for good, which old messages are sent as stubs."""

    def __init__(self, workspace: Path) -> None:
        self._workspace = workspace
        # call id -> (id of the call that superseded it, stub text of its result)
        self._results: dict[str, tuple[str, str]] = {}
        # call id -> the call's arguments as sent from now on (a JSON object text)
        self._arguments: dict[str, str] = {}

    @property
    def elided(self) -> int:
        return len(self._results) + len(self._arguments)

    def view(self, messages: list[Message]) -> list[Message]:
        """``messages`` as the model sees them under the elisions already decided."""
        return self._apply(messages)

    def build(self, messages: list[Message], input_tokens: int) -> list[Message]:
        """The request view; decides a new batch first when the pressure and size say so."""
        if input_tokens >= REWRITE_PRESSURE_TOKENS:
            results, arguments, reclaimable = self._candidates(messages)
            if reclaimable >= REWRITE_BATCH_CHARS:
                self._results.update(results)
                self._arguments.update(arguments)
        return self._apply(messages)

    # -- applying ----------------------------------------------------------------------

    def _apply(self, messages: list[Message]) -> list[Message]:
        if not self._results and not self._arguments:
            return messages
        boundary = max(len(messages) - KEEP_RECENT_MESSAGES, 0)
        present = {c.id for m in messages if m.role == "assistant" for c in m.tool_calls}
        out = list(messages)
        for i in range(boundary):
            m = messages[i]
            if m.role == "tool":
                decided = self._results.get(m.tool_call_id or "")
                if decided is not None and decided[0] in present:
                    out[i] = replace(m, content=decided[1])
            elif m.role == "assistant" and any(c.id in self._arguments for c in m.tool_calls):
                calls = [
                    replace(c, arguments=self._arguments[c.id]) if c.id in self._arguments else c
                    for c in m.tool_calls
                ]
                out[i] = replace(m, tool_calls=calls)
        return out

    # -- deciding ----------------------------------------------------------------------

    def _candidates(
        self, messages: list[Message]
    ) -> tuple[dict[str, tuple[str, str]], dict[str, str], int]:
        """New elisions among the messages older than the recent ones, and the characters
        they would reclaim. Reads the original messages, so the answer does not depend on
        what an earlier batch already elided."""
        boundary = max(len(messages) - KEEP_RECENT_MESSAGES, 0)
        calls = self._calls(messages)
        present = {c.call.id for c in calls}
        results: dict[str, tuple[str, str]] = {}
        arguments: dict[str, str] = {}
        reclaimable = 0

        writes: dict[str, list[_Call]] = {}
        reads: dict[str, list[tuple[_Call, tuple[int, int]]]] = {}
        for c in calls:
            if c.args is None or c.result is None or messages[c.result].is_error:
                continue
            if c.name in WRITE_TOOLS:
                for key in self._paths(c):
                    writes.setdefault(key, []).append(c)
            elif c.name in READ_TOOLS and (span := _read_span(c, messages[c.result])):
                key = self._key(c.args.get("file_path") or c.args.get("path"))
                reads.setdefault(key, []).append((c, span))

        for key, same_file in reads.items():
            for k, (c, span) in enumerate(same_file):
                assert c.result is not None
                assert c.args is not None
                held = self._results.get(c.call.id)
                if c.result >= boundary or (held is not None and held[0] in present):
                    continue
                content = messages[c.result].content
                if len(content) < MIN_READ_ELIDE_CHARS:
                    continue
                newer = [r for r, s in same_file[k + 1 :] if s[0] <= span[0] and s[1] >= span[1]]
                newer += [w for w in writes.get(key, []) if w.seq > c.seq]
                if not newer:
                    continue
                by = min(newer, key=lambda x: x.seq)
                stub = read_stub(str(c.args.get("file_path") or c.args.get("path")))
                results[c.call.id] = (by.call.id, stub)
                reclaimable += len(content) - len(stub)

        large = [c for c in calls if c.name in WRITE_TOOLS and c.args and _long_keys(c.args)]
        for c in large[:-KEEP_RECENT_ARGUMENT_CALLS]:
            assert c.args is not None
            if c.index >= boundary or c.result is None or c.call.id in self._arguments:
                continue
            if _pending_error(messages, c.result):
                continue
            stub = _stub_arguments(c)
            arguments[c.call.id] = stub
            reclaimable += len(c.call.arguments) - len(stub)
        return results, arguments, reclaimable

    def _calls(self, messages: list[Message]) -> list[_Call]:
        """Every tool call in order, with its parsed arguments and result index. A call id
        that is not unique in the conversation is skipped: it cannot be told apart."""
        counts: dict[str, int] = {}
        for m in messages:
            if m.role == "assistant":
                for c in m.tool_calls:
                    counts[c.id] = counts.get(c.id, 0) + 1
            elif m.role == "tool" and m.tool_call_id:
                counts[m.tool_call_id] = counts.get(m.tool_call_id, 0) + 1
        result_at = {
            m.tool_call_id: i
            for i, m in enumerate(messages)
            if m.role == "tool" and m.tool_call_id and counts[m.tool_call_id] == 2
        }
        out: list[_Call] = []
        for i, m in enumerate(messages):
            if m.role != "assistant":
                continue
            for c in m.tool_calls:
                if counts.get(c.id) != 2 or c.id not in result_at:
                    continue
                out.append(
                    _Call(
                        seq=len(out),
                        index=i,
                        call=c,
                        name=c.name.strip().lower().removeprefix("functions."),
                        args=_parse(c.arguments),
                        result=result_at[c.id],
                    )
                )
        return out

    def _key(self, raw: Any) -> str:
        if not isinstance(raw, str) or not raw:
            return ""
        return os.path.normcase(os.path.normpath(os.path.join(self._workspace, raw)))

    def _paths(self, c: _Call) -> list[str]:
        args = c.args or {}
        if c.name == "apply_patch":
            patch = args.get("input")
            found = _PATCH_PATH.findall(patch) if isinstance(patch, str) else []
            return [k for k in (self._key(p) for p in found) if k]
        key = self._key(args.get("file_path") or args.get("path"))
        return [key] if key else []


def _parse(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw or "{}")
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def _read_span(c: _Call, result: Message) -> tuple[int, int] | None:
    """The lines a successful whole-file or ranged read returned, or None (a directory
    listing, several files at once, an empty file, anything not understood)."""
    args = c.args or {}
    if args.get("file_paths") or not (args.get("file_path") or args.get("path")):
        return None
    content = result.content
    if "<type>file</type>" not in content[:600]:
        return None
    match = _FOOTER.search(content[-400:])
    if match is None:
        return None
    if match.group(1) is not None:
        try:
            start = max(int(args.get("offset") or 1), 1)
        except (TypeError, ValueError):
            return None
        return start, int(match.group(1))
    return int(match.group(2)), int(match.group(3))


def _long_keys(args: dict[str, Any]) -> list[str]:
    return [k for k, v in args.items() if isinstance(v, str) and len(v) > ELIDE_ARGUMENT_CHARS]


def _stub_arguments(c: _Call) -> str:
    args = c.args or {}
    out: dict[str, Any] = {}
    for key, value in args.items():
        if isinstance(value, str) and len(value) > ELIDE_ARGUMENT_CHARS:
            note = f"[{key} elided: {len(value)} chars; the tool result records the outcome]"
            if c.name == "apply_patch":
                paths = _PATCH_PATH.findall(value)
                note = (
                    f"[{key} elided: {len(value)} chars; patched {', '.join(paths[:5]) or 'files'}]"
                )
            out[key] = note
        else:
            out[key] = value
    return json.dumps(out, ensure_ascii=False)


def _pending_error(messages: list[Message], result: int) -> bool:
    """A failed result the model has not answered yet (no assistant message after it)."""
    if not messages[result].is_error:
        return False
    return not any(m.role == "assistant" for m in messages[result + 1 :])
