"""OS clipboard access for delivering secrets without printing them.

Bridge credential rotation needs to hand a freshly generated
``Bearer <secret>`` value to the user while keeping it out of stdout, stderr,
logs and snapshots.  These helpers wrap the platform clipboard behind a
fail-soft, monkeypatchable interface so the flow can be exercised in tests
without a desktop session and without ever materialising the secret in a log.

Nothing here logs clipboard contents.  Secret-delivery callers use
:func:`write_text`, which forwards the value verbatim and reports only a boolean
success.  :func:`read_text` is a separate, user-gesture-only paste helper for the
local TUI; it returns clipboard text to that local process and is never polled in
the background.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from typing import Optional

#: How long a copied ``Bearer`` value lives on the clipboard before it is
#: overwritten.  Tuned for "long enough to paste, short enough to forget".
CLIPBOARD_AUTO_CLEAR_SECONDS = 120


def bearer_value(secret: str) -> str:
    """Build the exact ``Authorization`` header value a bridge client expects.

    Centralising this string means tests can assert on a single source of
    truth instead of every call site reconstructing the prefix.
    """
    if not isinstance(secret, str) or not secret:
        raise ValueError("secret must be a non-empty string")
    return f"Bearer {secret}"


def _platform_command() -> Optional[list[str]]:
    """Return the clipboard command for this platform, or ``None`` if unknown.

    On Windows ``Set-Clipboard`` is fed over stdin so the secret never has to
    be quoted into a command line (avoiding both injection and encoding
    pitfalls).  macOS and X11 use their conventional filters.
    """
    if os.name == "nt":
        return ["powershell.exe", "-NoProfile", "-Command", "$input | Set-Clipboard"]
    if shutil.which("pbcopy"):
        return ["pbcopy"]
    if shutil.which("xclip"):
        return ["xclip", "-selection", "clipboard"]
    if shutil.which("xsel"):
        return ["xsel", "--clipboard", "--input"]
    return None


def read_text(*, max_chars: int = 2_000_000) -> Optional[str]:
    """Read clipboard text after an explicit local paste gesture.

    Windows uses the native clipboard API directly: no shell, command line, log,
    or background polling sees the clipboard. Other platforms intentionally
    return ``None`` for now and keep relying on the terminal's Paste event.
    """
    try:
        limit = max(1, int(max_chars))
    except (TypeError, ValueError):
        limit = 2_000_000
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        user32.OpenClipboard.argtypes = [wintypes.HWND]
        user32.OpenClipboard.restype = wintypes.BOOL
        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL
        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = wintypes.HANDLE
        kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalLock.restype = wintypes.LPVOID
        kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        if not user32.OpenClipboard(None):
            return None
        try:
            handle = user32.GetClipboardData(13)  # CF_UNICODETEXT
            if not handle:
                return None
            pointer = kernel32.GlobalLock(handle)
            if not pointer:
                return None
            try:
                value = ctypes.wstring_at(pointer)
            finally:
                kernel32.GlobalUnlock(handle)
        finally:
            user32.CloseClipboard()
    except Exception:
        return None
    if not value or len(value) > limit:
        return None
    return value


def write_text(text: str) -> bool:
    """Place ``text`` on the system clipboard.

    Returns ``True`` only when a platform clipboard was found and the write
    exited cleanly.  Any failure -- missing binary, non-zero exit, timeout,
    OS error -- returns ``False`` rather than raising, so a clipboard problem
    can never become a reason to print a secret to the console as a fallback.
    """
    if not isinstance(text, str):
        return False
    command = _platform_command()
    if command is None:
        return False
    try:
        completed = subprocess.run(
            command,
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
            # Set-Clipboard is a utility child, never an interactive shell.
            # Without this flag Windows may flash a PowerShell window both on
            # copy and again when the auto-clear timer overwrites the clipboard.
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0)
                if os.name == "nt"
                else 0
            ),
        )
    except Exception:
        # Intentionally broad: the clipboard is a convenience channel, and a
        # failure here must never propagate far enough to tempt a caller into
        # printing the secret instead.
        return False
    return completed.returncode == 0


def schedule_clear(seconds: int = CLIPBOARD_AUTO_CLEAR_SECONDS) -> Optional[threading.Timer]:
    """Overwrite the clipboard with an empty string after ``seconds``.

    Fail-soft by construction: the timer thread swallows every exception and a
    failure to schedule (or to clear) simply leaves the previous value in
    place -- it never raises, and it never logs the value being cleared.

    Returns the started daemon :class:`threading.Timer` (or ``None`` if
    scheduling itself failed) so a caller that wants to cancel early can.
    """
    try:
        delay = max(1, int(seconds))
    except (TypeError, ValueError):
        delay = CLIPBOARD_AUTO_CLEAR_SECONDS

    def _clear() -> None:
        try:
            write_text("")
        except Exception:
            pass

    try:
        timer = threading.Timer(delay, _clear)
        timer.daemon = True
        timer.start()
        return timer
    except Exception:
        return None
