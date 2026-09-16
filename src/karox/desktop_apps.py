"""Guarded native Windows application control for hosted KaroX sessions.

The hosted MCP schema remains stable: ``workspace_worker.execute_browser_command``
routes ``app.*`` actions here. Desktop access is distinct from browser access,
requires the elevated profile plus ``user_confirmed=true`` for initial discovery
or attachment, and is scoped to the current in-memory session. Bindings pin HWND
and PID so a recycled handle cannot redirect input into another process.
"""
from __future__ import annotations

import ctypes
import hashlib
import io
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .core import CoreError, InvalidCommand
from .models import AccessProfile, Capability
from .policy import PolicyDenied

_APP_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_MAX_TEXT = 20_000
_MAX_WINDOWS = 80
_BUILTINS = {"traycer": ("Traycer", "traycer")}


def _background_control_contract() -> dict[str, Any]:
    """Machine-readable invariants for non-intrusive native app control."""

    return {
        "background_control": True,
        "input_backend": "win32-window-messages",
        "focus_steal_blocked": True,
        "global_keyboard_injection": False,
        "global_pointer_injection": False,
        "screen_capture_backend": "win32-printwindow",
        "screen_capture_mode": "on-demand",
        "background_polling": False,
    }


class DesktopAppError(CoreError):
    """The selected native application could not be used safely.

    This is a guarded runtime failure, not an untyped RuntimeError. Keeping it in
    the CoreError family lets hosted/MCP transports serialize the real failure
    instead of collapsing a safe Win32 refusal into an opaque TaskGroup error.
    """


class DesktopAppSecurityError(PermissionError):
    """A desktop action would escape the selected application/window boundary."""


@dataclass(frozen=True)
class Window:
    hwnd: int
    title: str
    rect: tuple[int, int, int, int]
    pid: int
    minimized: bool = False

    @property
    def width(self) -> int:
        return max(0, self.rect[2] - self.rect[0])

    @property
    def height(self) -> int:
        return max(0, self.rect[3] - self.rect[1])

    def public(self) -> dict[str, Any]:
        return {
            "window_id": self.hwnd,
            "title": self.title[:300],
            "process_id": self.pid,
            "rect": list(self.rect),
            "width": self.width,
            "height": self.height,
            "minimized": self.minimized,
        }


@dataclass(frozen=True)
class Binding:
    app_id: str
    hwnd: int
    pid: int
    access: str
    attached_at: float


def _user32() -> Any:
    if os.name != "nt":
        raise DesktopAppError("native desktop application control is currently Windows-only")
    return ctypes.windll.user32


def _windows() -> list[Window]:
    from ctypes import wintypes

    user32 = _user32()
    found: list[Window] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd: int, _lp: int) -> bool:
        if len(found) >= _MAX_WINDOWS:
            return False
        if not user32.IsWindowVisible(hwnd):
            return True
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(min(length + 1, 4097))
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        title = buffer.value.strip()
        if not title:
            return True
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        found.append(
            Window(
                int(hwnd),
                title,
                bounds,
                int(pid.value),
                bool(user32.IsIconic(hwnd)),
            )
        )
        return True

    user32.EnumWindows(enum_proc, 0)
    return sorted(found, key=lambda item: item.width * item.height, reverse=True)


def _find_bound(binding: Binding) -> Window:
    for window in _windows():
        if window.hwnd != binding.hwnd:
            continue
        if window.pid != binding.pid:
            raise DesktopAppSecurityError(
                "bound window handle was reused by a different process"
            )
        return window
    raise DesktopAppError("attached application window is no longer open")


def _focus(window: Window) -> None:
    """Compatibility no-op: desktop automation never steals user foreground."""
    del window


def _wait_windows(milliseconds: int) -> None:
    """Bounded synchronous Win32 wait used only inside an explicit user action.

    This is deliberately not a Python sleep/polling worker: desktop control has
    no background thread, timer, capture loop, or input loop. A restore/launch
    call may briefly wait for Windows to apply the action it just requested.
    """
    if os.name != "nt":
        raise DesktopAppError("native desktop application control is currently Windows-only")
    kernel32 = ctypes.windll.kernel32
    kernel32.Sleep.argtypes = [ctypes.c_ulong]
    kernel32.Sleep.restype = None
    kernel32.Sleep(max(0, int(milliseconds)))


