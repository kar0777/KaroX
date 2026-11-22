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
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        check=False,
    )


def _python(code: str) -> str:
    return f"{shlex.quote(sys.executable.replace(chr(92), '/'))} -c {shlex.quote(code)}"


def _make_ctx(tmp_path: Path, ws: Path, *, env: dict[str, str] | None = None) -> ToolContext:
    return ToolContext(
        workspace=ws,
        session_dir=tmp_path / "session",
        files=FileTracker(ws),
        jobs=JobManager(),
        shell=detect_shell(),
        env=dict(env or {}),
    )


def _init_repo(ws: Path) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    assert _git(ws, "init").returncode == 0
    assert _git(ws, "config", "user.name", "Test User").returncode == 0
    assert _git(ws, "config", "user.email", "test@example.com").returncode == 0
    assert _git(ws, "config", "core.autocrlf", "false").returncode == 0
    (ws / ".gitignore").write_bytes(b"*.ignored\nnode_modules/\n")
    (ws / "app.txt").write_bytes(b"head-version\n")
    (ws / "other.txt").write_bytes(b"other-head\n")
    assert _git(ws, "add", ".").returncode == 0
    assert _git(ws, "commit", "-m", "initial").returncode == 0


def _snapshot_repo(ws: Path) -> dict[str, bytes]:
    """Capture workspace git state that baseline must never alter."""
    return {
        "status": _git(ws, "status", "--porcelain=v1", "--untracked-files=all", "-z").stdout,
        "stash": _git(ws, "stash", "list", "--format=%H %s").stdout,
        "head": _git(ws, "rev-parse", "HEAD").stdout,
        "branch": _git(ws, "symbolic-ref", "HEAD").stdout,
        "staged_app": _git(ws, "show", ":app.txt").stdout,
    }


def _dirty_tree_with_stash(ws: Path) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """Create a user stash plus staged, unstaged, untracked, and ignored workspace changes."""
    (ws / "other.txt").write_bytes(b"user-stashed-change\n")
    assert _git(ws, "stash", "push", "-m", "user-own-stash").returncode == 0

    (ws / "app.txt").write_bytes(b"staged-version\n")
    assert _git(ws, "add", "app.txt").returncode == 0
    (ws / "app.txt").write_bytes(b"worktree-version\nline2\n")
    (ws / "sub").mkdir(exist_ok=True)
    (ws / "sub" / "untracked.bin").write_bytes(b"\x00\x01\xffuntracked\n")
    (ws / "keep.ignored").write_bytes(b"do-not-touch-ignored\n")
    expected_files = {
        "app.txt": b"worktree-version\nline2\n",
        "other.txt": b"other-head\n",
        "sub/untracked.bin": b"\x00\x01\xffuntracked\n",
        "keep.ignored": b"do-not-touch-ignored\n",
    }
    return expected_files, _snapshot_repo(ws)


def _worktree_entries(ws: Path) -> list[str]:
    out = _git(ws, "worktree", "list", "--porcelain").stdout.decode("utf-8", "replace")
    return [line for line in out.splitlines() if line.startswith("worktree ")]


def _assert_untouched(ws: Path, expected_files: dict[str, bytes], snap: dict[str, bytes]) -> None:
    for rel, content in expected_files.items():
        assert (ws / rel).read_bytes() == content, rel
    assert _snapshot_repo(ws) == snap
    assert len(_worktree_entries(ws)) == 1
    refs = _git(ws, "for-each-ref", "refs/karox").stdout.strip()
    assert refs == b""


def _run_baseline(ctx: ToolContext, **args):
    return asyncio.run(BaselineTool(ctx.shell).run(args, ctx))


