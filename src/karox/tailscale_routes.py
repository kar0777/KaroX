"""Individual Tailscale Serve/Funnel route inventory and ownership.

Phase 0.4 of the recovery plan: the launcher must not refuse to start just
because *any* route is active, and it must never replace a route that belongs
to another program. The existing ``query_funnel_ownership`` answers only "is
something active?" -- it cannot distinguish a KaroX-owned route from a foreign
one, so it forces an all-or-nothing refusal.

This module enumerates the individual routes from ``tailscale serve status
--json`` (with a fallback text parser), matches each one against a KaroX
bridge's watchdog record (same local port = owned), and returns a verdict:

* ``ROUTE_REUSE`` -- an owned route for the same profile is live; keep it;
* ``ROUTE_STALE_OWNED`` -- an owned route exists but its bridge is dead;
  only this specific route may be removed;
* ``ROUTE_FOREIGN`` -- a route exists that does not match any KaroX bridge;
  never touched, the launcher picks a different path;
* ``ROUTE_FREE`` -- no route at all.

No function here performs a mutation. Deleting an owned route is the caller's
job, and only after the candidate bridge is healthy.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
from typing import Any, Callable, Optional

from .port_ownership import OwnershipVerdict


# --------------------------------------------------------------------------- #
# Route inventory                                                              #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class TailscaleRoute:
    """One Serve/Funnel route as Tailscale reports it."""

    host: str
    path: str
    protocol: str  # "https" or "http"
    local_target: Optional[str]  # e.g. "http://127.0.0.1:8765"
    mode: str  # "serve" or "funnel"
    raw_key: str  # the original key in the JSON, for traceability

    @property
    def local_port(self) -> Optional[int]:
        """Extract the local TCP port from ``local_target`` if present."""
        if not self.local_target:
            return None
        match = re.search(r":(\d+)(?:/|$)", self.local_target)
        return int(match.group(1)) if match else None

    @property
    def public_port(self) -> int:
        """Return the public Serve/Funnel listener port for this route."""
        match = re.search(r"(?:^|://)[^/\s:]+:(\d+)", self.raw_key)
        if match:
            return int(match.group(1))
        return 443 if self.protocol == "https" else 80

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def _parse_serve_json(payload: Any) -> list[TailscaleRoute]:
    """Parse ``tailscale serve status --json`` into individual routes.

    Tailscale's JSON shape has evolved across versions. The common forms are:

    ``{"Foreground": {"<id>": {"Web": {"host:443": {"Handlers": {"/": {"Proxy": "..."}}}}}}}``
    and the flatter
    ``{"Web": {"host:443": {"Handlers": {"/": {"Proxy": "..."}}}}}``
    and the flat
    ``{"https://host:443": {"/": "http://127.0.0.1:8765"}}``.

    All three are handled. A route the parser cannot understand is skipped
    rather than raising: a partial inventory is safer than a failed one.
    """
    routes: list[TailscaleRoute] = []
    if not isinstance(payload, dict):
        return routes

    # Normalize: unwrap a top-level Foreground wrapper so the handler search
    # sees the inner Web/TCP config regardless of the nesting level.
    config_sources: list[Any] = [payload]
    foreground = payload.get("Foreground")
    if isinstance(foreground, dict):
        config_sources.extend(foreground.values())

    for source in config_sources:
        if not isinstance(source, dict):
            continue
        _parse_one_config(source, routes)

    # Deduplicate by (host, path, protocol, local_target)
    seen: set[tuple[str, str, str, Optional[str]]] = set()
    unique: list[TailscaleRoute] = []
    for route in routes:
        identity = (route.host, route.path, route.protocol, route.local_target)
        if identity not in seen:
            seen.add(identity)
            unique.append(route)
    return unique


def _extract_handler(
    host_key: str, path_key: str, handler: Any, mode: str, routes: list[TailscaleRoute]
) -> None:
    local_target: Optional[str] = None
    if isinstance(handler, dict):
        local_target = handler.get("Proxy") or handler.get("Text") or handler.get("Path")
    elif isinstance(handler, str):
        local_target = handler
    if local_target is None:
        return
    stripped = host_key.split("://", 1)[-1]
    host_only = stripped.split(":", 1)[0]
    protocol = "https" if "443" in host_key or host_key.startswith("https") else "http"
    routes.append(
        TailscaleRoute(
            host=host_only,
            path=path_key,
            protocol=protocol,
            local_target=str(local_target),
            mode=mode,
            raw_key=f"{host_key}{path_key}",
        )
    )


def _parse_one_config(source: dict[str, Any], routes: list[TailscaleRoute]) -> None:
    """Extract routes from one config dict (possibly nested under Foreground)."""
    allow_funnel = source.get("AllowFunnel") if isinstance(source.get("AllowFunnel"), dict) else None
    web = source.get("Web") if isinstance(source.get("Web"), dict) else None
    if isinstance(web, dict):
        for host_key, host_config in web.items():
            handlers = (
                host_config.get("Handlers", {})
                if isinstance(host_config, dict)
                else host_config if isinstance(host_config, dict)
                else {}
            )
            mode = "funnel" if (
                isinstance(allow_funnel, dict) and host_key in allow_funnel
            ) else "serve"
            if isinstance(handlers, dict):
                for path_key, handler in handlers.items():
                    _extract_handler(str(host_key), str(path_key), handler, mode, routes)

    # Form 2: flat {"https://host:443": {"/": "http://..."}}
    for key, value in source.items():
        if not isinstance(value, dict):
            continue
        key_str = str(key)
        if key_str.startswith(("http://", "https://")) or ":443" in key_str:
            mode = "funnel" if isinstance(allow_funnel, dict) and key_str in allow_funnel else "serve"
            for path_key, handler in value.items():
                _extract_handler(key_str, str(path_key), handler, mode, routes)


def _parse_serve_text(output: str) -> list[TailscaleRoute]:
    """Fallback parser for the human-readable ``serve status`` output.

    Used when ``--json`` is unavailable or the JSON is malformed. Extracts
    ``http(s)://host/path -> http://127.0.0.1:PORT`` lines. Conservative: a
    line it cannot parse is skipped, so a foreign route is never mistaken for
    a KaroX one.
    """
    routes: list[TailscaleRoute] = []
    # Matches patterns like: "https://host.ts.net/ -> http://127.0.0.1:8765"
    pattern = re.compile(
        r"(https?://[^\s]+?)(/?\S*)\s*(?:->|=>|—)\s*(http://[\d.]+:\d+)",
        re.IGNORECASE,
    )
    for line in output.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        full_url = match.group(1)
        path = match.group(2) or "/"
        local = match.group(3)
        host = full_url.split("://", 1)[1].split("/", 1)[0]
        routes.append(
            TailscaleRoute(
                host=host,
                path=path,
                protocol="https" if full_url.startswith("https") else "http",
                local_target=local,
                mode="funnel" if "funnel" in line.lower() else "serve",
                raw_key=f"{full_url}{path}",
            )
        )
    return routes


def _run_route_probe(
    run: Callable[..., subprocess.CompletedProcess[str]],
    argv: list[str],
    **kwargs: Any,
) -> subprocess.CompletedProcess[str]:
    """Run a read-only Tailscale route probe without flashing a console on Windows.

    On Windows every subprocess-compatible runner receives ``CREATE_NO_WINDOW``.
    The injected runner contract already mirrors ``subprocess.run`` and therefore
    accepts platform subprocess kwargs; this also makes the no-flash behaviour
    directly regression-testable without launching a real console process.
    """
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return run(argv, **kwargs)


def inventory_tailscale_routes(
    executable: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[TailscaleRoute]:
    """Enumerate individual Serve/Funnel routes.

    Tries ``--json`` first, falls back to the text parser. Never raises: an
    unparseable or unavailable state returns an empty list, which the caller
    treats as "no routes known" (safe for reuse decisions, conservative for
    foreign-route detection).
    """
    # Try JSON first.
    try:
        result = _run_route_probe(
            run,
            [executable, "serve", "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    output = (result.stdout or "").strip()
    if result.returncode == 0 and output:
        try:
            payload = json.loads(output)
            routes = _parse_serve_json(payload)
            if routes:
                return routes
        except json.JSONDecodeError:
            pass  # fall through to text parser
    # Fallback: plain text.
    try:
        result = _run_route_probe(
            run,
            [executable, "serve", "status"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    text_output = (result.stdout or result.stderr or "").strip()
    lowered = text_output.lower()
    if "no serve config" in lowered or "not configured" in lowered or not text_output:
        return []
    return _parse_serve_text(text_output)


# --------------------------------------------------------------------------- #
# Route classification for a saved profile                                     #
# --------------------------------------------------------------------------- #


ROUTE_FREE = "free"
ROUTE_REUSE = "reuse_owned"
ROUTE_STALE_OWNED = "stale_owned_route"
ROUTE_FOREIGN = "foreign_route"


@dataclasses.dataclass(frozen=True)
class RouteVerdict:
    verdict: str
    reason: str
    matching_route: Optional[TailscaleRoute]
    foreign_routes: tuple[TailscaleRoute, ...]
    bridge_ownership: Optional[OwnershipVerdict]

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "matching_route": self.matching_route.to_dict() if self.matching_route else None,
            "foreign_routes": [r.to_dict() for r in self.foreign_routes],
            "bridge_ownership": self.bridge_ownership.to_dict() if self.bridge_ownership else None,
        }


def classify_route_for_profile(
    *,
    routes: list[TailscaleRoute],
    profile_name: str,
    bridge_port: int,
    bridge_ownership: OwnershipVerdict,
) -> RouteVerdict:
    """Decide whether a saved profile may reuse, repoint, or must avoid routes.

    A route is "owned" when its ``local_target`` port matches the bridge's port
    AND the bridge ownership verdict proves a KaroX process is live (or was).
    Any route whose port does not match is foreign and must never be touched.
    """
    if not routes:
        return RouteVerdict(
            ROUTE_FREE,
            f"no Tailscale Serve/Funnel routes active; profile '{profile_name}' "
            "may create one",
            matching_route=None,
            foreign_routes=(),
            bridge_ownership=bridge_ownership,
        )

    owned: list[TailscaleRoute] = []
    foreign: list[TailscaleRoute] = []
    for route in routes:
        if route.local_port == bridge_port:
            owned.append(route)
        else:
            foreign.append(route)

    if owned:
        # Is the bridge for this port live and proven?
        if bridge_ownership.verdict in ("reuse_same_profile",):
            return RouteVerdict(
                ROUTE_REUSE,
                f"an owned route for profile '{profile_name}' (port {bridge_port}) "
                "is live; reuse it, do not recreate",
                matching_route=owned[0],
                foreign_routes=tuple(foreign),
                bridge_ownership=bridge_ownership,
            )
        # The bridge is stale/dead but the route still points at its port.
        # Only these specific owned routes may be removed.
        return RouteVerdict(
            ROUTE_STALE_OWNED,
            f"owned route(s) for profile '{profile_name}' (port {bridge_port}) "
            "exist but the bridge is not live; remove only these, then recreate",
            matching_route=owned[0],
            foreign_routes=tuple(foreign),
            bridge_ownership=bridge_ownership,
        )

    # No owned route, but foreign routes exist: never touch them.
    return RouteVerdict(
        ROUTE_FOREIGN,
        f"{len(foreign)} foreign Tailscale route(s) are active and must not be "
        f"changed; profile '{profile_name}' must use a different path or hostname",
        matching_route=None,
        foreign_routes=tuple(foreign),
        bridge_ownership=bridge_ownership,
    )


def route_path_for_profile(profile_name: str) -> str:
    """A deterministic, non-conflicting path for a profile's route.

    When a foreign route occupies ``/``, KaroX uses ``/karox/<profile>/mcp``
    so it never competes with an existing route's path.
    """
    safe = re.sub(r"[^a-zA-Z0-9_-]", "-", profile_name).strip("-") or "default"
    return f"/karox/{safe}/mcp"


__all__ = [
    "ROUTE_FOREIGN",
    "ROUTE_FREE",
    "ROUTE_REUSE",
    "ROUTE_STALE_OWNED",
    "RouteVerdict",
    "TailscaleRoute",
    "classify_route_for_profile",
    "inventory_tailscale_routes",
    "route_path_for_profile",
]
