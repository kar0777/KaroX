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


def test_the_codex_patch_format_updates_adds_moves_and_deletes() -> None:
    """GPT-family models write *** Begin Patch; it was refused as "invalid patch hunk line"."""

    patch = (
        "*** Begin Patch\n"
        "*** Update File: f.txt\n"
        "@@\n line 7\n-line 8\n+LINE 8\n"
        "@@ line 14\n line 15\n+inserted\n line 16\n"
        "*** Add File: new.txt\n+first\n+\n+third\n\n"
        "*** Update File: old.txt\n*** Move to: moved.txt\n"
        "*** Delete File: gone.txt\n"
        "*** End Patch\n"
    )
    update, added, moved, deleted = parse_unified_patch(patch)
    assert (update.old_path, update.new_path) == ("f.txt", "f.txt")
    expected = SOURCE.replace("line 8\n", "LINE 8\n").replace("line 15\n", "line 15\ninserted\n")
    assert apply_hunks(SOURCE, update.hunks, path="f.txt") == expected
    assert added.old_path is None and apply_hunks("", added.hunks, path="new.txt") == "first\n\nthird\n"
    assert (moved.old_path, moved.new_path, moved.hunks) == ("old.txt", "moved.txt", ())
    (blank,) = parse_unified_patch("*** Begin Patch\n*** Add File: blank.txt\n+\n*** End Patch\n")
    assert apply_hunks("", blank.hunks, path="blank.txt") == "\n"
    assert (deleted.old_path, deleted.new_path) == ("gone.txt", None)


def test_a_codex_anchor_picks_the_repeated_block_after_it() -> None:
    source = "def a():\n    x = 1\n    return x\n\ndef b():\n    x = 1\n    return x\n"
    patch = (
        "*** Begin Patch\n*** Update File: f.py\n"
        "@@ def b():\n     x = 1\n-    return x\n+    return x + 1\n"
        "*** End Patch\n"
    )
    (file_patch,) = parse_unified_patch(patch)
    updated = apply_hunks(source, file_patch.hunks, path="f.py")
    assert updated == "def a():\n    x = 1\n    return x\n\ndef b():\n    x = 1\n    return x + 1\n"
    unanchored = patch.replace("@@ def b():", "@@")
    (file_patch,) = parse_unified_patch(unanchored)
    with pytest.raises(InvalidCommand, match="matches 2 places"):
        apply_hunks(source, file_patch.hunks, path="f.py")


def test_codex_end_of_file_and_bare_additions_after_an_anchor() -> None:
    patch = "*** Begin Patch\n*** Update File: f.txt\n@@\n+appended\n*** End of File\n*** End Patch\n"
    (file_patch,) = parse_unified_patch(patch)
    assert apply_hunks(SOURCE, file_patch.hunks, path="f.txt") == SOURCE + "appended\n"
    patch = "*** Begin Patch\n*** Update File: f.txt\n@@ line 4\n+after four\n*** End Patch\n"
    (file_patch,) = parse_unified_patch(patch)
    assert apply_hunks(SOURCE, file_patch.hunks, path="f.txt") == SOURCE.replace(
        "line 4\n", "line 4\nafter four\n"
    )


def test_a_unified_diff_inside_the_codex_wrapper_still_applies() -> None:
    patch = "*** Begin Patch\n--- a/f.txt\n+++ b/f.txt\n@@ -8 +8 @@\n-line 8\n+LINE 8\n*** End Patch\n"
    assert _apply(patch) == SOURCE.replace("line 8\n", "LINE 8\n")


def test_codex_sections_still_verify_every_line() -> None:
    with pytest.raises(InvalidCommand, match="starts with '\\+'"):
        parse_unified_patch("*** Begin Patch\n*** Add File: a.txt\nno plus\n*** End Patch\n")
    with pytest.raises(InvalidCommand, match="no hunks"):
        parse_unified_patch("*** Begin Patch\n*** Update File: a.txt\n*** End Patch\n")
    with pytest.raises(InvalidCommand, match="outside a file section"):
        parse_unified_patch("*** Begin Patch\nstray\n*** Update File: a.txt\n@@\n-a\n+b\n")
    (file_patch,) = parse_unified_patch("*** Begin Patch\n*** Update File: f.txt\n@@\n-not there\n+x\n")
    with pytest.raises(InvalidCommand, match="not found"):
        apply_hunks(SOURCE, file_patch.hunks, path="f.txt")


def test_form_feeds_and_vertical_tabs_survive_a_patch() -> None:
    # splitlines() broke at \f and \v, so they came back as newlines.
    source = "a\x0bb\nfoo\n\x0c\ndef f():\n    pass\n"
    patch = "--- a/f.txt\n+++ b/f.txt\n@@ -2 +2 @@\n-foo\n+bar\n"
    assert _apply(patch, source) == source.replace("foo", "bar")


def test_removing_a_line_that_starts_with_two_dashes() -> None:
    # "--- x" in a hunk removes "-- x" (SQL, Lua); it is no file header.
    source = "-- comment\nselect 1;\n"
    patch = "--- a/q.sql\n+++ b/q.sql\n@@ -1,2 +1,2 @@\n--- comment\n+-- note\n select 1;\n"
    assert _apply(patch, source) == "-- note\nselect 1;\n"
    two_files = patch + "--- a/r.sql\n+++ b/r.sql\n@@ -1 +1 @@\n-x\n+y\n"
    assert [p.new_path for p in parse_unified_patch(two_files)] == ["q.sql", "r.sql"]


def test_mixed_line_endings_change_only_the_patched_lines() -> None:
    source = "a\r\nb\nc\r\nd\n"
    patch = "--- a/f.txt\n+++ b/f.txt\n@@ -2 +2,2 @@\n-b\n+B\n+B2\n"
    assert _apply(patch, source) == "a\r\nB\nB2\nc\r\nd\n"
    insert = "--- a/f.txt\n+++ b/f.txt\n@@ -3 +3,2 @@\n+new\n c\n"
    assert _apply(insert, source) == "a\r\nb\nnew\r\nc\r\nd\n"
