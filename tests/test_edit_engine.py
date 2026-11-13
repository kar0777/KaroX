"""Edits found by the 2026-11-13 audit: deletions and the whitespace-folded retry."""

from __future__ import annotations

from karox.edit_engine import EditOp, apply_edits


def test_deleting_a_whole_line_leaves_no_blank_line() -> None:
    text, failures = apply_edits("a = 1\nimport os\nb = 2\n", [EditOp(old="import os\n", new="")])
    assert failures == [] and text == "a = 1\nb = 2\n"


def test_a_line_replaced_without_its_newline_still_ends_the_line() -> None:
    text, failures = apply_edits("a = 1\nb = 2\n", [EditOp(old="a = 1\n", new="a = 3")])
    assert failures == [] and text == "a = 3\nb = 2\n"


def test_the_folded_retry_never_matches_inside_a_word() -> None:
    text, failures = apply_edits("    max_items = 10\n", [EditOp(old="items = 1 ", new="count = 5")])
    assert text == "    max_items = 10\n"
    assert failures and failures[0].reason == "not_found"


def test_the_folded_retry_still_bridges_crlf_and_trailing_blanks() -> None:
    text, failures = apply_edits(
        "x = 1   \r\ny = 2\r\n", [EditOp(old="x = 1\ny = 2\n", new="x = 3\ny = 4\n")]
    )
    assert failures == [] and text == "x = 3\r\ny = 4\r\n"
