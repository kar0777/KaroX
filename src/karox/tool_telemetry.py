"""Content-free, best-effort telemetry for hosted MCP tool calls.

The hosted bridge needs measurements before it can prove that high-level tools
reduce calls and context.  This module deliberately records *metadata only*:
arguments, file contents, browser form values, user messages and credentials are
never accepted by the persistence API.

Telemetry is an observability aid, not part of the command transaction.  Every
write is best-effort and callers are expected to swallow telemetry failures.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from mcp.types import CallToolResult, ImageContent

from .paths import runtime_dir
from .security import redact

TOOL_TRACE_SCHEMA_VERSION = 1
_DEFAULT_LIMIT = 10_000
_MAX_IDENTIFIER = 256


def _utc_timestamp(value: float) -> str:
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat()


def _bounded_identifier(value: Any, *, default: str = "unknown") -> str:
    if not isinstance(value, str):
        return default
    cleaned = str(redact(value)).strip()
    if not cleaned:
        return default
    return cleaned[:_MAX_IDENTIFIER]


def _stable_identifier(prefix: str, value: Any) -> str:
    if value is None:
        return f"{prefix}-unknown"
    payload = str(value).encode("utf-8", errors="replace")
    return f"{prefix}-{hashlib.sha256(payload).hexdigest()[:20]}"


def json_size(value: Any) -> int:
    """Return a deterministic JSON byte count without retaining the payload."""
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except Exception:
        payload = repr(type(value))
    return len(payload.encode("utf-8", errors="replace"))


def _structured_content(result: Any) -> Mapping[str, Any]:
    if isinstance(result, Mapping):
        return result
    if isinstance(result, CallToolResult):
        value = result.structuredContent
        if isinstance(value, Mapping):
            return value
    return {}


def _find_scalar(mapping: Mapping[str, Any], key: str) -> Any:
    if key in mapping:
        return mapping[key]
    data = mapping.get("data")
    if isinstance(data, Mapping) and key in data:
        return data[key]
    return None


def result_metadata(result: Any) -> dict[str, Any]:
    """Extract size/mode/status facts without returning any result content."""
    structured = _structured_content(result)
    is_error = bool(getattr(result, "isError", False))
    explicit_ok = _find_scalar(structured, "ok")
    success = not is_error if not isinstance(explicit_ok, bool) else explicit_ok
    error_code = _find_scalar(structured, "error_code")
    artifact_id = _find_scalar(structured, "artifact_id")
    artifact_size = _find_scalar(structured, "size")
    if not isinstance(artifact_size, int) or artifact_size < 0:
        artifact_size = 0
    has_image = False
    if isinstance(result, CallToolResult):
        has_image = any(isinstance(item, ImageContent) for item in result.content)
    if not success:
        mode = "error"
    elif has_image:
        mode = "image"
    elif isinstance(artifact_id, str) and artifact_id:
        mode = "artifact"
    else:
        mode = "inline"
    output_size = json_size(result.model_dump(mode="json") if isinstance(result, CallToolResult) else result)
    inline_size = max(0, output_size - artifact_size)
    return {
        "success": bool(success),
        "error_code": _bounded_identifier(error_code, default="") if error_code else None,
        "output_size_bytes": output_size,
        "inline_output_bytes": inline_size,
        "artifact_output_bytes": artifact_size,
        "result_mode": mode,
        "cache_hit": bool(_find_scalar(structured, "cache_hit")),
        "idempotent_replay": bool(_find_scalar(structured, "idempotent_replay")),
    }


def safety_tier_for_tool(tool_name: str, *, read_only: bool) -> int:
    """Return the explicit hosted safety tier for a public tool call."""
    if read_only:
        return 0
    if tool_name in {
        "karox.browser.close",
        "karox.browser.request_user_takeover",
        "karox.browser.resume_after_user_takeover",
    }:
        return 3
    if tool_name in {"karox.task.execute_plan"}:
        return 2
    return 1


@dataclass(frozen=True)
class ToolTraceContext:
    session_id: str
    task_id: str
    connection_id: str
    client_kind: str
    permission_profile: str

    @classmethod
    def from_runtime(
        cls,
        runtime: Any,
        diagnostics: Optional[Mapping[str, Any]] = None,
    ) -> Optional["ToolTraceContext"]:
        try:
            info_reader = getattr(runtime, "session_info", None)
            info = dict(info_reader()) if callable(info_reader) else {}
        except Exception:
            info = {}
        session_id = info.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return None
        diagnostics = diagnostics or {}
        task = info.get("task", "")
        profile = diagnostics.get("saved_profile") or diagnostics.get("connection_id")
        return cls(
            session_id=_bounded_identifier(session_id),
            task_id=_stable_identifier("task", task or session_id),
            connection_id=_bounded_identifier(profile, default=_stable_identifier("connection", session_id)),
            client_kind=_bounded_identifier(diagnostics.get("target_profile"), default="hosted-mcp"),
            permission_profile=_bounded_identifier(info.get("access_profile"), default="unknown"),
        )


@dataclass(frozen=True)
class ToolTraceEvent:
    trace_id: str
    task_id: str
    operation_id: str
    session_id: str
    connection_id: str
    client_kind: str
    tool_name: str
    tool_schema_version: int
    started_at: str
    duration_ms: float
    success: bool
    error_code: Optional[str]
    input_size_bytes: int
    output_size_bytes: int
    inline_output_bytes: int
    artifact_output_bytes: int
    result_mode: str
    cache_hit: bool
    idempotent_replay: bool
    permission_tier: int
    permission_profile: str
    user_gate_required: bool
    repository_revision_before: Optional[int]
    repository_revision_after: Optional[int]
    schema_version: int = TOOL_TRACE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ToolTraceStore:
    """SQLite-backed, bounded, session-scoped tool trace store."""

    def __init__(
        self,
        session_id: str,
        *,
        root: Optional[Path] = None,
        limit: int = _DEFAULT_LIMIT,
    ) -> None:
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("tool trace store requires a session id")
        if not isinstance(limit, int) or limit < 100 or limit > 1_000_000:
            raise ValueError("tool trace limit must be between 100 and 1000000")
        base = root or (runtime_dir() / "vnext" / "tool-telemetry")
        self.root = Path(base).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        safe_session = _stable_identifier("session", session_id)
        self.path = self.root / f"{safe_session}.sqlite3"
        self.limit = limit
        self._lock = threading.RLock()
        self._connection: Optional[sqlite3.Connection] = None
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = self._connection
        if connection is None:
            connection = sqlite3.connect(
                self.path,
                timeout=2.0,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            self._connection = connection
        return connection

    def close(self) -> None:
        """Close the reusable SQLite handle; a later access may reopen it."""
        with self._lock:
            connection = self._connection
            self._connection = None
            if connection is not None:
                connection.close()

    def _initialize(self) -> None:
        with self._lock:
            connection = self._connect()
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_trace (
                    trace_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_tool_trace_started ON tool_trace(started_at)"
            )
            connection.commit()

    def record(self, event: ToolTraceEvent) -> None:
        if not isinstance(event, ToolTraceEvent):
            raise ValueError("tool trace event has the wrong type")
        payload = json.dumps(event.to_dict(), ensure_ascii=False, sort_keys=True)
        with self._lock:
            connection = self._connect()
            connection.execute(
                "INSERT OR REPLACE INTO tool_trace(trace_id, started_at, payload) VALUES (?, ?, ?)",
                (event.trace_id, event.started_at, payload),
            )
            connection.execute(
                """
                DELETE FROM tool_trace
                WHERE trace_id IN (
                    SELECT trace_id FROM tool_trace
                    ORDER BY started_at DESC
                    LIMIT -1 OFFSET ?
                )
                """,
                (self.limit,),
            )
            connection.commit()

    def list(self, *, limit: int = 1000) -> tuple[dict[str, Any], ...]:
        bounded = max(1, min(int(limit), self.limit))
        with self._lock:
            connection = self._connect()
            rows = connection.execute(
                "SELECT payload FROM tool_trace ORDER BY started_at DESC LIMIT ?",
                (bounded,),
            ).fetchall()
        return tuple(json.loads(str(row["payload"])) for row in rows)

    def aggregate(self) -> dict[str, Any]:
        rows = self.list(limit=self.limit)
        if not rows:
            return {
                "tool_calls": 0,
                "repeated_tool_calls": 0,
                "input_argument_bytes": 0,
                "inline_result_bytes": 0,
                "artifact_bytes": 0,
                "wall_clock_ms": 0.0,
                "validation_failures": 0,
                "permission_failures": 0,
                "retries": 0,
                "cache_hits": 0,
                "cache_misses": 0,
                "task_success": None,
            }
        signatures: dict[tuple[str, str], int] = {}
        for row in rows:
            key = (str(row.get("tool_name")), str(row.get("operation_id")))
            signatures[key] = signatures.get(key, 0) + 1
        failures = [row for row in rows if not bool(row.get("success"))]
        return {
            "tool_calls": len(rows),
            "repeated_tool_calls": sum(max(0, count - 1) for count in signatures.values()),
            "input_argument_bytes": sum(int(row.get("input_size_bytes", 0)) for row in rows),
            "inline_result_bytes": sum(int(row.get("inline_output_bytes", 0)) for row in rows),
            "artifact_bytes": sum(int(row.get("artifact_output_bytes", 0)) for row in rows),
            "wall_clock_ms": round(sum(float(row.get("duration_ms", 0.0)) for row in rows), 3),
            "validation_failures": sum(row.get("error_code") == "invalid_request" for row in failures),
            "permission_failures": sum(row.get("error_code") == "denied" for row in failures),
            "retries": sum(bool(row.get("idempotent_replay")) for row in rows),
            "cache_hits": sum(bool(row.get("cache_hit")) for row in rows),
            "cache_misses": sum(not bool(row.get("cache_hit")) for row in rows),
            "task_success": all(bool(row.get("success")) for row in rows),
        }


class ToolTraceSpan:
    """In-memory span that commits exactly one content-free event."""

    def __init__(
        self,
        *,
        store: ToolTraceStore,
        context: ToolTraceContext,
        tool_name: str,
        arguments: Mapping[str, Any],
        read_only: bool,
        idempotency_key: Optional[str],
        repository_revision_before: Optional[int],
        clock: Any = time.perf_counter,
        wall_clock: Any = time.time,
    ) -> None:
        self.store = store
        self.context = context
        self.tool_name = _bounded_identifier(tool_name)
        self.read_only = bool(read_only)
        self.input_size_bytes = json_size(arguments)
        self.permission_tier = safety_tier_for_tool(tool_name, read_only=read_only)
        self.user_gate_required = self.permission_tier == 3
        self.operation_id = _stable_identifier(
            "operation", idempotency_key or f"{tool_name}\0{json_size(arguments)}"
        )
        self.repository_revision_before = repository_revision_before
        self._clock = clock
        self._wall_clock = wall_clock
        self._started_monotonic = float(clock())
        self._started_wall = float(wall_clock())
        self.trace_id = f"trace-{uuid.uuid4().hex}"
        self._finished = False

    def finish(
        self,
        result: Any,
        *,
        repository_revision_after: Optional[int],
        forced_error_code: Optional[str] = None,
    ) -> None:
        if self._finished:
            return
        self._finished = True
        metadata = result_metadata(result)
        if forced_error_code:
            metadata["success"] = False
            metadata["error_code"] = _bounded_identifier(forced_error_code)
            metadata["result_mode"] = "error"
        event = ToolTraceEvent(
            trace_id=self.trace_id,
            task_id=self.context.task_id,
            operation_id=self.operation_id,
            session_id=self.context.session_id,
            connection_id=self.context.connection_id,
            client_kind=self.context.client_kind,
            tool_name=self.tool_name,
            tool_schema_version=1,
            started_at=_utc_timestamp(self._started_wall),
            duration_ms=round(max(0.0, (float(self._clock()) - self._started_monotonic) * 1000), 3),
            success=bool(metadata["success"]),
            error_code=metadata["error_code"],
            input_size_bytes=self.input_size_bytes,
            output_size_bytes=int(metadata["output_size_bytes"]),
            inline_output_bytes=int(metadata["inline_output_bytes"]),
            artifact_output_bytes=int(metadata["artifact_output_bytes"]),
            result_mode=str(metadata["result_mode"]),
            cache_hit=bool(metadata["cache_hit"]),
            idempotent_replay=bool(metadata["idempotent_replay"]),
            permission_tier=self.permission_tier,
            permission_profile=self.context.permission_profile,
            user_gate_required=self.user_gate_required,
            repository_revision_before=self.repository_revision_before,
            repository_revision_after=repository_revision_after,
        )
        self.store.record(event)


def default_trace_store(context: Optional[ToolTraceContext]) -> Optional[ToolTraceStore]:
    if context is None or os.environ.get("KAROX_TOOL_TELEMETRY", "1") == "0":
        return None
    try:
        return ToolTraceStore(context.session_id)
    except Exception:
        return None
