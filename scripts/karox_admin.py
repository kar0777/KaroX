#!/usr/bin/env python3
"""KaroX administrative CLI shared by Windows, macOS and Linux launchers."""
from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from pathlib import Path
from typing import Any, Iterable, Optional

APP_ROOT = Path(__file__).resolve().parents[1]
RELEASE_STATUS_URL = "https://raw.githubusercontent.com/kar0777/KaroX/main/RELEASE.json"
BOOTSTRAP_PS_URL = "https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.ps1"
BOOTSTRAP_SH_URL = "https://raw.githubusercontent.com/kar0777/KaroX/main/bootstrap.sh"
CACHE_TTL_SECONDS = 6 * 60 * 60

SECRET_KEY_RE = re.compile(
    r"(api[_-]?key|authorization|bearer|token|password|secret|cookie|credential)",
    re.IGNORECASE,
)
SECRET_PATTERNS = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,})\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*"),
)


def _home_paths() -> tuple[Path, Path]:
    home = Path.home()
    if os.name == "nt":
        config = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) / "RepoPilotBridge"
        runtime = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local")) / "RepoPilotBridge"
    elif sys.platform == "darwin":
        config = home / "Library" / "Application Support" / "RepoPilotBridge"
        runtime = home / ".local" / "share" / "RepoPilotBridge"
    else:
        config = Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "RepoPilotBridge"
        runtime = Path(os.environ.get("XDG_DATA_HOME", home / ".local" / "share")) / "RepoPilotBridge"
    return config, runtime


CONFIG_DIR, RUNTIME_DIR = _home_paths()
SESSIONS_DIR = RUNTIME_DIR / "sessions"
CACHE_DIR = RUNTIME_DIR / "cache"


def read_version() -> str:
    version_file = APP_ROOT / "VERSION"
    if version_file.is_file():
        value = version_file.read_text(encoding="utf-8", errors="replace").strip()
        if value:
            return value
    repo_tools = APP_ROOT / "server" / "repo_tools.py"
    if repo_tools.is_file():
        match = re.search(r'^VERSION\s*=\s*"([^"]+)"', repo_tools.read_text(encoding="utf-8", errors="replace"), re.MULTILINE)
        if match:
            return match.group(1)
    return "unknown"


def semver(value: str) -> tuple[int, int, int, str]:
    match = re.match(r"^v?(\d+)\.(\d+)\.(\d+)(.*)$", value.strip())
    if not match:
        return (0, 0, 0, value)
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)), match.group(4))


def redact_string(value: str, limit: int = 8000) -> str:
    text = value
    for pattern in SECRET_PATTERNS:
        text = pattern.sub("[REDACTED]", text)
    if len(text) > limit:
        text = text[:limit] + f"… [truncated {len(text) - limit} chars]"
    return text


def redact(value: Any, key: str = "") -> Any:
    if key and SECRET_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value[:500]]
    if isinstance(value, str):
        return redact_string(value)
    return value


def load_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return default


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
        return True
    except Exception:
        return False


def sessions() -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    if not SESSIONS_DIR.is_dir():
        return output
    for directory in sorted(SESSIONS_DIR.iterdir(), reverse=True):
        if not directory.is_dir():
            continue
        raw = load_json(directory / "session.json", {})
        if not isinstance(raw, dict):
            continue
        server_alive = process_alive(raw.get("serverPid"))
        tunnel_alive = process_alive(raw.get("tunnelPid"))
        status = "running" if server_alive and tunnel_alive else "partial" if server_alive or tunnel_alive else "stopped"
        item = {
            "id": raw.get("id") or directory.name,
            "title": raw.get("title") or raw.get("sessionTitle") or "KaroX session",
            "repo": raw.get("repo"),
            "mode": raw.get("mode"),
            "branch": raw.get("branch"),
            "aiClient": raw.get("aiClient"),
            "tunnelProvider": raw.get("tunnelProvider"),
            "tunnelUrl": raw.get("tunnelUrl"),
            "startedAt": raw.get("startedAt"),
            "status": status,
            "serverAlive": server_alive,
            "tunnelAlive": tunnel_alive,
        }
        output.append(redact(item))
    return output


def command_exists(name: str) -> bool:
    return shutil.which(name) is not None


def module_available(name: str) -> bool:
    try:
        __import__(name)
        return True
    except Exception:
        return False


