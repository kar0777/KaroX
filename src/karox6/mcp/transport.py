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
import os
import re
import secrets
import sys
import threading
from collections.abc import Awaitable, Callable, Iterable
from typing import Any
from urllib.parse import urlsplit

from karox6.mcp.protocol import McpServer, parse_error

MAX_BODY = 4 * 1024 * 1024
DRAIN_LIMIT = 64 * 1024  # a refused request's body is read (and dropped) only up to this
MAX_HEADERS = 100
MAX_CONNECTIONS = 64
MAX_SESSIONS = 1000
MAX_BATCH = 100
HEAD_TIMEOUT_S = 30.0  # also how long an idle keep-alive connection stays open
BODY_TIMEOUT_S = 60.0
DEFAULT_ORIGINS = ("https://chatgpt.com", "https://chat.openai.com")
STDIN_EOF_GRACE_S = 1.0  # clients kill a server ~2 s after closing its stdin


# -- stdio ------------------------------------------------------------------------------


async def serve_stdio(server: McpServer) -> None:
    """Serve one client on stdin/stdout until stdin closes.

    A closed stdin means the client is leaving (and will kill the server within a few
    seconds), so requests still in flight, such as a long-poll, get only a short grace.
    """
    loop = asyncio.get_running_loop()
    lock = asyncio.Lock()
    try:
        fd = sys.stdin.fileno()
    except (AttributeError, OSError, ValueError) as exc:  # no stdin at all (pythonw, ...)
        raise OSError(f"stdin is not readable: {exc}") from None
    incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
    reader = threading.Thread(
        target=_read_stdin, args=(loop, incoming, fd), name="karox-mcp-stdin", daemon=True
    )
    reader.start()

    async def write(payload: Any) -> None:
        data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        async with lock:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()

    async def lines() -> Any:
        while (line := await incoming.get()) is not None:
            yield line

    await serve_lines(server, lines(), write, grace_s=STDIN_EOF_GRACE_S)


def _read_stdin(
    loop: asyncio.AbstractEventLoop, incoming: asyncio.Queue[bytes | None], fd: int
) -> None:
    """Raw reads of stdin in a daemon thread. A read blocked in an executor job would hold
    up the exit after Ctrl+C or SIGTERM (asyncio.run waits for executor threads), and one
    blocked inside sys.stdin.buffer holds its lock during interpreter shutdown. However
    the thread ends, the stream ends with None: the server never waits for a dead reader."""

    def put(item: bytes | None) -> bool:
        try:
            loop.call_soon_threadsafe(incoming.put_nowait, item)
        except RuntimeError:  # the loop is closed: nobody is listening any more
            return False
        return True

    pending = bytearray()
    try:
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                if pending:
                    put(bytes(pending))
                return
            start = len(pending)
            pending += chunk
            while (end := pending.find(b"\n", start)) >= 0:
                if not put(bytes(pending[:end])):
                    return
                del pending[: end + 1]
                start = 0
    finally:
        put(None)


