"""Local MCP control tools for Claude Code and OpenCode.

These tools manage the remote Ellipsis session; they do not expose a fake model
completion endpoint and they never move the user's repository to the cloud.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from .ellipsis_runtime import (
    EllipsisAgentConfig,
    EllipsisAgentManager,
    infer_command_allowlists,
)
from .models import AccessProfile


_MANAGER = EllipsisAgentManager()


def karox_agent_start(
    repository: str,
    task: str,
    *,
    access_profile: str = "workspace_write",
    budget: float = 0.10,
    tunnel: str = "tailscale",
    port: int = 8766,
    allow_commit: bool = False,
) -> dict[str, Any]:
    root = Path(repository).expanduser().resolve(strict=True)
    commands = infer_command_allowlists(root)
    state = _MANAGER.start(
        EllipsisAgentConfig(
            repository=root,
            task=task,
            access_profile=AccessProfile(access_profile),
            budget=budget,
            tunnel=tunnel,
            port=port,
            verification_commands=commands,
            command_commands=commands,
            allow_commit=allow_commit,
        )
    )
    _MANAGER.detach(state.local_session_id)
    return state.public_dict()


def karox_agent_status(session_id: Optional[str] = None) -> dict[str, Any]:
    state, _ = _MANAGER.status(session_id)
    return state.public_dict()


def karox_agent_send(
    message: str, session_id: Optional[str] = None
) -> dict[str, Any]:
    state = _MANAGER.send(session_id, message)
    return state.public_dict()


def karox_agent_stop(session_id: Optional[str] = None) -> dict[str, Any]:
    state = _MANAGER.stop(session_id)
    return state.public_dict()


def karox_agent_result(session_id: Optional[str] = None) -> dict[str, Any]:
    return dict(_MANAGER.report(session_id))


def build_server():
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise RuntimeError("the mcp package is required") from exc
    server = FastMCP("KaroX Ellipsis Agent Control")
    server.tool()(karox_agent_start)
    server.tool()(karox_agent_status)
    server.tool()(karox_agent_send)
    server.tool()(karox_agent_stop)
    server.tool()(karox_agent_result)
    return server


def main() -> None:
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
