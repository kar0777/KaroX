"""Bridge host process/job/port primitives and readiness waits."""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Callable, Optional


_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000

_JOB_OBJECT_LIMIT_BREAKAWAY_OK = 0x0800

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9

_PROCESS_TERMINATE = 0x0001

_PROCESS_SET_QUOTA = 0x0100

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

_STILL_ACTIVE = 259

_PR_SET_PDEATHSIG = 1

# A fresh Python child may spend several seconds importing the runtime before
# uvicorn can bind. This is a bounded startup budget, not a readiness claim:
# acceptance still requires a reachable listener owned by that process tree.
_BRIDGE_STARTUP_TIMEOUT_SECONDS = 45.0

_PUBLIC_READY_TIMEOUT_SECONDS = 30.0

_CHILD_PORT_RELEASE_TIMEOUT_SECONDS = 10.0

class WebBridgeLaunchError(RuntimeError):
    """A managed web bridge could not be prepared or kept alive."""

def _creation_flags() -> int:
    if os.name != "nt":
        return 0
    # The bridge child must not share the launcher's console-control process
    # group. A CTRL_C/CTRL_BREAK used to cancel a hosted request must never be
    # interpreted as bridge shutdown.
    return int(
        getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
    )

def parent_death_hook() -> Optional[Callable[[], None]]:
    """Return a pre-exec hook that has the kernel kill the child with us.

    Windows uses a job object instead (see :func:`_create_child_job`). On Linux
    ``PR_SET_PDEATHSIG`` is the equivalent: a hard kill of the launcher never
    runs the cleanup path, and an orphaned bridge could otherwise keep a public
    Funnel route pointed at an authenticated listener.
    """
    if os.name == "nt" or not sys.platform.startswith("linux"):
        return None
    import ctypes

    try:
        # Resolved in the parent: loading a library between fork and exec in a
        # process that has threads can deadlock, and this one always does.
        libc = ctypes.CDLL("libc.so.6", use_errno=True)
    except OSError:
        return None

    def hook() -> None:
        libc.prctl(_PR_SET_PDEATHSIG, signal.SIGTERM, 0, 0, 0)

    return hook

def _child_options() -> dict[str, Any]:
    """Popen options that put a child in its own reapable process group."""
    options: dict[str, Any] = {
        "creationflags": _creation_flags(),
        # POSIX only: the group leader is the child itself, so the whole tree it
        # spawns can be signalled by one killpg instead of leaking behind it.
        "start_new_session": True,
    }
    hook = parent_death_hook()
    if hook is not None:
        options["preexec_fn"] = hook
    return options

def _pid_of(process: Optional[object]) -> Optional[int]:
    pid = getattr(process, "pid", None)
    return pid if isinstance(pid, int) and pid > 0 else None

def _create_child_job() -> Optional[int]:
    """Open a Windows job whose closure kills everything assigned to it.

    A hard kill of the launcher (``taskkill /F``, a closed console) never runs
    the cleanup path. The job handle dies with this process and Windows then
    terminates the children, which is the only kill-on-parent-death primitive
    available here.
    """
    if os.name != "nt":
        return None
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
            ("PerProcessUserTimeLimit", ctypes.c_longlong),
            ("PerJobUserTimeLimit", ctypes.c_longlong),
            ("LimitFlags", ctypes.c_uint32),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", ctypes.c_uint32),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", ctypes.c_uint32),
            ("SchedulingClass", ctypes.c_uint32),
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
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    limits = _ExtendedLimits()
    # The bridge and tunnel remain kill-on-close children. Breakaway stays
    # allowed for descendants that intentionally own separate user work.
    limits.BasicLimitInformation.LimitFlags = (
        _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | _JOB_OBJECT_LIMIT_BREAKAWAY_OK
    )
    ok = kernel32.SetInformationJobObject(
        wintypes.HANDLE(job),
        _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
        ctypes.byref(limits),
        ctypes.sizeof(limits),
    )
    if not ok:
        kernel32.CloseHandle(wintypes.HANDLE(job))
        return None
    return int(job)

def _adopt_child(job: Optional[int], process: Optional[object]) -> bool:
    """Assign a child to the kill-on-close job; POSIX relies on its group."""
    pid = _pid_of(process)
    if job is None or pid is None or os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(
        _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid
    )
    if not handle:
        return False
    try:
        return bool(
            kernel32.AssignProcessToJobObject(
                wintypes.HANDLE(job), wintypes.HANDLE(handle)
            )
        )
    finally:
        kernel32.CloseHandle(wintypes.HANDLE(handle))