def fetch_release_status(*, force: bool = False, timeout: float = 2.5) -> dict[str, Any]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = CACHE_DIR / "release-status.json"
    if not force and cache.is_file() and time.time() - cache.stat().st_mtime < CACHE_TTL_SECONDS:
        cached = load_json(cache, {})
        if isinstance(cached, dict) and cached.get("version"):
            return cached
    request = urllib.request.Request(
        RELEASE_STATUS_URL,
        headers={"User-Agent": f"KaroX/{read_version()} update-check", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
        if not isinstance(data, dict) or not data.get("version"):
            raise ValueError("release metadata has no version")
        cache.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return data
    except Exception:
        cached = load_json(cache, {})
        if isinstance(cached, dict) and cached.get("version"):
            cached = dict(cached)
            cached["stale"] = True
            return cached
        raise


def check_result(name: str, ok: bool, detail: str = "", severity: str = "error") -> dict[str, Any]:
    return {"name": name, "ok": bool(ok), "detail": detail, "severity": severity}


def doctor_report(*, include_update: bool = True) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    version = read_version()
    checks.append(check_result("Application root", APP_ROOT.is_dir(), str(APP_ROOT)))
    for relative in (
        "VERSION",
        "start.ps1",
        "start.sh",
        "start.core.ps1",
        "start.core.sh",
        "server/repo_tools.py",
        "server/app_entry.py",
        "scripts/patch_notion_provider.py",
        "scripts/karox_admin.py",
    ):
        path = APP_ROOT / relative
        checks.append(check_result(relative, path.is_file(), str(path)))
    checks.append(check_result("Python", sys.version_info >= (3, 10), sys.version.split()[0]))
    for module in ("fastapi", "uvicorn", "pydantic", "httpx", "mcp"):
        checks.append(check_result(f"Python module: {module}", module_available(module)))
    checks.append(check_result("Git", command_exists("git"), shutil.which("git") or "not found"))
    cloudflared = shutil.which("cloudflared") or str(RUNTIME_DIR / "bin" / ("cloudflared.exe" if os.name == "nt" else "cloudflared"))
    checks.append(check_result("Cloudflare Tunnel", Path(cloudflared).exists() if os.path.isabs(cloudflared) else command_exists("cloudflared"), cloudflared, "warn"))
    checks.append(check_result("Runtime directory writable", _is_writable(RUNTIME_DIR), str(RUNTIME_DIR)))
    checks.append(check_result("Config directory writable", _is_writable(CONFIG_DIR), str(CONFIG_DIR)))

    settings_path = CONFIG_DIR / "settings.json"
    settings = load_json(settings_path, None)
    settings_ok = settings is None or isinstance(settings, dict)
    checks.append(check_result("settings.json", settings_ok, "not created yet" if settings is None else str(settings_path)))
    if isinstance(settings, dict):
        language = settings.get("language", "en")
        provider = settings.get("tunnelProvider", "cloudflare")
        client = settings.get("aiClient", "promptql")
        checks.append(check_result("Configured language", language in {"en", "ru"}, str(language), "warn"))
        checks.append(check_result("Configured tunnel", provider in {"cloudflare", "tailscale"}, str(provider), "warn"))
        checks.append(check_result("Configured AI target", client in {"promptql", "notion", "other", "letaido"}, str(client), "warn"))

    release: Optional[dict[str, Any]] = None
    if include_update:
        try:
            release = fetch_release_status()
            latest = str(release.get("version"))
            checks.append(check_result("Release metadata", True, latest, "warn"))
        except Exception as exc:
            checks.append(check_result("Release metadata", False, str(exc), "warn"))

    errors = [item for item in checks if not item["ok"] and item["severity"] == "error"]
    warnings = [item for item in checks if not item["ok"] and item["severity"] == "warn"]
    return {
        "ok": not errors,
        "version": version,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "appRoot": str(APP_ROOT),
        "configDir": str(CONFIG_DIR),
        "runtimeDir": str(RUNTIME_DIR),
        "sessionCount": len(sessions()),
        "errors": len(errors),
        "warnings": len(warnings),
        "release": release,
        "checks": checks,
    }


def _is_writable(path: Path) -> bool:
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / f".karox-write-{os.getpid()}"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        return True
    except Exception:
        return False


def print_doctor(report: dict[str, Any]) -> None:
    print(f"★ KaroX doctor · v{report['version']} · {report['platform']}")
    print("─" * 72)
    for item in report["checks"]:
        if item["ok"]:
            marker = "OK"
        elif item["severity"] == "warn":
            marker = "WARN"
        else:
            marker = "FAIL"
        detail = f" — {item['detail']}" if item.get("detail") else ""
        print(f"[{marker:4}] {item['name']}{detail}")
    print("─" * 72)
    print(f"Result: {'READY' if report['ok'] else 'NEEDS ATTENTION'} · errors={report['errors']} warnings={report['warnings']}")


def run_deep_doctor() -> int:
    if os.name == "nt":
        script = APP_ROOT / "doctor.ps1"
        if not script.is_file():
            print("doctor.ps1 is missing", file=sys.stderr)
            return 2
        return subprocess.call(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)])
    script = APP_ROOT / "doctor.sh"
    if not script.is_file():
        print("doctor.sh is missing", file=sys.stderr)
        return 2
    return subprocess.call(["bash", str(script)])


