"""The developer-command boundary for git and ordinary checks."""

from __future__ import annotations

import pytest

from karox.core import CoreRuntime, InvalidCommand


def test_a_safe_git_read_is_allowed_in_a_check() -> None:
    assert CoreRuntime._validate_process(["git", "diff", "--check"]) == [
        "git",
        "diff",
        "--check",
    ]


def test_a_git_push_is_still_refused() -> None:
    with pytest.raises(InvalidCommand, match="read-only"):
        CoreRuntime._validate_process(["git", "push", "origin", "main"])

def test_a_check_cannot_kill_processes_by_image_name() -> None:
    with pytest.raises(InvalidCommand, match="image name"):
        CoreRuntime._validate_process(["taskkill", "/IM", "python.exe", "/F"])


def test_a_check_may_stop_one_process_by_pid() -> None:
    argv = ["taskkill", "/PID", "424242", "/T", "/F"]
    assert CoreRuntime._validate_process(argv) == argv
