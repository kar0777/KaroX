"""Secret-free coordination for zero-downtime saved-bridge child handoff.

The MCP child cannot replace itself without briefly dropping the local listener.
For durable Tailscale bridges the owner can instead warm a replacement child on a
second loopback port, retarget the already-owned Funnel route, and only then let
the old child exit. This module is deliberately tiny and dependency-light so the
child and owner can coordinate through one atomic JSON request without importing
each other's runtime stacks.
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from .paths import runtime_dir

_SCHEMA_VERSION = 1
_SAFE_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_ACTIVE_STATES = frozenset({"requested", "route_switched"})
_TERMINAL_STATES = frozenset({"completed", "failed"})


class BridgeHandoffError(RuntimeError):
    """A rolling handoff request could not be safely coordinated."""


def rolling_restart_request_path(session_id: str) -> Path:
    if not isinstance(session_id, str) or not session_id or "\x00" in session_id:
        raise BridgeHandoffError("rolling restart requires a valid session id")
    return runtime_dir() / "web-bridge" / f"{session_id}.rolling-restart.json"


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(dict(value), ensure_ascii=False, sort_keys=True).encode("utf-8")
    temporary = path.with_name(f"{path.name}.{os.getpid()}.{time.time_ns()}.tmp")
    with temporary.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def request_rolling_restart(
    *,
    session_id: str,
    owner_pid: int,
    bridge_pid: int,
    request_key_sha256: str,
) -> dict[str, Any]:
    """Persist one addressed rolling-restart request.

    Only hashes and process/session identity are persisted. A second distinct
    request cannot overwrite an active handoff; terminal records may be replaced
    by the next request.
    """

    if not isinstance(owner_pid, int) or owner_pid <= 0:
        raise BridgeHandoffError("rolling restart owner pid is invalid")
    if not isinstance(bridge_pid, int) or bridge_pid <= 0:
        raise BridgeHandoffError("rolling restart bridge pid is invalid")
    if not isinstance(request_key_sha256, str) or _SAFE_DIGEST.fullmatch(request_key_sha256) is None:
        raise BridgeHandoffError("rolling restart request digest is invalid")

    path = rolling_restart_request_path(session_id)
    existing = _read(path)
    if existing:
        same = existing.get("request_key_sha256") == request_key_sha256
        state = existing.get("status")
        if same and state in _ACTIVE_STATES | _TERMINAL_STATES:
            return existing
        if not same and state in _ACTIVE_STATES:
            raise BridgeHandoffError("another rolling restart is already active")

    value = {
        "schema_version": _SCHEMA_VERSION,
        "session_id": session_id,
        "owner_pid": owner_pid,
        "bridge_pid": bridge_pid,
        "request_key_sha256": request_key_sha256,
        "requested_at": time.time(),
        "updated_at": time.time(),
        "status": "requested",
    }
    _write(path, value)
    return value


def read_rolling_restart_request(
    session_id: str,
    *,
    owner_pid: Optional[int] = None,
    bridge_pid: Optional[int] = None,
    request_key_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """Read one request and optionally require exact identity fields."""

    value = _read(rolling_restart_request_path(session_id))
    if not value or value.get("schema_version") != _SCHEMA_VERSION:
        return {}
    if value.get("session_id") != session_id:
        return {}
    if owner_pid is not None and value.get("owner_pid") != owner_pid:
        return {}
    if bridge_pid is not None and value.get("bridge_pid") != bridge_pid:
        return {}
    if request_key_sha256 is not None and value.get("request_key_sha256") != request_key_sha256:
        return {}
    return value


def update_rolling_restart_request(
    session_id: str,
    request_key_sha256: str,
    status: str,
    *,
    new_bridge_pid: Optional[int] = None,
    new_port: Optional[int] = None,
    failure_class: Optional[str] = None,
) -> dict[str, Any]:
    """Advance one request without ever widening its identity."""

    if status not in {"requested", "route_switched", "completed", "failed"}:
        raise BridgeHandoffError("rolling restart status is invalid")
    value = read_rolling_restart_request(
        session_id,
        request_key_sha256=request_key_sha256,
    )
    if not value:
        raise BridgeHandoffError("rolling restart request is missing or changed")
    value["status"] = status
    value["updated_at"] = time.time()
    if isinstance(new_bridge_pid, int) and new_bridge_pid > 0:
        value["new_bridge_pid"] = new_bridge_pid
    if isinstance(new_port, int) and 0 < new_port <= 65535:
        value["new_port"] = new_port
    if failure_class:
        value["failure_class"] = str(failure_class)[:80]
    _write(rolling_restart_request_path(session_id), value)
    return value


def clear_rolling_restart_request(
    session_id: str,
    *,
    request_key_sha256: Optional[str] = None,
) -> bool:
    """Remove a request only if it is the expected generation."""

    path = rolling_restart_request_path(session_id)
    value = _read(path)
    if not value:
        return True
    if request_key_sha256 is not None and value.get("request_key_sha256") != request_key_sha256:
        return False
    try:
        path.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


__all__ = [
    "BridgeHandoffError",
    "clear_rolling_restart_request",
    "read_rolling_restart_request",
    "request_rolling_restart",
    "rolling_restart_request_path",
    "update_rolling_restart_request",
]