async def serve_lines(
    server: McpServer,
    lines: Any,
    write: Callable[[Any], Awaitable[None]],
    *,
    grace_s: float | None = None,
) -> None:
    """Handle each JSON-RPC line concurrently (a long-poll must not block other calls).

    When the lines end, requests still running get ``grace_s`` (None: all the time they
    need) and are then cancelled."""
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
        _done, late = await asyncio.wait(list(tasks), timeout=grace_s)
        for task in late:
            task.cancel()
        if late:
            await asyncio.gather(*late, return_exceptions=True)


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
        self._open = 0

    async def start(self, host: str, port: int) -> asyncio.Server:
        return await asyncio.start_server(self._connection, host, port, limit=64 * 1024)

    async def _connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self._open >= MAX_CONNECTIONS:
            writer.close()
            return
        self._open += 1
        try:
            while True:
                head = await asyncio.wait_for(_read_head(reader), HEAD_TIMEOUT_S)
                if head is None:
                    break
                method, target, headers = head
                keep = headers.get("connection", "").lower() != "close"
                # Refuse before reading the body, so an unauthenticated client cannot make
                # the server buffer large uploads: a small body is dropped, a large one
                # ends the connection.
                early = self.precheck(method, target, headers)
                if early is not None and _declared_length(headers) > DRAIN_LIMIT:
                    _write_response(writer, *early, keep_alive=False)
                    await writer.drain()
                    break
                limit = MAX_BODY if early is None else DRAIN_LIMIT
                body = await asyncio.wait_for(_read_body(reader, headers, limit), BODY_TIMEOUT_S)
                status, out, payload = early or await self.respond(method, target, headers, body)
                _write_response(writer, status, out, payload, keep_alive=keep)
                await writer.drain()
                if not keep:
                    break
        except _BadRequest:
            with contextlib.suppress(Exception):
                _write_response(writer, *_json(400, {"error": "bad request"}), keep_alive=False)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, TimeoutError):
            pass
        finally:
            self._open -= 1
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def precheck(
        self, method: str, target: str, headers: dict[str, str]
    ) -> tuple[int, dict[str, str], bytes] | None:
        """The answer when the body is not needed: health, refused origin, preflight, no token."""
        path = urlsplit(target).path.rstrip("/") or "/"
        if path == "/healthz" and method == "GET":
            return 200, {"Content-Type": "text/plain"}, b"ok\n"
        origin = headers.get("origin")
        authorized = self._authorized(path, headers)
        preflight = method == "OPTIONS" and (path == self.path or path.startswith(self.path + "/"))
        # The origin check guards against DNS rebinding: a page in the user's browser
        # reaching this server without knowing the token. A request that carries the token
        # comes from a client the user gave it to, so every connector host (ChatGPT, Claude,
        # Le Chat, a web IDE, ...) works without listing its origin. A preflight carries no
        # credentials and only lets the browser send the real request, which still needs
        # the token.
        if origin and not (authorized or preflight or self._origin_ok(origin)):
            return _json(403, {"error": "origin not allowed"})
        cors = self._cors(origin)
        if preflight:
            # Browsers send CORS preflights without credentials: answer before the token check.
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
        if not authorized:
            return _json(401 if path.startswith(self.path) else 404, {"error": "not found"}, cors)
        return None

    async def respond(
        self, method: str, target: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        early = self.precheck(method, target, headers)
        if early is not None:
            return early
        cors = self._cors(headers.get("origin"))
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
        except (ValueError, RecursionError) as exc:
            return _json(400, parse_error(str(exc) or type(exc).__name__), cors)
        batch = isinstance(message, list)
        items = message if batch else [message]
        if batch and not 0 < len(items) <= MAX_BATCH:
            return _json(
                400,
                {
                    "jsonrpc": "2.0",
                    "id": None,
                    "error": {
                        "code": -32600,
                        "message": f"a batch holds 1 to {MAX_BATCH} messages",
                    },
                },
                cors,
            )
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
        peer = session_id or ""
        replies = [
            r for r in [await self.server.handle(m, peer=peer) for m in items] if r is not None
        ]
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
            while len(self.sessions) > MAX_SESSIONS:  # forget the oldest sessions
                self.sessions.pop(next(iter(self.sessions)))
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


_UNIQUE_HEADERS = frozenset({"content-length", "transfer-encoding", "authorization", "host"})


async def _line(reader: asyncio.StreamReader) -> bytes:
    """One complete line; a line over the stream limit or cut by EOF is a bad request."""
    try:
        line = await reader.readline()
    except ValueError as exc:  # longer than the stream limit
        raise _BadRequest("line too long") from exc
    if not line.endswith(b"\n"):
        raise _BadRequest("truncated request")
    return line


async def _read_head(reader: asyncio.StreamReader) -> tuple[str, str, dict[str, str]] | None:
    """Request line and headers; None when the client closed the connection between requests."""
    try:
        first = await reader.readline()
    except ValueError as exc:
        raise _BadRequest("line too long") from exc
    if not first:
        return None
    if not first.endswith(b"\n"):
        raise _BadRequest("truncated request")
    parts = first.decode("latin-1").strip().split()
    if len(parts) != 3 or not parts[2].startswith("HTTP/1."):
        raise _BadRequest(first[:80])
    method, target = parts[0].upper(), parts[1]
    headers: dict[str, str] = {}
    for _ in range(MAX_HEADERS + 1):
        raw = await _line(reader)
        if raw in (b"\r\n", b"\n"):
            break
        text = raw.decode("latin-1").rstrip("\r\n")
        name, sep, value = text.partition(":")
        # No folded lines and no space before the colon (RFC 9112): both enable smuggling.
        if not sep or not name or name != name.strip() or text[0] in " \t":
            raise _BadRequest("malformed header")
        name = name.lower()
        if name in headers:
            if name in _UNIQUE_HEADERS:
                raise _BadRequest(f"repeated {name} header")
            headers[name] += ", " + value.strip()
        else:
            headers[name] = value.strip()
    else:
        raise _BadRequest("too many headers")
    if "transfer-encoding" in headers:
        if headers["transfer-encoding"].lower() != "chunked" or "content-length" in headers:
            raise _BadRequest("unsupported message framing")
    elif not re.fullmatch(r"[0-9]{1,12}", headers.get("content-length", "0")):
        raise _BadRequest("bad content-length")
    return method, target, headers


def _declared_length(headers: dict[str, str]) -> int:
    if "transfer-encoding" in headers:
        return MAX_BODY + 1  # unknown in advance
    return int(headers.get("content-length") or 0)


async def _read_body(reader: asyncio.StreamReader, headers: dict[str, str], limit: int) -> bytes:
    if "transfer-encoding" not in headers:
        length = int(headers.get("content-length") or 0)
        if length > limit:
            raise _BadRequest("body too large")
        return await reader.readexactly(length) if length else b""
    chunks: list[bytes] = []
    total = 0
    while True:
        size_text = (await _line(reader)).split(b";")[0].strip()
        if not re.fullmatch(rb"[0-9a-fA-F]{1,8}", size_text):
            raise _BadRequest("bad chunk size")
        size = int(size_text, 16)
        if size == 0:
            break
        total += size
        if total > limit:
            raise _BadRequest("body too large")
        chunks.append(await reader.readexactly(size))
        if await _line(reader) not in (b"\r\n", b"\n"):
            raise _BadRequest("bad chunk ending")
    for _ in range(MAX_HEADERS + 1):  # trailer fields, up to the empty line
        if await _line(reader) in (b"\r\n", b"\n"):
            return b"".join(chunks)
    raise _BadRequest("too many trailer fields")


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