def tail_text(path: Path, max_bytes: int = 120_000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return redact_string(handle.read().decode("utf-8", errors="replace"), max_bytes)
    except Exception as exc:
        return f"[unavailable: {exc}]"


def create_support_bundle(output: Optional[Path]) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    destination = output or (Path.cwd() / f"KaroX-support-{timestamp}.zip")
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    report = doctor_report(include_update=False)
    session_items = sessions()

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("summary.json", json.dumps(redact({
            "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": read_version(),
            "platform": platform.platform(),
            "python": sys.version,
            "appRoot": str(APP_ROOT),
            "configDir": str(CONFIG_DIR),
            "runtimeDir": str(RUNTIME_DIR),
            "sessions": session_items,
            "doctor": report,
        }), ensure_ascii=False, indent=2))

        settings = load_json(CONFIG_DIR / "settings.json", {})
        archive.writestr("config/settings.redacted.json", json.dumps(redact(settings), ensure_ascii=False, indent=2))

        if SESSIONS_DIR.is_dir():
            for directory in sorted(SESSIONS_DIR.iterdir(), reverse=True)[:20]:
                if not directory.is_dir():
                    continue
                session = load_json(directory / "session.json", {})
                archive.writestr(
                    f"sessions/{directory.name}/session.redacted.json",
                    json.dumps(redact(session), ensure_ascii=False, indent=2),
                )
                logs = directory / "logs"
                if logs.is_dir():
                    for log in sorted(logs.iterdir()):
                        if log.is_file() and log.suffix.lower() in {".log", ".txt", ".jsonl"}:
                            archive.writestr(f"sessions/{directory.name}/logs/{log.name}.tail.txt", tail_text(log))

        release_cache = CACHE_DIR / "release-status.json"
        if release_cache.is_file():
            archive.writestr("cache/release-status.json", tail_text(release_cache, 30_000))

    # Final defensive scan: a known session key must never survive in the bundle.
    with zipfile.ZipFile(destination, "r") as archive:
        for name in archive.namelist():
            data = archive.read(name).decode("utf-8", errors="ignore")
            if re.search(r'"apiKey"\s*:\s*"(?!\[REDACTED\])[^"\n]+"', data, re.IGNORECASE):
                destination.unlink(missing_ok=True)
                raise RuntimeError(f"Support bundle safety check failed in {name}")
    return destination


def latest_running_session(session_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    items = sessions()
    if session_id:
        return next((item for item in items if str(item.get("id")) == session_id), None)
    return next((item for item in items if item.get("status") in {"running", "partial"} and item.get("tunnelUrl")), None)


def print_status(as_json: bool) -> int:
    data = {
        "version": read_version(),
        "appRoot": str(APP_ROOT),
        "configDir": str(CONFIG_DIR),
        "runtimeDir": str(RUNTIME_DIR),
        "sessions": sessions(),
    }
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return 0
    print(f"★ KaroX v{data['version']}")
    print(f"App     : {data['appRoot']}")
    print(f"Runtime : {data['runtimeDir']}")
    print()
    if not data["sessions"]:
        print("No saved sessions.")
        return 0
    for item in data["sessions"]:
        print(f"[{str(item['status']).upper():7}] {item['id']} · {item['title']}")
        print(f"          {item.get('repo') or '-'}")
        print(f"          {item.get('branch') or '-'} · {item.get('mode') or '-'} · {item.get('aiClient') or '-'}")
    return 0


def print_update_status(status: dict[str, Any], *, quiet: bool = False) -> tuple[bool, str]:
    current = read_version()
    latest = str(status.get("version", "unknown"))
    newer = semver(latest) > semver(current)
    if not quiet:
        print(f"Current: v{current}")
        print(f"Latest : v{latest}{' (cached)' if status.get('stale') else ''}")
        print("Update available." if newer else "KaroX is up to date.")
        if status.get("url"):
            print(f"Release: {status['url']}")
    return newer, latest


def apply_update(yes: bool) -> int:
    status = fetch_release_status(force=True, timeout=5)
    newer, latest = print_update_status(status)
    if not newer:
        return 0
    if not yes:
        answer = input(f"Install KaroX v{latest} now? [Y/n] ").strip().lower()
        if answer not in {"", "y", "yes", "д", "да"}:
            print("Update cancelled.")
            return 0
    url = BOOTSTRAP_PS_URL if os.name == "nt" else BOOTSTRAP_SH_URL
    suffix = ".ps1" if os.name == "nt" else ".sh"
    with tempfile.TemporaryDirectory(prefix="karox-update-") as tmp:
        script = Path(tmp) / f"bootstrap{suffix}"
        request = urllib.request.Request(url, headers={"User-Agent": f"KaroX/{read_version()} updater"})
        with urllib.request.urlopen(request, timeout=15) as response:
            script.write_bytes(response.read())
        env = os.environ.copy()
        env["KAROX_NO_START"] = "1"
        if os.name == "nt":
            command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]
        else:
            command = ["bash", str(script)]
        print("Starting verified stable-channel installer…")
        return subprocess.call(command, env=env)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="karox", description="KaroX product administration")
    sub = parser.add_subparsers(dest="command", required=True)

    version = sub.add_parser("version", help="show installed version")
    version.add_argument("--json", action="store_true")

    status = sub.add_parser("status", help="show saved and live sessions")
    status.add_argument("--json", action="store_true")

    doctor = sub.add_parser("doctor", help="run product diagnostics")
    doctor.add_argument("--json", action="store_true")
    doctor.add_argument("--deep", action="store_true", help="also run the full endpoint harness")

    update = sub.add_parser("update", help="check for or install a stable release")
    update.add_argument("--check", action="store_true", help="only check for updates")
    update.add_argument("--yes", action="store_true", help="install without confirmation")

    support = sub.add_parser("support", help="create a redacted support bundle")
    support.add_argument("--output", type=Path)

    dashboard = sub.add_parser("dashboard", help="open Control Center for a live session")
    dashboard.add_argument("--session")

    sub.add_parser("notice", help=argparse.SUPPRESS)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "version":
        data = {"name": "KaroX", "version": read_version(), "appRoot": str(APP_ROOT)}
        print(json.dumps(data, ensure_ascii=False) if args.json else f"KaroX v{data['version']}")
        return 0
    if args.command == "status":
        return print_status(args.json)
    if args.command == "doctor":
        report = doctor_report()
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print_doctor(report)
        if args.deep:
            deep_code = run_deep_doctor()
            if deep_code:
                return deep_code
        return 0 if report["ok"] else 1
    if args.command == "update":
        if args.check:
            status = fetch_release_status(force=True, timeout=5)
            newer, _ = print_update_status(status)
            return 10 if newer else 0
        return apply_update(args.yes)
    if args.command == "support":
        path = create_support_bundle(args.output)
        print(f"Support bundle created: {path}")
        return 0
    if args.command == "dashboard":
        item = latest_running_session(args.session)
        if not item or not item.get("tunnelUrl"):
            print("No live session with a public URL was found.", file=sys.stderr)
            return 2
        url = str(item["tunnelUrl"]).rstrip("/") + "/control"
        print(f"Opening {url}")
        return 0 if webbrowser.open(url, new=2) else 1
    if args.command == "notice":
        try:
            status = fetch_release_status(timeout=0.8)
            newer, latest = print_update_status(status, quiet=True)
            if newer:
                print(f"  ↑ KaroX v{latest} is available · run: karox update")
        except Exception:
            pass
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
