"""Reviewed prompt text that ships with the wheel.

The visual-checker prompt and the self-review request are files: the agent's own
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

def self_review_text() -> str:
    """The one maintainer-review request before a code run's first finish."""
    return _read_text("self_review.md")
