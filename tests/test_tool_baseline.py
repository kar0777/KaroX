import asyncio
import os
import shlex
import shutil
import subprocess
import sys
import threading
import time
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
        "all_refs": _git(ws, "for-each-ref").stdout,
        "refs_heads": _git(ws, "for-each-ref", "refs/heads").stdout,
        "refs_stash": _git(ws, "for-each-ref", "refs/stash").stdout,
        "config": (ws / ".git" / "config").read_bytes(),
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


def _create_dir_link(src: Path, dst: Path) -> None:
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(src), str(dst))
    else:
        os.symlink(src, dst, target_is_directory=True)


def _remove_dir_link(link: Path) -> None:
    if link.is_symlink():
        link.unlink()
    elif link.exists():
        os.rmdir(link)


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
    assert "cleanup_failed" not in res_pass.meta
    _assert_untouched(ws, expected_files, snap)

    # Confirm the temp checkout directory uses the hostname+PID-tagged prefix and was removed.
    cwd_line = next(ln for ln in res_pass.content.splitlines() if ln.startswith("CWD="))
    checkout_path = Path(cwd_line.removeprefix("CWD="))
    assert checkout_path.resolve() != ws.resolve()
    expected_prefix = f"karox-baseline-{baseline_mod._safe_hostname()}-{os.getpid()}-"
    assert checkout_path.parent.name.startswith(expected_prefix)
    assert not checkout_path.parent.exists()

    # Failing command: non-zero exit reported, workspace and refs still clean.
    res_fail = _run_baseline(
        ctx, command=_python("import sys; print('preexisting-fail'); sys.exit(4)")
    )
    assert not res_fail.is_error, res_fail.content
    assert res_fail.meta["exit_code"] == 4
    assert "preexisting-fail" in res_fail.content
    assert "[exit code: 4]" in res_fail.content
    _assert_untouched(ws, expected_files, snap)


def test_baseline_timeout_kills_process_and_cleans_clone(tmp_path):
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


def test_baseline_reports_cleanup_failure(tmp_path, monkeypatch):
    ws = tmp_path / "repo"
    _init_repo(ws)
    ctx = _make_ctx(tmp_path, ws)

    leaked: list[Path] = []

    def fail_cleanup(temp_parent: Path) -> bool:
        leaked.append(temp_parent)
        return False

    monkeypatch.setattr(baseline_mod, "_cleanup_temp_dir", fail_cleanup)
    try:
        res = _run_baseline(ctx, command="echo hello")
        assert not res.is_error, res.content
        assert res.meta.get("cleanup_failed") is True
        assert res.meta.get("cleanup_path") == str(leaked[0])
        assert "failed to remove temporary checkout directory" in res.content.lower()
        assert str(leaked[0]) in res.content
    finally:
        for path in leaked:
            baseline_mod._rmtree_force(path)


def test_baseline_isolates_stash_branches_and_config_from_user_repo(tmp_path):
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

    # 2. Running git commit, git branch x, git config user.foo bar, and git stash inside baseline.
    git_mut = _python(
        "import subprocess; from pathlib import Path; "
        "subprocess.run(['git', 'config', 'user.name', 'Clone User'], check=True); "
        "subprocess.run(['git', 'config', 'user.email', 'clone@example.com'], check=True); "
        "subprocess.run(['git', 'config', 'user.foo', 'bar'], check=True); "
        "subprocess.run(['git', 'branch', 'x'], check=True); "
        "Path('app.txt').write_bytes(b'to-stash\\n'); "
        "subprocess.run(['git', 'stash'], check=True); "
        "Path('app.txt').write_bytes(b'committed-in-checkout\\n'); "
        "subprocess.run(['git', 'add', 'app.txt'], check=True); "
        "subprocess.run(['git', 'commit', '-m', 'temp-commit'], check=True); "
        "subprocess.run(['git', 'switch', '-c', 'temp-side-branch'], check=True); "
        "print('isolated-ok')"
    )
    res_mut = _run_baseline(ctx, command=git_mut)
    assert not res_mut.is_error, res_mut.content
    assert res_mut.meta["exit_code"] == 0, res_mut.content
    assert "isolated-ok" in res_mut.content
    # User's refs/heads, refs/stash (including user-own-stash), and .git/config are untouched.
    _assert_untouched(ws, expected_files, snap)
    assert b"user-own-stash" in snap["stash"]
    assert _git(ws, "config", "--local", "--get", "user.foo").returncode != 0


