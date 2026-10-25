"""Reviewed prompt text that ships with the wheel.

Only the visual-checker prompt is assembled from a file: the agent's own
contract lives in ``karox.agent.SYSTEM_PROMPT`` (one copy of those words), and
the v7 profile assembler was retired with the loop that used it.
"""

from __future__ import annotations

from importlib import resources

VISUAL_CHECKER_FILE = "visual_checker.md"


def _read_text(name: str) -> str:
    return (
        resources.files("karox.prompts")
        .joinpath(name)
        .read_text(encoding="utf-8")
        .strip()
    )


def visual_checker_text() -> str:
    """The separate prompt the visual checker answers with, without task history."""

    return _read_text(VISUAL_CHECKER_FILE)