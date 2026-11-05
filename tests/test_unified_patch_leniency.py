"""The hunk body is authoritative; every applied line is still verified."""

from __future__ import annotations

import pytest

from karox.core_types import InvalidCommand
from karox.unified_patch import apply_hunks, parse_unified_patch

SOURCE = "".join(f"line {index}\n" for index in range(1, 21))


def _apply(patch: str, source: str = SOURCE) -> str:
    (file_patch,) = parse_unified_patch(patch)
    return apply_hunks(source, file_patch.hunks, path="f.txt")


def test_wrong_hunk_counts_are_recounted_from_the_body() -> None:
    """swe-pytest-10356: -10/+33 over a -9/+31 body cost a turn of recounting."""

    patch = "--- a/f.txt\n+++ b/f.txt\n@@ -3,9 +3,12 @@\n line 3\n-line 4\n+LINE 4\n line 5\n"
    assert _apply(patch) == SOURCE.replace("line 4\n", "LINE 4\n")


def test_a_bare_header_locates_its_hunk_by_its_lines() -> None:
    """eng-csv-report: a codex-style "@@" header was refused as "no hunks"."""

    patch = (
        "--- a/f.txt\n+++ b/f.txt\n@@\n line 7\n-line 8\n+LINE 8\n"
        "@@ def something\n line 15\n+inserted\n line 16\n"
    )
    expected = SOURCE.replace("line 8\n", "LINE 8\n").replace(
        "line 15\n", "line 15\ninserted\n"
    )
    assert _apply(patch) == expected


def test_drifted_line_numbers_apply_at_the_unique_match() -> None:
    patch = "--- a/f.txt\n+++ b/f.txt\n@@ -2,3 +2,3 @@\n line 11\n-line 12\n+LINE 12\n line 13\n"
    assert _apply(patch) == SOURCE.replace("line 12\n", "LINE 12\n")


def test_ambiguous_or_missing_blocks_are_still_conflicts() -> None:
    repeated = "a\nb\na\nb\n"
    ambiguous = "--- a/f.txt\n+++ b/f.txt\n@@\n a\n-b\n+B\n"
    with pytest.raises(InvalidCommand, match="matches 2 places"):
        _apply(ambiguous, repeated)
    numbered_ambiguous = "--- a/f.txt\n+++ b/f.txt\n@@ -2,2 +2,2 @@\n a\n-b\n+B\n"
    with pytest.raises(InvalidCommand, match="occur 2 times"):
        _apply(numbered_ambiguous, repeated)
    missing = "--- a/f.txt\n+++ b/f.txt\n@@ -3,2 +3,2 @@\n line 3\n-not there\n+x\n"
    with pytest.raises(InvalidCommand, match="patch conflict in f.txt at line 4"):
        _apply(missing)
    bare_missing = "--- a/f.txt\n+++ b/f.txt\n@@\n-not there\n+x\n"
    with pytest.raises(InvalidCommand, match="not found"):
        _apply(bare_missing)


def test_blank_context_lines_without_their_space_are_context() -> None:
    source = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
    patch = (
        "--- a/f.py\n+++ b/f.py\n@@ -1,6 +1,6 @@\n def a():\n     return 1\n\n\n"
        " def b():\n-    return 2\n+    return 3\n\n"
    )
    (file_patch,) = parse_unified_patch(patch)
    assert apply_hunks(source, file_patch.hunks, path="f.py") == source.replace("return 2", "return 3")


def test_a_pure_insertion_goes_after_the_stated_line() -> None:
    patch = "--- a/f.txt\n+++ b/f.txt\n@@ -3,0 +4,1 @@\n+after three\n"
    assert _apply(patch) == SOURCE.replace("line 3\n", "line 3\nafter three\n")


def test_invalid_hunk_lines_are_still_refused() -> None:
    with pytest.raises(InvalidCommand, match="invalid patch hunk line"):
        parse_unified_patch("--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n*bad\n")
