"""Tailscale readiness, route ownership, and stable Funnel lifecycle.

KaroX publishes a managed bridge on the device's stable ``*.ts.net`` hostname.
The public route is daemon-managed (``tailscale funnel --bg``), so a restart of
the bridge owner can reuse the same public origin instead of reissuing a new
URL. Every mutation here first proves which individual route KaroX owns and
refuses to touch a foreign one.

``find_tailscale``, ``query_tailscale_status``, ``prepare_tailscale_funnel``,
``start_tailscale_funnel`` and ``stop_tailscale_funnel`` are the public surface;
the helpers keep the ownership checks testable without launching Tailscale.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit


_TS_URL = re.compile(r"https://[A-Za-z0-9.-]+\.ts\.net(?=$|[\s/])")
_ENABLE_URL = re.compile(r'https://login\.tailscale\.com/admin/funnel[^\s"]*')


class TailscaleError(RuntimeError):
    """Tailscale cannot safely publish the requested bridge."""


def find_tailscale(explicit: Optional[str] = None) -> Optional[str]:
    """Use the existing KaroX/TUI lookup locations plus common package paths."""
    candidates: list[Path] = []
    requested = explicit or os.environ.get("KAROX_TAILSCALE_EXE", "").strip()
    if requested:
        candidates.append(Path(requested).expanduser())
    discovered = shutil.which("tailscale") or shutil.which("tailscale.exe")
    if discovered:
        candidates.append(Path(discovered))

    executable = "tailscale.exe" if os.name == "nt" else "tailscale"
    runtime_override = (
        os.environ.get("KAROX_VNEXT_RUNTIME_DIR")
        or os.environ.get("KAROX_RUNTIME_DIR")
        or ""
    ).strip()
    if runtime_override:
        candidates.append(Path(runtime_override).expanduser() / "bin" / executable)

    if os.name == "nt":
        for base in (
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            os.environ.get("LOCALAPPDATA"),
        ):
            if not base:
                continue
            root = Path(base)
            candidates.extend(
                (
                    root / "Tailscale" / "tailscale.exe",
                    root / "Microsoft" / "WinGet" / "Links" / "tailscale.exe",
                    root / "Microsoft" / "WindowsApps" / "tailscale.exe",
                )
            )
    elif sys.platform == "darwin":
        candidates.extend(
            (
                Path("/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
                Path("/opt/homebrew/bin/tailscale"),
                Path("/usr/local/bin/tailscale"),
            )
        )
    else:
        candidates.extend((Path("/usr/bin/tailscale"), Path("/usr/local/bin/tailscale")))

    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip().rstrip(".")


def parse_tailscale_status(
    payload: dict[str, Any], *, executable: str = ""
) -> dict[str, Any]:
    """Normalize status shapes used by old and current Tailscale clients."""
    self_node = _mapping(payload.get("Self"))
    current_tailnet = _mapping(payload.get("CurrentTailnet"))
    tailnet = _mapping(payload.get("Tailnet"))
    backend_state = _text(payload.get("BackendState"))
    auth_url = _text(payload.get("AuthURL"))
    host_name = _text(self_node.get("HostName") or payload.get("HostName"))
    suffix = _text(
        payload.get("MagicDNSSuffix")
        or current_tailnet.get("MagicDNSSuffix")
        or tailnet.get("MagicDNSSuffix")
        or current_tailnet.get("DNSName")
    ).lstrip(".")
    dns_name = _text(self_node.get("DNSName") or payload.get("DNSName"))
    if not dns_name and host_name and suffix:
        dns_name = f"{host_name}.{suffix}".strip(".")
    raw_ips = self_node.get("TailscaleIPs") or payload.get("TailscaleIPs") or []
    tailscale_ips = (
        [
            str(item).strip()
            for item in raw_ips
            if isinstance(item, str) and item.strip()
        ]
        if isinstance(raw_ips, list)
        else []
    )

    state = backend_state.lower()
    if state == "running" and dns_name:
        code = "ready"
        detail = "Tailscale is connected and has a stable tailnet hostname."
    elif state in {"needslogin", "nologin"} or auth_url:
        code = "login_required"
        detail = "Tailscale is installed but login is not complete."
    elif state == "needsmachineauth":
        code = "machine_approval_required"
        detail = "This Tailscale device still needs tailnet administrator approval."
    elif state in {"starting", "stopped", "nostate"}:
        code = "daemon_not_ready"
        detail = f"Tailscale is not ready ({backend_state or 'unknown state'})."
    elif state == "running":
        code = "hostname_unavailable"
        detail = "Tailscale is connected but no stable MagicDNS hostname is available."
    else:
        code = "status_unknown"
        detail = f"Tailscale is not ready ({backend_state or 'unknown state'})."
    return {
        "installed": True,
        "ready": code == "ready",
        "code": code,
        "detail": detail,
        "executable": executable,
        "backend_state": backend_state,
        "dns_name": dns_name,
        "public_url": f"https://{dns_name}" if dns_name else None,
        "auth_url": auth_url or None,
        "host_name": host_name or None,
        "magic_dns_suffix": suffix or None,
        "tailscale_ips": tailscale_ips,
    }


def query_tailscale_status(
    executable: Optional[str] = None,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    resolved = find_tailscale(executable)
    if resolved is None:
        return {
            "installed": False,
            "ready": False,
            "code": "not_installed",
            "detail": "Tailscale is not installed.",
            "executable": None,
            "backend_state": None,
            "dns_name": None,
            "public_url": None,
            "auth_url": None,
        }
    try:
        result = run(
            [resolved, "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {
            "installed": True,
            "ready": False,
            "code": "status_failed",
            "detail": f"Cannot query Tailscale status: {type(exc).__name__}",
            "executable": resolved,
            "backend_state": None,
            "dns_name": None,
            "public_url": None,
            "auth_url": None,
        }
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "tailscale status failed").strip()
        lowered = detail.lower()
        code = (
            "login_required"
            if "logged out" in lowered or "login" in lowered
            else "status_failed"
        )
        return {
            "installed": True,
            "ready": False,
            "code": code,
            "detail": detail,
            "executable": resolved,
            "backend_state": None,
            "dns_name": None,
            "public_url": None,
            "auth_url": None,
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {
            "installed": True,
            "ready": False,
            "code": "invalid_status_json",
            "detail": "tailscale status returned invalid JSON.",
            "executable": resolved,
            "backend_state": None,
            "dns_name": None,
            "public_url": None,
            "auth_url": None,
        }
    if not isinstance(payload, dict):
        return {
            "installed": True,
            "ready": False,
            "code": "invalid_status_json",
            "detail": "tailscale status returned a non-object JSON value.",
            "executable": resolved,
            "backend_state": None,
            "dns_name": None,
            "public_url": None,
            "auth_url": None,
        }
    return parse_tailscale_status(payload, executable=resolved)


def _serve_payload_has_routes(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {"tcp", "web", "allowfunnel", "services"} and item:
                return True
            if _serve_payload_has_routes(item):
                return True
    elif isinstance(value, list):
        return any(_serve_payload_has_routes(item) for item in value)
    return False


def query_funnel_ownership(
    executable: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Report whether configuring Funnel would overwrite an existing route."""
    attempts = (
        [executable, "funnel", "status", "--json"],
        [executable, "funnel", "status"],
        [executable, "serve", "status", "--json"],
    )
    last_detail = ""
    for argv in attempts:
        try:
            result = run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            last_detail = type(exc).__name__
            continue
        output = (result.stdout or result.stderr or "").strip()
        last_detail = output
        if result.returncode != 0:
            lowered = output.lower()
            if "unknown flag" in lowered or "unknown command" in lowered:
                continue
            if "no serve config" in lowered or "not configured" in lowered:
                return {"known": True, "active": False, "detail": output}
            continue
        if argv[-1] == "--json":
            try:
                payload = json.loads(result.stdout or "{}")
            except json.JSONDecodeError:
                continue
            active = _serve_payload_has_routes(payload)
            return {
                "known": True,
                "active": active,
                "detail": "existing Serve/Funnel routes" if active else "no routes",
            }
        lowered = output.lower()
        active = bool(_TS_URL.search(output)) or (
            bool(output)
            and "no serve config" not in lowered
            and "not configured" not in lowered
        )
        return {"known": True, "active": active, "detail": output}
    return {
        "known": False,
        "active": False,
        "detail": last_detail or "cannot determine current Serve/Funnel ownership",
    }


