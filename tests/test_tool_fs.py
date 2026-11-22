import asyncio
import os

import pytest

from karox6.tools.fs import EditTool, ReadTool, WriteTool


def call(tool, ctx, **args):
    return asyncio.run(tool.run(args, ctx))


def read(ctx, **args):
    return call(ReadTool(), ctx, file_path="file", **args)


def edit(ctx, old, new, **args):
    return call(EditTool(), ctx, file_path="file", old_string=old, new_string=new, **args)


def test_numbering_and_footer(ctx):
    (ctx.workspace / "file").write_text("one\ntwo\nthree\n")
    result = read(ctx, offset=2, limit=1)
    assert "2: two" in result.content
    assert "Use offset=3" in result.content
    assert "End of file - 3 lines" in read(ctx, offset=3).content


def test_long_line(ctx):
    (ctx.workspace / "file").write_text("x" * 3000)
    assert "line truncated to 2000 chars" in read(ctx).content


def test_size_cap(ctx):
    (ctx.workspace / "file").write_text(("x" * 1000 + "\n") * 100)
    result = read(ctx)
    assert result.meta["lines"][1] < 100
    assert "Use offset=" in result.content
    assert len(result.content.encode()) < 52 * 1024


def test_bom_crlf(ctx):
    (ctx.workspace / "file").write_bytes(b"\xef\xbb\xbfhello\r\nworld\r\n")
    result = read(ctx)
    assert "1: hello\n2: world" in result.content


def test_empty(ctx):
    (ctx.workspace / "file").touch()
    assert "Empty file" in read(ctx).content


def test_missing(ctx):
    assert read(ctx).is_error


def test_binary(ctx):
    (ctx.workspace / "file").write_bytes(b"x\0y")
    assert "binary" in read(ctx).content


def test_directory(ctx):
    (ctx.workspace / "dir").mkdir()
    (ctx.workspace / "dir" / "a").touch()
    result = call(ReadTool(), ctx, file_path="dir")
    assert "directory" in result.content
    assert "a" in result.content


def test_offset_past_end(ctx):
    (ctx.workspace / "file").write_text("one")
    assert read(ctx, offset=2).is_error


def test_read_observes(ctx):
    path = ctx.workspace / "file"
    path.write_text("one")
    read(ctx)
    assert ctx.files.check_fresh(path) is None


def test_create_parents(ctx):
    result = call(WriteTool(), ctx, file_path="nested/file", content="hello")
    assert "Created" in result.content
    assert (ctx.workspace / "nested/file").read_text() == "hello"


def test_write_requires_read(ctx):
    (ctx.workspace / "file").write_text("old")
    assert call(WriteTool(), ctx, file_path="file", content="new").is_error


def test_write_after_read(ctx):
    path = ctx.workspace / "file"
    path.write_text("old")
    read(ctx)
    result = call(WriteTool(), ctx, file_path="file", content="new")
    assert "Updated" in result.content
    assert path.read_text() == "new"


def test_write_stale(ctx):
    path = ctx.workspace / "file"
    path.write_text("old")
    read(ctx)
    path.write_text("newer")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    assert "changed on disk" in call(WriteTool(), ctx, file_path="file", content="x").content


def test_write_crlf(ctx):
    path = ctx.workspace / "file"
    path.write_bytes(b"a\r\nb\r\n")
    read(ctx)
    call(WriteTool(), ctx, file_path="file", content="c\nd\n")
    assert path.read_bytes() == b"c\r\nd\r\n"


def test_edit_exact(ctx):
    path = ctx.workspace / "file"
    path.write_text("one\ntwo\n")
    read(ctx)
    assert not edit(ctx, "one", "changed").is_error
    assert path.read_text() == "changed\ntwo\n"


def test_multiple(ctx):
    (ctx.workspace / "file").write_text("x\ny\nx\n")
    read(ctx)
    result = edit(ctx, "x", "z")
    assert result.is_error
    assert "lines 1, 3" in result.content


