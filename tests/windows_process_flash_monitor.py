from __future__ import annotations

import ctypes
import json
import os
import time
from ctypes import wintypes
from pathlib import Path

OUTPUT = Path("benchmarks/agent_throughput/latest_windows_process_flash.json")
DURATION_SECONDS = 30.0
POLL_SECONDS = 0.025

TH32CS_SNAPPROCESS = 0x00000002
MAX_PATH = 260


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * MAX_PATH),
    ]


def snapshot() -> dict[int, tuple[str, int]]:
    if os.name != "nt":
        return {}
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if handle == wintypes.HANDLE(-1).value:
        return {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    result: dict[int, tuple[str, int]] = {}
    try:
        ok = kernel32.Process32FirstW(handle, ctypes.byref(entry))
        while ok:
            result[int(entry.th32ProcessID)] = (
                str(entry.szExeFile),
                int(entry.th32ParentProcessID),
            )
            ok = kernel32.Process32NextW(handle, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(handle)
    return result


def visible_windows(pids: set[int]) -> dict[int, list[str]]:
    if os.name != "nt" or not pids:
        return {}
    user32 = ctypes.windll.user32
    found: dict[int, list[str]] = {}
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def collect(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        process_id = int(pid.value)
        if process_id not in pids:
            return True
        length = int(user32.GetWindowTextLengthW(hwnd))
        buffer = ctypes.create_unicode_buffer(max(1, length + 1))
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        found.setdefault(process_id, []).append(buffer.value)
        return True

    user32.EnumWindows(collect, 0)
    return found


def main() -> None:
    initial = snapshot()
    known = set(initial)
    observed: dict[int, dict[str, object]] = {}
    started = time.perf_counter()
    while time.perf_counter() - started < DURATION_SECONDS:
        current = snapshot()
        now_ms = round((time.perf_counter() - started) * 1000.0, 1)
        for pid, (name, ppid) in current.items():
            if pid in known:
                continue
            parent_name = current.get(ppid, initial.get(ppid, ("", 0)))[0]
            observed[pid] = {
                "pid": pid,
                "name": name,
                "parent_pid": ppid,
                "parent_name": parent_name,
                "first_seen_ms": now_ms,
            }
            known.add(pid)
        time.sleep(POLL_SECONDS)

    interesting = sorted(
        observed.values(),
        key=lambda item: float(item["first_seen_ms"]),
    )
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(
        json.dumps(
            {
                "duration_seconds": DURATION_SECONDS,
                "poll_ms": POLL_SECONDS * 1000.0,
                "new_processes": interesting,
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"captured={len(interesting)} output={OUTPUT}")


def test_monitor_process_flashes() -> None:
    main()


if __name__ == "__main__":
    main()
