"""Authenticated Streamable HTTP wire server for :class:`McpProxy`."""

from __future__ import annotations

import hmac
import os
from typing import Any, Callable, Mapping, Optional, Sequence
from urllib.parse import urlsplit

import anyio
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from starlette.responses import Response

from .core import CoreError
from .hosted_bridge import (
    DEFAULT_HOSTED_DEADLINE_SECONDS,
    HostedBridgeError,
    HostedToolRuntime,
)
from .sessions import SessionError


ALLOWED_HOSTS_ENVIRONMENT = "KAROX_MCP_ALLOWED_HOSTS"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
# `karox bridge connect` declares the tunnel host for the bridge it starts; a
# hand-run `karox bridge serve` behind someone else's tunnel has to be told, and
# the 421 body is the only place an operator will look.
HOST_REJECTION_HINT = (
    "invalid Host header; declare the public host in "
    f"{ALLOWED_HOSTS_ENVIRONMENT}"
)

# The wire answers a third-party agent, so an error may only carry a code from
# this table. Raw exception text on this path has already leaked absolute paths
# -- and with them the user's home directory and OS account name -- to whoever
# holds the bearer token.
BRIDGE_ERROR_MESSAGES: dict[str, str] = {
    "tool_not_exposed": "the tool is not exposed by this bridge",
    "idempotency_key_required": (
        "mutating calls require a stable _meta.karoxIdempotencyKey"
    ),
    "idempotency_key_invalid": (
        "_meta.karoxIdempotencyKey must be a string of 1-256 characters"
    ),
    "denied": "the call was denied by the KaroX session policy",
    "not_found": "the requested repository path does not exist",
    "invalid_request": "the call was rejected as invalid",
    "internal": "the tool failed",
}


def normalize_host(value: Optional[str]) -> str:
    """Return a lowercase hostname without port, or ``""`` when unusable."""
    raw = (value or "").strip().lower().rstrip(".")
    if not raw or any(
        char in raw for char in ("/", "\\", "@", "\x00", " ", "\t", "\r", "\n")
    ):
        return ""
    if raw.startswith("["):
        closing = raw.find("]")
        if closing < 0:
            return ""
        host = raw[1:closing]
        remainder = raw[closing + 1 :]
        if remainder and not (remainder.startswith(":") and remainder[1:].isdigit()):
            return ""
        return host
    if raw.count(":") == 1:
        host, port = raw.rsplit(":", 1)
        if port.isdigit():
            return host
    return raw


def resolve_allowed_hosts(extra: Optional[Sequence[str]] = None) -> frozenset[str]:
    """Return the host names this bridge is willing to answer to.

    A public tunnel host name is not derivable from inside the listener, so it
    has to be declared -- by the launcher that created the tunnel or by the
    operator through ``KAROX_MCP_ALLOWED_HOSTS``. Everything else arriving at a
    loopback listener that is published over a tunnel is a name someone else
    pointed at this machine, which is exactly the DNS rebinding case.
    """
    hosts = set(_LOOPBACK_HOSTS)
    declared = list(extra or ())
    declared.extend(os.environ.get(ALLOWED_HOSTS_ENVIRONMENT, "").split(","))
    for item in declared:
        host = normalize_host(item)
        if host:
            hosts.add(host)
    return frozenset(hosts)


def origin_is_allowed(origin: Optional[str], allowed: frozenset[str]) -> bool:
    """Validate a browser ``Origin`` against the same host allowlist.

    A missing Origin is accepted because non-browser MCP clients never send one;
    a browser driving a rebound name always does, which is what this rejects.
    """
    if origin is None:
        return True
    parts = urlsplit(origin.strip())
    if parts.scheme not in {"http", "https"}:
        return False
    if parts.path or parts.query or parts.fragment:
        return False
    return normalize_host(parts.netloc) in allowed