def _normalize_funnel_mount_path(mount_path: str) -> str:
    if not isinstance(mount_path, str) or not mount_path.startswith("/"):
        raise ValueError("Tailscale Funnel mount path must start with /")
    if "?" in mount_path or "#" in mount_path or "//" in mount_path:
        raise ValueError("Tailscale Funnel mount path is invalid")
    normalized = mount_path.rstrip("/") or "/"
    if any(part in {".", ".."} for part in normalized.split("/")):
        raise ValueError("Tailscale Funnel mount path is invalid")
    return normalized


def tailscale_funnel_argv(
    executable: str,
    port: int,
    *,
    https_port: int = 443,
    mount_path: str = "/",
) -> tuple[str, ...]:
    if not 1 <= port <= 65_535:
        raise ValueError("Tailscale Funnel target port must be between 1 and 65535")
    if https_port not in {443, 8443, 10000, 10001}:
        raise ValueError("Tailscale Funnel HTTPS port must be 443, 8443, 10000, or 10001")
    normalized_path = _normalize_funnel_mount_path(mount_path)
    values = [executable, "funnel", "--yes", f"--https={https_port}"]
    if normalized_path != "/":
        values.append(f"--set-path={normalized_path}")
    values.append(f"http://127.0.0.1:{port}")
    return tuple(values)


