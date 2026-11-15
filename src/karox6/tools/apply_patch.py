"""apply_patch tool wrapper (engine in karox6.tools.patch). Off by default; see registry."""

from __future__ import annotations

from typing import Any

from karox6.tools.base import ToolContext, ToolResult
from karox6.tools.patch import PatchError, apply_patch


class ApplyPatchTool:
    name = "apply_patch"
    description = (
        "Create, update, delete or move UTF-8 text files with one patch in the "
        "`*** Begin Patch` / `*** End Patch` format (`*** Add File:`, `*** Update File:` with "
        "`@@` hunks of ` `/`-`/`+` lines, `*** Delete File:`, `*** Move to:`). Give every hunk "
        "enough context lines to match one place. The whole patch is checked first and a "
        "rejected patch changes nothing."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {"input": {"type": "string", "description": "The full patch text."}},
        "required": ["input"],
    }
    read_only = False

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            result = apply_patch(str(args["input"]), ctx.workspace)
        except PatchError as exc:
            return ToolResult(f"Error: {exc}", is_error=True)
        for change in result.changes:
            target = change.new_path or change.path
            ctx.files.record_write(ctx.workspace / target)
        return ToolResult(result.summary())
