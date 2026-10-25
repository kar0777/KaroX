"""The Win32 probe: every raw OS call the computer-use surface makes.

All logic layers (identity, capture, inspection, input, launch) talk to this
protocol instead of calling ctypes directly. That keeps the contracts testable
with an in-memory fake on any platform, and it keeps the non-intrusion rule
auditable in one file: this module never calls a global input-injection,
foreground-activation or hooking API. Input is delivered exclusively with
``PostMessageW``/``SendMessageTimeoutW`` to a pinned HWND, capture exclusively
with ``PrintWindow``/``WM_PRINT`` against that HWND, and window state changes
exclusively with ``ShowWindowAsync`` no-activate/normal verbs on windows the
session already pinned.

DPI: the process opts into per-monitor awareness once, so every rectangle and
capture is in physical pixels and matches what the user's screen shows.
"""

from __future__ import annotations

import ctypes
import os
from typing import Any, NamedTuple, Optional

from .errors import PlatformUnsupportedError


class RawWindow(NamedTuple):
    hwnd: int
    title: str
    rect: tuple[int, int, int, int]
    pid: int
    visible: bool
    iconic: bool


_MAX_WINDOWS = 120
_MAX_TEXT_CHARS = 4096
_GWL_STYLE = -16
_WS_VISIBLE = 0x10000000
_GA_ROOT = 2
_SMTO_ABORTIFHUNG = 0x0002
_SMTO_NOTIMEOUTIFNOTHUNG = 0x0008
_WM_NULL = 0x0000
_WM_GETTEXT = 0x000D
_WM_PRINT = 0x0317
_PRF_FLAGS = 0x0002 | 0x0004 | 0x0008 | 0x0010 | 0x0020
_SW_SHOWNORMAL = 1
_SW_SHOWNOACTIVATE = 4
_SW_SHOWMINNOACTIVE = 7
_CREATE_NEW_CONSOLE = 0x00000010
_STARTF_USESHOWWINDOW = 0x00000001

_DPI_AWARENESS: Optional[str] = None


def _require_windows() -> Any:
    if os.name != "nt":
        raise PlatformUnsupportedError(
            "native computer use is currently Windows-only"
        )
    return ctypes.windll


class _BitmapStructs:
    def __init__(self) -> None:
        from ctypes import wintypes

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

        self.BITMAPINFOHEADER = BITMAPINFOHEADER
        self.BITMAPINFO = BITMAPINFO


