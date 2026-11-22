"""The system clipboard, both ways, with no third-party library.

Pictures: read as a PNG file (``/paste``, Alt+V, Ctrl+V in the chat). Windows reads
through the Win32 clipboard API (the PNG a browser or the Snipping Tool puts there,
else the plain bitmap, converted here); macOS uses ``pngpaste`` or AppleScript; Linux
uses ``wl-paste`` (Wayland) or ``xclip`` (X11).

Text: ``grab_text`` reads it (Ctrl+V), ``copy_text`` writes it (mouse-drag copy,
``/copy``). Windows writes CF_UNICODETEXT through ctypes Win32 calls, macOS ``pbcopy``,
Linux ``wl-copy`` / ``xclip`` / ``xsel``; when none of these can work (an SSH session),
``copy_text`` falls back to an OSC 52 sequence for the caller to emit.

KAROX_FAKE_CLIPBOARD=<dir> replaces the real clipboard with files in <dir>
(``text`` holds text, ``image.png`` a picture): how the terminal tests exercise these
paths without touching the user's clipboard.
"""

from __future__ import annotations

import base64
import contextlib
import os
import shutil
import struct
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path, PurePath
from typing import Any

MAX_PIXELS = 40_000_000  # refuse absurd bitmaps before allocating for them
_FAKE_ENV = "KAROX_FAKE_CLIPBOARD"


class ClipboardError(Exception):
    pass


def _fake_dir() -> Path | None:
    raw = os.environ.get(_FAKE_ENV)
    return Path(raw) if raw else None


