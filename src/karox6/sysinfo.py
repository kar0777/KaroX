"""Tiny, dependency-free process facts (resident memory) for perf gates."""

from __future__ import annotations

import sys


def rss_mb() -> float:
    """Current resident set size of this process in MiB (peak on macOS)."""
    if sys.platform == "win32":
        return _windows_working_set_mb()
    if sys.platform == "linux":
        try:
            with open("/proc/self/status", encoding="ascii") as fh:
                for line in fh:
                    if line.startswith("VmRSS:"):
                        return int(line.split()[1]) / 1024.0
        except OSError:
            pass
    import resource

    peak = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    # macOS reports bytes, Linux kilobytes.
    return peak / (1024.0 * 1024.0) if sys.platform == "darwin" else peak / 1024.0


def _windows_working_set_mb() -> float:
    if sys.platform != "win32":
        return -1.0
    import ctypes
    from ctypes import wintypes

    class _Counters(ctypes.Structure):
        _fields_ = (
            ("cb", wintypes.DWORD),
            ("PageFaultCount", wintypes.DWORD),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        )

    counters = _Counters()
    counters.cb = ctypes.sizeof(_Counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    psapi.GetProcessMemoryInfo.argtypes = (wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD)
    psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
    handle = kernel32.GetCurrentProcess()
    if not psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
        return -1.0
    return float(counters.WorkingSetSize) / (1024.0 * 1024.0)
