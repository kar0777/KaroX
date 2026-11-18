"""File tools: read, write, edit.

Contracts follow DSH's fs tools (MIT, see THIRD_PARTY_NOTICES.md): line-numbered
reads with offset/limit, read-before-change, literal edits that must match once.
When an exact edit match fails, a short cascade of conservative fallbacks (line-number
prefixes pasted from read output, trailing whitespace, indentation, Unicode
punctuation) is tried; a fallback is used only when it matches exactly one place.
"""

from __future__ import annotations

import asyncio
import re
import stat
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

from karox6.tools.atomic import write_atomic
from karox6.tools.base import ToolContext, ToolResult
from karox6.tools.clones import TIME_BUDGET_S, copies_note, find_copies

READ_LIMIT = 2000
READ_MAX_LINE = 2000
READ_MAX_BYTES = 50 * 1024
READ_MAX_FILE_BYTES = 64 * 1024 * 1024  # larger files: grep or a shell slice
BINARY_PROBE = 8000


def _display(path: Path, ctx: ToolContext) -> str:
    try:
        return path.relative_to(ctx.workspace).as_posix()
    except ValueError:
        return str(path)


def _is_binary(data: bytes) -> bool:
    return b"\0" in data[:BINARY_PROBE]


def _decode(data: bytes) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        data = data[3:]
    return data.decode("utf-8", errors="replace")


class ReadTool:
    name = "read"
    description = (
        "Read a UTF-8 text file and return line-numbered content (`N: text`). Reads up to "
        f"{READ_LIMIT} lines from `offset` (1-based); use offset and limit to continue in large "
        "files. A directory path lists its entries. `file_paths` reads several whole files in "
        f"one call (together at most {READ_MAX_BYTES // 1024} KB). Use this tool, not shell "
        "commands like cat, to inspect files."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to the file."},
            "offset": {"type": "integer", "description": "First line to read (1-based)."},
            "limit": {"type": "integer", "description": f"Lines to read (default {READ_LIMIT})."},
            "file_paths": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Several files to read from the start, instead of file_path.",
            },
        },
    }
    read_only = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        many = args.get("file_paths")
        if isinstance(many, list) and many and not args.get("file_path"):
            if args.get("offset") or args.get("limit"):
                return ToolResult(
                    "Error: invalid arguments: offset and limit apply to file_path only; "
                    "file_paths reads whole files.",
                    is_error=True,
                )
            return _read_many([str(p) for p in many], ctx)
        raw = str(args.get("file_path") or args.get("path") or "")
        if not raw:
            return ToolResult(
                "Error: invalid arguments: give file_path (or file_paths).", is_error=True
            )
        result, _ = _read_file(
            raw, ctx, int(args.get("offset") or 1), int(args.get("limit") or READ_LIMIT)
        )
        return result


READ_MAX_FILES = 20


def _read_many(paths: list[str], ctx: ToolContext) -> ToolResult:
    parts: list[str] = []
    errors = 0
    budget = READ_MAX_BYTES
    skipped: list[str] = []
    for raw in paths[:READ_MAX_FILES]:
        if budget <= 0:
            skipped.append(raw[:200])
            continue
        result, used = _read_file(raw, ctx, 1, READ_LIMIT, budget)
        budget -= used if not result.is_error else len(result.content)
        errors += result.is_error
        parts.append(result.content)
    skipped.extend(p[:200] for p in paths[READ_MAX_FILES:])
    if skipped:
        names = ", ".join(skipped[:20]) + (
            f" (+{len(skipped) - 20} more)" if len(skipped) > 20 else ""
        )
        parts.append(
            f"(Not read - this call reads at most {READ_MAX_FILES} files and "
            f"{READ_MAX_BYTES // 1024} KB: {names}. Read them in another call.)"
        )
    return ToolResult("\n\n".join(parts), is_error=errors == len(parts) and not skipped)


