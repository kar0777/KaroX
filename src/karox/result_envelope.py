"""Artifact-backed result envelopes for hosted MCP clients.

Large tool responses are expensive twice: they consume the transport budget and
then remain in the model context.  This module stores a redacted full response in
the existing session-scoped artifact store and returns a deterministic digest.
Small responses are returned unchanged, preserving the existing public tools.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Optional

from mcp.types import CallToolResult, ImageContent, TextContent

from .artifacts import ArtifactStore
from .security import redact

DEFAULT_INLINE_RESULT_BYTES = 64 * 1024
MIN_INLINE_RESULT_BYTES = 4 * 1024
MAX_INLINE_RESULT_BYTES = 1024 * 1024

# The retrieval tools are how a client *escapes* a spilled result. Spilling their
# own answer would hand back another artifact id instead of the bytes that were
# asked for, and following that pointer spills again -- an unbounded regress in
# which the content is never delivered and a fresh copy of the payload is written
# on every hop. Their output is already bounded by the caller's own
# ``max_output_bytes`` (4 KiB - 1 MiB), so it is returned inline unchanged.
NEVER_SPILLED_TOOLS = frozenset(
    {
        "karox.artifact.get",
        "karox.artifact.read_image",
    }
)


def configured_inline_result_bytes() -> int:
    raw = os.environ.get("KAROX_INLINE_RESULT_BYTES", "").strip()
    if not raw:
        return DEFAULT_INLINE_RESULT_BYTES
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_INLINE_RESULT_BYTES
    return max(MIN_INLINE_RESULT_BYTES, min(value, MAX_INLINE_RESULT_BYTES))


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def _safe_payload(result: Any) -> tuple[Any, bool]:
    if isinstance(result, CallToolResult):
        if any(isinstance(item, ImageContent) for item in result.content):
            return result, True
        payload = result.model_dump(mode="json")
        return redact(payload), bool(result.isError)
    return redact(result), False


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _scalar(mapping: Mapping[str, Any], key: str) -> Any:
    if key in mapping:
        return mapping[key]
    data = mapping.get("data")
    if isinstance(data, Mapping):
        return data.get(key)
    return None


def _summary(tool_name: str, payload: Any, *, is_error: bool) -> str:
    mapping = _mapping(payload)
    if is_error or _scalar(mapping, "ok") is False:
        code = _scalar(mapping, "error_code")
        return f"{tool_name} failed" + (f" ({code})" if isinstance(code, str) else "")
    for key in ("summary", "command", "path", "process_id"):
        value = _scalar(mapping, key)
        if isinstance(value, str) and value.strip():
            return f"{tool_name}: {value.strip()[:240]}"
    return f"{tool_name} completed; full result stored as an artifact"


def _important_findings(payload: Any) -> tuple[str, ...]:
    mapping = _mapping(payload)
    findings: list[str] = []
    safe_keys = (
        "match_count",
        "total_lines",
        "changed",
        "exit_code",
        "timed_out",
        "running",
        "truncated",
        "idempotent_replay",
    )
    for key in safe_keys:
        value = _scalar(mapping, key)
        if isinstance(value, (str, int, float, bool)) and not isinstance(value, bytes):
            findings.append(f"{key}={value}")
    return tuple(findings[:8])


def _available_sections(payload: Any) -> tuple[str, ...]:
    mapping = _mapping(payload)
    sections = [str(key) for key in mapping.keys() if isinstance(key, str)]
    data = mapping.get("data")
    if isinstance(data, Mapping):
        sections.extend(f"data.{key}" for key in data if isinstance(key, str))
    return tuple(dict.fromkeys(sections))[:64]


@dataclass(frozen=True)
class ResultEnvelope:
    summary: str
    important_findings: tuple[str, ...]
    diagnostics: dict[str, Any]
    truncated: bool
    total_size: int
    artifact_id: str
    available_sections: tuple[str, ...]
    content_hash: str
    expires_at: Optional[str]
    persistence_policy: str
    result_mode: str = "artifact"
    schema_version: int = 1

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["important_findings"] = list(self.important_findings)
        value["available_sections"] = list(self.available_sections)
        value["ok"] = not bool(self.diagnostics.get("is_error"))
        return value


def artifact_backed_result(
    result: dict[str, Any] | CallToolResult,
    *,
    tool_name: str,
    store: Optional[ArtifactStore],
    threshold_bytes: Optional[int] = None,
) -> dict[str, Any] | CallToolResult:
    """Return ``result`` unchanged unless its safe JSON representation is large."""
    if store is None:
        return result
    if tool_name in NEVER_SPILLED_TOOLS:
        # Retrieving an artifact must yield content, not another pointer.
        return result
    payload, is_error = _safe_payload(result)
    if isinstance(payload, CallToolResult):
        return result
    encoded = _json_bytes(payload)
    threshold = configured_inline_result_bytes() if threshold_bytes is None else int(threshold_bytes)
    threshold = max(MIN_INLINE_RESULT_BYTES, min(threshold, MAX_INLINE_RESULT_BYTES))
    if len(encoded) <= threshold:
        return result
    try:
        record = store.put(
            encoded,
            name=f"{tool_name.replace('.', '-')}-result.json",
            mime="application/json",
        )
    except Exception:
        return result
    envelope = ResultEnvelope(
        summary=_summary(tool_name, payload, is_error=is_error),
        important_findings=_important_findings(payload),
        diagnostics={
            "is_error": is_error,
            "inline_threshold_bytes": threshold,
            "original_type": "call_tool_result" if isinstance(result, CallToolResult) else "mapping",
        },
        truncated=True,
        total_size=len(encoded),
        artifact_id=record.artifact_id,
        available_sections=_available_sections(payload),
        content_hash=record.sha256,
        expires_at=record.expires_at,
        persistence_policy=record.persistence_policy,
    )
    compact = envelope.to_dict()
    text = json.dumps(compact, ensure_ascii=False, sort_keys=True)
    return CallToolResult(
        content=[TextContent(type="text", text=text)],
        structuredContent=compact,
        isError=is_error,
    )
