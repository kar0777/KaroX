"""Prompt contracts that keep a game's ready state testable.

The third `3d-game` run was verified at 29/30: its probe compared
``doc.body.dataset`` to itself, so an empty ``reset()`` state passed. These
words are the model's only instruction to assert real hook output.
"""

from __future__ import annotations

from karox.agent import SYSTEM_PROMPT


def test_game_ready_state_requires_non_empty_hook_arrays() -> None:
    assert "state().obstacles.length > 0" in SYSTEM_PROMPT
    assert "state().coins.length > 0" in SYSTEM_PROMPT
    assert "compares a value to itself" in SYSTEM_PROMPT