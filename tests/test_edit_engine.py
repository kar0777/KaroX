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


CRLF = "a = 1\r\nb = 2\r\na = 1\r\nb = 2\r\n"


def test_a_folded_match_honours_occurrence_all() -> None:
    text, failures = apply_edits(CRLF, [EditOp(old="a = 1\nb = 2\n", new="c = 3\n", occurrence="all")])
    assert failures == [] and text == "c = 3\r\nc = 3\r\n"


def test_a_folded_match_honours_an_occurrence_number() -> None:
    text, failures = apply_edits(CRLF, [EditOp(old="a = 1\nb = 2\n", new="c = 3\n", occurrence=2)])
    assert failures == [] and text == "a = 1\r\nb = 2\r\nc = 3\r\n"
    # A number past the folded matches is refused, never applied to the first.
    _, failures = apply_edits("a = 1\r\nb = 2\r\n", [EditOp(old="a = 1\nb = 2\n", new="c\n", occurrence=2)])
    assert failures and failures[0].reason == "not_found"


def test_one_line_becoming_several_keeps_crlf() -> None:
    text, failures = apply_edits("a = 1\r\nb = 2\r\n", [EditOp(old="a = 1", new="a = 1\nc = 3")])
    assert failures == [] and text == "a = 1\r\nc = 3\r\nb = 2\r\n"


def _core(tmp_path, text: str):
    from karox.core_tools import ExtendedCoreRuntime
    from karox.policy import AccessProfile, CapabilityPolicy
    from karox.sessions import SessionStore

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_bytes(text.encode("utf-8"))
    return ExtendedCoreRuntime(repo, CapabilityPolicy(AccessProfile.WORKSPACE_WRITE), SessionStore(tmp_path / "s"))


def test_old_string_gets_the_folded_retry_the_edits_form_has(tmp_path) -> None:
    core = _core(tmp_path, "def f():\n    return 1   \n")
    result = core._edit_file({"path": "mod.py", "old_string": "def f():\n    return 1\n", "new_string": "def f():\n    return 2\n"}, 30)
    assert result["changed"] is True
    assert (core.repository / "mod.py").read_text(encoding="utf-8") == "def f():\n    return 2\n"


def test_a_missing_old_string_names_the_closest_lines(tmp_path) -> None:
    core = _core(tmp_path, "def f():\n    return 1\n\ndef g():\n    return 2\n")
    result = core._edit_file({"path": "mod.py", "old_string": "def h():\n    return 2\n", "new_string": "x = 1\n"}, 30)
    assert result["changed"] is False and result["error_code"] == "edit_failed"
    assert result["failures"] and result["failures"][0]["reason"] == "not_found"
