"""Apply Codex-format patches with a read-only preflight phase.

Format and the 4-pass line matching follow Codex CLI's apply_patch (Apache-2.0) and
DSH's dsh-fs-patch (MIT); see THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from karox6.tools.atomic import write_atomic


class PatchError(ValueError):
    """A patch is invalid or cannot be applied."""


@dataclass(frozen=True, slots=True)
class FileChange:
    op: Literal["add", "update", "delete", "move"]
    path: str
    new_path: str | None = None
    added: int = 0
    removed: int = 0


@dataclass(slots=True)
class PatchResult:
    changes: list[FileChange] = field(default_factory=list)

    def summary(self) -> str:
        """Describe the applied file operations."""
        lines = ["Success. Updated the following files:"]
        for change in self.changes:
            letter = {"add": "A", "update": "M", "delete": "D", "move": "M"}[change.op]
            name = change.path
            if change.new_path is not None:
                name += f" -> {change.new_path}"
            counts = []
            if change.added:
                counts.append(f"+{change.added}")
            if change.removed:
                counts.append(f"-{change.removed}")
            suffix = f" ({' '.join(counts)})" if counts else ""
            lines.append(f"{letter} {name}{suffix}")
        return "\n".join(lines)


@dataclass(slots=True)
class _Hunk:
    headers: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    eof: bool = False


@dataclass(slots=True)
class _Operation:
    kind: Literal["add", "update", "delete"]
    path: str
    destination: str | None = None
    contents: list[str] = field(default_factory=list)
    hunks: list[_Hunk] = field(default_factory=list)


def _split_lines(text: str) -> list[str]:
    """Split on newlines only (str.splitlines also breaks on form feeds, U+2028, ...)."""
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


def _parse(text: str) -> list[_Operation]:
    lines = _split_lines(text)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if lines and re.fullmatch(r"(?:apply_patch\s+)?<<\s*['\"]?([\w]+)['\"]?", lines[0].strip()):
        match = re.fullmatch(r"(?:apply_patch\s+)?<<\s*['\"]?([\w]+)['\"]?", lines.pop(0).strip())
        assert match is not None
        if not lines or lines[-1].strip() != match.group(1):
            raise PatchError("Missing heredoc terminator")
        lines.pop()
    if lines and lines[0].rstrip() == "*** Begin Patch":
        lines.pop(0)
    if lines and lines[-1].rstrip() == "*** End Patch":
        lines.pop()
    operations: list[_Operation] = []
    current: _Operation | None = None
    hunk: _Hunk | None = None
    for line in lines:
        marker = line.rstrip()
        action = next(
            (k for k in ("Add", "Update", "Delete") if marker.startswith(f"*** {k} File: ")), None
        )
        if action:
            path = marker[len(f"*** {action} File: ") :].strip()
            if not path:
                raise PatchError("Empty file path")
            kind: Literal["add", "update", "delete"] = {
                "Add": "add",
                "Update": "update",
                "Delete": "delete",
            }[action]  # type: ignore[assignment]
            current = _Operation(kind, path)
            operations.append(current)
            hunk = None
        elif current is None:
            raise PatchError(f"Unexpected patch line: {line!r}")
        elif marker.startswith("*** Move to: "):
            if current.kind != "update" or current.destination is not None or current.hunks:
                raise PatchError("Move marker must immediately follow Update File")
            current.destination = marker[len("*** Move to: ") :].strip()
            if not current.destination:
                raise PatchError("Empty move destination")
        elif current.kind == "add":
            if not line.startswith("+"):
                raise PatchError(f"Invalid add line: {line!r}")
            current.contents.append(line[1:])
        elif current.kind == "delete":
            raise PatchError(f"Unexpected delete line: {line!r}")
        elif marker == "*** End of File":
            if hunk is None or not hunk.lines or hunk.eof:
                raise PatchError("Unexpected End of File marker")
            hunk.eof = True
            hunk = None
        elif line == "@@" or line.startswith("@@ "):
            header = line[2:].strip()
            if hunk is None or hunk.lines:
                hunk = _Hunk()
                current.hunks.append(hunk)
            hunk.headers.append(header)
        elif line == "" or line[0] in " +-":
            if hunk is None:
                hunk = _Hunk()
                current.hunks.append(hunk)
            hunk.lines.append(line if line else " ")
        else:
            raise PatchError(f"Invalid patch line: {line!r}")
    if not operations:
        raise PatchError("Empty patch")
    for op in operations:
        if op.kind == "update" and (not op.hunks or any(not h.lines for h in op.hunks)):
            raise PatchError(f"Update has no hunk: {op.path}")
    return operations


def _normalize(line: str) -> str:
    quotes = {"‘": "'", "’": "'", "‚": "'", "‛": "'", "“": '"', "”": '"', "„": '"', "‟": '"'}
    return "".join(
        "-"
        if unicodedata.category(c) == "Pd" or c == "−"
        else quotes.get(c, " " if c.isspace() else c)
        for c in line
    ).strip()


def _seek(lines: list[str], wanted: list[str], start: int, eof: bool = False) -> int | None:
    if not wanted:
        return len(lines) if eof else start
    last = len(lines) - len(wanted)
    for transform in (lambda s: s, str.rstrip, str.strip, _normalize):
        indices = range(last, start - 1, -1) if eof else range(start, last + 1)
        target = [transform(line) for line in wanted]
        for index in indices:
            if [transform(line) for line in lines[index : index + len(wanted)]] == target:
                return index
    return None


def _resolve(root: Path, name: str) -> tuple[Path, str]:
    path = Path(name)
    if ".." in path.parts:
        raise PatchError(f"Path escapes root: {name}")
    resolved = (root / path).resolve()
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise PatchError(f"Path escapes root: {name}") from exc
    if not relative.parts:
        raise PatchError(f"Invalid file path: {name}")
    return resolved, relative.as_posix()


def _read(path: Path, name: str) -> bytes:
    try:
        data = path.read_bytes()
        if b"\0" in data[:8000]:
            raise PatchError(f"Binary file: {name}")
        data.decode("utf-8")
        return data
    except (OSError, UnicodeError) as exc:
        raise PatchError(f"Cannot read text file {name}: {exc}") from exc


def _update(data: bytes, op: _Operation, name: str) -> tuple[bytes, int, int]:
    text = data.decode("utf-8")
    newline = "\r\n" if "\r\n" in text else "\n"
    ended = text.endswith("\n")
    lines = _split_lines(text)
    position = 0
    added = removed = 0
    for hunk in op.hunks:
        for header in hunk.headers:
            if header:
                found = _seek(lines, [header], position)
                if found is None:
                    raise PatchError(f"Cannot find context {header!r} in {name}; re-read the file")
                position = found + 1
        old = [line[1:] for line in hunk.lines if line[0] in " -"]
        new = [line[1:] for line in hunk.lines if line[0] in " +"]
        found = _seek(lines, old, position, hunk.eof)
        if found is None:
            raise PatchError(f"Cannot match hunk in {name}: {old[:3]!r}; re-read the file")
        replacement: list[str] = []
        cursor = found
        for line in hunk.lines:
            if line[0] == " ":
                replacement.append(lines[cursor])
                cursor += 1
            elif line[0] == "-":
                cursor += 1
            else:
                replacement.append(line[1:])
        lines[found : found + len(old)] = replacement
        position = found + len(new)
        added += sum(line.startswith("+") for line in hunk.lines)
        removed += sum(line.startswith("-") for line in hunk.lines)
    result = newline.join(lines) + (newline if ended and lines else "")
    return result.encode("utf-8"), added, removed


def apply_patch(patch_text: str, root: Path) -> PatchResult:
    """Parse and preflight a patch before writing its operations in order."""
    operations = _parse(patch_text)
    root = root.resolve()
    state: dict[Path, bytes | None] = {}
    actions: list[tuple[Path, Path | None, bytes | None, FileChange]] = []

    def get(path: Path, name: str) -> bytes | None:
        if path not in state:
            state[path] = _read(path, name) if path.exists() else None
        return state[path]

    for op in operations:
        path, name = _resolve(root, op.path)
        existing = get(path, name)
        if op.kind == "add":
            if existing is not None:
                raise PatchError(f"Add target already exists: {name}")
            data = ("\n".join(op.contents) + "\n").encode("utf-8") if op.contents else b""
            change = FileChange("add", name, added=len(op.contents))
            state[path] = data
            actions.append((path, None, data, change))
        elif existing is None:
            raise PatchError(f"File does not exist: {name}")
        elif op.kind == "delete":
            change = FileChange("delete", name)
            state[path] = None
            actions.append((path, None, None, change))
        else:
            destination = None
            dest_name = None
            if op.destination is not None:
                destination, dest_name = _resolve(root, op.destination)
                if get(destination, dest_name) is not None:
                    raise PatchError(f"Move target already exists: {dest_name}")
            data, added, removed = _update(existing, op, name)
            change = FileChange(
                "move" if destination else "update", name, dest_name, added, removed
            )
            state[path] = None if destination else data
            if destination:
                state[destination] = data
            actions.append((path, destination, data, change))
    result = PatchResult()
    for path, destination, action_data, change in actions:
        try:
            if action_data is None:
                path.unlink()
            else:
                target = destination or path
                target.parent.mkdir(parents=True, exist_ok=True)
                write_atomic(target, action_data)
                if destination:
                    path.unlink()
            result.changes.append(change)
        except OSError as exc:
            applied = ", ".join(c.path for c in result.changes) or "none"
            raise PatchError(f"Commit failed at {change.path}: {exc}; applied: {applied}") from exc
    return result
