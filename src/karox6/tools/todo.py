"""todo_write: the agent's own task list (DSH/Claude Code contract)."""

from __future__ import annotations

from typing import Any

from karox6.tools.base import ToolContext, ToolResult

_MARK = {"completed": "[x]", "in_progress": "[~]", "pending": "[ ]"}


class TodoTool:
    name = "todo_write"
    description = (
        "Record and update a task list to plan multi-step work and show progress; skip it for "
        "trivial single-step tasks. Send the whole list each time. Add one todo per concrete "
        "step before you start, keep the step being worked on `in_progress`, and mark each todo "
        "`completed` as soon as it is done."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "todos": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "content": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": ["pending", "in_progress", "completed"],
                        },
                    },
                    "required": ["content", "status"],
                },
            }
        },
        "required": ["todos"],
    }
    read_only = False

    def __init__(self) -> None:
        self.items: list[dict[str, str]] = []

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        items: list[dict[str, str]] = []
        problems: list[str] = []
        for index, raw in enumerate(args["todos"], 1):
            if not isinstance(raw, dict):
                problems.append(f"item {index} must be an object")
                continue
            content = str(raw.get("content") or "").strip()
            status = str(raw.get("status") or "pending").strip()
            if not content:
                problems.append(f"item {index} has empty content")
            if status not in _MARK:
                problems.append(f"item {index} has unknown status {status!r}")
            items.append({"content": content, "status": status})
        if problems:
            return ToolResult("Error: " + "; ".join(problems), is_error=True)
        self.items = items
        done = sum(1 for i in items if i["status"] == "completed")
        lines = [f"{_MARK[i['status']]} {i['content']}" for i in items]
        return ToolResult(
            f"Todo list updated ({done}/{len(items)} done):\n" + "\n".join(lines),
            meta={"todos": items},
        )
