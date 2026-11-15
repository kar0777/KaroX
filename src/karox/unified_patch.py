"""Strict unified-diff parser for atomic workspace transactions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .core_types import InvalidCommand
from .workspace_transaction import WorkspaceTransaction


MAX_PATCH_BYTES = 4_000_000
_HUNK = re.compile(
    r"^@@ -(?P<old_start>[0-9]+)(?:,(?P<old_count>[0-9]+))? "
    r"\+(?P<new_start>[0-9]+)(?:,(?P<new_count>[0-9]+))? @@(?: .*)?$"
)


@dataclass(frozen=True)
class PatchHunk:
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    lines: tuple[str, ...]
    old_no_newline: bool = False
    new_no_newline: bool = False
    #: False for a bare ``@@`` header: the hunk is located by its own lines.
    numbered: bool = True
    #: Text after a bare ``@@`` (``@@ def parse():``): the hunk follows that line.
    anchor: str = ""
    #: ``*** End of File`` closed the hunk: it ends at the end of the file.
    at_end: bool = False


@dataclass(frozen=True)
class FilePatch:
    old_path: Optional[str]
    new_path: Optional[str]
    hunks: tuple[PatchHunk, ...]


def _path_from_header(value: str) -> Optional[str]:
    value = value.split("\t", 1)[0].strip()
    if value == "/dev/null":
        return None
    if value.startswith(("a/", "b/")):
        value = value[2:]
    if not value:
        raise InvalidCommand("patch path is empty")
    return value


def _is_hunk_start(line: str) -> bool:
    return line.startswith("@@")


_CODEX_FILE = re.compile(r"^\*\*\* (?P<kind>Add|Update|Delete) File: (?P<path>.+?)\s*$")
_CODEX_WRAPPER = frozenset({"*** Begin Patch", "*** End Patch"})
_CODEX_MOVE = "*** Move to: "
_CODEX_EOF = "*** End of File"


def codex_patch_files(text: str) -> list[tuple[str, str]]:
    """``(kind, path)`` for each file a ``*** Begin Patch`` text names.

    ``kind`` is ``add``, ``update``, ``delete`` or ``move`` (a rename's
    destination). The risk mapper reads this, never the parser, so a patch
    that fails to parse is still classified by every path it names.
    """

    found: list[tuple[str, str]] = []
    for line in text.splitlines():
        match = _CODEX_FILE.match(line.rstrip())
        if match is not None:
            found.append((match.group("kind").lower(), match.group("path").strip()))
        elif line.startswith(_CODEX_MOVE) and line[len(_CODEX_MOVE):].strip():
            found.append(("move", line[len(_CODEX_MOVE):].strip()))
    return found


def _hunk_body(lines: list[str], index: int, stop: Any) -> tuple[list[str], int, bool, bool, bool]:
    """Body lines of one hunk from ``index`` until ``stop(line)``.

    Returns the body, the next index, the two no-newline markers and whether
    an ``*** End of File`` line closed it. A blank line is a context line whose
    leading space was stripped; blank lines at the end are layout.
    """

    body: list[str] = []
    from_blank: list[bool] = []
    old_no_newline = new_no_newline = at_end = False
    while index < len(lines):
        line = lines[index]
        if stop(line):
            break
        if line.rstrip() == _CODEX_EOF:
            at_end = True
            index += 1
            break
        if line == "\\ No newline at end of file":
            if not body:
                raise InvalidCommand("no-newline marker must follow a patch hunk line")
            previous_prefix = body[-1][0]
            if previous_prefix in {"-", " "}:
                old_no_newline = True
            if previous_prefix in {"+", " "}:
                new_no_newline = True
            index += 1
            continue
        if not line:
            body.append(" ")
            from_blank.append(True)
            index += 1
            continue
        if line[0] not in {" ", "+", "-"}:
            raise InvalidCommand(f"invalid patch hunk line: {line!r}")
        body.append(line)
        from_blank.append(False)
        index += 1
    while body and from_blank[-1]:
        body.pop()
        from_blank.pop()
    return body, index, old_no_newline, new_no_newline, at_end


def _make_hunk(header: str, body: list[str], flags: tuple[bool, bool, bool]) -> PatchHunk:
    if not body:
        raise InvalidCommand("patch hunk has no lines")
    match = _HUNK.match(header) if header else None
    return PatchHunk(
        old_start=int(match.group("old_start")) if match else 0,
        old_count=sum(1 for line in body if line[0] in {" ", "-"}),
        new_start=int(match.group("new_start")) if match else 0,
        new_count=sum(1 for line in body if line[0] in {" ", "+"}),
        lines=tuple(body),
        old_no_newline=flags[0],
        new_no_newline=flags[1],
        numbered=match is not None,
        anchor="" if match else header[2:].strip(),
        at_end=flags[2],
    )


def _parse_codex_patch(lines: list[str]) -> tuple[FilePatch, ...]:
    """The ``*** Begin Patch`` format GPT-family models are trained to write.

    ``*** Update File:`` sections hold ``@@``-separated hunks without line
    numbers (``@@ def name():`` names a line the hunk follows), ``*** Add
    File:`` holds ``+`` lines, ``*** Delete File:`` nothing. Each hunk becomes
    an unnumbered unified hunk, so applying it verifies every context and
    removed line exactly as for a unified diff.
    """

    def ends_section(line: str) -> bool:
        stripped = line.rstrip()
        return stripped in _CODEX_WRAPPER or _CODEX_FILE.match(stripped) is not None

    files: list[FilePatch] = []
    index = 0
    while index < len(lines):
        stripped = lines[index].rstrip()
        match = _CODEX_FILE.match(stripped)
        if match is None:
            if stripped and stripped not in _CODEX_WRAPPER:
                raise InvalidCommand(f"invalid patch line outside a file section: {lines[index]!r}")
            index += 1
            continue
        kind, path = match.group("kind"), match.group("path").strip()
        index += 1
        if kind == "Delete":
            files.append(FilePatch(path, None, ()))
            continue
        if kind == "Add":
            added: list[str] = []
            trailing_blank = 0
            while index < len(lines) and not ends_section(lines[index]):
                line = lines[index]
                if not line.startswith("+") and line.strip():
                    raise InvalidCommand(f"every line of *** Add File starts with '+': {line!r}")
                trailing_blank = 0 if line.startswith("+") else trailing_blank + 1
                added.append(line if line.startswith("+") else "+")
                index += 1
            # Blank lines before the next section are layout, not file content.
            added = added[: len(added) - trailing_blank]
            if not added:
                raise InvalidCommand(f"*** Add File: {path} has no lines")
            hunk = PatchHunk(0, 0, 1, len(added), tuple(added))
            files.append(FilePatch(None, path, (hunk,)))
            continue
        new_path = path
        if index < len(lines) and lines[index].startswith(_CODEX_MOVE):
            new_path = lines[index][len(_CODEX_MOVE):].strip()
            index += 1
        hunks: list[PatchHunk] = []
        while index < len(lines) and not ends_section(lines[index]):
            header = ""
            if _is_hunk_start(lines[index]):
                header = lines[index].rstrip()
                index += 1
            elif not lines[index].strip():
                index += 1
                continue
            body, index, no_old, no_new, at_end = _hunk_body(
                lines, index, lambda line: _is_hunk_start(line) or ends_section(line)
            )
            hunks.append(_make_hunk(header, body, (no_old, no_new, at_end)))
        if not hunks and new_path == path:
            raise InvalidCommand(f"*** Update File: {path} has no hunks")
        files.append(FilePatch(path, new_path, tuple(hunks)))
    if not files:
        raise InvalidCommand("patch contains no file sections")
    return tuple(files)


def parse_unified_patch(text: str) -> tuple[FilePatch, ...]:
    """Parse a unified diff; the hunk body, not its header, is authoritative.

    Models miscount hunk lines: measured runs (2026-11-05) sent
    ``@@ -355,10 +355,33 @@`` over a body of -9/+31 lines, then spent a turn
    and ~4 KB of reasoning recounting, and a codex-style ``@@`` header without
    numbers was refused as "no hunks". Like ``git apply --recount``, the counts
    are taken from the body; a header without numbers locates its hunk by the
    hunk's own lines. Applying still verifies every context and removed line
    against the file, so a wrong body is a conflict, never a silent edit.
    """

    if not isinstance(text, str) or not text.strip():
        raise InvalidCommand("patch must be a non-empty string")
    if len(text.encode("utf-8")) > MAX_PATCH_BYTES:
        raise InvalidCommand(f"patch is larger than {MAX_PATCH_BYTES} bytes")
    lines = text.splitlines()
    if any(_CODEX_FILE.match(line.rstrip()) for line in lines):
        return _parse_codex_patch(lines)
    # A unified diff wrapped in *** Begin/End Patch: the wrapper is not a hunk line.
    lines = [line for line in lines if line.rstrip() not in _CODEX_WRAPPER]
    index = 0
    files: list[FilePatch] = []
    while index < len(lines):
        if lines[index].startswith("diff --git "):
            index += 1
            continue
        if not lines[index].startswith("--- "):
            index += 1
            continue
        old_path = _path_from_header(lines[index][4:])
        index += 1
        if index >= len(lines) or not lines[index].startswith("+++ "):
            raise InvalidCommand("patch file header misses +++ line")
        new_path = _path_from_header(lines[index][4:])
        index += 1
        hunks: list[PatchHunk] = []
        while index < len(lines) and not lines[index].startswith("--- "):
            if lines[index].startswith("diff --git "):
                break
            if not _is_hunk_start(lines[index]):
                index += 1
                continue
            header = lines[index].rstrip()
            index += 1
            body, index, no_old, no_new, at_end = _hunk_body(
                lines,
                index,
                lambda line: _is_hunk_start(line) or line.startswith(("--- ", "diff --git ")),
            )
            hunks.append(_make_hunk(header, body, (no_old, no_new, at_end)))
        if not hunks:
            raise InvalidCommand("patch file contains no hunks")
        if old_path is None and new_path is None:
            raise InvalidCommand("patch cannot use /dev/null for both paths")
        files.append(FilePatch(old_path, new_path, tuple(hunks)))
    if not files:
        raise InvalidCommand("patch contains no file headers")
    return tuple(files)


def _old_side(hunk: PatchHunk) -> list[str]:
    return [line[1:] for line in hunk.lines if line[0] in {" ", "-"}]


def _first_mismatch(source: list[str], start: int, old: list[str]) -> Optional[tuple[int, str, str]]:
    for offset, value in enumerate(old):
        position = start + offset
        if position >= len(source) or source[position] != value:
            found = source[position] if position < len(source) else "<end of file>"
            return position, value, found
    return None


def _anchor_line(source: list[str], cursor: int, anchor: str) -> Optional[int]:
    """Index just after the first line from ``cursor`` that ``anchor`` names."""

    wanted = anchor.strip()
    if not wanted:
        return None
    for exact in (True, False):
        for position in range(cursor, len(source)):
            line = source[position].strip()
            if line == wanted if exact else line.startswith(wanted):
                return position + 1
    return None


def _locate_hunk(source: list[str], cursor: int, hunk: PatchHunk, *, path: str) -> int:
    """Where the hunk's old side starts: its stated line, else its unique match.

    Line numbers a model derives from an earlier read drift after its own
    edits; the hunk's lines say exactly what they replace. A block that occurs
    more than once from the cursor on is ambiguous and is refused, as before.
    """

    old = _old_side(hunk)
    if hunk.numbered:
        target = max(0, hunk.old_start - 1)
        if not old:
            if hunk.old_start == 0 or hunk.old_count == 0:
                # "@@ -N,0 +M,k @@" inserts after line N.
                target = hunk.old_start
            if target < cursor or target > len(source):
                raise InvalidCommand(
                    f"patch hunk for {path} has invalid source line {hunk.old_start}"
                )
            return target
        if cursor <= target <= len(source) and _first_mismatch(source, target, old) is None:
            return target
    after_anchor = _anchor_line(source, cursor, hunk.anchor) if not hunk.numbered else None
    if not hunk.numbered and not old:
        if hunk.at_end:
            return len(source)
        if after_anchor is not None:
            return after_anchor
        raise InvalidCommand(
            f"patch hunk for {path} has no line numbers and no context lines to locate it"
        )
    width = len(old)
    hits = [
        start
        for start in range(cursor, len(source) - width + 1)
        if source[start : start + width] == old
    ]
    if len(hits) > 1 and after_anchor is not None:
        # "@@ def name():" says which of the repeated blocks is meant: the
        # first one after that line, as codex's apply_patch reads it.
        following = [start for start in hits if start >= after_anchor]
        hits = following[:1] or hits
    if len(hits) > 1 and hunk.at_end:
        hits = [start for start in hits if start + width == len(source)] or hits
    if len(hits) == 1:
        return hits[0]
    if hunk.numbered:
        target = max(0, hunk.old_start - 1)
        if target < cursor or target > len(source):
            raise InvalidCommand(
                f"patch hunk for {path} has invalid source line {hunk.old_start}"
            )
        position, value, found = _first_mismatch(source, target, old) or (target, "", "")
        detail = (
            f"; its lines occur {len(hits)} times after line {cursor}, add context"
            if hits
            else ""
        )
        raise InvalidCommand(
            f"patch conflict in {path} at line {position + 1}: "
            f"expected {value!r}, found {found!r}{detail}"
        )
    if hits:
        raise InvalidCommand(
            f"patch hunk for {path} matches {len(hits)} places; add context lines "
            "or @@ line numbers"
        )
    raise InvalidCommand(
        f"patch conflict in {path}: the hunk's lines were not found after line "
        f"{cursor} (first line {old[0]!r})"
    )


def apply_hunks(original: str, hunks: Iterable[PatchHunk], *, path: str) -> str:
    newline = "\r\n" if "\r\n" in original else "\n"
    had_final_newline = original.endswith(("\n", "\r"))
    source = original.replace("\r\n", "\n").splitlines()
    result: list[str] = []
    cursor = 0
    hunk_list = list(hunks)
    for hunk in hunk_list:
        target = _locate_hunk(source, cursor, hunk, path=path)
        result.extend(source[cursor:target])
        cursor = target
        for patch_line in hunk.lines:
            prefix, value = patch_line[0], patch_line[1:]
            if prefix == "+":
                result.append(value)
                continue
            if cursor >= len(source) or source[cursor] != value:
                found = source[cursor] if cursor < len(source) else "<end of file>"
                raise InvalidCommand(
                    f"patch conflict in {path} at line {cursor + 1}: "
                    f"expected {value!r}, found {found!r}"
                )
            if prefix == " ":
                result.append(value)
            cursor += 1
    result.extend(source[cursor:])
    updated = newline.join(result)
    final_newline = had_final_newline
    if not original and result:
        # Added lines in a normal unified diff carry a newline unless an
        # explicit marker says otherwise.
        final_newline = True
    for hunk in hunk_list:
        if hunk.old_no_newline:
            final_newline = not hunk.new_no_newline
        elif hunk.new_no_newline:
            final_newline = False
    if result and final_newline:
        # One empty line is still a line: "+" alone creates "\n", not "".
        updated += newline
    return updated


def expected_map(arguments: dict[str, Any]) -> dict[str, str]:
    value = arguments.get("expected_sha256", {})
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise InvalidCommand(
            "expected_sha256 must be an object mapping paths to digests"
        )
    result: dict[str, str] = {}
    for path, digest in value.items():
        if (
            not isinstance(path, str)
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise InvalidCommand(
                "expected_sha256 entries must map path strings to sha256 digests"
            )
        try:
            int(digest, 16)
        except ValueError as exc:
            raise InvalidCommand(
                "expected_sha256 entries must map path strings to sha256 digests"
            ) from exc
        result[path] = digest.lower()
    return result


def execute_apply_patch(runtime: Any, payload: dict[str, Any]) -> dict[str, Any]:
    patch = payload.get("patch")
    if not isinstance(patch, str):
        raise InvalidCommand("apply_patch requires patch string")
    dry_run = payload.get("dry_run", False)
    allow_secret = payload.get("allow_secret_literal", False)
    if not isinstance(dry_run, bool) or not isinstance(allow_secret, bool):
        raise InvalidCommand(
            "dry_run and allow_secret_literal must be boolean"
        )
    expected = expected_map(payload)
    expected_checked: set[str] = set()

    def first_expected(path: str) -> Optional[str]:
        if path in expected_checked:
            return None
        expected_checked.add(path)
        return expected.get(path)

    transaction = WorkspaceTransaction(runtime)
    for file_patch in parse_unified_patch(patch):
        source_relative = file_patch.old_path or file_patch.new_path
        assert source_relative is not None
        if file_patch.old_path is None:
            destination = file_patch.new_path
            assert destination is not None
            if transaction.exists(destination):
                raise InvalidCommand(
                    f"patch creates a file that already exists: {destination}"
                )
            original = ""
        else:
            original = transaction.read_text(file_patch.old_path)
        updated = apply_hunks(
            original,
            file_patch.hunks,
            path=source_relative,
        )
        if file_patch.new_path is None:
            transaction.stage_delete(
                source_relative,
                expected_sha256=first_expected(source_relative),
            )
            continue
        destination = file_patch.new_path
        if file_patch.old_path is not None and destination != source_relative:
            if transaction.exists(destination):
                raise InvalidCommand(
                    f"patch rename destination already exists: {destination}"
                )
            destination_expected = first_expected(destination)
            if destination_expected is not None:
                raise InvalidCommand(
                    "expected_sha256 cannot name a new patch rename destination: "
                    f"{destination}"
                )
            transaction.stage_move(
                source_relative,
                destination,
                expected_sha256=first_expected(source_relative),
            )
            if updated != original:
                transaction.stage_write(
                    destination,
                    updated,
                    allow_secret_literal=allow_secret,
                )
            continue
        transaction.stage_write(
            destination,
            updated,
            expected_sha256=first_expected(destination),
            allow_secret_literal=allow_secret,
        )
    unknown_expected = sorted(set(expected).difference(expected_checked))
    if unknown_expected:
        raise InvalidCommand(
            "expected_sha256 contains paths not touched by the patch: "
            + ", ".join(unknown_expected)
        )
    if dry_run:
        return transaction.preview()
    result = transaction.commit()
    result["action"] = "apply_patch"
    return result
