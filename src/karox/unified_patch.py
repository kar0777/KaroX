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
            match = _HUNK.match(lines[index])
            index += 1
            body: list[str] = []
            from_blank: list[bool] = []
            old_no_newline = False
            new_no_newline = False
            while index < len(lines):
                line = lines[index]
                if _is_hunk_start(line) or line.startswith(("--- ", "diff --git ")):
                    break
                if line == "\\ No newline at end of file":
                    if not body:
                        raise InvalidCommand(
                            "no-newline marker must follow a patch hunk line"
                        )
                    previous_prefix = body[-1][0]
                    if previous_prefix in {"-", " "}:
                        old_no_newline = True
                    if previous_prefix in {"+", " "}:
                        new_no_newline = True
                    index += 1
                    continue
                if not line:
                    # An editor or a model stripped the blank context line's
                    # leading space; trailing ones are dropped below.
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
                # A bare blank line between hunks or at the end of the text is
                # layout, not context the file must contain.
                body.pop()
                from_blank.pop()
            old_seen = sum(1 for line in body if line[0] in {" ", "-"})
            new_seen = sum(1 for line in body if line[0] in {" ", "+"})
            if not body:
                raise InvalidCommand("patch hunk has no lines")
            located = match is not None
            hunks.append(
                PatchHunk(
                    old_start=int(match.group("old_start")) if located else 0,
                    old_count=old_seen,
                    new_start=int(match.group("new_start")) if located else 0,
                    new_count=new_seen,
                    lines=tuple(body),
                    old_no_newline=old_no_newline,
                    new_no_newline=new_no_newline,
                    numbered=located,
                )
            )
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
    elif not old:
        raise InvalidCommand(
            f"patch hunk for {path} has no line numbers and no context lines to locate it"
        )
    width = len(old)
    hits = [
        start
        for start in range(cursor, len(source) - width + 1)
        if source[start : start + width] == old
    ]
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
    if updated and final_newline:
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
