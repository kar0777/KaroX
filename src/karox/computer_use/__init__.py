"""Non-intrusive computer use for KaroX.

Real typed desktop tools around the karo-core engine: window/app discovery,
background capture, UI inspection, targeted input, scrolling, application
launch and explicit visible-window actions. Background HWND message input and
on-demand PrintWindow capture are the default; there is no global keyboard or
pointer injection, no focus stealing and no interception of the user's own
input. Visible windows (such as "show me the terminal") exist as explicit,
one-shot-approved actions.

Public entry points:

* :class:`karox.computer_use.ComputerUseAdapter` — the narrow engine adapter;
  ``execute(action, payload)`` returns a structured :class:`ToolResult`.
* ``python -m karox.computer_use`` — the JSON CLI the engine drives via bash.
* :data:`karox.computer_use.TOOL_SPECS` — JSON schemas for typed-tool wiring.
* :mod:`karox.desktop_apps` — the hosted ``app.*`` facade over the same core.
"""

from __future__ import annotations

from .adapter import (
    ACTION_KINDS,
    TOOL_SPECS,
    ComputerUseAdapter,
    ToolResult,
    control_contract,
)
from .approval import (
    CONSEQUENTIAL_ACTIONS,
    ApprovalRejected,
    ApprovalStore,
    action_digest,
    is_consequential,
)
from .deadline import Deadline
from .errors import (
    ApprovalRequiredError,
    CaptureError,
    ComputerUseError,
    DesktopSecurityError,
    DesktopTimeoutError,
    InvalidActionError,
    LaunchError,
    PlatformUnsupportedError,
    TargetLostError,
)
from .identity import WindowIdentity, capture_identity, refresh_identity
from .session import DesktopSession, StoredBinding

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