def _close_job(job: Optional[int]) -> None:
    if job is None or os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle(wintypes.HANDLE(job))

def _signal_process_group(process: Optional[object], *, hard: bool) -> None:
    pid = _pid_of(process)
    if pid is None or os.name == "nt":
        return
    try:
        group = os.getpgid(pid)  # type: ignore[attr-defined,unused-ignore]
    except (OSError, AttributeError):
        pass
    else:
        # Signal the group only when the child is its own group leader
        # (`start_new_session` took effect). Otherwise the group is the
        # caller's, and killpg would signal the runtime along with the child.
        if group != pid:
            try:
                os.kill(pid, signal.SIGKILL if hard else signal.SIGTERM)
            except (OSError, AttributeError):
                pass
            return
        try:
            os.killpg(pid, signal.SIGKILL if hard else signal.SIGTERM)
        except (OSError, AttributeError):
            pass
        return
    # getpgid unusable on this platform: fall back to the single process.
    try:
        os.kill(pid, signal.SIGKILL if hard else signal.SIGTERM)
    except (OSError, AttributeError):
        pass

def _close_child_pipes(process: subprocess.Popen[str]) -> None:
    """Release a stopped child's piped handles instead of leaving them to GC."""
    for stream in (
        getattr(process, "stdin", None),
        getattr(process, "stdout", None),
        getattr(process, "stderr", None),
    ):
        if stream is None:
            continue
        try:
            stream.close()
        except (OSError, ValueError):
            pass

def _stop_process(process: Optional[subprocess.Popen[str]]) -> None:
    if process is None:
        return
    if process.poll() is not None:
        _close_child_pipes(process)
        return
    try:
        _signal_process_group(process, hard=False)
        process.terminate()
        process.wait(timeout=5)
    except (OSError, subprocess.SubprocessError):
        try:
            _signal_process_group(process, hard=True)
            process.kill()
            process.wait(timeout=5)
        except (OSError, subprocess.SubprocessError):
            pass
    finally:
        _close_child_pipes(process)

@dataclass(frozen=True)
class MirroredChildOutput:
    """A child's recent output and the thread still draining it."""

    tail: deque[str]
    reader: threading.Thread

    def detail(self) -> str:
        """The last line the child printed, as a suffix for an error message."""
        self.reader.join(timeout=2.0)
        return _tail_detail(self.tail)

def _tail_detail(output_tail: deque[str]) -> str:
    detail = next((line for line in reversed(output_tail) if line), "")
    return f": {detail}" if detail else ""

def _mirror_child_output(
    process: subprocess.Popen[str],
    *,
    name: str,
) -> MirroredChildOutput:
    """Echo a child's merged output to this console and keep its last lines.

    Windows starts these children with ``CREATE_NO_WINDOW``; without redirected
    handles such a child would print into a hidden console. Draining the pipe is
    what turns ``exited with code 2`` back into a reason.
    """
    output_tail: deque[str] = deque(maxlen=30)

    def drain() -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            for raw_line in stream:
                line = raw_line.rstrip()
                output_tail.append(line)
                print(f"[{name}] {line}", flush=True)
        except ValueError:
            pass
        finally:
            try:
                stream.close()
            except OSError:
                pass

    reader = threading.Thread(
        target=drain,
        name=f"karox-{name}-output",
        daemon=True,
    )
    reader.start()
    return MirroredChildOutput(output_tail, reader)

def _port_is_available(port: int) -> bool:
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", port))
    except OSError:
        return False
    return True

def _port_has_listener(port: int, *, host: str = "127.0.0.1") -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=0.2):
            return True
    except OSError:
        return False

def _wait_for_local_port_release(
    port: int,
    *,
    timeout_seconds: float = _CHILD_PORT_RELEASE_TIMEOUT_SECONDS,
) -> bool:
    """Wait until nothing accepts on ``port`` again, bounded."""
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))
    while time.monotonic() < deadline:
        if _port_is_available(port):
            return True
        time.sleep(0.1)
    return _port_is_available(port)

