import shutil
import subprocess

import pytest

from karox6.mcp.runs import git_diff


@pytest.mark.skipif(not shutil.which("git"), reason="git unavailable")
def test_untracked_names_are_not_quoted(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "core.quotePath", "true"], check=True)
    (tmp_path / "Ж файл.txt").write_text("x", encoding="utf-8")
    _, untracked = git_diff(tmp_path)
    assert untracked == ["Ж файл.txt"]