def _read_file(
    raw: str, ctx: ToolContext, offset: int, limit: int, max_bytes: int = READ_MAX_BYTES
) -> tuple[ToolResult, int]:
    """One file's read result and the bytes of content it used."""
    path = ctx.resolve(raw)
    shown = _display(path, ctx)
    if not path.exists():
        return ToolResult(f"Error: {shown} does not exist.", is_error=True), 0
    if path.is_dir():
        listing = _list_dir(path, shown)
        return ToolResult(listing), len(listing)
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            # A FIFO without a writer or a device would block the whole session.
            return ToolResult(f"Error: {shown} is not a regular file.", is_error=True), 0
        if info.st_size > READ_MAX_FILE_BYTES:
            return (
                ToolResult(
                    f"Error: {shown} is {info.st_size // (1024 * 1024)} MB, too large to "
                    "read whole; use grep, or shell head/tail/sed -n on the part you need.",
                    is_error=True,
                ),
                0,
            )
        data = path.read_bytes()
    except OSError as exc:
        return ToolResult(f"Error: cannot read {shown}: {exc}", is_error=True), 0
    if _is_binary(data):
        return (
            ToolResult(f"Error: {shown} is a binary file ({len(data)} bytes).", is_error=True),
            0,
        )
    ctx.files.observe(path)
    lines = _decode(data).replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    offset = max(offset, 1)
    limit = max(limit, 1)
    if total and offset > total:
        return (
            ToolResult(
                f"Error: offset {offset} is past the end of {shown} ({total} lines).",
                is_error=True,
            ),
            0,
        )
    out: list[str] = []
    size = 0
    last = offset - 1
    for number in range(offset, min(offset + limit, total + 1)):
        line = lines[number - 1]
        if len(line) > READ_MAX_LINE:
            line = line[:READ_MAX_LINE] + f"... (line truncated to {READ_MAX_LINE} chars)"
        entry = f"{number}: {line}"
        size += len(entry.encode("utf-8")) + 1
        if out and size > max_bytes:
            break
        out.append(entry)
        last = number
    if total == 0:
        footer = "(Empty file.)"
    elif last >= total:
        footer = f"(End of file - {total} lines.)"
    else:
        footer = f"(Showing lines {offset}-{last} of {total}. Use offset={last + 1} to continue.)"
    body = "\n".join(out)
    used = sum(len(e.encode("utf-8")) + 1 for e in out)
    return (
        ToolResult(
            f"<path>{shown}</path>\n<type>file</type>\n<content>\n{body}\n\n{footer}\n</content>",
            meta={"lines": [offset, last], "total_lines": total},
        ),
        used,
    )


def _list_dir(path: Path, shown: str) -> str:
    try:
        entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    except OSError as exc:
        return f"Error: cannot list {shown}: {exc}"
    names = [e.name + ("/" if e.is_dir() else "") for e in entries[:500]]
    more = f"\n({len(entries) - 500} more entries)" if len(entries) > 500 else ""
    return f"<path>{shown}</path>\n<type>directory</type>\n<entries>\n" + (
        "\n".join(names) + more + "\n</entries>"
    )


class WriteTool:
    name = "write"
    description = (
        "Create or fully replace a UTF-8 text file (parent directories are created). Read an "
        "existing file before overwriting it, and prefer edit for targeted changes."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to the file."},
            "content": {"type": "string", "description": "The complete new file content."},
        },
        "required": ["file_path", "content"],
    }
    read_only = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(str(args.get("file_path") or args.get("path") or ""))
        shown = _display(path, ctx)
        content = str(args["content"])
        if path.is_dir():
            return ToolResult(f"Error: {shown} is a directory.", is_error=True)
        existed = path.exists()
        if existed:
            stale = ctx.files.check_fresh(path)
            if stale:
                return ToolResult(f"Error: {stale}", is_error=True)
            try:
                old = path.read_bytes()
            except OSError as exc:
                return ToolResult(f"Error: cannot read {shown}: {exc}", is_error=True)
            if b"\r\n" in old and "\r\n" not in content:
                content = content.replace("\n", "\r\n")
        try:
            write_atomic(path, content.encode("utf-8"))
        except OSError as exc:
            return ToolResult(f"Error: cannot write {shown}: {exc}", is_error=True)
        ctx.files.record_write(path)
        lines = content.count("\n") + (0 if content.endswith("\n") or not content else 1)
        verb = "Updated" if existed else "Created"
        return ToolResult(f"{verb} {shown} ({lines} lines).", meta={"path": str(path)})


