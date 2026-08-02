"""Least-capability policy evaluated at the Core Runtime boundary."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, Mapping, Optional, Set

from .models import AccessProfile, Capability, Origin


class PolicyDenied(PermissionError):
    """Raised when an origin lacks an effective capability."""


_PROFILE_CAPABILITIES: Mapping[AccessProfile, FrozenSet[Capability]] = {
    AccessProfile.READ_ONLY: frozenset(
        {
            Capability.REPO_READ,
            Capability.GIT_READ,
            # Browser read is non-mutating observation of a localhost UI
            # (snapshot/screenshot/console/network) -- the same tier as
            # repo.read.  The TUI exposes it through its own checkbox, and the
            # hosted bridge runs read-only sessions that observe the dev
            # server, so it must be reachable from READ_ONLY.
            Capability.BROWSER_READ,
        }
    ),
    AccessProfile.BROWSER_CONTROL: frozenset(
        {
            Capability.REPO_READ,
            Capability.GIT_READ,
            Capability.BROWSER_READ,
            Capability.BROWSER_INPUT,
            Capability.NETWORK,
        }
    ),
    AccessProfile.WORKSPACE_WRITE: frozenset(
        {
            Capability.REPO_READ,
            Capability.REPO_WRITE,
            Capability.PROCESS_RUN,
            Capability.CHECKS_RUN,
            Capability.GIT_READ,
            Capability.MCP_CALL,
            # Browser read is reachable from read-only; browser *input*
            # (open/click/fill/select/press/close) drives a localhost UI and
            # is a workspace-scoped mutation gated behind --write, so it joins
            # the write tier here.  git.commit stays ELEVATED-only by design.
            Capability.BROWSER_READ,
            Capability.BROWSER_INPUT,
        }
    ),
    AccessProfile.ELEVATED: frozenset(
        {
            Capability.REPO_READ,
            Capability.REPO_WRITE,
            Capability.PROCESS_RUN,
            Capability.CHECKS_RUN,
            Capability.GIT_READ,
            Capability.GIT_COMMIT,
            Capability.BROWSER_READ,
            Capability.BROWSER_INPUT,
            Capability.DESKTOP_INPUT,
            Capability.NETWORK,
            Capability.MCP_CALL,
        }
    ),
}

# Push, publish, and authentication are intentionally absent even from the
# elevated profile. They require a future one-shot approval token.
_ALWAYS_EXPLICIT = frozenset(
    {Capability.GIT_PUSH, Capability.PACKAGE_PUBLISH, Capability.AUTH_COMMAND}
)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    capability: Capability
    origin: str
    reason: str


@dataclass(frozen=True)
class CapabilityToken:
    origin: str
    capabilities: FrozenSet[Capability]
    expires_at: float


@dataclass
class CapabilityPolicy:
    profile: AccessProfile
    origin_grants: Dict[str, Set[Capability]] = field(default_factory=dict)
    origin_denies: Dict[str, Set[Capability]] = field(default_factory=dict)
    explicit_tokens: Dict[str, CapabilityToken] = field(default_factory=dict)

    def set_grants(self, origin: Origin, grants: Iterable[Capability]) -> None:
        self.origin_grants[origin.key] = set(grants)

    def set_denies(self, origin: Origin, denies: Iterable[Capability]) -> None:
        self.origin_denies[origin.key] = set(denies)

    def add_token(
        self,
        token: str,
        origin: Origin,
        capabilities: Iterable[Capability],
        ttl_seconds: float = 300.0,
    ) -> None:
        if not token or len(token) < 16:
            raise ValueError("capability token identifiers must be unguessable")
        if ttl_seconds <= 0 or ttl_seconds > 3600:
            raise ValueError("capability token TTL must be between 0 and 3600 seconds")
        self.explicit_tokens[token] = CapabilityToken(
            origin=origin.key,
            capabilities=frozenset(capabilities),
            expires_at=time.time() + ttl_seconds,
        )

    def decide(
        self,
        origin: Origin,
        capability: Capability,
        token: Optional[str] = None,
        *,
        consume_token: bool = False,
    ) -> PolicyDecision:
        if capability in self.origin_denies.get(origin.key, set()):
            return PolicyDecision(False, capability, origin.key, "origin deny")

        profile_caps = _PROFILE_CAPABILITIES[self.profile]
        grants = self.origin_grants.get(origin.key)
        if grants is None:
            grants = set(profile_caps) if origin.kind.value == "user" else set()
        effective = set(profile_caps).intersection(grants)

        # browser.input implies browser.read: a session that may drive a
        # localhost UI (click/fill/select) may always observe it (snapshot/
        # get_text).  This is the capability-level backstop for the
        # ``WebBridgeConnectConfig`` tool-bundle normalization: even if a caller
        # assembles grants directly (bypassing that normalization), a request for
        # ``BROWSER_READ`` is allowed whenever ``BROWSER_INPUT`` is effectively
        # granted, so the bridge can never reach the contradictory state where
        # input is permitted but read is not.
        if (
            capability is Capability.BROWSER_READ
            and Capability.BROWSER_INPUT in effective
        ):
            return PolicyDecision(True, capability, origin.key, "input implies read")

        if capability in _ALWAYS_EXPLICIT:
            approval = self.explicit_tokens.get(token or "")
            if approval is None:
                return PolicyDecision(
                    False, capability, origin.key, "one-shot approval required"
                )
            if approval.expires_at < time.time():
                self.explicit_tokens.pop(token or "", None)
                return PolicyDecision(
                    False, capability, origin.key, "approval token expired"
                )
            if approval.origin != origin.key:
                return PolicyDecision(
                    False, capability, origin.key, "approval token origin mismatch"
                )
            if capability not in approval.capabilities:
                return PolicyDecision(
                    False, capability, origin.key, "approval token scope mismatch"
                )
            effective.add(capability)

        if capability not in effective:
            return PolicyDecision(
                False, capability, origin.key, "capability not granted"
            )
        if consume_token and capability in _ALWAYS_EXPLICIT:
            self.explicit_tokens.pop(token or "", None)
        return PolicyDecision(True, capability, origin.key, "allowed")

    def require(
        self,
        origin: Origin,
        capability: Capability,
        token: Optional[str] = None,
    ) -> PolicyDecision:
        decision = self.decide(origin, capability, token, consume_token=True)
        if not decision.allowed:
            raise PolicyDenied(
                f"{origin.key} cannot use {capability.value}: {decision.reason}"
            )
        return decision