def _restore_without_activation(window: Window) -> None:
    """Show a pinned minimized window without activating or focusing it."""
    if os.name != "nt":
        raise DesktopAppError("native desktop application control is currently Windows-only")
    from ctypes import wintypes

    user32 = _user32()
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.ShowWindowAsync.restype = wintypes.BOOL
    # SW_SHOWNOACTIVATE restores the latest size/position while explicitly
    # keeping the user's foreground window unchanged. Never use SW_RESTORE (9)
    # here: Windows may activate the restored app and steal keyboard focus.
    user32.ShowWindowAsync(window.hwnd, 4)
    for _ in range(20):
        if not user32.IsIconic(window.hwnd):
            return
        _wait_windows(25)
    raise DesktopAppError("Windows did not restore the application without activation")


def _start_menu_program_roots() -> tuple[Path, ...]:
    roots: list[Path] = []
    appdata = os.environ.get("APPDATA")
    programdata = os.environ.get("ProgramData")
    if appdata:
        roots.append(Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    if programdata:
        roots.append(Path(programdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs")
    return tuple(roots)


def _validated_start_menu_shortcut(raw: Any) -> Path:
    """Accept only an existing .lnk below a Windows Start Menu Programs root."""
    if not isinstance(raw, str) or not raw.strip():
        raise InvalidCommand("app.launch shortcut_path must be a non-empty string")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute() or candidate.suffix.casefold() != ".lnk":
        raise DesktopAppSecurityError("app.launch accepts only an absolute Start Menu .lnk")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise DesktopAppError("the requested Start Menu shortcut does not exist") from exc
    allowed = False
    for root in _start_menu_program_roots():
        try:
            resolved.relative_to(root.resolve(strict=False))
            allowed = True
            break
        except ValueError:
            continue
    if not allowed:
        raise DesktopAppSecurityError("app.launch shortcut must stay inside Start Menu Programs")
    return resolved


def _launch_start_menu_shortcut(raw: Any, title_contains: Any = "") -> dict[str, Any]:
    """Launch one installed Start Menu shortcut minimized and without activation.

    No executable arguments or shell text are accepted. Windows resolves the
    already-installed .lnk. SW_SHOWMINNOACTIVE requests a minimized launch
    without activation; the target application may still decide to self-focus,
    so the result reports this as a requested contract rather than a guarantee.
    """
    if os.name != "nt":
        raise DesktopAppError("native desktop application launch is currently Windows-only")
    shortcut = _validated_start_menu_shortcut(raw)
    title = str(title_contains or "").strip()
    shell32 = ctypes.windll.shell32
    shell32.ShellExecuteW.argtypes = [
        ctypes.c_void_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_wchar_p,
        ctypes.c_int,
    ]
    shell32.ShellExecuteW.restype = ctypes.c_void_p
    # SW_SHOWMINNOACTIVE = 7.
    result = shell32.ShellExecuteW(None, "open", str(shortcut), None, str(shortcut.parent), 7)
    code = int(result or 0)
    if code <= 32:
        raise DesktopAppError(f"Windows could not launch the Start Menu shortcut (code {code})")

    matched = None
    if title:
        deadline = time.monotonic() + 8.0
        needle = title.casefold()
        while time.monotonic() < deadline:
            matched = next((window for window in _windows() if needle in window.title.casefold()), None)
            if matched is not None:
                break
            _wait_windows(200)
    return {
        "launched": True,
        "shortcut_name": shortcut.name,
        "activation_requested": False,
        "matched_window": matched.public() if matched is not None else None,
        "launch_contract": {
            "arguments_allowed": False,
            "start_menu_only": True,
            "show_command": "SW_SHOWMINNOACTIVE",
        },
    }


def _window_health(window: Window) -> dict[str, Any]:
    """Read non-invasive Win32 liveness signals for the pinned HWND."""
    if os.name != "nt":
        return {
            "window_message_responsive": None,
            "windows_reports_hung": None,
            "is_foreground": None,
            "probe_timeout_ms": 250,
            "available": False,
        }
    from ctypes import wintypes

    user32 = _user32()
    user32.IsHungAppWindow.argtypes = [wintypes.HWND]
    user32.IsHungAppWindow.restype = wintypes.BOOL
    user32.GetForegroundWindow.argtypes = []
    user32.GetForegroundWindow.restype = wintypes.HWND
    user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
    user32.SendMessageTimeoutW.restype = wintypes.LPARAM
    response = ctypes.c_size_t()
    answered = bool(user32.SendMessageTimeoutW(window.hwnd, 0x0000, 0, 0, 0x0001 | 0x0002, 250, ctypes.byref(response)))
    return {
        "window_message_responsive": answered,
        "windows_reports_hung": bool(user32.IsHungAppWindow(window.hwnd)),
        "is_foreground": int(user32.GetForegroundWindow() or 0) == window.hwnd,
        "probe_timeout_ms": 250,
    }


def _process_sample(pid: int) -> dict[str, Any]:
    """Return a bounded process-tree activity sample without reading app content."""
    try:
        import psutil  # type: ignore
    except ImportError:
        return {"available": False}
    try:
        root = psutil.Process(pid)
        processes = [root, *root.children(recursive=True)]
        cpu_seconds = 0.0
        rss_bytes = 0
        live = 0
        for process in processes:
            try:
                times = process.cpu_times()
                cpu_seconds += float(times.user) + float(times.system)
                rss_bytes += int(process.memory_info().rss)
                live += 1
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return {
            "available": True,
            "process_tree_size": live,
            "cpu_seconds_total": round(cpu_seconds, 6),
            "rss_bytes_total": rss_bytes,
        }
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        return {"available": True, "process_tree_size": 0, "exited": True}


def _capture_window_png(window: Window, backend: str = "printwindow") -> tuple[bytes, int, int]:
    """Capture the pinned HWND off-screen without reading desktop pixels.

    Screen-region capture is intentionally forbidden here. If another program is
    covering Traycer, a screen grab would capture that other program and leak it
    into the KaroX session. PrintWindow renders only the pinned HWND and does not
    activate it, move the pointer, or synthesize keyboard input.
    """
    from ctypes import wintypes

    try:
        from PIL import Image  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise DesktopAppError("desktop screenshots require Pillow") from exc

    width, height = window.width, window.height
    if width <= 0 or height <= 0:
        raise DesktopAppError("attached application window has invalid capture bounds")

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD),
            ("biWidth", wintypes.LONG),
            ("biHeight", wintypes.LONG),
            ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD),
            ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD),
            ("biXPelsPerMeter", wintypes.LONG),
            ("biYPelsPerMeter", wintypes.LONG),
            ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class RGBQUAD(ctypes.Structure):
        _fields_ = [
            ("rgbBlue", ctypes.c_ubyte),
            ("rgbGreen", ctypes.c_ubyte),
            ("rgbRed", ctypes.c_ubyte),
            ("rgbReserved", ctypes.c_ubyte),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", RGBQUAD * 1)]

    user32 = _user32()
    gdi32 = ctypes.windll.gdi32
    # ctypes defaults pointer-returning Win32 functions to c_int. Declare the
    # pointer-sized signatures explicitly so 64-bit HDC/HBITMAP values cannot be
    # truncated on machines where the handles happen to be large.
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    user32.ReleaseDC.restype = ctypes.c_int
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.PrintWindow.restype = wintypes.BOOL
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteObject.restype = wintypes.BOOL
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    gdi32.DeleteDC.restype = wintypes.BOOL
    gdi32.GetDIBits.argtypes = [
        wintypes.HDC,
        wintypes.HBITMAP,
        wintypes.UINT,
        wintypes.UINT,
        wintypes.LPVOID,
        ctypes.POINTER(BITMAPINFO),
        wintypes.UINT,
    ]
    gdi32.GetDIBits.restype = ctypes.c_int

    window_dc = user32.GetWindowDC(window.hwnd)
    if not window_dc:
        raise DesktopAppError("Windows could not open the application window for capture")
    memory_dc = wintypes.HDC()
    bitmap = wintypes.HBITMAP()
    previous = wintypes.HGDIOBJ()
    try:
        memory_dc = gdi32.CreateCompatibleDC(window_dc)
        if not memory_dc:
            raise DesktopAppError("Windows could not create a background capture context")
        bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
        if not bitmap:
            raise DesktopAppError("Windows could not allocate a background capture bitmap")
        previous = gdi32.SelectObject(memory_dc, bitmap)
        if not previous:
            raise DesktopAppError("Windows could not select the background capture bitmap")

        if backend == "printwindow":
            # PW_RENDERFULLCONTENT helps Chromium/Electron. Older applications may
            # reject the flag, so retry classic PrintWindow before failing closed.
            rendered = bool(user32.PrintWindow(window.hwnd, memory_dc, 0x2))
            if not rendered:
                rendered = bool(user32.PrintWindow(window.hwnd, memory_dc, 0))
        elif backend == "wm-print":
            # Ask the pinned HWND to paint itself into our private memory DC. This
            # never reads desktop pixels and therefore cannot capture overlapping
            # applications. PRF_CHILDREN is important for Chromium/Electron.
            result = ctypes.c_size_t()
            flags = 0x0002 | 0x0004 | 0x0008 | 0x0010 | 0x0020
            user32.SendMessageTimeoutW.argtypes = [
                wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
            ]
            user32.SendMessageTimeoutW.restype = wintypes.LPARAM
            rendered = bool(
                user32.SendMessageTimeoutW(
                    window.hwnd, 0x0317, int(memory_dc), flags,
                    0x0001 | 0x0002, 750, ctypes.byref(result)
                )
            )
        else:
            raise InvalidCommand("unsupported desktop capture backend")
        if not rendered:
            raise DesktopAppError(
                f"the application does not support {backend} background capture"
            )

        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = 0  # BI_RGB
        info.bmiHeader.biSizeImage = width * height * 4
        pixels = ctypes.create_string_buffer(width * height * 4)
        rows = gdi32.GetDIBits(
            memory_dc,
            bitmap,
            0,
            height,
            pixels,
            ctypes.byref(info),
            0,
        )
        if rows != height:
            raise DesktopAppError("Windows returned an incomplete background screenshot")
        image = Image.frombuffer(
            "RGB", (width, height), pixels.raw, "raw", "BGRX", 0, 1
        )
        image.thumbnail((1800, 1800))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG", optimize=True)
        return buffer.getvalue(), image.size[0], image.size[1]
    finally:
        if memory_dc and previous:
            gdi32.SelectObject(memory_dc, previous)
        if bitmap:
            gdi32.DeleteObject(bitmap)
        if memory_dc:
            gdi32.DeleteDC(memory_dc)
        user32.ReleaseDC(window.hwnd, window_dc)


def _channel_ranges(
    extrema: tuple[float, float] | tuple[tuple[int, int], ...],
) -> list[float]:
    """Return one intensity range per band for either ``getextrema()`` shape.

    Pillow returns a single flat ``(lo, hi)`` pair for a one-band image and one
    pair per band otherwise, and types the result as that union. The sample is
    converted to RGB before it is measured, so only the per-band shape occurs
    today; handling both keeps the verdict independent of that conversion and
    keeps the module inside the mypy gate instead of unpacking a float.
    """
    per_band = [
        float(item[1]) - float(item[0]) for item in extrema if isinstance(item, tuple)
    ]
    if per_band:
        return per_band
    flat = [float(item) for item in extrema if isinstance(item, (int, float))]
    if len(flat) == 2 and len(flat) == len(extrema):
        return [flat[1] - flat[0]]
    return []


def _png_quality(png: bytes) -> dict[str, Any]:
    """Reject blank/uniform fallback frames before they can replace a real capture."""
    try:
        from PIL import Image  # type: ignore
        image = Image.open(io.BytesIO(png)).convert("RGB")
        sample = image.resize((64, 64))
        colors = sample.getcolors(maxcolors=4097)
        unique = 4097 if colors is None else len(colors)
        ranges = _channel_ranges(sample.getextrema())
        meaningful = unique > 4 and bool(ranges) and max(ranges) >= 8
        return {
            "meaningful": meaningful,
            "sample_unique_colors": unique,
            "channel_ranges": ranges,
        }
    except Exception as exc:  # defensive: quality must never break capture
        return {"meaningful": False, "error": type(exc).__name__}


def _capture_children(window: Window) -> list[Window]:
    """List only descendant HWNDs of the pinned window, largest first."""
    from ctypes import wintypes

    user32 = _user32()
    rows: list[Window] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def enum_proc(hwnd: int, _lp: int) -> bool:
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return True
        bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, len(buf))
        rows.append(Window(int(hwnd), buf.value or "child-window", bounds, int(pid.value), False))
        return True

    user32.EnumChildWindows(window.hwnd, enum_proc, 0)
    minimum_area = max(1, int(window.width * window.height * 0.35))
    rows = [item for item in rows if item.width * item.height >= minimum_area]
    return sorted(rows, key=lambda item: item.width * item.height, reverse=True)[:5]


