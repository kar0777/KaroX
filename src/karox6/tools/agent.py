"""Subagents (M5, opt-in with ``--subagents``): delegate a subtask to a child session.

The child starts with a fresh context, works with read/search/shell tools and returns
only its final report, so exploration that would otherwise stay in the main context for
the rest of the run (and be re-sent with every step) is paid once. Several ``agent`` calls
in one step run in parallel. Children cannot spawn children.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from karox6.tools.base import ToolContext, ToolResult

Spawn = Callable[[str, str, ToolContext], Awaitable[ToolResult]]

SUBAGENT_APPEND = (
    "You are a subagent working for another agent, which gave you the task below and will "
    "read only your final reply. Investigate with the tools; do not modify, create or delete "
    "files, and do not install anything. When you have the answer, stop calling tools and "
    "reply with a concise, self-contained report: the facts that answer the request, with "
    "file paths and line numbers, and anything you could not determine."
)


class AgentTool:
    name = "agent"
    description = (
        "Delegate a self-contained investigation to a subagent with a fresh context and get back "
        "only its final report. Use it for exploration that would otherwise fill your own "
        "context: locating where behaviour is implemented, surveying how a module or its tests "
        "work, or checking a hypothesis across many files. The subagent cannot see this "
        "conversation, so put everything it needs in `prompt` and say exactly what to report. "
        "It can read, search and run shell commands, but does not change files: make the edits "
        "yourself. Several agent calls in one step run in parallel."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "description": {
                "type": "string",
                "description": "A few words naming the subtask (shown in progress output).",
            },
            "prompt": {
                "type": "string",
                "description": "The full task for the subagent, including what to report back.",
            },
        },
        "required": ["prompt"],
    }
    read_only = True  # children do not edit files, so several may run at once

    def __init__(self, spawn: Spawn) -> None:
        self._spawn = spawn

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        prompt = str(args.get("prompt") or "").strip()
        if not prompt:
            return ToolResult("Error: prompt is empty.", is_error=True)
        label = " ".join(str(args.get("description") or "").split())[:60] or "subagent"
        return await self._spawn(prompt, label, ctx)
