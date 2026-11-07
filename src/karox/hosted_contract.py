"""Hosted bridge contract: descriptors, errors and the tool runtime protocol."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, Protocol



@dataclass(frozen=True)
class ProxyToolDescriptor:
    """A secret-free descriptor exposed to a hosted client.

    Only the tool name, description, and input schema are forwarded.  Server
    credentials, command lines, URLs, and environment never leave the bridge.
    This is the one symbol the release tree still needs from the v5 ``proxy``
    module; it is defined here so a hosted bridge never depends on the
    third-party MCP proxy being installed.
    """

    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    read_only: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "read_only": self.read_only,
        }

class HostedBridgeError(RuntimeError):
    pass

class HostedBridgeAccessDenied(HostedBridgeError, PermissionError):
    pass

class HostedApprovalRequired(HostedBridgeAccessDenied):
    """Exact-action user confirmation required before a hosted call may run."""

    def __init__(
        self,
        *,
        tool_name: str,
        action_digest: str,
        action_kind: str,
        risk: str,
        consequence: str,
        preview: Optional[dict[str, Any]] = None,
        message: Optional[str] = None,
    ) -> None:
        super().__init__(message or f"KaroX requires user approval for {action_kind}")
        self.tool_name = tool_name
        self.action_digest = action_digest
        self.action_kind = action_kind
        self.risk = risk
        self.consequence = consequence
        self.preview = dict(preview or {})
        self.message = message or f"KaroX requires user approval for {action_kind}."

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "action_digest": self.action_digest,
            "action_kind": self.action_kind,
            "risk": self.risk,
            "consequence": self.consequence,
            "preview": dict(self.preview),
            "message": self.message,
            "continue_independent_work": True,
            "defer_until_blocked": True,
        }

DEFAULT_HOSTED_DEADLINE_SECONDS = 600.0

class HostedToolRuntime(Protocol):
    def descriptors(self) -> list[ProxyToolDescriptor]: ...

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
    ) -> dict[str, Any]: ...