def test_replace_all(ctx):
    path = ctx.workspace / "file"
    path.write_text("x\nx\n")
    read(ctx)
    assert not edit(ctx, "x", "z", replace_all=True).is_error
    assert path.read_text() == "z\nz\n"


def test_not_found_hint(ctx):
    (ctx.workspace / "file").write_text("first\nsecond\n")
    read(ctx)
    assert "first line occurs at line(s) 1" in edit(ctx, "first\nwrong", "x").content


@pytest.mark.parametrize(("old", "new", "message"), [("a", "a", "identical"), ("", "x", "empty")])
def test_invalid_edit(ctx, old, new, message):
    (ctx.workspace / "file").write_text("a")
    assert message in edit(ctx, old, new).content


def test_edit_unread_exact_match(ctx):
    path = ctx.workspace / "file"
    path.write_text("a\nb\n")  # seen through shell output, never through read
    result = edit(ctx, "a", "c")
    assert not result.is_error
    assert path.read_text() == "c\nb\n"


def test_edit_unread_limits(ctx):
    path = ctx.workspace / "file"
    path.write_text("id = 1\nid = 2\n")
    blind = edit(ctx, "id", "k", replace_all=True)
    assert blind.is_error
    assert "2 places" in blind.content
    assert not edit(ctx, "id = 1", "id = 3").is_error  # exact and unique: fine
    # One exact edit is not a full picture: replacing the whole file still needs a read.
    assert "has not been read" in call(WriteTool(), ctx, file_path="file", content="x").content
    read(ctx)
    assert not call(WriteTool(), ctx, file_path="file", content="x").is_error


def test_edit_unread_needs_exact_match(ctx):
    path = ctx.workspace / "file"
    path.write_text("a  \nb  \n")
    result = edit(ctx, "a\nb", "c\nd")  # only a trailing-whitespace match
    assert result.is_error
    assert "has not been read" in result.content
    assert path.read_text() == "a  \nb  \n"


def test_edit_stale_after_read(ctx):
    path = ctx.workspace / "file"
    path.write_text("a")
    read(ctx)
    path.write_text("ab")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    result = edit(ctx, "a", "c")  # exact and unique: the stale read does not matter
    assert not result.is_error
    assert path.read_text() == "cb"


def test_edit_stale_needs_exact_match(ctx):
    path = ctx.workspace / "file"
    path.write_text("a\nb\n")
    read(ctx)
    path.write_text("a  \nb  \n")
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 2_000_000_000))
    result = edit(ctx, "a\nb", "c\nd")  # only a trailing-whitespace match
    assert result.is_error
    assert "changed on disk" in result.content
    assert path.read_text() == "a  \nb  \n"


def test_edit_crlf_bom(ctx):
    path = ctx.workspace / "file"
    path.write_bytes(b"\xef\xbb\xbfa\r\nb\r\n")
    read(ctx)
    edit(ctx, "a", "c")
    assert path.read_bytes() == b"\xef\xbb\xbfc\r\nb\r\n"


@pytest.mark.parametrize(
    ("content", "old", "new", "expected", "match"),
    [
        ("a\nb\n", "1: a\n2: b", "1: c\n2: d", "c\nd\n", "line-number"),
        ("a  \nb  \n", "a\nb", "c\nd", "c\nd\n", "trailing whitespace"),
        ("    a\n        b\n", "a\n    b", "c\n    d", "    c\n        d\n", "indentation"),
        ("“hello”\n", '"hello"', "bye", "bye\n", "Unicode punctuation"),
        ("a–b\n", "a-b", "c", "c\n", "Unicode punctuation"),
    ],
)
def test_fallback(ctx, content, old, new, expected, match):  # noqa: PLR0917
    path = ctx.workspace / "file"
    path.write_text(content, encoding="utf-8", newline="")
    read(ctx)
    result = edit(ctx, old, new)
    assert not result.is_error
    assert match in result.content
    assert path.read_text(encoding="utf-8") == expected


