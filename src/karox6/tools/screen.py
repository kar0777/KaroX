"""Screenshots for a model: the whole desktop or one window, as a PNG.

Capture only: nothing here moves, focuses, restores or sends input to any window.
Windows uses GDI through ctypes (no dependencies); macOS and Linux call the system's
screenshot tool.
"""

from __future__ import annotations

import asyncio
import shutil
import struct
import subprocess
import sys
import tempfile
import zlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

from karox6.tools.base import ToolContext, ToolResult

DEFAULT_WIDTH = 1600
MAX_WIDTH = 2560
MIN_WIDTH = 320
# Base64 makes this ~2 MB of reply; hosted clients drop much bigger tool results.
PNG_LIMIT = 1_500_000
MAX_WINDOWS = 300
CAPTURE_TIMEOUT_S = 30
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class ScreenError(Exception):
    """A capture that cannot be done; the message is meant for the model."""


@dataclass(slots=True)
class Shot:
    png: bytes
    width: int
    height: int
    what: str  # what was captured, for the reply text


@dataclass(slots=True)
class Window:
    hwnd: int
    title: str
    left: int
    top: int
    width: int
    height: int
    minimized: bool


class ScreenshotTool:
    name = "screenshot"
    description = (
        "See the user's screen: a PNG of the whole desktop (all monitors), or of one window "
        "given by 'window' (part of its title), captured even when other windows cover it "
        "and without bringing it to the front. list_windows: true lists the visible windows "
        "instead. Capture only: nothing is clicked, typed or moved."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "window": {
                "type": "string",
                "description": "part of a window title, case-insensitive (default: the whole "
                "desktop)",
            },
            "max_width": {
                "type": "integer",
                "description": f"shrink the image to at most this many pixels wide (default "
                f"{DEFAULT_WIDTH}, max {MAX_WIDTH})",
            },
            "list_windows": {
                "type": "boolean",
                "description": "true: list visible window titles and sizes as text, no image",
            },
        },
    }
    read_only = True

    async def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        try:
            if args.get("list_windows"):
                return ToolResult(await asyncio.to_thread(windows_text))
            width = max(MIN_WIDTH, min(int(args.get("max_width") or DEFAULT_WIDTH), MAX_WIDTH))
            window = str(args.get("window") or "").strip() or None
            shot = await asyncio.wait_for(
                asyncio.to_thread(capture, window, width), CAPTURE_TIMEOUT_S
            )
        except ScreenError as exc:
            return ToolResult(f"Error: {exc}", is_error=True)
        except TimeoutError:
            return ToolResult(f"Error: no capture after {CAPTURE_TIMEOUT_S} s.", is_error=True)
        text = (
            f"Captured {shot.what}; the image is {shot.width}x{shot.height} px "
            f"({len(shot.png) // 1024} KB PNG)."
        )
        return ToolResult(text, meta={"image_png": shot.png})


def capture(window: str | None = None, max_width: int = DEFAULT_WIDTH) -> Shot:
    """The whole desktop, or the topmost visible window whose title contains ``window``."""
    if sys.platform == "win32":
        return _win_capture(window, max_width)
    return _posix_capture(window, max_width)


def windows_text() -> str:
    if sys.platform != "win32":
        raise ScreenError(f"list_windows is not supported on this platform ({sys.platform})")
    windows = _win_windows()
    if not windows:
        return "No visible windows with a title."
    lines = [
        f'"{w.title}" {w.width}x{w.height}' + (" (minimized)" if w.minimized else "")
        for w in windows
    ]
    return "Visible windows, topmost first (title, size in pixels):\n" + "\n".join(lines)


# --- PNG ---------------------------------------------------------------------------------


def encode_png(width: int, height: int, rgb: bytes, level: int = 6) -> bytes:
    """An 8-bit truecolor PNG of ``rgb``: rows top to bottom, 3 bytes per pixel."""
    stride = width * 3
    if width <= 0 or height <= 0 or len(rgb) != stride * height:
        raise ValueError(f"{len(rgb)} bytes is not a {width}x{height} RGB image")
    # Filter type 0 on every row: it compressed UI screenshots best in tests, and the
    # others need per-byte arithmetic.
    raw = b"".join(b"\0" + rgb[y * stride : (y + 1) * stride] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", header)
        + _chunk(b"IDAT", zlib.compress(raw, level))
        + _chunk(b"IEND", b"")
    )


