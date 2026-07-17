"""Notion MCP entrypoint with runtime hardening and extended agent tools."""
from __future__ import annotations

# Import order is intentional: app_entry patches the shared repo_tools app first.
import app_entry as _hardened  # noqa: F401
import notion_gateway as _gateway
from notion_agent_tools import register as register_agent_tools

# FastMCP dispatches tools through its live ToolManager, so additive tools can be
# registered after the transport application has been constructed.
register_agent_tools(_gateway.mcp, _gateway._call)
app = _gateway.app

__all__ = ["app"]