def test_ambiguous_fallback(ctx):
    path = ctx.workspace / "file"
    path.write_text("a  \na   \n")
    read(ctx)
    assert edit(ctx, "a    ", "b").is_error
    assert path.read_text() == "a  \na   \n"


def test_read_default_first_line(ctx):
    (ctx.workspace / "file").write_text("first\nsecond\n")
    assert "1: first" in read(ctx).content


def test_edit_missing_file(ctx):
    assert edit(ctx, "x", "y").is_error


def test_edit_shows_the_changed_region_with_new_line_numbers(ctx):
    path = ctx.workspace / "file"
    path.write_text("".join(f"line{i}\n" for i in range(1, 31)))
    read(ctx)
    result = edit(ctx, "line10\n", "line10\nnew_a\nnew_b\nnew_c\n")
    assert not result.is_error
    snippet = result.content.split("The edited region now reads:\n")[1].splitlines()
    assert snippet[0] == "8: line8"
    assert "11: new_a" in snippet
    assert "13: new_c" in snippet
    assert snippet[-1] == "16: line13"


def test_edit_snippet_caps_long_regions(ctx):
    from karox6.tools.fs import SNIPPET_MAX_LINES, changed_snippet

    after = "\n".join(f"n{i}" for i in range(100)) + "\n"
    text = changed_snippet("x\n", after)
    assert len(text.splitlines()) == SNIPPET_MAX_LINES + 3  # blank, header, lines, ...
    assert text.endswith("...")


def test_edit_replace_all_has_no_snippet(ctx):
    (ctx.workspace / "file").write_text("x\ny\nx\n")
    read(ctx)
    result = call(
        EditTool(), ctx, file_path="file", old_string="x", new_string="z", replace_all=True
    )
    assert "edited region" not in result.content


def test_read_several_files_in_one_call(ctx):
    (ctx.workspace / "a.txt").write_text("alpha\n")
    (ctx.workspace / "b.txt").write_text("beta\n")
    result = call(ReadTool(), ctx, file_paths=["a.txt", "missing.txt", "b.txt"])
    assert not result.is_error
    text = result.content
    assert text.index("<path>a.txt</path>") < text.index("missing.txt") < text.index("b.txt")
    assert "1: alpha" in text
    assert "1: beta" in text
    assert "missing.txt does not exist" in text
    assert ctx.files.seen(ctx.workspace / "a.txt")
    assert ctx.files.seen(ctx.workspace / "b.txt")


