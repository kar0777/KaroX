from pathlib import Path

import pytest

from karox6.tools.patch import PatchError, apply_patch


def patch(body: str) -> str:
    return f"*** Begin Patch\n{body}\n*** End Patch"


@pytest.mark.parametrize("wrapper", [0, 1, 2, 3])
def test_add_wrappers(tmp_path: Path, wrapper: int) -> None:
    body = "*** Add File: dir/a\n+one\n+two"
    text = [
        patch(body),
        body,
        f"<<'EOF'\n{patch(body)}\nEOF",
        f'apply_patch <<"EOF"\n{patch(body)}\nEOF',
    ][wrapper]
    result = apply_patch(text, tmp_path)
    assert (tmp_path / "dir/a").read_bytes() == b"one\ntwo\n"
    assert result.changes[0].added == 2


@pytest.mark.parametrize(
    ("original", "old"),
    [
        ("hello", "hello"),
        ("hello   ", "hello"),
        ("  hello  ", "hello"),
        ("‘hello’ — world", "'hello' - world"),
        ("hello\u00a0world", "hello world"),
    ],
)
def test_fuzzy(tmp_path: Path, original: str, old: str) -> None:
    (tmp_path / "a").write_text(original + "\n", encoding="utf-8")
    apply_patch(patch(f"*** Update File: a\n-{old}\n+new"), tmp_path)
    assert (tmp_path / "a").read_text(encoding="utf-8") == "new\n"


@pytest.mark.parametrize("ending", [b"\n", b"\r\n", b""])
def test_endings(tmp_path: Path, ending: bytes) -> None:
    (tmp_path / "a").write_bytes(b"old" + ending)
    apply_patch(patch("*** Update File: a\n-old\n+new"), tmp_path)
    assert (tmp_path / "a").read_bytes() == b"new" + ending


@pytest.mark.parametrize("header", ["@@", "@@ section", "@@ section\n@@ inner"])
def test_headers(tmp_path: Path, header: str) -> None:
    (tmp_path / "a").write_text("section\ninner\nold\n", encoding="utf-8")
    apply_patch(patch(f"*** Update File: a\n{header}\n-old\n+new"), tmp_path)
    assert (tmp_path / "a").read_text(encoding="utf-8").endswith("new\n")


def test_hunks_and_eof(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("one\nx\none\n", encoding="utf-8")
    apply_patch(
        patch("*** Update File: a\n@@\n-one\n+first\n@@\n-one\n+last\n*** End of File"), tmp_path
    )
    assert (tmp_path / "a").read_text(encoding="utf-8") == "first\nx\nlast\n"


def test_multiple_operations(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("old\n", encoding="utf-8")
    (tmp_path / "gone").write_text("bye\n", encoding="utf-8")
    result = apply_patch(
        patch(
            "*** Update File: a\n*** Move to: dir/b\n-old\n+new\n"
            "*** Delete File: gone\n*** Add File: c\n+ok"
        ),
        tmp_path,
    )
    assert not (tmp_path / "a").exists()
    assert not (tmp_path / "gone").exists()
    assert (tmp_path / "dir/b").read_text(encoding="utf-8") == "new\n"
    assert [c.op for c in result.changes] == ["move", "delete", "add"]
    assert "M a -> dir/b (+1 -1)" in result.summary()
    assert "D gone" in result.summary()
    assert "A c (+1)" in result.summary()


def test_preflight_atomic(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("old\n", encoding="utf-8")
    (tmp_path / "b").write_text("other\n", encoding="utf-8")
    with pytest.raises(PatchError, match="re-read"):
        apply_patch(
            patch("*** Update File: a\n-old\n+new\n*** Update File: b\n-missing\n+x"), tmp_path
        )
    assert (tmp_path / "a").read_text(encoding="utf-8") == "old\n"


@pytest.mark.parametrize(
    "body",
    [
        "",
        "*** Add File: a\n+x",
        "*** Update File: missing\n-x\n+y",
        "*** Delete File: missing",
        "*** Add File: ../outside\n+x",
        "*** Update File: binary\n-x\n+y",
        "*** Delete File: binary",
        "*** Update File: a\n*** Move to: a\n-old\n+new",
        "*** Update File: a\n@@ missing\n-old\n+new",
        "*** Add File: a\ninvalid",
        "*** Update File: a",
        "*** Delete File: a\n+x",
        "nonsense",
        "*** Update File: a\n*** End of File",
        "*** Add File: \n+x",
    ],
)
def test_failures(tmp_path: Path, body: str) -> None:
    (tmp_path / "a").write_text("old\n", encoding="utf-8")
    (tmp_path / "binary").write_bytes(b"\0binary")
    with pytest.raises(PatchError):
        apply_patch(patch(body), tmp_path)
    assert (tmp_path / "a").read_text(encoding="utf-8") == "old\n"


def test_state_across_operations(tmp_path: Path) -> None:
    apply_patch(patch("*** Add File: a\n+old\n*** Update File: a\n-old\n+new"), tmp_path)
    assert (tmp_path / "a").read_text(encoding="utf-8") == "new\n"


def test_absolute_path(tmp_path: Path) -> None:
    apply_patch(patch(f"*** Add File: {tmp_path / 'a'}\n+x"), tmp_path)
    assert (tmp_path / "a").exists()


def test_context_empty(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("a\n\nb\n", encoding="utf-8")
    apply_patch(patch("*** Update File: a\n a\n\n-b\n+c"), tmp_path)
    assert (tmp_path / "a").read_text(encoding="utf-8") == "a\n\nc\n"


def test_fuzzy_context_preserved(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"  context  \nold\n")
    apply_patch(patch("*** Update File: a\n context\n-old\n+new"), tmp_path)
    assert (tmp_path / "a").read_bytes() == b"  context  \nnew\n"


def test_eof_picks_last_match(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"old\nold\n")
    apply_patch(patch("*** Update File: a\n-old\n+new\n*** End of File"), tmp_path)
    assert (tmp_path / "a").read_bytes() == b"old\nnew\n"


def test_markers_with_trailing_spaces(tmp_path: Path) -> None:
    apply_patch("*** Begin Patch   \n*** Add File: a   \n+x\n*** End Patch  ", tmp_path)
    assert (tmp_path / "a").read_bytes() == b"x\n"


def test_commit_failure_reports_applied(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from karox6.tools import patch as patch_module

    original = patch_module.write_atomic

    def failing_write(path: Path, data: bytes) -> None:
        if path.name == "b":
            raise OSError("write failed")
        original(path, data)

    monkeypatch.setattr(patch_module, "write_atomic", failing_write)
    with pytest.raises(PatchError, match="applied: a"):
        apply_patch(patch("*** Add File: a\n+x\n*** Add File: b\n+y"), tmp_path)
    assert (tmp_path / "a").read_bytes() == b"x\n"


def test_first_line_of_a_utf8_bom_file_matches_and_the_bom_is_kept(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"\xef\xbb\xbfold\nnext\n")
    apply_patch(patch("*** Update File: a\n-old\n+new"), tmp_path)
    assert (tmp_path / "a").read_bytes() == b"\xef\xbb\xbfnew\nnext\n"
