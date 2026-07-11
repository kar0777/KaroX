"""Notion MCP entrypoint with the shared KaroX runtime hardening applied first."""
from __future__ import annotations

# Import order is intentional: app_entry patches the shared repo_tools application
# before notion_gateway mounts the MCP transport on that same FastAPI instance.
import app_entry as _hardened  # noqa: F401
from notion_gateway import app

__all__ = ["app"]
