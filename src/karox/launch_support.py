"""Capability assessment for saved connection launchers.

A saved connection can be syntactically valid without being launchable by the
current KaroX runtime.  This module keeps that distinction explicit and returns
machine-stable blocker codes before a child process is spawned.

The first managed launcher is the saved ClickUp bearer/Streamable HTTP path.
Other presets remain visible and testable as configurations, but ``start`` and
``restart`` fail early until a launcher with an end-to-end contract is added.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .connections import McpClientTarget


BLOCKER_TRANSPORT_UNSUPPORTED = "transport_unsupported"
BLOCKER_TUNNEL_UNSUPPORTED = "tunnel_unsupported"
BLOCKER_CUSTOM_TUNNEL_NEEDS_URL = "custom_tunnel_requires_public_url"
BLOCKER_STABLE_URL_UNAVAILABLE = "stable_url_unavailable"
BLOCKER_OAUTH_NEEDS_STABLE_URL = "oauth_requires_stable_url"
BLOCKER_CREDENTIAL_MISSING = "credential_missing"
BLOCKER_AUTH_UNSUPPORTED = "auth_scheme_unsupported"
BLOCKER_LAUNCHER_UNAVAILABLE = "launcher_unavailable"

LAUNCHABLE_TRANSPORTS: frozenset[str] = frozenset({"streamable_http", "openapi"})
MANAGED_TUNNELS: frozenset[str] = frozenset({"cloudflare", "tailscale", "local"})
TEMPORARY_URL_TUNNELS: frozenset[str] = frozenset({"cloudflare"})
LAUNCHABLE_AUTH_SCHEMES: frozenset[str] = frozenset(
    {"bearer", "api_key", "custom_header", "oauth", "none"}
)


@dataclass(frozen=True)
class LaunchSupport:
    """Whether one saved connection has a concrete managed launcher."""

    connection_id: str
    supported: bool
    launcher_id: Optional[str]
    blockers: tuple[str, ...]
    transport: str
    tunnel: str
    auth_scheme: str
    stable_url: bool
    credential_ready: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "connection_id": self.connection_id,
            "supported": self.supported,
            "launcher_id": self.launcher_id or "",
            "blockers": list(self.blockers),
            "transport": self.transport,
            "tunnel": self.tunnel,
            "auth_scheme": self.auth_scheme,
            "stable_url": self.stable_url,
            "credential_ready": self.credential_ready,
        }


@dataclass(frozen=True)
class ConnectionLaunchResult:
    """Normalized launcher result consumed by the controller and presentations."""

    success: bool
    target: Optional[McpClientTarget] = None
    public_endpoint: Optional[str] = None
    local_endpoint: Optional[str] = None
    runtime_id: Optional[str] = None
    failure_kind: Optional[str] = None
    failure_detail: Optional[str] = None
    remediation: Optional[str] = None
    raw: Optional[Any] = None

    @classmethod
    def coerce(cls, value: Any) -> "ConnectionLaunchResult":
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls(
                success=bool(value.get("success")),
                target=value.get("target") if isinstance(value.get("target"), McpClientTarget) else None,
                public_endpoint=_text(value.get("public_endpoint")),
                local_endpoint=_text(value.get("local_endpoint")),
                runtime_id=_text(value.get("runtime_id")),
                failure_kind=_text(value.get("failure_kind")),
                failure_detail=_text(value.get("failure_detail")),
                remediation=_text(value.get("remediation")),
                raw=value,
            )
        return cls(
            success=bool(getattr(value, "success", False)),
            target=(
                getattr(value, "target", None)
                if isinstance(getattr(value, "target", None), McpClientTarget)
                else None
            ),
            public_endpoint=_text(getattr(value, "public_endpoint", None)),
            local_endpoint=_text(getattr(value, "local_endpoint", None)),
            runtime_id=_text(getattr(value, "runtime_id", None)),
            failure_kind=_text(getattr(value, "failure_kind", None)),
            failure_detail=_text(getattr(value, "failure_detail", None)),
            remediation=_text(getattr(value, "remediation", None)),
            raw=value,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "success": self.success,
            "target": self.target.to_dict() if self.target is not None else None,
            "public_endpoint": self.public_endpoint or "",
            "local_endpoint": self.local_endpoint or "",
            "runtime_id": self.runtime_id or "",
            "failure_kind": self.failure_kind or "",
            "failure_detail": self.failure_detail or "",
            "remediation": self.remediation or "",
        }


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def launch_support(
    target: McpClientTarget,
    *,
    credential_available: Optional[bool] = None,
) -> LaunchSupport:
    """Return the exact reasons a saved target can or cannot be launched.

    ``credential_available`` is resolved by the controller from the correct
    keyring namespace.  ``None`` means only metadata was inspected; an explicit
    ``False`` records a missing/unresolvable credential.
    """

    blockers: list[str] = []

    if target.transport not in LAUNCHABLE_TRANSPORTS:
        blockers.append(BLOCKER_TRANSPORT_UNSUPPORTED)
    if target.tunnel not in MANAGED_TUNNELS:
        blockers.append(BLOCKER_TUNNEL_UNSUPPORTED)
    if target.tunnel == "custom" and not target.public_url:
        blockers.append(BLOCKER_CUSTOM_TUNNEL_NEEDS_URL)
    if target.auth_scheme not in LAUNCHABLE_AUTH_SCHEMES:
        blockers.append(BLOCKER_AUTH_UNSUPPORTED)

    stable_url = bool(
        target.url_stability == "stable"
        and target.tunnel not in TEMPORARY_URL_TUNNELS
    )
    if target.url_stability == "stable" and target.tunnel in TEMPORARY_URL_TUNNELS:
        blockers.append(BLOCKER_STABLE_URL_UNAVAILABLE)
    if target.auth_scheme == "oauth" and not stable_url:
        blockers.append(BLOCKER_OAUTH_NEEDS_STABLE_URL)

    credential_ready = target.auth_scheme == "none" or bool(target.credential_ref)
    if credential_available is not None:
        credential_ready = target.auth_scheme == "none" or credential_available
    if not credential_ready:
        blockers.append(BLOCKER_CREDENTIAL_MISSING)

    launcher_id: Optional[str] = None
    if (
        target.preset_id == "clickup"
        and target.transport == "streamable_http"
        and target.auth_scheme == "bearer"
        and target.tunnel in MANAGED_TUNNELS
    ):
        launcher_id = "saved-clickup"
    else:
        blockers.append(BLOCKER_LAUNCHER_UNAVAILABLE)

    ordered = tuple(dict.fromkeys(blockers))
    return LaunchSupport(
        connection_id=target.connection_id,
        supported=not ordered,
        launcher_id=launcher_id,
        blockers=ordered,
        transport=target.transport,
        tunnel=target.tunnel,
        auth_scheme=target.auth_scheme,
        stable_url=stable_url,
        credential_ready=credential_ready,
    )


__all__ = [
    "LaunchSupport",
    "ConnectionLaunchResult",
    "launch_support",
    "BLOCKER_TRANSPORT_UNSUPPORTED",
    "BLOCKER_TUNNEL_UNSUPPORTED",
    "BLOCKER_CUSTOM_TUNNEL_NEEDS_URL",
    "BLOCKER_STABLE_URL_UNAVAILABLE",
    "BLOCKER_OAUTH_NEEDS_STABLE_URL",
    "BLOCKER_CREDENTIAL_MISSING",
    "BLOCKER_AUTH_UNSUPPORTED",
    "BLOCKER_LAUNCHER_UNAVAILABLE",
    "LAUNCHABLE_TRANSPORTS",
    "MANAGED_TUNNELS",
    "TEMPORARY_URL_TUNNELS",
    "LAUNCHABLE_AUTH_SCHEMES",
]
