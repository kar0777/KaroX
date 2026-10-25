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