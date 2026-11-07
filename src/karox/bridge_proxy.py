"""Authenticated Streamable HTTP wire server for a hosted KaroX bridge.

The same ASGI app serves both the stable MCP SDK transport and the stateless
MCP 2026-07-28 JSON wire, behind a constant-time bearer check and a
Host/Origin rebinding guard.
"""

from __future__ import annotations

from .bridge_wire import (  # noqa: F401
    _SAFE_INVALID_REQUEST_PREFIXES as _SAFE_INVALID_REQUEST_PREFIXES,
    _SAFE_DENIED_PREFIXES as _SAFE_DENIED_PREFIXES,
    _ROUTABLE_ALIAS_NAMES as _ROUTABLE_ALIAS_NAMES,
    _CALLER_AUTHORED_ERRORS as _CALLER_AUTHORED_ERRORS,
    BRIDGE_ERROR_MESSAGES as BRIDGE_ERROR_MESSAGES,
    ALLOWED_HOSTS_ENVIRONMENT as ALLOWED_HOSTS_ENVIRONMENT,
    HOST_REJECTION_HINT as HOST_REJECTION_HINT,
    LEGACY_MCP_PROTOCOL_VERSIONS as LEGACY_MCP_PROTOCOL_VERSIONS,
    MODERN_MCP_PROTOCOL_VERSION as MODERN_MCP_PROTOCOL_VERSION,
    _AliasCatalogLogFilter as _AliasCatalogLogFilter,
    _DETAIL_LIMIT as _DETAIL_LIMIT,
    _DIAGNOSTICS_TOOL_NAME as _DIAGNOSTICS_TOOL_NAME,
    _HOME_PATH_TEXT as _HOME_PATH_TEXT,
    _LOGGER as _LOGGER,
    _LOOPBACK_HOSTS as _LOOPBACK_HOSTS,
    _NOT_LISTED_MESSAGE as _NOT_LISTED_MESSAGE,
    _POSIX_PATH_TEXT as _POSIX_PATH_TEXT,
    _SAFE_DENIED_OPERATION as _SAFE_DENIED_OPERATION,
    _SAFE_OCCURRENCE_DETAIL as _SAFE_OCCURRENCE_DETAIL,
    _TRANSPORT_ACTIVE_REQUESTS as _TRANSPORT_ACTIVE_REQUESTS,
    _TRANSPORT_ACTIVITY_LOCK as _TRANSPORT_ACTIVITY_LOCK,
    _TRANSPORT_RESPONSES_COMPLETED as _TRANSPORT_RESPONSES_COMPLETED,
    _UNC_PATH_TEXT as _UNC_PATH_TEXT,
    _URL_TEXT as _URL_TEXT,
    _WINDOWS_PATH_TEXT as _WINDOWS_PATH_TEXT,
    _idna_normalize as _idna_normalize,
    _install_alias_catalog_log_filter as _install_alias_catalog_log_filter,
    _log_rebinding_421 as _log_rebinding_421,
    _maybe_record_client_evidence as _maybe_record_client_evidence,
    _transport_activity_finished as _transport_activity_finished,
    _transport_activity_started as _transport_activity_started,
    bridge_denied_detail as bridge_denied_detail,
    bridge_error_code as bridge_error_code,
    bridge_error_detail as bridge_error_detail,
    bridge_error_result as bridge_error_result,
    caller_safe_detail as caller_safe_detail,
    derive_idempotency_key as derive_idempotency_key,
    forwarded_host as forwarded_host,
    normalize_host as normalize_host,
    origin_is_allowed as origin_is_allowed,
    peer_is_loopback as peer_is_loopback,
    rebinding_rejection as rebinding_rejection,
    resolve_allowed_hosts as resolve_allowed_hosts,
    resolve_wire_tool_name as resolve_wire_tool_name,
    scope_headers as scope_headers,
    stale_catalog_hint as stale_catalog_hint,
    transport_activity_snapshot as transport_activity_snapshot,
    transport_security_settings as transport_security_settings,
    utf8_call_result as utf8_call_result,
    wire_tool_name as wire_tool_name,
)

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from typing import Any, Callable, Mapping, MutableMapping, Optional, Sequence

import anyio
from mcp.server import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.types import CallToolResult, TextContent, Tool, ToolAnnotations
from starlette.responses import JSONResponse, Response

