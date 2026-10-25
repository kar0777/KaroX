"""Targeted background input: window messages to one pinned HWND tree.

Nothing here synthesizes global input state. Text arrives as ``WM_CHAR``
units, keys as ``WM_KEYDOWN``/``WM_KEYUP`` with scan-code lparams, pointer
actions as the move/down/up message triples, and scrolling as ``WM_MOUSEWHEEL``
— all posted to the app's own internal focus target or the deepest owned child
under a point. The user's foreground window, caret and pointer never move, and
a message can only reach a window whose HWND, PID and process creation time
still match the attachment.
"""

from __future__ import annotations

from typing import Any, Optional

from .deadline import Deadline
from .errors import ComputerUseError, DesktopSecurityError, InvalidActionError
from .identity import WindowIdentity

MAX_TEXT = 20_000

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
WM_CHAR = 0x0102
WM_MOUSEMOVE = 0x0200
WM_MOUSEHWHEEL = 0x020E
WM_MOUSEWHEEL = 0x020A

VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_MENU = 0x12  # Alt

_NAMED_KEYS = {
    "enter": 0x0D, "return": 0x0D, "esc": 0x1B, "escape": 0x1B, "tab": 0x09,
    "space": 0x20, "backspace": 0x08, "delete": 0x2E, "del": 0x2E,
    "insert": 0x2D, "home": 0x24, "end": 0x23, "pageup": 0x21, "pgup": 0x21,
    "pagedown": 0x22, "pgdn": 0x22, "left": 0x25, "up": 0x26, "right": 0x27,
    "down": 0x28, "printscreen": 0x2C, "pause": 0x13,
}
_PUNCTUATION_VKS = {
    ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF,
    "`": 0xC0, "[": 0xDB, "\\": 0xDC, "]": 0xDD, "'": 0xDE,
}
_MODIFIERS = {"ctrl": VK_CONTROL, "control": VK_CONTROL, "shift": VK_SHIFT, "alt": VK_MENU}
_GLOBAL_MODIFIERS = {"win", "windows", "super", "meta", "cmd", "command"}

_BUTTON_MESSAGES = {
    "left": (0x0201, 0x0202, 0x0001),
    "right": (0x0204, 0x0205, 0x0002),
    "middle": (0x0207, 0x0208, 0x0010),
}


def parse_key(key: str) -> tuple[list[int], int]:
    """Parse ``key`` into (modifiers, virtual key); rejects global combos."""

    if not isinstance(key, str):
        raise InvalidActionError("key must be a string")
    normalized = key.strip().casefold()
    if not normalized:
        raise InvalidActionError("key must be a non-empty string")
    parts = [part.strip() for part in normalized.split("+")]
    if any(not part for part in parts):
        raise InvalidActionError(f"unsupported desktop key: {key!r}")
    for part in parts[:-1]:
        if part in _GLOBAL_MODIFIERS:
            raise InvalidActionError(
                f"global OS shortcut modifiers are refused: {part!r}"
            )
        if part not in _MODIFIERS:
            raise InvalidActionError(f"unsupported desktop key: {key!r}")
    modifiers = [_MODIFIERS[part] for part in parts[:-1]]
    main = parts[-1]
    if main in _GLOBAL_MODIFIERS:
        raise InvalidActionError(f"global OS shortcut modifiers are refused: {main!r}")
    if main in _MODIFIERS and not modifiers:
        raise InvalidActionError(f"unsupported desktop key: {key!r}")
    if main in _NAMED_KEYS:
        return modifiers, _NAMED_KEYS[main]
    if main.startswith("f") and main[1:].isdigit() and 1 <= int(main[1:]) <= 24:
        return modifiers, 0x6F + int(main[1:])
    if len(main) == 1:
        if main.isalpha() or main.isdigit():
            return modifiers, ord(main.upper())
        if main in _PUNCTUATION_VKS:
            return modifiers, _PUNCTUATION_VKS[main]
    raise InvalidActionError(f"unsupported desktop key: {key!r}")


def _owned_hwnd(probe: Any, ident: WindowIdentity, hwnd: int) -> bool:
    """True only for HWNDs that stay inside the pinned app/process."""

    if not hwnd:
        return False
    try:
        if not probe.is_window(hwnd):
            return False
        if int(probe.window_pid(hwnd) or 0) != ident.pid:
            return False
        return hwnd == ident.hwnd or int(probe.root_ancestor(hwnd) or 0) == ident.hwnd
    except Exception:
        return False