def _owned_hwnd(window: Window, hwnd: int) -> bool:
    """Return True only for HWNDs that stay inside the pinned app/process."""
    from ctypes import wintypes

    if not hwnd or not _user32().IsWindow(hwnd):
        return False
    pid = wintypes.DWORD()
    _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if int(pid.value) != window.pid:
        return False
    root = int(_user32().GetAncestor(hwnd, 2))  # GA_ROOT
    return hwnd == window.hwnd or root == window.hwnd


def _keyboard_target(window: Window) -> int:
    """Read the app thread's internal focus without changing global foreground."""
    from ctypes import wintypes

    class GUITHREADINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("flags", wintypes.DWORD),
            ("hwndActive", wintypes.HWND),
            ("hwndFocus", wintypes.HWND),
            ("hwndCapture", wintypes.HWND),
            ("hwndMenuOwner", wintypes.HWND),
            ("hwndMoveSize", wintypes.HWND),
            ("hwndCaret", wintypes.HWND),
            ("rcCaret", wintypes.RECT),
        ]

    user32 = _user32()
    pid = wintypes.DWORD()
    thread_id = int(user32.GetWindowThreadProcessId(window.hwnd, ctypes.byref(pid)))
    if int(pid.value) != window.pid or not thread_id:
        raise DesktopAppSecurityError("attached application process identity changed")
    info = GUITHREADINFO()
    info.cbSize = ctypes.sizeof(GUITHREADINFO)
    if user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
        for raw in (info.hwndFocus, info.hwndCaret, info.hwndActive):
            candidate = int(raw or 0)
            if _owned_hwnd(window, candidate):
                return candidate
    return window.hwnd


