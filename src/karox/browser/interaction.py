"""Browser session runtime mixin: owner thread, dispatch and lifecycle seams."""

from __future__ import annotations

import math
import re
import time
from typing import Any, Mapping, Optional
from .credentials import (
    BrowserCredentialInjectionError,
    inject_browser_credential,
)
from .errors import (
    BrowserError,
    BrowserSecurityError,
)
from .guards import (
    _PAYMENT_CREDENTIAL_HINT,
)
from .probes import (
    SECRET_FIELD_HINT as _SECRET_INPUT_HINT,
)
from .pageperf import (
    INPUT_TIMEOUT_SECONDS,
)
from .contract import (
    CancelToken,
)


_KEY_NAMES = {
    "enter": "Enter",
    "return": "Enter",
    "numpadenter": "NumpadEnter",
    "tab": "Tab",
    "escape": "Escape",
    "esc": "Escape",
    "backspace": "Backspace",
    "delete": "Delete",
    "del": "Delete",
    "insert": "Insert",
    "ins": "Insert",
    "space": "Space",
    "spacebar": "Space",
    "home": "Home",
    "end": "End",
    "pageup": "PageUp",
    "pgup": "PageUp",
    "pagedown": "PageDown",
    "pgdn": "PageDown",
    "arrowup": "ArrowUp",
    "up": "ArrowUp",
    "arrowdown": "ArrowDown",
    "down": "ArrowDown",
    "arrowleft": "ArrowLeft",
    "left": "ArrowLeft",
    "arrowright": "ArrowRight",
    "right": "ArrowRight",
    "capslock": "CapsLock",
    "numlock": "NumLock",
    "scrolllock": "ScrollLock",
    "printscreen": "PrintScreen",
    "pause": "Pause",
    "contextmenu": "ContextMenu",
}

#: Canonical Playwright key names for the spellings models actually emit.
#: The race showed three refused presses for a single "-": punctuation is a
#: printable key, and an unknown spelling should be repaired here rather than
#: cost a model turn.
for _number in range(1, 13):
    _KEY_NAMES[f"f{_number}"] = f"F{_number}"


_MODIFIER_NAMES = {
    "ctrl": "Control",
    "shift": "Shift",
    "alt": "Alt",
    "opt": "Alt",
    "option": "Alt",
    "meta": "Meta",
    "cmd": "Meta",
    "command": "Meta",
    "win": "Meta",
    "super": "Meta",
}

_SHORTCUT_MODIFIERS = frozenset({"Control", "Meta", "Alt"})

def _on_screen(box: Mapping[str, float], view: Mapping[str, int]) -> bool:
    """Whether the box's centre lies inside the viewport."""

    width, height = view.get("width"), view.get("height")
    if not width or not height:
        return False
    x = box["x"] + box["width"] / 2
    y = box["y"] + box["height"] / 2
    return 0 <= x < width and 0 <= y < height


