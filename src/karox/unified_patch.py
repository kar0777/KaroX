"""Strict unified-diff parser for atomic workspace transactions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .core import InvalidCommand
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


def parse_unified_patch(text: str) -> tuple[FilePatch, ...]:
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
            match = _HUNK.match(lines[index])
            if match is None:
                index += 1
                continue
            index += 1
            body: list[str] = []
            old_seen = 0
            new_seen = 0
            old_no_newline = False
            new_no_newline = False
            while index < len(lines):
                line = lines[index]
                if line.startswith(("@@ ", "--- ", "diff --git ")):
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
                if not line or line[0] not in {" ", "+", "-"}:
                    raise InvalidCommand(f"invalid patch hunk line: {line!r}")
                body.append(line)
                if line[0] in {" ", "-"}:
                    old_seen += 1
                if line[0] in {" ", "+"}:
                    new_seen += 1
                index += 1
            old_count = int(match.group("old_count") or "1")
            new_count = int(match.group("new_count") or "1")
            if old_seen != old_count or new_seen != new_count:
                raise InvalidCommand(
                    "patch hunk counts do not match body: "
                    f"expected -{old_count}/+{new_count}, "
                    f"saw -{old_seen}/+{new_seen}"
                )
            hunks.append(
                PatchHunk(
                    old_start=int(match.group("old_start")),
                    old_count=old_count,
                    new_start=int(match.group("new_start")),
                    new_count=new_count,
                    lines=tuple(body),
                    old_no_newline=old_no_newline,
                    new_no_newline=new_no_newline,
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


def apply_hunks(original: str, hunks: Iterable[PatchHunk], *, path: str) -> str:
    newline = "\r\n" if "\r\n" in original else "\n"
    had_final_newline = original.endswith(("\n", "\r"))
    source = original.replace("\r\n", "\n").splitlines()
    result: list[str] = []
    cursor = 0
    hunk_list = list(hunks)
    for hunk in hunk_list:
        target = max(0, hunk.old_start - 1)
        if target < cursor or target > len(source):
            raise InvalidCommand(
                f"patch hunk for {path} has invalid source line {hunk.old_start}"
            )
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
