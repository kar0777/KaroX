"""Typed error family for the computer-use surface.

Every failure a tool call can produce is one of these, so the engine adapter
can translate any of them into a structured result and the hosted facade can
translate them back into the Core's ``InvalidCommand`` contract. Nothing here
raises bare ``RuntimeError`` across a tool boundary.
"""

from __future__ import annotations

from typing import Any


class ComputerUseError(RuntimeError):
    """Base class for typed computer-use failures."""

    error_type = "computer_use"


class PlatformUnsupportedError(ComputerUseError):
    """Native desktop control is Windows-only in this release."""

    error_type = "platform_unsupported"


class DesktopSecurityError(ComputerUseError):
    """An action would escape the attached window/process boundary."""

    error_type = "desktop_security"


class DesktopTimeoutError(ComputerUseError):
    """The action's deadline expired before it could complete."""

    error_type = "desktop_timeout"


class TargetLostError(ComputerUseError):
    """The pinned window/process identity changed; fail closed."""

    error_type = "target_lost"


class InvalidActionError(ComputerUseError):
    """The action name or payload failed validation."""

    error_type = "invalid_action"


class ApprovalRequiredError(ComputerUseError):
    """A consequential action needs a one-shot human approval first."""

    error_type = "approval_required"

    def __init__(
        self,
        message: str,
        *,
        action_digest: str,
        headline: str = "",
        instructions: str = "",
    ) -> None:
        super().__init__(message)
        self.action_digest = action_digest
        self.headline = headline
        self.instructions = instructions

    def structured(self) -> dict[str, Any]:
        return {
            "type": self.error_type,
            "message": str(self),
            "action_digest": self.action_digest,
            "headline": self.headline,
            "instructions": self.instructions,
        }


class CaptureError(ComputerUseError):
    """Background capture produced no usable frame."""

    error_type = "capture_error"


class LaunchError(ComputerUseError):
    """Windows refused to launch the requested application or terminal."""

    error_type = "launch_error"


def error_struct(exc: ComputerUseError) -> dict[str, Any]:
    """Serialize one typed failure for a structured tool result."""

    if isinstance(exc, ApprovalRequiredError):
        return exc.structured()
    payload: dict[str, Any] = {"type": exc.error_type, "message": str(exc)[:500]}
    reason = getattr(exc, "reason", None)
    if isinstance(reason, str):
        payload["reason"] = reason
    return payload




__all__ = [
    "ApprovalRequiredError",
    "CaptureError",
    "ComputerUseError",
    "DesktopSecurityError",
    "DesktopTimeoutError",
    "InvalidActionError",
    "LaunchError",
    "PlatformUnsupportedError",
    "TargetLostError",
    "error_struct",
]
