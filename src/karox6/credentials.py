"""API keys saved by the chat's setup (`/key`), for when no environment variable holds one.

Each saved provider has its own key (``credentials-<name>``); ``credentials`` is the one
of a setup without a name (older versions saved only that one).

Kept in <home>/credentials: on Windows encrypted with DPAPI for the current user (another
account or machine cannot read it), elsewhere as a file only the owner may read. The key
never goes to config.toml, the command line or the logs.
"""

from __future__ import annotations

import base64
import contextlib
import os
import re
import sys
from pathlib import Path
from typing import Any

FILE = "credentials"


def key_path(home: Path, name: str = "") -> Path:
    slug = re.sub(r"[^a-z0-9_-]+", "-", name.lower()).strip("-")
    return home / (f"{FILE}-{slug}" if slug else FILE)


def save_key(home: Path, key: str, name: str = "") -> Path:
    path = key_path(home, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = _protect(key.encode("utf-8"))
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, path)
    return path


def load_key(home: Path, name: str = "") -> str | None:
    try:
        blob = key_path(home, name).read_bytes()
    except OSError:
        return None
    with contextlib.suppress(Exception):
        key = _unprotect(blob).decode("utf-8").strip()
        return key or None
    return None


def forget_key(home: Path, name: str = "") -> None:
    with contextlib.suppress(OSError):
        key_path(home, name).unlink()


def _protect(data: bytes) -> bytes:
    if sys.platform == "win32":
        return b"dpapi:" + base64.b64encode(_dpapi(data, encrypt=True))
    return b"plain:" + base64.b64encode(data)


def _unprotect(blob: bytes) -> bytes:
    kind, _, body = blob.partition(b":")
    raw = base64.b64decode(body)
    if kind == b"dpapi":
        return _dpapi(raw, encrypt=False)
    if kind == b"plain":
        return raw
    raise ValueError("unknown credentials format")


def _dpapi(data: bytes, *, encrypt: bool) -> bytes:
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    windll: Any = getattr(ctypes, "windll")  # noqa: B009 - Windows only
    crypt32 = windll.crypt32
    kernel32 = windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    src = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = Blob()
    call = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    ok = call(ctypes.byref(src), None, None, None, None, 0x1, ctypes.byref(out))  # UI_FORBIDDEN
    if not ok:
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)
