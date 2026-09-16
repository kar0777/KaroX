from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path

from karox.hosted_bridge import _hosted_bridge_health
from karox.route_health import (
    DEFAULT_ROUTE_FAILURE_PROBE_INTERVAL_SECONDS,
    DEFAULT_ROUTE_PROBE_INTERVAL_SECONDS,
    DEFAULT_ROUTE_FAILURE_THRESHOLD as default_route_failure_threshold,
)


def _write_watchdog(root: Path, *, failures: int, healthy, readiness: str = "READY") -> None:
    path = root / "web-bridge" / "watch.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    now = time.time()
    path.write_text(
        json.dumps(
            {
                "session_id": "session-health",
                "saved_profile": "chatgpt-dev",
                "owner_pid": 101,
                "bridge_pid": 202,
                "readiness_state": readiness,
                "tunnel": "tailscale",
                "public_route_healthy": healthy,
                "public_route_failures": failures,
                "public_route_checked_at": now - 0.5,
                "last_public_route_ok_at": now - 2.0,
                "local_bridge_healthy": True,
                "local_bridge_checked_at": now - 0.2,
                "tunnel_recoveries": 3,
                "last_tunnel_recovery_at": now - 4.0,
                "last_tunnel_recovery_error": None,
                "bridge_credential": "must-never-leak",
            }
        ),
        encoding="utf-8",
    )


def test_hosted_bridge_health_projects_only_safe_operational_state() -> None:
    old = dict(os.environ)
    with tempfile.TemporaryDirectory() as td:
        try:
            root = Path(td)
            os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(root)
            os.environ.pop("KAROX_RUNTIME_DIR", None)
            _write_watchdog(root, failures=0, healthy=True)

            result = _hosted_bridge_health("session-health")
            assert result["available"] is True
            assert result["state"] == "healthy"
            assert result["recommended_action"] == "none"
            assert result["owner_pid"] == 101
            assert result["bridge_pid"] == 202
            assert result["public_route_failures"] == 0
            assert result["probe_policy"]["healthy_interval_seconds"] == (
                DEFAULT_ROUTE_PROBE_INTERVAL_SECONDS
            )
            assert (
                result["probe_policy"]["failure_confirmation_interval_seconds"]
                == DEFAULT_ROUTE_FAILURE_PROBE_INTERVAL_SECONDS
            )
            assert result["probe_policy"]["failure_threshold"] == default_route_failure_threshold
            rendered = json.dumps(result)
            assert "must-never-leak" not in rendered
            assert "bridge_credential" not in rendered
        finally:
            os.environ.clear()
            os.environ.update(old)


def test_hosted_bridge_health_classifies_confirmation_and_recovery() -> None:
    old = dict(os.environ)
    with tempfile.TemporaryDirectory() as td:
        try:
            root = Path(td)
            os.environ["KAROX_VNEXT_RUNTIME_DIR"] = str(root)
            os.environ.pop("KAROX_RUNTIME_DIR", None)

            _write_watchdog(root, failures=1, healthy=False)
            confirming = _hosted_bridge_health("session-health")
            assert confirming["state"] == "confirming_public_failure"
            assert confirming["recommended_action"] == "wait_for_fast_confirmation"

            _write_watchdog(root, failures=2, healthy=False)
            recovery = _hosted_bridge_health("session-health")
            assert recovery["state"] == "public_recovery_due"
            assert recovery["recommended_action"] == "supervisor_recovery_in_progress"
        finally:
            os.environ.clear()
            os.environ.update(old)


def test_hosted_bridge_health_is_explicit_when_watchdog_is_missing() -> None:
    old = dict(os.environ)
    with tempfile.TemporaryDirectory() as td:
        try:
            os.environ["KAROX_VNEXT_RUNTIME_DIR"] = td
            os.environ.pop("KAROX_RUNTIME_DIR", None)
            result = _hosted_bridge_health("missing-session")
            assert result == {
                "available": False,
                "state": "watchdog_unavailable",
                "recommended_action": "continue_local_diagnostics",
            }
        finally:
            os.environ.clear()
            os.environ.update(old)