def grab_image(target: Path) -> Path:
    """Write the clipboard's picture to ``target`` (a .png path) and return it.
    Raises ClipboardError with a message for the user when there is none."""
    fake = _fake_dir()
    if fake is not None:
        source = fake / "image.png"
        if not source.is_file():
            raise ClipboardError(
                "the clipboard holds no picture (copy an image or a screenshot first)"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        return target
    if sys.platform == "win32":
        data = _windows_png()
    elif sys.platform == "darwin":
        data = _mac_png(target)
    else:
        data = _linux_png()
    if not data:
        raise ClipboardError("the clipboard holds no picture (copy an image or a screenshot first)")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    return target


# -- text: read (Ctrl+V) --------------------------------------------------------------


def grab_text() -> str | None:
    """The clipboard's text; None when it holds none (an image-only clipboard, files,
    nothing). Raises ClipboardError only when no clipboard can be read at all."""
    fake = _fake_dir()
    if fake is not None:
        try:
            return (fake / "text").read_text(encoding="utf-8")
        except OSError:
            return None
    if sys.platform == "win32":
        return _windows_text_get()
    if sys.platform == "darwin":
        if not shutil.which("pbpaste"):
            raise ClipboardError("reading the clipboard needs pbpaste")
        data = _run(["pbpaste"])
        return data.decode("utf-8", errors="replace") if data else None
    for command in (
        ["wl-paste", "--no-newline"],
        ["xclip", "-selection", "clipboard", "-o"],
        ["xsel", "--clipboard", "--output"],
    ):
        if shutil.which(command[0]):
            data = _run(command)
            return data.decode("utf-8", errors="replace") if data else None
    raise ClipboardError("reading the clipboard needs wl-paste (Wayland) or xclip/xsel (X11)")


def _windows_text_get() -> str | None:
    import ctypes
    from ctypes import wintypes

    windll: Any = getattr(ctypes, "windll")  # noqa: B009 - Windows only
    user32, kernel32 = windll.user32, windll.kernel32
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    if not user32.OpenClipboard(None):
        raise ClipboardError("the clipboard is in use by another program; try again")
    try:
        if not user32.IsClipboardFormatAvailable(13):  # CF_UNICODETEXT
            return None
        handle = user32.GetClipboardData(13)
        if not handle:
            return None
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            return None
        try:
            # Bounded by the allocation size, then the terminator cut off.
            return ctypes.wstring_at(pointer, kernel32.GlobalSize(handle) // 2).rstrip("\x00")
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()


# -- text: write (drag copy, /copy) ---------------------------------------------------


def osc52(text: str) -> str:
    """The OSC 52 escape sequence that asks the terminal to put ``text`` on the
    clipboard (works over SSH; many terminals honour it)."""
    return "\x1b]52;c;" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "\x07"


def copy_text(text: str, *, emit_osc52: Callable[[str], None] | None = None) -> str:
    """Put ``text`` on the system clipboard and return how it got there
    (``"win32"``, ``"pbcopy"``, ``"wl-copy"``, ``"xclip"``, ``"xsel"`` or ``"osc52"``
    when no system mechanism worked and ``emit_osc52`` was called instead).
    Raises ClipboardError with a readable reason when nothing works."""
    fake = _fake_dir()
    if fake is not None:
        fake.mkdir(parents=True, exist_ok=True)
        (fake / "text").write_text(text, encoding="utf-8")
        return "fake"
    if sys.platform == "win32":
        _windows_text_set(text)
        return "win32"
    reasons: list[str] = []
    commands: list[tuple[str, list[str]]]
    if sys.platform == "darwin":
        commands = [("pbcopy", ["pbcopy"])]
    else:
        commands = [
            ("wl-copy", ["wl-copy"]),
            ("xclip", ["xclip", "-selection", "clipboard"]),
            ("xsel", ["xsel", "--clipboard", "--input"]),
        ]
    for name, command in commands:
        if not shutil.which(name):
            continue
        try:
            _pipe(command, text.encode("utf-8"))
        except ClipboardError as exc:
            reasons.append(str(exc))
            continue
        return name
    if emit_osc52 is not None:
        emit_osc52(osc52(text))
        return "osc52"
    tried = "pbcopy" if sys.platform == "darwin" else "wl-copy, xclip or xsel"
    detail = f" ({'; '.join(reasons)})" if reasons else ""
    raise ClipboardError(
        f"no clipboard tool found: install {tried} to copy{detail}; "
        "in an SSH session a terminal with OSC 52 works too"
    )


def _windows_text_set(text: str) -> None:
    """CF_UNICODETEXT through the Win32 clipboard API (Cyrillic and friends survive:
    the data is UTF-16)."""
    import ctypes
    from ctypes import wintypes

    windll: Any = getattr(ctypes, "windll")  # noqa: B009 - Windows only
    user32, kernel32 = windll.user32, windll.kernel32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]

    data = (text + "\x00").encode("utf-16-le")
    handle = kernel32.GlobalAlloc(0x0042, len(data))  # GMEM_MOVEABLE | GMEM_DDESHARE
    if not handle:
        raise ClipboardError("could not allocate clipboard memory")
    try:
        pointer = kernel32.GlobalLock(handle)
        if not pointer:
            raise ClipboardError("could not lock clipboard memory")
        ctypes.memmove(pointer, data, len(data))
        kernel32.GlobalUnlock(handle)
        if not user32.OpenClipboard(None):
            raise ClipboardError("the clipboard is in use by another program; try again")
        try:
            if not user32.EmptyClipboard():
                raise ClipboardError("could not empty the clipboard")
            if not user32.SetClipboardData(13, handle):  # CF_UNICODETEXT
                raise ClipboardError("SetClipboardData failed")
            handle = None  # the clipboard owns the memory now
        finally:
            user32.CloseClipboard()
    finally:
        if handle:
            kernel32.GlobalFree(handle)


def _pipe(command: list[str], data: bytes) -> None:
    try:
        proc = subprocess.run(command, input=data, capture_output=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ClipboardError(f"{command[0]} failed to start: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", errors="replace").strip().splitlines()
        raise ClipboardError(f"{command[0]} failed: {detail[0] if detail else 'no details'}")


def dib_to_png(dib: bytes) -> bytes:
    """A Windows device-independent bitmap (CF_DIB: header + pixels, 24 or 32 bits per
    pixel, uncompressed) as PNG bytes."""
    from karox6.tools.screen import bgra_to_rgb, encode_png

    if len(dib) < 40:
        raise ClipboardError("the clipboard bitmap is damaged")
    size, width, height, _planes, bits, compression = struct.unpack_from("<IiiHHI", dib, 0)
    top_down = height < 0
    height = abs(height)
    if width <= 0 or height <= 0 or width * height > MAX_PIXELS:
        raise ClipboardError("the clipboard bitmap has an unusable size")
    if bits not in (24, 32) or compression not in (0, 3):
        raise ClipboardError(f"clipboard bitmaps with {bits} bits per pixel are not supported")
    offset = size + (12 if compression == 3 and size == 40 else 0)  # BI_BITFIELDS masks
    stride = (width * bits // 8 + 3) & ~3
    if len(dib) < offset + stride * height:
        raise ClipboardError("the clipboard bitmap is cut short")
    rows = []
    for y in range(height):
        source = y if top_down else height - 1 - y
        row = dib[offset + source * stride : offset + source * stride + width * bits // 8]
        if bits == 32:
            rows.append(bgra_to_rgb(row))
        else:
            rgb = bytearray(row)
            rgb[0::3], rgb[2::3] = row[2::3], row[0::3]
            rows.append(bytes(rgb))
    return encode_png(width, height, b"".join(rows))


def _windows_png() -> bytes | None:
    import ctypes
    from ctypes import wintypes

    windll: Any = getattr(ctypes, "windll")  # noqa: B009 - Windows only
    user32, kernel32 = windll.user32, windll.kernel32
    user32.GetClipboardData.restype = ctypes.c_void_p
    user32.GetClipboardData.argtypes = [wintypes.UINT]
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalSize.restype = ctypes.c_size_t
    kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
    png_format = user32.RegisterClipboardFormatW("PNG")
    if not user32.OpenClipboard(None):
        raise ClipboardError("the clipboard is in use by another program; try again")
    try:
        for fmt in (png_format, 8):  # 8 = CF_DIB
            if not fmt or not user32.IsClipboardFormatAvailable(fmt):
                continue
            handle = user32.GetClipboardData(fmt)
            if not handle:
                continue
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                continue
            try:
                data = ctypes.string_at(pointer, kernel32.GlobalSize(handle))
            finally:
                kernel32.GlobalUnlock(handle)
            return data if fmt == png_format else dib_to_png(data)
    finally:
        user32.CloseClipboard()
    return None


def _run(command: list[str]) -> bytes | None:
    try:
        proc = subprocess.run(
            command, stdin=subprocess.DEVNULL, capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 and proc.stdout else None


def _mac_png(target: Path) -> bytes | None:
    if shutil.which("pngpaste"):
        return _run(["pngpaste", "-"])
    target.parent.mkdir(parents=True, exist_ok=True)
    _run(["osascript", "-e", _mac_script(target)])
    try:
        data = target.read_bytes()
    except OSError:
        return None
    if not data:
        # The script creates the file before it knows whether there is a picture.
        with contextlib.suppress(OSError):
            target.unlink()
        return None
    return data


def _mac_script(target: PurePath) -> str:
    """AppleScript that writes the clipboard's PNG to ``target`` (an empty file when the
    clipboard holds no picture)."""
    backslash = chr(92)
    quoted = str(target).replace(backslash, backslash * 2).replace('"', backslash + '"')
    return (
        f'set f to (open for access POSIX file "{quoted}" with write permission)\n'
        "try\n  write (the clipboard as «class PNGf») to f\nend try\nclose access f"
    )


def _linux_png() -> bytes | None:
    if shutil.which("wl-paste"):
        data = _run(["wl-paste", "--no-newline", "--type", "image/png"])
        if data:
            return data
    if shutil.which("xclip"):
        return _run(["xclip", "-selection", "clipboard", "-t", "image/png", "-o"])
    if not shutil.which("wl-paste"):
        raise ClipboardError("reading the clipboard needs wl-paste (Wayland) or xclip (X11)")
    return None
