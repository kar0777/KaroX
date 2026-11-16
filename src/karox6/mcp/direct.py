"""Direct mode (`karox mcp --direct`): the client's own model calls KaroX's agent tools.

No second model: read, write, edit, grep, glob, the shell and the job tools are served
as MCP tools with their agent implementations. One ToolContext lives as long as the
server, so read-before-write state and background jobs carry over between calls.
Paths must stay inside the root unless ``any_path``; a destructive shell command runs
only as ``approval_mode`` allows ("ask": when the call repeats with ``confirmed: true``).
"""

from __future__ import annotations

import base64
import json
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from karox6.context.spill import limit_output
from karox6.core.session import GENERIC_TOOL_CAP
from karox6.core.store import new_session_id
from karox6.tools.base import ApprovalRequest, Tool, ToolArgsError, ToolContext, parse_arguments
from karox6.tools.files import FileTracker
from karox6.tools.jobs import JobManager
from karox6.tools.registry import build_tools
from karox6.tools.screen import ScreenshotTool
from karox6.tools.shell import ShellInfo, detect_shell

READ_ONLY = frozenset({"read", "grep", "glob", "job_output", "job_list", "screenshot"})
# Hosted clients give up on a tool call after about a minute: a command still running
# after this goes on as a background job (the shell's own timeout behaviour) instead.
CALL_WAIT_MS = 50_000
# The tools served, with titles: the agent's set without todo_write, a plan list for
# KaroX's own model.
DIRECT_TOOLS = {
    "read": "Read a file or directory",
    "grep": "Search file contents",
    "glob": "Find files by name",
    "write": "Write a file",
    "edit": "Edit a file",
    "bash": "Run a shell command",
    "pwsh": "Run a PowerShell command",
    "job_output": "Background job output",
    "job_list": "List background jobs",
    "job_kill": "Stop a background job",
    "screenshot": "Screenshot of the screen or a window",
}
SHELL_TIMEOUT = {
    "type": "integer",
    "description": f"Wait up to this many ms (default and max {CALL_WAIT_MS}); a command "
    "still running then goes on as a background job.",
}
CONFIRMED = {
    "type": "boolean",
    "description": "true only when repeating a refused destructive command after the user "
    "confirmed exactly that command in the chat",
}


CONFIRM_TTL_S = 900.0  # a refused call can be confirmed for 15 minutes
MAX_PENDING = 100


def _wait_ms(raw: Any) -> int:
    """A client's timeout (ms; a number or a numeric string) capped at CALL_WAIT_MS."""
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return CALL_WAIT_MS
    return CALL_WAIT_MS if value <= 0 else min(value, CALL_WAIT_MS)


class OutsideRoot(Exception):
    """A path outside the server root (the server runs without --any-cwd)."""


@dataclass(slots=True)
class ConfinedContext(ToolContext):
    """ToolContext whose paths must lie inside the workspace or the server's own output
    directory (long outputs are spilled there, and the tools point the model at them)."""

    any_path: bool = False

    def resolve(self, raw: str) -> Path:
        # Not super(): a slots dataclass is a new class, and zero-argument super() breaks.
        path = ToolContext.resolve(self, raw)
        real = path.resolve()  # a symlink inside the root may point anywhere
        if self.any_path or any(real.is_relative_to(p) for p in (self.workspace, self.session_dir)):
            return path
        raise OutsideRoot(
            f"{path} is outside the project root {self.workspace} (the server was started "
            "without --any-cwd)"
        )


