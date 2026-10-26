"""The narrow engine adapter for computer use.

One class, one method, structured results. ``ComputerUseAdapter.execute``
maps a typed action name plus a JSON payload to an :class:`ToolResult` whose
``data`` is the observation/action outcome and whose ``evidence`` is the
redacted, persistable record. The karo-core engine reaches this through the
``python -m karox.computer_use`` CLI (its tool protocol is bash, so a stable
JSON command line *is* the real agent path); a typed-tool integration can call
the adapter directly and hand ``TOOL_SPECS`` to the model.

Safety model, enforced here for every caller:

* consequential actions (process launch, anything that puts a window on the
  user's screen) fail closed behind a one-shot, action-digest-bound human
  approval token — or, for the hosted bridge, its user-confirmation flow;
* every payload is validated before any OS call;
* evidence never contains typed text, control text or raw pixels — only
  lengths, digests and artifact references;
* failures arrive as structured errors with stable ``type`` strings, never as
  bare exceptions leaking out of ``execute``.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from ..paths import runtime_dir
from . import capture, inspection, input as desktop_input, launch, redact
from .approval import (
    CONSEQUENTIAL_ACTIONS,
    ApprovalRejected,
    ApprovalStore,
    action_digest,
    is_consequential,
    normalize_payload,
)
from .deadline import Deadline
from .errors import (
    ApprovalRequiredError,
    ComputerUseError,
    DesktopSecurityError,
    InvalidActionError,
    PlatformUnsupportedError,
    error_struct,
)
from .session import DesktopSession

DEFAULT_TIMEOUT_SECONDS = 30.0

ACTION_KINDS: Mapping[str, str] = {
    "desktop.status": "observation",
    "desktop.list_windows": "observation",
    "desktop.list_apps": "observation",
    "desktop.request_approval": "observation",
    "desktop.capture": "observation",
    "desktop.inspect_ui": "observation",
    "desktop.attach": "action",
    "desktop.detach": "action",
    "desktop.type_text": "action",
    "desktop.press_key": "action",
    "desktop.click": "action",
    "desktop.scroll": "action",
    "desktop.launch_app": "action",
    "desktop.show_window": "action",
    "desktop.open_terminal": "action",
}


def control_contract(dpi_awareness: str = "unknown") -> dict[str, Any]:
    """Machine-readable invariants of the non-intrusive control surface."""

    return {
        "background_control": True,
        "input_backend": "win32-window-messages",
        "screen_capture_backend": "win32-printwindow",
        "screen_capture_mode": "on-demand",
        "focus_steal_blocked": True,
        "global_keyboard_injection": False,
        "global_pointer_injection": False,
        "background_polling": False,
        "dpi_awareness": dpi_awareness,
        "coordinate_space": "physical-pixels-window-relative",
        "identity_pinning": ["hwnd", "pid", "process_create_time", "executable_digest"],
    }


@dataclass(frozen=True)
class ToolResult:
    """One structured observation/action result."""

    action: str
    kind: str
    ok: bool
    data: dict[str, Any] = field(default_factory=dict)
    evidence: dict[str, Any] = field(default_factory=dict)
    error: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "kind": self.kind,
            "ok": self.ok,
            "data": self.data,
            "evidence": self.evidence,
            "error": self.error,
        }

    def to_json_line(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, default=str)


def _instructions(action: str, payload: Mapping[str, Any], session_id: str) -> str:
    effective = json.dumps(
        normalize_payload(action, payload), sort_keys=True, ensure_ascii=False, default=str
    )
    return (
        "A human must run, in an interactive terminal (never through the agent): "
        f"python -m karox.computer_use approve --action {action} --payload '{effective}' "
        f"--session {session_id} -- then retry this exact action once with "
        "--confirm-token <TOKEN>."
    )


def _require(payload: Mapping[str, Any], key: str, kinds: tuple[type, ...]) -> Any:
    value = payload.get(key)
    if isinstance(value, bool) and bool in kinds:
        return value
    if not isinstance(value, kinds) or isinstance(value, bool):
        names = "/".join(kind.__name__ for kind in kinds)
        raise InvalidActionError(f"payload.{key} must be {names}")
    return value


def _optional_str(payload: Mapping[str, Any], key: str) -> Optional[str]:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise InvalidActionError(f"payload.{key} must be a string")
    return value


def _optional_int(payload: Mapping[str, Any], key: str) -> Optional[int]:
    value = payload.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise InvalidActionError(f"payload.{key} must be an integer")
    return value


def _optional_bool(payload: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = payload.get(key, default)
    if not isinstance(value, bool):
        raise InvalidActionError(f"payload.{key} must be boolean")
    return value


class ComputerUseAdapter:
    """The narrow typed surface around the whole computer-use implementation."""

    def __init__(
        self,
        *,
        runtime_root: Optional[Path] = None,
        session_id: str = "cli",
        probe: Any = None,
        approvals: Optional[ApprovalStore] = None,
        default_timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self.runtime_root = Path(runtime_root) if runtime_root else runtime_dir() / "vnext" / "computer-use"
        self.session_id = session_id
        self.default_timeout = float(default_timeout)
        self._probe = probe
        self._probe_failed = False
        self._session: Optional[DesktopSession] = None
        self.approvals = approvals or ApprovalStore(self.runtime_root / "approvals.json")

    # -- plumbing -----------------------------------------------------------
    @property
    def probe(self) -> Any:
        if self._probe is None and not self._probe_failed:
            try:
                from .win32 import default_probe

                self._probe = default_probe()
            except PlatformUnsupportedError:
                self._probe_failed = True
        if self._probe is None:
            raise PlatformUnsupportedError("native computer use is currently Windows-only")
        return self._probe

    @property
    def session(self) -> DesktopSession:
        if self._session is None:
            self._session = DesktopSession(self.runtime_root, self.session_id, self.probe)
        return self._session

    def contract(self) -> dict[str, Any]:
        try:
            dpi = str(self.probe.ensure_dpi_awareness())
        except ComputerUseError:
            dpi = "unavailable"
        return control_contract(dpi)

    def _binding(self, payload: Mapping[str, Any], *, control: bool = False) -> Any:
        app_id = _require(payload, "app_id", (str,))
        return self.session.bound(app_id, control=control)

    # -- public entry point ---------------------------------------------------
    def execute(
        self,
        action: str,
        payload: Mapping[str, Any],
        *,
        timeout: Optional[float] = None,
        confirmation_token: Optional[str] = None,
        human_confirmed: bool = False,
        raise_errors: bool = False,
    ) -> ToolResult:
        """Run one typed desktop action and return a structured result."""

        kind = ACTION_KINDS.get(action) if isinstance(action, str) else None
        if kind is None:
            exc = InvalidActionError(f"unknown desktop action: {action!r}")
            if raise_errors:
                raise exc
            return ToolResult(
                action=str(action), kind="action", ok=False, error=error_struct(exc)
            )
        if not isinstance(payload, Mapping):
            exc = InvalidActionError("payload must be an object")
            if raise_errors:
                raise exc
            return ToolResult(action=action, kind=kind, ok=False, error=error_struct(exc))

        seconds = self.default_timeout if timeout is None else float(timeout)
        deadline = Deadline.after(seconds)
        try:
            if is_consequential(action, payload) and not human_confirmed:
                digest = action_digest(action, payload)
                if not confirmation_token:
                    raise ApprovalRequiredError(
                        "consequential desktop action requires a one-shot human approval",
                        action_digest=digest,
                        headline=f"{action} for session {self.session_id}",
                        instructions=_instructions(action, payload, self.session_id),
                    )
                self.approvals.redeem(confirmation_token, digest, self.session_id)
            deadline.check("desktop action")
            data, evidence_extra, target = self._dispatch(action, dict(payload), deadline)
        except ComputerUseError as exc:  # includes ApprovalRejected
            if raise_errors:
                raise
            return ToolResult(
                action=action,
                kind=kind,
                ok=False,
                evidence=self._base_evidence(action, kind),
                error=error_struct(exc),
            )
        evidence = self._base_evidence(action, kind)
        if target is not None:
            evidence["target"] = target
        evidence.update(evidence_extra)
        return ToolResult(action=action, kind=kind, ok=True, data=data, evidence=evidence)

    def _base_evidence(self, action: str, kind: str) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "action": action,
            "kind": kind,
            "timestamp": time.time(),
            "target": None,
            "redacted": {},
        }

    # -- handlers ---------------------------------------------------------------
    def _dispatch(
        self, action: str, payload: dict[str, Any], deadline: Deadline
    ) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]:
        handler = getattr(self, "_handle_" + action.split(".", 1)[1])
        return handler(payload, deadline)

    def _handle_status(
        self, payload: dict[str, Any], deadline: Deadline
    ) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]:
        del payload, deadline
        available = True
        try:
            probe = self.probe
        except PlatformUnsupportedError:
            probe = None
            available = False
        bindings: list[dict[str, Any]] = []
        if probe is not None:
            bindings = self.session.list_bindings()
        data = {
            "session_id": self.session_id,
            "platform_available": available,
            "platform": getattr(probe, "platform_name", "unavailable"),
            "bindings": bindings,
            "control_contract": self.contract(),
            "actions": sorted(ACTION_KINDS),
        }
        return data, {"bindings_count": len(bindings)}, None

    def _handle_list_windows(
        self, payload: dict[str, Any], deadline: Deadline
    ) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]:
        deadline.check("desktop list_windows")
        title_contains = _optional_str(payload, "title_contains")
        limit = _optional_int(payload, "limit") or 80
        rows = self.session.list_windows(title_contains=title_contains, limit=limit)
        foreground = int(self.probe.foreground_hwnd() or 0)
        windows = []
        for identity in rows:
            public = identity.public()
            public["is_foreground"] = identity.hwnd == foreground
            windows.append(public)
        return {"windows": windows, "count": len(windows)}, {"windows_count": len(windows)}, None

    def _handle_list_apps(
        self, payload: dict[str, Any], deadline: Deadline
    ) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]:
        deadline.check("desktop list_apps")
        limit = _optional_int(payload, "limit") or 300
        data = launch.list_start_menu_apps(limit=limit)
        return data, {"apps_count": len(data.get("apps", []))}, None

    def _handle_request_approval(
        self, payload: dict[str, Any], deadline: Deadline
    ) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]:
        del deadline
        action = _require(payload, "action", (str,))
        if action not in ACTION_KINDS:
            raise InvalidActionError(f"unknown desktop action: {action!r}")
        inner = payload.get("payload") or {}
        if not isinstance(inner, Mapping):
            raise InvalidActionError("payload.payload must be an object")
        digest = action_digest(action, inner)
        data = {
            "action": action,
            "action_digest": digest,
            "consequential": is_consequential(action, inner),
            "ttl_seconds": self.approvals.ttl_seconds,
            "instructions": _instructions(action, inner, self.session_id),
        }
        return data, {"approval_digest": digest}, None

    def _handle_attach(
        self, payload: dict[str, Any], deadline: Deadline
    ) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]:
        deadline.check("desktop attach")
        app_id = _require(payload, "app_id", (str,))
        access = payload.get("access", "observe")
        binding = self.session.attach(
            app_id,
            title_contains=_optional_str(payload, "title_contains"),
            window_id=_optional_int(payload, "window_id"),
            access=access if isinstance(access, str) else "observe",
        )
        data = {
            "attached": True,
            "app_id": app_id,
            "access": binding.access,
            "window": binding.identity.public(),
            "control_contract": self.contract(),
        }
        return data, {"redacted": {"app_id": app_id, "access": binding.access}}, binding.identity.public()

    def _handle_detach(
        self, payload: dict[str, Any], deadline: Deadline
    ) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]:
        del deadline
        app_id = _require(payload, "app_id", (str,))
        detached = self.session.detach(app_id)
        return {"app_id": app_id, "detached": detached}, {"detached": detached}, None

    def _handle_capture(
        self, payload: dict[str, Any], deadline: Deadline
    ) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]:
        binding = self._binding(payload)
        max_dim = _optional_int(payload, "max_dim") or 1800
        history = self.session.capture_history()
        result = capture.capture_window(
            self.probe,
            binding.identity,
            deadline=deadline,
            max_dim=max_dim,
            history=history,
            app_id=binding.app_id,
        )
        self.session.save_capture_history(history)
        artifact = self.session.put_artifact(result.png, f"desktop-{binding.app_id}.png")
        data = {
            "app_id": binding.app_id,
            "artifact": artifact,
            "width": result.width,
            "height": result.height,
            "capture_method": result.method,
            "sha256": result.sha256,
            "quality": result.quality,
            "non_intrusive": True,
            "capture_diagnostics": result.diagnostics,
            "window": binding.identity.public(),
            "control_contract": self.contract(),
        }
        evidence = {
            "artifacts": [
                {key: artifact[key] for key in ("artifact_id", "name", "mime", "size", "sha256")}
            ],
            "redacted": {
                "capture_sha256": result.sha256,
                "capture_method": result.method,
                "freshness": result.diagnostics.get("freshness"),
                "quality_meaningful": bool(result.quality.get("meaningful")),
            },
        }
        return data, evidence, binding.identity.public()

    def _handle_inspect_ui(
        self, payload: dict[str, Any], deadline: Deadline
    ) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]:
        binding = self._binding(payload)
        max_nodes = _optional_int(payload, "max_nodes") or 250
        include_text = _optional_bool(payload, "include_text", True)
        tree = inspection.inspect_window(
            self.probe,
            binding.identity,
            deadline=deadline,
            max_nodes=max_nodes,
            include_text=include_text,
        )
        data = {
            "app_id": binding.app_id,
            "window": tree["window"],
            "health": tree["health"],
            "process": tree["process"],
            "nodes": tree["nodes"],
            "truncated": tree["truncated"],
        }
        return data, dict(tree["evidence"]), binding.identity.public()

    def _handle_type_text(
        self, payload: dict[str, Any], deadline: Deadline
    ) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]:
        binding = self._binding(payload, control=True)
        text = _require(payload, "text", (str,))
        pace_ms = _optional_int(payload, "pace_ms") or 0
        result = desktop_input.type_text(
            self.probe, binding.identity, text, deadline=deadline, pace_ms=pace_ms
        )
        data = {
            "app_id": binding.app_id,
            "typed": True,
            "text_chars": result["chars"],
            "target_hwnd": result["target_hwnd"],
            "focus_source": result["focus_source"],
        }
        redacted = redact.redact_text(text)
        evidence = {
            "redacted": {
                "text_chars": redacted["chars"],
                "text_sha256_16": redacted["sha256_16"],
            }
        }
        return data, evidence, binding.identity.public()

    def _handle_press_key(
        self, payload: dict[str, Any], deadline: Deadline
    ) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]:
        binding = self._binding(payload, control=True)
        key = _require(payload, "key", (str,))
        result = desktop_input.press_key(self.probe, binding.identity, key, deadline=deadline)
        data = {
            "app_id": binding.app_id,
            "pressed": True,
            "key": result["key"],
            "target_hwnd": result["target_hwnd"],
            "focus_source": result["focus_source"],
        }
        if "modifier_caveat" in result:
            data["modifier_caveat"] = result["modifier_caveat"]
        return data, {"redacted": {"key": result["key"]}}, binding.identity.public()

    def _handle_click(
        self, payload: dict[str, Any], deadline: Deadline
    ) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]:
        binding = self._binding(payload, control=True)
        x = _require(payload, "x", (int,))
        y = _require(payload, "y", (int,))
        button = _optional_str(payload, "button") or "left"
        clicks = _optional_int(payload, "clicks") or 1
        result = desktop_input.click(
            self.probe, binding.identity, x, y, button=button, clicks=clicks, deadline=deadline
        )
        data = {"app_id": binding.app_id, **result}
        evidence = {"redacted": {"point": [x, y], "button": button, "clicks": clicks}}
        return data, evidence, binding.identity.public()

    def _handle_scroll(
        self, payload: dict[str, Any], deadline: Deadline
    ) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]:
        binding = self._binding(payload, control=True)
        result = desktop_input.scroll(
            self.probe,
            binding.identity,
            x=_optional_int(payload, "x"),
            y=_optional_int(payload, "y"),
            clicks=_optional_int(payload, "clicks") or -3,
            horizontal=_optional_int(payload, "horizontal") or 0,
            deadline=deadline,
        )
        data = {"app_id": binding.app_id, **result}
        evidence = {"redacted": {"clicks": result["clicks"], "direction": result["direction"]}}
        return data, evidence, binding.identity.public()

    def _handle_launch_app(
        self, payload: dict[str, Any], deadline: Deadline
    ) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]:
        shortcut = _require(payload, "shortcut_path", (str,))
        visible = _optional_bool(payload, "visible", False)
        title_contains = _optional_str(payload, "title_contains") or ""
        result = launch.launch_shortcut(
            self.probe,
            shortcut,
            visible=visible,
            deadline=deadline,
            title_contains=title_contains,
        )
        evidence = {
            "redacted": {
                "shortcut_sha256_16": hashlib.sha256(
                    shortcut.encode("utf-8", "surrogatepass")
                ).hexdigest()[:16],
                "visible_requested": visible,
            }
        }
        return result, evidence, None

    def _handle_show_window(
        self, payload: dict[str, Any], deadline: Deadline
    ) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]:
        binding = self._binding(payload, control=True)
        mode = _optional_str(payload, "mode") or "restore_noactivate"
        result = launch.restore_window(
            self.probe, binding.identity, mode=mode, deadline=deadline
        )
        data = {"app_id": binding.app_id, **result}
        return data, {"redacted": {"mode": mode}}, binding.identity.public()

    def _handle_open_terminal(
        self, payload: dict[str, Any], deadline: Deadline
    ) -> tuple[dict[str, Any], dict[str, Any], Optional[dict[str, Any]]]:
        shell = _optional_str(payload, "shell") or "auto"
        keep_open = _optional_bool(payload, "keep_open", True)
        result = launch.open_terminal(
            self.probe, shell=shell, keep_open=keep_open, deadline=deadline
        )
        evidence = {
            "redacted": {"shell": result["shell"], "keep_open": keep_open},
            "process_identity": result["evidence"]["process_identity"],
        }
        target = result.get("window")
        return result, evidence, target


TOOL_SPECS: Mapping[str, dict[str, Any]] = {
    "desktop.status": {
        "name": "desktop.status",
        "description": (
            "Report the computer-use session: platform availability, current window "
            "attachments and the non-intrusive control contract."
        ),
        "input_schema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "desktop.list_windows": {
        "name": "desktop.list_windows",
        "description": (
            "List open top-level windows (id, title, class, physical-pixel bounds, "
            "dpi scale, pid). Observation only; never activates anything."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title_contains": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 120},
            },
            "additionalProperties": False,
        },
    },
    "desktop.list_apps": {
        "name": "desktop.list_apps",
        "description": "List installed Start Menu shortcuts launchable by desktop.launch_app.",
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 300}},
            "additionalProperties": False,
        },
    },
    "desktop.request_approval": {
        "name": "desktop.request_approval",
        "description": (
            "Compute the one-shot approval digest and human instructions for a "
            "consequential action without executing it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"action": {"type": "string"}, "payload": {"type": "object"}},
            "required": ["action"],
            "additionalProperties": False,
        },
    },
    "desktop.attach": {
        "name": "desktop.attach",
        "description": (
            "Pin one window as a target. app_id is your own label for this "
            "attachment; choose the window with title_contains or with the "
            "window_id returned by desktop.list_windows. access=control is "
            "required before any input action."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app_id": {"type": "string"},
                "title_contains": {"type": "string"},
                "window_id": {"type": "integer"},
                "access": {"type": "string", "enum": ["observe", "control"]},
            },
            "required": ["app_id"],
            "additionalProperties": False,
        },
    },
    "desktop.detach": {
        "name": "desktop.detach",
        "description": "Release one pinned window attachment.",
        "input_schema": {
            "type": "object",
            "properties": {"app_id": {"type": "string"}},
            "required": ["app_id"],
            "additionalProperties": False,
        },
    },
    "desktop.capture": {
        "name": "desktop.capture",
        "description": (
            "Background screenshot of the attached window via PrintWindow/WM_PRINT. "
            "Never reads screen regions, never activates the window. Returns a PNG "
            "artifact reference plus freshness/quality diagnostics."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app_id": {"type": "string"},
                "max_dim": {"type": "integer", "minimum": 64, "maximum": 4096},
            },
            "required": ["app_id"],
            "additionalProperties": False,
        },
    },
    "desktop.inspect_ui": {
        "name": "desktop.inspect_ui",
        "description": (
            "Enumerate the attached window's control tree (class, text, bounds, "
            "visible/enabled) with hung-proof timeouts. Text is capped and never "
            "persisted to evidence."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app_id": {"type": "string"},
                "max_nodes": {"type": "integer", "minimum": 1, "maximum": 1000},
                "include_text": {"type": "boolean"},
            },
            "required": ["app_id"],
            "additionalProperties": False,
        },
    },
    "desktop.type_text": {
        "name": "desktop.type_text",
        "description": (
            "Type text into the attached window's internal keyboard focus via "
            "WM_CHAR. No global keyboard state is touched. Evidence stores only "
            "length and digest of the text."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app_id": {"type": "string"},
                "text": {"type": "string", "minLength": 1, "maxLength": 20000},
                "pace_ms": {"type": "integer", "minimum": 0, "maximum": 250},
            },
            "required": ["app_id", "text"],
            "additionalProperties": False,
        },
    },
    "desktop.press_key": {
        "name": "desktop.press_key",
        "description": (
            "Press one named key or ctrl/shift/alt combo in the attached window. "
            "Global OS shortcut modifiers (win/super/meta) are refused."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"app_id": {"type": "string"}, "key": {"type": "string"}},
            "required": ["app_id", "key"],
            "additionalProperties": False,
        },
    },
    "desktop.click": {
        "name": "desktop.click",
        "description": (
            "Background click at physical-pixel coordinates relative to the "
            "attached window's top-left corner. Messages go to the deepest owned "
            "child under the point; the real pointer never moves."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app_id": {"type": "string"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "button": {"type": "string", "enum": ["left", "right", "middle"]},
                "clicks": {"type": "integer", "minimum": 1, "maximum": 4},
            },
            "required": ["app_id", "x", "y"],
            "additionalProperties": False,
        },
    },
    "desktop.scroll": {
        "name": "desktop.scroll",
        "description": (
            "Scroll the attached window with posted WM_MOUSEWHEEL messages. "
            "Negative clicks scroll down/content-toward-user, positive up."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app_id": {"type": "string"},
                "x": {"type": "integer"},
                "y": {"type": "integer"},
                "clicks": {"type": "integer", "minimum": -100, "maximum": 100},
                "horizontal": {
                    "type": "integer",
                    "description": "Horizontal wheel delta; omit for vertical scrolling.",
                },
            },
            "required": ["app_id"],
            "additionalProperties": False,
        },
    },
    "desktop.launch_app": {
        "name": "desktop.launch_app",
        "description": (
            "Consequential: launch one installed Start Menu shortcut, minimized "
            "and without activation by default. Requires a one-shot human "
            "approval token bound to the exact action digest."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "shortcut_path": {"type": "string"},
                "title_contains": {"type": "string"},
                "visible": {"type": "boolean"},
            },
            "required": ["shortcut_path"],
            "additionalProperties": False,
        },
    },
    "desktop.show_window": {
        "name": "desktop.show_window",
        "description": (
            "Restore the attached window. mode=restore_noactivate (default) keeps "
            "the user's foreground untouched; mode=restore_visible is "
            "consequential and requires a one-shot human approval."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "app_id": {"type": "string"},
                "mode": {"type": "string", "enum": ["restore_noactivate", "restore_visible"]},
            },
            "required": ["app_id"],
            "additionalProperties": False,
        },
    },
    "desktop.open_terminal": {
        "name": "desktop.open_terminal",
        "description": (
            "Consequential: open a real visible terminal window (wt/pwsh/"
            "powershell/cmd). Requires a one-shot human approval token. Returns "
            "process identity and the matched window."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "shell": {"type": "string", "enum": ["auto", "cmd", "powershell", "pwsh", "wt"]},
                "keep_open": {"type": "boolean"},
            },
            "additionalProperties": False,
        },
    },
}


__all__ = [
    "ACTION_KINDS",
    "CONSEQUENTIAL_ACTIONS",
    "ComputerUseAdapter",
    "TOOL_SPECS",
    "ToolResult",
    "action_digest",
    "control_contract",
    "is_consequential",
    "normalize_payload",
    "ApprovalRejected",
    "ApprovalStore",
    "DesktopSecurityError",
]