def _drag_offset(payload: Mapping[str, Any], name: str) -> float:
    value = payload.get(name, 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BrowserError(f"drag {name} must be a number of pixels")
    value = float(value)
    if not math.isfinite(value) or abs(value) > 10_000:
        raise BrowserError(f"drag {name} must be a finite number up to 10000 px")
    return value

def _drag_inset(payload: Mapping[str, Any], name: str, extent: float) -> float:
    """Offset of the drag start inside its element; the centre by default."""

    if payload.get(name) is None:
        return extent / 2
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise BrowserError(f"drag {name} must be a number of pixels inside the element")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= extent:
        raise BrowserError(f"drag {name} must lie inside the element (0..{int(extent)} px)")
    return value

def _normalize_key(raw: str) -> str:
    """Canonicalize ``ctrl + enter`` / ``return`` / ``"-"`` into a key name."""

    key = re.sub(r"\s*\+\s*", "+", raw.strip())
    parts = [part for part in key.split("+") if part]
    if not parts:
        return key
    normalized: list[str] = []
    for part in parts[:-1]:
        normalized.append(_MODIFIER_NAMES.get(part.lower(), part))
    final = parts[-1]
    if len(final) == 1:
        normalized.append(final)
    else:
        normalized.append(_KEY_NAMES.get(final.lower(), final))
    return "+".join(normalized)

def _key_is_valid(key: str) -> bool:
    parts = key.split("+")
    if not parts or any(not part for part in parts):
        return False
    final = parts[-1]
    if len(final) == 1:
        return final.isprintable() and not final.isspace() and len(key) <= 32
    return bool(re.fullmatch(r"[A-Za-z0-9]{1,20}", final)) and len(key) <= 32

def _key_inserts_text(key: str) -> bool:
    """Whether this press writes a character instead of running a command."""

    parts = key.split("+")
    final = parts[-1]
    if len(final) != 1 or not final.isprintable() or final.isspace():
        return False
    modifiers = {_MODIFIER_NAMES.get(part.lower(), part) for part in parts[:-1]}
    return not (modifiers & _SHORTCUT_MODIFIERS)


class BrowserInteractionMixin:
    """Methods composed into BrowserSession; see browser/session.py."""

    def _click(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        page = self._active_page()
        locator = self._locator(payload, page)
        self._assert_action_safe(locator)
        timeout = self._timeout_ms(deadline_seconds, default=INPUT_TIMEOUT_SECONDS)

        def work() -> None:
            locator.click(timeout=timeout)

        recording = self._record_motion(locator)
        started = time.monotonic()
        try:
            self._with_stale_recovery(work, payload, deadline_seconds, cancel)
        except Exception as exc:  # noqa: BLE001 - re-raised, maybe explained
            explained = self._input_timeout("click", exc)
            if explained is exc:
                raise
            raise explained from None
        return self._input_timing(
            "click",
            started,
            {"clicked": True, "target": str(payload.get("selector") or payload.get("ref"))},
            recording,
        )

    def _dblclick(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        page = self._active_page()
        locator = self._locator(payload, page)
        timeout = self._timeout_ms(deadline_seconds, default=INPUT_TIMEOUT_SECONDS)

        def work() -> None:
            locator.dblclick(timeout=timeout)

        recording = self._record_motion(locator)
        started = time.monotonic()
        try:
            self._with_stale_recovery(work, payload, deadline_seconds, cancel)
        except Exception as exc:  # noqa: BLE001 - re-raised, maybe explained
            explained = self._input_timeout("double click", exc)
            if explained is exc:
                raise
            raise explained from None
        return self._input_timing(
            "double click",
            started,
            {"dblclicked": True, "target": str(payload.get("selector") or payload.get("ref"))},
            recording,
        )

    def _drag(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        """Press, move and release the mouse the way a person drags.

        Orbit cameras, sliders, canvases and drag-and-drop lists listen to real
        pointer input with pointer capture and momentum; an event dispatched by
        a script skips both, so a page that answered it could still fail a user
        (and a check) that drags for real. Starts at the element's centre, or
        at x/y inside it, and ends dx/dy pixels away or on to_selector/to_ref.
        """

        self._assert_agent_input_allowed()
        page = self._active_page()
        locator = self._locator(payload, page)
        self._assert_action_safe(locator)
        destination = None
        if payload.get("to_selector") is not None or payload.get("to_ref") is not None:
            destination = self._locator(
                {
                    "selector": payload.get("to_selector"),
                    "ref": payload.get("to_ref"),
                    "frame": payload.get("frame"),
                },
                page,
            )
            self._assert_action_safe(destination)
            dx = dy = 0.0
        else:
            dx = _drag_offset(payload, "dx")
            dy = _drag_offset(payload, "dy")
            if abs(dx) < 2 and abs(dy) < 2:
                raise BrowserError("drag needs dx/dy of at least 2 px, or to_selector/to_ref")
        steps = payload.get("steps", 12)
        if isinstance(steps, bool) or not isinstance(steps, int) or not 1 <= steps <= 60:
            raise BrowserError("drag steps must be an integer from 1 to 60")
        timeout = self._timeout_ms(deadline_seconds, default=INPUT_TIMEOUT_SECONDS)
        points: dict[str, list[int]] = {}

        def work() -> None:
            # Scrolling waits for the element to hold still over two animation
            # frames; on a canvas that draws one frame in 5 s (software WebGL)
            # that wait outlasts the timeout. An element already on screen
            # needs no scroll.
            box = locator.bounding_box(timeout=timeout)
            view = page.viewport_size or {}
            if not box or not _on_screen(box, view):
                locator.scroll_into_view_if_needed(timeout=timeout)
                box = locator.bounding_box(timeout=timeout)
            if not box:
                raise BrowserError("drag start has no box: the element is hidden or detached")
            start_x = box["x"] + _drag_inset(payload, "x", box["width"])
            start_y = box["y"] + _drag_inset(payload, "y", box["height"])
            if destination is not None:
                end_box = destination.bounding_box(timeout=timeout)
                if not end_box:
                    raise BrowserError("drag end has no box: the element is hidden or detached")
                end_x = end_box["x"] + end_box["width"] / 2
                end_y = end_box["y"] + end_box["height"] / 2
            else:
                end_x, end_y = start_x + dx, start_y + dy
            page.mouse.move(start_x, start_y)
            page.mouse.down()
            try:
                page.mouse.move(end_x, end_y, steps=steps)
            finally:
                page.mouse.up()
            points["from"] = [round(start_x), round(start_y)]
            points["to"] = [round(end_x), round(end_y)]

        started = time.monotonic()
        self._with_stale_recovery(work, payload, deadline_seconds, cancel)
        return self._input_timing(
            "drag",
            started,
            {
                "dragged": True,
                "target": str(payload.get("selector") or payload.get("ref")),
                **points,
            },
        )

    def _hover(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        page = self._active_page()
        locator = self._locator(payload, page)
        timeout = self._timeout_ms(deadline_seconds)

        def work() -> None:
            locator.hover(timeout=timeout)

        self._with_stale_recovery(work, payload, deadline_seconds, cancel)
        return {"hovered": True, "target": str(payload.get("selector") or payload.get("ref"))}

    def _focus(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        page = self._active_page()
        locator = self._locator(payload, page)
        locator.focus(timeout=self._timeout_ms(deadline_seconds))
        return {"focused": True, "target": str(payload.get("selector") or payload.get("ref"))}

    def _clear(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        page = self._active_page()
        locator = self._locator(payload, page)
        metadata = self._locator_metadata(locator)
        if metadata.get("secret") == "true" or metadata.get("type") == "password":
            raise BrowserSecurityError(
                "credential fields are cleared only through fill_credential or user takeover"
            )
        locator.fill("", timeout=self._timeout_ms(deadline_seconds))
        return {"cleared": True, "target": str(payload.get("selector") or payload.get("ref"))}

    def _fill(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        value = self._text(payload.get("value"))
        page = self._active_page()
        locator = self._locator(payload, page)
        metadata = self._guard_fill_target(locator, value)
        secret_field = (
            metadata.get("secret") == "true"
            or metadata.get("type") == "password"
            or bool(
                _SECRET_INPUT_HINT.search(
                    " ".join(
                        metadata.get(key, "")
                        for key in ("type", "name", "id", "aria", "placeholder")
                    )
                )
            )
        )
        if secret_field:
            self._mask_secret_element(locator)
        timeout = self._timeout_ms(deadline_seconds)

        def work() -> None:
            locator.fill(value, timeout=timeout)

        self._with_stale_recovery(work, payload, deadline_seconds, cancel)
        return {
            "filled": True,
            "target": str(payload.get("selector") or payload.get("ref")),
            "value_length": len(value),
            "protected_field": secret_field,
        }

    def _type(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        text = self._text(payload.get("text", payload.get("value", "")), name="text")
        delay_ms = payload.get("delay_ms", 0)
        if not isinstance(delay_ms, (int, float)) or not 0 <= delay_ms <= 1000:
            raise BrowserError("delay_ms must be between 0 and 1000")
        page = self._active_page()
        locator = self._locator(payload, page)
        metadata = self._guard_fill_target(locator, text)
        secret_field = metadata.get("type") == "password" or metadata.get("secret") == "true"
        if secret_field:
            self._mask_secret_element(locator)
        timeout = self._timeout_ms(deadline_seconds)

        def work() -> None:
            locator.press_sequentially(text, delay=float(delay_ms), timeout=timeout)

        self._with_stale_recovery(work, payload, deadline_seconds, cancel)
        return {
            "typed": True,
            "target": str(payload.get("selector") or payload.get("ref")),
            "chars": len(text),
            "protected_field": secret_field,
        }

    def _fill_credential(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        """Fill one login field from an opaque OS-keyring browser reference.

        The caller chooses only the target element, credential reference, and
        field name. The resolved value never enters tool arguments/results,
        logs, snapshots, or exception text.
        """

        self._assert_agent_input_allowed()
        selector = self._selector(payload.get("selector"))
        reference = payload.get("reference")
        field = payload.get("field")
        if not isinstance(reference, str) or not reference.startswith("os-keyring:browser/"):
            raise BrowserError(
                "browser credential reference must use os-keyring:browser/<name>"
            )
        if field not in {"username", "password"}:
            raise BrowserError("browser credential field must be username or password")
        if (
            self.policy.allowed_credential_refs
            and reference not in self.policy.allowed_credential_refs
        ):
            raise BrowserSecurityError(
                "browser credential reference is not approved for this browser session"
            )
        locator = self._active_page().locator(selector).first
        metadata = self._locator_metadata(locator)
        if not metadata or not metadata.get("type"):
            raise BrowserSecurityError(
                "credential input field could not be inspected safely; use user takeover"
            )
        descriptor = " ".join(
            metadata.get(key, "") for key in ("type", "name", "id", "aria", "placeholder")
        )
        field_type = metadata.get("type", "").lower()
        if _PAYMENT_CREDENTIAL_HINT.search(descriptor):
            raise BrowserSecurityError(
                "browser credential injection never fills payment, card, CVV/CVC, "
                "IBAN, or billing fields; use user takeover"
            )
        if field == "password":
            if field_type != "password" and not _SECRET_INPUT_HINT.search(descriptor):
                raise BrowserSecurityError(
                    "password credential may only be injected into a password/credential field"
                )
        else:
            if field_type == "password" or _SECRET_INPUT_HINT.search(descriptor):
                raise BrowserSecurityError(
                    "username credential may not be injected into a password/secret field"
                )
            if field_type not in {"email", "text", "input", "tel"}:
                raise BrowserSecurityError(
                    "username credential target must be a text, email, or username-like input"
                )
        try:
            result = inject_browser_credential(
                locator=locator,
                credential_store=self._credential_store,
                reference=reference,
                field=field,
                timeout_ms=self._timeout_ms(deadline_seconds),
            )
        except BrowserCredentialInjectionError:
            raise
        return {"selector": selector, **result.to_dict()}

    def _select(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        raw = payload.get("value")
        if isinstance(raw, str):
            values = [raw]
        elif isinstance(raw, list) and raw and all(isinstance(item, str) for item in raw):
            values = list(raw)
        else:
            raise BrowserError("select value must be a non-empty string or string array")
        page = self._active_page()
        locator = self._locator(payload, page)
        timeout = self._timeout_ms(deadline_seconds)

        def work() -> None:
            locator.select_option(values, timeout=timeout)

        self._with_stale_recovery(work, payload, deadline_seconds, cancel)
        return {
            "selected": True,
            "target": str(payload.get("selector") or payload.get("ref")),
            "count": len(values),
        }

    def _press(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        key = _normalize_key(self._text(payload.get("key"), name="key"))
        if not _key_is_valid(key):
            raise BrowserError(
                "key must be a key name like Enter or Control+A, or one "
                "printable character"
            )
        page = self._active_page()
        if _key_inserts_text(key):
            # A character press writes into the focused element exactly like
            # ``fill``, so it must pass the same secret-field gate. Without
            # this, the password guard was bypassed one character at a time.
            self._assert_press_text_allowed(payload, page)
        selector = payload.get("selector")
        timeout = self._timeout_ms(deadline_seconds, default=INPUT_TIMEOUT_SECONDS)
        started = time.monotonic()
        if selector is None and payload.get("ref") is None:
            if key.lower() in {"enter", "numpadenter", "space", "tab"}:
                self._assert_action_safe_page(page)
            page.keyboard.press(key)
            return self._input_timing(
                "key press", started, {"pressed": True, "key": key, "target": "page"}
            )
        locator = self._locator(payload, page)
        if key.lower() in {"enter", "numpadenter", "space"}:
            self._assert_action_safe(locator)

        def work() -> None:
            locator.press(key, timeout=timeout)

        try:
            self._with_stale_recovery(work, payload, deadline_seconds, cancel)
        except Exception as exc:  # noqa: BLE001 - re-raised, maybe explained
            explained = self._input_timeout("key press", exc)
            if explained is exc:
                raise
            raise explained from None
        return self._input_timing(
            "key press",
            started,
            {
                "pressed": True,
                "key": key,
                "target": str(payload.get("selector") or payload.get("ref")),
            },
        )

    def _set_checked(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        checked = payload.get("checked")
        if not isinstance(checked, bool):
            raise BrowserError("set_checked requires boolean checked")
        page = self._active_page()
        locator = self._locator(payload, page)
        timeout = self._timeout_ms(deadline_seconds)

        def work() -> None:
            locator.set_checked(checked, timeout=timeout)

        self._with_stale_recovery(work, payload, deadline_seconds, cancel)
        return {
            "set_checked": True,
            "checked": checked,
            "target": str(payload.get("selector") or payload.get("ref")),
        }

    def _scroll(
        self,
        payload: Mapping[str, Any],
        deadline_seconds: float,
        cancel: Optional[CancelToken],
    ) -> dict[str, Any]:
        page = self._active_page()
        direction = str(payload.get("direction", "down")).lower()
        if direction not in {"up", "down", "left", "right", "top", "bottom", "to_selector"}:
            raise BrowserError(
                "scroll direction must be up, down, left, right, top, bottom, or to_selector"
            )
        amount = payload.get("amount", 600)
        if not isinstance(amount, (int, float)) or not 1 <= amount <= 20_000:
            raise BrowserError("scroll amount must be between 1 and 20000 pixels")
        selector = payload.get("selector")
        if direction == "to_selector":
            if selector is None:
                raise BrowserError("to_selector requires a selector")
            locator = self._locator(payload, page)
            locator.scroll_into_view_if_needed(timeout=self._timeout_ms(deadline_seconds))
        else:
            script = """
            ([direction, amount]) => {
              const dx = direction === 'left' ? -amount : direction === 'right' ? amount : 0;
              const dy = direction === 'up' ? -amount : direction === 'down' ? amount : 0;
              if (direction === 'top') window.scrollTo(0, 0);
              else if (direction === 'bottom') window.scrollTo(0, document.documentElement.scrollHeight);
              else window.scrollBy(dx, dy);
              return true;
            }
            """
            page.evaluate(script, [direction, float(amount)])
        state = page.evaluate(
            """() => ({x: window.scrollX, y: window.scrollY,
                       width: document.documentElement.scrollWidth,
                       height: document.documentElement.scrollHeight,
                       viewport_height: window.innerHeight})"""
        )
        return {"scrolled": direction, **{str(k): v for k, v in (state or {}).items()}}