class EditTool:
    name = "edit"
    description = (
        "Edit an existing UTF-8 text file by replacing literal text. `old_string` must match "
        "exactly one place in the file (include enough surrounding lines to make it unique) "
        "unless `replace_all` is true; do not include the `N: ` line-number prefixes shown by "
        "read."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to the file."},
            "old_string": {"type": "string", "description": "Exact text to replace."},
            "new_string": {"type": "string", "description": "Replacement text."},
            "replace_all": {"type": "boolean", "description": "Replace every occurrence."},
        },
        "required": ["file_path", "old_string", "new_string"],
    }
    read_only = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = ctx.resolve(str(args.get("file_path") or args.get("path") or ""))
        shown = _display(path, ctx)
        old = str(args["old_string"])
        new = str(args["new_string"])
        if not path.exists():
            hint = " Use write to create a new file." if old == "" else ""
            return ToolResult(f"Error: {shown} does not exist.{hint}", is_error=True)
        if old == new:
            return ToolResult("Error: old_string and new_string are identical.", is_error=True)
        if old == "":
            return ToolResult(
                "Error: old_string is empty. Quote the text to replace, or use write.",
                is_error=True,
            )
        # An exact, unique old_string proves the agent knows the text it replaces, even if
        # it saw the file only through shell output or the file changed since its last
        # read (its own shell command, a formatter). Only a fuzzy match needs a fresh read.
        unsure = ""
        if not ctx.files.seen(path):
            unsure = "has not been read in this session"
        elif ctx.files.check_fresh(path):
            unsure = "changed on disk since it was last read"
        try:
            data = path.read_bytes()
        except OSError as exc:
            return ToolResult(f"Error: cannot read {shown}: {exc}", is_error=True)
        if _is_binary(data):
            return ToolResult(f"Error: {shown} is a binary file.", is_error=True)
        had_bom = data.startswith(b"\xef\xbb\xbf")
        # surrogateescape keeps bytes that are not UTF-8 (a Latin-1 fixture) as they were
        # instead of turning them into U+FFFD on write.
        raw = (data[3:] if had_bom else data).decode("utf-8", "surrogateescape")
        crlfs = raw.count("\r\n")
        mixed = 0 < crlfs < raw.count("\n")
        crlf = crlfs > 0 and not mixed
        old_n = old.replace("\r\n", "\n")
        new_n = new.replace("\r\n", "\n")
        replace_all = bool(args.get("replace_all"))

        result: tuple[str, int, str] | str
        if mixed and old_n in raw:
            # Mixed line endings: edit the text as it is, so lines outside the edit keep
            # their own endings.
            result = apply_edit(raw, old_n, new_n, replace_all)
            text = raw
        else:
            text = raw.replace("\r\n", "\n") if crlfs else raw
            result = apply_edit(text, old_n, new_n, replace_all)
        if isinstance(result, str):
            return ToolResult(f"Error: {result.replace('{path}', shown)}", is_error=True)
        updated, count, how = result
        if unsure and how != "exactly":
            return ToolResult(
                f"Error: old_string matches {shown} only approximately ({how}) and the file "
                f"{unsure}. Read it, then retry.",
                is_error=True,
            )
        if unsure and count != 1:
            return ToolResult(
                f"Error: replace_all would change {count} places in {shown}, which "
                f"{unsure}. Read it, then retry.",
                is_error=True,
            )
        if crlf:
            updated = updated.replace("\n", "\r\n")
        payload = updated.encode("utf-8", "surrogateescape")
        if had_bom:
            payload = b"\xef\xbb\xbf" + payload
        try:
            write_atomic(path, payload)
        except OSError as exc:
            return ToolResult(f"Error: cannot write {shown}: {exc}", is_error=True)
        ctx.files.record_write(path, observe=not unsure)
        note = f" (matched {how})" if how != "exactly" else ""
        plural = "s" if count != 1 else ""
        snippet = changed_snippet(text, updated.replace("\r\n", "\n")) if count == 1 else ""
        copies = await _copies_note(ctx, path, old_n, new_n) if ctx.copy_notes else ""
        return ToolResult(
            f"Edited {shown}: replaced {count} occurrence{plural}{note}.{snippet}{copies}",
            meta={"path": str(path), "match": how},
        )


