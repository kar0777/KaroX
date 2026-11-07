"""Compact full-screen client for KaroX.

The visual layer is deliberately separate from the agent runtime: it speaks the
``karox agent run --stream-json`` protocol over a child process and reads the
other surfaces (models, sessions, MCP, providers) through their ``--json`` CLI
forms. Nothing here imports :mod:`karox.agent`, so a heavy run cannot stall the
event loop and a renderer bug cannot corrupt a session.

The public names are lazy so importing ``karox.tui`` does not import Textual or
the application module unless a caller actually needs the UI.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "KaroxApp": (".app", "KaroxApp"),
    "run_tui": (".app", "run_tui"),
}
__all__ = ["KaroxApp", "run_tui"]


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
