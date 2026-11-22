import asyncio
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from karox6.tools import baseline as baseline_mod
from karox6.tools.base import ToolContext
from karox6.tools.baseline import BASELINE_HEADER, BaselineTool
from karox6.tools.files import FileTracker
from karox6.tools.jobs import JobManager
from karox6.tools.registry import build_tools
from karox6.tools.shell import detect_shell

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )
    return proc


def _python(code: str) -> str:
    return f"{shlex.quote(sys.executable.replace(chr(92), '/'))} -c {shlex.quote(code)}"


def _make_ctx(tmp_path: Path, ws: Path) -> ToolContext:
    return ToolContext(
        workspace=ws,
        session_dir=tmp_path / "session",
        files=FileTracker(ws),
        jobs=JobManager(),
        shell=detect_shell(),
    )


def _init_repo(ws: Path) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    assert _git(ws, "init").returncode == 0
    assert _git(ws, "config", "user.name", "Test User").returncode == 0
    assert _git(ws, "config", "user.email", "test@example.com").returncode == 0
    assert _git(ws, "config", "core.autocrlf", "false").returncode == 0
    (ws / ".gitignore").write_bytes(b"*.ignored\n")
    (ws / "app.txt").write_bytes(b"head-version\n")
    (ws / "other.txt").write_bytes(b"other-head\n")
    assert _git(ws, "add", ".").returncode == 0
    assert _git(ws, "commit", "-m", "initial").returncode == 0


def _dirty_tree(ws: Path) -> dict[str, bytes]:
    """Create tracked (staged + unstaged), untracked, and ignored files; return expected bytes."""
    (ws / "app.txt").write_bytes(b"staged-version\n")
    assert _git(ws, "add", "app.txt").returncode == 0
    (ws / "app.txt").write_bytes(b"worktree-version\nline2\n")
    (ws / "sub").mkdir(exist_ok=True)
    (ws / "sub" / "untracked.bin").write_bytes(b"\x00\x01\xffuntracked\n")
    (ws / "keep.ignored").write_bytes(b"do-not-touch-ignored\n")
    return {
        "app.txt": b"worktree-version\nline2\n",
        "other.txt": b"other-head\n",
        "sub/untracked.bin": b"\x00\x01\xffuntracked\n",
        "keep.ignored": b"do-not-touch-ignored\n",
    }


def _assert_restored(ws: Path, expected: dict[str, bytes], pre_status: bytes) -> None:
    for rel, content in expected.items():
        assert (ws / rel).read_bytes() == content, rel
    assert _git(ws, "show", ":app.txt").stdout == b"staged-version\n"
    assert _git(ws, "status", "--porcelain=v1", "--untracked-files=all", "-z").stdout == pre_status


def _run_baseline(ctx: ToolContext, **args):
    return asyncio.run(BaselineTool(ctx.shell).run(args, ctx))


def _baseline_refs(ws: Path) -> list[str]:
    out = _git(ws, "for-each-ref", "--format=%(refname)", "refs/karox/baseline").stdout.decode()
    return [line.strip() for line in out.splitlines() if line.strip()]


def test_baseline_passing_and_failing_commands_restore_byte_identically(tmp_path):
    ws = tmp_path / "repo"
    _init_repo(ws)
    expected = _dirty_tree(ws)
    pre_status = _git(ws, "status", "--porcelain=v1", "--untracked-files=all", "-z").stdout
    ctx = _make_ctx(tmp_path, ws)

    # Passing command: must observe HEAD (not uncommitted edits) and leave ignored file untouched.
    check_head = _python(
        "from pathlib import Path; "
        "assert Path('app.txt').read_bytes() == b'head-version\\n'; "
        "assert not Path('sub/untracked.bin').exists(); "
        "assert Path('keep.ignored').read_bytes() == b'do-not-touch-ignored\\n'; "
        "print('saw-head')"
    )
    res_pass = _run_baseline(ctx, command=check_head)
    assert not res_pass.is_error, res_pass.content
    assert res_pass.content.startswith(BASELINE_HEADER + "\n")
    assert "saw-head" in res_pass.content
    assert "[exit code: 0]" in res_pass.content
    assert "Restore confirmed" in res_pass.content.splitlines()[-1]
    assert res_pass.meta["exit_code"] == 0
    assert res_pass.meta["restored"] is True
    _assert_restored(ws, expected, pre_status)
    assert _git(ws, "stash", "list").stdout == b""

    refs = _baseline_refs(ws)
    assert len(refs) == 1
    assert refs[0] == res_pass.meta["saved_ref"]
    assert _git(ws, "cat-file", "-t", refs[0]).stdout.strip() == b"commit"

    # Failing command: non-zero exit code reported, changes still restored byte-identically.
    res_fail = _run_baseline(
        ctx, command=_python("import sys; print('preexisting-fail'); sys.exit(4)")
    )
    assert not res_fail.is_error, res_fail.content
    assert res_fail.meta["exit_code"] == 4
    assert "preexisting-fail" in res_fail.content
    assert "[exit code: 4]" in res_fail.content
    assert "Restore confirmed" in res_fail.content.splitlines()[-1]
    _assert_restored(ws, expected, pre_status)
    assert _git(ws, "stash", "list").stdout == b""
    assert len(_baseline_refs(ws)) == 2


