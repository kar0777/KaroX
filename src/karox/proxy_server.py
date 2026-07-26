"""Authenticated Streamable HTTP wire server for :class:`McpProxy`."""

from __future__ import annotations

import hmac
import uuid
from typing import Any, Callable, Mapping, Optional

import anyio
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import Tool, ToolAnnotations
from starlette.responses import Response

from .hosted_bridge import DEFAULT_HOSTED_DEADLINE_SECONDS, HostedToolRuntime


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
    async def call_tool(name: str, arguments: dict[str, object]) -> dict[str, Any]:
        descriptors = await anyio.to_thread.run_sync(proxy.descriptors)
        descriptor = next((item for item in descriptors if item.name == name), None)
        if descriptor is None:
            raise PermissionError(f"MCP tool is not exposed: {name}")
        idempotency_key = None
        if not descriptor.read_only:
            meta = server.request_context.meta
            extra = getattr(meta, "model_extra", None) if meta is not None else None
            supplied = (
                extra.get("karoxIdempotencyKey") if isinstance(extra, dict) else None
            )
            if supplied is None:
                # Standards-compliant MCP clients cannot always set _meta, so a
                # missing key must not make every mutating tool unusable. A
                # unique generated key still guarantees that an accepted call
                # executes exactly once through Core. Cross-request replay
                # protection remains available only to clients that supply a
                # stable key themselves.
                idempotency_key = f"auto-{uuid.uuid4().hex}"
            elif (
                not isinstance(supplied, str)
                or not supplied
                or len(supplied) > 256
            ):
                raise PermissionError(
                    "_meta.karoxIdempotencyKey must be a string of 1-256 characters"
                )
            else:
                idempotency_key = supplied
        return await anyio.to_thread.run_sync(
            lambda: proxy.execute(
                name,
                dict(arguments),
                idempotency_key=idempotency_key,
                deadline_seconds=deadline_seconds,
            )
        )

    # The proxy keeps durable state in SessionStore, not in transport sessions.
    # Stateless JSON responses avoid long-lived SSE streams and their upstream
    # AnyIO receive-stream cleanup problems on Windows.
    manager = StreamableHTTPSessionManager(
        app=server, json_response=True, stateless=True
    )
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
        return hmac.compare_digest(supplied, expected)

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
        if scope_type != "http" or scope.get("path") != path:
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
