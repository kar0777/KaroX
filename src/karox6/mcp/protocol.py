"""MCP JSON-RPC handler, transport-agnostic: one dict in, one dict (or None) out."""

from __future__ import annotations

import json
from typing import Any

from karox6 import __version__
from karox6.mcp.runs import RunError, RunManager

SUPPORTED_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LATEST = "2025-06-18"

PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

INSTRUCTIONS = (
    "KaroX is a coding agent working in a project directory on the user's machine. "
    "Start work with karox_run (a task in plain words), then call karox_status with "
    "wait_seconds until status is no longer 'running'; pass the returned 'next' as 'since' "
    "to get only new events. If pending_approvals is not empty, a destructive command is "
    "waiting: ask the user, then answer with karox_approve. Review the result with "
    "karox_diff. Give karox_run a unique request_id so a retried call never starts the "
    "same work twice."
)

_RUN_ID = {"type": "string", "description": "run id returned by karox_run"}
_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "karox_run",
        "title": "Start a KaroX task",
        "description": (
            "Start the coding agent on a task in a project directory. Returns at once with "
            "run_id (the agent keeps working in the background); then poll karox_status. "
            "Pass session_id to continue an earlier conversation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "task": {"type": "string", "description": "what the agent should do"},
                "cwd": {
                    "type": "string",
                    "description": "project directory (absolute, or relative to the server "
                    "root); default: the server root",
                },
                "session_id": {"type": "string", "description": "continue this session"},
                "request_id": {
                    "type": "string",
                    "description": "idempotency key: a repeated call with the same key returns "
                    "the existing run instead of starting a new one",
                },
                "wait_seconds": {
                    "type": "number",
                    "description": "wait up to this long (max 50) for the first events",
                },
            },
            "required": ["task"],
        },
        "annotations": {"destructiveHint": True, "openWorldHint": False},
    },
    {
        "name": "karox_status",
        "title": "Run status and events",
        "description": (
            "State of a run: status (running | completed | error | limit | loop | "
            "cancelled), events since index 'since', pending approvals, final_text when done. "
            "wait_seconds (max 50) long-polls until something new happens."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": _RUN_ID,
                "since": {"type": "integer", "description": "first event index to return"},
                "wait_seconds": {"type": "number", "description": "long-poll time, max 50"},
            },
            "required": ["run_id"],
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "karox_approve",
        "title": "Answer an approval",
        "description": (
            "Allow or deny a destructive command the agent is waiting on (see "
            "pending_approvals in karox_status). Ask the user before allowing."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": _RUN_ID,
                "approval_id": {"type": "string"},
                "approve": {"type": "boolean"},
            },
            "required": ["run_id", "approval_id", "approve"],
        },
        "annotations": {"destructiveHint": True, "openWorldHint": False},
    },
    {
        "name": "karox_cancel",
        "title": "Cancel a run",
        "description": "Stop a running task.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": _RUN_ID},
            "required": ["run_id"],
        },
        "annotations": {"destructiveHint": True, "idempotentHint": True, "openWorldHint": False},
    },
    {
        "name": "karox_diff",
        "title": "Diff of the workspace",
        "description": (
            "git diff of the run's project directory against HEAD, plus untracked files."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": _RUN_ID,
                "max_chars": {"type": "integer", "description": "cap (default 40000)"},
            },
            "required": ["run_id"],
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "karox_files",
        "title": "Changed files",
        "description": "Files the run's session changed.",
        "inputSchema": {
            "type": "object",
            "properties": {"run_id": _RUN_ID},
            "required": ["run_id"],
        },
        "annotations": _READ_ONLY,
    },
    {
        "name": "karox_runs",
        "title": "List runs",
        "description": "All runs started through this server, oldest first.",
        "inputSchema": {"type": "object", "properties": {}},
        "annotations": _READ_ONLY,
    },
]


class McpServer:
    def __init__(self, runs: RunManager, *, name: str = "karox") -> None:
        self.runs = runs
        self.name = name

    async def handle(self, message: Any) -> dict[str, Any] | None:
        """Answer one JSON-RPC message; notifications and client responses get None."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = message.get("method")
        if not isinstance(method, str):
            return None  # a response to a server request: we send none
        if "id" not in message:
            return None  # notification (initialized, cancelled, ...)
        request_id = message["id"]
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params must be an object")
        try:
            result = await self._dispatch(method, params)
        except _RpcError as exc:
            return _error(request_id, exc.code, exc.message)
        except Exception as exc:  # pragma: no cover - defensive
            return _error(request_id, INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in SUPPORTED_VERSIONS else LATEST
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "title": "KaroX", "version": __version__},
                "instructions": INSTRUCTIONS,
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": TOOLS}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(arguments, dict):
                raise _RpcError(INVALID_PARAMS, "tools/call needs name and an arguments object")
            return await self._call(name, arguments)
        if method in ("resources/list", "resources/templates/list", "prompts/list"):
            key = {"resources/list": "resources", "prompts/list": "prompts"}.get(
                method, "resourceTemplates"
            )
            return {key: []}
        raise _RpcError(METHOD_NOT_FOUND, f"method {method!r} not found")

    async def _call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            data = await self._tool(name, args)
        except _RpcError:
            raise
        except (RunError, KeyError, TypeError, ValueError, OverflowError) as exc:
            text = str(exc) if isinstance(exc, RunError) else f"bad arguments: {exc}"
            return {"content": [{"type": "text", "text": text}], "isError": True}
        text = json.dumps(data, ensure_ascii=False, indent=1)
        return {"content": [{"type": "text", "text": text}], "structuredContent": data}

    async def _tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        runs = self.runs
        if name == "karox_run":
            run, duplicate = runs.start(
                str(args["task"]),
                cwd=_opt_str(args, "cwd"),
                session_id=_opt_str(args, "session_id"),
                request_id=_opt_str(args, "request_id"),
            )
            wait = float(args.get("wait_seconds") or 0)
            view = await runs.status(run.run_id, wait_s=wait) if wait else run.summary()
            return {**view, "duplicate": duplicate}
        if name == "karox_status":
            return await runs.status(
                str(args["run_id"]),
                since=int(args.get("since") or 0),
                wait_s=float(args.get("wait_seconds") or 0),
            )
        if name == "karox_approve":
            return runs.approve(
                str(args["run_id"]), str(args["approval_id"]), _bool(args["approve"])
            )
        if name == "karox_cancel":
            return await runs.cancel(str(args["run_id"]))
        if name == "karox_diff":
            return await runs.diff(
                str(args["run_id"]), max_chars=int(args.get("max_chars") or 40_000)
            )
        if name == "karox_files":
            return await runs.files(str(args["run_id"]))
        if name == "karox_runs":
            return {"runs": runs.list()}
        raise _RpcError(INVALID_PARAMS, f"unknown tool {name!r}")


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def parse_error(detail: str) -> dict[str, Any]:
    return _error(None, PARSE_ERROR, f"parse error: {detail}")


def _opt_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    return str(value) if value not in (None, "") else None


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "allow", "approve")
    return bool(value)
