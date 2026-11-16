"""MCP JSON-RPC handler, transport-agnostic: one dict in, one dict (or None) out."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from karox6 import __version__
from karox6.mcp.direct import READ_ONLY, DirectTools
from karox6.mcp.redact import MARK, redact, redact_json
from karox6.mcp.runs import REPLY_CAP, RunError, RunManager, reply_text

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
    "to get only what is new (a short 'log', one line per action). If pending_approvals "
    "is not empty, a destructive command is waiting: ask the user, then answer with "
    "karox_approve. Review the result with "
    "karox_diff. Give karox_run a unique request_id so a retried call never starts the "
    "same work twice."
)
SECRETS_NOTE = (
    " Likely secrets in replies (keys, tokens, passwords) show as [secret removed: ...]; "
    "the files keep the real values, so never write such a marker back."
)
MARKER_REFUSED = (
    "Not executed: the arguments contain '[secret removed: ...]', a marker KaroX shows in "
    "place of a secret. The file holds the real value and writing the marker would "
    "destroy it: change the text around it, or ask the user to edit the secret."
)

_RUN_ID = {"type": "string", "description": "run id returned by karox_run"}
_DETAIL = {
    "type": "string",
    "enum": ["brief", "full"],
    "description": "brief (default): 'log', one line per action; full: raw 'events'",
}
_READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "karox_run",
        "title": "Start a KaroX task",
        "description": (
            "Start the coding agent on a task in a project directory. Returns run_id at once, "
            "or after wait_seconds with the first progress (the agent keeps working in the "
            "background); then poll karox_status. Pass session_id to continue an earlier "
            "conversation."
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
                "detail": _DETAIL,
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
            "cancelled), what happened since index 'since' (log: one line per action), "
            "pending approvals, final_text when done. wait_seconds (max 50) long-polls until "
            "something new happens."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": _RUN_ID,
                "since": {"type": "integer", "description": "first event index to return"},
                "wait_seconds": {"type": "number", "description": "long-poll time, max 50"},
                "detail": _DETAIL,
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
    """Serves either the KaroX agent (``runs``: karox_* tools) or, in direct mode, the
    agent's own tools for the client's model (``direct``).

    ``redactor`` masks secrets in every tool reply before it leaves (None: replies go
    out as they are); the CLI passes one that also knows this process's secret values."""

    def __init__(
        self,
        runs: RunManager | None = None,
        *,
        direct: DirectTools | None = None,
        name: str = "karox",
        redactor: Callable[[str], str] | None = redact,
    ) -> None:
        if (runs is None) == (direct is None):
            raise ValueError("McpServer needs exactly one of runs and direct")
        self.runs = runs
        self.direct = direct
        self.name = name
        self.redactor = redactor
        self._in_flight: dict[tuple[str, str], asyncio.Future[dict[str, Any]]] = {}

    async def handle(self, message: Any, *, peer: str = "") -> dict[str, Any] | None:
        """Answer one JSON-RPC message; notifications and client responses get None.

        ``peer`` names the client (its HTTP session): `notifications/cancelled` stops that
        client's request with the given id, which then gets no answer. A cancelled
        karox_run or karox_status only stops waiting; the run itself goes on."""
        if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
            return _error(None, INVALID_REQUEST, "not a JSON-RPC 2.0 message")
        method = message.get("method")
        if not isinstance(method, str):
            return None  # a response to a server request: we send none
        if "id" not in message:
            if method == "notifications/cancelled":
                self._cancel(peer, message.get("params"))
            return None  # other notifications (initialized, ...) need nothing
        request_id = message["id"]
        params = message.get("params") or {}
        if not isinstance(params, dict):
            return _error(request_id, INVALID_PARAMS, "params must be an object")
        key = (peer, json.dumps(request_id))
        work = asyncio.ensure_future(self._dispatch(method, params))
        if method != "initialize":  # a client must not cancel initialize
            self._in_flight[key] = work
        try:
            result = await work
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if work.cancelled() and current is not None and not current.cancelling():
                return None  # cancelled by the client, who expects no answer
            raise
        except _RpcError as exc:
            return _error(request_id, exc.code, exc.message)
        except Exception as exc:  # pragma: no cover - defensive
            text = self._mask(f"{type(exc).__name__}: {exc}")
            return _error(request_id, INTERNAL_ERROR, text)
        finally:
            if self._in_flight.get(key) is work:
                del self._in_flight[key]
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _cancel(self, peer: str, params: Any) -> None:
        if isinstance(params, dict) and "requestId" in params:
            work = self._in_flight.get((peer, json.dumps(params["requestId"])))
            if work is not None:
                work.cancel()

    async def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            asked = params.get("protocolVersion")
            version = asked if asked in SUPPORTED_VERSIONS else LATEST
            instructions = self.direct.instructions if self.direct else INSTRUCTIONS
            return {
                "protocolVersion": version,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": self.name, "title": "KaroX", "version": __version__},
                "instructions": instructions + (SECRETS_NOTE if self.redactor is not None else ""),
            }
        if method == "ping":
            return {}
        if method == "tools/list":
            return {"tools": self.direct.specs() if self.direct else TOOLS}
        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if not isinstance(name, str) or not isinstance(arguments, dict):
                raise _RpcError(INVALID_PARAMS, "tools/call needs name and an arguments object")
            if self.direct is None:
                return await self._call(name, arguments)
            if name not in self.direct.tools:
                raise _RpcError(INVALID_PARAMS, f"unknown tool {name!r}")
            if (
                self.redactor is not None
                and name not in READ_ONLY
                and MARK in json.dumps(arguments)
            ):
                return {"content": [{"type": "text", "text": MARKER_REFUSED}], "isError": True}
            result = await self.direct.call(name, arguments)
            for item in result["content"]:
                if item.get("type") == "text":  # a screenshot also carries an image item
                    item["text"] = self._mask(item["text"])
            return result
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
            text = self._mask(text)
            text = text if len(text) <= REPLY_CAP else text[: REPLY_CAP - 100] + " [cut]"
            return {"content": [{"type": "text", "text": text}], "isError": True}
        if self.redactor is not None:
            # Masked as one JSON text (a key beside its value) before fitting: a marker
            # can be longer than the secret it replaces.
            data = redact_json(data, self.redactor)
        data, text = fit_reply(data)
        return {"content": [{"type": "text", "text": text}], "structuredContent": data}

    def _mask(self, text: str) -> str:
        return self.redactor(text) if self.redactor is not None else text

    async def _tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        runs = self.runs
        if runs is None:
            raise _RpcError(INVALID_PARAMS, f"unknown tool {name!r}")
        if name == "karox_run":
            # Check every argument before starting: a run that starts and then fails on a
            # bad argument would keep the workspace busy without the client knowing its id.
            wait = float(args.get("wait_seconds") or 0)
            detail = str(args.get("detail") or "brief")
            if detail not in ("brief", "full"):
                raise RunError("detail must be 'brief' or 'full'")
            run, duplicate = runs.start(
                str(args["task"]),
                cwd=_opt_str(args, "cwd"),
                session_id=_opt_str(args, "session_id"),
                request_id=_opt_str(args, "request_id"),
            )
            view = (
                await runs.status(run.run_id, wait_s=wait, detail=detail) if wait else run.summary()
            )
            return {**view, "duplicate": duplicate}
        if name == "karox_status":
            return await runs.status(
                str(args["run_id"]),
                since=int(args.get("since") or 0),
                wait_s=float(args.get("wait_seconds") or 0),
                detail=str(args.get("detail") or "brief"),
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


def fit_reply(data: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """The reply and its text, cut to at most REPLY_CAP characters (clients choke on more).

    Tools keep their big parts in bounds where they can say how to get the rest
    (karox_status pages with since/next, karox_diff says what to ask for). This guards
    everything else: the largest field loses its tail (text) or its first items (a list,
    such as the oldest runs) until the reply fits, and "cut" says what was left out."""
    text = reply_text(data)
    if len(text) <= REPLY_CAP:
        return data, text
    data, cuts = dict(data), []
    while (excess := len(text) - REPLY_CAP) > 0:
        key = max((k for k in data if k != "cut"), key=lambda k: len(reply_text(data[k])))
        value, need = data[key], excess + 300  # room for the note below
        if isinstance(value, str):
            data[key] = value[: max(len(value) - need, 0)]
            cuts.append(f"{key}: its last {len(value) - len(data[key])} characters")
        elif isinstance(value, list) and value:
            drop = freed = 0
            while drop < len(value) and freed < need:  # each item frees at least its text
                freed += len(reply_text(value[drop]))
                drop += 1
            data[key] = value[drop:]
            cuts.append(f"{key}: its first {drop} of {len(value)} items")
        else:
            data[key] = None
            cuts.append(f"{key}: all of it")
        data["cut"] = f"reply over {REPLY_CAP} characters; left out " + "; ".join(cuts)
        text = reply_text(data)
    return data, text


def parse_error(detail: str) -> dict[str, Any]:
    return _error(None, PARSE_ERROR, f"parse error: {detail}")


def _opt_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    return str(value) if value not in (None, "") else None


def _bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "allow", "approve")
    return bool(value)
