#!/usr/bin/env python3
"""Wait for Tailscale login and resolve the stable device DNS name.

The Tailscale GUI/CLI can report Running before Self.DNSName is populated.
This helper polls status, understands transitional states, and derives the
stable *.ts.net hostname from HostName + MagicDNSSuffix when necessary.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass


def find_tailscale() -> str | None:
    override = os.environ.get("KAROX_TAILSCALE_EXE", "").strip()
    candidates: list[str] = [override] if override else []
    found = shutil.which("tailscale") or shutil.which("tailscale.exe")
    if found:
        candidates.append(found)
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
            if base:
                candidates.extend(
                    [
                        str(Path(base) / "Tailscale" / "tailscale.exe"),
                        str(Path(base) / "Microsoft" / "WinGet" / "Links" / "tailscale.exe"),
                        str(Path(base) / "Microsoft" / "WindowsApps" / "tailscale.exe"),
                    ]
                )
    elif sys.platform == "darwin":
        candidates.extend(
            [
                "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
                "/opt/homebrew/bin/tailscale",
                "/usr/local/bin/tailscale",
            ]
        )
    else:
        candidates.extend(["/usr/bin/tailscale", "/usr/local/bin/tailscale"])

    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str:
    return str(value or "").strip().rstrip(".")


def parse_status(payload: dict[str, Any], executable: str = "") -> dict[str, Any]:
    self_node = _dict(payload.get("Self"))
    current_tailnet = _dict(payload.get("CurrentTailnet"))
    tailnet = _dict(payload.get("Tailnet"))

    backend_state = _text(payload.get("BackendState"))
    auth_url = _text(payload.get("AuthURL"))
    host_name = _text(self_node.get("HostName") or payload.get("HostName"))
    magic_suffix = _text(
        payload.get("MagicDNSSuffix")
        or current_tailnet.get("MagicDNSSuffix")
        or tailnet.get("MagicDNSSuffix")
        or current_tailnet.get("DNSName")
    ).lstrip(".")
    dns_name = _text(self_node.get("DNSName") or payload.get("DNSName"))
    if not dns_name and host_name and magic_suffix:
        dns_name = f"{host_name}.{magic_suffix}".strip(".")

    ips = self_node.get("TailscaleIPs") or payload.get("TailscaleIPs") or []
    if not isinstance(ips, list):
        ips = [str(ips)] if ips else []

    running = backend_state.lower() == "running"
    ready = running and bool(dns_name)
    if ready:
        error = ""
    elif backend_state.lower() in {"needslogin", "nologin"}:
        error = "Tailscale login is not complete. Finish sign-in in the browser or Tailscale app."
    elif backend_state.lower() in {"starting", "stopped", "needsmachineauth"}:
        error = f"Tailscale is still starting ({backend_state or 'unknown state'})."
    elif running and not dns_name:
        error = "Tailscale is connected, but the stable tailnet DNS name is not available yet."
    else:
        error = f"Tailscale is not ready ({backend_state or 'unknown state'})."

    return {
        "installed": True,
        "ready": ready,
        "executable": executable,
        "backendState": backend_state,
        "dnsName": dns_name,
        "baseUrl": f"https://{dns_name}" if dns_name else "",
        "authUrl": auth_url,
        "hostName": host_name,
        "magicDnsSuffix": magic_suffix,
        "tailscaleIPs": [str(item) for item in ips],
        "error": error,
    }


def query_status(executable: str | None = None) -> dict[str, Any]:
    executable = executable or find_tailscale()
    if not executable:
        return {
            "installed": False,
            "ready": False,
            "executable": "",
            "backendState": "",
            "dnsName": "",
            "baseUrl": "",
            "authUrl": "",
            "hostName": "",
            "magicDnsSuffix": "",
            "tailscaleIPs": [],
            "error": "Tailscale is not installed.",
        }
    try:
        result = subprocess.run(
            [executable, "status", "--json"],
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "installed": True,
            "ready": False,
            "executable": executable,
            "backendState": "",
            "dnsName": "",
            "baseUrl": "",
            "authUrl": "",
            "hostName": "",
            "magicDnsSuffix": "",
            "tailscaleIPs": [],
            "error": str(exc),
        }
    if result.returncode != 0:
        return {
            "installed": True,
            "ready": False,
            "executable": executable,
            "backendState": "",
            "dnsName": "",
            "baseUrl": "",
            "authUrl": "",
            "hostName": "",
            "magicDnsSuffix": "",
            "tailscaleIPs": [],
            "error": (result.stderr or result.stdout or "tailscale status failed").strip(),
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {
            "installed": True,
            "ready": False,
            "executable": executable,
            "backendState": "",
            "dnsName": "",
            "baseUrl": "",
            "authUrl": "",
            "hostName": "",
            "magicDnsSuffix": "",
            "tailscaleIPs": [],
            "error": f"Invalid tailscale status JSON: {exc}",
        }
    if not isinstance(payload, dict):
        return {
            "installed": True,
            "ready": False,
            "executable": executable,
            "backendState": "",
            "dnsName": "",
            "baseUrl": "",
            "authUrl": "",
            "hostName": "",
            "magicDnsSuffix": "",
            "tailscaleIPs": [],
            "error": "tailscale status returned a non-object JSON value.",
        }
    return parse_status(payload, executable)


def wait_until_ready(timeout: float, interval: float = 2.0) -> dict[str, Any]:
    deadline = time.monotonic() + max(0.0, timeout)
    attempts = 0
    started = time.monotonic()
    last = query_status()
    while True:
        attempts += 1
        last["attempts"] = attempts
        last["elapsedSeconds"] = round(time.monotonic() - started, 1)
        if last.get("ready") or time.monotonic() >= deadline:
            return last
        time.sleep(max(0.2, interval))
        last = query_status()


def main() -> int:
    parser = argparse.ArgumentParser(description="Wait for a stable Tailscale *.ts.net hostname.")
    parser.add_argument("--wait", type=float, default=0.0, metavar="SECONDS")
    parser.add_argument("--interval", type=float, default=2.0, metavar="SECONDS")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    state = wait_until_ready(args.wait, args.interval) if args.wait > 0 else query_status()
    if args.json:
        print(json.dumps(state, ensure_ascii=False))
    else:
        for key, value in state.items():
            print(f"{key}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
