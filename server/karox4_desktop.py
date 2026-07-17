"""KaroX 4.0 — Phase 5: screen/window capture, short GIF recording,
strictly opt-in desktop input (off by default, non-interference).
"""
from __future__ import annotations

import base64
import io
import time
from typing import Any, Dict, List, Literal, Optional, Tuple

from fastapi import Header, HTTPException
from pydantic import BaseModel, Field

import repo_tools as core
import karox4_core as k4

app = core.app
IS_WINDOWS = core.IS_WINDOWS if hasattr(core, "IS_WINDOWS") else (__import__("os").name == "nt")

# --------------------------------------------------------------------------
# Window lookup (Windows, ctypes)
# --------------------------------------------------------------------------

def find_window(title_substring: str) -> Optional[Dict[str, Any]]:
    if not IS_WINDOWS:
        return None
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: List[Dict[str, Any]] = []
    needle = title_substring.lower()

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value
        if needle in title.lower():
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            found.append({
                "hwnd": int(hwnd),
                "title": title,
                "rect": (rect.left, rect.top, rect.right, rect.bottom),
            })
        return True

    user32.EnumWindows(enum_proc, 0)
    if not found:
        return None
    found.sort(key=lambda w: (w["rect"][2] - w["rect"][0]) * (w["rect"][3] - w["rect"][1]), reverse=True)
    return found[0]


def _grab(bbox: Optional[Tuple[int, int, int, int]] = None):
    try:
        from PIL import ImageGrab  # type: ignore
    except ImportError:
        raise HTTPException(status_code=501, detail="Pillow не установлен: pip install pillow")
    return ImageGrab.grab(bbox=bbox, all_screens=True)


def _encode_png(img, max_dimension: int) -> Tuple[str, int, int]:
    max_dimension = max(200, min(int(max_dimension), 3840))
    if max(img.size) > max_dimension:
        img.thumbnail((max_dimension, max_dimension))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=True)
    return base64.b64encode(buf.getvalue()).decode("ascii"), img.size[0], img.size[1]


@app.get("/desktop/screenshot")
def desktop_screenshot(
    window_title: Optional[str] = None,
    region: Optional[str] = None,  # "left,top,right,bottom"
    max_dimension: int = 1600,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    core.check_auth(x_api_key)
    bbox: Optional[Tuple[int, int, int, int]] = None
    window_info = None
    if window_title:
        window_info = find_window(window_title)
        if window_info is None:
            raise HTTPException(status_code=404, detail=f"Окно не найдено: {window_title}" if IS_WINDOWS else "Поиск окна поддерживается только на Windows")
        bbox = window_info["rect"]
    elif region:
        try:
            parts = [int(x) for x in region.split(",")]
            if len(parts) != 4:
                raise ValueError
            bbox = (parts[0], parts[1], parts[2], parts[3])
        except ValueError:
            raise HTTPException(status_code=400, detail="region должен быть 'left,top,right,bottom'")
    img = _grab(bbox)
    encoded, w, h = _encode_png(img, max_dimension)
    core.audit("desktop_screenshot", {"windowTitle": (window_title or "")[:100], "region": region, "width": w, "height": h})
    return {
        "mimeType": "image/png",
        "width": w,
        "height": h,
        "window": {"title": window_info["title"], "rect": window_info["rect"]} if window_info else None,
        "contentBase64": encoded,
    }


@app.get("/desktop/record")
def desktop_record(
    seconds: float = 3,
    fps: float = 2,
    window_title: Optional[str] = None,
    max_dimension: int = 800,
    x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
):
    core.check_auth(x_api_key)
    try:
        from PIL import ImageGrab  # noqa: F401  # type: ignore
    except ImportError:
        raise HTTPException(status_code=501, detail="Pillow не установлен: pip install pillow")
    seconds = max(0.5, min(float(seconds), 15))
    fps = max(0.5, min(float(fps), 5))
    max_dimension = max(200, min(int(max_dimension), 1600))
    bbox = None
    if window_title:
        info = find_window(window_title)
        if info is None:
            raise HTTPException(status_code=404, detail=f"Окно не найдено: {window_title}")
        bbox = info["rect"]
    frames = []
    interval = 1.0 / fps
    deadline = time.time() + seconds
    while time.time() < deadline and len(frames) < 75:
        img = _grab(bbox)
        if max(img.size) > max_dimension:
            img.thumbnail((max_dimension, max_dimension))
        frames.append(img.convert("P", palette=1))
        time.sleep(max(0.0, interval - 0.05))
    if not frames:
        raise HTTPException(status_code=500, detail="Не удалось захватить кадры")
    buf = io.BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=int(interval * 1000), loop=0)
    data = buf.getvalue()
    if len(data) > 8_000_000:
        raise HTTPException(status_code=413, detail="GIF слишком большой — уменьшите seconds/fps/max_dimension")
    core.audit("desktop_record", {"seconds": seconds, "fps": fps, "frames": len(frames), "bytes": len(data)})
    return {"mimeType": "image/gif", "frames": len(frames), "bytes": len(data), "contentBase64": base64.b64encode(data).decode("ascii")}

