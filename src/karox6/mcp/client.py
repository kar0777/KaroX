"""MCP client: external MCP servers become KaroX tools named ``mcp__<server>__<tool>``.

Config (the ``mcpServers`` shape other MCP clients use)::

    {"mcpServers": {
        "files": {"command": "npx", "args": ["-y", "some-server"], "env": {"X": "1"}},
        "remote": {"url": "https://host/mcp", "headers": {"X-Team": "a"},
                   "bearer_token_env": "REMOTE_TOKEN", "timeout": 120}
    }}

stdio servers are spawned as child processes (newline-delimited JSON-RPC); HTTP
servers use Streamable HTTP (JSON or SSE replies, ``Mcp-Session-Id`` kept, a new
session started when the server forgets the old one). Secrets come from the environment
(``bearer_token_env``), never from the config text. ``timeout`` (seconds) limits each
tool call. A server that fails to start is skipped with a warning that quotes the end of
its stderr; the run goes on without it. A stdio server that dies mid-run is restarted
on its next call (at most ``MAX_RESTARTS`` times). Calls that time out or are interrupted
are cancelled on the server (``notifications/cancelled``), and server pings are answered.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import re
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import httpx

from karox6 import __version__
from karox6.context.spill import limit_output
from karox6.lifetime import bind_to_karox, join_karox_job
from karox6.tools.base import ToolContext, ToolResult

PROTOCOL_VERSION = "2025-06-18"
CONNECT_TIMEOUT_S = 30.0
CALL_TIMEOUT_S = 600.0
INLINE_CHARS = 30_000
STDERR_TAIL_CHARS = 2_000
MAX_RESTARTS = 3
_NAME_RX = re.compile(r"[^A-Za-z0-9_-]+")


class McpClientError(Exception):
    pass


class Connection(Protocol):
    async def request(self, method: str, params: dict[str, Any], timeout: float) -> Any: ...

    async def notify(self, method: str, params: dict[str, Any]) -> None: ...

    async def close(self) -> None: ...


def _executable(command: str, path: str | None = None) -> str:
    """Windows: ``npx`` is ``npx.cmd``, and CreateProcess never searches PATHEXT, so the
    usual MCP config ``{"command": "npx", ...}`` failed with FileNotFoundError. Resolve
    the name the way a shell would; leave anything unresolvable unchanged."""
    if sys.platform != "win32":
        return command
    return shutil.which(command, path=path) or command


class StdioConnection:
    def __init__(self, command: list[str], env: dict[str, str] | None, cwd: str | None) -> None:
        self.command = command
        self.env = env
        self.cwd = cwd
        self.proc: asyncio.subprocess.Process | None = None
        self.stderr_tail = ""
        self._ids = itertools.count(1)
        self._waiting: dict[int, asyncio.Future[Any]] = {}
        self._reader: asyncio.Task[None] | None = None
        self._stderr: asyncio.Task[None] | None = None
        self._replies: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    @property
    def alive(self) -> bool:
        """Running and still talking: a closed stdout counts as gone even before the
        exit status arrives."""
        return (
            self.proc is not None
            and self.proc.returncode is None
            and self._reader is not None
            and not self._reader.done()
        )

    async def _settle(self) -> None:
        """Let the stderr reader catch the last words of a server that just died.
        (asyncio.wait neither raises if that task was cancelled nor cancels it.)"""
        if self._stderr is not None and not self._stderr.done():
            await asyncio.wait({self._stderr}, timeout=0.5)

    async def start(self) -> None:
        env = {**os.environ, **(self.env or {})}
        join_karox_job()  # Windows: the server is born in the kill-on-exit job
        self.proc = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=self.cwd,
            limit=16 * 1024 * 1024,
        )
        bind_to_karox(self.proc.pid)
        self._stderr = asyncio.create_task(self._drain_stderr())
        self._reader = asyncio.create_task(self._read())

    async def _drain_stderr(self) -> None:
        """Keep the end of the server's stderr (quoted when it fails); never block it."""
        assert self.proc is not None
        assert self.proc.stderr is not None
        with contextlib.suppress(Exception):
            while chunk := await self.proc.stderr.read(4096):
                text = self.stderr_tail + chunk.decode("utf-8", "replace")
                self.stderr_tail = text[-STDERR_TAIL_CHARS:]

    def gone(self, what: str) -> McpClientError:
        tail = self.stderr_tail.strip()
        if tail:
            what += f"; its stderr ends with: {tail[-500:]}"
        return McpClientError(what)

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
                if not isinstance(message, dict):
                    continue
                if "method" in message:
                    if "id" in message:  # a request from the server: answer, never ignore
                        task = asyncio.create_task(self._answer(message))
                        self._replies.add(task)
                        task.add_done_callback(self._replies.discard)
                    continue
                if "id" in message:
                    future = self._waiting.pop(message["id"], None)
                    if future is not None and not future.done():
                        future.set_result(message)
        finally:
            await self._settle()
            error = self.gone("server process exited")
            for future in self._waiting.values():
                if not future.done():
                    future.set_exception(error)
            self._waiting.clear()

    async def _answer(self, message: dict[str, Any]) -> None:
        method = str(message.get("method"))
        reply: dict[str, Any] = {"jsonrpc": "2.0", "id": message["id"]}
        if method == "ping":
            reply["result"] = {}
        else:
            reply["error"] = {"code": -32601, "message": f"KaroX does not support {method}"}
        with contextlib.suppress(Exception):
            await self._send(reply)

    async def _send(self, payload: dict[str, Any]) -> None:
        data = (json.dumps(payload) + "\n").encode("utf-8")
        async with self._lock:
            if not self.alive or self.proc is None or self.proc.stdin is None:
                await self._settle()
                raise self.gone("server process is not running")
            try:
                self.proc.stdin.write(data)
                await self.proc.stdin.drain()
            except (OSError, RuntimeError) as exc:  # the pipe broke: the server just died
                await self._settle()
                raise self.gone("server process exited") from exc

    def _cancel(self, rid: int, reason: str) -> None:
        """Tell the server to drop a request (best effort, without waiting)."""
        if not self.alive or self.proc is None or self.proc.stdin is None:
            return
        note = {
            "jsonrpc": "2.0",
            "method": "notifications/cancelled",
            "params": {"requestId": rid, "reason": reason},
        }
        with contextlib.suppress(Exception):
            self.proc.stdin.write((json.dumps(note) + "\n").encode("utf-8"))

    async def request(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        rid = next(self._ids)
        future: asyncio.Future[Any] = asyncio.get_running_loop().create_future()
        self._waiting[rid] = future
        sent = False
        try:
            async with asyncio.timeout(timeout):  # sending counts too: a stuck pipe times out
                await self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
                sent = True
                reply = await future
        except (TimeoutError, asyncio.CancelledError) as exc:
            if sent and method != "initialize":  # only requests the server has seen
                timed_out = isinstance(exc, TimeoutError)
                self._cancel(rid, f"timed out after {timeout:g}s" if timed_out else "interrupted")
            raise
        finally:
            self._waiting.pop(rid, None)
        return _result(reply)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        proc = self.proc
        try:
            if proc is not None:
                with contextlib.suppress(Exception):
                    if proc.stdin is not None:
                        proc.stdin.close()  # also after an exit: frees the pipe transport
                if proc.returncode is None:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(proc.wait(), 3)
        finally:
            # Runs even if close() itself is cancelled: no process or task is left behind.
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
            tasks = [t for t in (self._stderr, self._reader, *self._replies) if t is not None]
            for task in tasks:
                task.cancel()
            if proc is not None and proc.returncode is None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), 3)
            if tasks:
                await asyncio.wait(tasks, timeout=1)


