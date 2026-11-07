"""Tailscale GUI/service repair and readiness."""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from .bridge_tailscale_status import (
    find_tailscale,
    query_tailscale_status,
)


@dataclass(frozen=True)
class TailscaleLaunchPlan:
    executable: str
    public_url: str
    argv: tuple[str, ...]
    ownership: dict[str, Any]

_TAILSCALE_AUTOFIX_CODES = frozenset(
    {"daemon_not_ready", "hostname_unavailable", "status_unknown", "status_failed"}
)

def _tailscale_auto_up_disabled() -> bool:
    """Auto-bring-up is on unless the operator explicitly turns it off."""
    return os.environ.get("KAROX_TAILSCALE_AUTO_UP", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }

def find_tailscale_gui(executable: Optional[str] = None) -> Optional[str]:
    """Locate the Tailscale GUI app (``tailscale-ipn.exe`` on Windows).

    On Windows the Tailscale service runs ``tailscaled``, but the IPN backend is
    driven by the user-session GUI app: with the GUI not running, the service can
    report ``RUNNING`` while the engine stays in ``NoState`` forever -- the state
    a freshly-booted or just-installed Windows machine reports, and the one
    ``tailscale up`` cannot fix.
    """
    if os.name != "nt":
        return None
    if executable is None:
        executable = find_tailscale()
    if not executable:
        return None
    gui = Path(executable).parent / "tailscale-ipn.exe"
    return str(gui) if gui.is_file() else None

def _same_windows_session(pid: int, mine: int, kernel32: Any) -> bool:
    """True when ``pid`` runs in this process's session; True when undeterminable.

    A machine-wide process list contains every session: on a fast-user-switch or
    RDP host, another user's tray app must not be mistaken for ours.
    """
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover - ctypes ships with CPython
        return True
    try:
        theirs = wintypes.DWORD()
        ours = wintypes.DWORD()
        if not kernel32.ProcessIdToSessionId(pid, ctypes.byref(theirs)):
            return True
        if not kernel32.ProcessIdToSessionId(mine, ctypes.byref(ours)):
            return True
    except (AttributeError, OSError):  # pragma: no cover - no session query
        return True
    return int(theirs.value) == int(ours.value)

