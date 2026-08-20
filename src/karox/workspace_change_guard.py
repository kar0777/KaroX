"""Cheap fail-closed workspace change evidence for read-only plan blocks.

The guard is deliberately only an optimization hint.  Callers must fall back to
an authoritative repository identity whenever it is unsupported, uncertain, or
reports a working-tree change.  Windows can monitor a repository subtree without
polling by using ReadDirectoryChangesW; other platforms keep the existing
identity path until an equally strong native backend is added.
"""

from __future__ import annotations

import ctypes
import os
import struct
import threading
from ctypes import wintypes
from pathlib import Path
from typing import Any

_FILE_LIST_DIRECTORY = 0x0001
_FILE_SHARE_READ = 0x00000001
_FILE_SHARE_WRITE = 0x00000002
_FILE_SHARE_DELETE = 0x00000004
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_NOTIFY_FILTER = (
    0x00000001  # FILE_NOTIFY_CHANGE_FILE_NAME
    | 0x00000002  # FILE_NOTIFY_CHANGE_DIR_NAME
    | 0x00000008  # FILE_NOTIFY_CHANGE_SIZE
    | 0x00000010  # FILE_NOTIFY_CHANGE_LAST_WRITE
    | 0x00000040  # FILE_NOTIFY_CHANGE_CREATION
)
_ERROR_OPERATION_ABORTED = 995
_BUFFER_BYTES = 64 * 1024
# Generated/dependency trees are intentionally excluded from repository source
# identity elsewhere in KaroX. Their background churn (Vite, Netlify, coverage,
# package installs) must not invalidate a read-only source inspection plan.
_IGNORED_GENERATED_COMPONENTS = frozenset(
    {
        ".mypy_cache",
        ".netlify",
        ".next",
        ".nuxt",
        ".output",
        ".pytest_cache",
        ".ruff_cache",
        ".svelte-kit",
        ".tox",
        ".turbo",
        ".venv",
        ".vite",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "node_modules",
        "out",
        "site-packages",
        "target",
        "venv",
    }
)


