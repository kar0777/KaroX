"""Authenticated Streamable HTTP wire server for :class:`McpProxy`."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
from typing import Any, Callable, Iterable, Mapping, MutableMapping, Optional, Sequence
from urllib.parse import urlsplit

import anyio
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from starlette.responses import JSONResponse, Response

from .artifacts import ArtifactStore
from .core import CoreError
from .hosted_bridge import (
    DEFAULT_HOSTED_DEADLINE_SECONDS,
    HostedBridgeError,
    HostedToolRuntime,
)
from .process_launcher import is_executable_resolution_error
from .result_envelope import artifact_backed_result
from .sessions import SessionError
from .tool_telemetry import (
    ToolTraceContext,
    ToolTraceSpan,
    default_trace_store,
)


ALLOWED_HOSTS_ENVIRONMENT = "KAROX_MCP_ALLOWED_HOSTS"
MODERN_MCP_PROTOCOL_VERSION = "2026-07-28"
LEGACY_MCP_PROTOCOL_VERSIONS = (
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
# `karox bridge connect` declares the tunnel host for the bridge it starts; a
# hand-run `karox bridge serve` behind someone else's tunnel has to be told, and
# the 421 body is the only place an operator will look.
HOST_REJECTION_HINT = (
    "invalid Host header; declare the public host in "
    f"{ALLOWED_HOSTS_ENVIRONMENT}"
)

# Process-local, content-free HTTP activity used only to defer a supervised
# MCP-child restart until the response that requested it has left the wire.
# This child serves one hosted bridge, so process scope is the correct boundary.
_TRANSPORT_ACTIVITY_LOCK = threading.Lock()
_TRANSPORT_ACTIVE_REQUESTS = 0
_TRANSPORT_RESPONSES_COMPLETED = 0


def _transport_activity_started() -> None:
    global _TRANSPORT_ACTIVE_REQUESTS
    with _TRANSPORT_ACTIVITY_LOCK:
        _TRANSPORT_ACTIVE_REQUESTS += 1


def _transport_activity_finished(*, completed: bool) -> None:
    global _TRANSPORT_ACTIVE_REQUESTS, _TRANSPORT_RESPONSES_COMPLETED
    with _TRANSPORT_ACTIVITY_LOCK:
        _TRANSPORT_ACTIVE_REQUESTS = max(0, _TRANSPORT_ACTIVE_REQUESTS - 1)
        if completed:
            _TRANSPORT_RESPONSES_COMPLETED += 1


def transport_activity_snapshot() -> dict[str, int]:
    """Return secret-free process-level transport liveness counters."""
    with _TRANSPORT_ACTIVITY_LOCK:
        return {
            "active_requests": _TRANSPORT_ACTIVE_REQUESTS,
            "responses_completed": _TRANSPORT_RESPONSES_COMPLETED,
        }


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
    "request_interrupted": "the call was interrupted before it finished",
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


# MCP itself puts no constraint on a tool's name, but the model APIs behind the
# clients that consume a remote MCP server do: an Anthropic tool definition must
# match ``^[a-zA-Z0-9_-]{1,64}$``, and OpenAI's function names are the same
# shape.  A name containing a dot therefore cannot be handed to the model at all.
#
# KaroX names its tools ``karox.repo.read_file``, and that spelling is an
# identifier throughout the codebase -- session allowlists, launch profiles,
# stored policy, and the hosted-bridge catalogue all key on it.  A client that
# turns our ``tools/list`` into model tools drops every one of them, and the
# symptom is the worst kind: the handshake succeeds, the connector shows as
# connected, and the model reports that it can see no tools.
#
# The dot is a wire-encoding problem, not an identity problem, so it is fixed at
# the wire and nowhere else.  Outbound names swap dots for underscores; inbound
# calls accept either spelling, so a client holding the old name keeps working
# and the internal identifier never changes.
_DIAGNOSTICS_TOOL_NAME = "karox.bridge.diagnostics"


def wire_tool_name(internal_name: str) -> str:
    """Return the ``tools/list`` spelling of an internal tool name."""
    return internal_name.replace(".", "_")


# ``tools/list`` advertises wire spellings only, but ``tools/call`` accepts the
# dotted internal spelling as a compatibility alias (see module comment above).
# The MCP SDK does not know about the alias, so its lowlevel server logs
# ``Tool 'karox.repo.read_file' not listed, no validation will be performed``
# for every dotted call on a perfectly normal flow.  That warning is precise
# noise: the call is routed, validated, and policy-checked by ``proxy.execute``.
#
# The filter below drops exactly that record and only for names this process
# currently routes as aliases.  A genuinely unknown tool name still warns, and
# nothing else about the ``mcp`` loggers is touched.
_ROUTABLE_ALIAS_NAMES: set[str] = {_DIAGNOSTICS_TOOL_NAME}
_NOT_LISTED_MESSAGE = re.compile(r"Tool '(?P<name>[^']+)' not listed")


class _AliasCatalogLogFilter(logging.Filter):
    """Suppress the SDK catalog warning for advertised-alias calls only."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        match = _NOT_LISTED_MESSAGE.search(message)
        if match is None:
            return True
        return match.group("name") not in _ROUTABLE_ALIAS_NAMES