def classify_funnel_failure(detail: str) -> dict[str, Optional[str]]:
    lowered = detail.lower()
    enable_match = _ENABLE_URL.search(detail)
    if "funnel is not enabled" in lowered or "enable funnel" in lowered:
        code = "funnel_policy_denied"
    elif (
        "not allowed" in lowered
        or "access denied" in lowered
        or "permission denied" in lowered
    ):
        code = "funnel_policy_denied"
    elif "already" in lowered and ("serve" in lowered or "funnel" in lowered):
        code = "route_in_use"
    elif "address already in use" in lowered or "port 443" in lowered:
        code = "https_port_in_use"
    elif "login" in lowered or "logged out" in lowered:
        code = "login_required"
    elif "certificate" in lowered or "https" in lowered and "disabled" in lowered:
        code = "https_unavailable"
    else:
        code = "funnel_failed"
    return {
        "code": code,
        "detail": detail.strip() or "tailscale funnel failed",
        "enable_url": enable_match.group(0) if enable_match else None,
    }


def tailscale_doctor(
    executable: Optional[str] = None,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    status = query_tailscale_status(executable, run=run)
    if not status.get("ready"):
        return {"status": "unavailable", **status}
    assert isinstance(status.get("executable"), str)
    ownership = query_funnel_ownership(status["executable"], run=run)
    if not ownership["known"]:
        overall = "degraded"
    elif ownership["active"]:
        overall = "in_use"
    else:
        overall = "ok"
    return {
        "status": overall,
        **status,
        "route_ownership": ownership,
        "url_stability": "stable_device_hostname",
        "cleanup": "daemon_owned_route",
    }


@dataclass(frozen=True)
class TailscaleLaunchPlan:
    executable: str
    public_url: str
    argv: tuple[str, ...]
    ownership: dict[str, Any]


# Tailscale backend states an unattended ``tailscale up`` can recover without a
# human in a browser: the daemon is still booting (NoState/starting/stopped), or
# it is up but lost its MagicDNS name. ``login_required`` and
# ``machine_approval_required`` need a person (a browser login or an admin's
# approval), so those are reported with their auth URL rather than blocked on.
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


def prepare_tailscale_funnel(
    port: int,
    *,
    https_port: int = 443,
    mount_path: str = "/",
    executable: Optional[str] = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    emit: Optional[Callable[[str], None]] = None,
    restart_service: bool = False,
    elevated_run: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
) -> TailscaleLaunchPlan:
    """Fail before mutation unless this process can own a new Funnel route.

    If Tailscale is installed but only transiently not ready -- the daemon is
    still starting, which is the state a freshly-booted Windows machine reports
    -- one ``tailscale up`` is run and readiness is polled before giving up.
    A login gate is reported with its auth URL instead of blocking on a browser
    the launch path never opens.
    """
    if https_port not in {443, 8443, 10000, 10001}:
        raise TailscaleError(
            "invalid_https_port: Funnel supports 443, 8443, 10000, or 10001"
        )
    try:
        normalized_path = _normalize_funnel_mount_path(mount_path)
    except ValueError as exc:
        raise TailscaleError(f"invalid_mount_path: {exc}") from exc
    status = ensure_tailscale_ready(
        executable,
        run=run,
        emit=emit,
        restart_service=restart_service,
        elevated_run=elevated_run,
    )
    if not status.get("ready"):
        auth_url = status.get("auth_url")
        suffix = f" — sign in at {auth_url}" if auth_url else ""
        raise TailscaleError(f"{status.get('code')}: {status.get('detail')}{suffix}")
    resolved = status.get("executable")
    public_url = status.get("public_url")
    if not isinstance(resolved, str) or not isinstance(public_url, str):
        raise TailscaleError("hostname_unavailable: stable Tailscale URL is unavailable")
    ownership = query_funnel_ownership(resolved, run=run)
    if not ownership.get("known"):
        raise TailscaleError(
            "ownership_unknown: cannot prove that starting Funnel will preserve "
            "existing Tailscale Serve/Funnel routes"
        )
    if ownership.get("active"):
        routes = inventory_tailscale_routes(resolved, run=run)
        if not routes:
            # Something is active but we cannot parse it into individual
            # routes. Fail-safe: refuse rather than risk clearing a foreign
            # route we could not identify.
            raise TailscaleError(
                "route_in_use: an existing Tailscale Serve/Funnel route is "
                "active but its details cannot be parsed; KaroX refuses to "
                "replace it. Run `karox bridge doctor`."
            )
        # Tailscale can host multiple path mounts on one HTTPS listener. Only
        # the route KaroX is about to own -- the requested path -- can conflict
        # with this launcher. Sibling paths must be preserved.
        desired_routes = [
            route
            for route in routes
            if route.public_port == https_port
            and (route.path.rstrip("/") or "/") == normalized_path
        ]
        foreign = [route for route in desired_routes if route.local_port != port]
        stale_owned = [route for route in desired_routes if route.local_port == port]
        if foreign:
            descriptions = ", ".join(
                f"{route.host}:{route.public_port}{route.path}->{route.local_target}"
                for route in foreign
            )
            raise TailscaleError(
                f"route_in_use: HTTPS {https_port} is already owned by another "
                f"Tailscale route ({descriptions})"
            )
        if stale_owned:
            if emit is not None:
                emit(
                    "Clearing the stale KaroX Funnel listener "
                    f"HTTPS {https_port} -> localhost:{port}…"
                )
            cleanup_argv = [resolved, "funnel", f"--https={https_port}"]
            if normalized_path != "/":
                cleanup_argv.append(f"--set-path={normalized_path}")
            cleanup_argv.extend((f"http://127.0.0.1:{port}", "off"))
            try:
                result = run(
                    cleanup_argv,
                    check=False,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise TailscaleError(
                    "stale_route_clear_failed: could not clear stale owned "
                    f"route: {type(exc).__name__}"
                ) from exc
            if result.returncode != 0:
                raise TailscaleError(
                    "stale_route_clear_failed: Tailscale refused the exact route cleanup"
                )
            remaining = inventory_tailscale_routes(resolved, run=run)
            if any(
                route.public_port == https_port
                and (route.path.rstrip("/") or "/") == normalized_path
                and route.local_port == port
                for route in remaining
            ):
                raise TailscaleError(
                    "route_in_use: stale route could not be cleared; "
                    "run `karox bridge doctor`"
                )
        # Routes on the other supported HTTPS ports are intentionally preserved.
    published_url = public_url.rstrip("/")
    if https_port != 443:
        published_url = f"{published_url}:{https_port}"
    return TailscaleLaunchPlan(
        executable=resolved,
        public_url=published_url,
        argv=tailscale_funnel_argv(
            resolved, port, https_port=https_port, mount_path=normalized_path
        ),
        ownership=ownership,
    )


# --------------------------------------------------------------------------- #
# Stable daemon-owned Funnel lifecycle                                         #
# --------------------------------------------------------------------------- #


# Imports are local so a host without the route module never pays for it during
# a plain status query, and tests can monkeypatch route inventory cleanly.
def inventory_tailscale_routes(
    executable: str,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> list[Any]:
    from .tailscale_routes import inventory_tailscale_routes as inventory

    return inventory(executable, run=run)


@dataclass(frozen=True)
class TailscaleFunnel:
    """A daemon-managed Funnel route owned by one bridge profile."""

    executable: str
    public_url: str
    port: int
    https_port: int = 443
    mount_path: str = "/"
    lifecycle: str = "daemon_background"

    def to_dict(self) -> dict[str, Any]:
        return {
            "executable": self.executable,
            "public_url": self.public_url,
            "port": self.port,
            "https_port": self.https_port,
            "mount_path": self.mount_path,
            "lifecycle": self.lifecycle,
        }


def background_funnel_argv(
    executable: str,
    port: int,
    *,
    https_port: int = 443,
    mount_path: str = "/",
) -> list[str]:
    """The daemon configuration command for one exact KaroX route."""
    if not 1 <= port <= 65_535:
        raise ValueError("Tailscale Funnel target port must be between 1 and 65535")
    if https_port not in {443, 8443, 10000, 10001}:
        raise ValueError("Tailscale Funnel HTTPS port must be 443, 8443, 10000, or 10001")
    normalized_path = _normalize_funnel_mount_path(mount_path)
    argv = [
        executable,
        "funnel",
        "--bg",
        "--yes",
        f"--https={https_port}",
    ]
    if normalized_path != "/":
        argv.append(f"--set-path={normalized_path}")
    argv.append(f"http://127.0.0.1:{port}")
    return argv


def _matching_background_funnel_routes(
    executable: str,
    *,
    public_url: str,
    port: int,
    https_port: int = 443,
    mount_path: str = "/",
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> tuple[list[Any], list[Any]]:
    """Split exact-mount routes into owned (same local port) and foreign."""
    host = urlsplit(public_url).hostname or ""
    normalized_path = mount_path.rstrip("/") or "/"
    routes = inventory_tailscale_routes(executable, run=run)
    same_mount = [
        route
        for route in routes
        if route.public_port == https_port
        and (not route.host or route.host == host)
        and (route.path.rstrip("/") or "/") == normalized_path
    ]
    owned = [route for route in same_mount if route.local_port == port]
    foreign = [route for route in same_mount if route.local_port != port]
    return owned, foreign


def _run_kwargs(timeout_seconds: float) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "check": False,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": max(1.0, float(timeout_seconds)),
        "stdin": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return kwargs


def _apply_background_funnel(
    executable: str,
    *,
    public_url: str,
    port: int,
    https_port: int = 443,
    mount_path: str = "/",
    timeout_seconds: float,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    expected_stale_ports: frozenset[int] = frozenset(),
) -> None:
    """Configure the exact owned route and prove the daemon applied it."""
    try:
        result = run(
            background_funnel_argv(
                executable,
                port,
                https_port=https_port,
                mount_path=mount_path,
            ),
            **_run_kwargs(timeout_seconds),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise TailscaleError(
            f"cannot configure background Tailscale Funnel: {type(exc).__name__}"
        ) from exc
    detail = "\n".join(
        value.strip()
        for value in (result.stdout or "", result.stderr or "")
        if value and value.strip()
    )
    if result.returncode != 0:
        failure = classify_funnel_failure(detail)
        raise TailscaleError(f"{failure['code']}: {failure['detail']}")

    # `--bg` stores configuration in tailscaled. Do not trust the CLI exit code
    # alone: verify that the daemon reports the exact host/port route KaroX owns.
    deadline = time.monotonic() + min(max(float(timeout_seconds), 1.0), 15.0)
    while time.monotonic() < deadline:
        owned, others = _matching_background_funnel_routes(
            executable,
            public_url=public_url,
            port=port,
            https_port=https_port,
            mount_path=mount_path,
            run=run,
        )
        # During an owned retarget the daemon may briefly still report the
        # route at the previous KaroX-owned port. That route is stale, not
        # foreign: wait for the daemon to converge instead of refusing.
        stale = [route for route in others if route.local_port in expected_stale_ports]
        foreign = [
            route for route in others if route.local_port not in expected_stale_ports
        ]
        if foreign:
            raise TailscaleError(
                "route_in_use: background Funnel configuration introduced or "
                "encountered a foreign Tailscale route; refusing to claim ownership"
            )
        if owned and not stale:
            return
        time.sleep(0.2)
    raise TailscaleError(
        "funnel_not_applied: background Tailscale Funnel was accepted but the "
        "owned route did not appear in Tailscale status"
    )


def find_owned_funnel(
    port: int,
    *,
    https_port: int = 443,
    mount_path: str = "/",
    executable: Optional[str] = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> Optional[TailscaleFunnel]:
    """Return the exact owned route if it is already live, else ``None``.

    Read-only. A foreign route on the same mount raises, because starting the
    bridge would otherwise have to replace it.
    """
    status = query_tailscale_status(executable, run=run)
    if not status.get("ready"):
        return None
    resolved = status.get("executable")
    public_url = status.get("public_url")
    if not isinstance(resolved, str) or not isinstance(public_url, str):
        return None
    published_url = public_url.rstrip("/")
    if https_port != 443:
        published_url = f"{published_url}:{https_port}"
    owned, foreign = _matching_background_funnel_routes(
        resolved,
        public_url=published_url,
        port=port,
        https_port=https_port,
        mount_path=mount_path,
        run=run,
    )
    if foreign:
        descriptions = ", ".join(
            f"{route.host}:{route.public_port}{route.path}->{route.local_target}"
            for route in foreign
        )
        raise TailscaleError(
            f"route_in_use: HTTPS {https_port} path {mount_path} is owned by a "
            f"foreign Tailscale route ({descriptions})"
        )
    if owned:
        return TailscaleFunnel(
            resolved,
            published_url,
            port,
            https_port=https_port,
            mount_path=mount_path,
        )
    return None


def start_tailscale_funnel(
    port: int,
    *,
    https_port: int = 443,
    mount_path: str = "/",
    executable: Optional[str] = None,
    timeout_seconds: float = 30.0,
    emit: Optional[Callable[[str], None]] = None,
    restart_service: bool = False,
    elevated_run: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> TailscaleFunnel:
    """Create a daemon-managed, persistent Funnel with proven route ownership."""
    try:
        plan = prepare_tailscale_funnel(
            port,
            https_port=https_port,
            mount_path=mount_path,
            executable=executable,
            run=run,
            emit=emit,
            restart_service=restart_service,
            elevated_run=elevated_run,
        )
    except TailscaleError:
        raise
    _apply_background_funnel(
        plan.executable,
        public_url=plan.public_url,
        port=port,
        https_port=https_port,
        mount_path=mount_path,
        timeout_seconds=timeout_seconds,
        run=run,
    )
    if emit is not None:
        emit(f"Background Funnel is ready: {plan.public_url}")
    return TailscaleFunnel(
        plan.executable,
        plan.public_url,
        port,
        https_port=https_port,
        mount_path=mount_path,
    )


def ensure_tailscale_funnel(
    port: int,
    *,
    https_port: int = 443,
    mount_path: str = "/",
    executable: Optional[str] = None,
    timeout_seconds: float = 30.0,
    emit: Optional[Callable[[str], None]] = None,
    restart_service: bool = False,
    elevated_run: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> TailscaleFunnel:
    """Reuse the exact owned route when it is live, otherwise create it.

    Reuse is what keeps the public ``*.ts.net`` origin (and therefore every
    OAuth registration bound to it) stable across bridge restarts.
    """
    existing = find_owned_funnel(
        port,
        https_port=https_port,
        mount_path=mount_path,
        executable=executable,
        run=run,
    )
    if existing is not None:
        if emit is not None:
            emit(f"Reusing the existing Funnel route: {existing.public_url}")
        return existing
    return start_tailscale_funnel(
        port,
        https_port=https_port,
        mount_path=mount_path,
        executable=executable,
        timeout_seconds=timeout_seconds,
        emit=emit,
        restart_service=restart_service,
        elevated_run=elevated_run,
        run=run,
    )


def refresh_tailscale_funnel(
    tunnel: TailscaleFunnel,
    *,
    timeout_seconds: float = 30.0,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> TailscaleFunnel:
    """Reapply only the daemon route already proven to belong to this bridge."""
    owned, foreign = _matching_background_funnel_routes(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=tunnel.port,
        https_port=tunnel.https_port,
        mount_path=tunnel.mount_path,
        run=run,
    )
    if foreign:
        raise TailscaleError(
            "route_in_use: Tailscale route recovery found a foreign route; "
            "refusing to mutate Funnel"
        )
    # If the route disappeared entirely, reapplying is also safe: its hostname
    # and local target are fixed by the already-proven Tailscale identity and
    # bridge port.
    _apply_background_funnel(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=tunnel.port,
        https_port=tunnel.https_port,
        mount_path=tunnel.mount_path,
        timeout_seconds=timeout_seconds,
        run=run,
    )
    return tunnel


def retarget_tailscale_funnel(
    tunnel: TailscaleFunnel,
    new_port: int,
    *,
    timeout_seconds: float = 30.0,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> TailscaleFunnel:
    """Repoint the owned daemon Funnel route at a new local backend port.

    The stable public hostname is the bridge's identity: it never changes here,
    and no bridge credential is read, written, or rotated. A same-listener route
    that matches neither the current nor the requested port belongs to another
    program, so the retarget is refused before any mutation.
    """
    requested_port = int(new_port)
    if requested_port == tunnel.port:
        return refresh_tailscale_funnel(
            tunnel, timeout_seconds=timeout_seconds, run=run
        )
    _owned, others = _matching_background_funnel_routes(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=tunnel.port,
        https_port=tunnel.https_port,
        mount_path=tunnel.mount_path,
        run=run,
    )
    foreign = [route for route in others if route.local_port != requested_port]
    if foreign:
        raise TailscaleError(
            "route_in_use: Tailscale retarget found a route owned by neither the "
            "current nor the requested bridge port; refusing to mutate a foreign route"
        )
    _apply_background_funnel(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=requested_port,
        https_port=tunnel.https_port,
        mount_path=tunnel.mount_path,
        timeout_seconds=timeout_seconds,
        run=run,
        expected_stale_ports=frozenset({tunnel.port}),
    )
    return TailscaleFunnel(
        tunnel.executable,
        tunnel.public_url,
        requested_port,
        https_port=tunnel.https_port,
        mount_path=tunnel.mount_path,
    )


def stop_tailscale_funnel(
    tunnel: TailscaleFunnel,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    """Turn off only the exact background Funnel route still owned by KaroX."""
    owned, foreign = _matching_background_funnel_routes(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=tunnel.port,
        https_port=tunnel.https_port,
        mount_path=tunnel.mount_path,
        run=run,
    )
    if not owned:
        # No KaroX-owned route to stop. Report success only when nothing
        # foreign is occupying the same public endpoint; claiming a clean stop
        # while another program still serves it would hide a live exposure.
        return not foreign
    if foreign:
        # Fail closed during cleanup. Leaving a stale proxy to a closed localhost
        # port is safer than deleting another program's Tailscale route.
        return False
    cleanup_argv = [
        tunnel.executable,
        "funnel",
        f"--https={tunnel.https_port}",
    ]
    if tunnel.mount_path != "/":
        # Tailscale path routes are daemon config entries: this exact form
        # removes only the selected path and preserves the root handler.
        cleanup_argv.extend((f"--set-path={tunnel.mount_path}", "off"))
    else:
        cleanup_argv.extend((f"http://127.0.0.1:{tunnel.port}", "off"))
    try:
        result = run(cleanup_argv, **_run_kwargs(15))
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode != 0:
        return False
    remaining, _foreign = _matching_background_funnel_routes(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=tunnel.port,
        https_port=tunnel.https_port,
        mount_path=tunnel.mount_path,
        run=run,
    )
    return not remaining


def stop_tailscale_route_for_port(
    port: int,
    *,
    https_port: int = 443,
    executable: Optional[str] = None,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> bool:
    """Stop the unique route whose local target is ``port``, or refuse.

    Used only when the owner is already dead and its watchdog record is gone:
    the port is KaroX's own proven identity at that point. Multiple matching
    routes are ambiguous and are left untouched.
    """
    resolved = find_tailscale(executable)
    if resolved is None:
        return False
    routes = [
        route
        for route in inventory_tailscale_routes(resolved, run=run)
        if route.public_port == https_port and route.local_port == port
    ]
    if len(routes) != 1:
        return False
    route = routes[0]
    host = route.host
    public_url = f"https://{host}"
    if https_port != 443:
        public_url = f"{public_url}:{https_port}"
    return stop_tailscale_funnel(
        TailscaleFunnel(resolved, public_url, port, https_port, route.path),
        run=run,
    )


def funnel_status(
    tunnel: TailscaleFunnel,
    *,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    """Report exact-route ownership without mutating anything."""
    owned, foreign = _matching_background_funnel_routes(
        tunnel.executable,
        public_url=tunnel.public_url,
        port=tunnel.port,
        https_port=tunnel.https_port,
        mount_path=tunnel.mount_path,
        run=run,
    )
    return {
        "owned": bool(owned),
        "foreign": [route.to_dict() for route in foreign],
        "public_url": tunnel.public_url,
        "port": tunnel.port,
        "https_port": tunnel.https_port,
        "mount_path": tunnel.mount_path,
    }


# KaroX 5 spellings retained for callers that still use the background-funnel
# names. The implementation is the same daemon-owned route.
TailscaleBackgroundFunnel = TailscaleFunnel
start_tailscale_background_funnel = start_tailscale_funnel
refresh_tailscale_background_funnel = refresh_tailscale_funnel
retarget_tailscale_background_funnel = retarget_tailscale_funnel
stop_tailscale_background_funnel = stop_tailscale_funnel


__all__ = [
    "TailscaleBackgroundFunnel",
    "TailscaleError",
    "TailscaleFunnel",
    "TailscaleLaunchPlan",
    "background_funnel_argv",
    "classify_funnel_failure",
    "ensure_tailscale_funnel",
    "ensure_tailscale_ready",
    "find_owned_funnel",
    "find_tailscale",
    "find_tailscale_gui",
    "funnel_status",
    "launch_tailscale_gui",
    "parse_tailscale_status",
    "prepare_tailscale_funnel",
    "query_funnel_ownership",
    "query_tailscale_status",
    "refresh_tailscale_background_funnel",
    "refresh_tailscale_funnel",
    "restart_tailscale_service",
    "retarget_tailscale_background_funnel",
    "retarget_tailscale_funnel",
    "start_tailscale_background_funnel",
    "start_tailscale_funnel",
    "stop_tailscale_background_funnel",
    "stop_tailscale_funnel",
    "stop_tailscale_route_for_port",
    "tailscale_doctor",
    "tailscale_funnel_argv",
    "tailscale_gui_pids",
    "tailscale_service_fix_hint",
]