class WorkspaceChangeGuard:
    """Watch one repository for filesystem and Git-control changes.

    ``finish()`` is the synchronization barrier.  A quiet result is trustworthy
    only after the pending OS read has been cancelled and the watcher thread has
    processed every notification delivered before that cancellation.
    """

    def __init__(self, repository: Path) -> None:
        self.repository = repository.expanduser().resolve(strict=True)
        self.supported = False
        self._changed = threading.Event()
        self._failed = threading.Event()
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._handle: Any = None
        self._kernel32: Any = None
        self._close_lock = threading.Lock()
        self._closed = False
        self.ignored_git_events = 0
        self.changed_paths: list[str] = []
        # Submodules and linked worktrees can keep their actual Git metadata
        # outside the selected working tree. Until the guard watches every such
        # gitdir too, use the established two-identity path for those layouts.
        git_marker = self.repository / ".git"
        if (
            os.name != "nt"
            or (self.repository / ".gitmodules").exists()
            or git_marker.is_file()
        ):
            return
        try:
            self._start_windows()
        except Exception:
            self._failed.set()
            self.close()

    @staticmethod
    def _is_internal_git_path(value: str) -> bool:
        normalized = value.replace("\\", "/").casefold()
        return normalized.startswith(".git/")

    @staticmethod
    def _is_ignored_generated_path(value: str) -> bool:
        normalized = value.replace("\\", "/")
        parts = [part.casefold() for part in normalized.split("/") if part]
        return any(part in _IGNORED_GENERATED_COMPONENTS for part in parts)

    @staticmethod
    def _paths(buffer: bytes) -> tuple[str, ...]:
        paths: list[str] = []
        offset = 0
        while offset + 12 <= len(buffer):
            next_offset, _action, name_bytes = struct.unpack_from("<III", buffer, offset)
            start = offset + 12
            end = start + name_bytes
            if end > len(buffer):
                raise ValueError("truncated ReadDirectoryChangesW notification")
            paths.append(buffer[start:end].decode("utf-16-le", errors="replace"))
            if not next_offset:
                break
            if next_offset < 12 or offset + next_offset <= offset:
                raise ValueError("invalid ReadDirectoryChangesW notification offset")
            offset += next_offset
        return tuple(paths)

    def _start_windows(self) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_file = kernel32.CreateFileW
        create_file.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        create_file.restype = wintypes.HANDLE
        read_changes = kernel32.ReadDirectoryChangesW
        read_changes.argtypes = [
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPVOID,
            wintypes.LPVOID,
        ]
        read_changes.restype = wintypes.BOOL
        cancel_io = kernel32.CancelIoEx
        cancel_io.argtypes = [wintypes.HANDLE, wintypes.LPVOID]
        cancel_io.restype = wintypes.BOOL
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL

        handle = create_file(
            str(self.repository),
            _FILE_LIST_DIRECTORY,
            _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
            None,
            _OPEN_EXISTING,
            _FILE_FLAG_BACKUP_SEMANTICS,
            None,
        )
        invalid_handle = wintypes.HANDLE(-1).value
        if handle in (None, invalid_handle):
            raise OSError(ctypes.get_last_error(), "CreateFileW failed")
        self._kernel32 = kernel32
        self._handle = handle
        self._thread = threading.Thread(
            target=self._watch_windows,
            name="karox-workspace-change-guard",
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(1.0):
            raise OSError("ReadDirectoryChangesW watcher did not become ready")
        self.supported = True

    def _watch_windows(self) -> None:
        assert self._kernel32 is not None
        assert self._handle is not None
        read_changes = self._kernel32.ReadDirectoryChangesW
        self._ready.set()
        while not self._stop.is_set():
            returned = wintypes.DWORD(0)
            buffer = ctypes.create_string_buffer(_BUFFER_BYTES)
            ok = read_changes(
                self._handle,
                buffer,
                len(buffer),
                True,
                _NOTIFY_FILTER,
                ctypes.byref(returned),
                None,
                None,
            )
            if not ok:
                error = ctypes.get_last_error()
                if self._stop.is_set() and error == _ERROR_OPERATION_ABORTED:
                    return
                self._failed.set()
                return
            if returned.value == 0:
                # Windows uses a zero-byte successful completion when its kernel
                # change buffer overflowed.  Missing events means the optimization
                # can no longer prove anything, so fail closed to git status.
                self._failed.set()
                return
            try:
                paths = self._paths(buffer.raw[: returned.value])
            except Exception:
                self._failed.set()
                return
            for path in paths:
                if self._is_ignored_generated_path(path):
                    continue
                if len(self.changed_paths) < 16:
                    self.changed_paths.append(path)
                self._changed.set()
                return

    @property
    def changed(self) -> bool:
        return self._changed.is_set()

    @property
    def failed(self) -> bool:
        return self._failed.is_set()

    def finish(self) -> bool:
        """Stop the watcher and return whether it proved the working tree quiet."""
        self.close()
        return self.supported and not self.changed and not self.failed

    def close(self) -> None:
        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._stop.set()
            kernel32 = self._kernel32
            handle = self._handle
            thread = self._thread
            if kernel32 is not None and handle is not None:
                # CancelIoEx is intentionally used rather than closing the handle
                # under a blocking ReadDirectoryChangesW call.  The watcher gets a
                # chance to process a completion that already won the race first.
                kernel32.CancelIoEx(handle, None)
            if thread is not None:
                thread.join(timeout=1.0)
                if thread.is_alive():
                    self._failed.set()
            if kernel32 is not None and handle is not None:
                kernel32.CloseHandle(handle)
            self._handle = None


def start_workspace_change_guard(repository: Path) -> WorkspaceChangeGuard:
    """Return a platform guard; unsupported cases simply force caller fallback."""
    return WorkspaceChangeGuard(repository)
