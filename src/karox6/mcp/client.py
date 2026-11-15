"""MCP client: external MCP servers become KaroX tools named ``mcp__<server>__<tool>``.

Config (the ``mcpServers`` shape other MCP clients use)::

    {"mcpServers": {
        "files": {"command": "npx", "args": ["-y", "some-server"], "env": {"X": "1"}},
        "remote": {"url": "https://host/mcp", "headers": {"X-Team": "a"},
                   "bearer_token_env": "REMOTE_TOKEN"}
    }}

stdio servers are spawned as child processes (newline-delimited JSON-RPC); HTTP
servers use Streamable HTTP (JSON or SSE replies, ``Mcp-Session-Id`` kept). Secrets
come from the environment (``bearer_token_env``), never from the config text.
A server that fails to start is skipped with a warning; the run goes on without it.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from karox6 import __version__
from karox6.context.spill import limit_output
from karox6.tools.base import ToolContext, ToolResult

PROTOCOL_VERSION = "2025-06-18"
CONNECT_TIMEOUT_S = 30.0
CALL_TIMEOUT_S = 600.0
INLINE_CHARS = 30_000
_NAME_RX = re.compile(r"[^A-Za-z0-9_-]+")


class McpClientError(Exception):
    pass


class Connection(Protocol):
    async def request(self, method: str, params: dict[str, Any], timeout: float) -> Any: ...

    async def notify(self, method: str, params: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...


class StdioConnection:
    def __init__(self, command: list[str], env: dict[str, str] | None, cwd: str | None) -> None:
        self.command = command
        self.env = env
        self.cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None
        self._ids = itertools.count(1)
        self._waiting: dict[int, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        env = {**os.environ, **(self.env or {})}
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            cwd=self.cwd,
            limit=16 * 1024 * 1024,
        )
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        assert self.proc is not None
        assert self.proc.stdout is not None
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue  # servers sometimes log to stdout; ignore non-JSON lines
                if isinstance(message, dict) and "id" in message and "method" not in message:
                    future = self._waiting.pop(message["id"], None)
                    if future is not None and not future.done():
                        future.set_result(message)
        finally:
            for future in self._waiting.values():
                if not future.done():
                    future.set_exception(McpClientError("server process exited"))
            self._waiting.clear()

    async def _send(self, payload: dict[str, Any]) -> None:
        if self.proc is None or self.proc.stdin is None or self.proc.returncode is not None:
            raise McpClientError("server process is not running")
        async with self._lock:
            self.proc.stdin.write((json.dumps(payload) + "\n").encode("utf-8"))
            await self.proc.stdin.drain()

    async def request(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        rid = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._waiting[rid] = future
        await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            reply = await asyncio.wait_for(future, timeout)
        finally:
            self._waiting.pop(rid, None)
        return _result(reply)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        if self.proc is not None and self.proc.returncode is None:
            with contextlib.suppress(Exception):
                if self.proc.stdin is not None:
                    self.proc.stdin.close()
            try:
                await asyncio.wait_for(self.proc.wait(), 3)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    self.proc.kill()
                with contextlib.suppress(Exception):
                    await self.proc.wait()
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader


class HttpConnection:
    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **(headers or {}),
        }
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(CALL_TIMEOUT_S, connect=20.0), transport=transport
        )
        self.session_id: str | None = None
        self._ids = itertools.count(1)

    def _headers(self) -> dict[str, str]:
        headers = dict(self.headers)
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
            headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
        return headers

    async def request(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        rid = next(self._ids)
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        response = await asyncio.wait_for(
            self.client.post(self.url, json=payload, headers=self._headers()), timeout
        )
        if response.status_code >= 400:
            raise McpClientError(f"HTTP {response.status_code}: {response.text[:300]}")
        if method == "initialize" and response.headers.get("mcp-session-id"):
            self.session_id = response.headers["mcp-session-id"]
        kind = response.headers.get("content-type", "")
        if "text/event-stream" in kind:
            for message in _sse_messages(response.text):
                if isinstance(message, dict) and message.get("id") == rid:
                    return _result(message)
            raise McpClientError("no reply in the event stream")
        return _result(response.json())

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        await self.client.post(self.url, json=payload, headers=self._headers())

    async def close(self) -> None:
        if self.session_id:
            with contextlib.suppress(Exception):
                await self.client.delete(self.url, headers=self._headers())
        await self.client.aclose()


@dataclass(slots=True)
class McpServerHandle:
    name: str
    connection: Connection
    tools: list[dict[str, Any]]


class McpTool:
    """A tool of an external MCP server, adapted to the KaroX Tool protocol."""

    def __init__(self, server: McpServerHandle, spec: dict[str, Any]) -> None:
        self.server = server
        self.remote_name = str(spec["name"])
        self.name = tool_name(server.name, self.remote_name)
        text = str(spec.get("description") or spec.get("title") or self.remote_name)
        self.description = f"[MCP server {server.name}] {text}"[:2000]
        schema = spec.get("inputSchema") or {}
        self.parameters: dict[str, Any] = (
            schema if schema.get("type") == "object" else {"type": "object", "properties": {}}
        )
        annotations = spec.get("annotations") or {}
        self.read_only = annotations.get("readOnlyHint") is True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            result = await self.server.connection.request(
                "tools/call", {"name": self.remote_name, "arguments": args}, CALL_TIMEOUT_S
            )
        except (McpClientError, httpx.HTTPError, TimeoutError, OSError) as exc:
            return ToolResult(f"MCP server {self.server.name} failed: {exc}", is_error=True)
        text = render_result(result)
        inline, _ = limit_output(
            text, spill_dir=ctx.spill_dir, name=self.name, max_chars=INLINE_CHARS
        )
        return ToolResult(inline, is_error=bool(result.get("isError")))


def tool_name(server: str, tool: str) -> str:
    name = f"mcp__{_NAME_RX.sub('_', server)}__{_NAME_RX.sub('_', tool)}"
    return name[:64]


def render_result(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in result.get("content") or []:
        kind = block.get("type")
        if kind == "text":
            parts.append(str(block.get("text", "")))
        elif kind == "resource":
            resource = block.get("resource") or {}
            parts.append(str(resource.get("text") or f"[resource {resource.get('uri', '')}]"))
        elif kind == "resource_link":
            parts.append(f"[resource link {block.get('uri', '')}]")
        else:
            parts.append(f"[{kind} content omitted]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False, indent=1))
    return "\n".join(parts) or "(no output)"


async def connect(name: str, spec: dict[str, Any]) -> McpServerHandle:
    connection: StdioConnection | HttpConnection
    if spec.get("url"):
        headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
        token_env = spec.get("bearer_token_env")
        if token_env:
            token = os.environ.get(str(token_env))
            if not token:
                raise McpClientError(f"environment variable {token_env} is not set")
            headers["Authorization"] = f"Bearer {token}"
        connection = HttpConnection(str(spec["url"]), headers)
    elif spec.get("command"):
        command = [str(spec["command"]), *[str(a) for a in spec.get("args") or []]]
        env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
        connection = StdioConnection(command, env, spec.get("cwd"))
        await connection.start()
    else:
        raise McpClientError("needs 'command' (stdio) or 'url' (HTTP)")
    try:
        await connection.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "karox", "version": __version__},
            },
            CONNECT_TIMEOUT_S,
        )
        await connection.notify("notifications/initialized", {})
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        for _ in range(50):
            params = {"cursor": cursor} if cursor else {}
            page = await connection.request("tools/list", params, CONNECT_TIMEOUT_S)
            tools += [t for t in page.get("tools") or [] if isinstance(t, dict) and t.get("name")]
            cursor = page.get("nextCursor")
            if not cursor:
                break
    except BaseException:
        await connection.close()
        raise
    return McpServerHandle(name, connection, tools)


async def load_mcp_tools(path: Path) -> tuple[list[McpTool], list[McpServerHandle]]:
    """Connect every configured server; return their tools and handles (close them later)."""
    data = json.loads(path.read_text(encoding="utf-8"))
    servers = data.get("mcpServers") or data.get("servers") or {}
    if not isinstance(servers, dict):
        raise ValueError(f"{path}: 'mcpServers' must be an object")

    async def one(name: str, spec: Any) -> McpServerHandle | None:
        if not isinstance(spec, dict) or spec.get("disabled"):
            return None
        try:
            return await asyncio.wait_for(connect(name, spec), CONNECT_TIMEOUT_S + 5)
        except Exception as exc:
            sys.stderr.write(f"karox: MCP server {name!r} skipped: {exc}\n")
            return None

    handles = [h for h in await asyncio.gather(*(one(n, s) for n, s in servers.items())) if h]
    tools: list[McpTool] = []
    seen: set[str] = set()
    for handle in handles:
        for spec in handle.tools:
            tool = McpTool(handle, spec)
            if tool.name not in seen:
                seen.add(tool.name)
                tools.append(tool)
    return tools, handles


async def close_all(handles: list[McpServerHandle]) -> None:
    for handle in handles:
        with contextlib.suppress(Exception):
            await handle.connection.close()


def _result(reply: Any) -> Any:
    if not isinstance(reply, dict):
        raise McpClientError("malformed JSON-RPC reply")
    if "error" in reply:
        error = reply["error"] or {}
        raise McpClientError(f"{error.get('code')}: {error.get('message')}")
    return reply.get("result") or {}


def _sse_messages(text: str) -> list[Any]:
    messages: list[Any] = []
    data: list[str] = []
    for line in [*text.splitlines(), ""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line.strip() and data:
            with contextlib.suppress(ValueError):
                messages.append(json.loads("\n".join(data)))
            data = []
    return messages