def test_baseline_passing_failing_and_head_content(tmp_path):
    ws = tmp_path / "repo"
    _init_repo(ws)
    expected_files, snap = _dirty_tree_with_stash(ws)
    ctx = _make_ctx(tmp_path, ws)

    check_head = _python(
        "import os; from pathlib import Path; "
        "assert Path('app.txt').read_bytes() == b'head-version\\n'; "
        "assert not Path('sub/untracked.bin').exists(); "
        "assert not Path('keep.ignored').exists(); "
        "print('CWD=' + os.getcwd()); "
        "print('CONTENT=' + Path('app.txt').read_text().strip())"
    )
    res_pass = _run_baseline(ctx, command=check_head)
    assert not res_pass.is_error, res_pass.content
    assert res_pass.content.startswith(BASELINE_HEADER + "\n")
    assert "CONTENT=head-version" in res_pass.content
    assert "[exit code: 0]" in res_pass.content
    assert res_pass.meta["exit_code"] == 0
    _assert_untouched(ws, expected_files, snap)

    # Confirm the temp checkout directory itself was removed.
    cwd_line = next(ln for ln in res_pass.content.splitlines() if ln.startswith("CWD="))
    checkout_path = Path(cwd_line.removeprefix("CWD="))
    assert checkout_path.resolve() != ws.resolve()
    assert not checkout_path.exists()

    # Failing command: non-zero exit reported, workspace and worktree list still clean.
    res_fail = _run_baseline(
        ctx, command=_python("import sys; print('preexisting-fail'); sys.exit(4)")
    )
    assert not res_fail.is_error, res_fail.content
    assert res_fail.meta["exit_code"] == 4
    assert "preexisting-fail" in res_fail.content
    assert "[exit code: 4]" in res_fail.content
    _assert_untouched(ws, expected_files, snap)


def test_baseline_timeout_kills_process_and_cleans_worktree(tmp_path):
    ws = tmp_path / "repo"
    _init_repo(ws)
    expected_files, snap = _dirty_tree_with_stash(ws)
    ctx = _make_ctx(tmp_path, ws)

    res = _run_baseline(
        ctx,
        command=_python("import time; print('sleeping', flush=True); time.sleep(30)"),
        timeout=1000,
    )
    assert not res.is_error, res.content
    assert res.meta.get("timed_out") is True
    assert "timed out" in res.content
    _assert_untouched(ws, expected_files, snap)


def test_baseline_command_writing_committing_and_switching_leaves_workspace_untouched(tmp_path):
    ws = tmp_path / "repo"
    _init_repo(ws)
    expected_files, snap = _dirty_tree_with_stash(ws)
    ctx = _make_ctx(tmp_path, ws)

    # 1. Writing/modifying/deleting files in the checkout.
    writer = _python(
        "from pathlib import Path; "
        "Path('app.txt').write_bytes(b'mutated-in-checkout\\n'); "
        "Path('other.txt').unlink(); "
        "Path('generated.tmp').write_bytes(b'leftover\\n'); "
        "print('mutated')"
    )
    res_write = _run_baseline(ctx, command=writer)
    assert not res_write.is_error, res_write.content
    assert res_write.meta["exit_code"] == 0
    assert not (ws / "generated.tmp").exists()
    _assert_untouched(ws, expected_files, snap)

    # 2. Running git commit inside the detached checkout.
    commit_cmd = _python(
        "import subprocess; from pathlib import Path; "
        "Path('app.txt').write_bytes(b'committed-in-checkout\\n'); "
        "subprocess.run(['git', 'add', 'app.txt'], check=True); "
        "subprocess.run(['git', 'commit', '-m', 'temp-commit'], check=True); "
        "print('committed')"
    )
    res_commit = _run_baseline(ctx, command=commit_cmd)
    assert not res_commit.is_error, res_commit.content
    assert res_commit.meta["exit_code"] == 0
    _assert_untouched(ws, expected_files, snap)

    # 3. Running git switch -c inside the checkout.
    switch_cmd = _python(
        "import subprocess; "
        "subprocess.run(['git', 'switch', '-c', 'temp-side-branch'], check=True); "
        "print('switched')"
    )
    res_switch = _run_baseline(ctx, command=switch_cmd)
    assert not res_switch.is_error, res_switch.content
    assert res_switch.meta["exit_code"] == 0
    _assert_untouched(ws, expected_files, snap)


def test_baseline_subdirectory_cwd_matches_workspace(tmp_path):
    repo = tmp_path / "repo"
    _init_repo(repo)
    pkg = repo / "packages" / "alpha"
    pkg.mkdir(parents=True)
    (pkg / "data.txt").write_bytes(b"head-alpha\n")
    assert _git(repo, "add", ".").returncode == 0
    assert _git(repo, "commit", "-m", "add subdir").returncode == 0
    (pkg / "data.txt").write_bytes(b"dirty-alpha\n")

    ctx = _make_ctx(tmp_path, pkg)
    res = _run_baseline(
        ctx,
        command=_python(
            "from pathlib import Path; "
            "print(Path.cwd().parts[-2:]); "
            "print(Path('data.txt').read_text().strip())"
        ),
    )
    assert not res.is_error, res.content
    assert "('packages', 'alpha')" in res.content
    assert "head-alpha" in res.content
    assert (pkg / "data.txt").read_bytes() == b"dirty-alpha\n"
    assert len(_worktree_entries(repo)) == 1


