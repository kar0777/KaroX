import asyncio
import os
import shutil
import subprocess

import pytest

from karox6.tools.search import GlobTool, GrepTool, glob_regex, ripgrep


@pytest.fixture(params=["python", "rg"])
def backend(request, monkeypatch):
    # The temporary workspace may sit inside the checkout; treat it as non-git
    # except in the test that explicitly initializes its own repository.
    if not request.node.name.startswith("test_glob_gitignore"):
        monkeypatch.setattr("karox6.tools.search._git_ls_files", lambda root: None)
    if request.param == "python":
        monkeypatch.setenv("KAROX_NO_RG", "1")
    else:
        monkeypatch.delenv("KAROX_NO_RG", raising=False)
        if not shutil.which("rg"):
            pytest.skip("ripgrep unavailable")
    ripgrep.cache_clear()
    yield request.param
    ripgrep.cache_clear()


def call(tool, ctx, **args):
    return asyncio.run(tool.run(args, ctx))


@pytest.mark.parametrize(
    ("pattern", "path", "matches"),
    [
        ("*.py", "a/b.py", True),
        ("*.py", "a/b.txt", False),
        ("src/*.py", "src/a.py", True),
        ("src/*.py", "src/sub/a.py", False),
        ("**/test_*.py", "test_a.py", True),
        ("**/test_*.py", "x/test_a.py", True),
        ("*.{a,b}", "x.b", True),
        ("*.{a,b}", "x.c", False),
        ("file?.py", "file1.py", True),
        ("file?.py", "file12.py", False),
        ("[ab].py", "a.py", True),
        ("[!ab].py", "c.py", True),
    ],
)
def test_glob_regex(pattern, path, matches):
    assert bool(glob_regex(pattern).match(path)) is matches


def test_glob_order_and_path(ctx, backend):
    sub = ctx.workspace / "sub"
    sub.mkdir()
    for i in range(3):
        path = sub / f"{i}.py"
        path.touch()
        os.utime(path, (1000 + i, 1000 + i))
    result = call(GlobTool(), ctx, pattern="*.py", path="sub")
    assert result.content.splitlines() == ["sub/2.py", "sub/1.py", "sub/0.py"]


def test_glob_empty(ctx, backend):
    assert call(GlobTool(), ctx, pattern="*.xyz").content == "No files found."


def test_glob_cap(ctx, backend):
    for i in range(105):
        (ctx.workspace / f"{i:03}.txt").touch()
    result = call(GlobTool(), ctx, pattern="*.txt")
    assert "105 files matched" in result.content
    assert "showing the 100 newest" in result.content
    spill = list(ctx.spill_dir.glob("*-glob.txt"))
    assert len(spill) == 1
    assert len(spill[0].read_text().splitlines()) == 105


def test_glob_skips_directories(ctx, backend, monkeypatch):
    monkeypatch.setattr("karox6.tools.search._git_ls_files", lambda root: None)
    for name in (".git", "node_modules", ".venv"):
        directory = ctx.workspace / name
        directory.mkdir()
        (directory / "hidden.py").touch()
    assert call(GlobTool(), ctx, pattern="*.py").content == "No files found."


def test_glob_gitignore(ctx, backend):
    if not shutil.which("git"):
        pytest.skip("git unavailable")
    subprocess.run(["git", "init", "-q"], cwd=ctx.workspace, check=True, capture_output=True)
    (ctx.workspace / ".gitignore").write_text("ignored*.txt\n")
    (ctx.workspace / "ignored.txt").touch()
    (ctx.workspace / "visible.txt").touch()
    result = call(GlobTool(), ctx, pattern="*.txt")
    assert "visible.txt" in result.content
    assert "ignored.txt" not in result.content
    assert "ignored.txt" in call(GlobTool(), ctx, pattern="ignored*.txt").content


def test_grep_grouped_include(ctx, backend):
    (ctx.workspace / "a.py").write_text("no\nHello\nhello\n")
    (ctx.workspace / "b.txt").write_text("hello\n")
    result = call(GrepTool(), ctx, pattern="(?i)hello", include="*.py")
    assert result.content == "a.py\nLine 2: Hello\nLine 3: hello"


def test_grep_invalid(ctx, backend):
    assert call(GrepTool(), ctx, pattern="[").is_error


def test_grep_file(ctx, backend):
    (ctx.workspace / "a.txt").write_text("hello\n")
    assert call(GrepTool(), ctx, pattern="hello", path="a.txt").content == "a.txt\nLine 1: hello"


def test_grep_binary(ctx, backend):
    (ctx.workspace / "binary").write_bytes(b"hello\0world")
    assert call(GrepTool(), ctx, pattern="hello").content == "No matches found."


def test_grep_cap(ctx, backend):
    (ctx.workspace / "a.txt").write_text("hit\n" * 260)
    result = call(GrepTool(), ctx, pattern="hit")
    assert "260 matches" in result.content
    assert "Line 250: hit" in result.content
    assert "Line 251: hit" not in result.content
    spill = list(ctx.spill_dir.glob("*-grep.txt"))
    assert len(spill) == 1
    assert "Line 260: hit" in spill[0].read_text()


def test_grep_empty(ctx, backend):
    (ctx.workspace / "a").write_text("nothing")
    assert call(GrepTool(), ctx, pattern="missing").content == "No matches found."