def _click_target(window: Window, screen_x: int, screen_y: int) -> tuple[int, int, int]:
    """Resolve the deepest child HWND at a point without consulting other apps."""
    from ctypes import wintypes

    user32 = _user32()
    target = window.hwnd
    flags = 0x0001 | 0x0002 | 0x0004  # skip invisible/disabled/transparent
    for _ in range(12):
        point = wintypes.POINT(screen_x, screen_y)
        if not user32.ScreenToClient(target, ctypes.byref(point)):
            break
        child = int(user32.ChildWindowFromPointEx(target, point, flags))
        if not child or child == target or not _owned_hwnd(window, child):
            break
        target = child
    point = wintypes.POINT(screen_x, screen_y)
    if not user32.ScreenToClient(target, ctypes.byref(point)):
        raise DesktopAppError("Windows could not map background click coordinates")
    client = wintypes.RECT()
    if not user32.GetClientRect(target, ctypes.byref(client)):
        raise DesktopAppError("Windows could not read target client bounds")
    if not (client.left <= point.x < client.right and client.top <= point.y < client.bottom):
        raise DesktopAppSecurityError("background click is outside the application client area")
    return target, int(point.x), int(point.y)


def _send_vk(window: Window, vk: int, *, up: bool = False) -> None:
    user32 = _user32()
    target = _keyboard_target(window)
    scan = int(user32.MapVirtualKeyW(vk, 0)) & 0xFF
    lparam = 1 | (scan << 16)
    if up:
        lparam |= 0xC0000000
    if not user32.PostMessageW(target, 0x0101 if up else 0x0100, vk, lparam):
        raise DesktopAppError("Windows rejected background key input")