def scope_headers(scope: Mapping[str, Any]) -> dict[str, str]:
    """Decode request headers without consuming the ASGI receive channel."""
    headers: dict[str, str] = {}
    for raw_name, raw_value in scope.get("headers", ()):
        name = raw_name.decode("latin-1").lower()
        value = raw_value.decode("latin-1").strip()
        headers[name] = f"{headers[name]}, {value}" if name in headers else value
    return headers


def transport_security_settings(
    allowed: frozenset[str],
) -> TransportSecuritySettings:
    """Express the same allowlist in the SDK's exact-match vocabulary."""
    hosts: list[str] = []
    origins: list[str] = []
    for host in sorted(allowed):
        literal = f"[{host}]" if ":" in host else host
        hosts.extend((literal, f"{literal}:*"))
        for scheme in ("http", "https"):
            origins.extend((f"{scheme}://{literal}", f"{scheme}://{literal}:*"))
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


def bridge_error_result(code: str) -> CallToolResult:
    """Build the only tool-error shape this wire is allowed to send."""
    message = BRIDGE_ERROR_MESSAGES[code]
    return CallToolResult(
        content=[TextContent(type="text", text=f"{code}: {message}")],
        structuredContent={"ok": False, "error_code": code, "error": message},
        isError=True,
    )


def _error_code(exc: BaseException) -> str:
    if isinstance(exc, FileNotFoundError):
        return "not_found"
    if isinstance(exc, (PermissionError, SessionError)):
        return "denied"
    if isinstance(exc, (CoreError, HostedBridgeError, TypeError, ValueError)):
        return "invalid_request"
    return "internal"