# --------------------------------------------------------------------------
# Desktop input — strictly opt-in per session, target window only
# --------------------------------------------------------------------------

class AllowInputBody(BaseModel):
    enabled: bool


@app.post("/desktop/allow-input")
def desktop_allow_input(body: AllowInputBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    k4.SESSION_FLAGS["desktopInputAllowed"] = bool(body.enabled)
    core.audit("desktop_allow_input", {"enabled": bool(body.enabled)})
    return {"ok": True, "desktopInputAllowed": k4.SESSION_FLAGS["desktopInputAllowed"]}


class DesktopInputBody(BaseModel):
    windowTitle: str
    action: Literal["click", "type", "key"]
    x: Optional[int] = None  # window-relative
    y: Optional[int] = None
    text: Optional[str] = None
    key: Optional[str] = None


@app.post("/desktop/input")
def desktop_input(body: DesktopInputBody, x_api_key: Optional[str] = Header(None, alias="X-API-Key")):
    core.check_auth(x_api_key)
    core.ensure_not_read_only("POST /desktop/input")
    if not k4.SESSION_FLAGS.get("desktopInputAllowed"):
        raise HTTPException(status_code=403, detail="Ввод в окна выключен (non-interference). Опт-ин на сессию: POST /desktop/allow-input {\"enabled\": true}")
    if not IS_WINDOWS:
        raise HTTPException(status_code=501, detail="Ввод в окна поддерживается только на Windows")
    try:
        import pyautogui  # type: ignore
    except ImportError:
        raise HTTPException(status_code=501, detail="pyautogui не установлен: pip install pyautogui")
    info = find_window(body.windowTitle)
    if info is None:
        raise HTTPException(status_code=404, detail=f"Окно не найдено: {body.windowTitle}")
    import ctypes
    ctypes.windll.user32.SetForegroundWindow(info["hwnd"])
    time.sleep(0.3)
    left, top, right, bottom = info["rect"]
    result: Dict[str, Any] = {"window": info["title"], "action": body.action}
    if body.action == "click":
        if body.x is None or body.y is None:
            raise HTTPException(status_code=400, detail="Нужны x и y (относительно окна)")
        abs_x, abs_y = left + int(body.x), top + int(body.y)
        if not (left <= abs_x <= right and top <= abs_y <= bottom):
            raise HTTPException(status_code=403, detail="Клик вне границ целевого окна запрещён")
        pyautogui.click(abs_x, abs_y)
        result.update({"x": abs_x, "y": abs_y})
    elif body.action == "type":
        if not body.text:
            raise HTTPException(status_code=400, detail="Нужен text")
        pyautogui.typewrite(body.text[:500], interval=0.02)
        result["typedChars"] = len(body.text[:500])
    elif body.action == "key":
        if not body.key:
            raise HTTPException(status_code=400, detail="Нужен key")
        allowed_keys = {"enter", "esc", "tab", "space", "up", "down", "left", "right", "f1", "f2", "f3", "f5", "e", "w", "a", "s", "d"}
        if body.key.lower() not in allowed_keys:
            raise HTTPException(status_code=403, detail=f"Клавиша не в allowlist: {body.key}")
        pyautogui.press(body.key.lower())
        result["key"] = body.key.lower()
    core.audit("desktop_input", result)
    k4.emit_event("desktop_input", result)
    return {"ok": True, **result}
