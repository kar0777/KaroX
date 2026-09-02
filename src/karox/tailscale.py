"""Tailscale readiness and ownership-safe Funnel helpers.

The managed bridge uses a foreground ``tailscale funnel`` child.  It refuses to
replace an existing Serve/Funnel configuration and therefore never needs a global
``reset`` during cleanup: stopping the child only stops the route that child owns.
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
    tailscale_ips = [
        str(item).strip()
        for item in raw_ips
        if isinstance(item, str) and item.strip()
    ] if isinstance(raw_ips, list) else []

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
        code = "login_required" if "logged out" in lowered or "login" in lowered else "status_failed"
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
    if https_port not in {443, 8443, 10000}:
        raise ValueError("Tailscale Funnel HTTPS port must be 443, 8443, or 10000")
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
    elif "not allowed" in lowered or "access denied" in lowered or "permission denied" in lowered:
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
        "cleanup": "foreground_child_only",
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
    ``tailscale up`` cannot fix. The GUI installs next to the CLI in the standard
    ``C:\\Program Files\\Tailscale`` layout, so it is looked up there.
    """
    if os.name != "nt":
        return None
    if executable is None:
        executable = find_tailscale()
    if not executable:
        return None
    gui = Path(executable).parent / "tailscale-ipn.exe"
    return str(gui) if gui.is_file() else None


def launch_tailscale_gui(
    executable: Optional[str] = None,
    *,
    popen: Callable[..., Any] = subprocess.Popen,
    emit: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """Start the Tailscale GUI app in the user session; return its path or None.

    The GUI runs in the user session and drives the backend without a UAC prompt,
    so it is tried before the service restart. ``popen`` is injectable so the
    launch can be verified without actually starting a tray app.
    """
    gui_app = find_tailscale_gui(executable)
    if gui_app is None:
        return None
    flags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) | int(
        getattr(subprocess, "DETACHED_PROCESS", 0)
    )
    if emit is not None:
        emit("Starting the Tailscale app so its backend can come online…")
    try:
        popen([gui_app], creationflags=flags, close_fds=True)
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
    call is bounded by ``timeout_seconds``; the auth URL is read back from the
    daemon status afterwards rather than parsed out of this potentially blocking
    call.
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
    """One-UAC restart of the Tailscale service from a non-elevated process.

    A non-elevated outer PowerShell launches a single elevated PowerShell (the
    one UAC prompt) that stops then starts the ``Tailscale`` service and exits;
    ``-Wait`` blocks until it has, so readiness can be re-polled right after.
    The inner command is single-quote-safe by construction (no embedded quotes).
    """
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
    """Restart the Tailscale service/daemon; return True if it claims success.

    On Windows an already-elevated process restarts ``Tailscale`` directly; a
    non-elevated one raises a single UAC prompt via an elevated PowerShell child
    so the user -- not KaroX -- consents to the privilege change. ``elevated_run``
    is injectable so the UAC command can be verified without actually prompting.
    POSIX is best-effort: ``systemctl restart tailscaled`` is tried, and a
    guidance line is emitted when it cannot be run unattended.
    """
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
    service starts, which is exactly when a user reaches for ``bridge connect`` --
    is recovered by one ``tailscale up`` plus a short poll. A node that needs a
    browser login is left alone and returned with its auth URL, so a
    non-interactive launch fails fast with something to click instead of hanging
    on a browser it never opens.

    When ``restart_service`` is set and the daemon is still stuck after ``up``,
    the Tailscale service is restarted (one UAC prompt on Windows) and readiness
    is re-polled: ``tailscale up`` returns success on a node that already has a
    key, so a daemon whose engine never came up -- the state a freshly-booted
    Windows machine reports -- is the case ``up`` alone cannot fix.

    ``tailscale_doctor`` does not call this: diagnostics never mutate Tailscale,
    only the launch path does.
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
    # NoState. Launch it *before* `tailscale up` so Start/Restart from the TUI
    # repairs the common "Tailscale app is closed" state without a manual step.
    if os.name == "nt" and not _tailscale_gui_launch_disabled():
        gui_launched = bool(
            launch_tailscale_gui(resolved, popen=popen, emit=emit)
        )
        if gui_launched:
            time.sleep(max(0.25, poll_interval_seconds))
            status = _poll_tailscale_ready(
                resolved, run=run, attempts=poll_attempts,
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
        resolved, run=run, emit=emit, attempts=poll_attempts,
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
            # Give the GUI a moment to drive the backend before re-polling.
            time.sleep(max(1.0, poll_interval_seconds))
            status = _poll_tailscale_ready(
                resolved, run=run, attempts=poll_attempts,
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
            # Give the freshly-started daemon a moment before polling it again.
            time.sleep(poll_interval_seconds)
            status = _poll_tailscale_ready(
                resolved, run=run, emit=emit, attempts=poll_attempts,
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
    for attempt in range(max(1, attempts)):
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
    still starting, which is the state a freshly-booted Windows machine reports --
    one ``tailscale up`` is run and readiness is polled before giving up, so
    launching a bridge does not die on a daemon that finishes booting two seconds
    later. With ``restart_service`` the Tailscale service is restarted (one UAC
    prompt on Windows) when ``up`` alone leaves the engine stuck. A login gate is
    reported with its auth URL instead of blocking on a browser the launch path
    never opens.
    """
    if https_port not in {443, 8443, 10000}:
        raise TailscaleError("invalid_https_port: Funnel supports 443, 8443, or 10000")
    try:
        normalized_path = _normalize_funnel_mount_path(mount_path)
    except ValueError as exc:
        raise TailscaleError(f"invalid_mount_path: {exc}") from exc
    status = ensure_tailscale_ready(
        executable, run=run, emit=emit,
        restart_service=restart_service, elevated_run=elevated_run,
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
        # Phase 0.4: not every active route is foreign. A stale route left by a
        # dead KaroX bridge (same port, no live process) is safe to clear, while
        # a foreign route must never be touched. Enumerate the individual routes
        # and decide.
        from .tailscale_routes import inventory_tailscale_routes

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
        # the route KaroX is about to own -- the root path -- can conflict with
        # this launcher. Sibling paths (for example /karox-notion) must be
        # preserved and must not prevent ChatGPT's root route from starting.
        desired_routes = [
            r
            for r in routes
            if r.public_port == https_port
            and (r.path.rstrip("/") or "/") == normalized_path
        ]
        foreign = [r for r in desired_routes if r.local_port != port]
        stale_owned = [r for r in desired_routes if r.local_port == port]
        if foreign:
            # There is at least one route that does not point at our bridge
            # port. Refuse to touch it.
            descriptions = ", ".join(
                f"{r.host}:{r.public_port}{r.path}->{r.local_target}" for r in foreign
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
            try:
                result = run(
                    [
                        resolved,
                        "funnel",
                        f"--https={https_port}",
                        f"http://127.0.0.1:{port}",
                        "off",
                    ],
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
                    f"stale_route_clear_failed: could not clear stale owned "
                    f"route: {type(exc).__name__}"
                ) from exc
            if result.returncode != 0:
                raise TailscaleError(
                    "stale_route_clear_failed: Tailscale refused the exact route cleanup"
                )
            remaining = inventory_tailscale_routes(resolved, run=run)
            if any(
                route.public_port == https_port and route.local_port == port
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
