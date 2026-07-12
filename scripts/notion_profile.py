#!/usr/bin/env python3
"""Persistent Notion MCP connection profile for KaroX.

The profile keeps one Bearer credential and the stable Tailscale Funnel hostname
across repository sessions and KaroX upgrades. On Windows the credential is
protected with the current user's DPAPI key when pywin32 is available.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

SCHEMA_VERSION = 1
PROFILE_NAME = "notion-connection.json"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def default_config_dir() -> Path:
    override = os.environ.get("KAROX_CONFIG_DIR")
    if override:
        return Path(override).expanduser()
    if os.name == "nt":
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "RepoPilotBridge"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "RepoPilotBridge"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "RepoPilotBridge"


def profile_path(config_dir: Path | None = None) -> Path:
    return (config_dir or default_config_dir()) / PROFILE_NAME


def _protect_windows(value: str) -> str | None:
    if os.name != "nt":
        return None
    try:
        import win32crypt  # type: ignore

        encrypted = win32crypt.CryptProtectData(
            value.encode("utf-8"),
            "KaroX Notion persistent MCP key",
            None,
            None,
            None,
            0,
        )
        if isinstance(encrypted, tuple):
            encrypted = encrypted[-1]
        return base64.b64encode(bytes(encrypted)).decode("ascii")
    except Exception:
        return None


def _unprotect_windows(value: str) -> str:
    import win32crypt  # type: ignore

    result = win32crypt.CryptUnprotectData(base64.b64decode(value), None, None, None, 0)
    decrypted = result[-1] if isinstance(result, tuple) else result
    return bytes(decrypted).decode("utf-8")


def _encode_key(key: str) -> dict[str, str]:
    protected = _protect_windows(key)
    if protected:
        return {"keyStorage": "windows-dpapi", "protectedKey": protected}
    return {"keyStorage": "file-0600", "apiKey": key}


def _decode_key(profile: dict[str, Any]) -> str:
    storage = str(profile.get("keyStorage", ""))
    if storage == "windows-dpapi":
        protected = str(profile.get("protectedKey", ""))
        if not protected:
            raise RuntimeError("The Notion profile is missing its protected credential.")
        try:
            return _unprotect_windows(protected)
        except Exception as exc:
            raise RuntimeError(
                "The persistent Notion key cannot be decrypted for this Windows user. "
                "Run: karox notion reset-connection"
            ) from exc
    key = str(profile.get("apiKey", ""))
    if not key:
        raise RuntimeError("The Notion profile is missing its credential.")
    return key


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        try:
            os.chmod(tmp_name, 0o600)
        except OSError:
            pass
        os.replace(tmp_name, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass


def _new_profile() -> dict[str, Any]:
    key = secrets.token_urlsafe(48)
    now = utc_now()
    return {
        "schemaVersion": SCHEMA_VERSION,
        "provider": "tailscale",
        "createdAt": now,
        "updatedAt": now,
        "baseUrl": "",
        **_encode_key(key),
    }


def load_profile(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Invalid Notion connection profile: {path}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"Invalid Notion connection profile: {path}")
    if int(data.get("schemaVersion", 0)) != SCHEMA_VERSION:
        raise RuntimeError("Unsupported Notion connection profile version.")
    return data


def ensure_profile(path: Path) -> dict[str, Any]:
    profile = load_profile(path)
    if profile is None:
        profile = _new_profile()
        _atomic_write(path, profile)
    _decode_key(profile)
    return profile


def rotate_key(path: Path) -> dict[str, Any]:
    profile = ensure_profile(path)
    for field in ("apiKey", "protectedKey", "keyStorage"):
        profile.pop(field, None)
    profile.update(_encode_key(secrets.token_urlsafe(48)))
    profile["updatedAt"] = utc_now()
    _atomic_write(path, profile)
    return profile


def normalize_base_url(value: str) -> str:
    raw = value.strip().rstrip("/")
    parsed = urlparse(raw)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("The persistent Notion URL must be an HTTPS origin.")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError("Store only the HTTPS origin; /mcp is added automatically.")
    host = parsed.hostname.lower().rstrip(".")
    if not (host.endswith(".ts.net") or host.endswith(".trycloudflare.com") or os.environ.get("KAROX_ALLOW_CUSTOM_NOTION_URL") == "1"):
        raise ValueError("Expected a Tailscale .ts.net origin. Custom origins require KAROX_ALLOW_CUSTOM_NOTION_URL=1.")
    port = f":{parsed.port}" if parsed.port else ""
    return f"https://{host}{port}"


def set_url(path: Path, value: str) -> dict[str, Any]:
    profile = ensure_profile(path)
    normalized = normalize_base_url(value)
    previous = str(profile.get("baseUrl", ""))
    profile["baseUrl"] = normalized
    profile["updatedAt"] = utc_now()
    _atomic_write(path, profile)
    profile["urlChanged"] = bool(previous and previous != normalized)
    return profile


def find_tailscale() -> str | None:
    candidates: list[str] = []
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


def tailscale_status() -> dict[str, Any]:
    executable = find_tailscale()
    if not executable:
        return {"installed": False, "ready": False, "executable": "", "dnsName": "", "baseUrl": "", "error": "Tailscale is not installed."}
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
        return {"installed": True, "ready": False, "executable": executable, "dnsName": "", "baseUrl": "", "error": str(exc)}
    if result.returncode != 0:
        return {
            "installed": True,
            "ready": False,
            "executable": executable,
            "dnsName": "",
            "baseUrl": "",
            "error": (result.stderr or result.stdout or "tailscale status failed").strip(),
        }
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        return {"installed": True, "ready": False, "executable": executable, "dnsName": "", "baseUrl": "", "error": f"Invalid tailscale status JSON: {exc}"}
    self_node = payload.get("Self") if isinstance(payload.get("Self"), dict) else {}
    dns_name = str(self_node.get("DNSName") or payload.get("DNSName") or "").strip().rstrip(".")
    ready = str(payload.get("BackendState", "")).lower() == "running" and bool(dns_name)
    return {
        "installed": True,
        "ready": ready,
        "executable": executable,
        "dnsName": dns_name,
        "baseUrl": f"https://{dns_name}" if dns_name else "",
        "error": "" if ready else "Tailscale is installed but is not connected or MagicDNS is unavailable.",
    }


def sync_tailscale(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    profile = ensure_profile(path)
    status = tailscale_status()
    if status.get("ready") and status.get("baseUrl"):
        profile = set_url(path, str(status["baseUrl"]))
    return profile, status


def public_view(profile: dict[str, Any], path: Path, include_key: bool = False) -> dict[str, Any]:
    key = _decode_key(profile)
    base_url = str(profile.get("baseUrl", ""))
    data: dict[str, Any] = {
        "configured": True,
        "schemaVersion": profile.get("schemaVersion"),
        "provider": profile.get("provider", "tailscale"),
        "baseUrl": base_url,
        "mcpUrl": f"{base_url}/mcp" if base_url else "",
        "tokenHint": f"…{key[-6:]}",
        "keyStorage": profile.get("keyStorage"),
        "configPath": str(path),
        "createdAt": profile.get("createdAt", ""),
        "updatedAt": profile.get("updatedAt", ""),
    }
    if include_key:
        data["apiKey"] = key
    return data


def emit(data: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False))
        return
    for key, value in data.items():
        print(f"{key}: {value}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the persistent KaroX Notion MCP connection.")
    parser.add_argument("--config-dir", type=Path, default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("ensure", "status", "sync-tailscale", "connection"):
        item = sub.add_parser(name)
        item.add_argument("--json", action="store_true")
        item.add_argument("--include-key", action="store_true")
        if name == "connection":
            item.add_argument("--show-token", action="store_true")
    set_url_parser = sub.add_parser("set-url")
    set_url_parser.add_argument("--url", required=True)
    set_url_parser.add_argument("--json", action="store_true")
    rotate = sub.add_parser("rotate")
    rotate.add_argument("--json", action="store_true")
    sub.add_parser("key")
    sub.add_parser("reset")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    path = profile_path(args.config_dir)
    if args.command == "reset":
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        print(str(path))
        return 0
    if args.command == "key":
        print(_decode_key(ensure_profile(path)))
        return 0
    if args.command == "rotate":
        profile = rotate_key(path)
        emit(public_view(profile, path), args.json)
        return 0
    if args.command == "set-url":
        profile = set_url(path, args.url)
        view = public_view(profile, path)
        view["urlChanged"] = bool(profile.get("urlChanged"))
        emit(view, args.json)
        return 0
    if args.command == "sync-tailscale":
        profile, status = sync_tailscale(path)
        view = public_view(profile, path, include_key=args.include_key)
        view["tailscale"] = status
        emit(view, args.json)
        return 0
    profile = ensure_profile(path) if args.command in {"ensure", "connection"} else load_profile(path)
    if profile is None:
        emit({"configured": False, "configPath": str(path)}, args.json)
        return 1
    include_key = bool(args.include_key or (args.command == "connection" and args.show_token))
    emit(public_view(profile, path, include_key=include_key), args.json)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1)