def _press(window: Window, key: str) -> None:
    base = {
        "enter": 0x0D,
        "esc": 0x1B,
        "tab": 0x09,
        "space": 0x20,
        "backspace": 0x08,
        "delete": 0x2E,
        "home": 0x24,
        "end": 0x23,
        "left": 0x25,
        "up": 0x26,
        "right": 0x27,
        "down": 0x28,
    }
    combos = {
        "ctrl+a": (0x11, 0x41),
        "ctrl+enter": (0x11, 0x0D),
        "shift+enter": (0x10, 0x0D),
    }
    if key in base:
        _send_vk(window, base[key]); _send_vk(window, base[key], up=True); return
    if key in combos:
        modifier, target = combos[key]
        _send_vk(window, modifier); _send_vk(window, target); _send_vk(window, target, up=True); _send_vk(window, modifier, up=True); return
    raise InvalidCommand("unsupported desktop key: " + key)


def _type_unicode(window: Window, text: str) -> None:
    user32 = _user32()
    target = _keyboard_target(window)
    raw = text.encode("utf-16-le", errors="strict")
    for index in range(0, len(raw), 2):
        unit = raw[index] | (raw[index + 1] << 8)
        if not user32.PostMessageW(target, 0x0102, unit, 1):  # WM_CHAR
            raise DesktopAppError("Windows rejected background text input")


def _click(window: Window, x: int, y: int) -> None:
    if not 0 <= x < window.width or not 0 <= y < window.height:
        raise DesktopAppSecurityError("click is outside the attached window")
    user32 = _user32()
    target, client_x, client_y = _click_target(
        window, window.rect[0] + x, window.rect[1] + y
    )
    packed = (client_x & 0xFFFF) | ((client_y & 0xFFFF) << 16)
    for message, flags in ((0x0200, 0), (0x0201, 0x0001), (0x0202, 0)):
        if not user32.PostMessageW(target, message, flags, packed):
            raise DesktopAppError("Windows rejected background mouse input")


