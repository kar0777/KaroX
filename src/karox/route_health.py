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


# Hosted ChatGPT traffic treats a several-second public-route outage as a hard
# tool failure even while the local MCP child stays perfectly healthy. Probe
# frequently, then confirm a failed public route once on the fast cadence before
# mutating the owned Funnel. This keeps recovery sub-second after detection while
# a single transient POP/network hiccup cannot reconfigure an otherwise healthy
# route.
DEFAULT_ROUTE_PROBE_INTERVAL_SECONDS = 2.0
DEFAULT_ROUTE_FAILURE_PROBE_INTERVAL_SECONDS = 0.5
DEFAULT_ROUTE_FAILURE_THRESHOLD = 2
DEFAULT_ROUTE_PROBE_TIMEOUT_SECONDS = 1.5


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


_PROBE_FAILURE_EXCEPTION_CLASSES: dict[str, str] = {
    "TimeoutException": "timeout",
    "ConnectTimeout": "timeout",
    "ReadTimeout": "timeout",
    "WriteTimeout": "timeout",
    "PoolTimeout": "timeout",
    "TimeoutError": "timeout",
    "ConnectError": "connection_error",
    "ConnectionError": "connection_error",
    "TLSException": "tls_error",
    "SSLError": "tls_error",
    "RemoteProtocolError": "protocol_error",
    "InvalidURL": "probe_error",
    "UnsupportedProtocol": "probe_error",
}


def public_mcp_route_failure_class(
    public_url: str,
    *,
    path: str = "/mcp",
    timeout_seconds: float = DEFAULT_ROUTE_PROBE_TIMEOUT_SECONDS,
    get: Optional[Callable[..., Any]] = None,
) -> str:
    """Classify the last public-probe failure without exposing request details.

    The steady-state health loop only stores a boolean, but a repair decision
    needs more: a stuck DNS resolver, a TLS-level break, an idle timeout and an
    unexpected HTTP status all call for different next steps (and different
    human-facing diagnostics). The returned short class is redacted evidence:
    it names the failure stage, never a URL path, header or body.
    """
    if get is None:
        import httpx

        get = httpx.get
    endpoint = f"{public_url.rstrip('/')}{path}"
    try:
        response = get(endpoint, timeout=float(timeout_seconds), follow_redirects=False)
    except Exception as exc:
        name = type(exc).__name__
        mapped = _PROBE_FAILURE_EXCEPTION_CLASSES.get(name)
        if mapped:
            return mapped
        return f"probe_exception:{name[:40]}"
    code = int(getattr(response, "status_code", 0) or 0)
    if code == 401:
        return "ok"
    return f"http_{code}"


@dataclass
class RouteHealthTracker:
    """Debounce transient route failures before recovery is requested."""

    interval_seconds: float = DEFAULT_ROUTE_PROBE_INTERVAL_SECONDS
    failure_probe_interval_seconds: float = DEFAULT_ROUTE_FAILURE_PROBE_INTERVAL_SECONDS
    failure_threshold: int = DEFAULT_ROUTE_FAILURE_THRESHOLD
    consecutive_failures: int = 0
    next_probe_at: float = 0.0
    recovery_count: int = 0

    def __post_init__(self) -> None:
        if self.interval_seconds <= 0:
            raise ValueError("route probe interval must be positive")
        if self.failure_probe_interval_seconds <= 0:
            raise ValueError("route failure probe interval must be positive")
        if self.failure_probe_interval_seconds > self.interval_seconds:
            # Tiny intervals are used by deterministic supervisor tests and by
            # deliberately aggressive local health policies. The default fast
            # confirmation interval must never make such a valid healthy cadence
            # impossible to construct; clamp it to the caller's cheaper cadence
            # instead of turning configuration into a bridge-start failure.
            self.failure_probe_interval_seconds = self.interval_seconds
        if self.failure_threshold < 1:
            raise ValueError("route failure threshold must be positive")

    def due(self, now: float) -> bool:
        return float(now) >= self.next_probe_at

    def observe(self, *, now: float, healthy: bool) -> bool:
        """Record one due probe; return True when recovery should be attempted.

        Healthy routes stay on the low-cost steady cadence. The first failed
        public probe switches to a short confirmation cadence so a real Funnel
        outage is repaired quickly, while one transient network hiccup remains
        debounced and never mutates the owned route by itself.
        """
        if healthy:
            self.consecutive_failures = 0
            self.next_probe_at = float(now) + self.interval_seconds
            return False
        self.consecutive_failures += 1
        self.next_probe_at = float(now) + self.failure_probe_interval_seconds
        return self.consecutive_failures >= self.failure_threshold

    def recovered(self, *, now: float) -> None:
        self.consecutive_failures = 0
        self.recovery_count += 1
        self.next_probe_at = float(now) + self.interval_seconds


__all__ = [
    "DEFAULT_ROUTE_FAILURE_PROBE_INTERVAL_SECONDS",
    "DEFAULT_ROUTE_FAILURE_THRESHOLD",
    "DEFAULT_ROUTE_PROBE_INTERVAL_SECONDS",
    "DEFAULT_ROUTE_PROBE_TIMEOUT_SECONDS",
    "RouteHealthTracker",
    "public_mcp_route_failure_class",
    "public_mcp_route_healthy",
]