def _chunk(kind: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(kind + data)
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)


def png_size(png: bytes) -> tuple[int, int]:
    if len(png) < 24 or not png.startswith(PNG_SIGNATURE) or png[12:16] != b"IHDR":
        raise ScreenError("the screenshot tool did not produce a PNG")
    width, height = struct.unpack(">II", png[16:24])
    return int(width), int(height)


def bgra_to_rgb(bgra: bytes) -> bytes:
    rgb = bytearray(len(bgra) // 4 * 3)
    rgb[0::3] = bgra[2::4]
    rgb[1::3] = bgra[1::4]
    rgb[2::3] = bgra[0::4]
    return bytes(rgb)


def _smaller(width: int, size: int) -> int:
    """A width whose PNG should fit PNG_LIMIT (bytes grow with the pixel count)."""
    return max(MIN_WIDTH, int(width * min(0.85, (PNG_LIMIT / size) ** 0.5)))


# --- Windows -----------------------------------------------------------------------------

PW_RENDERFULLCONTENT = 2  # renders DirectX/Chromium content that plain PrintWindow leaves black
SRCCOPY = 0x00CC0020
CAPTUREBLT = 0x40000000  # include layered windows (menus, tooltips) in a screen copy
HALFTONE = 4
DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 76, 77, 78, 79
SM_CMONITORS = 80
PER_MONITOR_AWARE_V2 = -4
PER_MONITOR_AWARE = -3


@cache
def _win32() -> Any:
    """user32/gdi32/dwmapi with declared signatures: ctypes' int default truncates
    64-bit handles. Own WinDLL objects, so other code's prototypes stay untouched."""
    if sys.platform != "win32":  # also tells mypy on other platforms to skip the rest
        raise ScreenError(f"not supported on this platform ({sys.platform})")
    import ctypes
    from ctypes import wintypes as w
    from types import SimpleNamespace

    class BitmapInfoHeader(ctypes.Structure):
        _fields_ = [
            ("biSize", w.DWORD),
            ("biWidth", w.LONG),
            ("biHeight", w.LONG),
            ("biPlanes", w.WORD),
            ("biBitCount", w.WORD),
            ("biCompression", w.DWORD),
            ("biSizeImage", w.DWORD),
            ("biXPelsPerMeter", w.LONG),
            ("biYPelsPerMeter", w.LONG),
            ("biClrUsed", w.DWORD),
            ("biClrImportant", w.DWORD),
        ]

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
    dwmapi = ctypes.WinDLL("dwmapi")
    enum_proc = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    c_int, ptr = ctypes.c_int, ctypes.c_void_p
    signatures: list[tuple[Any, str, Any, list[Any]]] = [
        (user32, "GetDC", w.HDC, [w.HWND]),
        (user32, "ReleaseDC", c_int, [w.HWND, w.HDC]),
        (user32, "PrintWindow", w.BOOL, [w.HWND, w.HDC, w.UINT]),
        (user32, "GetSystemMetrics", c_int, [c_int]),
        (user32, "EnumWindows", w.BOOL, [enum_proc, w.LPARAM]),
        (user32, "IsWindowVisible", w.BOOL, [w.HWND]),
        (user32, "IsIconic", w.BOOL, [w.HWND]),
        (user32, "IsHungAppWindow", w.BOOL, [w.HWND]),
        (user32, "GetWindowTextLengthW", c_int, [w.HWND]),
        (user32, "GetWindowTextW", c_int, [w.HWND, w.LPWSTR, c_int]),
        (user32, "GetWindowRect", w.BOOL, [w.HWND, ctypes.POINTER(w.RECT)]),
        (gdi32, "CreateCompatibleDC", w.HDC, [w.HDC]),
        (gdi32, "CreateCompatibleBitmap", w.HBITMAP, [w.HDC, c_int, c_int]),
        (gdi32, "CreateDIBSection", w.HBITMAP, [w.HDC, ptr, w.UINT, ptr, w.HANDLE, w.DWORD]),
        (gdi32, "SelectObject", w.HGDIOBJ, [w.HDC, w.HGDIOBJ]),
        (gdi32, "DeleteObject", w.BOOL, [w.HGDIOBJ]),
        (gdi32, "DeleteDC", w.BOOL, [w.HDC]),
        (gdi32, "SetStretchBltMode", c_int, [w.HDC, c_int]),
        (gdi32, "SetBrushOrgEx", w.BOOL, [w.HDC, c_int, c_int, ptr]),
        (gdi32, "StretchBlt", w.BOOL, [w.HDC, *[c_int] * 4, w.HDC, *[c_int] * 4, w.DWORD]),
        (gdi32, "GdiFlush", w.BOOL, []),
        (dwmapi, "DwmGetWindowAttribute", ctypes.c_long, [w.HWND, w.DWORD, ptr, w.DWORD]),
    ]
    for dll, name, restype, argtypes in signatures:
        function = getattr(dll, name)
        function.restype, function.argtypes = restype, argtypes
    try:  # Windows 10 1607 and later
        user32.SetThreadDpiAwarenessContext.restype = ptr
        user32.SetThreadDpiAwarenessContext.argtypes = [ptr]
        set_dpi = user32.SetThreadDpiAwarenessContext
    except AttributeError:
        set_dpi = None
    return SimpleNamespace(
        ctypes=ctypes,
        w=w,
        user32=user32,
        gdi32=gdi32,
        dwmapi=dwmapi,
        enum_proc=enum_proc,
        header=BitmapInfoHeader,
        set_dpi=set_dpi,
    )


@contextmanager
def _dpi_aware(api: Any) -> Iterator[None]:
    """Physical pixels on every monitor: a DPI-unaware thread sees a scaled display as
    smaller and gets a shrunken, blurred copy of it."""
    previous = None
    if api.set_dpi is not None:  # this thread only, restored afterwards
        previous = api.set_dpi(PER_MONITOR_AWARE_V2) or api.set_dpi(PER_MONITOR_AWARE)
    else:  # older Windows: process-wide, which only works once
        try:
            api.ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)
        except (OSError, AttributeError):
            api.user32.SetProcessDPIAware()
    try:
        yield
    finally:
        if previous:
            api.set_dpi(previous)