def _install_alias_catalog_log_filter() -> None:
    target = logging.getLogger("mcp.server.lowlevel.server")
    if not any(isinstance(item, _AliasCatalogLogFilter) for item in target.filters):
        target.addFilter(_AliasCatalogLogFilter())


_install_alias_catalog_log_filter()


def resolve_wire_tool_name(
    supplied: str, internal_names: Iterable[str]
) -> Optional[str]:
    """Return the internal tool name a ``tools/call`` name refers to.

    An exact internal name wins, so a caller that speaks the dotted spelling is
    unaffected.  Failing that, the supplied name is matched against the wire
    spelling of each candidate.  Returns ``None`` when nothing matches, which the
    caller reports as ``tool_not_exposed``.
    """
    candidates = list(internal_names)
    if supplied in candidates:
        return supplied
    for name in candidates:
        if wire_tool_name(name) == supplied:
            return name
    return None


def stale_catalog_hint(
    supplied: str,
    advertised_wire_names: Sequence[str],
    cached_wire_names: Optional[Sequence[str]] = None,
) -> str:
    """One short, actionable line for a client whose cached catalog is stale.

    This proxy answers stateless JSON (no long-lived stream), so the MCP
    ``notifications/tools/list_changed`` mechanism cannot reach a connected
    client and ``capabilities.tools.listChanged`` is honestly ``false``. A
    client that cached an old ``tools/list`` therefore keeps calling names
    outside the current catalog until it reconnects. When that happens, name
    exactly one fix: reconnect this client. Never propose a new physical
    bridge, a credential rotation, or a URL change; none of them refresh a
    client-side catalog cache and all of them are destructive.

    ``cached_wire_names`` is optional because this stateless wire usually does
    not know what a given client cached; a caller that does know (a session
    store, a test, a future stateful transport) gets the precise count of
    newly available tools.
    """
    advertised = set(advertised_wire_names)
    if cached_wire_names is not None:
        added = sorted(advertised - set(cached_wire_names))
        if added:
            noun = "tool is" if len(added) == 1 else "tools are"
            return (
                f"{len(added)} new {noun} available; reconnect this client to "
                "refresh its tool catalog (same bridge, same credential, "
                "same URL)"
            )
    return (
        f"the current catalog advertises {len(advertised)} tools; if this "
        "client connected before the last catalog upgrade its cached tool "
        "list is stale, and reconnecting this client refreshes it "
        "(same bridge, same credential, same URL)"
    )


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


def bridge_error_result(code: str, *, detail: Optional[str] = None) -> CallToolResult:
    """Build the only tool-error shape this wire is allowed to send.

    ``detail`` must be server-generated, secret-free text (never raw exception
    content); it extends the fixed table message with an actionable hint such
    as the stale-catalog reconnect guidance.
    """
    # ``get`` with the internal fallback: an error handler that itself raises
    # KeyError replaces the intended tool error with a 500, which is how a
    # benign interrupt once escaped as a crash.
    message = BRIDGE_ERROR_MESSAGES.get(code, BRIDGE_ERROR_MESSAGES["internal"])
    if detail:
        message = f"{message}; {detail}"
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


