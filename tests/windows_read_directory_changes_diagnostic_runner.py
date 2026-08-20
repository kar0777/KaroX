from __future__ import annotations

import ctypes
import json
import os
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks" / "agent_throughput" / "latest_windows_read_directory_changes.json"

FILE_LIST_DIRECTORY = 0x0001
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILE_NOTIFY_CHANGE_FILE_NAME = 0x00000001
FILE_NOTIFY_CHANGE_DIR_NAME = 0x00000002
FILE_NOTIFY_CHANGE_SIZE = 0x00000008
FILE_NOTIFY_CHANGE_LAST_WRITE = 0x00000010
FILE_NOTIFY_CHANGE_CREATION = 0x00000040


def _probe(root: Path) -> dict[str, object]:
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

    started = time.perf_counter()
    handle = create_file(
        str(root),
        FILE_LIST_DIRECTORY,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        FILE_FLAG_BACKUP_SEMANTICS,
        None,
    )
    invalid = wintypes.HANDLE(-1).value
    if handle == invalid:
        raise OSError(ctypes.get_last_error(), "CreateFileW failed")
    handle_ms = (time.perf_counter() - started) * 1000

    changed = threading.Event()
    ready = threading.Event()
    returned = wintypes.DWORD(0)
    buffer = ctypes.create_string_buffer(64 * 1024)
    error: list[int] = []

    def watch() -> None:
        ready.set()
        ok = read_changes(
            handle,
            buffer,
            len(buffer),
            True,
            FILE_NOTIFY_CHANGE_FILE_NAME
            | FILE_NOTIFY_CHANGE_DIR_NAME
            | FILE_NOTIFY_CHANGE_SIZE
            | FILE_NOTIFY_CHANGE_LAST_WRITE
            | FILE_NOTIFY_CHANGE_CREATION,
            ctypes.byref(returned),
            None,
            None,
        )
        if ok and returned.value:
            changed.set()
        elif not ok:
            error.append(ctypes.get_last_error())

    thread = threading.Thread(target=watch, name="karox-rdcw-proof", daemon=True)
    thread.start()
    assert ready.wait(1.0)
    # In production the initial git-status identity takes much longer than this,
    # so this bounded delay is conservative for the proof and removes thread
    # scheduling noise from the event-delivery measurement.
    time.sleep(0.05)

    target = root / "same-size.txt"
    target.write_bytes(b"alpha\n")
    assert changed.wait(1.0), error

    # Re-arm and test the hard case: same size + restored mtime.
    changed.clear()
    returned.value = 0
    thread.join(timeout=1.0)
    before = target.stat()
    error.clear()
    thread = threading.Thread(target=watch, name="karox-rdcw-proof-2", daemon=True)
    thread.start()
    assert ready.wait(1.0)
    time.sleep(0.05)
    detected_started = time.perf_counter()
    target.write_bytes(b"omega\n")
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    detected = changed.wait(1.0)
    detect_ms = (time.perf_counter() - detected_started) * 1000

    cancel_io(handle, None)
    close_handle(handle)
    thread.join(timeout=1.0)
    return {
        "handle_open_ms": round(handle_ms, 4),
        "same_size_restored_mtime_detected": detected,
        "detection_ms": round(detect_ms, 4),
        "bytes_returned": int(returned.value),
        "errors": error,
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows-only ReadDirectoryChangesW proof")
def test_read_directory_changes_detects_hard_case() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        result = _probe(Path(temporary))
    payload = {
        "schema_version": 1,
        "benchmark": "windows-read-directory-changes-hard-case",
        "result": result,
        "isolation": {"temporary_directory": True, "live_bridge_touched": False},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert result["same_size_restored_mtime_detected"] is True
    assert not result["errors"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
