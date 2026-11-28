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


def child_priority() -> int:
    """Windows creation flags for the commands KaroX runs: below-normal priority, so a
    build, a test run or a browser an agent starts never takes the CPU from the user's
    own programs (a game, the IDE); children inherit it. KAROX_COMMAND_PRIORITY=normal
    keeps the normal priority. 0 elsewhere."""
    import os
    import subprocess

    if sys.platform != "win32":
        return 0
    if os.environ.get("KAROX_COMMAND_PRIORITY", "").strip().lower() == "normal":
        # explicit: 0 would inherit KaroX's own class, below normal when it runs so
        return int(getattr(subprocess, "NORMAL_PRIORITY_CLASS", 0))
    return int(getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))


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
