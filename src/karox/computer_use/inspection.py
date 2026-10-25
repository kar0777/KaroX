"""Background UI inspection: the Win32 control tree of one pinned window.

Nodes are enumerated with ``EnumChildWindows`` (direct children per level, own
recursion with depth and node caps) and read with hung-proof
``SendMessageTimeoutW`` calls, so a frozen application can stall one node for
at most its timeout instead of the whole inspection. Text is returned to the
requesting model capped, while the persisted evidence stays text-free.
"""

from __future__ import annotations

import hashlib
from typing import Any

from .deadline import Deadline
from .identity import WindowIdentity
from .redact import scrub

_WS_VISIBLE = 0x10000000
_WS_DISABLED = 0x08000000
_MAX_DEPTH = 8
_DEFAULT_MAX_NODES = 250
_TEXT_LIMIT = 512
_PROBE_TIMEOUT_MS = 250


def window_health(probe: Any, ident: WindowIdentity) -> dict[str, Any]:
    """Non-invasive liveness signals for the pinned HWND."""

    responsive: Any = None
    try:
        ok, _ = probe.send_message_timeout(ident.hwnd, 0x0000, 0, 0, _PROBE_TIMEOUT_MS)
        responsive = bool(ok)
    except Exception:
        responsive = None
    hung: Any = None
    try:
        hung = bool(probe.is_hung(ident.hwnd))
    except Exception:
        hung = None
    foreground = False
    try:
        foreground = int(probe.foreground_hwnd() or 0) == ident.hwnd
    except Exception:
        foreground = False
    return {
        "window_message_responsive": responsive,
        "windows_reports_hung": hung,
        "is_foreground": foreground,
        "probe_timeout_ms": _PROBE_TIMEOUT_MS,
    }


def process_sample(probe: Any, pid: int) -> dict[str, Any]:
    """Bounded process facts: identity signals only, never app content."""

    info = None
    try:
        info = probe.process_info(pid) if pid else None
    except Exception:
        info = None
    if info is None:
        return {"available": False}
    create_time, executable = info
    return {
        "available": True,
        "process_id": pid,
        "create_time": create_time,
        "executable_sha256_16": hashlib.sha256(
            (executable or "").replace("\\", "/").casefold().encode("utf-8")
        ).hexdigest()[:16],
    }


def _walk(
    probe: Any,
    hwnd: int,
    pid: int,
    depth: int,
    budget: list[int],
    nodes: list[dict[str, Any]],
    deadline: Deadline,
    include_text: bool,
) -> None:
    if depth > _MAX_DEPTH or budget[0] <= 0:
        return
    try:
        children = probe.enum_child_windows(hwnd)
    except Exception:
        return
    for child in children:
        if budget[0] <= 0:
            return
        deadline.check("desktop ui inspection")
        budget[0] -= 1
        styles = 0
        try:
            styles = int(probe.window_styles(child.hwnd) or 0)
        except Exception:
            styles = 0
        text = ""
        if include_text:
            try:
                text = scrub(probe.get_text(child.hwnd, _TEXT_LIMIT), limit=_TEXT_LIMIT)
            except Exception:
                text = ""
        class_name = ""
        try:
            class_name = str(probe.class_name(child.hwnd) or "")
        except Exception:
            class_name = ""
        nodes.append(
            {
                "window_id": child.hwnd,
                "parent": hwnd,
                "depth": depth,
                "class": class_name[:120],
                "text": text,
                "rect": list(child.rect),
                "width": max(0, child.rect[2] - child.rect[0]),
                "height": max(0, child.rect[3] - child.rect[1]),
                "visible": bool(styles & _WS_VISIBLE) if styles else child.visible,
                "enabled": not bool(styles & _WS_DISABLED),
                "same_process": int(child.pid) == int(pid),
            }
        )
        _walk(probe, child.hwnd, pid, depth + 1, budget, nodes, deadline, include_text)


def inspect_window(
    probe: Any,
    ident: WindowIdentity,
    *,
    deadline: Deadline,
    max_nodes: int = _DEFAULT_MAX_NODES,
    include_text: bool = True,
) -> dict[str, Any]:
    """Return the control tree, window health and redacted evidence."""

    deadline.check("desktop ui inspection")
    nodes: list[dict[str, Any]] = []
    budget = [max(1, min(int(max_nodes), 1000))]
    _walk(probe, ident.hwnd, ident.pid, 1, budget, nodes, deadline, include_text)
    health = window_health(probe, ident)
    text_nodes = [node for node in nodes if node.get("text")]
    tree: dict[str, Any] = {
        "window": ident.public(),
        "health": health,
        "process": process_sample(probe, ident.pid),
        "nodes": nodes,
        "truncated": budget[0] <= 0,
    }
    classes: dict[str, int] = {}
    for node in nodes:
        key = node["class"] or "?"
        classes[key] = classes.get(key, 0) + 1
    tree["evidence"] = {
        "node_count": len(nodes),
        "classes": classes,
        "text_node_count": len(text_nodes),
        "redacted": {
            "node_text_total_chars": sum(len(node.get("text", "")) for node in text_nodes),
            "control_text_persisted": False,
        },
        "health": health,
    }
    return tree


__all__ = ["inspect_window", "process_sample", "window_health"]
