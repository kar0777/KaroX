"""Authenticated Streamable HTTP wire server for :class:`McpProxy`."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sys
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
from .process_launcher import is_executable_resolution_error
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
    # Still reachable on the OpenAPI wire, where the key travels in a header the
    # client controls. The MCP wire derives one instead, because its web clients
    # cannot send `_meta` at all.
    "idempotency_key_required": "mutating calls require a stable idempotency key",
    "idempotency_key_invalid": (
        "the supplied idempotency key must be a string of 1-256 characters"
    ),
    "denied": "the call was denied by the KaroX session policy",
    "not_found": "the requested repository path does not exist",
    "executable_not_found": (
        "the guarded command's executable could not be resolved "
        "(install the missing tool or use a command in the allowlist)"
    ),
    "invalid_request": "the call was rejected as invalid",
    "internal": "the tool failed",
}


def derive_idempotency_key(tool_name: str, arguments: Mapping[str, object]) -> str:
    """Content-address a mutation whose caller cannot name its own retries.

    ChatGPT and Claude speak MCP without ``_meta``, so neither can carry a key of
    its own. Refusing those calls left ``--write`` advertising two tools that
    could never run: the connector listed ``repo.write_file``, the model called
    it, and every call came back as ``idempotency_key_required``.

    Hashing the call gives the property the key exists for. A retry after a lost
    response repeats the same tool and the same arguments, so it lands on the
    same key and returns the first outcome instead of writing twice -- which a
    per-attempt key would not have done.

    The cost is that a deliberate repeat of a byte-identical mutation is also
    treated as a retry. Both write tools are declarative, so that is close to
    free: ``repo.write_file`` states the whole content, and ``repo.edit_file``
    states an exact match plus the number of occurrences it expects, so a real
    repeat is a no-op or a loud mismatch either way. The remaining case -- the
    file changed underneath and the same content is written again -- is why the
    result carries ``idempotent_replay``, so a replay is never reported to the
    model as a write that just landed.
    """
    payload = json.dumps(
        {"tool": tool_name, "arguments": arguments},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"derived-{digest}"


def _idna_normalize(host: str) -> str:
    """Return the punycode form of an internationalized host, or ``""``.

    Tailscale MagicDNS names are ASCII, but a user can publish the bridge behind
    an IDN origin, and the browser/TLS layer speaks punycode (``xn--...``) while
    the operator may paste the unicode form into ``--public-url``. Comparing the
    two as raw strings makes a perfectly good host look foreign and earns a 421.
    """
    if not host or host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return ""


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
        return _idna_normalize(host)
    if raw.count(":") == 1:
        host, port = raw.rsplit(":", 1)
        if port.isdigit():
            return _idna_normalize(host)
    return _idna_normalize(raw)


def forwarded_host(headers: Mapping[str, str]) -> str:
    """Extract the original host a trusted proxy forwarded, or ``""``.

    Only the *first* hop's value is taken: a chain like ``a, b`` is read left to
    right and the leftmost is the closest proxy, which is the one this bridge
    configured. Untrusted later hops are ignored so an external client cannot
    prepend its own host.
    """
    forwarded = headers.get("forwarded")
    if forwarded:
        for hop in forwarded.split(","):
            for pair in hop.split(";"):
                key, _, val = pair.strip().partition("=")
                if key.lower() == "host" and val:
                    return normalize_host(val.strip().strip('"'))
    xfh = headers.get("x-forwarded-host")
    if xfh:
        return normalize_host(xfh.split(",")[0].strip())
    return ""


def peer_is_loopback(scope: Mapping[str, Any]) -> bool:
    """True when the request reached the listener from this machine.

    The tunnel child connects to ``127.0.0.1:<port>``, so the only loopback peer
    in production is the tunnel itself. That is the trusted local proxy path
    permitted to set ``Forwarded``/``X-Forwarded-Host``; a remote client is not.
    """
    client = scope.get("client")
    if not isinstance(client, (list, tuple)) or len(client) < 1:
        return False
    return str(client[0]) in {"127.0.0.1", "::1", "localhost"}


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


def bridge_error_code(exc: BaseException) -> str:
    """Classify a handler failure into one of the codes callers may be told."""
    # A missing executable (npm.cmd not on PATH) raises ExecutableResolutionError,
    # a FileNotFoundError subclass.  It must NOT be collapsed with a genuine
    # missing-repository FileNotFoundError into ``not_found`` ("the requested
    # repository path does not exist"), which previously misdiagnosed a WinError 2
    # as a repo-path problem even though the repo was readable.  Check it first.
    if is_executable_resolution_error(exc):
        return "executable_not_found"
    if isinstance(exc, FileNotFoundError):
        return "not_found"
    if isinstance(exc, (PermissionError, SessionError)):
        return "denied"
    if isinstance(exc, (CoreError, HostedBridgeError, TypeError, ValueError)):
        return "invalid_request"
    return "internal"


def _log_rebinding_421(
    scope: Mapping[str, Any],
    headers: Mapping[str, str],
    *,
    rejected_host: str,
    reason: str,
    allowed: frozenset[str],
    expected_host: Optional[str] = None,
) -> None:
    """Print one redacted line explaining a 421, to the launcher console.

    The 421 used to be a bare ``invalid Host header`` on an open endpoint, so the
    only way to learn which header a real client actually sent was to reproduce it
    with a packet capture. This prints what the guard saw -- nothing it must not.

    Nothing sensitive is logged: the Authorization header, Cookie, any token, the
    approval password, and the bridge credential are all absent from ``scope``'s
    path. ``scope["path"]`` carries no query string in ASGI (``query_string`` is
    separate), and even that is not printed, so a ``code``/``token`` leaking into
    a redirect's query is not a concern here.
    """
    method = scope.get("method", "GET").upper()
    path = scope.get("path", "")
    request_id = secrets.token_hex(8)
    forwarded = headers.get("forwarded") or headers.get("x-forwarded-host")
    expected = expected_host or ",".join(sorted(allowed))
    print(
        f"[karox-rebind] 421 {method} {path} request_id={request_id} "
        f"host={rejected_host or '<none>'} "
        f"x-forwarded-host={'yes' if forwarded else 'no'} "
        f"x-forwarded-proto={'yes' if headers.get('x-forwarded-proto') else 'no'} "
        f"forwarded={'yes' if headers.get('forwarded') else 'no'} "
        f"expected_origin={expected} reason={reason}",
        file=sys.stderr,
        flush=True,
    )


def rebinding_rejection(
    scope: Mapping[str, Any], allowed: frozenset[str]
) -> Optional[Response]:
    """Reject a request whose Host or Origin was not declared for this bridge.

    Every wire published through the same tunnel shares this guard: a name
    someone else pointed at a loopback listener is the DNS rebinding case no
    matter which path it lands on.
    """
    headers = scope_headers(scope)
    host = normalize_host(headers.get("host"))
    if host not in allowed:
        # A trusted local tunnel/proxy connects from loopback and may rewrite the
        # Host to its loopback target while preserving the public name it received
        # in Forwarded/X-Forwarded-Host. An external client is not a loopback peer,
        # so it cannot use this path to smuggle in a host of its own choosing.
        if peer_is_loopback(scope):
            forwarded = forwarded_host(headers)
            if forwarded and forwarded in allowed:
                host = forwarded
        if host not in allowed:
            _log_rebinding_421(
                scope,
                headers,
                rejected_host=normalize_host(headers.get("host")),
                reason="host_not_allowed",
                allowed=allowed,
            )
            return Response(HOST_REJECTION_HINT, status_code=421)
    if not origin_is_allowed(headers.get("origin"), allowed):
        _log_rebinding_421(
            scope,
            headers,
            rejected_host=host,
            reason="origin_not_allowed",
            allowed=allowed,
        )
        return Response("invalid Origin header", status_code=421)
    return None


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
    diagnostics: Optional[Mapping[str, Any]] = None,
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
    diagnostics_payload: Optional[dict[str, Any]] = None
    diagnostics_source: Any = diagnostics
    if diagnostics_source is None:
        raw_diagnostics = os.environ.get("KAROX_BRIDGE_DIAGNOSTICS_JSON", "").strip()
        if raw_diagnostics:
            try:
                diagnostics_source = json.loads(raw_diagnostics)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    "KAROX_BRIDGE_DIAGNOSTICS_JSON must contain valid JSON"
                ) from exc
    if diagnostics_source is not None:
        try:
            diagnostics_payload = json.loads(
                json.dumps(dict(diagnostics_source), ensure_ascii=False, sort_keys=True)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("bridge diagnostics must be JSON serializable") from exc
        if not isinstance(diagnostics_payload, dict):
            raise ValueError("bridge diagnostics must be a JSON object")

    allowed = resolve_allowed_hosts(allowed_hosts)
    server = Server("karox-proxy")

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        descriptors = await anyio.to_thread.run_sync(proxy.descriptors)
        tools = [
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
        if diagnostics_payload is not None:
            tools.append(
                Tool(
                    name="karox.bridge.diagnostics",
                    description=(
                        "Return the effective KaroX bridge contract: available and "
                        "disabled tools with reasons, verification allowlist, "
                        "deadline, tunnel stability, and session lifetime."
                    ),
                    inputSchema={"type": "object", "properties": {}, "additionalProperties": False},
                    annotations=ToolAnnotations(
                        readOnlyHint=True,
                        destructiveHint=False,
                        idempotentHint=True,
                        openWorldHint=False,
                    ),
                )
            )
        return tools

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict[str, object]
    ) -> dict[str, Any] | CallToolResult:
        try:
            if name == "karox.bridge.diagnostics" and diagnostics_payload is not None:
                if arguments:
                    return bridge_error_result("invalid_request")
                return CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=json.dumps(
                                diagnostics_payload,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        )
                    ],
                    structuredContent=dict(diagnostics_payload),
                    isError=False,
                )
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
                if supplied is None:
                    # A client that knows its own retries is trusted to say so; one
                    # that cannot send _meta at all gets a key derived from the
                    # call, which is stable across exactly those retries.
                    idempotency_key = derive_idempotency_key(name, arguments)
                elif (
                    not isinstance(supplied, str)
                    or not supplied
                    or len(supplied) > 256
                ):
                    return bridge_error_result("idempotency_key_invalid")
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
        except Exception as exc:
            return bridge_error_result(bridge_error_code(exc))

    # The proxy keeps durable state in SessionStore, not in transport sessions.
    # Stateless JSON responses avoid long-lived SSE streams and their upstream
    # AnyIO receive-stream cleanup problems on Windows.
    manager = StreamableHTTPSessionManager(
        app=server,
        json_response=True,
        stateless=True,
        security_settings=transport_security_settings(allowed),
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
        rejection = rebinding_rejection(scope, allowed)
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