def _maybe_record_client_evidence(tool_name: str, *, success: bool) -> None:
    """Record client-verification evidence when an external client calls runtime.status.

    This hook fires inside the bridge serve child's ``call_tool`` handler, so
    it is triggered ONLY by a real MCP tool call from an external client
    (e.g. ChatGPT). Local self-tests via :func:`_verify_bridge_endpoint` do
    ``initialize`` + ``tools/list`` only and never call ``call_tool``, so this
    hook is never triggered by them.

    Evidence fields are safe only: timestamp, tool name, success/failure.
    Never stores: OAuth token, Authorization header, tool arguments, file
    contents, user messages.
    """
    # Only the canonical verification tool counts toward fully_verified.
    if tool_name != "runtime.status":
        return
    raw_diagnostics = os.environ.get("KAROX_BRIDGE_DIAGNOSTICS_JSON", "").strip()
    if not raw_diagnostics:
        return
    try:
        diagnostics = json.loads(raw_diagnostics)
    except (ValueError, TypeError):
        return
    saved_profile = diagnostics.get("saved_profile")
    if not isinstance(saved_profile, str) or not saved_profile:
        return
    import time
    try:
        from .connection_status import write_client_evidence
        write_client_evidence(saved_profile, {
            "last_tool_call_at": time.time(),
            "last_tool_call_name": "karox.runtime.status",
            "success": success,
            "client_kind": "mcp_external",
        })
    except Exception:
        # Evidence recording is best-effort: a failure here must never break
        # the tool call itself.
        pass


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
    inline_result_bytes: Optional[int] = None,
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

    transport_runtime: dict[str, Any] = {
        "requests_started": 0,
        "responses_started": 0,
        "responses_completed": 0,
        "client_disconnects": 0,
        "authorized_requests": 0,
        "unauthorized_requests": 0,
        "mcp_tool_calls_started": 0,
        "mcp_tool_calls_completed": 0,
        "last_mcp_tool_call_started_at": None,
        "last_mcp_tool_call_completed_at": None,
        "last_http_version": None,
        "last_method": None,
        "last_status": None,
        "last_request_started_at": None,
        "last_response_completed_at": None,
        "last_request_duration_ms": None,
        "max_request_duration_ms": 0.0,
        "last_response_body_bytes": None,
        "max_response_body_bytes": 0,
    }

    def current_diagnostics() -> Optional[dict[str, Any]]:
        if diagnostics_payload is None:
            return None
        live = dict(diagnostics_payload)
        live["transport_runtime"] = dict(transport_runtime)

        # Saved bridges have an external credential-free supervisor. Diagnostics
        # must read this state live instead of freezing it into the child process
        # environment at startup, otherwise a hosted client cannot tell whether
        # self-heal is actually active after an owner restart.
        saved_profile = live.get("saved_profile")
        if isinstance(saved_profile, str) and saved_profile:
            try:
                from .saved_bridge_supervisor import saved_bridge_supervisor_status

                supervisor = saved_bridge_supervisor_status(saved_profile)
                live["durable_supervisor"] = {
                    "desired_running": bool(supervisor.get("desired_running")),
                    "supervisor_alive": bool(supervisor.get("supervisor_alive")),
                    "supervisor_process_alive": bool(
                        supervisor.get("supervisor_process_alive")
                    ),
                    "supervisor_heartbeat_fresh": bool(
                        supervisor.get("supervisor_heartbeat_fresh")
                    ),
                    "supervisor_heartbeat_age_seconds": supervisor.get(
                        "supervisor_heartbeat_age_seconds"
                    ),
                    "supervisor_main_progress_fresh": bool(
                        supervisor.get("supervisor_main_progress_fresh")
                    ),
                    "supervisor_main_progress_age_seconds": supervisor.get(
                        "supervisor_main_progress_age_seconds"
                    ),
                    "supervisor_pid": supervisor.get("supervisor_pid"),
                    "heartbeat_at": supervisor.get("heartbeat_at"),
                    "last_owner_pid": supervisor.get("last_owner_pid"),
                    "restart_count": int(supervisor.get("restart_count") or 0),
                    "last_restart_at": supervisor.get("last_restart_at"),
                    "last_error": supervisor.get("last_error"),
                }
            except Exception as exc:
                live["durable_supervisor"] = {
                    "desired_running": True,
                    "supervisor_alive": False,
                    "last_error": f"status_unavailable:{type(exc).__name__}",
                }
        return live

    trace_context = ToolTraceContext.from_runtime(proxy, diagnostics_payload)
    trace_store = default_trace_store(trace_context)
    try:
        result_artifact_store = (
            ArtifactStore(trace_context.session_id) if trace_context is not None else None
        )
    except Exception:
        result_artifact_store = None

    def compact_result(
        tool_name: str,
        result: dict[str, Any] | CallToolResult,
    ) -> dict[str, Any] | CallToolResult:
        try:
            return artifact_backed_result(
                result,
                tool_name=tool_name,
                store=result_artifact_store,
                threshold_bytes=inline_result_bytes,
            )
        except Exception:
            return result

    def repository_revision() -> Optional[int]:
        try:
            reader = getattr(proxy, "session_info", None)
            info = dict(reader()) if callable(reader) else {}
            value = info.get("revision")
            return int(value) if isinstance(value, int) and not isinstance(value, bool) else None
        except Exception:
            return None

    def start_trace(
        tool_name: str,
        arguments: Mapping[str, Any],
        *,
        read_only: bool,
        idempotency_key: Optional[str],
    ) -> Optional[ToolTraceSpan]:
        if trace_store is None or trace_context is None:
            return None
        try:
            return ToolTraceSpan(
                store=trace_store,
                context=trace_context,
                tool_name=tool_name,
                arguments=arguments,
                read_only=read_only,
                idempotency_key=idempotency_key,
                repository_revision_before=repository_revision(),
            )
        except Exception:
            return None

    def finish_trace(
        span: Optional[ToolTraceSpan],
        result: Any,
        *,
        forced_error_code: Optional[str] = None,
    ) -> None:
        transport_runtime["mcp_tool_calls_completed"] += 1
        transport_runtime["last_mcp_tool_call_completed_at"] = time.time()
        if span is None:
            return
        try:
            # A read-only KaroX call cannot advance the session repository
            # revision itself, so a second session-store read adds latency without
            # adding information. Mutating calls still sample the authoritative
            # revision again after execution.
            revision_after = (
                span.repository_revision_before if span.read_only else repository_revision()
            )
            span.finish(
                result,
                repository_revision_after=revision_after,
                forced_error_code=forced_error_code,
            )
        except Exception:
            # Telemetry must never change the command result.
            pass

    allowed = resolve_allowed_hosts(allowed_hosts)
    server = Server("karox-proxy")
    # Tool names, schemas, and ownership are immutable for the lifetime of a
    # bridge process. Keep a compact routing snapshot for tools/call so a hosted
    # client does not force every runtime to rebuild descriptors merely to map
    # the wire spelling back to the same internal name. tools/list still calls
    # proxy.descriptors() live (and refreshes this snapshot), while each concrete
    # runtime.execute() continues to enforce session revocation/policy on every
    # invocation.
    descriptor_routes: Optional[tuple[dict[str, Any], dict[str, Any]]] = None

    def update_descriptor_routes(descriptors: Sequence[Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        nonlocal descriptor_routes
        by_internal: dict[str, Any] = {}
        by_wire: dict[str, Any] = {}
        for item in descriptors:
            by_internal[item.name] = item
            by_wire.setdefault(wire_tool_name(item.name), item)
        # The dotted internal spellings are callable aliases of the advertised
        # wire names; record them so the SDK's 'not listed' warning is dropped
        # for exactly these names and no others.
        _ROUTABLE_ALIAS_NAMES.update(
            name for name in by_internal if wire_tool_name(name) != name
        )
        descriptor_routes = (by_internal, by_wire)
        return descriptor_routes

    async def exposed_tools() -> list[Tool]:
        descriptors = await anyio.to_thread.run_sync(proxy.descriptors)
        update_descriptor_routes(descriptors)
        tools = [
            Tool(
                name=wire_tool_name(item.name),
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
                    name=wire_tool_name(_DIAGNOSTICS_TOOL_NAME),
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

    @server.list_tools()
    async def list_tools() -> list[Tool]:
        return await exposed_tools()

    async def execute_tool(
        name: str,
        arguments: dict[str, object],
        *,
        meta_extra: Optional[Mapping[str, Any]] = None,
    ) -> dict[str, Any] | CallToolResult:
        transport_runtime["mcp_tool_calls_started"] += 1
        transport_runtime["last_mcp_tool_call_started_at"] = time.time()
        resolved_name = name
        descriptor: Any = None
        idempotency_key: Optional[str] = None
        span: Optional[ToolTraceSpan] = None
        result: dict[str, Any] | CallToolResult
        try:
            if (
                diagnostics_payload is not None
                and resolve_wire_tool_name(name, (_DIAGNOSTICS_TOOL_NAME,)) is not None
            ):
                resolved_name = _DIAGNOSTICS_TOOL_NAME
                span = start_trace(
                    resolved_name,
                    arguments,
                    read_only=True,
                    idempotency_key=None,
                )
                if arguments:
                    result = bridge_error_result("invalid_request")
                    finish_trace(span, result)
                    return result
                live_diagnostics = current_diagnostics() or {}
                result = CallToolResult(
                    content=[
                        TextContent(
                            type="text",
                            text=json.dumps(
                                live_diagnostics,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        )
                    ],
                    structuredContent=live_diagnostics,
                    isError=False,
                )
                finish_trace(span, result)
                return result
            routes = descriptor_routes
            if routes is None:
                descriptors = await anyio.to_thread.run_sync(proxy.descriptors)
                routes = update_descriptor_routes(descriptors)
            by_internal, by_wire = routes
            # Exact internal names still win over a normalized wire alias, which
            # preserves resolve_wire_tool_name() semantics if two unusual names
            # would otherwise normalize to the same spelling.
            descriptor = by_internal.get(name)
            if descriptor is None:
                descriptor = by_wire.get(name)
            if descriptor is None:
                span = start_trace(
                    resolved_name,
                    arguments,
                    read_only=True,
                    idempotency_key=None,
                )
                result = bridge_error_result(
                    "tool_not_exposed",
                    detail=stale_catalog_hint(name, tuple(by_wire.keys())),
                )
                finish_trace(span, result)
                return result
            resolved_name = descriptor.name
            if not descriptor.read_only:
                supplied = (
                    meta_extra.get("karoxIdempotencyKey")
                    if isinstance(meta_extra, Mapping)
                    else None
                )
                if supplied is None:
                    # A client that knows its own retries is trusted to say so; one
                    # that cannot send _meta at all gets a key derived from the
                    # call, which is stable across exactly those retries.
                    idempotency_key = derive_idempotency_key(resolved_name, arguments)
                elif (
                    not isinstance(supplied, str)
                    or not supplied
                    or len(supplied) > 256
                ):
                    span = start_trace(
                        resolved_name,
                        arguments,
                        read_only=False,
                        idempotency_key=None,
                    )
                    result = bridge_error_result("idempotency_key_invalid")
                    finish_trace(span, result)
                    return result
                else:
                    idempotency_key = supplied
            span = start_trace(
                resolved_name,
                arguments,
                read_only=bool(descriptor.read_only),
                idempotency_key=idempotency_key,
            )
            result = await anyio.to_thread.run_sync(
                lambda: proxy.execute(
                    resolved_name,
                    dict(arguments),
                    idempotency_key=idempotency_key,
                    deadline_seconds=deadline_seconds,
                )
            )
            result = compact_result(resolved_name, result)
            # Record client evidence when an external client (e.g. ChatGPT)
            # calls karox.runtime.status. This is the only tool call that
            # counts toward fully_verified: local self-tests do initialize +
            # tools/list only, never call_tool, so this hook is never
            # triggered by _verify_bridge_endpoint.
            _maybe_record_client_evidence(resolved_name, success=True)
            finish_trace(span, result)
            return result
        except KeyboardInterrupt:
            # A command/request interrupt belongs to this tool invocation. It is
            # never an operator instruction to stop uvicorn or the managed web
            # bridge. Legacy synchronous commands contain their child tree in
            # CoreRuntime; this is the final transport boundary.
            _maybe_record_client_evidence(resolved_name, success=False)
            result = bridge_error_result("request_interrupted")
            if span is None:
                span = start_trace(
                    resolved_name,
                    arguments,
                    read_only=bool(getattr(descriptor, "read_only", True)),
                    idempotency_key=idempotency_key,
                )
            finish_trace(span, result, forced_error_code="request_interrupted")
            return result
        except Exception as exc:
            # Record evidence for failed runtime.status calls too.
            _maybe_record_client_evidence(resolved_name, success=False)
            code = bridge_error_code(exc)
            result = bridge_error_result(code)
            if span is None:
                span = start_trace(
                    resolved_name,
                    arguments,
                    read_only=bool(getattr(descriptor, "read_only", True)),
                    idempotency_key=idempotency_key,
                )
            finish_trace(span, result, forced_error_code=code)
            return result

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict[str, object]
    ) -> dict[str, Any] | CallToolResult:
        meta = server.request_context.meta
        extra = getattr(meta, "model_extra", None) if meta is not None else None
        return await execute_tool(name, arguments, meta_extra=extra)

    def modern_result_meta() -> dict[str, Any]:
        try:
            from . import __version__ as karox_version
        except Exception:
            karox_version = "unknown"
        return {
            "io.modelcontextprotocol/serverInfo": {
                "name": "karox-proxy",
                "version": str(karox_version),
            }
        }

    def modern_tool_payload(tool: Tool) -> dict[str, Any]:
        dumper = getattr(tool, "model_dump", None)
        if callable(dumper):
            payload = dumper(mode="json", by_alias=True, exclude_none=True)
            if isinstance(payload, dict):
                return payload
        return {
            "name": tool.name,
            "description": tool.description,
            "inputSchema": tool.inputSchema,
        }

    def modern_call_result_payload(
        result: dict[str, Any] | CallToolResult,
    ) -> dict[str, Any]:
        if isinstance(result, CallToolResult):
            payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        else:
            payload = {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            result,
                            ensure_ascii=False,
                            sort_keys=True,
                            default=str,
                        ),
                    }
                ],
                "structuredContent": result,
                "isError": False,
            }
        payload["resultType"] = "complete"
        payload.setdefault("_meta", modern_result_meta())
        return payload

    def modern_error(
        request_id: Any,
        code: int,
        message: str,
    ) -> dict[str, Any]:
        return {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }

    async def modern_mcp_request(
        scope: dict[str, Any],
        receive: Any,
        send: Any,
    ) -> None:
        """Serve the stateless MCP 2026-07-28 wire without disturbing v1 clients.

        KaroX still uses the stable v1 Python SDK for its legacy ChatGPT/Claude
        clients. The July 2026 protocol removed initialize/sessions and added
        server/discover plus per-request metadata. Hyperagent moved to that wire
        before KaroX could migrate its whole SDK surface to v2, so this narrow
        adapter exposes the same tool runtime to both protocol eras.
        """
        if str(scope.get("method") or "").upper() != "POST":
            await JSONResponse(
                modern_error(None, -32600, "Modern MCP requests must use POST"),
                status_code=405,
            )(scope, receive, send)
            return

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            message_type = message.get("type")
            if message_type == "http.disconnect":
                return
            if message_type != "http.request":
                continue
            body_chunk = message.get("body", b"")
            if isinstance(body_chunk, (bytes, bytearray, memoryview)):
                rendered = bytes(body_chunk)
                total += len(rendered)
                if total > 16 * 1024 * 1024:
                    await JSONResponse(
                        modern_error(None, -32600, "MCP request body is too large"),
                        status_code=413,
                    )(scope, receive, send)
                    return
                chunks.append(rendered)
            if not bool(message.get("more_body", False)):
                break

        try:
            request = json.loads(b"".join(chunks).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            await JSONResponse(
                modern_error(None, -32700, "Parse error"),
                status_code=400,
            )(scope, receive, send)
            return
        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            await JSONResponse(
                modern_error(None, -32600, "Invalid Request"),
                status_code=400,
            )(scope, receive, send)
            return

        request_id = request.get("id")
        method = request.get("method")
        params = request.get("params")
        if not isinstance(method, str) or not isinstance(params, dict):
            await JSONResponse(
                modern_error(request_id, -32600, "Invalid Request"),
                status_code=400,
            )(scope, receive, send)
            return

        headers = scope_headers(scope)
        header_method = headers.get("mcp-method")
        if header_method and header_method != method:
            await JSONResponse(
                modern_error(request_id, -32600, "Mcp-Method header mismatch"),
                status_code=400,
            )(scope, receive, send)
            return
        meta = params.get("_meta")
        meta_extra = meta if isinstance(meta, dict) else {}
        body_version = meta_extra.get("io.modelcontextprotocol/protocolVersion")
        if body_version not in {None, MODERN_MCP_PROTOCOL_VERSION}:
            await JSONResponse(
                modern_error(request_id, -32600, "MCP protocol version mismatch"),
                status_code=400,
            )(scope, receive, send)
            return

        if method == "server/discover":
            discover_result: dict[str, Any] = {
                "supportedVersions": [
                    MODERN_MCP_PROTOCOL_VERSION,
                    *LEGACY_MCP_PROTOCOL_VERSIONS,
                ],
                "capabilities": {"tools": {}},
                "resultType": "complete",
                "_meta": modern_result_meta(),
            }
            await JSONResponse(
                {"jsonrpc": "2.0", "id": request_id, "result": discover_result}
            )(scope, receive, send)
            return

        if method == "tools/list":
            tools = await exposed_tools()
            list_result: dict[str, Any] = {
                "tools": [modern_tool_payload(tool) for tool in tools],
                "resultType": "complete",
                "_meta": modern_result_meta(),
            }
            await JSONResponse(
                {"jsonrpc": "2.0", "id": request_id, "result": list_result}
            )(scope, receive, send)
            return

        if method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments", {})
            header_name = headers.get("mcp-name")
            if (
                not isinstance(name, str)
                or not name
                or not isinstance(arguments, dict)
                or (header_name is not None and header_name != name)
            ):
                await JSONResponse(
                    modern_error(request_id, -32602, "Invalid tools/call params"),
                    status_code=400,
                )(scope, receive, send)
                return
            call_result = await execute_tool(name, arguments, meta_extra=meta_extra)
            await JSONResponse(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "result": modern_call_result_payload(call_result),
                }
            )(scope, receive, send)
            return

        await JSONResponse(
            modern_error(request_id, -32601, "Method not found"),
            status_code=404,
        )(scope, receive, send)

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
            raw = values[0].decode("utf-8").strip()
        except UnicodeDecodeError:
            return False
        # Accept both ``Bearer <token>`` and a bare ``<token>``.
        #
        # RFC 6750 spells the scheme, and this used to require it -- a bare token
        # raised ValueError on the split and became a 401 with no explanation.
        # But real MCP clients ask the user for a *token*, not for a header
        # value: ClickUp's "Authorization header" option says "you'll paste in a
        # token generated from your MCP server's settings", so what lands on the
        # wire depends on whether that client prepends the scheme itself.  A
        # connection that works or 401s based on an undocumented detail of the
        # peer is not a contract we can ask a user to debug, and the failure is
        # indistinguishable from a wrong secret.
        #
        # This widens only the *encoding* of the credential, never the check: the
        # token must still match exactly under compare_digest below.  A non-Bearer
        # scheme (``Basic``, ``Token``) is still refused rather than being
        # misread as a bare token that happens to contain a space, and the 401
        # still advertises ``WWW-Authenticate: Bearer`` so a conforming client is
        # told which scheme to use.
        scheme, _, remainder = raw.partition(" ")
        if remainder:
            if scheme.lower() != "bearer":
                return False
            supplied = remainder.strip()
        else:
            supplied = scheme
        if not supplied:
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
            try:
                async with manager.run():
                    while True:
                        message = await receive()
                        if message["type"] == "lifespan.startup":
                            await send({"type": "lifespan.startup.complete"})
                        elif message["type"] == "lifespan.shutdown":
                            await send({"type": "lifespan.shutdown.complete"})
                            return
            finally:
                closer = getattr(proxy, "close", None)
                if callable(closer):
                    try:
                        await anyio.to_thread.run_sync(closer)
                    except Exception:
                        # Optional runtime cleanup must not break ASGI shutdown.
                        pass
                if trace_store is not None:
                    trace_store.close()
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

        # Content-free transport telemetry starts at the raw ASGI boundary, before
        # authentication and before the MCP SDK sees the request.  It deliberately
        # records no headers, token, body, query string, tool name, or client IP.
        # The counters let bridge.diagnostics distinguish an upstream connection
        # failure (request never arrived) from a server-side response/disconnect.
        transport_runtime["requests_started"] += 1
        transport_runtime["last_http_version"] = str(scope.get("http_version") or "")[:16]
        transport_runtime["last_method"] = str(scope.get("method") or "")[:16]
        transport_runtime["last_request_started_at"] = time.time()
        request_started_perf = time.perf_counter()
        response_body_bytes = 0
        activity_finished = False
        _transport_activity_started()

        async def tracked_receive() -> Any:
            message = await receive()
            if message.get("type") == "http.disconnect":
                transport_runtime["client_disconnects"] += 1
            return message

        async def tracked_send(message: MutableMapping[str, Any]) -> None:
            nonlocal response_body_bytes, activity_finished
            message_type = message.get("type")
            final_body = False
            if message_type == "http.response.start":
                transport_runtime["responses_started"] += 1
                status = message.get("status")
                transport_runtime["last_status"] = (
                    int(status) if isinstance(status, int) and not isinstance(status, bool) else None
                )
            elif message_type == "http.response.body":
                body = message.get("body")
                if isinstance(body, (bytes, bytearray, memoryview)):
                    response_body_bytes += len(body)
                final_body = not bool(message.get("more_body", False))
                if final_body:
                    duration_ms = round((time.perf_counter() - request_started_perf) * 1000, 3)
                    transport_runtime["responses_completed"] += 1
                    transport_runtime["last_response_completed_at"] = time.time()
                    transport_runtime["last_request_duration_ms"] = duration_ms
                    transport_runtime["max_request_duration_ms"] = max(
                        float(transport_runtime["max_request_duration_ms"]),
                        duration_ms,
                    )
                    transport_runtime["last_response_body_bytes"] = response_body_bytes
                    transport_runtime["max_response_body_bytes"] = max(
                        int(transport_runtime["max_response_body_bytes"]),
                        response_body_bytes,
                    )
            await send(message)
            # The self-restart coordinator may terminate the process as soon as
            # this counter advances, so publish completion only after ASGI send
            # itself returned for the final response body.
            if final_body and not activity_finished:
                activity_finished = True
                _transport_activity_finished(completed=True)

        try:
            if not await authorized(scope):
                transport_runtime["unauthorized_requests"] += 1
                await Response(
                    "unauthorized",
                    status_code=401,
                    headers=dict(unauthorized_headers or {}),
                )(scope, tracked_receive, tracked_send)
                return
            transport_runtime["authorized_requests"] += 1
            if scope_headers(scope).get("mcp-protocol-version") == MODERN_MCP_PROTOCOL_VERSION:
                await modern_mcp_request(scope, tracked_receive, tracked_send)
                return
            await manager.handle_request(scope, tracked_receive, tracked_send)
        finally:
            if not activity_finished:
                activity_finished = True
                _transport_activity_finished(completed=False)

    return app