class _SessionExpired(Exception):
    def __init__(self, session: str | None) -> None:
        super().__init__(session)
        self.session = session


class HttpConnection:
    def __init__(
        self,
        url: str,
        headers: dict[str, str] | None = None,
        *,
        call_timeout: float = CALL_TIMEOUT_S,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.url = url
        self.headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            **(headers or {}),
        }
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(max(call_timeout, CONNECT_TIMEOUT_S) + 5, connect=20.0),
            transport=transport,
        )
        self.session_id: str | None = None
        self.sessions_started = 0
        self._initialized = False
        self._init_params: dict[str, Any] | None = None
        self._ids = itertools.count(1)
        self._background: set[asyncio.Task[Any]] = set()
        self._session_lock = asyncio.Lock()

    def _headers(self, *, fresh: bool = False) -> dict[str, str]:
        headers = dict(self.headers)
        if not fresh:  # an initialize request carries neither header
            if self._initialized:
                headers["MCP-Protocol-Version"] = PROTOCOL_VERSION
            if self.session_id:
                headers["Mcp-Session-Id"] = self.session_id
        return headers

    async def request(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        if method == "initialize":
            self._init_params = params
        try:
            return await self._request(method, params, timeout)
        except _SessionExpired as expired:
            # The server forgot the session (restart, idle expiry): the spec says start a
            # new one with a fresh initialize, then repeat the request once.
            await self._renew(expired.session, timeout)
            try:
                return await self._request(method, params, timeout)
            except _SessionExpired:
                raise McpClientError("HTTP 404: the server dropped the new session too") from None

    async def _renew(self, stale: str | None, timeout: float) -> None:
        if self._init_params is None:
            raise McpClientError("HTTP 404 before the session was initialized")
        async with asyncio.timeout(timeout), self._session_lock:  # waiting counts too
            if self.session_id != stale:
                return  # a concurrent call already started the new session
            await self._request("initialize", self._init_params, min(timeout, CONNECT_TIMEOUT_S))
            await self.notify("notifications/initialized", {})

    async def _request(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        rid = next(self._ids)
        payload = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        fresh = method == "initialize"
        sent_session = None if fresh else self.session_id
        try:
            response = await asyncio.wait_for(
                self.client.post(self.url, json=payload, headers=self._headers(fresh=fresh)),
                timeout,
            )
        except (TimeoutError, asyncio.CancelledError) as exc:
            if method != "initialize":
                reason = "interrupted" if isinstance(exc, asyncio.CancelledError) else "timed out"
                self._cancel(rid, reason)
            raise
        if response.status_code == 404 and sent_session:
            raise _SessionExpired(sent_session)
        if response.status_code >= 400:
            raise McpClientError(f"HTTP {response.status_code}: {response.text[:300]}")
        kind = response.headers.get("content-type", "")
        reply: Any = None
        if "text/event-stream" in kind:
            for message in _sse_messages(response.text):
                if (
                    isinstance(message, dict)
                    and message.get("id") == rid
                    and "method" not in message
                ):
                    reply = message
                    break
            else:
                raise McpClientError("no reply in the event stream")
        else:
            reply = response.json()
        result = _result(reply)
        if method == "initialize":
            self.session_id = response.headers.get("mcp-session-id") or None
            self._initialized = True
            self.sessions_started += 1
        return result

    def _cancel(self, rid: int, reason: str) -> None:
        params = {"requestId": rid, "reason": reason}
        task = asyncio.get_running_loop().create_task(
            self._quiet_notify("notifications/cancelled", params)
        )
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _quiet_notify(self, method: str, params: dict[str, Any]) -> None:
        with contextlib.suppress(Exception):
            await asyncio.wait_for(self.notify(method, params), 10)

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params}
        await self.client.post(self.url, json=payload, headers=self._headers())

    async def close(self) -> None:
        if self._background:  # pending cancel notices; asyncio.wait never raises for them
            _, pending = await asyncio.wait(set(self._background), timeout=2)
            for task in pending:  # a stalled notice must not outlive the client
                task.cancel()
            if pending:
                await asyncio.wait(pending, timeout=1)
        if self.session_id:
            with contextlib.suppress(Exception):
                await self.client.delete(self.url, headers=self._headers())
        await self.client.aclose()


@dataclass(slots=True)
class McpServerHandle:
    name: str
    connection: Connection
    tools: list[dict[str, Any]]
    spec: dict[str, Any] = field(default_factory=dict)
    call_timeout: float = CALL_TIMEOUT_S
    restarts: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def ensure_running(self) -> None:
        """Restart a stdio server that died since the last call (bounded)."""
        connection = self.connection
        if not isinstance(connection, StdioConnection) or connection.alive or not self.spec:
            return
        async with self.lock:
            if self.connection is not connection:
                return  # another call restarted it meanwhile
            if self.restarts >= MAX_RESTARTS:
                raise connection.gone(f"server exited and was restarted {MAX_RESTARTS} times")
            self.restarts += 1
            await connection.close()
            fresh = await _open(self.spec)
            if not isinstance(fresh, StdioConnection):  # the spec of a stdio server
                await fresh.close()
                raise McpClientError("cannot restart: the server is no longer a local command")
            try:
                await _initialize(fresh)
            except BaseException as exc:
                await fresh.close()
                self.connection = fresh  # dead as well; its stderr tells the next caller why
                if isinstance(exc, TimeoutError):
                    raise fresh.gone(
                        f"restarted server did not initialize within {CONNECT_TIMEOUT_S:g}s"
                    ) from None
                raise
            self.connection = fresh


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
        server = self.server
        try:
            await server.ensure_running()
            result = await server.connection.request(
                "tools/call", {"name": self.remote_name, "arguments": args}, server.call_timeout
            )
        except TimeoutError:
            return ToolResult(
                f"MCP server {server.name}: {self.remote_name} did not answer within "
                f"{server.call_timeout:g}s; the call was cancelled.",
                is_error=True,
            )
        except (McpClientError, httpx.HTTPError, OSError) as exc:
            return ToolResult(f"MCP server {server.name} failed: {exc}", is_error=True)
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


def call_timeout(spec: dict[str, Any]) -> float:
    """Per-call limit from the server's ``timeout`` (seconds), else the default."""
    value = spec.get("timeout")
    if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
        return float(value)
    return CALL_TIMEOUT_S


async def _open(spec: dict[str, Any]) -> StdioConnection | HttpConnection:
    if spec.get("url"):
        headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
        token_env = spec.get("bearer_token_env")
        if token_env:
            token = os.environ.get(str(token_env))
            if not token:
                raise McpClientError(f"environment variable {token_env} is not set")
            headers["Authorization"] = f"Bearer {token}"
        return HttpConnection(str(spec["url"]), headers, call_timeout=call_timeout(spec))
    if spec.get("command"):
        env = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
        path = env.get("PATH") or env.get("Path")
        command = [
            _executable(str(spec["command"]), path),
            *[str(a) for a in spec.get("args") or []],
        ]
        connection = StdioConnection(command, env, spec.get("cwd"))
        await connection.start()
        return connection
    raise McpClientError("needs 'command' (stdio) or 'url' (HTTP)")


async def _initialize(connection: Connection) -> None:
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


async def connect(name: str, spec: dict[str, Any]) -> McpServerHandle:
    connection = await _open(spec)
    try:
        await _initialize(connection)
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
    return McpServerHandle(name, connection, tools, spec=spec, call_timeout=call_timeout(spec))


def configured_servers(path: Path) -> dict[str, dict[str, Any]]:
    """The servers a config file names (name -> its spec); empty when there is no file or
    it cannot be read."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    servers = data.get("mcpServers") or data.get("servers") or {}
    if not isinstance(servers, dict):
        return {}
    return {str(name): spec for name, spec in servers.items() if isinstance(spec, dict)}


def describe_server(spec: dict[str, Any]) -> tuple[str, str]:
    """(transport, where) of a configured server, for display: no environment, no headers
    (they may hold tokens)."""
    if spec.get("url"):
        return "http", str(spec["url"])
    command = [str(spec.get("command") or ""), *(str(a) for a in spec.get("args") or [])]
    return "stdio", " ".join(part for part in command if part)


class ServerSkipped(str):
    """The warning for a server that could not be connected: the text as before, plus the
    server's name and the reason for callers that keep track (the chat's /mcp screen)."""

    server: str
    reason: str

    def __new__(cls, server: str, reason: str) -> ServerSkipped:
        self = super().__new__(cls, f"karox: MCP server {server!r} skipped: {reason}\n")
        self.server = server
        self.reason = reason
        return self


async def load_mcp_tools(
    path: Path, *, warn: Callable[[str], object] | None = None
) -> tuple[list[McpTool], list[McpServerHandle]]:
    """Connect every configured server; return their tools and handles (close them later).

    On failure or cancellation part-way, the servers already connected are closed here:
    the caller never received their handles. A server that cannot be connected is reported
    to ``warn`` as a :class:`ServerSkipped` and left out.
    """
    report = warn or sys.stderr.write
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: the MCP config must be a JSON object")
    servers = data.get("mcpServers") or data.get("servers") or {}
    if not isinstance(servers, dict):
        raise ValueError(f"{path}: 'mcpServers' must be an object")
    connected: list[McpServerHandle] = []

    async def one(name: str, spec: Any) -> None:
        if not isinstance(spec, dict) or spec.get("disabled"):
            return
        try:
            handle = await asyncio.wait_for(connect(name, spec), CONNECT_TIMEOUT_S + 5)
        except Exception as exc:
            reason = str(exc) or f"{type(exc).__name__} (no answer in {CONNECT_TIMEOUT_S:g}s)"
            report(ServerSkipped(name, reason))
            return
        connected.append(handle)

    try:
        names = list(servers)
        await asyncio.gather(*(one(n, servers[n]) for n in names))
        order = {n: i for i, n in enumerate(names)}
        handles = sorted(connected, key=lambda h: order.get(h.name, len(order)))
        tools: list[McpTool] = []
        seen: set[str] = set()
        for handle in handles:
            for spec in handle.tools:
                try:
                    tool = McpTool(handle, spec)
                except Exception as exc:
                    report(
                        f"karox: MCP tool from {handle.name!r} skipped: {type(exc).__name__}: "
                        f"{exc}\n"
                    )
                    continue
                if tool.name not in seen:
                    seen.add(tool.name)
                    tools.append(tool)
    except BaseException:
        await asyncio.shield(close_all(connected))
        raise
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
    # SSE lines end only at CRLF, LF or CR; splitlines() also breaks at U+2028 and
    # friends, which JSON may carry unescaped, and the message was silently dropped.
    for line in [*re.split(r"\r\n|\n|\r", text), ""]:
        if line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line.strip() and data:
            with contextlib.suppress(ValueError):
                messages.append(json.loads("\n".join(data)))
            data = []
    return messages
