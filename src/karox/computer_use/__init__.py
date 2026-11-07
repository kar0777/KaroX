"""Non-intrusive computer use for KaroX.

Real typed desktop tools around the karo-core engine: window/app discovery,
background capture, UI inspection, targeted input, scrolling, application
launch and explicit visible-window actions. Background HWND message input and
on-demand PrintWindow capture are the default; there is no global keyboard or
pointer injection, no focus stealing and no interception of the user's own
input. Visible windows (such as "show me the terminal") exist as explicit,
one-shot-approved actions.

Public names are lazy so importing ``karox.computer_use`` does not load every
platform adapter and does not participate in a package-initializer cycle.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

_EXPORTS = {
    "ACTION_KINDS": (".adapter", "ACTION_KINDS"),
    "CONSEQUENTIAL_ACTIONS": (".approval", "CONSEQUENTIAL_ACTIONS"),
    "TOOL_SPECS": (".adapter", "TOOL_SPECS"),
    "ApprovalRejected": (".approval", "ApprovalRejected"),
    "ApprovalRequiredError": (".errors", "ApprovalRequiredError"),
    "ApprovalStore": (".approval", "ApprovalStore"),
    "CaptureError": (".errors", "CaptureError"),
    "ComputerUseAdapter": (".adapter", "ComputerUseAdapter"),
    "ComputerUseError": (".errors", "ComputerUseError"),
    "Deadline": (".deadline", "Deadline"),
    "DesktopSecurityError": (".errors", "DesktopSecurityError"),
    "DesktopSession": (".session", "DesktopSession"),
    "DesktopTimeoutError": (".errors", "DesktopTimeoutError"),
    "InvalidActionError": (".errors", "InvalidActionError"),
    "LaunchError": (".errors", "LaunchError"),
    "PlatformUnsupportedError": (".errors", "PlatformUnsupportedError"),
    "StoredBinding": (".session", "StoredBinding"),
    "TargetLostError": (".errors", "TargetLostError"),
    "ToolResult": (".adapter", "ToolResult"),
    "WindowIdentity": (".identity", "WindowIdentity"),
    "action_digest": (".approval", "action_digest"),
    "capture_identity": (".identity", "capture_identity"),
    "control_contract": (".adapter", "control_contract"),
    "is_consequential": (".approval", "is_consequential"),
    "refresh_identity": (".identity", "refresh_identity"),
}
__all__ = [
    "ACTION_KINDS",
    "CONSEQUENTIAL_ACTIONS",
    "TOOL_SPECS",
    "ApprovalRejected",
    "ApprovalRequiredError",
    "ApprovalStore",
    "CaptureError",
    "ComputerUseAdapter",
    "ComputerUseError",
    "Deadline",
    "DesktopSecurityError",
    "DesktopSession",
    "DesktopTimeoutError",
    "InvalidActionError",
    "LaunchError",
    "PlatformUnsupportedError",
    "StoredBinding",
    "TargetLostError",
    "ToolResult",
    "WindowIdentity",
    "action_digest",
    "capture_identity",
    "control_contract",
    "is_consequential",
    "refresh_identity",
]


def __getattr__(name: str) -> Any:
    try:
        module_name, attribute = _EXPORTS[name]
    except KeyError:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None
    value = getattr(import_module(module_name, __name__), attribute)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