def test_baseline_removes_origin_so_git_push_cannot_change_user_refs(tmp_path):
    ws = tmp_path / "repo"
    _init_repo(ws)
    expected_files, snap = _dirty_tree_with_stash(ws)
    ctx = _make_ctx(tmp_path, ws)

    push_attempt = _python(
        "import subprocess; "
        "remotes = subprocess.run(['git', 'remote'], capture_output=True, text=True, check=True); "
        "assert remotes.stdout.strip() == '', remotes.stdout; "
        "p1 = subprocess.run(['git', 'push', 'origin', 'HEAD:refs/heads/injected']); "
        "subprocess.run(['git', 'tag', 'injected-tag'], check=True); "
        "p2 = subprocess.run(['git', 'push', '--tags']); "
        "print(f'PUSH_RCS={p1.returncode},{p2.returncode}')"
    )
    res = _run_baseline(ctx, command=push_attempt)
    assert not res.is_error, res.content
    assert res.meta["exit_code"] == 0, res.content
    rc_line = next(ln for ln in res.content.splitlines() if ln.startswith("PUSH_RCS="))
    rc1, rc2 = (int(x) for x in rc_line.removeprefix("PUSH_RCS=").split(","))
    assert rc1 != 0
    assert rc2 != 0
    _assert_untouched(ws, expected_files, snap)


def test_baseline_never_calls_git_worktree_and_preserves_stale_worktree(tmp_path, monkeypatch):
    ws = tmp_path / "repo"
    _init_repo(ws)
    other_wt = tmp_path / "other_wt"
    assert _git(ws, "worktree", "add", "--detach", str(other_wt), "HEAD").returncode == 0
    shutil.rmtree(other_wt)  # Leave a stale worktree entry as if on an unmounted drive.
    entries_before = _worktree_entries(ws)
    assert len(entries_before) == 2

    real_git = baseline_mod._git
    seen_args: list[tuple[str, ...]] = []

    def spy_git(cwd: Path, *args: str):
        seen_args.append(args)
        return real_git(cwd, *args)

    monkeypatch.setattr(baseline_mod, "_git", spy_git)
    res = _run_baseline(_make_ctx(tmp_path, ws), command="echo ok")
    assert not res.is_error, res.content
    assert not any("worktree" in call for call in seen_args)
    assert _worktree_entries(ws) == entries_before


@pytest.mark.parametrize(
    "cmd",
    [
        "npm ci",
        "npm install",
        "npm i",
        "npm add left-pad",
        "npm uninstall left-pad",
        "npm rm left-pad",
        "npm prune",
        "npm update",
        "npm up",
        "npm dedupe",
        "npm rebuild",
        "npm link",
        "npm -w pkg install",
        "npm --workspace pkg prune",
        "pnpm ci",
        "pnpm install --frozen-lockfile",
        "pnpm add zod",
        "pnpm remove zod",
        "pnpm prune",
        "pnpm update",
        "yarn",
        "yarn --frozen-lockfile",
        "yarn install",
        "yarn add react",
        "yarn remove react",
        "bun install",
        "bun i",
        "bun add zod",
        "bun remove zod",
        "pip install -e .",
        "pip3 install pytest",
        "pip uninstall pytest",
        "python -m pip install -e .",
        "uv pip install pytest",
        "uv sync",
        "uv add ruff",
        "uv remove ruff",
        "poetry install",
        "poetry add requests",
        "poetry remove requests",
        "pdm install",
        "pdm add httpx",
        "conda install numpy",
        "conda remove numpy",
        "pipx install ruff",
        "python setup.py develop",
        "python3 setup.py install",
        "./setup.py develop",
        "echo pre && npm ci",
    ],
)
def test_baseline_refuses_package_install_commands(tmp_path, cmd):
    ws = tmp_path / "repo"
    _init_repo(ws)
    nm = ws / "node_modules" / "pkg"
    nm.mkdir(parents=True)
    sentinel = nm / "keep.js"
    sentinel.write_bytes(b"keep\n")

    ctx = _make_ctx(tmp_path, ws)
    res = _run_baseline(ctx, command=cmd)
    assert res.is_error, f"expected refusal for {cmd!r}, got: {res.content}"
    assert "package installation" in res.content.lower()
    assert sentinel.read_bytes() == b"keep\n"