def _win_windows() -> list[Window]:
    api = _win32()
    with _dpi_aware(api):
        return _list_windows(api)


def _list_windows(api: Any) -> list[Window]:
    """Visible, uncloaked top-level windows with a title, topmost first."""
    user32 = api.user32
    handles: list[int] = []

    def visit(hwnd: int, _param: int) -> bool:
        handles.append(hwnd)  # nothing that can raise: ctypes swallows callback errors
        return True

    user32.EnumWindows(api.enum_proc(visit), 0)
    found: list[Window] = []
    for hwnd in handles:
        if len(found) >= MAX_WINDOWS:
            break
        if not hwnd or not user32.IsWindowVisible(hwnd) or _cloaked(api, hwnd):
            continue
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            continue
        buffer = api.ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, length + 1)
        title = buffer.value.strip()
        left, top, right, bottom = _frame(api, hwnd)
        if title and right > left and bottom > top:
            minimized = bool(user32.IsIconic(hwnd))
            found.append(Window(hwnd, title, left, top, right - left, bottom - top, minimized))
    return found


def _cloaked(api: Any, hwnd: int) -> bool:
    """Hidden by the shell (suspended Store apps, other virtual desktops) though "visible"."""
    value = api.w.DWORD()
    size = api.ctypes.sizeof(value)
    ref = api.ctypes.byref(value)
    return api.dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ref, size) == 0 and bool(
        value.value
    )


def _frame(api: Any, hwnd: int) -> tuple[int, int, int, int]:
    """The visible frame: GetWindowRect also counts the invisible resize borders."""
    rect = api.w.RECT()
    ref, size = api.ctypes.byref(rect), api.ctypes.sizeof(rect)
    if api.dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ref, size) != 0:
        api.user32.GetWindowRect(hwnd, ref)
    return rect.left, rect.top, rect.right, rect.bottom


def _find(windows: list[Window], query: str) -> tuple[Window, int]:
    wanted = query.casefold()
    matches = [w for w in windows if wanted in w.title.casefold()]
    exact = [w for w in matches if w.title.casefold() == wanted]
    if not matches:
        titles = ", ".join(f'"{w.title}"' for w in windows[:15])
        raise ScreenError(
            f"no visible window title contains {query!r}; visible windows: {titles or 'none'}"
        )
    return (exact or matches)[0], len(matches)