class Win32Probe:
    """ctypes implementation of the computer-use probe protocol."""

    available = True
    platform_name = "win32"

    def __init__(self) -> None:
        windll = _require_windows()
        self._user32 = windll.user32
        self._gdi32 = windll.gdi32
        self._shell32 = windll.shell32
        self._kernel32 = windll.kernel32
        self._structs = _BitmapStructs()
        self._declare_signatures()
        self._keepalive: list[Any] = []

    # -- signatures -------------------------------------------------------
    def _declare_signatures(self) -> None:
        from ctypes import wintypes

        user32 = self._user32
        # Pointer-returning functions default to c_int in ctypes; every handle
        # is declared pointer-sized so 64-bit values cannot be truncated.
        user32.EnumWindows.restype = wintypes.BOOL
        user32.EnumChildWindows.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetClientRect.restype = wintypes.BOOL
        user32.ScreenToClient.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
        user32.ScreenToClient.restype = wintypes.BOOL
        user32.ChildWindowFromPointEx.argtypes = [wintypes.HWND, wintypes.POINT, wintypes.UINT]
        user32.ChildWindowFromPointEx.restype = wintypes.HWND
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.IsIconic.restype = wintypes.BOOL
        user32.IsHungAppWindow.argtypes = [wintypes.HWND]
        user32.IsHungAppWindow.restype = wintypes.BOOL
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        user32.GetDpiForWindow.argtypes = [wintypes.HWND]
        user32.GetDpiForWindow.restype = wintypes.UINT
        user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.SendMessageTimeoutW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
            wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t),
        ]
        user32.SendMessageTimeoutW.restype = ctypes.c_ssize_t
        user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindowAsync.restype = wintypes.BOOL
        user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
        user32.PrintWindow.restype = wintypes.BOOL
        user32.GetWindowDC.argtypes = [wintypes.HWND]
        user32.GetWindowDC.restype = wintypes.HDC
        user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        user32.ReleaseDC.restype = ctypes.c_int
        user32.MapVirtualKeyW.argtypes = [wintypes.UINT, wintypes.UINT]
        user32.MapVirtualKeyW.restype = wintypes.UINT
        self._kernel32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p,
            wintypes.BOOL, wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR,
            ctypes.c_void_p, ctypes.c_void_p,
        ]
        self._kernel32.CreateProcessW.restype = wintypes.BOOL

        gdi32 = self._gdi32
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
            wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
            wintypes.LPVOID, ctypes.c_void_p, wintypes.UINT,
        ]
        gdi32.GetDIBits.restype = ctypes.c_int

        self._shell32.ShellExecuteW.argtypes = [
            ctypes.c_void_p, wintypes.LPCWSTR, wintypes.LPCWSTR,
            wintypes.LPCWSTR, wintypes.LPCWSTR, ctypes.c_int,
        ]
        self._shell32.ShellExecuteW.restype = ctypes.c_void_p
        self._kernel32.Sleep.argtypes = [wintypes.DWORD]
        self._kernel32.Sleep.restype = None
        self._kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._kernel32.CloseHandle.restype = wintypes.BOOL

    # -- DPI ---------------------------------------------------------------
    def ensure_dpi_awareness(self) -> str:
        global _DPI_AWARENESS
        if _DPI_AWARENESS is not None:
            return _DPI_AWARENESS
        mode = "none"
        try:
            _require_windows()
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 == (HANDLE)-4
            if self._user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                mode = "per-monitor-v2"
            else:
                shcore = ctypes.windll.shcore
                shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
                if shcore.SetProcessDpiAwareness(2) == 0:  # PROCESS_PER_MONITOR_DPI_AWARE
                    mode = "per-monitor"
                else:
                    self._user32.SetProcessDPIAware()
                    mode = "system"
        except (OSError, AttributeError):
            try:
                self._user32.SetProcessDPIAware()
                mode = "system"
            except (OSError, AttributeError):
                mode = "none"
        _DPI_AWARENESS = mode
        return mode

    # -- enumeration ---------------------------------------------------------
    def _raw_window(self, hwnd: int) -> Optional[RawWindow]:
        from ctypes import wintypes

        user32 = self._user32
        length = int(user32.GetWindowTextLengthW(hwnd))
        if length <= 0:
            return None
        buffer = ctypes.create_unicode_buffer(min(length + 1, _MAX_TEXT_CHARS + 1))
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        title = buffer.value.strip()
        if not title:
            return None
        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
        if bounds[2] <= bounds[0] or bounds[3] <= bounds[1]:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return RawWindow(
            hwnd, title, bounds, int(pid.value),
            bool(user32.IsWindowVisible(hwnd)), bool(user32.IsIconic(hwnd)),
        )

    def enum_windows(self) -> list[RawWindow]:
        from ctypes import wintypes

        found: list[RawWindow] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def enum_proc(hwnd: int, _lp: int) -> bool:
            if len(found) >= _MAX_WINDOWS:
                return False
            if not self._user32.IsWindowVisible(hwnd):
                return True
            raw = self._raw_window(int(hwnd))
            if raw is not None:
                found.append(raw)
            return True

        self._keepalive.append(enum_proc)
        self._user32.EnumWindows(enum_proc, 0)
        self._keepalive.remove(enum_proc)
        return found

    def enum_child_windows(self, hwnd: int) -> list[RawWindow]:
        from ctypes import wintypes

        rows: list[RawWindow] = []

        @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        def enum_proc(child: int, _lp: int) -> bool:
            child = int(child)
            if len(rows) >= 400:
                return False
            # EnumChildWindows recurses into grandchildren; this protocol
            # exposes direct children only so callers control depth themselves.
            if int(self._user32.GetParent(child) or 0) != hwnd:
                return True
            rect = wintypes.RECT()
            if not self._user32.GetWindowRect(child, ctypes.byref(rect)):
                return True
            bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
            pid = wintypes.DWORD()
            self._user32.GetWindowThreadProcessId(child, ctypes.byref(pid))
            buffer = ctypes.create_unicode_buffer(256)
            self._user32.GetWindowTextW(child, buffer, len(buffer))
            rows.append(
                RawWindow(
                    child, buffer.value.strip(), bounds, int(pid.value),
                    bool(self._user32.IsWindowVisible(child)),
                    bool(self._user32.IsIconic(child)),
                )
            )
            return True

        self._keepalive.append(enum_proc)
        self._user32.EnumChildWindows(hwnd, enum_proc, 0)
        self._keepalive.remove(enum_proc)
        return rows

    # -- window facts -----------------------------------------------------------
    def window_text(self, hwnd: int, max_chars: int) -> str:
        buffer = ctypes.create_unicode_buffer(min(max(1, max_chars), _MAX_TEXT_CHARS) + 1)
        self._user32.GetWindowTextW(hwnd, buffer, len(buffer))
        return buffer.value

    def class_name(self, hwnd: int) -> str:
        buffer = ctypes.create_unicode_buffer(256)
        self._user32.GetClassNameW(hwnd, buffer, len(buffer))
        return buffer.value

    def window_rect(self, hwnd: int) -> Optional[tuple[int, int, int, int]]:
        from ctypes import wintypes

        rect = wintypes.RECT()
        if not self._user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))

    def client_rect(self, hwnd: int) -> Optional[tuple[int, int, int, int]]:
        from ctypes import wintypes

        rect = wintypes.RECT()
        if not self._user32.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))

    def screen_to_client(self, hwnd: int, x: int, y: int) -> Optional[tuple[int, int]]:
        from ctypes import wintypes

        point = wintypes.POINT(int(x), int(y))
        if not self._user32.ScreenToClient(hwnd, ctypes.byref(point)):
            return None
        return (int(point.x), int(point.y))

    def child_from_point(self, hwnd: int, x: int, y: int) -> int:
        from ctypes import wintypes

        flags = 0x0001 | 0x0002 | 0x0004  # skip invisible/disabled/transparent
        child = int(self._user32.ChildWindowFromPointEx(hwnd, wintypes.POINT(int(x), int(y)), flags) or 0)
        return child

    def window_pid(self, hwnd: int) -> int:
        from ctypes import wintypes

        pid = wintypes.DWORD()
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value)

    def is_window(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindow(hwnd))

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self._user32.IsIconic(hwnd))

    def is_visible(self, hwnd: int) -> bool:
        return bool(self._user32.IsWindowVisible(hwnd))

    def is_hung(self, hwnd: int) -> bool:
        return bool(self._user32.IsHungAppWindow(hwnd))

    def foreground_hwnd(self) -> int:
        return int(self._user32.GetForegroundWindow() or 0)

    def dpi_for_window(self, hwnd: int) -> int:
        try:
            dpi = int(self._user32.GetDpiForWindow(hwnd) or 0)
            if dpi > 0:
                return dpi
        except (OSError, AttributeError):
            pass
        try:
            return int(self._user32.GetDpiForSystem() or 96)
        except (OSError, AttributeError):
            return 96

    def root_ancestor(self, hwnd: int) -> int:
        return int(self._user32.GetAncestor(hwnd, _GA_ROOT) or 0)

    def window_styles(self, hwnd: int) -> int:
        return int(self._user32.GetWindowLongPtrW(hwnd, _GWL_STYLE) or 0)

    def thread_focus(self, hwnd: int) -> tuple[int, int, int, int, int]:
        """(thread_id, pid, focus, caret, active) for the window's GUI thread."""

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

        pid = wintypes.DWORD()
        thread_id = int(self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or 0)
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        focus = caret = active = 0
        if thread_id and self._user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)):
            focus = int(info.hwndFocus or 0)
            caret = int(info.hwndCaret or 0)
            active = int(info.hwndActive or 0)
        return (thread_id, int(pid.value), focus, caret, active)

    # -- messaging ---------------------------------------------------------------
    def post_message(self, hwnd: int, msg: int, wparam: int, lparam: int) -> bool:
        return bool(self._user32.PostMessageW(hwnd, msg, wparam, lparam))

    def send_message_timeout(
        self, hwnd: int, msg: int, wparam: int, lparam: int, timeout_ms: int
    ) -> tuple[bool, int]:
        response = ctypes.c_size_t()
        ok = bool(
            self._user32.SendMessageTimeoutW(
                hwnd, msg, wparam, lparam,
                _SMTO_ABORTIFHUNG | _SMTO_NOTIMEOUTIFNOTHUNG,
                max(1, int(timeout_ms)), ctypes.byref(response),
            )
        )
        return (ok, int(response.value))

    def get_text(self, hwnd: int, max_chars: int) -> str:
        capped = min(max(1, max_chars), _MAX_TEXT_CHARS)
        ok, length = self.send_message_timeout(hwnd, 0x000E, 0, 0, 250)  # WM_GETTEXTLENGTH
        if not ok:
            return ""
        size = min(int(length), capped) + 1
        buffer = ctypes.create_unicode_buffer(size)
        ok, _ = self.send_message_timeout(
            hwnd, _WM_GETTEXT, size, ctypes.addressof(buffer), 250
        )
        return buffer.value if ok else ""

    def map_virtual_key(self, vk: int) -> int:
        return int(self._user32.MapVirtualKeyW(int(vk), 0) or 0) & 0xFF

    # -- capture -----------------------------------------------------------------
    def capture_bits(self, hwnd: int, width: int, height: int, backend: str) -> Optional[bytes]:
        """Render one HWND into a private memory DC; returns top-down BGRX bytes.

        Never reads screen pixels: an overlapping foreign window cannot leak
        into the buffer. ``None`` means the window refused the paint request.
        """

        from ctypes import wintypes

        if width <= 0 or height <= 0:
            return None
        gdi32 = self._gdi32
        user32 = self._user32
        window_dc = user32.GetWindowDC(hwnd)
        if not window_dc:
            return None
        memory_dc = wintypes.HDC()
        bitmap = wintypes.HBITMAP()
        previous = wintypes.HGDIOBJ()
        try:
            memory_dc = gdi32.CreateCompatibleDC(window_dc)
            if not memory_dc:
                return None
            bitmap = gdi32.CreateCompatibleBitmap(window_dc, width, height)
            if not bitmap:
                return None
            previous = gdi32.SelectObject(memory_dc, bitmap)
            if not previous:
                return None

            if backend == "printwindow":
                # PW_RENDERFULLCONTENT helps Chromium/Electron; older apps may
                # reject the flag, so retry classic PrintWindow before giving up.
                rendered = bool(user32.PrintWindow(hwnd, memory_dc, 0x2))
                if not rendered:
                    rendered = bool(user32.PrintWindow(hwnd, memory_dc, 0))
            elif backend == "wm-print":
                ok, _ = self.send_message_timeout(hwnd, _WM_PRINT, int(memory_dc), _PRF_FLAGS, 750)
                rendered = ok
            else:
                return None
            if not rendered:
                return None

            info = self._structs.BITMAPINFO()
            info.bmiHeader.biSize = ctypes.sizeof(self._structs.BITMAPINFOHEADER)
            info.bmiHeader.biWidth = width
            info.bmiHeader.biHeight = -height  # top-down
            info.bmiHeader.biPlanes = 1
            info.bmiHeader.biBitCount = 32
            info.bmiHeader.biCompression = 0  # BI_RGB
            info.bmiHeader.biSizeImage = width * height * 4
            pixels = ctypes.create_string_buffer(width * height * 4)
            rows = gdi32.GetDIBits(
                memory_dc, bitmap, 0, height, pixels, ctypes.byref(info), 0
            )
            if rows != height:
                return None
            return pixels.raw
        finally:
            if memory_dc and previous:
                gdi32.SelectObject(memory_dc, previous)
            if bitmap:
                gdi32.DeleteObject(bitmap)
            if memory_dc:
                gdi32.DeleteDC(memory_dc)
            user32.ReleaseDC(hwnd, window_dc)

    # -- launch / window state -----------------------------------------------------
    def shell_execute(self, target: str, arguments: str, cwd: str, show_command: int) -> int:
        result = self._shell32.ShellExecuteW(
            None, "open", str(target), str(arguments or ""), str(cwd or ""), int(show_command)
        )
        return int(result or 0)

    def create_process(self, executable: str, arguments: str, cwd: Optional[str], show: int) -> int:
        """CreateProcessW with an explicit show command; returns the pid or 0."""

        from ctypes import wintypes

        class STARTUPINFOW(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("lpReserved", wintypes.LPWSTR),
                ("lpDesktop", wintypes.LPWSTR),
                ("lpTitle", wintypes.LPWSTR),
                ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD),
                ("dwXCountChars", wintypes.DWORD), ("dwYCountChars", wintypes.DWORD),
                ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD),
                ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
                ("hStdInput", wintypes.HANDLE),
                ("hStdOutput", wintypes.HANDLE),
                ("hStdError", wintypes.HANDLE),
            ]

        class PROCESS_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("hProcess", wintypes.HANDLE),
                ("hThread", wintypes.HANDLE),
                ("dwProcessId", wintypes.DWORD),
                ("dwThreadId", wintypes.DWORD),
            ]

        command_line = ctypes.create_unicode_buffer(f'"{executable}" {arguments}'.strip())
        startup = STARTUPINFOW()
        startup.cb = ctypes.sizeof(STARTUPINFOW)
        startup.dwFlags = _STARTF_USESHOWWINDOW
        startup.wShowWindow = int(show)
        process_info = PROCESS_INFORMATION()
        ok = self._kernel32.CreateProcessW(
            str(executable), command_line, None, None, False,
            _CREATE_NEW_CONSOLE, None, str(cwd) if cwd else None,
            ctypes.byref(startup), ctypes.byref(process_info),
        )
        if not ok:
            return 0
        pid = int(process_info.dwProcessId)
        self._kernel32.CloseHandle(process_info.hThread)
        self._kernel32.CloseHandle(process_info.hProcess)
        return pid

    def show_window_async(self, hwnd: int, command: int) -> bool:
        return bool(self._user32.ShowWindowAsync(hwnd, int(command)))

    def sleep_ms(self, ms: int) -> None:
        self._kernel32.Sleep(max(0, int(ms)))

    # -- process facts ----------------------------------------------------------------
    def process_info(self, pid: int) -> Optional[tuple[float, str]]:
        try:
            import psutil  # type: ignore[import-untyped]
        except ImportError:
            return None
        try:
            process = psutil.Process(int(pid))
            try:
                exe = str(process.exe() or "")
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                exe = ""
            return (float(process.create_time()), exe)
        except (psutil.NoSuchProcess, psutil.AccessDenied, ValueError):
            return None


_PROBE: Optional[Win32Probe] = None


def default_probe() -> Win32Probe:
    """The process-wide real probe; typed error off Windows."""

    global _PROBE
    _require_windows()
    if _PROBE is None:
        _PROBE = Win32Probe()
    return _PROBE


# Show-command constants re-exported for the launch layer.
SW_SHOWNORMAL = _SW_SHOWNORMAL
SW_SHOWNOACTIVATE = _SW_SHOWNOACTIVATE
SW_SHOWMINNOACTIVE = _SW_SHOWMINNOACTIVE

__all__ = [
    "RawWindow",
    "SW_SHOWNORMAL",
    "SW_SHOWNOACTIVATE",
    "SW_SHOWMINNOACTIVE",
    "Win32Probe",
    "default_probe",
]