@pytest.mark.parametrize(
    "cmd",
    [
        "npm test",
        "npm run lint",
        "npm -w pkg test",
        "pnpm test",
        "yarn test",
        "yarn --version",
        "bun test",
        "uv run pytest",
        "poetry run pytest",
        "pdm run pytest",
        "conda list",
        "pipx list",
        "pip list",
        "python setup.py --version",
        "pytest -k install",
        "echo 'npm install'",
    ],
)
def test_baseline_allows_non_install_commands(cmd):
    assert baseline_mod._package_install_command(cmd) is None


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


def test_baseline_missing_or_unmappable_subdir_returns_error(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    _init_repo(repo)
    untracked_sub = repo / "only_in_working_tree"
    untracked_sub.mkdir()

    ctx_missing = _make_ctx(tmp_path / "s1", untracked_sub)
    res_missing = _run_baseline(ctx_missing, command="echo should-not-run")
    assert res_missing.is_error
    assert "does not exist in head" in res_missing.content.lower()
    assert "should-not-run" not in res_missing.content

    # Unmappable relative_to (e.g. repo_root reported on another root/subst drive).
    other_root = tmp_path / "other_root"
    other_root.mkdir()
    head_sha = _git(repo, "rev-parse", "--verify", "HEAD").stdout.decode().strip()
    monkeypatch.setattr(baseline_mod, "_validate_repo", lambda _cwd: (other_root, head_sha, None))
    res_unmapped = _run_baseline(_make_ctx(tmp_path / "s2", repo), command="echo hi")
    assert res_unmapped.is_error
    assert "could not be mapped" in res_unmapped.content.lower()


def test_baseline_refuses_command_containing_workspace_path_before_running(tmp_path, monkeypatch):
    ws = tmp_path / "repo"
    _init_repo(ws)
    ctx = _make_ctx(tmp_path, ws)

    started = False

    async def fail_start(*args, **kwargs):
        nonlocal started
        started = True
        raise AssertionError("ProcessRun.start must not be called")

    monkeypatch.setattr(baseline_mod.ProcessRun, "start", fail_start)

    res = _run_baseline(ctx, command=f"echo {shlex.quote(ws.as_posix())}")
    assert res.is_error
    assert "absolute workspace path" in res.content.lower()
    assert not started

    # Subdir workspace referencing parent repo_root is also refused before ProcessRun.start.
    sub = ws / "subpkg"
    sub.mkdir()
    (sub / "f.txt").write_text("ok\n")
    assert _git(ws, "add", ".").returncode == 0
    assert _git(ws, "commit", "-m", "subpkg").returncode == 0
    res_root = _run_baseline(_make_ctx(tmp_path / "s2", sub), command=f"ls {ws.as_posix()}")
    assert res_root.is_error
    assert "absolute workspace path" in res_root.content.lower()
    assert not started

    if sys.platform == "win32":
        win_path = str(ws.resolve())
        posix = ws.resolve().as_posix()
        msys = f"/{posix[0].lower()}{posix[2:]}"
        assert baseline_mod._command_references_workspace(f"cat {win_path}\\app.txt", ws)
        assert baseline_mod._command_references_workspace(
            f"cat {win_path.replace(chr(92), chr(92) * 2)}", ws
        )
        assert baseline_mod._command_references_workspace(f"cd {posix.upper()} && pytest", ws)
        assert baseline_mod._command_references_workspace(f"cd {msys} && pytest", ws)
        assert not baseline_mod._command_references_workspace(f"cd {posix}-other && pytest", ws)


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

    # src/ layout: checkout root AND checkout/src prepended, importing HEAD package from src/.
    src_repo = tmp_path / "src_repo"
    _init_repo(src_repo)
    (src_repo / "src" / "mypkg").mkdir(parents=True)
    (src_repo / "src" / "mypkg" / "__init__.py").write_bytes(b"VALUE = 'head-src'\n")
    assert _git(src_repo, "add", ".").returncode == 0
    assert _git(src_repo, "commit", "-m", "add src pkg").returncode == 0
    (src_repo / "src" / "mypkg" / "__init__.py").write_bytes(b"VALUE = 'edited-src'\n")

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


def test_baseline_node_env_uses_path_and_node_path_without_linking(tmp_path):
    ws = tmp_path / "node_repo"
    _init_repo(ws)
    nm = ws / "node_modules"
    (nm / "demo-lib").mkdir(parents=True)
    sentinel = nm / "demo-lib" / "index.js"
    sentinel.write_bytes(b"module.exports = 42;\n")
    bin_dir = nm / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "fake-tool").write_bytes(b"#!/bin/sh\necho fake\n")

    ext_target = tmp_path / "external_target"
    ext_target.mkdir()
    ext_sentinel = ext_target / "precious.txt"
    ext_sentinel.write_bytes(b"do-not-delete\n")

    check_code = (
        "import os; from pathlib import Path; "
        "print('NM_EXISTS=' + str(Path('node_modules').exists())); "
        "print('NODE_PATH=' + os.environ.get('NODE_PATH', '')); "
        "print('PATH_ALL=' + os.environ.get('PATH', '')); "
        "Path('node_modules/clone_only').mkdir(parents=True); "
        "Path('node_modules/clone_only/x.js').write_text('hi'); "
        + (
            f"import _winapi; _winapi.CreateJunction({str(ext_target)!r}, 'extra_link')"
            if sys.platform == "win32"
            else f"os.symlink({str(ext_target)!r}, 'extra_link', target_is_directory=True)"
        )
    )

    merged = baseline_mod._merge_node_env({"PATH": "/usr/bin"}, ws)
    assert merged["PATH"].split(os.pathsep)[0] == str(bin_dir)
    assert merged["NODE_PATH"].split(os.pathsep)[0] == str(nm)

    ctx = _make_ctx(tmp_path, ws)
    res = _run_baseline(ctx, command=_python(check_code))
    assert not res.is_error, res.content
    lines = dict(ln.split("=", 1) for ln in res.content.splitlines() if "=" in ln)
    assert lines["NM_EXISTS"] == "False"
    assert Path(lines["NODE_PATH"].split(os.pathsep)[0]).resolve() == nm.resolve()
    path_entries = [Path(p).resolve() for p in lines["PATH_ALL"].split(os.pathsep) if p]
    assert bin_dir.resolve() in path_entries
    # User's node_modules was untouched; writes in clone stayed in clone.
    assert not (nm / "clone_only").exists()
    assert sentinel.read_bytes() == b"module.exports = 42;\n"
    # Command-created link inside clone did not touch its target on rmtree.
    assert ext_sentinel.read_bytes() == b"do-not-delete\n"