from .artifacts import ArtifactStore
from .hosted_core import (
    DEFAULT_HOSTED_DEADLINE_SECONDS,
    HostedApprovalRequired,
    HostedBridgeError,
    HostedToolRuntime,
)
from .result_envelope import artifact_backed_result
from .tool_telemetry import (
    ToolTraceContext,
    ToolTraceSpan,
    default_trace_store,
)




# Auth/discovery are the bridge control plane. They must stay responsive even
# when many agents are simultaneously running long repository/check commands in
# AnyIO's default worker pool. A separate capacity limiter makes run_sync create
# control-plane workers without waiting for a default-pool token, while keeping
# live credential resolution (and therefore immediate key rotation) intact.
_CONTROL_PLANE_THREAD_LIMITER = anyio.CapacityLimiter(8)
# Tool execution gets its own bounded worker budget as well. Without this,
# several long-running hosted agents consume AnyIO's shared default tokens and
# unrelated MCP calls start timing out or looking like bridge outages. Keep the
# control plane fully independent, while still bounding CPU/disk pressure from
# actual tools when many chats share one durable bridge.
_TOOL_EXECUTION_THREAD_LIMITER = anyio.CapacityLimiter(24)
# Keep genuinely long synchronous calls out of the latency-sensitive tool pool.
# A few model/pytest/server operations may legitimately occupy a worker for
# minutes; they must never consume every token needed by repo.read/status/search.
_LONG_TOOL_EXECUTION_THREAD_LIMITER = anyio.CapacityLimiter(8)
# Per-call bookkeeping (session revision snapshot, artifact compaction of large
# results, SQLite telemetry) is disk I/O. It used to run on the event loop that
# also serves health probes, so a slow disk or busy SQLite WAL under parallel
# hosted load stalled every connection and the owner recycled the child.
_RESULT_POSTPROCESS_THREAD_LIMITER = anyio.CapacityLimiter(8)
_LONG_RUNNING_TOOL_NAMES = frozenset(
    {
        "karox.command.run",
        "karox.checks.run",
        "karox.tests.run",
        "karox.checks.run_affected",
        "karox.task.execute_plan",
        "karox.dev_server.start",
        "karox.dev_server.restart",
    }
)






















_install_alias_catalog_log_filter()


