def test_read_several_files_share_one_byte_budget(ctx):
    from karox6.tools.fs import READ_MAX_BYTES

    line = "x" * 99 + "\n"
    (ctx.workspace / "big.txt").write_text(line * (READ_MAX_BYTES // 100 + 50))
    (ctx.workspace / "big2.txt").write_text(line * 100)
    result = call(ReadTool(), ctx, file_paths=["big.txt", "big2.txt"])
    first, second = result.content.split("<path>big2.txt</path>")
    assert "Use offset=" in first
    assert "Use offset=" in second or "(Not read" in second
    assert len(result.content.encode()) < READ_MAX_BYTES + 2_000


def test_read_needs_a_path(ctx):
    assert call(ReadTool(), ctx).is_error
    assert call(ReadTool(), ctx, file_paths=["nope"]).is_error


def test_read_many_caps_files_and_counts_errors(ctx):
    from karox6.tools.fs import READ_MAX_FILES

    paths = [f"missing{i}.txt" for i in range(READ_MAX_FILES + 30)]
    result = call(ReadTool(), ctx, file_paths=paths)
    assert result.content.count("does not exist") == READ_MAX_FILES
    assert "(+10 more)" in result.content
    assert len(result.content) < 5_000


def test_read_many_rejects_offset_and_limit(ctx):
    (ctx.workspace / "a.txt").write_text("a\n")
    assert call(ReadTool(), ctx, file_paths=["a.txt"], limit=5).is_error


def test_huge_file_is_refused_with_a_hint(ctx, monkeypatch):
    from karox6.tools import fs

    monkeypatch.setattr(fs, "READ_MAX_FILE_BYTES", 10)
    (ctx.workspace / "file").write_text("x" * 100)
    result = read(ctx, limit=1)
    assert result.is_error
    assert "grep" in result.content


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no FIFOs here")
def test_fifo_is_not_read(ctx):
    os.mkfifo(ctx.workspace / "file")
    result = read(ctx)
    assert result.is_error
    assert "not a regular file" in result.content


def test_edit_keeps_bytes_that_are_not_utf8(ctx):
    (ctx.workspace / "file").write_bytes(b"name = 'caf\xe9'\nvalue = 1\n")
    read(ctx)
    result = edit(ctx, "value = 1", "value = 2")
    assert not result.is_error
    assert (ctx.workspace / "file").read_bytes() == b"name = 'caf\xe9'\nvalue = 2\n"


def test_edit_keeps_mixed_line_endings(ctx):
    (ctx.workspace / "file").write_bytes(b"a = 1\r\nb = 2\nc = 3\n")
    read(ctx)
    assert not edit(ctx, "b = 2", "b = 20").is_error
    assert (ctx.workspace / "file").read_bytes() == b"a = 1\r\nb = 20\nc = 3\n"


def test_failed_write_leaves_the_old_file(ctx, monkeypatch):
    from karox6.tools import atomic

    (ctx.workspace / "file").write_text("keep me\n")
    read(ctx)

    def boom(src, dst):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(atomic.os, "replace", boom)
    result = edit(ctx, "keep me", "changed")
    assert result.is_error
    assert (ctx.workspace / "file").read_text() == "keep me\n"
    assert [p.name for p in ctx.workspace.iterdir()] == ["file"]  # no temp file left


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes")
def test_write_keeps_the_file_mode(ctx):
    path = ctx.workspace / "file"
    path.write_text("#!/bin/sh\necho hi\n")
    path.chmod(0o755)
    read(ctx)
    assert not edit(ctx, "echo hi", "echo bye").is_error
    assert path.stat().st_mode & 0o777 == 0o755


def test_edit_next_to_latin1_bytes_shows_and_logs_clean_text(ctx):
    import json

    (ctx.workspace / "file").write_bytes(b"x = 'caf\xe9'\ny = 1\n")
    read(ctx)
    result = edit(ctx, "y = 1", "y = 2")
    assert not result.is_error
    json.dumps(result.content, ensure_ascii=False).encode("utf-8")  # no lone surrogate


def test_edit_multi_hunk_success_and_evolving_text(ctx):
    path = ctx.workspace / "file"
    path.write_text("".join(f"line{i}\n" for i in range(1, 31)))
    read(ctx)
    result = call(
        EditTool(),
        ctx,
        file_path="file",
        edits=[
            {"old_string": "line2\n", "new_string": "TWO\n"},
            {"old_string": "TWO\nline3\n", "new_string": "TWO_FINAL\nline3\n"},
            {"old_string": "line25\n", "new_string": "TWENTY_FIVE\n"},
        ],
    )
    assert not result.is_error
    text = path.read_text()
    assert "TWO_FINAL\nline3\n" in text
    assert "TWENTY_FIVE\n" in text
    assert "2: TWO_FINAL" in result.content
    assert "25: TWENTY_FIVE" in result.content
    assert result.meta["edits"] == 3
    assert result.meta["lines"] == [2, 2, 25]


def test_edit_multi_hunk_replace_all(ctx):
    path = ctx.workspace / "file"
    path.write_text("tag = 1\nfoo = 1\ntag = 2\n")
    read(ctx)
    result = call(
        EditTool(),
        ctx,
        file_path="file",
        edits=[
            {"old_string": "tag", "new_string": "key", "replace_all": True},
            {"old_string": "foo = 1", "new_string": "bar = 2"},
        ],
    )
    assert not result.is_error
    assert path.read_text() == "key = 1\nbar = 2\nkey = 2\n"


def test_edit_multi_hunk_failure_rolls_back_and_names_index(ctx):
    path = ctx.workspace / "file"
    original = "alpha\nbeta\ngamma\n"
    path.write_text(original)
    read(ctx)
    result = call(
        EditTool(),
        ctx,
        file_path="file",
        edits=[
            {"old_string": "alpha", "new_string": "ALPHA"},
            {"old_string": "missing_text", "new_string": "X"},
        ],
    )
    assert result.is_error
    assert "edits[1]" in result.content
    assert "No edits were applied" in result.content
    assert path.read_text() == original

    identical = call(
        EditTool(),
        ctx,
        file_path="file",
        edits=[
            {"old_string": "alpha", "new_string": "ALPHA"},
            {"old_string": "beta", "new_string": "beta"},
        ],
    )
    assert identical.is_error
    assert "edits[1]" in identical.content
    assert "No edits were applied" in identical.content
    assert path.read_text() == original

    empty_old = call(
        EditTool(),
        ctx,
        file_path="file",
        edits=[
            {"old_string": "alpha", "new_string": "ALPHA"},
            {"old_string": "", "new_string": "X"},
        ],
    )
    assert empty_old.is_error
    assert "edits[1]" in empty_old.content
    assert "No edits were applied" in empty_old.content
    assert path.read_text() == original


def test_edit_multi_hunk_validation_and_empty_edits(ctx):
    path = ctx.workspace / "file"
    path.write_text("a\nb\n")
    read(ctx)
    # Empty edits with no single old_string/new_string fails as missing single form.
    assert call(EditTool(), ctx, file_path="file", edits=[]).is_error
    # Passing both single form and edits fails with "No edits were applied".
    both = call(
        EditTool(),
        ctx,
        file_path="file",
        old_string="a",
        new_string="c",
        edits=[{"old_string": "b", "new_string": "d"}],
    )
    assert both.is_error
    assert "No edits were applied" in both.content
    # Malformed hunk names its index and states no edits were applied.
    bad = call(EditTool(), ctx, file_path="file", edits=[{"old_string": "a"}])
    assert bad.is_error
    assert "edits[0]" in bad.content
    assert "No edits were applied" in bad.content
    assert path.read_text() == "a\nb\n"
    # Empty edits=[] is treated as absent so the single form works unchanged.
    single = call(EditTool(), ctx, file_path="file", old_string="a", new_string="A", edits=[])
    assert not single.is_error
    assert path.read_text() == "A\nb\n"
    # Schema-filled empty old_string/new_string alongside edits works.
    multi = call(
        EditTool(),
        ctx,
        file_path="file",
        old_string="",
        new_string="",
        edits=[{"old_string": "b", "new_string": "B"}],
    )
    assert not multi.is_error
    assert path.read_text() == "A\nB\n"


def test_edit_multi_hunk_mixed_line_endings(ctx):
    path = ctx.workspace / "file"
    raw = (
        b"header1 = 1\r\nheader2 = 2\r\n"
        b"line_a = 10\nmid1 = 1\r\nmid2 = 2\r\nmid3 = 3\r\nmid4 = 4\r\n"
        b"mid5 = 5\r\nmid6 = 6\r\nmid7 = 7\r\nmid8 = 8\r\n"
        b"line_b = 20\ntail1 = 1\r\ntail2 = 2\r\n"
    )
    path.write_bytes(raw)
    read(ctx)
    result = call(
        EditTool(),
        ctx,
        file_path="file",
        edits=[
            {"old_string": "line_a = 10", "new_string": "line_a = 100"},
            {"old_string": "line_b = 20", "new_string": "line_b = 200"},
        ],
    )
    assert not result.is_error
    written = path.read_bytes()
    assert b"header1 = 1\r\n" in written
    assert b"mid4 = 4\r\n" in written
    assert b"line_a = 100\n" in written
    assert b"line_b = 200\n" in written
    assert "3: line_a = 100" in result.content
    assert "12: line_b = 200" in result.content
    assert "mid4 = 4" not in result.content


def test_edit_tool_description_mentions_edits():
    assert "`edits`" in EditTool.description