def test_baseline_timeout_kills_process_and_restores(tmp_path):
    ws = tmp_path / "repo"
    _init_repo(ws)
    expected = _dirty_tree(ws)
    pre_status = _git(ws, "status", "--porcelain=v1", "--untracked-files=all", "-z").stdout
    ctx = _make_ctx(tmp_path, ws)

    res = _run_baseline(
        ctx,
        command=_python("import time; print('sleeping', flush=True); time.sleep(30)"),
        timeout=1000,
    )
    assert not res.is_error, res.content
    assert res.meta.get("timed_out") is True
    assert res.meta.get("restored") is True
    assert "timed out" in res.content
    assert "Restore confirmed" in res.content.splitlines()[-1]
    _assert_restored(ws, expected, pre_status)
    assert _git(ws, "stash", "list").stdout == b""


@pytest.mark.parametrize(
    "writer_code",
    [
        "from pathlib import Path; Path('app.txt').write_bytes(b'baseline-wrote-tracked\\n')",
        "from pathlib import Path; Path('sub').mkdir(exist_ok=True); "
        "Path('sub/untracked.bin').write_bytes(b'baseline-wrote-untracked\\n')",
        "from pathlib import Path; Path('generated.tmp').write_bytes(b'leftover\\n')",
    ],
)
def test_baseline_leftovers_path_restores_and_saves_leftovers(tmp_path, writer_code):
    ws = tmp_path / "repo"
    _init_repo(ws)
    expected = _dirty_tree(ws)
    pre_status = _git(ws, "status", "--porcelain=v1", "--untracked-files=all", "-z").stdout
    ctx = _make_ctx(tmp_path, ws)

    res = _run_baseline(ctx, command=_python(writer_code))
    assert not res.is_error, res.content
    assert res.meta["restored"] is True
    assert res.meta.get("leftovers_stashed") is True
    assert "Restore confirmed" in res.content.splitlines()[-1]
    _assert_restored(ws, expected, pre_status)

    stash_list = _git(ws, "stash", "list").stdout.decode()
    assert "karox-baseline-leftovers" in stash_list
    # The karox-baseline stash itself was dropped.
    lines = [ln for ln in stash_list.splitlines() if ln.strip()]
    assert len(lines) == 1
    assert "karox-baseline-leftovers" in lines[0]


def test_baseline_untracked_only_changes_create_ref_and_restore(tmp_path):
    ws = tmp_path / "repo"
    _init_repo(ws)
    (ws / "only_new.txt").write_bytes(b"brand-new\n")
    pre_status = _git(ws, "status", "--porcelain=v1", "--untracked-files=all", "-z").stdout
    ctx = _make_ctx(tmp_path, ws)

    res = _run_baseline(
        ctx,
        command=_python(
            "from pathlib import Path; assert not Path('only_new.txt').exists(); print('clean')"
        ),
    )
    assert not res.is_error, res.content
    assert "clean" in res.content
    assert (ws / "only_new.txt").read_bytes() == b"brand-new\n"
    assert _git(ws, "status", "--porcelain=v1", "--untracked-files=all", "-z").stdout == pre_status
    assert len(_baseline_refs(ws)) == 1


