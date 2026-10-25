"""Guarded native Windows application control for hosted KaroX sessions.

The hosted MCP schema remains stable: ``workspace_worker.execute_browser_command``
routes ``app.*`` actions here. This module is the hosted facade over
:mod:`karox.computer_use` — the same non-intrusive engine the local CLI path
drives — so there is exactly one implementation of background capture,
identity-pinned input and consequential-action approval.

Desktop access is distinct from browser access: it requires the elevated
profile plus ``user_confirmed=true`` for discovery, attachment, launch and
every visible-window action, and it is scoped to the current session.
Bindings pin HWND, PID and process creation time, so a recycled handle or a
restarted process can never redirect input into another application.
"""

from __future__ import annotations

import re
from typing import Any

from .computer_use.adapter import ComputerUseAdapter
from .computer_use.errors import (
    DesktopSecurityError,
    InvalidActionError,
)
from .core_types import InvalidCommand
from .models import AccessProfile, Capability
from .policy import PolicyDenied




class DesktopAppSecurityError(PermissionError):
    """A desktop action would escape the selected application/window boundary."""


class DesktopAppsController:
    """Per-runtime holder of one computer-use adapter."""

    def __init__(self, adapter: ComputerUseAdapter) -> None:
        self.adapter = adapter


#: Actions that open desktop access or put something on the user's screen.
_OPT_IN_ACTIONS = frozenset(
    {"app.discover", "app.attach", "app.launch", "app.show", "app.terminal"}
)

_KNOWN_ACTIONS = frozenset(
    {
        *_OPT_IN_ACTIONS,
        "app.status",
        "app.snapshot",
        "app.inspect",
        "app.ui",
        "app.focus",
        "app.restore",
        "app.click",
        "app.type",
        "app.key",
        "app.scroll",
        "app.detach",
    }
)


def _safe_session_id(runtime: Any) -> str:
    raw = str(getattr(runtime, "session_id", "") or "hosted")
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "-", raw)[:64]
    return cleaned or "hosted"


def _controller(runtime: Any) -> DesktopAppsController:
    existing = getattr(runtime, "_desktop_apps", None)
    if isinstance(existing, DesktopAppsController):
        return existing
    adapter = ComputerUseAdapter(session_id=_safe_session_id(runtime))
    controller = DesktopAppsController(adapter)
    try:
        runtime._desktop_apps = controller
    except Exception:  # a runtime that rejects attributes still works per-call
        pass
    return controller


def _policy_parts(runtime: Any) -> tuple[Any, Any]:
    policy = getattr(runtime, "policy", None)
    origin = getattr(runtime, "hosted_origin", None)
    if policy is None or origin is None:
        raise InvalidCommand(
            "desktop actions require a runtime with a capability policy and origin"
        )
    return policy, origin


def _opt_in(runtime: Any, payload: dict[str, Any]) -> None:
    """Explicit per-call user confirmation plus the elevated capability."""

    if payload.get("user_confirmed") is not True:
        raise DesktopSecurityError("desktop access requires explicit user confirmation")
    policy, origin = _policy_parts(runtime)
    if getattr(policy, "profile", None) is not AccessProfile.ELEVATED:
        raise PolicyDenied("desktop access requires the elevated KaroX profile")
    policy.origin_grants.setdefault(origin.key, set()).add(Capability.DESKTOP_INPUT)
    policy.require(origin, Capability.DESKTOP_INPUT)


def _require_capability(runtime: Any) -> None:
    policy, origin = _policy_parts(runtime)
    policy.require(origin, Capability.DESKTOP_INPUT)


