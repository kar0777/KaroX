"""Test tools for the Windows clipboard code: a fake Win32 API that enforces the
contract the real one has (a clipboard written without an owner window refuses
``SetClipboardData``), and a save / restore of a whole clipboard, used by the opt-in
real-clipboard test and checked here against the fake before it is ever pointed at a
user's real clipboard."""

from __future__ import annotations

import ctypes
import types
from typing import Any

from karox6 import clipboard

CF_TEXT, CF_OEMTEXT, CF_DIB, CF_UNICODETEXT, CF_HDROP, CF_LOCALE, CF_DIBV5 = 1, 7, 8, 13, 15, 16, 17
_MEMORY_FORMATS = {CF_TEXT, CF_OEMTEXT, CF_DIB, CF_UNICODETEXT, CF_HDROP, CF_LOCALE, CF_DIBV5}
_HWND_MESSAGE = -3  # CreateWindowEx parent of a message-only window


class FakeWin32:
    """``clipboard._win32()`` stand-in: a clipboard in a dict and memory in ctypes
    buffers, with a log of every call and switches to make single calls fail."""

    def __init__(self) -> None:
        self.formats: dict[int, bytes] = {}
        self.log: list[str] = []
        self.memory: dict[int, Any] = {}  # live handles -> buffer
        self.on_clipboard: set[int] = set()  # handles the clipboard owns now
        self.windows: set[int] = set()
        self.is_open = False
        self.open_owner: int | None = None
        self.owner: int | None = None
        self.fail: set[str] = set()  # "open", "empty", "set", "lock", "alloc", "window"
        self.open_failures = 0  # OpenClipboard fails this many times first
        self._next = 0x1000
        self._registered: dict[str, int] = {}
        self.user32 = types.SimpleNamespace(
            OpenClipboard=self._open,
            CloseClipboard=self._close,
            EmptyClipboard=self._empty,
            IsClipboardFormatAvailable=lambda fmt: int(fmt in self.formats),
            GetClipboardData=self._get,
            SetClipboardData=self._set,
            RegisterClipboardFormatW=self._register,
            EnumClipboardFormats=self._enum,
            CreateWindowExW=self._create_window,
            DestroyWindow=self._destroy_window,
        )
        self.kernel32 = types.SimpleNamespace(
            GetConsoleWindow=lambda: 0,
            GlobalAlloc=self._alloc,
            GlobalLock=self._lock,
            GlobalUnlock=lambda handle: 1,
            GlobalFree=self._free,
            GlobalSize=lambda handle: len(self.memory[handle]) if handle in self.memory else 0,
        )

    # -- windows and memory ---------------------------------------------------------------

    def _new(self) -> int:
        self._next += 0x10
        return self._next

    def _create_window(self, *args: Any) -> int:
        self.log.append("CreateWindowEx")
        assert args[8] == _HWND_MESSAGE, "the owner window must be message-only"
        if "window" in self.fail:
            return 0
        hwnd = self._new()
        self.windows.add(hwnd)
        return hwnd

    def _destroy_window(self, hwnd: int) -> int:
        self.log.append("DestroyWindow")
        self.windows.discard(hwnd)
        return 1

    def _alloc(self, flags: int, size: int) -> int:
        self.log.append("GlobalAlloc")
        if "alloc" in self.fail:
            return 0
        handle = self._new()
        self.memory[handle] = ctypes.create_string_buffer(size)
        return handle

    def _lock(self, handle: int) -> int:
        self.log.append("GlobalLock")
        if "lock" in self.fail:
            return 0
        return ctypes.addressof(self.memory[handle])

    def _free(self, handle: int) -> int:
        self.log.append("GlobalFree")
        self.memory.pop(handle, None)
        return 0

    # -- the clipboard --------------------------------------------------------------------

    def _open(self, owner: int | None) -> int:
        self.log.append(f"OpenClipboard({'NULL' if not owner else 'hwnd'})")
        assert not self.is_open, "the clipboard is already open"
        if "open" in self.fail:
            return 0
        if self.open_failures:
            self.open_failures -= 1
            return 0
        self.is_open, self.open_owner = True, owner or None
        return 1

    def _close(self) -> int:
        self.log.append("CloseClipboard")
        assert self.is_open, "CloseClipboard without OpenClipboard"
        self.is_open = False
        return 1

    def _empty(self) -> int:
        self.log.append("EmptyClipboard")
        assert self.is_open
        if "empty" in self.fail:
            return 0
        self.formats.clear()
        for handle in self.on_clipboard:
            self.memory.pop(handle, None)
        self.on_clipboard.clear()
        self.owner = self.open_owner  # per the docs: the window that opened it, or none
        return 1

    def _set(self, fmt: int, handle: int) -> int:
        self.log.append("SetClipboardData")
        assert self.is_open
        if "set" in self.fail or self.owner is None:
            return 0  # no owner: the documented failure of "OpenClipboard(NULL)"
        self.formats[fmt] = ctypes.string_at(
            ctypes.addressof(self.memory[handle]), len(self.memory[handle])
        )
        self.on_clipboard.add(handle)
        return handle

    def _get(self, fmt: int) -> int:
        assert self.is_open
        if fmt not in self.formats:
            return 0
        handle = self._new()
        buffer = ctypes.create_string_buffer(self.formats[fmt], len(self.formats[fmt]))
        self.memory[handle] = buffer
        self.on_clipboard.add(handle)
        return handle

    def _register(self, name: str) -> int:
        return self._registered.setdefault(name, 0xC000 + len(self._registered))

    def _enum(self, fmt: int) -> int:
        later = sorted(f for f in self.formats if f > fmt)
        return later[0] if later else 0

    # -- checks ---------------------------------------------------------------------------

    def leaked(self) -> set[int]:
        """Memory still allocated that no clipboard owns, plus windows still alive."""
        return (set(self.memory) - self.on_clipboard) | self.windows


# -- save and restore a clipboard ---------------------------------------------------------


def snapshot(api: Any) -> list[tuple[int, bytes]]:
    """Every format of the clipboard that is plain memory (text, bitmaps, a PNG, file
    lists, ...), as (format, bytes). Handles that are not memory (GDI bitmaps and
    metafiles) are skipped: Windows makes them from the memory formats on demand."""
    user32 = api.user32
    saved: list[tuple[int, bytes]] = []
    clipboard._open_clipboard(user32)
    try:
        fmt = user32.EnumClipboardFormats(0)
        while fmt:
            if fmt in _MEMORY_FORMATS or fmt >= 0xC000:
                handle = user32.GetClipboardData(fmt)
                data = clipboard._read_global(api, handle) if handle else None
                if data:
                    saved.append((fmt, data))
            fmt = user32.EnumClipboardFormats(fmt)
    finally:
        user32.CloseClipboard()
    return saved


def restore(api: Any, saved: list[tuple[int, bytes]]) -> None:
    """Put ``saved`` back: the clipboard is emptied first, so an empty ``saved`` gives
    back an empty clipboard."""
    user32, kernel32 = api.user32, api.kernel32
    owner, ours = clipboard._owner_window(api)
    try:
        clipboard._open_clipboard(user32, owner)
        try:
            user32.EmptyClipboard()
            for fmt, data in saved:
                handle = kernel32.GlobalAlloc(0x0042, len(data))
                pointer = kernel32.GlobalLock(handle)
                ctypes.memmove(pointer, data, len(data))
                kernel32.GlobalUnlock(handle)
                if not user32.SetClipboardData(fmt, handle):
                    kernel32.GlobalFree(handle)
        finally:
            user32.CloseClipboard()
    finally:
        if ours:
            user32.DestroyWindow(owner)