def test_baseline_pythonpath_prepends_checkout_and_src_layout(tmp_path):
    # Flat layout: package at repo root + pre-existing PYTHONPATH preserved after checkout.
    flat = tmp_path / "flat_repo"
    _init_repo(flat)
    (flat / "flatpkg").mkdir()
    (flat / "flatpkg" / "__init__.py").write_bytes(b"VALUE = 'head-flat'\n")
    assert _git(flat, "add", ".").returncode == 0
    assert _git(flat, "commit", "-m", "add flatpkg").returncode == 0
    (flat / "flatpkg" / "__init__.py").write_bytes(b"VALUE = 'edited-flat'\n")

    extra_dir = tmp_path / "extra_py"
    extra_dir.mkdir()
    ctx_flat = _make_ctx(tmp_path / "s_flat", flat, env={"PYTHONPATH": str(extra_dir)})
    res_flat = _run_baseline(
        ctx_flat,
        command=_python(
            "import os, flatpkg; "
            "pp = os.environ['PYTHONPATH'].split(os.pathsep); "
            "print('VAL=' + flatpkg.VALUE); "
            "print('FIRST=' + pp[0]); "
            "print('LAST=' + pp[-1]); "
            "print('CWD=' + os.getcwd())"
        ),
    )
    assert not res_flat.is_error, res_flat.content
    lines = dict(ln.split("=", 1) for ln in res_flat.content.splitlines() if "=" in ln)
    assert lines["VAL"] == "head-flat"
    assert Path(lines["FIRST"]).resolve() == Path(lines["CWD"]).resolve()
    assert Path(lines["LAST"]).resolve() == extra_dir.resolve()
    assert len(_worktree_entries(flat)) == 1

    # src/ layout: checkout root AND checkout/src prepended, importing HEAD package from src/.
    src_repo = tmp_path / "src_repo"
    _init_repo(src_repo)
    (src_repo / "src" / "mypkg").mkdir(parents=True)
    (src_repo / "src" / "mypkg" / "__init__.py").write_bytes(b"VALUE = 'head-src'\n")
    assert _git(src_repo, "add", ".").returncode == 0
    assert _git(src_repo, "commit", "-m", "add src pkg").returncode == 0
    (src_repo / "src" / "mypkg" / "__init__.py").write_bytes(b"VALUE = 'edited-src'\n")

    # Simulate an editable install pointing at workspace/src via pre-existing PYTHONPATH.
    ctx_src = _make_ctx(tmp_path / "s_src", src_repo, env={"PYTHONPATH": str(src_repo / "src")})
    res_src = _run_baseline(
        ctx_src,
        command=_python(
            "import os, mypkg; "
            "pp = os.environ['PYTHONPATH'].split(os.pathsep); "
            "print('VAL=' + mypkg.VALUE); "
            "print('P0=' + pp[0]); "
            "print('P1=' + pp[1]); "
            "print('CWD=' + os.getcwd())"
        ),
    )
    assert not res_src.is_error, res_src.content
    lines_src = dict(ln.split("=", 1) for ln in res_src.content.splitlines() if "=" in ln)
    assert lines_src["VAL"] == "head-src"
    assert Path(lines_src["P0"]).resolve() == Path(lines_src["CWD"]).resolve()
    assert Path(lines_src["P1"]).resolve() == (Path(lines_src["CWD"]) / "src").resolve()
    assert len(_worktree_entries(src_repo)) == 1


def test_baseline_node_modules_linked_and_workspace_target_preserved(tmp_path):
    ws = tmp_path / "node_repo"
    _init_repo(ws)
    nm = ws / "node_modules" / "demo-lib"
    nm.mkdir(parents=True)
    sentinel = nm / "index.js"
    sentinel.write_bytes(b"module.exports = 42;\n")

    ctx = _make_ctx(tmp_path, ws)
    res = _run_baseline(
        ctx,
        command=_python(
            "from pathlib import Path; "
            "p = Path('node_modules/demo-lib/index.js'); "
            "print('EXISTS=' + str(p.exists())); "
            "print('TEXT=' + (p.read_text().strip() if p.exists() else ''))"
        ),
    )
    assert not res.is_error, res.content
    assert "EXISTS=True" in res.content
    assert "TEXT=module.exports = 42;" in res.content
    # Crucial: removing the temporary worktree must never delete the workspace's node_modules.
    assert sentinel.read_bytes() == b"module.exports = 42;\n"
    assert len(_worktree_entries(ws)) == 1