def chat_push_approval_handoff_result(
    approval: HostedApprovalRequired,
    *,
    wire_name: str,
    arguments: Mapping[str, Any],
) -> CallToolResult:
    """Return a model-readable handoff for chat-native Git push approval.

    Clients without MCP elicitation cannot prove the user's chat reply at the
    protocol layer. KaroX already supports a deliberately weaker compatibility
    path where the agent records the user's explicit approval as a short-lived
    reported task fact. This result makes that required relay explicit instead
    of falsely telling the client that an identical retry is sufficient.
    """

    preview = dict(approval.preview)
    remote = preview.get("remote")
    branch = preview.get("branch")
    head = preview.get("head")
    if not all(isinstance(value, str) and value for value in (remote, branch, head)):
        return bridge_error_result(
            "denied",
            detail="Git push approval could not be bound to an exact remote, branch, and HEAD",
        )

    checkpoint_arguments: dict[str, Any] = {
        "updates": {
            "chat_user_approval": {
                "value": {
                    "approved": True,
                    "action_kind": "git.push",
                    "remote": remote,
                    "branch": branch,
                    "head": head,
                },
                "origin": "reported_by_agent",
                "evidence": ["user.chat.explicit_approval"],
            }
        }
    }
    workstream_id = arguments.get("workstream_id")
    if isinstance(workstream_id, str) and workstream_id:
        checkpoint_arguments["workstream_id"] = workstream_id

    text = (
        f"approval_required: ask the user to approve exactly one push of HEAD {head} "
        f"to {remote}/{branch}. After the user explicitly approves in chat, call "
        "karox_task_checkpoint with structuredContent.chat_approval.checkpoint_arguments, "
        f"then retry {wire_name} once with the original arguments. Do not checkpoint "
        "approval before the user says yes."
    )
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent={
            "ok": False,
            "error_code": "approval_required",
            "approval_required": True,
            "error": approval.message,
            "action_kind": approval.action_kind,
            "risk": approval.risk,
            "consequence": approval.consequence,
            "preview": preview,
            "chat_approval": {
                "after_explicit_user_approval_only": True,
                "checkpoint_tool": "karox_task_checkpoint",
                "checkpoint_arguments": checkpoint_arguments,
                "retry_tool": wire_name,
                "retry_arguments": dict(arguments),
                "expires_in_seconds": 300,
            },
        },
        # A transport-level MCP error hides structuredContent in some hosted
        # clients. The action still did not execute (ok=False), but this complete
        # result lets the agent carry out the explicit chat-approval handoff.
        isError=False,
    )




















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

    # MCP 2026-07-28 returns human-input continuations in an opaque
    # ``requestState`` value that the client echoes on a fresh retry. Treat that
    # value as attacker-controlled: approval state is integrity-protected,
    # short-lived, and bound to the exact tool arguments. The sealing key is
    # process-local by design; if the child recycles mid-dialog the retry fails
    # closed and the new child can ask again instead of trusting stale state.
    approval_state_secret = secrets.token_bytes(32)
    approval_state_used: dict[str, float] = {}
    approval_state_lock = threading.Lock()

    def _approval_b64_encode(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    def _approval_b64_decode(value: str) -> bytes:
        if not isinstance(value, str) or not value or len(value) > 16_384:
            raise ValueError("approval request state is invalid")
        padding = "=" * (-len(value) % 4)
        try:
            decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
        except (ValueError, UnicodeEncodeError) as exc:
            raise ValueError("approval request state is invalid") from exc
        # urlsafe_b64decode accepts non-canonical trailing bits. Re-encode and
        # require a byte-exact canonical spelling so changing even the final
        # character of requestState can never alias the same authenticated bytes.
        if _approval_b64_encode(decoded) != value:
            raise ValueError("approval request state is not canonical base64url")
        return decoded

    def _approval_arguments_digest(name: str, arguments: Mapping[str, Any]) -> str:
        canonical = json.dumps(
            {"name": name, "arguments": dict(arguments)},
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def _seal_approval_state(
        *,
        wire_name: str,
        arguments: Mapping[str, Any],
        approval: HostedApprovalRequired,
    ) -> str:
        payload = {
            "v": 1,
            "wire_name": wire_name,
            "arguments_sha256": _approval_arguments_digest(wire_name, arguments),
            "action_digest": approval.action_digest,
            "expires_at": time.time() + 300.0,
        }
        raw = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        signature = hmac.new(approval_state_secret, raw, hashlib.sha256).digest()
        return f"v1.{_approval_b64_encode(raw)}.{_approval_b64_encode(signature)}"

    def _open_approval_state(
        request_state: Any,
        *,
        wire_name: str,
        arguments: Mapping[str, Any],
    ) -> dict[str, Any]:
        if not isinstance(request_state, str):
            raise ValueError("approval request state is missing")
        parts = request_state.split(".")
        if len(parts) != 3 or parts[0] != "v1":
            raise ValueError("approval request state is invalid")
        raw = _approval_b64_decode(parts[1])
        signature = _approval_b64_decode(parts[2])
        expected = hmac.new(approval_state_secret, raw, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("approval request state failed integrity validation")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("approval request state is invalid") from exc
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ValueError("approval request state is invalid")
        if payload.get("wire_name") != wire_name:
            raise ValueError("approval retry belongs to another tool")
        if payload.get("arguments_sha256") != _approval_arguments_digest(wire_name, arguments):
            raise ValueError("approval retry arguments changed")
        expires_at = payload.get("expires_at")
        if (
            isinstance(expires_at, bool)
            or not isinstance(expires_at, (int, float))
            or float(expires_at) < time.time()
        ):
            raise ValueError("approval request state expired")
        action_digest = payload.get("action_digest")
        if not isinstance(action_digest, str) or len(action_digest) != 64:
            raise ValueError("approval action digest is invalid")
        return payload

    def _consume_approval_state(request_state: str, payload: Mapping[str, Any]) -> None:
        digest = hashlib.sha256(request_state.encode("utf-8")).hexdigest()
        expires_at = float(payload["expires_at"])
        now = time.time()
        with approval_state_lock:
            for key, expiry in tuple(approval_state_used.items()):
                if expiry < now:
                    approval_state_used.pop(key, None)
            if digest in approval_state_used:
                raise ValueError("approval request state was already used")
            approval_state_used[digest] = expires_at

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
        "started_at": time.time(),
        "requests_started": 0,
        "responses_started": 0,
        "responses_completed": 0,
        "client_disconnects": 0,
        "authorized_requests": 0,
        "unauthorized_requests": 0,
        "mcp_tool_calls_started": 0,
        "mcp_tool_calls_completed": 0,
        "mcp_tool_calls_succeeded": 0,
        "mcp_tool_calls_failed": 0,
        "last_mcp_tool_call_started_at": None,
        "last_mcp_tool_call_completed_at": None,
        "last_mcp_protocol_version": None,
        "last_mcp_dispatch_path": None,
        "last_correlation_id": None,
        "last_successful_invocation_at": None,
        "last_failure_class": None,
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
    lifecycle_runtime: dict[str, Any] = {
        "state": "STARTING",
        "ready": False,
        "ready_at": None,
        "canary_result": "PENDING",
        "canary_failure_class": None,
        "server_pid": os.getpid(),
        # Hosted tools execute in bounded worker threads in this server process;
        # there is no second mutable global worker process to lose or orphan.
        "worker_pid": os.getpid(),
    }
    # The durable owner can outlive many MCP children and therefore carries a
    # point-in-time diagnostics JSON. A freshly respawned child may have a newer
    # tool catalogue than that owner. Track the descriptors this child actually
    # serves so diagnostics can self-heal instead of claiming a new guarded tool
    # is unavailable until the whole owner/tunnel is restarted.
    live_tool_names: list[str] = []

    def current_diagnostics() -> Optional[dict[str, Any]]:
        if diagnostics_payload is None:
            return None
        live = dict(diagnostics_payload)
        # Catalog self-heal applies only to owner-manufactured snapshots: they
        # always carry the tool_catalog structure this child refreshes. An
        # explicitly injected minimal payload is documented as verbatim, so a
        # test or operator passing one sees it unchanged apart from live
        # transport counters.
        if live_tool_names and isinstance(live.get("tool_catalog"), dict):
            # The child is authoritative for what it actually serves. A durable
            # owner may have generated diagnostics before this child learned a
            # new backwards-compatible tool, so repair the catalog/counts here
            # rather than requiring an owner/tunnel restart just to make status
            # truthful.
            actual_tools = list(live_tool_names)
            live["available_tools"] = actual_tools
            capabilities = live.get("client_capabilities")
            if isinstance(capabilities, dict):
                refreshed_capabilities = dict(capabilities)
                refreshed_capabilities["available_tool_count"] = len(actual_tools)
                refreshed_capabilities["available_tools_digest"] = hashlib.sha256(
                    "\0".join(actual_tools).encode("utf-8")
                ).hexdigest()
                live["client_capabilities"] = refreshed_capabilities

            catalog = live.get("tool_catalog")
            if isinstance(catalog, dict):
                refreshed_catalog = dict(catalog)
                refreshed_catalog["advertised_tool_count"] = len(actual_tools)
                raw_groups = refreshed_catalog.get("groups")
                groups = (
                    {key: list(value) for key, value in raw_groups.items() if isinstance(value, list)}
                    if isinstance(raw_groups, dict)
                    else {}
                )
                grouped = {item for values in groups.values() for item in values}
                for tool_name in actual_tools:
                    if tool_name in grouped:
                        continue
                    if tool_name.startswith("karox.browser."):
                        group = "browser"
                    elif tool_name.startswith("karox.dev_server."):
                        group = "devserver"
                    elif tool_name.startswith("karox.memory."):
                        group = "memory"
                    elif tool_name.startswith("karox.task."):
                        group = "task"
                    elif tool_name.startswith("karox.runtime."):
                        group = "admin"
                    else:
                        group = "core"
                    groups.setdefault(group, []).append(tool_name)
                    grouped.add(tool_name)
                refreshed_catalog["groups"] = groups
                live["tool_catalog"] = refreshed_catalog

            restrictions = live.get("mode_restrictions")
            if isinstance(restrictions, dict):
                refreshed_restrictions = dict(restrictions)
                if "karox.git.push" in actual_tools:
                    refreshed_restrictions["no_git_push"] = False
                    refreshed_restrictions["git_push_approval"] = (
                        "one_shot_mcp_user_confirmation"
                    )
                live["mode_restrictions"] = refreshed_restrictions
        live["transport_runtime"] = dict(transport_runtime)
        activity = transport_activity_snapshot()
        started_at = float(transport_runtime["started_at"])
        live["lifecycle"] = {
            **dict(lifecycle_runtime),
            "uptime_seconds": round(max(0.0, time.time() - started_at), 3),
            "active_requests_count": activity["active_requests"],
            # Streamable HTTP is deliberately stateless, and the protocol does
            # not expose a stable client identity on each request. Report that
            # limitation instead of inventing a misleading session count.
            "active_sessions_count": None,
            "active_sessions_reason": "stateless_transport_has_no_client_identity",
            "server_state": lifecycle_runtime["state"],
            "worker_state": "READY" if lifecycle_runtime["ready"] else lifecycle_runtime["state"],
            "registration_state": (
                "ENDPOINT_READY" if lifecycle_runtime["ready"] else "NOT_READY"
            ),
            "transport_state": (
                "READY" if lifecycle_runtime["ready"] else lifecycle_runtime["state"]
            ),
        }
        live.update(
            {
                "process_state": lifecycle_runtime["state"],
                "worker_state": live["lifecycle"]["worker_state"],
                "server_state": live["lifecycle"]["server_state"],
                "registration_state": live["lifecycle"]["registration_state"],
                "transport_state": live["lifecycle"]["transport_state"],
                "uptime_seconds": live["lifecycle"]["uptime_seconds"],
                "active_sessions_count": live["lifecycle"]["active_sessions_count"],
                "active_requests_count": live["lifecycle"]["active_requests_count"],
                "last_successful_invocation": transport_runtime[
                    "last_successful_invocation_at"
                ],
                "last_failure_class": transport_runtime["last_failure_class"],
                "server_pid": lifecycle_runtime["server_pid"],
                "worker_pid": lifecycle_runtime["worker_pid"],
                "readiness_result": bool(lifecycle_runtime["ready"]),
                "end_to_end_canary_result": lifecycle_runtime["canary_result"],
            }
        )

        # The release runtime has no external supervisor process. Diagnostics
        # still report the same shape, with an explicit reason, so a hosted
        # client can tell "no supervisor configured" apart from a dead one
        # instead of seeing the key disappear after an upgrade.
        saved_profile = live.get("saved_profile")
        if isinstance(saved_profile, str) and saved_profile:
            live["durable_supervisor"] = {
                "desired_running": False,
                "supervisor_alive": False,
                "supervisor_process_alive": False,
                "supervisor_heartbeat_fresh": False,
                "supervisor_heartbeat_age_seconds": None,
                "supervisor_main_progress_fresh": False,
                "supervisor_main_progress_age_seconds": None,
                "supervisor_pid": None,
                "heartbeat_at": None,
                "last_owner_pid": os.getpid(),
                "restart_count": 0,
                "last_restart_at": None,
                "last_error": "supervisor_not_configured",
            }
            live["reconnect_count"] = 0
        live["human_summary"] = (
            f"KaroX {lifecycle_runtime['state']} | "
            f"canary={lifecycle_runtime['canary_result']} | "
            f"active_requests={activity['active_requests']} | "
            f"tool_calls_ok={transport_runtime['mcp_tool_calls_succeeded']} | "
            f"tool_calls_failed={transport_runtime['mcp_tool_calls_failed']}"
        )
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
        failed = bool(forced_error_code) or bool(getattr(result, "isError", False))
        if failed:
            transport_runtime["mcp_tool_calls_failed"] += 1
            transport_runtime["last_failure_class"] = forced_error_code or "tool_error"
        else:
            transport_runtime["mcp_tool_calls_succeeded"] += 1
            transport_runtime["last_successful_invocation_at"] = time.time()
            transport_runtime["last_failure_class"] = None
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

    async def finish_trace_off_loop(
        span: Optional[ToolTraceSpan],
        result: Any,
        *,
        forced_error_code: Optional[str] = None,
    ) -> None:
        """Count completion on the loop; persist the span in a worker thread."""
        finish_trace(None, result, forced_error_code=forced_error_code)
        if span is None:
            return

        def persist() -> None:
            try:
                revision_after = (
                    span.repository_revision_before
                    if span.read_only
                    else repository_revision()
                )
                span.finish(
                    result,
                    repository_revision_after=revision_after,
                    forced_error_code=forced_error_code,
                )
            except Exception:
                pass

        try:
            await anyio.to_thread.run_sync(
                persist, limiter=_RESULT_POSTPROCESS_THREAD_LIMITER
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
        nonlocal descriptor_routes, live_tool_names
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
        live_tool_names = list(by_internal)
        descriptor_routes = (by_internal, by_wire)
        return descriptor_routes

    async def exposed_tools() -> list[Tool]:
        descriptors = await anyio.to_thread.run_sync(
            proxy.descriptors,
            limiter=_CONTROL_PLANE_THREAD_LIMITER,
        )
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
        approved_action_digest: Optional[str] = None,
    ) -> dict[str, Any] | CallToolResult:
        transport_runtime["mcp_tool_calls_started"] += 1
        transport_runtime["last_mcp_tool_call_started_at"] = time.time()
        resolved_name = name
        descriptor: Any = None
        idempotency_key: Optional[str] = None
        span: Optional[ToolTraceSpan] = None
        result: dict[str, Any] | CallToolResult
        correlation_id = f"mcp-{secrets.token_hex(16)}"
        transport_runtime["last_correlation_id"] = correlation_id
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
                descriptors = await anyio.to_thread.run_sync(
                    proxy.descriptors,
                    limiter=_CONTROL_PLANE_THREAD_LIMITER,
                )
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
            # The span snapshot reads the durable session record from disk; keep
            # that I/O off the event loop that also answers health probes.
            read_only_call = bool(descriptor.read_only)
            trace_key = idempotency_key
            trace_name = resolved_name

            def begin_span() -> Optional[ToolTraceSpan]:
                return start_trace(
                    trace_name,
                    arguments,
                    read_only=read_only_call,
                    idempotency_key=trace_key,
                )

            span = await anyio.to_thread.run_sync(
                begin_span,
                limiter=_RESULT_POSTPROCESS_THREAD_LIMITER,
            )
            execution_limiter = (
                _LONG_TOOL_EXECUTION_THREAD_LIMITER
                if resolved_name in _LONG_RUNNING_TOOL_NAMES
                else _TOOL_EXECUTION_THREAD_LIMITER
            )
            if approved_action_digest is None:
                invoke = lambda: proxy.execute(
                    resolved_name,
                    dict(arguments),
                    idempotency_key=idempotency_key,
                    deadline_seconds=deadline_seconds,
                )
            else:
                approved = getattr(proxy, "execute_approved", None)
                if not callable(approved):
                    raise HostedBridgeError(
                        "protocol-level approval is unavailable for this tool runtime"
                    )
                invoke = lambda: approved(
                    resolved_name,
                    dict(arguments),
                    expected_action_digest=approved_action_digest,
                    idempotency_key=idempotency_key,
                    deadline_seconds=deadline_seconds,
                )
            result = await anyio.to_thread.run_sync(
                invoke,
                limiter=execution_limiter,
            )
            result = await anyio.to_thread.run_sync(
                compact_result,
                resolved_name,
                result,
                limiter=_RESULT_POSTPROCESS_THREAD_LIMITER,
            )
            # Record client evidence when an external client (e.g. ChatGPT)
            # calls karox.runtime.status. This is the only tool call that
            # counts toward fully_verified: local self-tests do initialize +
            # tools/list only, never call_tool, so this hook is never
            # triggered by _verify_bridge_endpoint.
            _maybe_record_client_evidence(resolved_name, success=True)
            await finish_trace_off_loop(span, result)
            return result
        except HostedApprovalRequired:
            # Modern MCP turns this typed stop into an input_required round.
            # Do not collapse it into a generic denied result here, because the
            # transport layer must keep the exact action digest out of model
            # arguments while letting the client obtain real user input.
            raise
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
            if code == "invalid_request":
                detail = bridge_error_detail(exc)
            elif code == "denied":
                detail = bridge_denied_detail(exc)
            elif code == "verification_not_approved":
                # Names the refused argv and the approved set, so the next call
                # picks a command that runs instead of guessing variants.
                detail = caller_safe_detail(exc)
            else:
                detail = None
            result = bridge_error_result(code, detail=detail)
            if span is None:
                span = start_trace(
                    resolved_name,
                    arguments,
                    read_only=bool(getattr(descriptor, "read_only", True)),
                    idempotency_key=idempotency_key,
                )
            finish_trace(span, result, forced_error_code=code)
            _LOGGER.warning(
                "MCP tool invocation failed correlation_id=%s failure_class=%s",
                correlation_id,
                type(exc).__name__,
            )
            return result

    @server.call_tool()
    async def call_tool(
        name: str, arguments: dict[str, object]
    ) -> tuple[list[TextContent], dict[str, Any]] | CallToolResult:
        meta = server.request_context.meta
        extra = getattr(meta, "model_extra", None) if meta is not None else None
        try:
            return utf8_call_result(
                await execute_tool(name, arguments, meta_extra=extra)
            )
        except HostedApprovalRequired:
            # The native approval UX is carried by the stateless 2026-07-28
            # input_required wire below. Older SDK/session clients must fail
            # closed rather than receiving or inventing an approval token, and
            # no page URL is offered: a link the model can hand out is not proof
            # the owner answered. The supported channels are named so an agent
            # knows what to ask the owner for.
            return bridge_error_result(
                "denied",
                detail=(
                    "this action needs an exact user approval: the owner approves it "
                    "in the chat and the same tool call is retried, or the client "
                    "carries an MCP 2026-07-28 user approval round"
                ),
            )

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
                # MCP 2026-07-28 makes discovery/list results explicitly cacheable.
                # The bridge catalog is stable for a running child, so a short
                # private TTL reduces redundant remote discovery without hiding
                # profile/source changes for more than one minute.
                "ttlMs": 60_000,
                "cacheScope": "private",
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
                "ttlMs": 60_000,
                "cacheScope": "private",
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

            input_responses = params.get("inputResponses")
            request_state = params.get("requestState")
            continuation = input_responses is not None or request_state is not None
            call_result: dict[str, Any] | CallToolResult
            if continuation:
                if not isinstance(input_responses, dict) or not isinstance(request_state, str):
                    await JSONResponse(
                        modern_error(request_id, -32602, "Invalid approval continuation"),
                        status_code=400,
                    )(scope, receive, send)
                    return
                try:
                    approval_state = _open_approval_state(
                        request_state,
                        wire_name=name,
                        arguments=arguments,
                    )
                except ValueError:
                    await JSONResponse(
                        modern_error(request_id, -32602, "Invalid or expired approval state"),
                        status_code=400,
                    )(scope, receive, send)
                    return
                try:
                    _consume_approval_state(request_state, approval_state)
                except ValueError:
                    await JSONResponse(
                        modern_error(request_id, -32602, "Approval state was already used"),
                        status_code=400,
                    )(scope, receive, send)
                    return
                response = input_responses.get("karox_approval")
                action = response.get("action") if isinstance(response, dict) else None
                content = response.get("content") if isinstance(response, dict) else None
                approved = (
                    action == "accept"
                    and isinstance(content, dict)
                    and content.get("approve") is True
                )
                if not approved:
                    call_result = CallToolResult(
                        content=[
                            TextContent(
                                type="text",
                                text="approval_declined: user did not approve this action",
                            )
                        ],
                        structuredContent={
                            "ok": False,
                            "error_code": "approval_declined",
                            "error": "the user did not approve this action",
                        },
                        isError=True,
                    )
                else:
                    call_result = await execute_tool(
                        name,
                        arguments,
                        meta_extra=meta_extra,
                        approved_action_digest=str(approval_state["action_digest"]),
                    )
            else:
                try:
                    call_result = await execute_tool(name, arguments, meta_extra=meta_extra)
                except HostedApprovalRequired as approval:
                    capabilities = meta_extra.get(
                        "io.modelcontextprotocol/clientCapabilities", {}
                    )
                    elicitation = (
                        capabilities.get("elicitation")
                        if isinstance(capabilities, dict)
                        else None
                    )
                    if (
                        not isinstance(elicitation, dict)
                        and approval.consequence != "external"
                    ):
                        # Non-external Smart Stops are not allowed to hijack the
                        # whole agent turn just because this hosted client lacks
                        # protocol elicitation. Defer this one action, let the
                        # agent finish every independent step, and surface the
                        # unresolved gate at the end. Bypass mode is the explicit
                        # opt-in that removes repository-deletion gates.
                        call_result = CallToolResult(
                            content=[
                                TextContent(
                                    type="text",
                                    text=(
                                        "approval_deferred: skip this blocked action for now, "
                                        "continue independent work, and ask the user only when "
                                        "the remaining work depends on this approval"
                                    ),
                                )
                            ],
                            structuredContent={
                                "ok": False,
                                "error_code": "approval_deferred",
                                "error": approval.message,
                                "action_kind": approval.action_kind,
                                "risk": approval.risk,
                                "consequence": approval.consequence,
                                "preview": dict(approval.preview),
                                "continue_independent_work": True,
                                "defer_until_blocked": True,
                            },
                            isError=True,
                        )
                    elif not isinstance(elicitation, dict):
                        # Hosted clients without protocol elicitation still need
                        # an actionable way to relay an explicit chat approval.
                        # Git push already has a narrow, one-shot compatibility
                        # path in CoreToolBridge; expose the exact checkpoint
                        # payload as a normal structured result so clients do not
                        # lose it behind a transport-level MCP error. Other
                        # external actions remain denied unless the protocol can
                        # carry a trusted approval round.
                        if approval.action_kind == "git.push":
                            call_result = chat_push_approval_handoff_result(
                                approval,
                                wire_name=name,
                                arguments=arguments,
                            )
                        else:
                            call_result = bridge_error_result(
                                "denied",
                                detail=(
                                    "this external action needs an exact user approval "
                                    "through an MCP elicitation-capable client"
                                ),
                            )
                    else:
                        input_required = {
                            "resultType": "input_required",
                            "inputRequests": {
                                "karox_approval": {
                                    "method": "elicitation/create",
                                    "params": {
                                        "mode": "form",
                                        "message": approval.message,
                                        "requestedSchema": {
                                            "type": "object",
                                            "properties": {
                                                "approve": {
                                                    "type": "boolean",
                                                    "title": "Approve this one action",
                                                    "default": False,
                                                }
                                            },
                                            "required": ["approve"],
                                            "additionalProperties": False,
                                        },
                                    },
                                }
                            },
                            "requestState": _seal_approval_state(
                                wire_name=name,
                                arguments=arguments,
                                approval=approval,
                            ),
                            "_meta": modern_result_meta(),
                        }
                        await JSONResponse(
                            {
                                "jsonrpc": "2.0",
                                "id": request_id,
                                "result": input_required,
                            }
                        )(scope, receive, send)
                        return
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
                    await anyio.to_thread.run_sync(
                        bearer_authorizer,
                        supplied,
                        limiter=_CONTROL_PLANE_THREAD_LIMITER,
                    )
                )
            except Exception:
                return False
        try:
            expected = await anyio.to_thread.run_sync(
                resolve_token,
                limiter=_CONTROL_PLANE_THREAD_LIMITER,
            )
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
                            try:
                                tools = await exposed_tools()
                                if not tools:
                                    raise RuntimeError("empty tool catalog")
                                if diagnostics_payload is not None:
                                    metrics_before_canary = dict(transport_runtime)
                                    try:
                                        canary = await execute_tool(_DIAGNOSTICS_TOOL_NAME, {})
                                    finally:
                                        # The startup canary is reported through
                                        # lifecycle.canary_result, not counted as
                                        # an external client invocation.
                                        transport_runtime.clear()
                                        transport_runtime.update(metrics_before_canary)
                                    if bool(getattr(canary, "isError", False)):
                                        raise RuntimeError("diagnostics canary returned an error")
                                lifecycle_runtime.update(
                                    {
                                        "state": "READY",
                                        "ready": True,
                                        "ready_at": time.time(),
                                        "canary_result": "PASS",
                                        "canary_failure_class": None,
                                    }
                                )
                                await send({"type": "lifespan.startup.complete"})
                            except Exception as exc:
                                lifecycle_runtime.update(
                                    {
                                        "state": "DEGRADED",
                                        "ready": False,
                                        "canary_result": "FAIL",
                                        "canary_failure_class": type(exc).__name__,
                                    }
                                )
                                _LOGGER.error(
                                    "MCP startup canary failed failure_class=%s",
                                    type(exc).__name__,
                                )
                                await send(
                                    {
                                        "type": "lifespan.startup.failed",
                                        "message": "KaroX MCP startup canary failed",
                                    }
                                )
                                return
                        elif message["type"] == "lifespan.shutdown":
                            lifecycle_runtime.update({"state": "STOPPED", "ready": False})
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
        request_path = scope.get("path")
        rejection = rebinding_rejection(scope, allowed)
        if rejection is not None:
            await rejection(scope, receive, send)
            return
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
            if not lifecycle_runtime["ready"]:
                await Response(
                    "bridge not ready",
                    status_code=503,
                    headers={"Retry-After": "1"},
                )(scope, tracked_receive, tracked_send)
                return
            if not await authorized(scope):
                transport_runtime["unauthorized_requests"] += 1
                await Response(
                    "unauthorized",
                    status_code=401,
                    headers=dict(unauthorized_headers or {}),
                )(scope, tracked_receive, tracked_send)
                return
            transport_runtime["authorized_requests"] += 1
            protocol_version = scope_headers(scope).get("mcp-protocol-version")
            transport_runtime["last_mcp_protocol_version"] = (
                protocol_version[:64] if isinstance(protocol_version, str) else None
            )
            if protocol_version == MODERN_MCP_PROTOCOL_VERSION:
                transport_runtime["last_mcp_dispatch_path"] = "direct_json"
                await modern_mcp_request(scope, tracked_receive, tracked_send)
                return
            transport_runtime["last_mcp_dispatch_path"] = "sdk_streamable_http"
            await manager.handle_request(scope, tracked_receive, tracked_send)
        finally:
            if not activity_finished:
                activity_finished = True
                _transport_activity_finished(completed=False)

    return app