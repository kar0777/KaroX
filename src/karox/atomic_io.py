"""One atomic file write, shared by Core and the workspace transaction.

Both paths wrote the same tempfile/fchmod/fsync/replace sequence; a policy
change (permissions, durability, cleanup) had to be made twice and could drift.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional


#: Sources whose compiled form CPython caches and validates by mtime.
_BYTECODE_SOURCES = frozenset({".py", ".pyw"})
_SECOND_NS = 1_000_000_000


def new_file_mode() -> int:
    """The mode an ordinary editor gives a new file: 0666 minus the umask.

    ``mkstemp`` creates its temporary 0600; without this a file the agent
    created stayed private to its user while every other new file in the
    project followed the umask.
    """

    mask = os.umask(0o022)
    os.umask(mask)
    return 0o666 & ~mask


def atomic_write(path: Path, content: bytes, mode: Optional[int] = None) -> None:
    """Replace ``path`` with ``content`` atomically, preserving ``mode``."""

    path.parent.mkdir(parents=True, exist_ok=True)
    previous_mtime_ns = _bytecode_source_mtime_ns(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            if mode is not None and hasattr(os, "fchmod"):
                os.fchmod(handle.fileno(), mode)
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
    if previous_mtime_ns is not None:
        _advance_past_cached_bytecode(path, previous_mtime_ns)


def _bytecode_source_mtime_ns(path: Path) -> Optional[int]:
    if path.suffix.lower() not in _BYTECODE_SOURCES:
        return None
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _advance_past_cached_bytecode(path: Path, previous_mtime_ns: int) -> None:
    """Give a rewritten Python source a later whole second than its predecessor.

    CPython (and pytest's assertion rewriter) trust a cached ``.pyc`` while
    the source's whole-second mtime and its size match the values recorded at
    compile time. Two versions of a module written within one second with the
    same length are indistinguishable to that check, so a test run after the
    second write silently imported the first: a correct repair verified red,
    and a broken one could verify green. Moving the new version's mtime past
    the previous version's second makes every cached form of it stale. Only
    the timestamp changes; the cache itself is never touched.
    """

    try:
        current = path.stat()
    except OSError:
        return
    previous_second = previous_mtime_ns // _SECOND_NS
    if current.st_mtime_ns // _SECOND_NS > previous_second:
        return
    try:
        os.utime(
            path,
            ns=(current.st_atime_ns, (previous_second + 1) * _SECOND_NS),
        )
    except OSError:
        pass