def _win_capture(query: str | None, max_width: int) -> Shot:
    api = _win32()
    user32 = api.user32
    with _dpi_aware(api):
        screen = user32.GetDC(None)
        if not screen:
            raise ScreenError("Windows gave no screen to capture (no interactive desktop?)")
        try:
            if query is None:
                metric = user32.GetSystemMetrics
                left, top = metric(SM_XVIRTUALSCREEN), metric(SM_YVIRTUALSCREEN)
                width, height = metric(SM_CXVIRTUALSCREEN), metric(SM_CYVIRTUALSCREEN)
                shot = _stretched_png(api, screen, (left, top, width, height), max_width)
                monitors = metric(SM_CMONITORS)
                plural = "s" * (monitors != 1)
                shot.what = f"the whole desktop ({monitors} monitor{plural}, {shot.what})"
                return shot
            window, count = _find(_list_windows(api), query)
            if window.minimized:
                raise ScreenError(
                    f'window "{window.title}" is minimized; it cannot be captured until the '
                    "user restores it (this tool never moves or restores windows)"
                )
            shot = _window_png(api, screen, window, max_width)
            many = f"; the topmost of {count} matching windows" if count > 1 else ""
            shot.what = f'window "{window.title}" ({shot.what}{many})'
            return shot
        finally:
            user32.ReleaseDC(None, screen)


def _window_png(api: Any, screen: int, window: Window, max_width: int) -> Shot:
    """PrintWindow into a memory bitmap: the window paints itself, so windows covering it
    do not show and it is not brought to the front."""
    user32, gdi32 = api.user32, api.gdi32
    outer = api.w.RECT()
    found = user32.GetWindowRect(window.hwnd, api.ctypes.byref(outer))
    full_w, full_h = outer.right - outer.left, outer.bottom - outer.top
    if not found or full_w <= 0 or full_h <= 0:  # closed since it was listed
        raise ScreenError(f"the window '{window.title}' is gone")
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, full_w, full_h)
    if not memory or not bitmap:
        _free(gdi32, memory, bitmap, None)
        raise ScreenError("Windows could not allocate a bitmap for the window")
    old = gdi32.SelectObject(memory, bitmap)
    try:
        # A hung window would block PrintWindow (it waits for the window to paint).
        painted = not user32.IsHungAppWindow(window.hwnd) and (
            user32.PrintWindow(window.hwnd, memory, PW_RENDERFULLCONTENT)
            or user32.PrintWindow(window.hwnd, memory, 0)
        )
        if painted:
            # PrintWindow draws from the outer corner; crop to the visible frame.
            left, top = window.left - outer.left, window.top - outer.top
            return _stretched_png(api, memory, (left, top, window.width, window.height), max_width)
        # The window did not paint itself: copy its area of the screen instead.
        screen_rect = (window.left, window.top, window.width, window.height)
        shot = _stretched_png(api, screen, screen_rect, max_width)
        shot.what += "; copied from the screen, so windows on top of it show too"
        return shot
    finally:
        _free(gdi32, memory, bitmap, old)


def _stretched_png(api: Any, source: int, rect: tuple[int, int, int, int], max_width: int) -> Shot:
    """Copy ``rect`` of the source DC, scaled by GDI to at most ``max_width`` (and less
    while the PNG is over PNG_LIMIT): Python only touches the scaled pixels."""
    src_w, src_h = rect[2], rect[3]
    if src_w <= 0 or src_h <= 0:
        raise ScreenError(f"nothing to capture: the area is {src_w}x{src_h} px")
    width = min(max_width, src_w)
    while True:
        height = max(1, round(src_h * width / src_w))
        bgra = _stretch(api, source, rect, width, height)
        png = encode_png(width, height, bgra_to_rgb(bgra))
        if len(png) <= PNG_LIMIT or width <= MIN_WIDTH:
            return Shot(png, width, height, f"{src_w}x{src_h} px")
        width = _smaller(width, len(png))


