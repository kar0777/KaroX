"""Tailscale discovery, status/funnel queries, doctor and failure classification."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
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