def tailscale_gui_pids(gui_name: str = "tailscale-ipn.exe") -> tuple[int, ...]:
    """PIDs of the Tailscale GUI app running in this user session, if any."""
    if os.name != "nt":
        return ()
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover - ctypes ships with CPython
        return ()

    class ProcessEntry32W(ctypes.Structure):
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
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    # Each probe owns its ctypes function wrappers: the local structure type
    # must not replace argtypes on a wrapper shared with concurrent probes.
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32FirstW.restype = wintypes.BOOL
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry32W)]
    kernel32.Process32NextW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.ProcessIdToSessionId.argtypes = [
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL

    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (-1, ctypes.c_void_p(-1).value):
        return ()
    wanted = gui_name.lower()
    mine = os.getpid()
    found: list[int] = []
    try:
        entry = ProcessEntry32W()
        entry.dwSize = ctypes.sizeof(ProcessEntry32W)
        if not kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
            return ()
        while True:
            pid = int(entry.th32ProcessID)
            if entry.szExeFile.lower() == wanted and _same_windows_session(
                pid, mine, kernel32
            ):
                found.append(pid)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    except (OSError, AttributeError):  # pragma: no cover - enumeration refused
        return ()
    finally:
        kernel32.CloseHandle(snapshot)
    return tuple(found)

def launch_tailscale_gui(
    executable: Optional[str] = None,
    *,
    popen: Callable[..., Any] = subprocess.Popen,
    emit: Optional[Callable[[str], None]] = None,
    running_pids: Optional[Callable[[], tuple[int, ...]]] = None,
) -> Optional[str]:
    """Start the Tailscale GUI app in the user session; return its path or None.

    The GUI runs in the user session and drives the backend without a UAC prompt,
    so it is tried before the service restart. An already-running tray app is
    left alone.
    """
    gui_app = find_tailscale_gui(executable)
    if gui_app is None:
        return None
    probe = tailscale_gui_pids if running_pids is None else running_pids
    if probe():
        return gui_app
    # Disconnect standard streams from KaroX and do not request a detached
    # console. CREATE_NO_WINDOW applies to console executables, not GUI windows.
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if emit is not None:
        emit("Starting the Tailscale app so its backend can come online…")
    try:
        popen(
            [gui_app],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=flags,
            close_fds=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return gui_app

def _tailscale_gui_launch_disabled() -> bool:
    return os.environ.get("KAROX_TAILSCALE_GUI_LAUNCH", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }

def _bring_tailscale_up(
    executable: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout_seconds: float = 20.0,
) -> Optional[subprocess.CompletedProcess[str]]:
    """Run ``tailscale up`` once; never raise.

    With an existing node key this returns within a couple of seconds as the
    daemon reconnects. A logged-out node would block on a browser login, so the
    call is bounded by ``timeout_seconds``.
    """
    try:
        return run(
            [executable, "up"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return None

def _tailscale_service_restart_disabled() -> bool:
    """Service restart is on unless the operator explicitly turns it off."""
    return os.environ.get("KAROX_TAILSCALE_SERVICE_RESTART", "1").strip().lower() in {
        "0",
        "false",
        "no",
        "off",
    }

def _is_windows_admin() -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())  # type: ignore[attr-defined]
    except OSError:
        return False

def _elevated_windows_service_restart_argv() -> list[str]:
    """One-UAC restart of the Tailscale service from a non-elevated process."""
    inner = (
        "Stop-Service Tailscale -Force -ErrorAction SilentlyContinue; "
        "Start-Service Tailscale"
    )
    argument_list = "@('-NoProfile','-Command','" + inner + "')"
    outer = (
        "Start-Process -Verb RunAs -Wait -FilePath powershell "
        "-ArgumentList " + argument_list
    )
    return ["powershell", "-NoProfile", "-Command", outer]

def tailscale_service_fix_hint() -> str:
    """The one command a user runs when auto-restart is off or refused UAC."""
    if os.name == "nt":
        return (
            "Restart the Tailscale service from an elevated terminal: "
            "`Stop-Service Tailscale -Force; Start-Service Tailscale`, or rerun "
            "the command as administrator."
        )
    return "Restart the Tailscale daemon (e.g. `systemctl restart tailscaled`)."

def restart_tailscale_service(
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    elevated_run: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
    emit: Optional[Callable[[str], None]] = None,
    timeout_seconds: float = 45.0,
) -> bool:
    """Restart the Tailscale service/daemon; return True if it claims success."""
    if os.name == "nt":
        if _is_windows_admin():
            if emit is not None:
                emit("Restarting the Tailscale service (already elevated)…")
            stop = _run_sc("stop", "Tailscale", run=run, timeout_seconds=timeout_seconds)
            start = _run_sc("start", "Tailscale", run=run, timeout_seconds=timeout_seconds)
            return bool(stop and start)
        if emit is not None:
            emit(
                "Tailscale is stuck; requesting permission to restart its "
                "service (approve the Windows prompt)…"
            )
        runner = elevated_run or run
        try:
            result = runner(
                _elevated_windows_service_restart_argv(),
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                stdin=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            if emit is not None:
                emit(f"Service restart failed: {type(exc).__name__}")
            return False
        if result.returncode != 0 and emit is not None:
            detail = (result.stderr or result.stdout or "").strip()
            emit(
                "Service restart was not confirmed"
                + (f": {detail}" if detail else "")
            )
        return result.returncode == 0
    # POSIX: try the systemd unit; if it needs root, surface the command rather
    # than prompting for a password KaroX cannot supply unattended.
    if emit is not None:
        emit("Restarting the Tailscale daemon (systemctl)…")
    try:
        result = run(
            ["systemctl", "restart", "tailscaled"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
        )
    except (OSError, subprocess.SubprocessError):
        if emit is not None:
            emit(tailscale_service_fix_hint())
        return False
    if result.returncode != 0 and emit is not None:
        emit(tailscale_service_fix_hint())
    return result.returncode == 0

def _run_sc(
    action: str,
    service: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    timeout_seconds: float = 45.0,
) -> bool:
    try:
        result = run(
            ["sc", action, service],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_seconds,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0

def ensure_tailscale_ready(
    executable: Optional[str] = None,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    up_timeout_seconds: float = 20.0,
    poll_attempts: int = 8,
    poll_interval_seconds: float = 1.5,
    emit: Optional[Callable[[str], None]] = None,
    restart_service: bool = False,
    elevated_run: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
    popen: Callable[..., Any] = subprocess.Popen,
) -> dict[str, Any]:
    """Bring Tailscale online and wait for it to be ready, when it can be.

    A transient ``starting``/``NoState`` daemon -- the common case right after the
    service starts, which is exactly when a user reaches for ``bridge connect``
    -- is recovered by one ``tailscale up`` plus a short poll. A node that needs
    a browser login is left alone and returned with its auth URL, so a
    non-interactive launch fails fast with something to click instead of hanging
    on a browser it never opens.
    """
    status = query_tailscale_status(executable, run=run)
    if status.get("ready"):
        return status
    resolved = status.get("executable")
    code = status.get("code")
    if (
        not isinstance(resolved, str)
        or _tailscale_auto_up_disabled()
        or code not in _TAILSCALE_AUTOFIX_CODES
    ):
        # ``login_required`` and ``machine_approval_required`` already carry the
        # auth URL in ``status``; the caller surfaces it instead of blocking.
        return status

    gui_launched = False
    # On Windows the user-session IPN app is part of making the backend useful.
    # If it was exited, `tailscale status` may fail outright instead of returning
    # NoState. Launch it *before* `tailscale up`.
    if os.name == "nt" and not _tailscale_gui_launch_disabled():
        gui_launched = bool(launch_tailscale_gui(resolved, popen=popen, emit=emit))
        if gui_launched:
            time.sleep(max(0.25, poll_interval_seconds))
            status = _poll_tailscale_ready(
                resolved,
                run=run,
                attempts=poll_attempts,
                interval=poll_interval_seconds,
            )
            if status.get("ready"):
                if emit is not None:
                    emit(f"Tailscale is ready: {status.get('public_url')}")
                return status
            code = status.get("code")
            if code not in _TAILSCALE_AUTOFIX_CODES:
                return status

    if emit is not None:
        emit("Tailscale is starting; bringing it online with `tailscale up`…")
    _bring_tailscale_up(resolved, run=run, timeout_seconds=up_timeout_seconds)
    status = _poll_tailscale_ready(
        resolved,
        run=run,
        emit=emit,
        attempts=poll_attempts,
        interval=poll_interval_seconds,
    )
    if status.get("ready"):
        return status
    # On Windows the IPN backend is driven by the GUI app, not the CLI: a service
    # stuck in NoState after `up` is most often a GUI that never started, so the
    # GUI is launched (no UAC) and readiness re-polled before reaching for the
    # heavier service restart.
    if (
        os.name == "nt"
        and not gui_launched
        and not _tailscale_gui_launch_disabled()
        and status.get("code") in _TAILSCALE_AUTOFIX_CODES
    ):
        launched = launch_tailscale_gui(resolved, popen=popen, emit=emit)
        if launched:
            time.sleep(max(1.0, poll_interval_seconds))
            status = _poll_tailscale_ready(
                resolved,
                run=run,
                attempts=poll_attempts,
                interval=poll_interval_seconds,
            )
            if status.get("ready"):
                if emit is not None:
                    emit(f"Tailscale is ready: {status.get('public_url')}")
                return status
    if (
        restart_service
        and not _tailscale_service_restart_disabled()
        and status.get("code") in _TAILSCALE_AUTOFIX_CODES
    ):
        restarted = restart_tailscale_service(
            run=run, elevated_run=elevated_run, emit=emit
        )
        if restarted:
            time.sleep(poll_interval_seconds)
            status = _poll_tailscale_ready(
                resolved,
                run=run,
                emit=emit,
                attempts=poll_attempts,
                interval=poll_interval_seconds,
            )
            if status.get("ready"):
                return status
            if emit is not None:
                emit(tailscale_service_fix_hint())
        elif emit is not None:
            emit(tailscale_service_fix_hint())
    return status

def _poll_tailscale_ready(
    resolved: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    emit: Optional[Callable[[str], None]] = None,
    attempts: int = 8,
    interval: float = 1.5,
) -> dict[str, Any]:
    if emit is not None:
        emit("Waiting for the Tailscale daemon to come online…")
    status: dict[str, Any] = {}
    for _attempt in range(max(1, attempts)):
        status = query_tailscale_status(resolved, run=run)
        if status.get("ready"):
            if emit is not None:
                emit(f"Tailscale is ready: {status.get('public_url')}")
            return status
        # Stop polling once the state leaves the set ``up`` can fix: it has moved
        # to a login/approval gate or to an unrecoverable failure.
        if status.get("code") not in _TAILSCALE_AUTOFIX_CODES:
            break
        time.sleep(interval)
    return status