def resolve_keyboard_target(probe: Any, ident: WindowIdentity) -> tuple[int, str]:
    """(hwnd, source): the app thread's internal focus, without touching global state.

    ``source`` is ``thread_focus`` when the app's own GUI thread reports a
    focus/caret/active window inside the pinned tree, and ``window_fallback``
    when it reports none — an honest signal that the app may discard keys.
    """

    thread_id, pid, focus, caret, active = probe.thread_focus(ident.hwnd)
    if not thread_id or int(pid) != ident.pid:
        raise DesktopSecurityError("attached application process identity changed")
    for candidate in (focus, caret, active):
        if _owned_hwnd(probe, ident, int(candidate or 0)):
            return int(candidate), "thread_focus"
    return ident.hwnd, "window_fallback"


def _key_lparam(probe: Any, vk: int, *, up: bool) -> int:
    scan = 0
    mapper = getattr(probe, "map_virtual_key", None)
    if callable(mapper):
        try:
            scan = int(mapper(vk)) & 0xFF
        except Exception:
            scan = 0
    lparam = 1 | (scan << 16)
    if up:
        lparam |= 0xC0000000
    return lparam


def _post(probe: Any, hwnd: int, msg: int, wparam: int, lparam: int, what: str) -> None:
    if not probe.post_message(hwnd, msg, wparam, lparam):
        raise ComputerUseError(f"Windows rejected background {what} input")


def type_text(
    probe: Any,
    ident: WindowIdentity,
    text: str,
    *,
    deadline: Deadline,
    pace_ms: int = 0,
) -> dict[str, Any]:
    """Post the text as WM_CHAR units to the app's internal focus target."""

    if not isinstance(text, str) or not text:
        raise InvalidActionError("text must be a non-empty string")
    if len(text) > MAX_TEXT:
        raise InvalidActionError(f"text must contain 1-{MAX_TEXT} characters")
    if "\x00" in text:
        raise InvalidActionError("text must not contain NUL characters")
    deadline.check("desktop type_text")
    target, focus_source = resolve_keyboard_target(probe, ident)
    raw = text.encode("utf-16-le", errors="strict")
    units = len(raw) // 2
    for index in range(0, len(raw), 2):
        if index % 512 == 0:
            deadline.check("desktop type_text")
        unit = raw[index] | (raw[index + 1] << 8)
        _post(probe, target, WM_CHAR, unit, 1, "text")
        if pace_ms > 0:
            probe.sleep_ms(min(int(pace_ms), 250))
    return {
        "typed": True,
        "chars": len(text),
        "units": units,
        "target_hwnd": target,
        "focus_source": focus_source,
    }


def press_key(
    probe: Any,
    ident: WindowIdentity,
    key: str,
    *,
    deadline: Deadline,
) -> dict[str, Any]:
    """Post one key or modifier combo to the app's internal focus target."""

    modifiers, vk = parse_key(key)
    deadline.check("desktop press_key")
    target, focus_source = resolve_keyboard_target(probe, ident)
    for modifier in modifiers:
        _post(probe, target, WM_KEYDOWN, modifier, _key_lparam(probe, modifier, up=False), "key")
    _post(probe, target, WM_KEYDOWN, vk, _key_lparam(probe, vk, up=False), "key")
    _post(probe, target, WM_KEYUP, vk, _key_lparam(probe, vk, up=True), "key")
    for modifier in reversed(modifiers):
        _post(probe, target, WM_KEYUP, modifier, _key_lparam(probe, modifier, up=True), "key")
    result: dict[str, Any] = {
        "pressed": True,
        "key": str(key).strip().casefold(),
        "target_hwnd": target,
        "focus_source": focus_source,
    }
    if modifiers:
        # Posted messages never update the target thread's live key state —
        # making them would require global input injection, which is refused.
        # Apps that poll GetKeyState therefore may not see the modifier, and
        # an app-side TranslateMessage may derive the unmodified character.
        result["modifier_caveat"] = (
            "posted modifier combos are only recognised by apps that read the "
            "modifier from the message stream; apps polling live key state may "
            "treat the key as unmodified"
        )
    return result


