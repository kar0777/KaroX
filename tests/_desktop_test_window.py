"""A real, controlled Win32 window for live computer-use evidence.

Live tests must never touch the user's foreground session. This harness
creates its own top-level window on a background thread, minimized and
without activation (``SW_SHOWMINNOACTIVATE``), with an ``EDIT`` child control
and a painted multi-color client area so capture quality gates have something
real to measure. Keyboard focus is set thread-locally with ``SetFocus`` — that
never changes the global foreground window.

Windows-only; guarded by the live test module.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time
from ctypes import wintypes
from typing import Optional

WM_DESTROY = 0x0002
WM_PAINT = 0x000F
WM_CLOSE = 0x0010
WM_PRINTCLIENT = 0x0318
WM_APP_CLOSE = 0x8000 + 7
WM_APP_FOCUS = 0x8000 + 9

WS_OVERLAPPEDWINDOW = 0x00CF0000
WS_MINIMIZE = 0x20000000
WS_CHILD = 0x40000000
WS_VISIBLE = 0x10000000
WS_VSCROLL = 0x00200000
ES_MULTILINE = 0x0004
SW_SHOWMINNOACTIVATE = 7

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)


def _declare_signatures() -> None:
    """Pointer-sized signatures; ctypes defaults would truncate LPARAM/HDC."""

    if os.name != "nt":
        return
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.DefWindowProcW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.DefWindowProcW.restype = LRESULT
    user32.PostMessageW.argtypes = [
        wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
    ]
    user32.PostMessageW.restype = wintypes.BOOL
    user32.BeginPaint.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.BeginPaint.restype = wintypes.HDC
    user32.EndPaint.argtypes = [wintypes.HWND, ctypes.c_void_p]
    user32.EndPaint.restype = wintypes.BOOL
    user32.FillRect.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT), wintypes.HBRUSH]
    user32.FillRect.restype = ctypes.c_int
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.CreateWindowExW.argtypes = [
        wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
    ]
    user32.CreateWindowExW.restype = wintypes.HWND
    kernel32 = ctypes.windll.kernel32
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    user32.RegisterClassW.argtypes = [ctypes.c_void_p]
    user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    user32.DestroyWindow.argtypes = [wintypes.HWND]
    user32.DestroyWindow.restype = wintypes.BOOL
    user32.UpdateWindow.argtypes = [wintypes.HWND]
    user32.IsWindow.argtypes = [wintypes.HWND]
    user32.SetFocus.argtypes = [wintypes.HWND]
    user32.SetFocus.restype = wintypes.HWND
    gdi32.CreateSolidBrush.argtypes = [wintypes.DWORD]
    gdi32.CreateSolidBrush.restype = wintypes.HBRUSH
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]

_COLORS = (
    0x000000FF,  # red (COLORREF is 0x00BBGGRR)
    0x0000FF00,  # green
    0x00FF0000,  # blue
    0x00FFFFFF,  # white
    0x00000000,  # black
    0x0000FFFF,  # yellow
)


class _PaintStruct(ctypes.Structure):
    _fields_ = [
        ("hdc", wintypes.HDC),
        ("fErase", wintypes.BOOL),
        ("rcPaint", wintypes.RECT),
        ("fRestore", wintypes.BOOL),
        ("fIncUpdate", wintypes.BOOL),
        ("rgbReserved", ctypes.c_byte * 32),
    ]


class TestWindow:
    """One owned background window with an EDIT child; safe to input into."""

    __test__ = False  # not a pytest test class

    def __init__(self, title: str, width: int = 640, height: int = 480) -> None:
        self.title = title
        self.width = width
        self.height = height
        self.hwnd: int = 0
        self.edit_hwnd: int = 0
        self.thread_id: int = 0
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()
        self._failed: Optional[str] = None
        self._wndproc_ref: Optional[WNDPROC] = None

    # -- wndproc ------------------------------------------------------------
    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        user32 = ctypes.windll.user32
        gdi32 = ctypes.windll.gdi32
        if msg in (WM_PAINT, WM_PRINTCLIENT):
            if msg == WM_PAINT:
                paint = _PaintStruct()
                hdc = user32.BeginPaint(hwnd, ctypes.byref(paint))
            else:
                hdc = wparam  # WM_PRINTCLIENT supplies the target DC
            if hdc:
                rect = wintypes.RECT()
                user32.GetClientRect(hwnd, ctypes.byref(rect))
                w = max(1, rect.right - rect.left)
                h = max(1, rect.bottom - rect.top)
                cell_w = max(1, w // 3)
                cell_h = max(1, h // 2)
                index = 0
                for row in range(2):
                    for column in range(3):
                        brush = gdi32.CreateSolidBrush(_COLORS[index % len(_COLORS)])
                        box = wintypes.RECT(
                            column * cell_w,
                            row * cell_h,
                            (column + 1) * cell_w if column < 2 else w,
                            (row + 1) * cell_h if row < 1 else h,
                        )
                        user32.FillRect(hdc, ctypes.byref(box), brush)
                        gdi32.DeleteObject(brush)
                        index += 1
            if msg == WM_PAINT:
                user32.EndPaint(hwnd, ctypes.byref(paint))
            return 0
        if msg == WM_APP_FOCUS:
            # A real app holds thread-internal keyboard focus; this harness
            # re-establishes it on its own thread (never the global one).
            user32.SetFocus(self.edit_hwnd)
            return 0
        if msg == WM_APP_CLOSE:
            user32.DestroyWindow(hwnd)
            return 0
        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0
        try:
            return int(user32.DefWindowProcW(hwnd, msg, wparam, lparam) or 0)
        except Exception:  # pragma: no cover - a callback must never raise
            return 0

    # -- lifecycle --------------------------------------------------------------
    def _run(self) -> None:
        try:
            _declare_signatures()
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32
            hinstance = kernel32.GetModuleHandleW(None)

            class WNDCLASSW(ctypes.Structure):
                _fields_ = [
                    ("style", wintypes.UINT),
                    ("lpfnWndProc", WNDPROC),
                    ("cbClsExtra", ctypes.c_int),
                    ("cbWndExtra", ctypes.c_int),
                    ("hInstance", wintypes.HINSTANCE),
                    ("hIcon", wintypes.HICON),
                    ("hCursor", wintypes.HANDLE),
                    ("hbrBackground", wintypes.HBRUSH),
                    ("lpszMenuName", wintypes.LPCWSTR),
                    ("lpszClassName", wintypes.LPCWSTR),
                ]

            self._wndproc_ref = WNDPROC(self._wndproc)  # keepalive: GC'd callbacks crash
            class_name = f"KaroXLiveTest{os.getpid()}_{int(time.time() * 1000) % 100000}"
            wc = WNDCLASSW()
            wc.style = 0
            wc.lpfnWndProc = self._wndproc_ref
            wc.hInstance = hinstance
            wc.hbrBackground = 6  # COLOR_BTNFACE + 1
            wc.lpszClassName = class_name
            if not user32.RegisterClassW(ctypes.byref(wc)):
                self._failed = f"RegisterClassW failed: {ctypes.GetLastError()}"
                self._ready.set()
                return

            hwnd = user32.CreateWindowExW(
                0, class_name, self.title,
                WS_OVERLAPPEDWINDOW | WS_MINIMIZE,
                200, 200, self.width, self.height,
                None, None, hinstance, None,
            )
            if not hwnd:
                self._failed = f"CreateWindowExW failed: {ctypes.GetLastError()}"
                self._ready.set()
                return
            self.hwnd = int(hwnd)
            self.thread_id = int(kernel32.GetCurrentThreadId())

            edit = user32.CreateWindowExW(
                0, "EDIT", "",
                WS_CHILD | WS_VISIBLE | ES_MULTILINE | WS_VSCROLL,
                16, 16, self.width - 48, 120,
                hwnd, 101, hinstance, None,
            )
            if not edit:
                self._failed = f"EDIT creation failed: {ctypes.GetLastError()}"
                self._ready.set()
                return
            self.edit_hwnd = int(edit)

            # SW_SHOWMINNOACTIVATE: visible to enumeration, never activated,
            # never shown on screen, user's foreground untouched.
            user32.ShowWindowAsync(hwnd, SW_SHOWMINNOACTIVATE)
            user32.UpdateWindow(hwnd)
            # Thread-local focus so background WM_CHAR lands in the EDIT.
            user32.SetFocus(edit)
            self._ready.set()

            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:  # pragma: no cover - harness must not hang tests
            self._failed = f"{type(exc).__name__}: {exc}"
            self._ready.set()

    def start(self, timeout: float = 10.0) -> None:
        if os.name != "nt":
            raise RuntimeError("TestWindow requires Windows")
        self._thread = threading.Thread(target=self._run, name="karox-live-window", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("test window did not become ready")
        if self._failed:
            raise RuntimeError(self._failed)

    def focus_edit(self, timeout: float = 2.0) -> None:
        """Ask the owning thread to set its internal focus to the EDIT child.

        Only valid once the window is restored: Windows refuses thread focus
        for a minimized window. This never touches the global foreground.
        """
        if not self.hwnd:
            return
        ctypes.windll.user32.PostMessageW(self.hwnd, WM_APP_FOCUS, 0, 0)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            time.sleep(0.02)
            user32 = ctypes.windll.user32
            user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            pid = wintypes.DWORD()
            thread_id = int(user32.GetWindowThreadProcessId(self.hwnd, ctypes.byref(pid)) or 0)
            if not thread_id:
                continue

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

            info = GUITHREADINFO()
            info.cbSize = ctypes.sizeof(GUITHREADINFO)
            if user32.GetGUIThreadInfo(thread_id, ctypes.byref(info)) and int(
                info.hwndFocus or 0
            ) == self.edit_hwnd:
                return

    def stop(self, timeout: float = 5.0) -> None:
        if self.hwnd:
            ctypes.windll.user32.PostMessageW(self.hwnd, WM_APP_CLOSE, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout)
        self.hwnd = 0
        self.edit_hwnd = 0


__all__ = ["TestWindow"]