class DirectTools:
    def __init__(
        self,
        root: Path,
        *,
        home: Path,
        any_path: bool = False,
        approval_mode: str = "ask",
        shell: ShellInfo | None = None,
        screenshot: bool = True,
    ) -> None:
        self.root = root.resolve()
        self.any_path = any_path
        self.approval_mode = approval_mode
        # Refused destructive calls awaiting the user's answer: (name, arguments) -> expiry.
        # confirmed: true runs only a call refused before, once: a model cannot skip the
        # refusal that tells it to ask, or confirm one command and run another.
        self._pending: dict[str, float] = {}
        shell = shell or detect_shell()
        self.tools: dict[str, Tool] = {
            t.name: t for t in build_tools(shell) if t.name in DIRECT_TOOLS
        }
        if screenshot:
            self.tools["screenshot"] = ScreenshotTool()
        self.shell_name = "bash" if shell.kind == "bash" else "pwsh"
        self.ctx = ConfinedContext(
            workspace=self.root,
            session_dir=(home / "mcp-direct" / new_session_id()).resolve(),
            files=FileTracker(self.root),
            jobs=JobManager(),
            shell=shell,
            any_path=any_path,
        )

    @property
    def instructions(self) -> str:
        shell = self.shell_name
        where = "" if self.any_path else "; paths outside it are refused"
        danger = {
            "ask": "is refused until the user confirms it: ask them in the chat, showing the "
            f"exact command, and only if they agree call {shell} again with the same arguments "
            "and confirmed: true.",
            "allow": "runs without asking, so check every delete or move target first.",
        }.get(self.approval_mode, "is refused; leave such steps to the user.")
        see = (
            " Call screenshot to see the user's screen or one window (list_windows: true "
            "lists them)."
            if "screenshot" in self.tools
            else ""
        )
        return (
            "KaroX gives you direct access to a project directory on the user's machine: "
            f"{self.root}. You do the work yourself with its tools: read, grep and glob to "
            "look around, edit for targeted changes and write for new files (read an existing "
            f"file before replacing it), {shell} to run commands and tests (check the "
            f"[exit code: N] marker). Relative paths start at the project directory{where}. "
            f"A call returns within {CALL_WAIT_MS // 1000} s: a command still running then "
            "goes on as a background job; read it with job_output (wait: true waits for it), "
            "stop it with job_kill. A destructive command (recursive deletes "
            f"outside the project, force pushes, ...) {danger}{see}"
        )

    def specs(self) -> list[dict[str, Any]]:
        return [self._spec(tool) for tool in self.tools.values()]

    def _spec(self, tool: Tool) -> dict[str, Any]:
        schema = tool.parameters
        if tool.name == self.shell_name:
            properties = {**schema["properties"], "timeout": SHELL_TIMEOUT}
            if self.approval_mode == "ask":
                properties["confirmed"] = CONFIRMED
            schema = {**schema, "properties": properties}
        read_only = tool.name in READ_ONLY
        return {
            "name": tool.name,
            "title": DIRECT_TOOLS[tool.name],
            "description": tool.description,
            "inputSchema": schema,
            "annotations": {
                "readOnlyHint": read_only,
                "destructiveHint": not read_only,
                # Only the shell reaches beyond this machine (downloads, git, ...).
                "openWorldHint": tool.name == self.shell_name,
            },
        }

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run one tool call; the result is text content (``isError`` on failure).
        ``name`` must be one of ``self.tools``."""
        tool = self.tools[name]
        key = json.dumps(
            [name, {k: v for k, v in arguments.items() if k != "confirmed"}],
            sort_keys=True,
            default=str,
        )
        now = time.monotonic()
        self._pending = {k: t for k, t in self._pending.items() if t > now}
        confirmed = (
            str(arguments.get("confirmed")).strip().lower() == "true" and key in self._pending
        )
        asked: list[ApprovalRequest] = []

        async def approve(request: ApprovalRequest) -> bool:
            asked.append(request)
            if self.approval_mode == "allow":
                return True
            if self.approval_mode == "ask" and confirmed:
                self._pending.pop(key, None)  # one confirmation runs one call
                return True
            return False

        # A context per call: concurrent calls must not see each other's approver. Files
        # and jobs are shared, so read-before-write and background jobs carry over.
        ctx = replace(self.ctx, approve=approve)
        try:
            args = parse_arguments(json.dumps(arguments), tool.parameters)
            args.pop("confirmed", None)
            if name == self.shell_name:
                args["timeout"] = _wait_ms(args.get("timeout"))
            elif name == "job_output" and args.get("timeout"):
                args["timeout"] = _wait_ms(args["timeout"])
            result = await tool.run(args, ctx)
            text, is_error, meta = result.content, result.is_error, result.meta
        except (ToolArgsError, OutsideRoot) as exc:
            text, is_error, meta = f"Error: {exc}.", True, {}
        except Exception as exc:
            text, is_error, meta = f"Error: {name} failed: {type(exc).__name__}: {exc}", True, {}
        if meta.get("blocked_rule") and asked and self.approval_mode == "ask":
            request = asked[-1]
            if len(self._pending) < MAX_PENDING:
                self._pending[key] = now + CONFIRM_TTL_S
            text = (
                f"Not executed: this command matches the destructive-command rule "
                f"'{request.rule}' ({request.reason}). Ask the user in the chat whether to "
                f"run it, quoting it exactly: {request.summary}\nOnly if they confirm, call "
                f"{name} again with the same arguments and confirmed: true."
            )
        if name != "read":
            text, _ = limit_output(
                text, spill_dir=ctx.spill_dir, name=name, max_chars=GENERIC_TOOL_CAP
            )
        notes = self.ctx.jobs.drain_finished()
        if notes:
            text += "\n\n" + "\n".join(notes)
        content: list[dict[str, Any]] = [{"type": "text", "text": text}]
        if png := meta.get("image_png"):
            data = base64.b64encode(png).decode("ascii")
            content.insert(0, {"type": "image", "data": data, "mimeType": "image/png"})
        return {"content": content, "isError": is_error}

    async def shutdown(self) -> None:
        await self.ctx.jobs.shutdown()