def _windows_parent_pid(pid: int) -> Optional[int]:
    """Return a Windows process parent without requiring optional psutil."""
    if os.name != "nt" or not isinstance(pid, int) or pid <= 0:
        return None
    try:
        completed = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    "$p=Get-CimInstance Win32_Process -Filter "
                    f"'ProcessId = {pid}'; "
                    "if($null -ne $p){[Console]::Write($p.ParentProcessId)}"
                ),
            ],
            capture_output=True,
            text=True,
            encoding="ascii",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=2.0,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            check=False,
        )
        value = completed.stdout.strip()
        return int(value) if value.isdigit() else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None

def _windows_pid_descends_from(holder_pid: int, root_pid: int) -> Optional[bool]:
    """Prove a listener PID is the spawned process or one of its descendants."""
    seen: set[int] = set()
    current = holder_pid
    for _ in range(32):
        if current == root_pid:
            return True
        if current <= 0 or current in seen:
            return False
        seen.add(current)
        parent = _windows_parent_pid(current)
        if parent is None:
            return None
        current = parent
    return False

def _listener_belongs_to_process_tree(port: int, root_pid: int) -> Optional[bool]:
    """Best-effort proof that the local listener belongs to ``root_pid``.

    ``None`` means the platform cannot prove ownership. ``False`` is meaningful:
    a different live process owns the port, so readiness must not accept that
    listener as proof that the child we just spawned is healthy.
    """
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        if os.name != "nt":
            return None
        try:
            completed = subprocess.run(
                ["netstat", "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                encoding="ascii",
                errors="replace",
                stdin=subprocess.DEVNULL,
                timeout=2.0,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        wanted = f":{int(port)}"
        for line in completed.stdout.splitlines():
            fields = line.split()
            if len(fields) < 5 or fields[0].upper() != "TCP":
                continue
            if fields[3].upper() != "LISTENING" or not fields[1].endswith(wanted):
                continue
            try:
                holder_pid = int(fields[4])
            except ValueError:
                return None
            return _windows_pid_descends_from(holder_pid, root_pid)
        return None
    try:
        holder_pid: Optional[int] = None
        for connection in psutil.net_connections(kind="tcp"):
            if connection.laddr and connection.laddr.port == port:
                holder_pid = connection.pid
                break
        if not isinstance(holder_pid, int) or holder_pid <= 0:
            return None
        if holder_pid == root_pid:
            return True
        process = psutil.Process(holder_pid)
        return any(parent.pid == root_pid for parent in process.parents())
    except Exception:
        return None

def local_mcp_route_healthy(
    port: int,
    *,
    path: str = "/mcp",
    timeout_seconds: float = 1.0,
    get: Optional[Callable[..., Any]] = None,
) -> bool:
    """Probe this bridge's own loopback MCP listener.

    KaroX's MCP endpoint intentionally rejects an unauthenticated ``GET /mcp``
    with HTTP 401. Seeing that response proves the listener is alive and reached
    the auth layer without exposing a credential or creating a session.
    """
    endpoint = f"http://127.0.0.1:{int(port)}{path}"
    if get is None:
        import httpx

        get = httpx.get
    try:
        response = get(
            endpoint,
            timeout=float(timeout_seconds),
            follow_redirects=False,
        )
    except Exception:
        return False
    return int(getattr(response, "status_code", 0) or 0) == 401


def _wait_for_public_mcp_route(
    process: subprocess.Popen[str],
    public_url: str,
    *,
    path: str = "/mcp",
    timeout_seconds: float = _PUBLIC_READY_TIMEOUT_SECONDS,
    output: Optional[MirroredChildOutput] = None,
) -> None:
    """Wait until the public MCP ingress reaches the local auth layer."""
    import httpx

    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            suffix = output.detail() if output is not None else ""
            raise WebBridgeLaunchError(f"KaroX bridge exited with code {code}{suffix}")
        remaining = max(0.1, deadline - time.monotonic())
        try:
            response = httpx.get(
                f"{public_url.rstrip('/')}{path}",
                timeout=min(1.5, remaining),
                follow_redirects=False,
            )
            if int(getattr(response, "status_code", 0) or 0) == 401:
                return
        except Exception:
            pass
        time.sleep(0.2)
    suffix = _tail_detail(output.tail) if output is not None else ""
    raise WebBridgeLaunchError(
        "Tailscale Funnel did not make the public MCP route reachable before "
        f"the bridge readiness deadline{suffix}"
    )