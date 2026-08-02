"""Live connectivity checks for saved connections.

Both checks run the *real* transport the runtime uses, not a mock:

* an MCP client target is tested by opening a Streamable HTTP MCP session with
  the connection's auth headers and performing ``initialize`` + ``tools/list`` --
  the same ``mcp`` SDK client path ``McpClient`` takes;
* a model provider is tested by building the registered adapter via
  :class:`~karox.provider_factory.ProviderFactory` and sending one minimal
  request, exactly as ``karox model test`` does.

Errors are classified into the small vocabulary the TUI shows the user (DNS
failure, timeout, unauthorized, ...).  No function here ever returns a secret,
and error text is passed through :func:`~karox.security.redact` so a classified
failure cannot leak the credential via its own message.
"""

from __future__ import annotations

__test__ = False

from datetime import timedelta
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

import anyio
import httpx

from .connections import McpClientTarget, auth_headers
from .credentials import CredentialError
from .provider_factory import ProviderFactory
from .providers import ModelMessage, ModelRequest, ProviderError, ProviderErrorKind
from .registry import ModelRecord, ProviderRecord
from .security import redact


def _clean(message: str) -> str:
    return redact(message, secrets=()) if message else ""


def _first_cause_message(exc: BaseException) -> str:
    """Drill into ``ExceptionGroup`` wrappers to the first real cause's message.

    The MCP SDK (via ``anyio``) runs ``initialize``/``list_tools`` inside a
    ``TaskGroup``, so a transport failure surfaces as a ``ExceptionGroup`` whose
    own ``str()`` is the uninformative "unhandled errors in a TaskGroup (1
    sub-exception)".  This walks the group tree (a group may nest groups) to
    the first non-group leaf and returns its ``str()``, so the user sees the
    actual HTTP status / connect refusal / timeout and the classifier matches
    the real error instead of the wrapper.
    """
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None:
        # Guard against a pathological cycle.
        if id(current) in seen:
            break
        seen.add(id(current))
        # ``BaseExceptionGroup.exceptions`` is the unwrapped sub-exception list
        # (Python 3.11+); older exceptions raise AttributeError and we fall
        # back to the exception's own message.
        leaf: BaseException | None = None
        nested: BaseException | None = None
        sub_exceptions = getattr(current, "exceptions", None)
        if sub_exceptions:
            # Prefer a leaf (non-group) sub-exception; otherwise descend the
            # first nested group.
            for sub in sub_exceptions:
                if getattr(sub, "exceptions", None):
                    nested = sub
                else:
                    leaf = sub
                    break
            if leaf is not None:
                return _clean(str(leaf)) or _clean(str(current))
            current = nested
            continue
        # Plain exception: also chase its ``__cause__``/``__context__`` if the
        # message itself is empty (some SDK errors carry the detail only there).
        message = _clean(str(current))
        if message:
            return message
        chained = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        if chained is not None:
            current = chained
            continue
        return message
    return _clean(str(exc))


def _classify_http(status: int) -> str:
    if status in {401, 403}:
        return "unauthorized"
    if status == 404:
        return "model_not_found"
    if status == 429:
        return "rate_limit"
    if 500 <= status <= 599:
        return "server_error"
    if 400 <= status <= 499:
        return "client_error"
    return "http_error"


def _classify_request_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "dns_failure"
    if isinstance(exc, httpx.HTTPError):
        return "network"
    return "network"


