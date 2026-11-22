"""Workspace snapshots behind /undo: byte-exact, private, and never over newer work."""

import shutil
import subprocess

import pytest

from karox6.core.checkpoint import Checkpoints

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def git(ws, *args):
    return subprocess.run(
        ["git", *args], cwd=ws, check=True, capture_output=True, stdin=subprocess.DEVNULL
    ).stdout.decode()


@pytest.fixture
def project(tmp_path):
    ws = tmp_path / "ws"
    (ws / "sub").mkdir(parents=True)
    (ws / "crlf.txt").write_bytes(b"one\r\ntwo\r\n")
    (ws / "data.bin").write_bytes(bytes(range(256)))
    (ws / "sub" / "module.py").write_text("x = 1\n", encoding="utf-8")
    (ws / "кириллица.txt").write_text("привет\n", encoding="utf-8")
    return ws, Checkpoints(tmp_path / "home", ws)


def test_undo_and_redo_move_only_what_the_turn_changed(project):
    ws, store = project
    before = store.snapshot()
    assert before is not None
    # a turn: one file edited, one deleted, one created in a new folder
    (ws / "crlf.txt").write_bytes(b"one\r\nTWO\r\n")
    (ws / "sub" / "module.py").unlink()
    (ws / "new" / "deep").mkdir(parents=True)
    (ws / "new" / "deep" / "made.txt").write_text("created\n", encoding="utf-8")
    after = store.snapshot()
    assert sorted(store.changes(before, after)) == [
        ("A", "new/deep/made.txt"),
        ("D", "sub/module.py"),
        ("M", "crlf.txt"),
    ]
    changed, skipped = store.move(after, before, store.snapshot())
    assert changed == ["crlf.txt", "new/deep/made.txt", "sub/module.py"]
    assert skipped == []
    assert (ws / "crlf.txt").read_bytes() == b"one\r\ntwo\r\n"  # line endings as they were
    assert (ws / "sub" / "module.py").read_text(encoding="utf-8") == "x = 1\n"
    assert not (ws / "new").exists()  # the folders the turn made are gone too
    assert (ws / "data.bin").read_bytes() == bytes(range(256))
    assert (ws / "кириллица.txt").read_text(encoding="utf-8") == "привет\n"
    # redo puts the turn's result back
    changed, skipped = store.move(before, after, store.snapshot())
    assert (changed, skipped) == (["crlf.txt", "new/deep/made.txt", "sub/module.py"], [])
    assert (ws / "crlf.txt").read_bytes() == b"one\r\nTWO\r\n"
    assert not (ws / "sub" / "module.py").exists()
    assert (ws / "new" / "deep" / "made.txt").read_text(encoding="utf-8") == "created\n"


def test_a_file_changed_after_the_turn_is_never_overwritten(project):
    ws, store = project
    before = store.snapshot()
    (ws / "crlf.txt").write_bytes(b"agent edit\n")
    (ws / "sub" / "module.py").write_text("x = 2\n", encoding="utf-8")
    after = store.snapshot()
    # the user keeps working on one of the files in their editor
    (ws / "crlf.txt").write_bytes(b"agent edit\nplus my own line\n")
    (ws / "mine.txt").write_text("unrelated new file\n", encoding="utf-8")
    changed, skipped = store.move(after, before, store.snapshot())
    assert changed == ["sub/module.py"]
    assert skipped == ["crlf.txt"]
    assert (ws / "crlf.txt").read_bytes() == b"agent edit\nplus my own line\n"
    assert (ws / "sub" / "module.py").read_text(encoding="utf-8") == "x = 1\n"
    assert (ws / "mine.txt").exists()  # not part of the turn: untouched


def test_the_users_repository_is_not_touched(project):
    ws, store = project
    git(ws, "init", "-q")
    git(ws, "add", "crlf.txt")
    (ws / ".gitignore").write_text("secret.env\n", encoding="utf-8")
    (ws / "secret.env").write_text("KEY=1\n", encoding="utf-8")
    status = git(ws, "status", "--porcelain=v1")
    index = (ws / ".git" / "index").read_bytes()
    before = store.snapshot()
    (ws / "crlf.txt").write_bytes(b"changed\n")
    (ws / "secret.env").write_text("KEY=2\n", encoding="utf-8")
    after = store.snapshot()
    assert store.changes(before, after) == [("M", "crlf.txt")]  # ignored files: not recorded
    store.move(after, before, store.snapshot())
    assert (ws / "crlf.txt").read_bytes() == b"one\r\ntwo\r\n"
    assert (ws / "secret.env").read_text(encoding="utf-8") == "KEY=2\n"
    # Checked before any git command of this test runs again: `git status` itself
    # refreshes the index file.
    assert (ws / ".git" / "index").read_bytes() == index  # same staged state, byte for byte
    assert git(ws, "status", "--porcelain=v1") == status
    assert store.git_dir.parent == ws.parent / "home" / "snapshots"


def test_dependency_folders_and_the_projects_gitattributes_do_not_matter(project):
    ws, store = project
    (ws / ".gitattributes").write_text("* text=auto eol=lf\n", encoding="utf-8")
    (ws / "node_modules" / "pkg").mkdir(parents=True)
    (ws / "node_modules" / "pkg" / "index.js").write_text("big\n", encoding="utf-8")
    before = store.snapshot()
    (ws / "node_modules" / "pkg" / "index.js").write_text("changed\n", encoding="utf-8")
    (ws / "crlf.txt").write_bytes(b"x\r\ny\n")  # mixed endings must survive exactly
    after = store.snapshot()
    assert store.changes(before, after) == [("M", "crlf.txt")]
    (ws / "crlf.txt").write_bytes(b"later\n")
    later = store.snapshot()
    store.move(later, after, store.snapshot())
    assert (ws / "crlf.txt").read_bytes() == b"x\r\ny\n"


def test_without_git_snapshots_switch_off_with_a_reason(project, monkeypatch):
    _, store = project
    monkeypatch.setenv("PATH", "")
    assert store.snapshot() is None
    assert store.disabled == "git is not installed"
    assert store.snapshot() is None  # stays off: no retry on every turn