def test_baseline_refusals_touch_nothing(tmp_path):
    # 1. Not a git work tree.
    no_git = tmp_path / "no_git"
    no_git.mkdir()
    (no_git / "a.txt").write_text("hi\n")
    res = _run_baseline(_make_ctx(tmp_path / "s1", no_git), command="echo hi")
    assert res.is_error
    assert "not a git work tree" in res.content.lower()

    # 2. Git repo with no commits.
    no_commits = tmp_path / "no_commits"
    no_commits.mkdir()
    assert _git(no_commits, "init").returncode == 0
    (no_commits / "a.txt").write_text("hi\n")
    res = _run_baseline(_make_ctx(tmp_path / "s2", no_commits), command="echo hi")
    assert res.is_error
    assert "no commits" in res.content.lower()

    # 3. Clean tree (only ignored files present).
    clean = tmp_path / "clean"
    _init_repo(clean)
    (clean / "note.ignored").write_text("ignored\n")
    res = _run_baseline(_make_ctx(tmp_path / "s3", clean), command="echo hi")
    assert res.is_error
    assert "just run the command normally" in res.content.lower()
    assert (clean / "note.ignored").read_text() == "ignored\n"
    assert _baseline_refs(clean) == []

    # 4. Mid-merge (real conflicting merge).
    merging = tmp_path / "merging"
    _init_repo(merging)
    assert _git(merging, "checkout", "-b", "side").returncode == 0
    (merging / "app.txt").write_bytes(b"side-change\n")
    assert _git(merging, "commit", "-am", "side").returncode == 0
    assert _git(merging, "checkout", "-").returncode == 0
    (merging / "app.txt").write_bytes(b"main-change\n")
    assert _git(merging, "commit", "-am", "main").returncode == 0
    merge_res = _git(merging, "merge", "side")
    assert merge_res.returncode != 0
    before = (merging / "app.txt").read_bytes()
    res = _run_baseline(_make_ctx(tmp_path / "s4", merging), command="echo hi")
    assert res.is_error
    assert "merge" in res.content.lower()
    assert (merging / "app.txt").read_bytes() == before
    assert _baseline_refs(merging) == []

    # 5. Mid-rebase / mid-cherry-pick markers.
    for marker_name, label in (("rebase-merge", "rebase"), ("CHERRY_PICK_HEAD", "cherry-pick")):
        repo = tmp_path / f"state_{label}"
        _init_repo(repo)
        (repo / "app.txt").write_bytes(b"dirty\n")
        marker = repo / ".git" / marker_name
        if marker_name == "rebase-merge":
            marker.mkdir()
        else:
            marker.write_text("0" * 40 + "\n")
        res = _run_baseline(_make_ctx(tmp_path / f"s_{label}", repo), command="echo hi")
        assert res.is_error
        assert label in res.content.lower()
        assert (repo / "app.txt").read_bytes() == b"dirty\n"
        assert _baseline_refs(repo) == []


def test_baseline_disaster_recovery_names_refs_and_copies_back_untracked(tmp_path, monkeypatch):
    ws = tmp_path / "repo"
    _init_repo(ws)
    _dirty_tree(ws)
    ctx = _make_ctx(tmp_path, ws)

    real_git = baseline_mod._git

    def fail_apply_and_pop(cwd: Path, *args: str):
        if len(args) >= 2 and args[0] == "stash" and args[1] in ("pop", "apply"):
            return baseline_mod._GitOut(1, b"", b"simulated stash failure")
        return real_git(cwd, *args)

    monkeypatch.setattr(baseline_mod, "_git", fail_apply_and_pop)
    res = _run_baseline(ctx, command="echo ran")
    assert res.is_error
    assert "refs/karox/baseline/" in res.content
    assert "stash@{" in res.content
    # Untracked file was copied back from the private temp dir.
    assert (ws / "sub" / "untracked.bin").read_bytes() == b"\x00\x01\xffuntracked\n"
    # Stash and saved ref were preserved for manual recovery.
    assert b"karox-baseline" in real_git(ws, "stash", "list").stdout
    assert len(_baseline_refs(ws)) == 1


def test_baseline_registered_in_main_tools():
    names = [t.name for t in build_tools(detect_shell())]
    assert "baseline" in names