def test_baseline_clean_tree_and_mid_merge_are_allowed(tmp_path):
    # Clean tree runs against HEAD without refusing.
    clean = tmp_path / "clean"
    _init_repo(clean)
    res_clean = _run_baseline(_make_ctx(tmp_path / "s_clean", clean), command="echo clean-ok")
    assert not res_clean.is_error, res_clean.content
    assert "clean-ok" in res_clean.content
    assert len(_worktree_entries(clean)) == 1

    # Mid-merge repo also runs against HEAD without refusing or altering merge state.
    merging = tmp_path / "merging"
    _init_repo(merging)
    assert _git(merging, "checkout", "-b", "side").returncode == 0
    (merging / "app.txt").write_bytes(b"side-change\n")
    assert _git(merging, "commit", "-am", "side").returncode == 0
    assert _git(merging, "checkout", "-").returncode == 0
    (merging / "app.txt").write_bytes(b"main-change\n")
    assert _git(merging, "commit", "-am", "main").returncode == 0
    assert _git(merging, "merge", "side").returncode != 0
    before = (merging / "app.txt").read_bytes()

    res_merge = _run_baseline(
        _make_ctx(tmp_path / "s_merge", merging),
        command=_python("from pathlib import Path; print(Path('app.txt').read_text().strip())"),
    )
    assert not res_merge.is_error, res_merge.content
    assert "main-change" in res_merge.content
    assert (merging / "app.txt").read_bytes() == before
    assert (merging / ".git" / "MERGE_HEAD").exists()
    assert len(_worktree_entries(merging)) == 1


def test_baseline_refusals_without_git_or_commits(tmp_path):
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


def test_baseline_cancellation_cleans_up_worktree(tmp_path, monkeypatch):
    ws = tmp_path / "repo"
    _init_repo(ws)
    expected_files, snap = _dirty_tree_with_stash(ws)
    ctx = _make_ctx(tmp_path, ws)

    started = []
    real_start = baseline_mod.ProcessRun.start

    async def spy(*args, **kwargs):
        run = await real_start(*args, **kwargs)
        started.append(run)
        return run

    monkeypatch.setattr(baseline_mod.ProcessRun, "start", spy)

    async def scenario():
        task = asyncio.create_task(
            BaselineTool(ctx.shell).run(
                {"command": _python("import time; print('ready', flush=True); time.sleep(30)")},
                ctx,
            )
        )
        for _ in range(200):
            if started and b"ready" in started[0].head:
                break
            await asyncio.sleep(0.05)
        assert started
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    _assert_untouched(ws, expected_files, snap)


def test_baseline_fallback_rmtree_and_prune_when_worktree_remove_fails(tmp_path, monkeypatch):
    ws = tmp_path / "repo"
    _init_repo(ws)
    expected_files, snap = _dirty_tree_with_stash(ws)
    ctx = _make_ctx(tmp_path, ws)

    real_git = baseline_mod._git

    def fail_worktree_remove(cwd: Path, *args: str):
        if len(args) >= 2 and args[0] == "worktree" and args[1] == "remove":
            return baseline_mod._GitOut(1, b"", b"simulated worktree remove failure")
        return real_git(cwd, *args)

    monkeypatch.setattr(baseline_mod, "_git", fail_worktree_remove)
    res = _run_baseline(ctx, command=_python("import os; print('CWD=' + os.getcwd())"))
    assert not res.is_error, res.content
    cwd_line = next(ln for ln in res.content.splitlines() if ln.startswith("CWD="))
    assert not Path(cwd_line.removeprefix("CWD=")).exists()
    _assert_untouched(ws, expected_files, snap)


def test_baseline_registered_in_main_tools():
    tools = {t.name: t for t in build_tools(detect_shell())}
    assert "baseline" in tools
    assert "temporary checkout of HEAD" in tools["baseline"].description
    assert "git stash" in tools["baseline"].description
