from __future__ import annotations

import ctypes
import json
import os
import struct
import subprocess
import tempfile
import threading
import time
from ctypes import wintypes
from pathlib import Path

import pytest

from _support import SRC, initialize_git_repository  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "benchmarks" / "agent_throughput" / "latest_windows_git_status_watcher_noise.json"

FILE_LIST_DIRECTORY = 0x0001
SHARE = 0x00000001 | 0x00000002 | 0x00000004
OPEN_EXISTING = 3
FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
FILTER = 0x00000001 | 0x00000002 | 0x00000008 | 0x00000010 | 0x00000040


class WatchOnce:
    def __init__(self, root: Path) -> None:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel32 = kernel32
        self.handle = kernel32.CreateFileW(str(root), FILE_LIST_DIRECTORY, SHARE, None, OPEN_EXISTING, FILE_FLAG_BACKUP_SEMANTICS, None)
        self.changed = threading.Event()
        self.ready = threading.Event()
        self.returned = wintypes.DWORD(0)
        self.buffer = ctypes.create_string_buffer(64 * 1024)
        self.error = 0
        self.paths: list[str] = []
        self.events: list[dict[str, object]] = []
        self.thread = threading.Thread(target=self._watch, daemon=True)
        self.thread.start()
        self.ready.wait(1.0)

    def _watch(self) -> None:
        self.ready.set()
        ok = self.kernel32.ReadDirectoryChangesW(
            self.handle,
            self.buffer,
            len(self.buffer),
            True,
            FILTER,
            ctypes.byref(self.returned),
            None,
            None,
        )
        if ok and self.returned.value:
            raw = self.buffer.raw[: self.returned.value]
            offset = 0
            while offset + 12 <= len(raw):
                next_offset, action, name_bytes = struct.unpack_from("<III", raw, offset)
                start = offset + 12
                end = min(len(raw), start + name_bytes)
                path = raw[start:end].decode("utf-16-le", errors="replace")
                self.paths.append(path)
                self.events.append({"action": int(action), "path": path})
                if not next_offset:
                    break
                offset += next_offset
            self.changed.set()
        elif not ok:
            self.error = ctypes.get_last_error()

    def close(self) -> None:
        self.kernel32.CancelIoEx(self.handle, None)
        self.kernel32.CloseHandle(self.handle)
        self.thread.join(timeout=1.0)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _probe(repo: Path) -> dict[str, object]:
    watcher = WatchOnce(repo)
    time.sleep(0.02)
    started = time.perf_counter()
    _git(repo, "status", "--porcelain=v1", "--untracked-files=normal", "-z")
    status_ms = (time.perf_counter() - started) * 1000
    noisy = watcher.changed.wait(0.15)
    result = {
        "status_ms": round(status_ms, 3),
        "watcher_changed": noisy,
        "bytes_returned": int(watcher.returned.value),
        "paths": list(watcher.paths),
        "events": list(watcher.events),
        "error": watcher.error,
    }
    watcher.close()
    return result


@pytest.mark.skipif(os.name != "nt", reason="Windows-only watcher diagnostic")
def test_git_status_watcher_noise() -> None:
    with tempfile.TemporaryDirectory() as temporary:
        repo = Path(temporary) / "repo"
        initialize_git_repository(repo)
        (repo / "tracked.txt").write_bytes(b"before\n")
        _git(repo, "add", ".")
        _git(repo, "-c", "user.name=KaroX Test", "-c", "user.email=karox@example.invalid", "commit", "-m", "fixture")
        clean_first = _probe(repo)
        clean_second = _probe(repo)
        (repo / "tracked.txt").write_bytes(b"after-with-different-size\n")
        dirty = _probe(repo)
    payload = {
        "schema_version": 1,
        "benchmark": "windows-git-status-watcher-noise",
        "clean_first": clean_first,
        "clean_second": clean_second,
        "dirty": dirty,
        "isolation": {"temporary_repository": True, "live_bridge_touched": False},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    assert not clean_first["error"]
    assert not clean_second["error"]
    assert not dirty["error"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__]))
