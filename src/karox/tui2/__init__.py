"""Compact full-screen client for KaroX.

The visual layer is deliberately separate from the agent runtime: it speaks the
``karox agent run --stream-json`` protocol over a child process and reads the
other surfaces (models, sessions, MCP, providers) through their ``--json`` CLI
forms. Nothing here imports :mod:`karox.agent`, so a heavy run cannot stall the
event loop and a renderer bug cannot corrupt a session.
"""

from __future__ import annotations

from .app import KaroxApp, run_tui

__all__ = ["KaroxApp", "run_tui"]