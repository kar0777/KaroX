"""Hosted bridge health snapshot."""

from __future__ import annotations

import json
import time
from typing import Any, Optional
from .paths import runtime_dir
from .route_health import (
    DEFAULT_ROUTE_FAILURE_PROBE_INTERVAL_SECONDS,
    DEFAULT_ROUTE_FAILURE_THRESHOLD,
    DEFAULT_ROUTE_PROBE_INTERVAL_SECONDS,
)



def _hosted_bridge_health(session_id: str) -> dict[str, Any]:
    """Return safe public-edge diagnostics for this hosted bridge session."""
    root = runtime_dir() / "web-bridge"
    candidates: list[tuple[float, dict[str, Any]]] = []
    try:
        paths = tuple(root.glob("*.json"))
    except OSError:
        paths = ()
    for path in paths:
        if path.name.endswith(".last-exit.json"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("session_id") != session_id:
                continue
            candidates.append((path.stat().st_mtime, payload))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    if not candidates:
        return {
            "available": False,
            "state": "watchdog_unavailable",
            "recommended_action": "continue_local_diagnostics",
        }

    _mtime, record = max(candidates, key=lambda item: item[0])
    now = time.time()
    readiness = str(record.get("readiness_state") or "UNKNOWN")
    healthy = record.get("public_route_healthy")
    failures_raw = record.get("public_route_failures")
    failures = int(failures_raw) if isinstance(failures_raw, int) and failures_raw >= 0 else 0

    if readiness != "READY":
        state = "bridge_not_ready"
        action = "wait_for_bridge_ready"
    elif healthy is True and failures == 0:
        state = "healthy"
        action = "none"
    elif failures <= 1:
        state = "confirming_public_failure"
        action = "wait_for_fast_confirmation"
    else:
        state = "public_recovery_due"
        action = "supervisor_recovery_in_progress"

    def age(value: Any) -> Optional[float]:
        if isinstance(value, (int, float)):
            return round(max(0.0, now - float(value)), 3)
        return None

    return {
        "available": True,
        "state": state,
        "recommended_action": action,
        "saved_profile": record.get("saved_profile"),
        "owner_pid": record.get("owner_pid"),
        "bridge_pid": record.get("bridge_pid"),
        "readiness_state": readiness,
        "tunnel": record.get("tunnel"),
        "public_route_healthy": healthy,
        "public_route_failures": failures,
        "public_route_check_age_seconds": age(record.get("public_route_checked_at")),
        "last_public_route_ok_age_seconds": age(record.get("last_public_route_ok_at")),
        "local_bridge_healthy": record.get("local_bridge_healthy"),
        "local_bridge_state": record.get("local_bridge_state"),
        "local_bridge_busy_seconds": record.get("local_bridge_busy_seconds"),
        "last_child_recycle_reason": record.get("last_child_recycle_reason"),
        "child_recycle_history": [
            {
                "at": item.get("at"),
                "reason": item.get("reason"),
                "pid": item.get("pid"),
            }
            for item in list(record.get("child_recycle_history") or [])[-5:]
            if isinstance(item, dict)
        ],
        "local_bridge_check_age_seconds": age(record.get("local_bridge_checked_at")),
        "tunnel_recoveries": int(record.get("tunnel_recoveries") or 0),
        "last_tunnel_recovery_age_seconds": age(record.get("last_tunnel_recovery_at")),
        "last_tunnel_recovery_error": record.get("last_tunnel_recovery_error"),
        "probe_policy": {
            "healthy_interval_seconds": DEFAULT_ROUTE_PROBE_INTERVAL_SECONDS,
            "failure_confirmation_interval_seconds": DEFAULT_ROUTE_FAILURE_PROBE_INTERVAL_SECONDS,
            "failure_threshold": DEFAULT_ROUTE_FAILURE_THRESHOLD,
        },
    }