def test_baseline_cleanup_refuses_symlink_or_junction_temp_dir(tmp_path):
    target = tmp_path / "precious_target"
    target.mkdir()
    sentinel = target / "keep.txt"
    sentinel.write_bytes(b"intact\n")

    link_dir = tmp_path / f"karox-baseline-{baseline_mod._safe_hostname()}-999999-link"
    _create_dir_link(target, link_dir)
    try:
        assert baseline_mod._cleanup_temp_dir(link_dir) is False
        assert baseline_mod._is_link(link_dir)
        assert sentinel.read_bytes() == b"intact\n"
    finally:
        _remove_dir_link(link_dir)


def test_baseline_stale_cleanup_checks_host_pid_age_and_links(tmp_path):
    dead_proc = subprocess.Popen([sys.executable, "-c", "pass"])
    dead_proc.wait(timeout=10)
    dead_pid = dead_proc.pid

    fake_tmp = tmp_path / "fake_tmp"
    fake_tmp.mkdir()
    host = baseline_mod._safe_hostname()
    old_mtime = time.time() - 7200

    # 1. Eligible stale dir: same host, dead PID, older than 1 hour -> removed.
    stale_dir = fake_tmp / f"karox-baseline-{host}-{dead_pid}-stale1"
    (stale_dir / "wt").mkdir(parents=True)
    (stale_dir / "wt" / "file.txt").write_bytes(b"old\n")
    os.utime(stale_dir, (old_mtime, old_mtime))

    # 2. Fresh dead-PID dir (< 1 hour old) -> kept.
    fresh_dir = fake_tmp / f"karox-baseline-{host}-{dead_pid}-fresh1"
    fresh_dir.mkdir()

    # 3. Different hostname (old + dead PID) -> kept.
    other_host_dir = fake_tmp / f"karox-baseline-otherhost-{dead_pid}-stale2"
    other_host_dir.mkdir()
    os.utime(other_host_dir, (old_mtime, old_mtime))

    # 4. Live PID (same host + old) -> kept.
    live_dir = fake_tmp / f"karox-baseline-{host}-{os.getpid()}-live1"
    live_dir.mkdir()
    os.utime(live_dir, (old_mtime, old_mtime))

    # 5. Stale candidate that is itself a symlink/junction -> kept and target untouched.
    link_target = tmp_path / "link_target"
    link_target.mkdir()
    sentinel = link_target / "survive.txt"
    sentinel.write_bytes(b"survive\n")
    link_stale = fake_tmp / f"karox-baseline-{host}-{dead_pid}-link1"
    _create_dir_link(link_target, link_stale)

    # 6. Unrelated dir -> kept.
    other_dir = fake_tmp / "unrelated-dir"
    other_dir.mkdir()

    try:
        baseline_mod._cleanup_stale_temp_dirs(fake_tmp)

        assert not stale_dir.exists()
        assert fresh_dir.exists()
        assert other_host_dir.exists()
        assert live_dir.exists()
        assert baseline_mod._is_link(link_stale)
        assert sentinel.read_bytes() == b"survive\n"
        assert other_dir.exists()
    finally:
        _remove_dir_link(link_stale)


