"""On Windows, the processes KaroX starts end with KaroX, however KaroX ends.

Ctrl+C, SIGTERM and a closed MCP stdin run the cleanup that kills child process trees.
But on Windows an MCP client stops its server with TerminateProcess, and closing the
console window ends KaroX the same way: no cleanup runs, and every command, background
job and MCP server it started kept running. A job object with KILL_ON_JOB_CLOSE makes
Windows itself kill them when the job's last handle closes, which is when KaroX exits.

KaroX joins that job itself before it starts its first child (`join_karox_job`), so
every descendant is born in it: there is no window between a child's start and its
assignment. If joining fails (an old Windows without nested jobs), `bind_to_karox`
assigns each child by pid after it started. A program that explicitly asks to leave
(CREATE_BREAKAWAY_FROM_JOB, as libuv does for a detached launch such as an editor) may;
libuv puts its own children in the same kind of job.

POSIX needs none of this: children run in their own sessions and the cleanup paths
(including SIGTERM) kill those.
"""

from __future__ import annotations

import contextlib
import sys
import threading
from typing import Any

_KILL_ON_JOB_CLOSE = 0x2000
_BREAKAWAY_OK = 0x0800
_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

_lock = threading.Lock()
# "job": the job handle, kept (never inherited) for the life of the process;
# "joined": whether KaroX itself is in it.
_held: dict[str, Any] = {}


def join_karox_job() -> bool:
    """Before starting a child: put KaroX in its kill-on-exit job (Windows, once).
    True when KaroX is in it, so the children it starts from now on are too."""
    if sys.platform != "win32":
        return False
    try:
        with _lock:
            kernel32 = _kernel32()
            if not _ensure_job(kernel32):
                return False
            if "joined" not in _held:
                current = kernel32.GetCurrentProcess()
                _held["joined"] = bool(kernel32.AssignProcessToJobObject(_held["job"], current))
            return bool(_held["joined"])
    except (OSError, AttributeError, ValueError):
        return False


def bind_to_karox(pid: int) -> bool:
    """After starting a child: make sure process ``pid`` is in the job (Windows).
    A no-op when KaroX joined it before the start. True when the child is in it."""
    if sys.platform != "win32":
        return False
    if _held.get("joined"):
        return True
    try:
        with _lock:
            kernel32 = _kernel32()
            if not _ensure_job(kernel32):
                return False
            access = _PROCESS_SET_QUOTA | _PROCESS_TERMINATE
            process = kernel32.OpenProcess(access, False, pid)
            if not process:
                return False
            try:
                return bool(kernel32.AssignProcessToJobObject(_held["job"], process))
            finally:
                kernel32.CloseHandle(process)
    except (OSError, AttributeError, ValueError):
        return False


def _descendant_pids(kernel32: Any, root_pid: int) -> list[int]:
    """Return ``root_pid`` and any already-running Windows descendants."""
    import ctypes
    from ctypes import wintypes

    class ProcessEntry32W(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    snap = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    invalid_handle = ctypes.c_void_p(-1).value
    if not snap or snap == invalid_handle:
        return [root_pid]
    children: dict[int, list[int]] = {}
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(entry)
        if kernel32.Process32FirstW(snap, ctypes.byref(entry)):
            while True:
                children.setdefault(int(entry.th32ParentProcessID), []).append(
                    int(entry.th32ProcessID)
                )
                if not kernel32.Process32NextW(snap, ctypes.byref(entry)):
                    break
    finally:
        kernel32.CloseHandle(snap)

    seen = {root_pid}
    order = [root_pid]
    stack = [root_pid]
    while stack:
        current = stack.pop()
        for child in children.get(current, ()):
            if child not in seen:
                seen.add(child)
                order.append(child)
                stack.append(child)
    return order


def create_run_job(pid: int) -> int | None:
    """Create a per-run Windows job object containing ``pid`` and its descendants.

    Intentionally omits BREAKAWAY_OK so MSYS2 / Git Bash cannot break child processes
    out of the job, and omits KILL_ON_JOB_CLOSE so callers stop the whole tree explicitly
    with :func:`terminate_run_job` (catching orphaned grandchildren even after the leader
    exits). Returns the raw HANDLE as an int, or None outside Windows / on error.
    """
    if sys.platform != "win32":
        return None
    try:
        kernel32 = _kernel32()
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None
        access = _PROCESS_SET_QUOTA | _PROCESS_TERMINATE
        assigned_any = False
        for target_pid in _descendant_pids(kernel32, pid):
            process = kernel32.OpenProcess(access, False, target_pid)
            if not process:
                continue
            try:
                if kernel32.AssignProcessToJobObject(job, process):
                    assigned_any = True
            finally:
                kernel32.CloseHandle(process)
        if not assigned_any:
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except (OSError, AttributeError, ValueError):
        return None


def terminate_run_job(job: int) -> bool:
    """Terminate all processes in a per-run Windows job object and close its handle."""
    if sys.platform != "win32":
        return False
    try:
        kernel32 = _kernel32()
        try:
            return bool(kernel32.TerminateJobObject(job, 1))
        finally:
            kernel32.CloseHandle(job)
    except (OSError, AttributeError, ValueError):
        return False


def close_run_job(job: int) -> None:
    """Close a per-run Windows job handle without terminating its processes."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(OSError, AttributeError, ValueError):
        _kernel32().CloseHandle(job)


def _kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined,unused-ignore]
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def _ensure_job(kernel32: Any) -> bool:
    import ctypes

    if "job" in _held:
        return True
    job = kernel32.CreateJobObjectW(None, None)  # default security: not inheritable
    if not job:
        return False
    limits = extended_limits()
    limits.BasicLimitInformation.LimitFlags = _KILL_ON_JOB_CLOSE | _BREAKAWAY_OK
    if not kernel32.SetInformationJobObject(
        job, _EXTENDED_LIMIT_INFORMATION, ctypes.byref(limits), ctypes.sizeof(limits)
    ):
        kernel32.CloseHandle(job)
        return False
    _held["job"] = job
    return True


def extended_limits() -> Any:
    """A zeroed JOBOBJECT_EXTENDED_LIMIT_INFORMATION (144 bytes on 64-bit Windows)."""
    import ctypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
        ]

    class IoCounters(ctypes.Structure):
        _fields_ = [
            (name, ctypes.c_uint64)
            for name in (
                "ReadOperationCount",
                "WriteOperationCount",
                "OtherOperationCount",
                "ReadTransferCount",
                "WriteTransferCount",
                "OtherTransferCount",
            )
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", Basic),
            ("IoInfo", IoCounters),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    return Extended()