async def _copies_note(ctx: ToolContext, path: Path, old: str, new: str) -> str:
    """Other places that still hold the replaced code; never fails the edit."""
    try:
        path.relative_to(ctx.workspace)
    except ValueError:
        return ""
    budget = ctx.wait_cap_s(TIME_BUDGET_S)  # never past the run's time limit
    if budget < 0.2:
        return ""
    try:
        found = await asyncio.wait_for(
            asyncio.to_thread(find_copies, ctx.workspace, path, old, new, budget_s=budget),
            timeout=budget + 1.0,
        )
    except Exception:  # includes the wait timing out: the edit itself already succeeded
        return ""
    return copies_note(found, ctx.reported_copies)


SNIPPET_CONTEXT = 3
SNIPPET_MAX_LINES = 30


def changed_snippet(before: str, after: str) -> str:
    """The edited region of ``after`` with a few lines around it, numbered as read shows
    them, so the model sees the result and the new line numbers without re-reading."""
    if before == after:
        return ""
    limit = min(len(before), len(after))
    head = 0
    while head < limit and before[head] == after[head]:
        head += 1
    tail = 0
    while tail < limit - head and before[-1 - tail] == after[-1 - tail]:
        tail += 1
    lines = after.split("\n")
    if len(lines) > 1 and lines[-1] == "":
        lines.pop()  # the final newline does not start a line
    first = after.count("\n", 0, head)  # 0-based line of the first changed char
    # the last changed character (a changed region may end with its own newline)
    last = min(after.count("\n", 0, max(len(after) - tail - 1, head)), len(lines) - 1)
    first = min(first, last)
    start = max(first - SNIPPET_CONTEXT, 0)
    end = min(last + SNIPPET_CONTEXT, len(lines) - 1)
    shown = range(start, min(end, start + SNIPPET_MAX_LINES - 1) + 1)
    body = "\n".join(f"{n + 1}: {lines[n]}" for n in shown)
    more = "\n..." if end > shown[-1] else ""
    return f"\nThe edited region now reads:\n{body}{more}"


# --- matching -----------------------------------------------------------------

_LINE_PREFIX = re.compile(r"^\s*\d+: ?")


def apply_edit(text: str, old: str, new: str, replace_all: bool) -> tuple[str, int, str] | str:
    """Return (new text, replacements, how) or an error message (``{path}`` placeholder)."""
    count = text.count(old)
    if count == 1 or (count > 1 and replace_all):
        return text.replace(old, new), count, "exactly"
    if count > 1:
        lines = _match_lines(text, old)
        where = ", ".join(str(n) for n in lines[:8])
        return (
            f"old_string matches {count} places in {{path}} (starting at lines {where}). "
            "Include more surrounding lines to make it unique, or set replace_all."
        )
    for how, attempt in _FALLBACKS:
        done = attempt(text, old, new)
        if done is not None:
            return done, 1, how
    return _not_found(text, old)


def _match_lines(text: str, needle: str) -> list[int]:
    out: list[int] = []
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx < 0:
            return out
        out.append(text.count("\n", 0, idx) + 1)
        start = idx + 1


def _strip_line_numbers(text: str, old: str, new: str) -> str | None:
    old_lines = old.split("\n")
    if not all(_LINE_PREFIX.match(line) for line in old_lines if line.strip()):
        return None
    old2 = "\n".join(_LINE_PREFIX.sub("", line, count=1) for line in old_lines)
    new_lines = new.split("\n")
    if all(_LINE_PREFIX.match(line) for line in new_lines if line.strip()):
        new = "\n".join(_LINE_PREFIX.sub("", line, count=1) for line in new_lines)
    if text.count(old2) == 1:
        return text.replace(old2, new)
    return None