def resolve_point_target(
    probe: Any, ident: WindowIdentity, x: int, y: int
) -> tuple[int, int, int, int, int]:
    """Map a window-relative point to (target, client_x, client_y, screen_x, screen_y).

    Descends to the deepest owned child under the point (skipping invisible,
    disabled and transparent children), then refuses anything that falls
    outside that child's client area.
    """

    if isinstance(x, bool) or not isinstance(x, int) or isinstance(y, bool) or not isinstance(y, int):
        raise InvalidActionError("coordinates must be integers")
    if not 0 <= x < ident.width or not 0 <= y < ident.height:
        raise DesktopSecurityError("point is outside the attached window bounds")
    screen_x = ident.rect[0] + x
    screen_y = ident.rect[1] + y
    target = ident.hwnd
    for _ in range(12):
        point = probe.screen_to_client(target, screen_x, screen_y)
        if point is None:
            break
        child = int(probe.child_from_point(target, int(point[0]), int(point[1])) or 0)
        if not child or child == target or not _owned_hwnd(probe, ident, child):
            break
        target = child
    point = probe.screen_to_client(target, screen_x, screen_y)
    if point is None:
        raise ComputerUseError("Windows could not map background pointer coordinates")
    client_x, client_y = int(point[0]), int(point[1])
    client_rect = probe.client_rect(target)
    if client_rect is not None:
        if not (client_rect[0] <= client_x < client_rect[2] and client_rect[1] <= client_y < client_rect[3]):
            raise DesktopSecurityError("background pointer action is outside the target client area")
    return target, client_x, client_y, screen_x, screen_y


def _pack(x: int, y: int) -> int:
    return (int(x) & 0xFFFF) | ((int(y) & 0xFFFF) << 16)


def click(
    probe: Any,
    ident: WindowIdentity,
    x: int,
    y: int,
    *,
    button: str = "left",
    clicks: int = 1,
    deadline: Optional[Deadline] = None,
) -> dict[str, Any]:
    """Background click: move + down/up message triples, no pointer movement."""

    deadline = deadline or Deadline.after(10.0)
    deadline.check("desktop click")
    if button not in _BUTTON_MESSAGES:
        raise InvalidActionError("button must be left, right or middle")
    if isinstance(clicks, bool) or not isinstance(clicks, int) or not 1 <= clicks <= 4:
        raise InvalidActionError("clicks must be an integer 1-4")
    target, client_x, client_y, _, _ = resolve_point_target(probe, ident, x, y)
    down, up, flag = _BUTTON_MESSAGES[button]
    packed = _pack(client_x, client_y)
    _post(probe, target, WM_MOUSEMOVE, 0, packed, "mouse")
    for _ in range(clicks):
        deadline.check("desktop click")
        _post(probe, target, down, flag, packed, "mouse")
        _post(probe, target, up, 0, packed, "mouse")
    return {
        "clicked": True,
        "button": button,
        "clicks": clicks,
        "target_hwnd": target,
        "client": [client_x, client_y],
    }


def scroll(
    probe: Any,
    ident: WindowIdentity,
    *,
    x: Optional[int] = None,
    y: Optional[int] = None,
    clicks: int = -3,
    horizontal: int = 0,
    deadline: Optional[Deadline] = None,
) -> dict[str, Any]:
    """Background wheel scroll posted to the child under the point."""

    deadline = deadline or Deadline.after(10.0)
    deadline.check("desktop scroll")
    if isinstance(clicks, bool) or not isinstance(clicks, int) or clicks == 0 or abs(clicks) > 100:
        raise InvalidActionError("clicks must be a non-zero integer within +/-100")
    point_x = ident.width // 2 if x is None else x
    point_y = ident.height // 2 if y is None else y
    target, _, _, screen_x, screen_y = resolve_point_target(probe, ident, point_x, point_y)
    msg = WM_MOUSEHWHEEL if horizontal else WM_MOUSEWHEEL
    delta = 120 if clicks > 0 else -120
    wparam = (delta << 16)
    packed = _pack(screen_x, screen_y)  # wheel messages carry screen coordinates
    for _ in range(abs(clicks)):
        deadline.check("desktop scroll")
        _post(probe, target, msg, wparam, packed, "scroll")
    return {
        "scrolled": True,
        "clicks": clicks,
        "delta_total": 120 * clicks,
        "direction": "horizontal" if horizontal else "vertical",
        "method": "wheel-post",
        "target_hwnd": target,
    }


__all__ = [
    "MAX_TEXT",
    "click",
    "parse_key",
    "press_key",
    "resolve_keyboard_target",
    "resolve_point_target",
    "scroll",
    "type_text",
]
