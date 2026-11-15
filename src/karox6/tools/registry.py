"""The default tool set."""

from __future__ import annotations

from karox6.providers.base import ToolSpec
from karox6.tools.base import Tool
from karox6.tools.fs import EditTool, ReadTool, WriteTool
from karox6.tools.jobs import JobKillTool, JobListTool, JobOutputTool
from karox6.tools.search import GlobTool, GrepTool
from karox6.tools.shell import ShellInfo, ShellTool
from karox6.tools.todo import TodoTool


def build_tools(shell: ShellInfo, *, apply_patch: bool = False) -> list[Tool]:
    tools: list[Tool] = [
        ShellTool(shell),
        ReadTool(),
        WriteTool(),
        EditTool(),
        GlobTool(),
        GrepTool(),
        TodoTool(),
        JobOutputTool(),
        JobListTool(),
        JobKillTool(),
    ]
    if apply_patch:
        from karox6.tools.apply_patch import ApplyPatchTool

        tools.insert(4, ApplyPatchTool())
    return tools


def tool_specs(tools: list[Tool]) -> list[ToolSpec]:
    return [ToolSpec(t.name, t.description, t.parameters) for t in tools]
