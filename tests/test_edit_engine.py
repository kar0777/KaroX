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


def test_overlapping_matches_are_not_read_as_unique() -> None:
    # "aa" starts at 0 and at 1 in "aaa"; the first used to be edited unasked.
    text, failures = apply_edits("aaa\n", [EditOp(old="aa", new="X")])
    assert text == "aaa\n" and failures and failures[0].reason == "ambiguous"
    text, failures = apply_edits("aaa\n", [EditOp(old="aa", new="X", occurrence=1)])
    assert failures == [] and text == "Xa\n"


def test_a_reindented_match_still_obeys_its_occurrence_number() -> None:
    text, failures = apply_edits("  a\n  b\n", [EditOp(old="a\nb", new="x\ny", occurrence=2)])
    assert text == "  a\n  b\n" and failures and failures[0].reason == "not_found"
    text, failures = apply_edits("  a\n  b\n", [EditOp(old="a\nb", new="x\ny", occurrence=1)])
    assert failures == [] and text == "  x\n  y\n"


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


def test_read_search_and_edit_count_the_same_lines_across_form_feeds(tmp_path) -> None:
    from karox.edit_engine import _closest_block, _nearest_lines
    from karox.text_lines import physical_lines

    text = "a = 1\n\fb = 2\nc = '\u2028 x\x85  '\nd = 4\n"
    core = _core(tmp_path, text)
    window = core._read_lines({"path": "mod.py", "start": 4, "count": 1}, 30)
    assert window["lines"] == ["d = 4"] and window["total_lines"] == 4
    found = core._search({"query": "d = 4"}, 30)
    assert [(hit["path"], hit["line"]) for hit in found["matches"]] == [("mod.py", 4)]
    assert _nearest_lines(text, "d = 5\n")[0][0] == 4
    block = _closest_block(text, "c = '\u2028 x\x85  '\nd = 5\n")
    assert block is not None and block[0] == 3
    # A lone carriage return is content, not a line ending.
    assert physical_lines("abc\r") == ["abc\r"] and physical_lines("a\r\nb") == ["a", "b"]


def test_ripgrep_matches_on_a_latin1_line_are_kept(tmp_path) -> None:
    core = _core(tmp_path, "")
    (core.repository / "legacy.py").write_bytes(b"# caf\xe9 needle\n")
    found = core._search({"query": "needle"}, 30)
    assert [(hit["path"], hit["line"], hit["text"]) for hit in found["matches"]] == [
        ("legacy.py", 1, "# café needle")
    ]


def test_a_latin1_file_is_read_and_edited_byte_for_byte(tmp_path) -> None:
    core = _core(tmp_path, "")
    target = core.repository / "mod.py"
    target.write_bytes("# café\nx = 1\n".encode("latin-1"))
    window = core._read_lines({"path": "mod.py"}, 30)
    assert window["encoding"] == "latin-1" and window["lines"][0] == "# café"
    result = core._edit_file({"path": "mod.py", "old_string": "x = 1", "new_string": "x = 2"}, 30)
    assert result["changed"] is True
    assert target.read_bytes() == "# café\nx = 2\n".encode("latin-1")


def test_a_latin1_edit_refuses_text_the_encoding_cannot_hold(tmp_path) -> None:
    import pytest

    from karox.core import InvalidCommand

    core = _core(tmp_path, "")
    target = core.repository / "mod.py"
    target.write_bytes(b"# caf\xe9\nx = 1\n")
    with pytest.raises(InvalidCommand):
        core._edit_file({"path": "mod.py", "old_string": "x = 1", "new_string": "x = '€'"}, 30)
    assert target.read_bytes() == b"# caf\xe9\nx = 1\n"


def test_history_lookups_accept_an_absolute_path_inside_the_repository(tmp_path) -> None:
    import subprocess

    core = _core(tmp_path, "x = 1\n")
    git = ["git", "-C", str(core.repository), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "mod.py"], check=True)
    subprocess.run([*git, "commit", "-qm", "one"], check=True)
    absolute = str(core.repository / "mod.py")
    assert "one" in core._git_log({"path": absolute}, 30)["stdout"]
    assert core._read_lines({"path": absolute, "rev": "HEAD"}, 30)["lines"] == ["x = 1"]


def test_binary_data_is_still_refused(tmp_path) -> None:
    import pytest

    from karox.core import CoreError

    core = _core(tmp_path, "")
    (core.repository / "blob.bin").write_bytes(b"\xff\x00\xfe")
    with pytest.raises(CoreError):
        core._read_lines({"path": "blob.bin"}, 30)
