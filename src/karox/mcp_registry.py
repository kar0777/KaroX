"""MCP server registry, digests and selection validation."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any, Iterable, Mapping
from .mcp_types import (
    _canonical_digest,  # noqa: F401
    REGISTRY_VERSION as REGISTRY_VERSION,
    _SAFE_ID as _SAFE_ID,
    _SAFE_TOOL as _SAFE_TOOL,
    _SAFE_ENV as _SAFE_ENV,
    _SAFE_HEADER as _SAFE_HEADER,
    _SAFE_SCHEME as _SAFE_SCHEME,
    _SECRET_NAME as _SECRET_NAME,
    _MCP_REFERENCE_PREFIX as _MCP_REFERENCE_PREFIX,
    _MCP_SERVICE as _MCP_SERVICE,
    _JSON_SERIALIZATION_ERRORS as _JSON_SERIALIZATION_ERRORS,
    _SCHEMA_TYPES as _SCHEMA_TYPES,
    McpFailureKind as McpFailureKind,
    McpError as McpError,
    McpConfigurationError as McpConfigurationError,
    McpAccessDenied as McpAccessDenied,
    McpProtocolError as McpProtocolError,
    McpTransportError as McpTransportError,
    McpTimeoutError as McpTimeoutError,
    McpRemoteToolError as McpRemoteToolError,
    McpUnknownOutcome as McpUnknownOutcome,
    McpCredentialReference as McpCredentialReference,
    McpProgress as McpProgress,
    McpCredentialStore as McpCredentialStore,
    _safe_identifier as _safe_identifier,
    _finite_number as _finite_number,
    _string_mapping as _string_mapping,
    _validate_url as _validate_url,
    _reject_json_constant as _reject_json_constant,
    _strict_json_clone as _strict_json_clone,
    _validate_schema as _validate_schema,
    _reject_persisted_credential as _reject_persisted_credential,
    McpServerRecord as McpServerRecord,
    McpToolDescriptor as McpToolDescriptor,
    _descriptor as _descriptor,
    _resolved_read_only_remote_names as _resolved_read_only_remote_names,
    _normalize_discovered_tools as _normalize_discovered_tools,
)



def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass

class McpRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def _load(self) -> dict[str, McpServerRecord]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(
                self.path.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise McpConfigurationError(f"cannot read MCP registry: {exc}") from exc
        if not isinstance(value, dict) or set(value) != {"schema_version", "servers"}:
            raise McpConfigurationError("MCP registry has invalid top-level fields")
        if value.get("schema_version") != REGISTRY_VERSION or not isinstance(value.get("servers"), list):
            raise McpConfigurationError("MCP registry has an unsupported schema")
        try:
            records = [McpServerRecord.from_dict(item) for item in value["servers"]]
            return self._validated(records)
        except (TypeError, ValueError) as exc:
            raise McpConfigurationError(f"MCP registry is invalid: {exc}") from exc

    @staticmethod
    def _validated(records: Iterable[McpServerRecord]) -> dict[str, McpServerRecord]:
        items = list(records)
        by_id = {item.server_id: item for item in items}
        if len(by_id) != len(items):
            raise McpConfigurationError("MCP registry contains duplicate server IDs")
        namespaces = {item.namespace for item in items}
        if len(namespaces) != len(items):
            raise McpConfigurationError("MCP registry contains duplicate namespaces")
        return by_id

    def _save(self, records: Iterable[McpServerRecord]) -> None:
        by_id = self._validated(records)
        _atomic_json(
            self.path,
            {
                "schema_version": REGISTRY_VERSION,
                "servers": [
                    item.to_dict()
                    for item in sorted(by_id.values(), key=lambda record: record.server_id)
                ],
            },
        )

    def list(self) -> list[McpServerRecord]:
        return sorted(self._load().values(), key=lambda item: item.server_id)

    def get(self, server_id: str) -> McpServerRecord:
        try:
            return self._load()[server_id]
        except KeyError as exc:
            raise McpConfigurationError(f"MCP server does not exist: {server_id}") from exc

    def put(self, record: McpServerRecord) -> McpServerRecord:
        records = self._load()
        for current in records.values():
            if current.namespace == record.namespace and current.server_id != record.server_id:
                raise McpConfigurationError(
                    f"MCP namespace is already used by {current.server_id}: {record.namespace}"
                )
        records[record.server_id] = record
        self._save(records.values())
        return record

    def remove(self, server_id: str) -> McpServerRecord:
        records = self._load()
        try:
            removed = records.pop(server_id)
        except KeyError as exc:
            raise McpConfigurationError(f"MCP server does not exist: {server_id}") from exc
        self._save(records.values())
        return removed


def mcp_registry_digest(server: McpServerRecord) -> str:
    """Bind an approval to the exact non-secret server configuration."""
    return _canonical_digest(server.to_dict())

def validate_mcp_selection_registry(
    server: McpServerRecord, selection: Mapping[str, Any]
) -> None:
    """Reject stale or malformed selections before contacting a server."""
    if (
        selection.get("server_id") != server.server_id
        or selection.get("namespace") != server.namespace
    ):
        raise McpAccessDenied("MCP server identity changed; reselect the server")
    if selection.get("registry_digest") != mcp_registry_digest(server):
        raise McpAccessDenied("MCP server configuration changed; reselect the server")