def _traycer_live_diagnostics() -> dict[str, Any]:
    from pathlib import Path
    root = Path(os.environ.get("APPDATA", "")) / "Traycer"
    log = root / "traycer-desktop.log"
    if not log.is_file():
        return {"available": False, "reason": "traycer log not found"}
    stat = log.stat()
    with log.open("rb") as handle:
        handle.seek(max(0, stat.st_size - 512_000))
        text = handle.read().decode("utf-8", errors="replace")
    safe_words = ("stream", "host", "agent", "task", "run", "queue", "error", "failed", "availability", "reconnect")
    secret_words = ("token", "password", "authorization", "bearer", "cookie", "secret", "api_key", "apikey")
    events: list[str] = []
    current_time = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("[20") and len(line) >= 25:
            current_time = line[1:24]
        low = line.casefold()
        if "message:" not in low or not any(word in low for word in safe_words):
            continue
        if any(word in low for word in secret_words):
            continue
        message = line.split("message:", 1)[-1].strip().strip(",").strip("'\"")
        if message:
            events.append(f"{current_time} {message}".strip()[:500])
    recent = events[-80:]
    return {
        "available": True,
        "log_size": stat.st_size,
        "log_mtime": stat.st_mtime,
        "recent_events": recent,
        "recent_event_count": len(recent),
        "stream_recovery_events": sum(1 for item in recent if "availability recovered" in item.casefold()),
    }


