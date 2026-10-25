"""Placeholder for guarded desktop application control.

``hot_worker._DEFAULT_MODULE_GROUP`` names this module so a desktop-app
implementation can be dropped in without recycling the persistent worker
generation. The clean release ships no desktop/browser command surface, so the
dispatcher below refuses every action with the same typed ``InvalidCommand``
the rest of the worker contract uses instead of pretending a Win32 binding
exists.
"""

from __future__ import annotations

from typing import Any

from .core import InvalidCommand


def execute_desktop_app_action(
    runtime: Any,
    action: str,
    payload: dict[str, Any],
    deadline_seconds: float,
) -> dict[str, Any]:
    """Refuse desktop actions; see the module docstring for why."""

    del runtime, payload, deadline_seconds
    raise InvalidCommand(
        "desktop application control is not available in this release: "
        f"{action}"
    )


__all__ = ["execute_desktop_app_action"]