def _stretch(
    api: Any, source: int, rect: tuple[int, int, int, int], width: int, height: int
) -> bytes:
    ctypes, gdi32 = api.ctypes, api.gdi32
    header = api.header(biSize=ctypes.sizeof(api.header), biWidth=width, biHeight=-height)
    header.biPlanes, header.biBitCount = 1, 32  # top-down 32-bit BI_RGB: no row padding
    bits = ctypes.c_void_p()
    target = gdi32.CreateCompatibleDC(source)
    dib = gdi32.CreateDIBSection(target, ctypes.byref(header), 0, ctypes.byref(bits), None, 0)
    if not target or not dib or not bits.value:
        _free(gdi32, target, dib, None)
        raise ScreenError(f"Windows could not allocate a {width}x{height} bitmap")
    old = gdi32.SelectObject(target, dib)
    try:
        gdi32.SetStretchBltMode(target, HALFTONE)
        gdi32.SetBrushOrgEx(target, 0, 0, None)  # HALFTONE needs it reset
        left, top, src_w, src_h = rect
        rop = SRCCOPY | CAPTUREBLT
        if not gdi32.StretchBlt(target, 0, 0, width, height, source, left, top, src_w, src_h, rop):
            raise ScreenError("Windows could not copy the screen (is the desktop locked?)")
        gdi32.GdiFlush()
        return bytes(ctypes.string_at(bits.value, width * height * 4))
    finally:
        _free(gdi32, target, dib, old)


def _free(gdi32: Any, dc: int | None, bitmap: int | None, old: int | None) -> None:
    if dc and old:
        gdi32.SelectObject(dc, old)
    if bitmap:
        gdi32.DeleteObject(bitmap)
    if dc:
        gdi32.DeleteDC(dc)


# --- macOS and Linux ---------------------------------------------------------------------


def _posix_capture(query: str | None, max_width: int) -> Shot:
    if query is not None:
        raise ScreenError(
            f"capturing one window by title is not supported on this platform ({sys.platform}); "
            "call screenshot without 'window' for the whole screen"
        )
    with tempfile.TemporaryDirectory(prefix="karox-screen-") as tmp:
        path = Path(tmp) / "screen.png"
        if sys.platform == "darwin":
            command, what = ["screencapture", "-x", "-t", "png", str(path)], "the main display"
        elif shutil.which("grim"):  # Wayland
            command, what = ["grim", str(path)], "the screen"
        elif shutil.which("import"):  # ImageMagick on X11
            command, what = ["import", "-window", "root", str(path)], "the screen"
        else:
            raise ScreenError(
                "no screenshot tool found: install grim (Wayland) or ImageMagick (X11)"
            )
        _run(command)
        if not path.is_file():
            raise ScreenError(
                f"{command[0]} saved no image (on macOS, allow Screen Recording for the "
                "terminal that runs KaroX)"
            )
        png = path.read_bytes()
        full_w, full_h = width, height = png_size(png)
        target = min(max_width, width)
        for attempt in range(5):  # bounded: a resizer could ignore the size asked for
            if width <= target and len(png) <= PNG_LIMIT:
                break
            if width <= target:
                if width <= MIN_WIDTH:
                    break
                target = _smaller(width, len(png))
            out = Path(tmp) / f"small{attempt}.png"
            _resize(path, out, target)
            png = out.read_bytes()
            width, height = png_size(png)
        return Shot(png, width, height, f"{what} ({full_w}x{full_h} px)")


def _resize(source: Path, out: Path, width: int) -> None:
    if sys.platform == "darwin":
        command = ["sips", "--resampleWidth", str(width), str(source), "--out", str(out)]
    elif tool := shutil.which("magick") or shutil.which("convert"):
        command = [tool, str(source), "-resize", f"{width}x", str(out)]
    else:
        raise ScreenError(
            f"the screenshot is wider than {width} px or over {PNG_LIMIT // 1000} KB and "
            "there is nothing to shrink it with: install ImageMagick"
        )
    _run(command)


def _run(command: list[str]) -> None:
    try:
        done = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ScreenError(f"{command[0]} failed: {exc}") from exc
    if done.returncode != 0:
        detail = (done.stderr or done.stdout).strip()[-400:]
        raise ScreenError(f"{command[0]} exited with code {done.returncode}: {detail}")
