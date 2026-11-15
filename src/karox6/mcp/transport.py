"""MCP transports: stdio (newline-delimited JSON-RPC) and Streamable HTTP.

The HTTP side is a deliberately small HTTP/1.1 server on asyncio streams (no web
framework): POST carries JSON-RPC and is answered with application/json, GET has no
server-initiated stream (405), DELETE ends an MCP session. Every request must carry
the access token, either as the last path segment (``/mcp/<token>``, for clients
such as ChatGPT connectors that only take a URL) or as ``Authorization: Bearer``.
Browser origins outside the allow-list are refused (DNS-rebinding protection).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import secrets
import sys
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

from karox6.mcp.protocol import McpServer, parse_error

MAX_BODY = 4 * 1024 * 1024
MAX_HEADERS = 100
DEFAULT_ORIGINS = ("https://chatgpt.com", "https://chat.openai.com")


# -- stdio ------------------------------------------------------------------------------


async def serve_stdio(server: McpServer) -> None:
    """Serve one client on stdin/stdout until stdin closes."""
    loop = asyncio.get_running_loop()
    lock = asyncio.Lock()

    async def write(payload: Any) -> None:
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    async def lines() -> Any:
        while True:
            line = await loop.run_in_executor(None, sys.stdin.buffer.readline)
            if not line:
                return
            yield line

    await serve_lines(server, lines(), write)


async def serve_lines(
    server: McpServer,
    lines: Any,
    write: Callable[[Any], Awaitable[None]],
) -> None:
    """Handle each JSON-RPC line concurrently (a long-poll must not block other calls)."""
    tasks: set[asyncio.Task[None]] = set()

    async def one(raw: bytes) -> None:
        try:
            message = json.loads(raw)
        except ValueError as exc:
            await write(parse_error(str(exc)))
            return
        if isinstance(message, list):
            replies = [r for r in [await server.handle(m) for m in message] if r is not None]
            if replies:
                await write(replies)
            return
        reply = await server.handle(message)
        if reply is not None:
            await write(reply)

    async for raw in lines:
        if not raw.strip():
            continue
        task = asyncio.create_task(one(raw))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


# -- Streamable HTTP ------------------------------------------------------------------------


class HttpMcp:
    def __init__(
        self,
        server: McpServer,
        *,
        token: str,
        path: str = "/mcp",
        allowed_origins: Iterable[str] = DEFAULT_ORIGINS,
    ) -> None:
        if len(token) < 16:
            raise ValueError("the access token must be at least 16 characters")
        self.server = server
        self.token = token
        self.path = "/" + path.strip("/")
        self.allowed_origins = {o.rstrip("/").lower() for o in allowed_origins}
        self.sessions: dict[str, str] = {}  # MCP session id -> negotiated protocol version

    async def start(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(self._connection, host, port, limit=64 * 1024)

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                request = await _read_request(reader)
                if request is None:
                    break
                status, headers, body = await self.respond(*request)
                keep = request[2].get("connection", "").lower() != "close"
                _write_response(writer, status, headers, body, keep_alive=keep)
                await writer.drain()
                if not keep:
                    break
        except (ConnectionError, asyncio.IncompleteReadError, _BadRequest):
            pass
        finally:
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    async def respond(
        self, method: str, target: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        path = urlsplit(target).path.rstrip("/") or "/"
        if path == "/healthz" and method == "GET":
            return 200, {"Content-Type": "text/plain"}, b"ok\n"
        origin = headers.get("origin")
        if origin and not self._origin_ok(origin):
            return _json(403, {"error": "origin not allowed"})
        cors = self._cors(origin)
        if not self._authorized(path, headers):
            return _json(401 if path.startswith(self.path) else 404, {"error": "not found"}, cors)
        if method == "OPTIONS":
            return (
                204,
                {
                    **cors,
                    "Access-Control-Allow-Methods": "POST, GET, DELETE, OPTIONS",
                    "Access-Control-Allow-Headers": "content-type, authorization, "
                    "mcp-session-id, mcp-protocol-version, last-event-id",
                },
                b"",
            )
        session_id = headers.get("mcp-session-id")
        if method == "DELETE":
            if session_id and self.sessions.pop(session_id, None) is not None:
                return 200, cors, b""
            return _json(404, {"error": "unknown session"}, cors)
        if method == "GET":
            return 405, {**cors, "Allow": "POST, DELETE"}, b""
        if method != "POST":
            return 405, {**cors, "Allow": "POST, DELETE"}, b""
        try:
            message = json.loads(body)
        except ValueError as exc:
            return _json(400, parse_error(str(exc)), cors)
        batch = isinstance(message, list)
        items = message if batch else [message]
        initializing = any(isinstance(m, dict) and m.get("method") == "initialize" for m in items)
        extra: dict[str, str] = {}
        if initializing:
            session_id = secrets.token_hex(16)
            extra["Mcp-Session-Id"] = session_id
        elif not session_id:
            return _json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {"code": -32600, "message": "missing Mcp-Session-Id header"},
                },
                cors,
            )
        elif session_id not in self.sessions:
            return _json(
                404,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32600,
                        "message": "unknown or expired session: initialize again",
                    },
                },
                cors,
            )
        replies = [r for r in [await self.server.handle(m) for m in items] if r is not None]
        if initializing and session_id:
            version = next(
                (
                    r.get("result", {}).get("protocolVersion")
                    for r in replies
                    if isinstance(r.get("result"), dict)
                ),
                "",
            )
            self.sessions[session_id] = str(version or "")
        if not replies:
            return 202, {**cors, **extra}, b""
        payload = replies if batch else replies[0]
        return _json(200, payload, {**cors, **extra})

    def _authorized(self, path: str, headers: dict[str, str]) -> bool:
        if path.startswith(self.path + "/"):
            return hmac.compare_digest(path[len(self.path) + 1 :].encode(), self.token.encode())
        auth = headers.get("authorization", "")
        if path == self.path and auth.lower().startswith("bearer "):
            return hmac.compare_digest(auth[7:].strip().encode(), self.token.encode())
        return False

    def _origin_ok(self, origin: str) -> bool:
        origin = origin.rstrip("/").lower()
        if origin in self.allowed_origins:
            return True
        host = urlsplit(origin).hostname or ""
        return host in ("localhost", "127.0.0.1", "::1")

    def _cors(self, origin: str | None) -> dict[str, str]:
        if not origin:
            return {}
        return {
            "Access-Control-Allow-Origin": origin,
            "Vary": "Origin",
            "Access-Control-Expose-Headers": "Mcp-Session-Id",
        }


class _BadRequest(Exception):
    pass


async def _read_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], bytes] | None:
    line = await reader.readline()
    if not line:
        return None
    parts = line.decode("latin-1").strip().split()
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise _BadRequest(line[:80])
    method, target = parts[0].upper(), parts[1]
    headers: dict[str, str] = {}
    for _ in range(MAX_HEADERS + 1):
        raw = await reader.readline()
        if raw in (b"\r\n", b"\n", b""):
            break
        name, _, value = raw.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    else:
        raise _BadRequest("too many headers")
    body = b""
    if "chunked" in headers.get("transfer-encoding", "").lower():
        chunks = []
        total = 0
        while True:
            size = int((await reader.readline()).split(b";")[0].strip() or b"0", 16)
            if size == 0:
                await reader.readline()
                break
            total += size
            if total > MAX_BODY:
                raise _BadRequest("body too large")
            chunks.append(await reader.readexactly(size))
            await reader.readline()
        body = b"".join(chunks)
    elif headers.get("content-length"):
        length = int(headers["content-length"])
        if length > MAX_BODY:
            raise _BadRequest("body too large")
        body = await reader.readexactly(length)
    return method, target, headers, body


_REASONS = {
    200: "OK",
    202: "Accepted",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
}


def _json(
    status: int, payload: Any, headers: dict[str, str] | None = None
) -> tuple[int, dict[str, str], bytes]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return status, {**(headers or {}), "Content-Type": "application/json"}, body


def _write_response(
    writer: asyncio.StreamWriter,
    status: int,
    headers: dict[str, str],
    body: bytes,
    *,
    keep_alive: bool,
) -> None:
    lines = [f"HTTP/1.1 {status} {_REASONS.get(status, 'OK')}"]
    merged = {
        **headers,
        "Content-Length": str(len(body)),
        "Cache-Control": "no-store",
        "Connection": "keep-alive" if keep_alive else "close",
    }
    lines += [f"{k}: {v}" for k, v in merged.items()]
    writer.write(("\r\n".join(lines) + "\r\n\r\n").encode("latin-1") + body)
