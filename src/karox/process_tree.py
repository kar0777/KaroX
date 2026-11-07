"""Process-tree primitives: Windows job objects, group kill, capture and decode."""

from __future__ import annotations

import codecs
import locale
import os
import signal
import subprocess
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Optional


_WINDOWS_JOB_LIMIT_KILL_ON_CLOSE = 0x2000

_WINDOWS_JOB_LIMIT_BREAKAWAY_OK = 0x0800

_WINDOWS_JOB_EXTENDED_LIMIT_INFORMATION = 9

_WINDOWS_PROCESS_TERMINATE = 0x0001

_WINDOWS_PROCESS_SET_QUOTA = 0x0100

@lru_cache(maxsize=1)
def _windows_job_api() -> Optional[tuple[Any, Any]]:
    """Win32 Job Object entry points, or None where they are unavailable."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_ulonglong),
                ("WriteOperationCount", ctypes.c_ulonglong),
                ("OtherOperationCount", ctypes.c_ulonglong),
                ("ReadTransferCount", ctypes.c_ulonglong),
                ("WriteTransferCount", ctypes.c_ulonglong),
                ("OtherTransferCount", ctypes.c_ulonglong),
            ]

        class _BasicLimits(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _ExtendedLimits(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimits),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            wintypes.LPVOID,
            wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
        ]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        return kernel32, _ExtendedLimits
    except Exception:
        return None

def _new_process_group_kwargs() -> Dict[str, Any]:
    if os.name == "nt":
        # A guarded command must not share the bridge console-control lifecycle.
        # CREATE_NEW_PROCESS_GROUP prevents CTRL_C/CTRL_BREAK intended for an
        # expired hosted request from becoming a bridge KeyboardInterrupt;
        # CREATE_NO_WINDOW also prevents a hidden verification job from owning a
        # console. ProcessTree remains responsible for terminating descendants.
        return {
            "creationflags": int(
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
            )
        }
    return {"start_new_session": True}

def _kill_posix_process_group(pid: int) -> None:
    try:
        group = os.getpgid(pid)
    except OSError:
        return
    # If the child is not its own group leader then start_new_session did not
    # take effect and the group is ours, so signalling it would kill KaroX and
    # everything else sharing the terminal.
    if group != pid:
        return
    try:
        os.killpg(group, signal.SIGTERM)
    except OSError:
        return
    time.sleep(0.2)
    try:
        os.killpg(group, signal.SIGKILL)
    except OSError:
        pass

def _kill_windows_process_tree(pid: int) -> None:
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10.0,
            check=False,
            **_new_process_group_kwargs(),
        )
    except (OSError, subprocess.SubprocessError):
        pass

class ProcessTree:
    """A spawned child together with the descendants it goes on to create.

    Waiting on a timeout and killing only the direct child leaves the workers of
    a parallel test run alive, still holding the captured output handle and any
    file locks they took, so the next check inherits a broken workspace.  This
    keeps the whole tree reachable so it can be swept in one go.  It bounds
    cleanup, nothing else: the child still runs with the repository as its
    working directory and with whatever access the operating system gives it.
    """

    KILL_GRACE_SECONDS = 5.0

    def __init__(self, process: "subprocess.Popen[bytes]") -> None:
        self._process = process
        self._job: Optional[Any] = None
        api = _windows_job_api()
        if api is None:
            return
        import ctypes

        kernel32, extended_limits = api
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return
        limits = extended_limits()
        limits.BasicLimitInformation.LimitFlags = (
            _WINDOWS_JOB_LIMIT_KILL_ON_CLOSE | _WINDOWS_JOB_LIMIT_BREAKAWAY_OK
        )
        # Assignment can only happen once the process exists, so a child that
        # spawns before the handle is opened is not covered. That window is a few
        # microseconds against an interpreter start-up of tens of milliseconds.
        handle = kernel32.OpenProcess(
            _WINDOWS_PROCESS_SET_QUOTA | _WINDOWS_PROCESS_TERMINATE,
            False,
            process.pid,
        )
        assigned = False
        if handle:
            assigned = bool(
                kernel32.SetInformationJobObject(
                    job,
                    _WINDOWS_JOB_EXTENDED_LIMIT_INFORMATION,
                    ctypes.byref(limits),
                    ctypes.sizeof(limits),
                )
            ) and bool(kernel32.AssignProcessToJobObject(job, handle))
            kernel32.CloseHandle(handle)
        if assigned:
            self._job = job
        else:
            kernel32.CloseHandle(job)

    def terminate(self) -> None:
        tree_terminated = False
        if self._job is not None:
            api = _windows_job_api()
            if api is not None:
                try:
                    tree_terminated = bool(api[0].TerminateJobObject(self._job, 1))
                except OSError:
                    pass
        # A handle alone does not prove that job termination worked. If the
        # API disappears or fails, sweep the owned child tree without sending
        # console-control signals that could interrupt the bridge itself.
        if not tree_terminated:
            if os.name == "nt":
                _kill_windows_process_tree(self._process.pid)
            else:
                _kill_posix_process_group(self._process.pid)
        try:
            self._process.kill()
        except OSError:
            pass
        try:
            self._process.wait(timeout=self.KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            pass

    def close(self) -> None:
        if self._job is None:
            return
        api = _windows_job_api()
        if api is not None:
            # Windows only. The job carries KILL_ON_JOB_CLOSE, so releasing the
            # last handle also collects anything the finished check left
            # running. POSIX has no equivalent here: a check that succeeds and
            # deliberately leaves a helper behind keeps it there, and only the
            # timeout and interrupt paths sweep the group.
            api[0].CloseHandle(self._job)
        self._job = None

@dataclass(frozen=True)
class CapturedStream:
    text: str
    sha256: str
    truncated: bool
    total_bytes: int
    elided_bytes: int
    #: Unelided text for a diagnostic parser. Never serialized into a result:
    #: callers that ask for it pop it and attach only the derived map.
    full_text: str = ""

@lru_cache(maxsize=1)
def _legacy_output_encodings() -> tuple[str, ...]:
    """Code pages a guarded child may have written when its output is not UTF-8.

    ``security.child_process_environment`` forces ``PYTHONIOENCODING`` so a
    Python child always answers in UTF-8, which covers the test runners and
    linters most verification commands use. It cannot cover a child KaroX does
    not control the startup of: a Windows C# or C++ compiler, ``javac``, or any
    tool that writes through the console API answers in the host's OEM code page
    -- cp866 on a Russian-locale install -- and no environment variable changes
    that.

    Both candidates are single-byte on Windows, so either decodes any byte
    sequence without raising. Choosing the wrong one mangles the text; deleting
    the bytes, which is what ``errors="ignore"`` did at this call site, removes
    it. A mangled diagnostic still shows the agent the path, the line number and
    the ASCII keywords it needs to act on. A deleted one shows an empty error and
    invites the agent to report success.
    """
    candidates: list[str] = []
    if os.name == "nt":
        try:
            import ctypes

            oem = int(ctypes.windll.kernel32.GetOEMCP())  # type: ignore[attr-defined]
        except (AttributeError, OSError, ValueError):
            oem = 0
        if oem:
            candidates.append(f"cp{oem}")
    try:
        preferred = locale.getpreferredencoding(False)
    except (LookupError, ValueError):
        preferred = ""
    if preferred:
        candidates.append(preferred)
    return tuple(
        dict.fromkeys(
            name
            for name in candidates
            if name.replace("-", "").replace("_", "").lower() != "utf8"
        )
    )

def _decode_captured_bytes(raw: bytes, *, mid_stream_start: bool = False) -> tuple[str, int]:
    """Decode one end of a captured stream, reporting the source bytes it accounts for.

    The byte count is returned rather than recomputed by the caller because it is
    only equal to ``len(text.encode("utf-8"))`` while the decode really was
    UTF-8. Under the legacy fallback below, re-encoding the result produces a
    different length, and the elided-byte arithmetic that used to assume
    otherwise would report a negative number beside a sha256 of the whole stream.

    ``mid_stream_start`` marks a chunk taken from the middle of the stream, whose
    first bytes may be the continuation of a character whose lead byte was
    elided. Those bytes are unrecoverable on their own, so they are attributed to
    the elided middle instead of being decoded into a replacement character that
    was never in the output.
    """
    if not raw:
        return "", 0
    start = 0
    if mid_stream_start:
        while start < len(raw) and 0x80 <= raw[start] < 0xC0:
            start += 1
    body = raw[start:]
    if not body:
        return "", 0
    decoder = codecs.getincrementaldecoder("utf-8")()
    try:
        # ``final=False``: a chunk cut at a byte budget routinely ends inside a
        # multi-byte character, and that is a truncation rather than a decode
        # error. The decoder buffers those bytes instead of raising, and they are
        # excluded from the count below because they produced no text.
        text = decoder.decode(body, False)
    except UnicodeDecodeError:
        pass
    else:
        return text, len(text.encode("utf-8"))
    for encoding in _legacy_output_encodings():
        try:
            return body.decode(encoding), len(body)
        except (LookupError, UnicodeDecodeError):
            continue
    # Nothing decoded cleanly. ``replace`` keeps the readable remainder and marks
    # the rest, which is the whole point: never return less than the child wrote.
    return body.decode("utf-8", errors="replace"), len(body)