def _line_block_match(
    text: str, old: str, key: Callable[[str], str]
) -> tuple[list[str], int, int] | None:
    """Find the unique block of file lines equal to old's lines under ``key``."""
    file_lines = text.split("\n")
    old_lines = old.split("\n")
    while old_lines and not old_lines[-1].strip():
        old_lines.pop()
    while old_lines and not old_lines[0].strip():
        old_lines.pop(0)
    if not old_lines:
        return None
    want = [key(line) for line in old_lines]
    hits = [
        i
        for i in range(len(file_lines) - len(old_lines) + 1)
        if [key(line) for line in file_lines[i : i + len(old_lines)]] == want
    ]
    if len(hits) != 1:
        return None
    return file_lines, hits[0], len(old_lines)


def _replace_block(file_lines: list[str], start: int, size: int, new: str) -> str:
    new_lines = new.split("\n")
    while new_lines and not new_lines[-1].strip() and len(new_lines) > 1:
        new_lines.pop()
    return "\n".join(file_lines[:start] + new_lines + file_lines[start + size :])


def _trailing_ws(text: str, old: str, new: str) -> str | None:
    found = _line_block_match(text, old, str.rstrip)
    if found is None:
        return None
    file_lines, start, size = found
    return _replace_block(file_lines, start, size, new)


def _indentation(text: str, old: str, new: str) -> str | None:
    found = _line_block_match(text, old, str.strip)
    if found is None:
        return None
    file_lines, start, size = found
    old_lines = [ln for ln in old.split("\n") if ln.strip()]
    file_block = [ln for ln in file_lines[start : start + size] if ln.strip()]
    deltas = {_indent(f) - _indent(o) for f, o in zip(file_block, old_lines, strict=False)}
    if len(deltas) != 1:
        return None  # inconsistent shift: do not guess how to re-indent new_string
    delta = deltas.pop()
    shifted = []
    for line in new.split("\n"):
        if not line.strip():
            shifted.append(line)
        elif delta >= 0:
            shifted.append(_indent_char(file_block[0]) * delta + line)
        else:
            cut = min(-delta, _indent(line))
            shifted.append(line[cut:])
    return _replace_block(file_lines, start, size, "\n".join(shifted))


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" \t"))


def _indent_char(line: str) -> str:
    return "\t" if line.startswith("\t") else " "


_PUNCT = {
    "\u2018": "'",
    "\u2019": "'",
    "\u201c": '"',
    "\u201d": '"',
    "\u00a0": " ",
    "\u2009": " ",
    "\u202f": " ",
}


def _norm_punct(line: str) -> str:
    out = []
    for ch in line:
        if unicodedata.category(ch) == "Pd" or ch == "−":
            out.append("-")
        else:
            out.append(_PUNCT.get(ch, ch))
    return "".join(out).rstrip()


def _punctuation(text: str, old: str, new: str) -> str | None:
    found = _line_block_match(text, old, _norm_punct)
    if found is None:
        return None
    file_lines, start, size = found
    return _replace_block(file_lines, start, size, new)


_FALLBACKS: list[tuple[str, Callable[[str, str, str], str | None]]] = [
    ("after removing read line-number prefixes", _strip_line_numbers),
    ("ignoring trailing whitespace", _trailing_ws),
    ("ignoring indentation", _indentation),
    ("ignoring Unicode punctuation differences", _punctuation),
]


def _not_found(text: str, old: str) -> str:
    first = next((ln.strip() for ln in old.split("\n") if ln.strip()), "")
    hint = ""
    if first:
        lines = text.split("\n")
        near = [i + 1 for i, ln in enumerate(lines) if first in ln][:5]
        if near:
            hint = (
                f" Its first line occurs at line(s) {', '.join(map(str, near))}; "
                "compare the following lines with the file."
            )
    return (
        "old_string was not found in {path}."
        + hint
        + " Read the file again and copy the exact text."
    )
