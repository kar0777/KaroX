#!/usr/bin/env python3
"""Safely stop live KaroX server/tunnel processes recorded in session metadata."""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def runtime_dir() -> Path:
    configured = os.environ.get("KAROX_RUNTIME_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    home = Path.home()
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "KaroX"
    return Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "KaroX"


RUNTIME_DIR = runtime_dir()
SESSIONS_DIR = RUNTIME_DIR / "sessions"
APP_DIR = RUNTIME_DIR / "app"


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def process_alive(pid: Any) -> bool:
    try:
        number = int(pid)
        if number <= 0:
            return False
        if os.name == "nt":
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {number}", "/NH"],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            )
            return str(number) in result.stdout
        os.kill(number, 0)
        stat_path = Path("/proc") / str(number) / "stat"
        if stat_path.is_file():
            fields = stat_path.read_text(encoding="utf-8", errors="replace").split()
            if len(fields) > 2 and fields[2] == "Z":
                return False
        elif sys.platform == "darwin":
            state = subprocess.run(
                ["ps", "-p", str(number), "-o", "stat="],
                capture_output=True,
                text=True,
                timeout=3,
                check=False,
            ).stdout.strip()
            if state.startswith("Z"):
                return False
        return True
    except Exception:
        return False


def process_command_line(pid: Any) -> str:
    try:
        number = int(pid)
        if number <= 0:
            return ""
        if os.name == "nt":
            script = (
                f'$p=Get-CimInstance Win32_Process -Filter "ProcessId={number}"; '
                "if ($p) { [Console]::Out.Write($p.CommandLine) }"
            )
            result = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return (result.stdout or "").strip()
        cmdline = Path("/proc") / str(number) / "cmdline"
        if cmdline.is_file():
            return cmdline.read_bytes().replace(b"\0", b" ").decode("utf-8", errors="replace").strip()
        return subprocess.run(
            ["ps", "-p", str(number), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        ).stdout.strip()
    except Exception:
        return ""


def _marker(path: Path) -> str:
    try:
        return str(path.resolve()).replace("\\", "/").lower().rstrip("/")
    except Exception:
        return str(path).replace("\\", "/").lower().rstrip("/")


def is_karox_process(pid: Any, role: str = "") -> bool:
    """Refuse stale/reused PIDs unless their current command still belongs to KaroX."""
    command = process_command_line(pid)
    if not command:
        return False
    normalized = command.replace("\\", "/").lower()
    role = role.lower()
    app_marker = _marker(APP_DIR)
    sessions_marker = _marker(SESSIONS_DIR)
    runtime_marker = _marker(RUNTIME_DIR)
    if app_marker and app_marker in normalized:
        return True
    if sessions_marker and sessions_marker in normalized:
        return True
    if runtime_marker and runtime_marker in normalized and any(
        item in normalized for item in ("uvicorn", "repo_tools:app", "notion_entry:app", "cloudflared", "tailscale")
    ):
        return True
    if role == "server":
        return "uvicorn" in normalized and any(
            item in normalized for item in ("repo_tools:app", "notion_entry:app", "notion_gateway")
        )
    if role == "tunnel":
        return ("cloudflared" in normalized and "tunnel" in normalized) or (
            "tailscale" in normalized and "funnel" in normalized
        )
    if role == "runner":
        return any(item in normalized for item in ("run-server", "run-tunnel", "karox"))
    return False


def terminate_process(pid: Any, grace_seconds: float = 5.0) -> dict[str, Any]:
    number = int(pid)
    if not process_alive(number):
        return {"ok": True, "pid": number, "alreadyStopped": True}
    try:
        if os.name == "nt":
            result = subprocess.run(
                ["taskkill", "/PID", str(number), "/T", "/F"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
            detail = (result.stdout + "\n" + result.stderr).strip()
        else:
            os.kill(number, signal.SIGTERM)
            deadline = time.time() + grace_seconds
            while time.time() < deadline and process_alive(number):
                time.sleep(0.1)
            if process_alive(number):
                os.kill(number, signal.SIGKILL)
            detail = ""
        deadline = time.time() + grace_seconds
        while time.time() < deadline and process_alive(number):
            time.sleep(0.1)
        ok = not process_alive(number)
        return {"ok": ok, "pid": number, "detail": detail[-1000:]}
    except Exception as exc:
        return {"ok": False, "pid": number, "error": f"{type(exc).__name__}: {exc}"}


def session_records(session_id: str | None = None) -> list[tuple[str, dict[str, Any]]]:
    records: list[tuple[str, dict[str, Any]]] = []
    if not SESSIONS_DIR.is_dir():
        return records
    for directory in sorted(SESSIONS_DIR.iterdir(), reverse=True):
        if not directory.is_dir():
            continue
        raw = load_json(directory / "session.json")
        current_id = str(raw.get("id") or directory.name)
        if session_id and current_id != session_id:
            continue
        records.append((current_id, raw))
    return records


def stop_sessions(session_id: str | None = None) -> dict[str, Any]:
    records = session_records(session_id)
    if session_id and not records:
        return {"ok": False, "session": session_id, "matched": False, "stopped": 0, "failures": 0, "results": []}
    candidates: list[tuple[str, str, Any]] = []
    for current_id, raw in records:
        for role, key in (("tunnel", "tunnelPid"), ("server", "serverPid"), ("runner", "serverRunnerPid")):
            pid = raw.get(key)
            if pid:
                candidates.append((current_id, role, pid))
    seen: set[int] = set()
    results: list[dict[str, Any]] = []
    for current_id, role, pid in candidates:
        try:
            number = int(pid)
        except (TypeError, ValueError):
            continue
        if number in seen:
            continue
        seen.add(number)
        if not process_alive(number):
            results.append({"ok": True, "session": current_id, "role": role, "pid": number, "alreadyStopped": True})
            continue
        if not is_karox_process(number, role):
            results.append({
                "ok": False,
                "session": current_id,
                "role": role,
                "pid": number,
                "skipped": True,
                "error": "PID is alive but its command line no longer belongs to KaroX",
            })
            continue
        result = terminate_process(number)
        result.update({"session": current_id, "role": role})
        results.append(result)
    failures = sum(1 for item in results if not item.get("ok") and not item.get("alreadyStopped"))
    stopped = sum(1 for item in results if item.get("ok") and not item.get("alreadyStopped"))
    return {
        "ok": failures == 0,
        "session": session_id,
        "matched": bool(records),
        "stopped": stopped,
        "failures": failures,
        "results": results,
    }


def print_result(result: dict[str, Any]) -> int:
    if result.get("session") and not result.get("matched"):
        print(f"Session not found: {result['session']}", file=sys.stderr)
        return 2
    if not result.get("results"):
        print("No live KaroX session processes were found.")
        return 0
    for item in result["results"]:
        if item.get("ok") and item.get("alreadyStopped"):
            marker = "ALREADY"
        elif item.get("ok"):
            marker = "STOPPED"
        elif item.get("skipped"):
            marker = "SKIPPED"
        else:
            marker = "FAILED"
        detail = item.get("error") or ""
        print(f"[{marker:7}] {item.get('session')} · {item.get('role')} · PID {item.get('pid')} {detail}")
    print(f"Stopped: {result.get('stopped', 0)} · failures: {result.get('failures', 0)}")
    return 0 if result.get("ok") else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="karox stop", description="Stop KaroX session processes safely")
    parser.add_argument("--session", help="stop only one session ID")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = stop_sessions(args.session)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else (2 if args.session and not result.get("matched") else 1)
    return print_result(result)


if __name__ == "__main__":
    raise SystemExit(main())
