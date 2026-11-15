"""Executable resolution for guarded launches."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from karox.process_launcher import resolve_executable


@pytest.mark.skipif(os.name != "nt", reason="POSIX hands argv to execvp unchanged")
def test_a_relative_executable_is_found_where_the_child_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = tmp_path / "repo"
    (repository / "tools").mkdir(parents=True)
    script = repository / "tools" / "check.bat"
    script.write_text("@echo off\r\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    resolved = resolve_executable(["tools\\check.bat", "--fast"], repository)

    assert Path(resolved[0]).resolve() == script.resolve()
    assert resolved[1:] == ["--fast"]


def test_posix_argv_is_returned_unchanged_without_a_cwd() -> None:
    if os.name == "nt":
        pytest.skip("Windows resolves the first element")
    assert resolve_executable(["./run.sh", "x"]) == ["./run.sh", "x"]
