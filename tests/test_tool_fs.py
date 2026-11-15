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


def test_edit_requires_read(ctx):
    (ctx.workspace / "file").write_text("a")
    assert edit(ctx, "a", "b").is_error


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