def test_baseline_strips_git_override_env_vars(tmp_path, monkeypatch):
    ws = tmp_path / "repo"
    _init_repo(ws)
    decoy = tmp_path / "decoy_git"
    decoy.mkdir()
    git_vars = {
        "GIT_DIR": str(decoy),
        "GIT_WORK_TREE": str(decoy),
        "GIT_INDEX_FILE": str(decoy / "index"),
        "GIT_OBJECT_DIRECTORY": str(decoy / "objects"),
        "GIT_COMMON_DIR": str(decoy),
    }
    for k, v in git_vars.items():
        monkeypatch.setenv(k, v)

    ctx = _make_ctx(tmp_path, ws, env=git_vars)
    check = _python(
        "import os; from pathlib import Path; "
        "assert Path('app.txt').read_bytes() == b'head-version\\n'; "
        "leaked = [k for k in ('GIT_DIR','GIT_WORK_TREE','GIT_INDEX_FILE',"
        "'GIT_OBJECT_DIRECTORY','GIT_COMMON_DIR') if k in os.environ]; "
        "assert not leaked, leaked; "
        "print('ENV_CLEAN')"
    )
    res = _run_baseline(ctx, command=check)
    assert not res.is_error, res.content
    assert res.meta["exit_code"] == 0, res.content
    assert "ENV_CLEAN" in res.content


def test_baseline_disables_hooks_fsmonitor_and_lfs_smudge(tmp_path, monkeypatch):
    ws = tmp_path / "repo"
    _init_repo(ws)
    hook_marker = tmp_path / "hook_ran"
    hooks_dir = ws / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)
    hook_script = hooks_dir / "post-checkout"
    hook_script.write_text(f"#!/bin/sh\necho ran > {shlex.quote(hook_marker.as_posix())}\nexit 1\n")
    if sys.platform != "win32":
        hook_script.chmod(0o755)

    custom_hooks = tmp_path / "custom_hooks"
    custom_hooks.mkdir()
    custom_hook = custom_hooks / "post-checkout"
    custom_hook.write_text(f"#!/bin/sh\necho ran > {shlex.quote(hook_marker.as_posix())}\nexit 1\n")
    if sys.platform != "win32":
        custom_hook.chmod(0o755)
    assert _git(ws, "config", "core.hooksPath", custom_hooks.as_posix()).returncode == 0

    real_run = baseline_mod.subprocess.run
    seen_calls: list[tuple[list[str], dict[str, str]]] = []

    def spy_run(cmd, *args, **kwargs):
        seen_calls.append((list(cmd), dict(kwargs.get("env") or {})))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(baseline_mod.subprocess, "run", spy_run)
    res = _run_baseline(_make_ctx(tmp_path, ws), command="echo ok")
    assert not res.is_error, res.content
    assert not hook_marker.exists()

    clone_calls = [c for c in seen_calls if "clone" in c[0]]
    checkout_calls = [c for c in seen_calls if "checkout" in c[0]]
    assert clone_calls
    assert checkout_calls
    for cmd, env in (*clone_calls, *checkout_calls):
        assert any(a.startswith("core.hooksPath=") for a in cmd), cmd
        assert "core.fsmonitor=false" in cmd, cmd
        assert env.get("GIT_LFS_SKIP_SMUDGE") == "1"


