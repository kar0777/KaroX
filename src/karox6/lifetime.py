"""On Windows, the processes KaroX starts end with KaroX, however KaroX ends.

Ctrl+C, SIGTERM and a closed MCP stdin run the cleanup that kills child process trees.
But on Windows an MCP client stops its server with TerminateProcess, and closing the
console window ends KaroX the same way: no cleanup runs, and every command, background
job and MCP server it started kept running. A job object with KILL_ON_JOB_CLOSE makes
Windows itself kill them when the job's last handle closes, which is when KaroX exits.
Children of an assigned process join the job too. A program that explicitly asks to
leave (CREATE_BREAKAWAY_FROM_JOB, as libuv does for a detached launch such as an editor)
may; libuv puts its own children in the same kind of job. Best effort: a child started
in the moment before its parent is assigned is left out.

POSIX needs none of this: children run in their own sessions and the cleanup paths
(including SIGTERM) kill those.
"""

from __future__ import annotations

import sys
import threading
from typing import Any

_KILL_ON_JOB_CLOSE = 0x2000
_BREAKAWAY_OK = 0x0800
_EXTENDED_LIMIT_INFORMATION = 9
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_TERMINATE = 0x0001

_lock = threading.Lock()
_held: dict[str, Any] = {}  # "job": the handle, kept (never inherited) for the process's life


def bind_to_karox(pid: int) -> bool:
    """Put process ``pid`` in KaroX's kill-on-exit job (Windows). True when it is in."""
    if sys.platform != "win32":
        return False
    try:
        with _lock:
            return _assign(pid)
    except (OSError, AttributeError, ValueError):
        return False


def _assign(pid: int) -> bool:
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
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    if "job" not in _held:
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
    process = kernel32.OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
    if not process:
        return False
    try:
        return bool(kernel32.AssignProcessToJobObject(_held["job"], process))
    finally:
        kernel32.CloseHandle(process)


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