def test_mcp_client_target(
    target: McpClientTarget,
    *,
    endpoint_url: str,
    secret: str,
    timeout_seconds: float = 15.0,
) -> Dict[str, Any]:
    """Test a saved MCP client target against a running bridge endpoint.

    ``endpoint_url`` is the fully-resolved URL the external client would use
    (the tunnel URL once the bridge is up, or ``http://127.0.0.1:<port>/mcp``
    for a local test).  ``secret`` is the resolved connection credential.

    The check honours the connection's auth scheme: ``bearer`` and
    ``custom_header`` run a real MCP ``initialize`` + ``tools/list`` over the
    Streamable HTTP transport; ``api_key`` hits the OpenAPI discovery wire;
    ``oauth`` reports that it requires the web bridge's live approval flow; and
    ``none`` reports it as unsafe (the production bridge rejects unauthenticated
    calls, which is what the check then shows).
    """
    if not endpoint_url or not isinstance(endpoint_url, str):
        return {"state": "failed", "failure_kind": "invalid_url", "detail": "no endpoint URL"}
    parts = urlsplit(endpoint_url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return {"state": "failed", "failure_kind": "invalid_url", "detail": "endpoint URL is invalid"}
    if target.auth_scheme == "none":
        return {
            "state": "failed",
            "failure_kind": "unsafe",
            "detail": "no-auth connections are unsafe and rejected by the production bridge",
        }
    if target.auth_scheme == "oauth":
        return {
            "state": "needs_oauth_flow",
            "failure_kind": "oauth",
            "detail": (
                "OAuth requires the web bridge profile and a live approval; "
                "start the bridge with this connection to test it end to end."
            ),
        }
    if not secret:
        return {"state": "failed", "failure_kind": "unauthorized", "detail": "no secret provided"}
    if target.transport == "openapi" or target.auth_scheme in {"api_key"}:
        return _test_openapi_wire(endpoint_url, target, secret, timeout_seconds)
    return _test_mcp_wire(endpoint_url, target, secret, timeout_seconds)


def _test_openapi_wire(
    endpoint_url: str,
    target: McpClientTarget,
    secret: str,
    timeout_seconds: float,
) -> Dict[str, Any]:
    base = endpoint_url.rstrip("/")
    headers = auth_headers(target, secret)
    try:
        with httpx.Client(timeout=timeout_seconds, follow_redirects=False) as client:
            discovery = client.get(f"{base}/openapi.json", headers=headers)
            if discovery.status_code == 401:
                return _failure("unauthorized", "The server rejected the credential (HTTP 401).")
            if discovery.status_code >= 400:
                return _failure(
                    _classify_http(discovery.status_code),
                    f"The discovery document returned HTTP {discovery.status_code}.",
                )
            tools = client.get(f"{base}/tools", headers=headers)
            tool_count = _openapi_tool_count(tools)
            if tools.status_code == 401:
                return _failure("unauthorized", "The server rejected the credential (HTTP 401).")
            if tools.status_code >= 400:
                return _failure(
                    _classify_http(tools.status_code),
                    f"The tools list returned HTTP {tools.status_code}.",
                )
    except httpx.TimeoutException as exc:
        return _failure("timeout", _clean(str(exc)))
    except httpx.ConnectError as exc:
        return _failure("dns_failure", _clean(str(exc)))
    except httpx.HTTPError as exc:
        return _failure("network", _clean(str(exc)))
    return {
        "state": "ok",
        "wire": "openapi",
        "tool_count": tool_count,
        "detail": f"OpenAPI discovery reachable; {tool_count} tool(s) listed.",
    }


def _openapi_tool_count(response: httpx.Response) -> int:
    try:
        payload = response.json()
    except ValueError:
        return 0
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        tools = payload.get("tools")
        if isinstance(tools, list):
            return len(tools)
        paths = payload.get("paths")
        if isinstance(paths, dict):
            return sum(1 for methods in paths.values() if isinstance(methods, dict))
    return 0


def _test_mcp_wire(
    endpoint_url: str,
    target: McpClientTarget,
    secret: str,
    timeout_seconds: float,
) -> Dict[str, Any]:
    from mcp import ClientSession

    from .mcp_client import streamable_http_transport

    headers = auth_headers(target, secret)

    async def run() -> list[Any]:
        with anyio.fail_after(timeout_seconds):
            async with streamable_http_transport(
                endpoint_url,
                headers=headers,
                timeout_seconds=timeout_seconds,
                terminate_on_close=False,
            ) as streams:
                async with ClientSession(
                    streams[0], streams[1], read_timeout_seconds=timedelta(seconds=timeout_seconds)
                ) as session:
                    await session.initialize()
                    response = await session.list_tools()
                    return list(response.tools)

    try:
        tools = anyio.run(run)
    except httpx.TimeoutException as exc:
        return _failure("timeout", _clean(str(exc)))
    except httpx.ConnectError as exc:
        return _failure("dns_failure", _clean(str(exc)))
    except httpx.HTTPError as exc:
        return _failure("network", _clean(str(exc)))
    except Exception as exc:  # MCP / JSON-RPC / auth failures land here
        # The MCP SDK runs initialize/tools-list inside an ``anyio`` TaskGroup,
        # so a transport-level failure (HTTP 421 host-not-allowed, a connect
        # refusal, a timeout) is wrapped as a sub-exception of an
        # ``ExceptionGroup`` whose own message is the useless
        # "unhandled errors in a TaskGroup (1 sub-exception)".  Unwrap to the
        # first real cause so the user sees the actual reason and the
        # classification below matches the real error, not the wrapper.
        message = _clean(_first_cause_message(exc))
        lower = message.lower()
        if "401" in lower or "unauthorized" in lower:
            return _failure("unauthorized", message or "The server rejected the credential.")
        if "403" in lower or "forbidden" in lower:
            return _failure("unauthorized", message or "The credential lacks permission.")
        if "421" in lower or "host_not_allowed" in lower or "host not allowed" in lower:
            return _failure("host_not_allowed", message)
        # DNS resolution failures surface from the wrapped transport as
        # ``socket.gaierror`` (``[Errno 11001] getaddrinfo failed`` on Windows,
        # ``[Errno -2] Name or service not known`` on Linux).  They are not an
        # MCP-level error -- the server was never reached -- so classify them
        # as ``dns_failure`` so the caller can fall back to a loopback probe
        # rather than treating the endpoint as broken.
        if (
            "getaddrinfo" in lower
            or "gaierror" in lower
            or "name or service not known" in lower
            or "nodename nor servname" in lower
            or "temporary failure in name resolution" in lower
        ):
            return _failure("dns_failure", message)
        if "timeout" in lower or "timed out" in lower:
            return _failure("timeout", message)
        if "handshake" in lower or "initialize" in lower:
            return _failure("mcp_handshake_failed", message)
        if "tls" in lower or "ssl" in lower or "certificate" in lower:
            return _failure("tls_error", message)
        return _failure("mcp_error", message)
    return {
        "state": "ok",
        "wire": "streamable_http",
        "tool_count": len(tools),
        "detail": f"MCP initialize and tools/list succeeded; {len(tools)} tool(s) listed.",
    }


def _failure(kind: str, detail: str) -> Dict[str, Any]:
    return {"state": "failed", "failure_kind": kind, "detail": detail.strip()[:500]}


def test_model_provider(
    provider_record: ProviderRecord,
    model_record: ModelRecord,
    *,
    timeout_seconds: Optional[float] = None,
) -> Dict[str, Any]:
    """Send one minimal request through a registered provider's real adapter."""
    deadline = min(60.0, timeout_seconds or provider_record.timeout_seconds)
    try:
        response = (
            ProviderFactory()
            .create(provider_record)
            .complete(
                ModelRequest(
                    model=model_record.model_id,
                    messages=(ModelMessage("user", "Reply with exactly OK."),),
                    max_output_tokens=8,
                    deadline_seconds=deadline,
                )
            )
        )
    except ProviderError as exc:
        kind = _provider_error_kind(exc)
        status = f" (HTTP {exc.status_code})" if exc.status_code else ""
        return {
            "state": "failed",
            "failure_kind": kind,
            "detail": _clean(f"{exc.safe_message}{status}") or str(exc.kind),
            "provider_id": provider_record.provider_id,
            "model_id": model_record.model_id,
        }
    except CredentialError as exc:
        return {
            "state": "failed",
            "failure_kind": "unauthorized",
            "detail": _clean(str(exc)) or "the saved credential could not be read",
            "provider_id": provider_record.provider_id,
            "model_id": model_record.model_id,
        }
    except Exception as exc:  # network / DNS / TLS / timeout from the transport
        message = _clean(str(exc))
        lower = message.lower()
        if "timeout" in lower or "timed out" in lower:
            kind = "timeout"
        elif "name resolution" in lower or "nodename" in lower or "dns" in lower:
            kind = "dns_failure"
        elif "ssl" in lower or "tls" in lower or "certificate" in lower:
            kind = "tls_error"
        elif "connect" in lower:
            kind = "network"
        else:
            kind = "network"
        return {
            "state": "failed",
            "failure_kind": kind,
            "detail": message or type(exc).__name__,
            "provider_id": provider_record.provider_id,
            "model_id": model_record.model_id,
        }
    return {
        "state": "ok",
        "provider_id": provider_record.provider_id,
        "model_id": model_record.model_id,
        "finish_reason": response.finish_reason,
        "usage": response.usage,
        "transport_attempts": response.transport_attempts,
    }


def _provider_error_kind(exc: ProviderError) -> str:
    if exc.kind in {ProviderErrorKind.AUTHENTICATION, ProviderErrorKind.PERMISSION}:
        return "unauthorized"
    if exc.kind == ProviderErrorKind.MODEL_UNAVAILABLE:
        return "model_not_found"
    if exc.kind == ProviderErrorKind.RATE_LIMIT:
        return "rate_limit"
    if exc.kind == ProviderErrorKind.TRANSPORT:
        return "network"
    if exc.status_code == 404:
        return "model_not_found"
    if exc.status_code == 429:
        return "rate_limit"
    if exc.status_code and 500 <= exc.status_code <= 599:
        return "server_error"
    return "client_error"


__all__ = ["test_mcp_client_target", "test_model_provider"]
