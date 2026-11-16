"""Helper child processes never inherit KaroX's own stdin.

Under `karox6 mcp` (stdio) stdin is the JSON-RPC pipe and a reader thread blocks on it.
On Windows a child created with an inherited handle that another thread is reading
hangs in process creation, so `git` probes timed out and karox_diff reported "not a
git repository" for a real repository.
"""

from __future__ import annotations

import ast
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "karox6"
SPAWNERS = {
    "subprocess": {"run", "Popen", "call", "check_call", "check_output"},
    "asyncio": {"create_subprocess_exec", "create_subprocess_shell"},
}


def _spawn_calls(tree: ast.AST) -> list[ast.Call]:
    calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        owner_name = owner.id if isinstance(owner, ast.Name) else ""
        if node.func.attr in SPAWNERS.get(owner_name, ()):
            calls.append(node)
    return calls


def test_every_child_process_gets_an_explicit_stdin() -> None:
    missing = []
    found = 0
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for call in _spawn_calls(tree):
            found += 1
            if not any(kw.arg in {"stdin", "input"} for kw in call.keywords):
                missing.append(f"{path.relative_to(SRC)}:{call.lineno}")
    assert found >= 8  # the scan really sees the spawn sites
    assert missing == []


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not installed")
def test_git_probes_work_while_a_thread_reads_stdin(tmp_path: Path) -> None:
    repo = tmp_path / "репозиторий"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], stdin=subprocess.DEVNULL, check=True)
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    code = textwrap.dedent(
        """
        import os, sys, threading, time
        from pathlib import Path
        from karox6.core.prompt import git_summary
        from karox6.mcp.runs import git_diff
        threading.Thread(target=sys.stdin.buffer.readline, daemon=True).start()
        time.sleep(0.3)
        repo = Path(sys.argv[1])
        print(git_summary(repo))
        print(git_diff(repo)[1], flush=True)
        os._exit(0)  # the reader thread still holds stdin
        """
    )
    proc = subprocess.Popen(
        [sys.executable, "-c", code, str(repo)],
        stdin=subprocess.PIPE,  # kept open, like the MCP client's pipe
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        proc.wait(timeout=60)
        out = proc.stdout.read().decode("utf-8", "replace") if proc.stdout else ""
        err = proc.stderr.read().decode("utf-8", "replace") if proc.stderr else ""
    finally:
        if proc.stdin:
            proc.stdin.close()
        proc.kill()
        proc.wait()
    assert proc.returncode == 0, err
    lines = out.splitlines()
    assert lines[0].startswith("Git: repository on branch"), out
    assert lines[1] == "['a.txt']", out