def _run(
    adapter: ComputerUseAdapter,
    action: str,
    payload: dict[str, Any],
    timeout: float,
    human_confirmed: bool,
) -> dict[str, Any]:
    def call(name: str, inner: dict[str, Any], *, confirmed: bool = False) -> dict[str, Any]:
        result = adapter.execute(
            name, inner, timeout=timeout, human_confirmed=confirmed, raise_errors=True
        )
        return dict(result.data)

    contract = adapter.contract()

    if action == "app.discover":
        data = call(
            "desktop.list_windows",
            {"title_contains": payload.get("title_contains"), "limit": payload.get("limit") or 80},
        )
        windows = data.get("windows", [])
        for row in windows:
            row.setdefault("matched_profiles", [])
        apps = call("desktop.list_apps", {"limit": payload.get("limit") or 300})
        return {
            "windows": windows,
            "builtin_apps": [],
            "start_menu_apps": apps.get("apps", []),
            "control_contract": contract,
        }
    if action == "app.attach":
        data = call(
            "desktop.attach",
            {
                "app_id": payload.get("app_id"),
                "title_contains": payload.get("title_contains"),
                "window_id": payload.get("window_id"),
                "access": payload.get("access", "observe"),
            },
        )
        return {
            "attached": True,
            "app_id": data["app_id"],
            "display_name": data["app_id"],
            "access": data["access"],
            "window": data["window"],
            "control_contract": data.get("control_contract", contract),
        }
    if action == "app.launch":
        return call(
            "desktop.launch_app",
            {
                "shortcut_path": payload.get("shortcut_path"),
                "title_contains": payload.get("title_contains") or "",
                "visible": bool(payload.get("visible", False)),
            },
            confirmed=human_confirmed,
        )
    if action == "app.terminal":
        return call(
            "desktop.open_terminal",
            {
                "shell": payload.get("shell") or "auto",
                "keep_open": bool(payload.get("keep_open", True)),
            },
            confirmed=human_confirmed,
        )
    if action == "app.status":
        data = call("desktop.status", {})
        return {
            "bindings": data.get("bindings", []),
            "control_contract": data.get("control_contract", contract),
        }
    if action == "app.snapshot":
        foreground = payload.get("bring_to_front", False)
        if not isinstance(foreground, bool):
            raise InvalidCommand("bring_to_front must be boolean")
        if foreground:
            raise DesktopSecurityError(
                "foreground capture is disabled; app.snapshot is intentionally non-intrusive"
            )
        data = call(
            "desktop.capture",
            {"app_id": payload.get("app_id"), "max_dim": payload.get("max_dim")},
        )
        return {
            "app_id": data["app_id"],
            "artifact": data["artifact"],
            "width": data["width"],
            "height": data["height"],
            "capture_method": data["capture_method"],
            "non_intrusive": True,
            "capture_diagnostics": data["capture_diagnostics"],
            "window": data["window"],
            "control_contract": data.get("control_contract", contract),
        }
    if action in {"app.inspect", "app.ui"}:
        data = call(
            "desktop.inspect_ui",
            {
                "app_id": payload.get("app_id"),
                "max_nodes": payload.get("max_nodes"),
                "include_text": payload.get("include_text", True),
            },
        )
        return {
            "app_id": payload.get("app_id"),
            "window": data["window"],
            "health": data["health"],
            "process_activity": data["process"],
            "nodes": data["nodes"],
            "truncated": data["truncated"],
            "control_contract": contract,
        }
    if action == "app.focus":
        app_id = payload.get("app_id")
        adapter.session.bound(app_id if isinstance(app_id, str) else "")
        return {"app_id": app_id, "focused": False, **contract}
    if action == "app.restore":
        data = call(
            "desktop.show_window",
            {"app_id": payload.get("app_id"), "mode": "restore_noactivate"},
        )
        return {
            "app_id": data["app_id"],
            "restored": data["restored"],
            "activated": data["activated"],
            **contract,
        }
    if action == "app.show":
        data = call(
            "desktop.show_window",
            {"app_id": payload.get("app_id"), "mode": "restore_visible"},
            confirmed=human_confirmed,
        )
        return {
            "app_id": data["app_id"],
            "restored": data["restored"],
            "activated": data["activated"],
            "visible_requested": data["visible_requested"],
            **contract,
        }
    if action == "app.click":
        data = call(
            "desktop.click",
            {
                "app_id": payload.get("app_id"),
                "x": payload.get("x"),
                "y": payload.get("y"),
                "button": payload.get("button") or "left",
                "clicks": payload.get("clicks") or 1,
            },
        )
        return {
            "app_id": data["app_id"],
            "clicked": True,
            "x": payload.get("x"),
            "y": payload.get("y"),
            "target_hwnd": data.get("target_hwnd"),
            **contract,
        }
    if action == "app.type":
        data = call(
            "desktop.type_text",
            {
                "app_id": payload.get("app_id"),
                "text": payload.get("text"),
                "pace_ms": payload.get("pace_ms") or 0,
            },
        )
        # The raw text is never echoed back: hosted evidence stays redacted.
        return {
            "app_id": data["app_id"],
            "typed": True,
            "text_length": data["text_chars"],
            **contract,
        }
    if action == "app.key":
        data = call(
            "desktop.press_key",
            {"app_id": payload.get("app_id"), "key": payload.get("key")},
        )
        return {
            "app_id": data["app_id"],
            "pressed": True,
            "key": data["key"],
            **contract,
        }
    if action == "app.scroll":
        data = call(
            "desktop.scroll",
            {
                "app_id": payload.get("app_id"),
                "x": payload.get("x"),
                "y": payload.get("y"),
                "clicks": payload.get("clicks") or -3,
                "horizontal": payload.get("horizontal") or 0,
            },
        )
        return {
            "app_id": data["app_id"],
            "scrolled": True,
            "clicks": data["clicks"],
            "delta_total": data["delta_total"],
            **contract,
        }
    if action == "app.detach":
        data = call("desktop.detach", {"app_id": payload.get("app_id")})
        return {"app_id": data["app_id"], "detached": data["detached"]}
    raise InvalidCommand(f"unsupported desktop app action: {action}")


def execute_desktop_app_action(
    runtime: Any,
    action: str,
    payload: dict[str, Any],
    deadline_seconds: float,
) -> dict[str, Any]:
    """Execute one hosted ``app.*`` desktop action under policy and deadline."""

    if runtime is None:
        raise InvalidCommand(
            "desktop actions require a runtime with a capability policy"
        )
    if not isinstance(action, str) or not action:
        raise InvalidCommand("desktop action must be a non-empty string")
    if not isinstance(payload, dict):
        raise InvalidCommand("desktop payload must be an object")
    if action not in _KNOWN_ACTIONS:
        raise InvalidCommand(f"unsupported desktop app action: {action}")

    controller = _controller(runtime)
    if action in _OPT_IN_ACTIONS:
        _opt_in(runtime, payload)
    else:
        _require_capability(runtime)
    try:
        timeout = float(deadline_seconds)
    except (TypeError, ValueError):
        timeout = 30.0
    try:
        return _run(
            controller.adapter,
            action,
            payload,
            timeout,
            human_confirmed=payload.get("user_confirmed") is True,
        )
    except InvalidActionError as exc:
        raise InvalidCommand(str(exc)) from exc


__all__ = [
    "DesktopAppSecurityError",
    "DesktopAppsController",
    "execute_desktop_app_action",
]