def test_baseline_clean_tree_and_mid_merge_are_allowed(tmp_path):
    clean = tmp_path / "clean"
    _init_repo(clean)
    res_clean = _run_baseline(_make_ctx(tmp_path / "s_clean", clean), command="echo clean-ok")
    assert not res_clean.is_error, res_clean.content
    assert "clean-ok" in res_clean.content

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


def test_baseline_refusals_without_git_or_commits(tmp_path):
    no_git = tmp_path / "no_git"
    no_git.mkdir()
    (no_git / "a.txt").write_text("hi\n")
    res = _run_baseline(_make_ctx(tmp_path / "s1", no_git), command="echo hi")
    assert res.is_error
    assert "not a git work tree" in res.content.lower()

    no_commits = tmp_path / "no_commits"
    no_commits.mkdir()
    assert _git(no_commits, "init").returncode == 0
    (no_commits / "a.txt").write_text("hi\n")
    res = _run_baseline(_make_ctx(tmp_path / "s2", no_commits), command="echo hi")
    assert res.is_error
    assert "no commits" in res.content.lower()


def test_baseline_cancellation_cleans_up_clone(tmp_path, monkeypatch):
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


def test_baseline_cancellation_during_clone_is_shielded(tmp_path, monkeypatch):
    ws = tmp_path / "repo"
    _init_repo(ws)
    expected_files, snap = _dirty_tree_with_stash(ws)
    ctx = _make_ctx(tmp_path, ws)

    entered = threading.Event()
    release = threading.Event()
    events: list[str] = []
    seen_temp_parents: list[Path] = []

    real_prepare = baseline_mod._prepare_checkout
    real_cleanup = baseline_mod._cleanup_temp_dir

    def slow_prepare(repo_root: Path, head_sha: str, temp_parent: Path, wt_dir: Path):
        seen_temp_parents.append(temp_parent)
        entered.set()
        release.wait(timeout=10)
        res = real_prepare(repo_root, head_sha, temp_parent, wt_dir)
        events.append("clone_done")
        return res

    def spy_cleanup(temp_parent: Path):
        events.append("cleanup_start")
        return real_cleanup(temp_parent)

    monkeypatch.setattr(baseline_mod, "_prepare_checkout", slow_prepare)
    monkeypatch.setattr(baseline_mod, "_cleanup_temp_dir", spy_cleanup)

    async def scenario():
        task = asyncio.create_task(BaselineTool(ctx.shell).run({"command": "echo hi"}, ctx))
        for _ in range(200):
            if entered.is_set():
                break
            await asyncio.sleep(0.02)
        assert entered.is_set()
        task.cancel()
        await asyncio.sleep(0.05)
        # Cleanup must not have started while clone/checkout is still running.
        assert "cleanup_start" not in events
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert events == ["clone_done", "cleanup_start"]
    assert seen_temp_parents
    assert not seen_temp_parents[0].exists()
    _assert_untouched(ws, expected_files, snap)


def test_baseline_reports_pipestatus_for_failed_pipeline(tmp_path):
    ws = tmp_path / "repo"
    _init_repo(ws)
    ctx = _make_ctx(tmp_path, ws)
    if ctx.shell.kind != "bash":
        pytest.skip("bash pipeline statuses")

    res = _run_baseline(ctx, command="false | cat")
    assert not res.is_error, res.content
    assert res.meta["exit_code"] == 0
    assert res.meta["pipeline_exit_codes"] == [1, 0]
    assert "[pipeline exit codes: 1 0]" in res.content


def test_baseline_registered_in_main_tools():
    tools = {t.name: t for t in build_tools(detect_shell())}
    assert "baseline" in tools
    assert "temporary checkout of HEAD" in tools["baseline"].description
    assert "git stash" in tools["baseline"].description
