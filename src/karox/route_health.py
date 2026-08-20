"""Lightweight public-route health tracking for managed web bridges.

The tunnel process can remain alive while its public ingress is temporarily
unreachable.  Process liveness alone therefore is not sufficient evidence that
a ChatGPT/Claude connector can still reach the local bridge.

This module deliberately does *not* own or restart tunnels.  It only provides a
small state machine and a cheap unauthenticated probe; ownership-safe recovery
stays in :mod:`karox.web_bridge_launcher`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional


DEFAULT_ROUTE_PROBE_INTERVAL_SECONDS = 5.0
DEFAULT_ROUTE_FAILURE_THRESHOLD = 3
DEFAULT_ROUTE_PROBE_TIMEOUT_SECONDS = 2.0


def public_mcp_route_healthy(
    public_url: str,
    *,
    path: str = "/mcp",
    timeout_seconds: float = DEFAULT_ROUTE_PROBE_TIMEOUT_SECONDS,
    get: Optional[Callable[..., Any]] = None,
) -> bool:
    """Return True when the public MCP ingress reaches KaroX authentication.

    KaroX's MCP endpoint intentionally rejects an unauthenticated ``GET /mcp``
    with HTTP 401.  Seeing that response proves DNS, TLS, Funnel/Proxy routing,
    and the local bridge listener are all reachable without exposing a bearer
    credential or creating an MCP session.
    """
    endpoint = f"{public_url.rstrip('/')}{path}"
    if get is None:
        import httpx

        get = httpx.get
    try:
        response = get(
            endpoint,
            timeout=float(timeout_seconds),
            follow_redirects=False,
        )
    except Exception:
        return False
    return int(getattr(response, "status_code", 0)) == 401


@dataclass
class RouteHealthTracker:
    """Debounce transient route failures before recovery is requested."""

    interval_seconds: float = DEFAULT_ROUTE_PROBE_INTERVAL_SECONDS
    failure_threshold: int = DEFAULT_ROUTE_FAILURE_THRESHOLD
    consecutive_failures: int = 0
    next_probe_at: float = 0.0
    recovery_count: int = 0

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("route probe interval must be positive")
        if self.failure_threshold < 1:
            raise ValueError("route failure threshold must be positive")

    def due(self, now: float) -> bool:
        return float(now) >= self.next_probe_at

    def observe(self, *, now: float, healthy: bool) -> bool:
        """Record one due probe; return True when recovery should be attempted."""
        self.next_probe_at = float(now) + self.interval_seconds
        if healthy:
            self.consecutive_failures = 0
            return False
        self.consecutive_failures += 1
        return self.consecutive_failures >= self.failure_threshold

    def recovered(self, *, now: float) -> None:
        self.consecutive_failures = 0
        self.recovery_count += 1
        self.next_probe_at = float(now) + self.interval_seconds


__all__ = [
    "DEFAULT_ROUTE_FAILURE_THRESHOLD",
    "DEFAULT_ROUTE_PROBE_INTERVAL_SECONDS",
    "DEFAULT_ROUTE_PROBE_TIMEOUT_SECONDS",
    "RouteHealthTracker",
    "public_mcp_route_healthy",
]
