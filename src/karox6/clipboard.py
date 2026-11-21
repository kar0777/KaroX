"""A picture from the system clipboard, saved as a PNG file (``/paste`` in the chat).

Windows: read through the Win32 clipboard API (the PNG a browser or the Snipping Tool
puts there, else the plain bitmap, converted here). macOS: ``pngpaste`` or AppleScript.
Linux: ``wl-paste`` (Wayland) or ``xclip`` (X11). No third-party imaging library.
"""

from __future__ import annotations

import contextlib
import shutil
import struct
import subprocess
import sys
from pathlib import Path, PurePath
from typing import Any

MAX_PIXELS = 40_000_000  # refuse absurd bitmaps before allocating for them


class ClipboardError(Exception):
    pass


def grab_image(target: Path) -> Path:
    """Write the clipboard's picture to ``target`` (a .png path) and return it.
    Raises ClipboardError with a message for the user when there is none."""
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