class DesktopApps:
    def __init__(self, artifacts: Any) -> None:
        self.artifacts = artifacts
        self.bindings: dict[str, Binding] = {}
        self._snapshot_history: dict[str, dict[str, Any]] = {}
        self._process_history: dict[str, dict[str, Any]] = {}

    def discover(self) -> dict[str, Any]:
        rows = []
        for window in _windows():
            row = window.public()
            row["matched_profiles"] = [
                app_id
                for app_id, (_label, needle) in _BUILTINS.items()
                if needle.casefold() in window.title.casefold()
            ]
            rows.append(row)
        return {"windows": rows, "builtin_apps": sorted(_BUILTINS)}

    def attach(self, payload: dict[str, Any]) -> dict[str, Any]:
        app_id = payload.get("app_id")
        if not isinstance(app_id, str) or _APP_ID.fullmatch(app_id) is None:
            raise InvalidCommand("app.attach app_id must be a safe lowercase identifier")
        access = payload.get("access", "control")
        if access not in {"observe", "control"}:
            raise InvalidCommand("app.attach access must be observe or control")
        builtin = _BUILTINS.get(app_id)
        needle = builtin[1] if builtin else payload.get("title_contains")
        if not isinstance(needle, str) or not needle.strip():
            raise InvalidCommand("custom apps require title_contains")
        raw_window_id = payload.get("window_id")
        candidates = _windows()
        if raw_window_id is not None:
            if isinstance(raw_window_id, bool) or not isinstance(raw_window_id, int) or raw_window_id <= 0:
                raise InvalidCommand("app.attach window_id must be a positive integer")
            candidates = [item for item in candidates if item.hwnd == raw_window_id]
        else:
            candidates = [item for item in candidates if needle.casefold() in item.title.casefold()]
        if not candidates:
            raise DesktopAppError(f"no visible window matches {needle!r}")
        window = candidates[0]
        if builtin and needle.casefold() not in window.title.casefold():
            raise DesktopAppSecurityError("selected window does not match built-in app profile")
        binding = Binding(app_id, window.hwnd, window.pid, access, time.time())
        self.bindings[app_id] = binding
        return {
            "attached": True,
            "app_id": app_id,
            "display_name": builtin[0] if builtin else app_id,
            "access": access,
            "window": window.public(),
            "control_contract": _background_control_contract(),
        }

    def current(self, app_id: Any, *, control: bool = False) -> tuple[Binding, Window]:
        if not isinstance(app_id, str) or app_id not in self.bindings:
            raise DesktopAppSecurityError("desktop app is not attached in this session")
        binding = self.bindings[app_id]
        if control and binding.access != "control":
            raise DesktopAppSecurityError("desktop app is observation-only")
        try:
            return binding, _find_bound(binding)
        except (DesktopAppError, DesktopAppSecurityError):
            self.bindings.pop(app_id, None)
            raise

    def status(self) -> dict[str, Any]:
        rows = []
        for app_id in list(self.bindings):
            try:
                binding, window = self.current(app_id)
            except (DesktopAppError, DesktopAppSecurityError):
                continue
            process = _process_sample(window.pid)
            previous_process = self._process_history.get(app_id)
            if process.get("available") and previous_process and previous_process.get("available"):
                delta = float(process.get("cpu_seconds_total", 0.0)) - float(previous_process.get("cpu_seconds_total", 0.0))
                process["cpu_seconds_delta"] = round(max(0.0, delta), 6)
                process["cpu_advanced"] = delta > 0.001
            else:
                process["cpu_seconds_delta"] = None
                process["cpu_advanced"] = None
            self._process_history[app_id] = process
            rows.append(
                {
                    "app_id": app_id,
                    "access": binding.access,
                    "window": window.public(),
                    "health": _window_health(window),
                    "process_activity": process,
                    "snapshot_history": dict(self._snapshot_history.get(app_id, {})),
                    "control_contract": _background_control_contract(),
                }
            )
        return {"bindings": rows, "control_contract": _background_control_contract()}

    def snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        binding, window = self.current(payload.get("app_id"))
        foreground = payload.get("bring_to_front", False)
        if not isinstance(foreground, bool):
            raise InvalidCommand("bring_to_front must be boolean")
        if foreground:
            raise DesktopAppSecurityError(
                "foreground capture is disabled; app.snapshot is intentionally non-intrusive"
            )
        png, capture_width, capture_height = _capture_window_png(window)
        captured_at = time.time()
        digest = hashlib.sha256(png).hexdigest()
        primary_digest = digest
        previous = self._snapshot_history.get(binding.app_id)
        capture_method = "win32-printwindow"
        fallbacks: list[dict[str, Any]] = []
        # Electron/Chromium can return a cached DirectComposition surface from
        # PrintWindow. On repetition we try only window-owned paint surfaces.
        # Screen-region capture is never used.
        primary_repeated = bool(
            previous
            and previous.get("primary_sha256", previous.get("sha256")) == primary_digest
        )
        if primary_repeated:
            try:
                alt_png, alt_width, alt_height = _capture_window_png(window, "wm-print")
                alt_digest = hashlib.sha256(alt_png).hexdigest()
                quality = _png_quality(alt_png)
                attempt = {
                    "backend": "win32-wm-print",
                    "sha256": alt_digest,
                    "different_from_printwindow": alt_digest != digest,
                    "quality": quality,
                }
                fallbacks.append(attempt)
                if quality.get("meaningful") and alt_digest != digest:
                    png, capture_width, capture_height, digest = alt_png, alt_width, alt_height, alt_digest
                    capture_method = "win32-wm-print"
            except DesktopAppError as exc:
                fallbacks.append({"backend": "win32-wm-print", "error": str(exc)[:240]})

            if capture_method == "win32-printwindow":
                for child in _capture_children(window):
                    try:
                        child_png, child_width, child_height = _capture_window_png(child, "printwindow")
                        child_digest = hashlib.sha256(child_png).hexdigest()
                        quality = _png_quality(child_png)
                        attempt = {
                            "backend": "win32-child-printwindow",
                            "window_id": child.hwnd,
                            "window_class": child.title[:120],
                            "process_id": child.pid,
                            "sha256": child_digest,
                            "quality": quality,
                        }
                        fallbacks.append(attempt)
                        if quality.get("meaningful") and child_digest != digest:
                            png, capture_width, capture_height, digest = (
                                child_png, child_width, child_height, child_digest
                            )
                            capture_method = "win32-child-printwindow"
                            break
                    except DesktopAppError as exc:
                        fallbacks.append({
                            "backend": "win32-child-printwindow",
                            "window_id": child.hwnd,
                            "error": str(exc)[:240],
                        })
        if previous and previous.get("sha256") == digest:
            repeated_count = int(previous.get("repeated_count", 1)) + 1
            unchanged_since = float(previous.get("unchanged_since", previous.get("captured_at", captured_at)))
            freshness = "repeated_frame"
        else:
            repeated_count = 1
            unchanged_since = captured_at
            freshness = "changed" if previous else "first_sample"
        history = {
            "sha256": digest,
            "primary_sha256": primary_digest,
            "capture_method": capture_method,
            "captured_at": captured_at,
            "freshness": freshness,
            "repeated_count": repeated_count,
            "unchanged_since": unchanged_since,
            "unchanged_for_seconds": max(0.0, captured_at - unchanged_since),
        }
        self._snapshot_history[binding.app_id] = history
        record = self.artifacts.put(
            png, name=f"desktop-{binding.app_id}.png", mime="image/png"
        )
        return {
            "app_id": binding.app_id,
            "artifact": record.to_dict(),
            "width": capture_width,
            "height": capture_height,
            "capture_method": capture_method,
            "non_intrusive": True,
            "capture_diagnostics": {
                **history,
                "health": _window_health(window),
                "fallbacks": fallbacks,
                "interpretation": (
                    "identical PrintWindow frames do not by themselves prove the app is frozen; "
                    "KaroX tries quality-gated WM_PRINT and child-window capture on repetition and combines capture freshness with "
                    "window/process liveness signals"
                ),
            },
            "window": window.public(),
            "control_contract": _background_control_contract(),
        }

    def inspect(self, payload: dict[str, Any]) -> dict[str, Any]:
        binding, window = self.current(payload.get("app_id"))
        result: dict[str, Any] = {
            "app_id": binding.app_id,
            "window": window.public(),
            "health": _window_health(window),
            "process_activity": _process_sample(window.pid),
            "snapshot_history": dict(self._snapshot_history.get(binding.app_id, {})),
        }
        if binding.app_id == "traycer":
            result["live_diagnostics"] = _traycer_live_diagnostics()
        return result

    def input(self, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        binding, window = self.current(payload.get("app_id"), control=True)
        if action == "app.focus":
            return {
                "app_id": binding.app_id,
                "focused": False,
                **_background_control_contract(),
            }
        if action == "app.restore":
            _restore_without_activation(window)
            return {
                "app_id": binding.app_id,
                "restored": True,
                "activated": False,
                **_background_control_contract(),
            }
        if action == "app.click":
            x, y = payload.get("x"), payload.get("y")
            if isinstance(x, bool) or not isinstance(x, int) or isinstance(y, bool) or not isinstance(y, int):
                raise InvalidCommand("app.click x and y must be integers")
            _click(window, x, y)
            return {
                "app_id": binding.app_id,
                "clicked": True,
                "x": x,
                "y": y,
                **_background_control_contract(),
            }
        if action == "app.type":
            text = payload.get("text")
            if not isinstance(text, str) or not text or len(text) > _MAX_TEXT or "\x00" in text:
                raise InvalidCommand(f"app.type text must contain 1-{_MAX_TEXT} safe characters")
            _type_unicode(window, text)
            return {
                "app_id": binding.app_id,
                "typed": True,
                "text_length": len(text),
                **_background_control_contract(),
            }
        key = payload.get("key")
        if not isinstance(key, str) or not key:
            raise InvalidCommand("app.key key must be a non-empty string")
        normalized = key.casefold()
        _press(window, normalized)
        return {
            "app_id": binding.app_id,
            "pressed": True,
            "key": normalized,
            **_background_control_contract(),
        }


def _opt_in(runtime: Any, payload: dict[str, Any]) -> None:
    if payload.get("user_confirmed") is not True:
        raise DesktopAppSecurityError("desktop access requires explicit user confirmation")
    if getattr(runtime, "_access_profile", None) is not AccessProfile.ELEVATED:
        raise PolicyDenied("desktop access requires the elevated KaroX profile")
    runtime.policy.origin_grants.setdefault(runtime.hosted_origin.key, set()).add(
        Capability.DESKTOP_INPUT
    )
    runtime.policy.require(runtime.hosted_origin, Capability.DESKTOP_INPUT)


def execute_desktop_app_action(
    runtime: Any, action: str, payload: dict[str, Any], deadline_seconds: float
) -> dict[str, Any]:
    del deadline_seconds
    if action in {"app.discover", "app.attach", "app.launch"}:
        _opt_in(runtime, payload)
    else:
        runtime.policy.require(runtime.hosted_origin, Capability.DESKTOP_INPUT)
    controller = getattr(runtime, "_desktop_apps", None)
    if not isinstance(controller, DesktopApps):
        controller = DesktopApps(runtime._artifacts)
        runtime._desktop_apps = controller
    if action == "app.discover":
        return controller.discover()
    if action == "app.launch":
        return _launch_start_menu_shortcut(
            payload.get("shortcut_path"), payload.get("title_contains", "")
        )
    if action == "app.attach":
        return controller.attach(payload)
    if action == "app.status":
        return controller.status()
    if action == "app.snapshot":
        return controller.snapshot(payload)
    if action == "app.inspect":
        return controller.inspect(payload)
    if action in {"app.focus", "app.restore", "app.click", "app.type", "app.key"}:
        return controller.input(action, payload)
    if action == "app.detach":
        app_id = payload.get("app_id")
        if not isinstance(app_id, str):
            raise InvalidCommand("app.detach app_id must be string")
        return {"app_id": app_id, "detached": controller.bindings.pop(app_id, None) is not None}
    raise InvalidCommand(f"unsupported desktop app action: {action}")