def build_proxy_asgi_app(
    proxy: HostedToolRuntime,
    bearer_token: str | Callable[[], str] | None = None,
    *,
    path: str = "/mcp",
    # This deadline also caps how long checks.run may execute. Thirty seconds is
    # shorter than the test suite of any real repository, so the default used to
    # turn every honest verification attempt into a timeout.
    deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
    bearer_authorizer: Optional[Callable[[str], bool]] = None,
    unauthorized_headers: Optional[Mapping[str, str]] = None,
    allowed_hosts: Optional[Sequence[str]] = None,
) -> Any:
    """Expose selected Core and/or proxied tools as authenticated MCP."""
    if (bearer_token is None) == (bearer_authorizer is None):
        raise ValueError(
            "bridge requires exactly one bearer token resolver or authorizer"
        )
    if bearer_token is not None and not isinstance(bearer_token, str) and not callable(
        bearer_token
    ):
        raise ValueError("bridge bearer token must be a string or resolver")
    if bearer_authorizer is not None and not callable(bearer_authorizer):
        raise ValueError("bridge bearer authorizer must be callable")

    def resolve_token() -> str:
        if bearer_token is None:
            raise ValueError("bridge static bearer token is not configured")
        value = bearer_token() if callable(bearer_token) else bearer_token
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 65_536
            or any(char in value for char in ("\x00", "\r", "\n"))
        ):
            raise ValueError("bridge bearer token is invalid")
        return value

    if bearer_token is not None:
        resolve_token()
    if not isinstance(path, str) or not path.startswith("/") or "?" in path:
        raise ValueError("bridge MCP path must be an absolute URL path")
    if not 0.1 <= float(deadline_seconds) <= 3600.0:
        raise ValueError("bridge deadline must be between 0.1 and 3600 seconds")

    allowed = resolve_allowed_hosts(allowed_hosts)
    server = Server("karox-proxy")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        descriptors = await anyio.to_thread.run_sync(proxy.descriptors)
        return [
            Tool(
                name=item.name,
                description=item.description,
                inputSchema=item.input_schema,
                annotations=ToolAnnotations(
                    readOnlyHint=item.read_only,
                    destructiveHint=not item.read_only,
                    idempotentHint=item.read_only,
                    openWorldHint=False,
                ),
            )
            for item in descriptors
        ]

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict[str, object]
    ) -> dict[str, Any] | CallToolResult:
        try:
            descriptors = await anyio.to_thread.run_sync(proxy.descriptors)
            descriptor = next(
                (item for item in descriptors if item.name == name), None
            )
            if descriptor is None:
                return bridge_error_result("tool_not_exposed")
            idempotency_key = None
            if not descriptor.read_only:
                meta = server.request_context.meta
                extra = getattr(meta, "model_extra", None) if meta is not None else None
                supplied = (
                    extra.get("karoxIdempotencyKey") if isinstance(extra, dict) else None
                )
                # A generated per-attempt key would make every retry of the same
                # mutation a second commit, so a client that cannot supply a
                # stable key gets no mutating tools rather than silent duplicates.
                if supplied is None:
                    return bridge_error_result("idempotency_key_required")
                if (
                    not isinstance(supplied, str)
                    or not supplied
                    or len(supplied) > 256
                ):
                    return bridge_error_result("idempotency_key_invalid")
                idempotency_key = supplied
            return await anyio.to_thread.run_sync(
                lambda: proxy.execute(
                    name,
                    dict(arguments),
                    idempotency_key=idempotency_key,
                    deadline_seconds=deadline_seconds,
                )
            )
        except Exception as exc:
            return bridge_error_result(_error_code(exc))

    # The proxy keeps durable state in SessionStore, not in transport sessions.
    # Stateless JSON responses avoid long-lived SSE streams and their upstream
    # AnyIO receive-stream cleanup problems on Windows.
    manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=True,
        security_settings=transport_security_settings(allowed),
    )

    def rebinding_response(scope: dict[str, Any]) -> Optional[Response]:
        headers = scope_headers(scope)
        if normalize_host(headers.get("host")) not in allowed:
            return Response(HOST_REJECTION_HINT, status_code=421)
        if not origin_is_allowed(headers.get("origin"), allowed):
            return Response("invalid Origin header", status_code=421)
        return None

    async def authorized(scope: dict[str, Any]) -> bool:
        values = [
            value
            for name, value in scope.get("headers", ())
            if name.lower() == b"authorization"
        ]
        if len(values) != 1:
            return False
        try:
            scheme, supplied = values[0].decode("utf-8").split(" ", 1)
        except (UnicodeDecodeError, ValueError):
            return False
        if scheme.lower() != "bearer" or not supplied:
            return False
        if bearer_authorizer is not None:
            try:
                return bool(
                    await anyio.to_thread.run_sync(bearer_authorizer, supplied)
                )
            except Exception:
                return False
        try:
            expected = await anyio.to_thread.run_sync(resolve_token)
        except Exception:
            return False
        # compare_digest rejects a non-ASCII str with TypeError, which turned a
        # bad credential into a 500 instead of a 401.
        return hmac.compare_digest(
            supplied.encode("utf-8"), expected.encode("utf-8")
        )

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        scope_type = scope.get("type")
        if scope_type == "lifespan":
            async with manager.run():
                while True:
                    message = await receive()
                    if message["type"] == "lifespan.startup":
                        await send({"type": "lifespan.startup.complete"})
                    elif message["type"] == "lifespan.shutdown":
                        await send({"type": "lifespan.shutdown.complete"})
                        return
            return
        if scope_type != "http":
            await Response("not found", status_code=404)(scope, receive, send)
            return
        rejection = rebinding_response(scope)
        if rejection is not None:
            await rejection(scope, receive, send)
            return
        request_path = scope.get("path")
        if request_path == f"{path}/":
            # Serve /mcp/ in place rather than redirecting to /mcp: several MCP
            # clients do not resend the Authorization header across a redirect,
            # which turns the trailing slash into an unexplained 401.
            scope = dict(scope)
            scope["path"] = path
            scope["raw_path"] = path.encode("utf-8")
        elif request_path != path:
            await Response("not found", status_code=404)(scope, receive, send)
            return
        if not await authorized(scope):
            await Response(
                "unauthorized",
                status_code=401,
                headers=dict(unauthorized_headers or {}),
            )(scope, receive, send)
            return
        await manager.handle_request(scope, receive, send)

    return app
