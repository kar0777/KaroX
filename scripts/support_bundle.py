#!/usr/bin/env python3
"""Create a source-free, aggressively redacted KaroX support bundle."""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import zipfile
from pathlib import Path
from typing import Any, Iterable, Optional

import karox_admin as admin


def _known_session_secrets() -> set[str]:
    secrets: set[str] = set()
    if not admin.SESSIONS_DIR.is_dir():
        return secrets
    for directory in admin.SESSIONS_DIR.iterdir():
        if not directory.is_dir():
            continue
        session = admin.load_json(directory / "session.json", {})
        if not isinstance(session, dict):
            continue
        for key, value in session.items():
            if admin.SECRET_KEY_RE.search(str(key)) and isinstance(value, str) and len(value.strip()) >= 8:
                secrets.add(value.strip())
    return secrets


def scrub_text(value: str, secrets: Iterable[str], limit: int = 120_000) -> str:
    text = admin.redact_string(value, limit=max(limit, len(value)))
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED_SESSION_SECRET]")
    if len(text) > limit:
        text = text[:limit] + f"… [truncated {len(text) - limit} chars]"
    return text


def scrub_value(value: Any, secrets: Iterable[str], key: str = "") -> Any:
    if key and admin.SECRET_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): scrub_value(v, secrets, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_value(item, secrets) for item in value[:500]]
    if isinstance(value, str):
        return scrub_text(value, secrets, limit=8_000)
    return value


def tail_text(path: Path, secrets: Iterable[str], max_bytes: int = 120_000) -> str:
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - max_bytes))
            return scrub_text(handle.read().decode("utf-8", errors="replace"), secrets, max_bytes)
    except Exception as exc:
        return f"[unavailable: {type(exc).__name__}]"


def create_support_bundle(output: Optional[Path] = None) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    destination = (output or (Path.cwd() / f"KaroX-support-{timestamp}.zip")).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    secrets = _known_session_secrets()
    report = admin.doctor_report(include_update=False)
    session_items = admin.sessions()

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        summary = {
            "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": admin.read_version(),
            "platform": admin.platform.platform(),
            "python": sys.version,
            "appRoot": str(admin.APP_ROOT),
            "configDir": str(admin.CONFIG_DIR),
            "runtimeDir": str(admin.RUNTIME_DIR),
            "sessions": session_items,
            "doctor": report,
            "privacy": {
                "sourceCodeIncluded": False,
                "knownValuesRemoved": len(secrets),
                "logMode": "bounded tails only",
            },
        }
        archive.writestr(
            "summary.json",
            json.dumps(scrub_value(summary, secrets), ensure_ascii=False, indent=2),
        )

        settings = admin.load_json(admin.CONFIG_DIR / "settings.json", {})
        archive.writestr(
            "config/settings.redacted.json",
            json.dumps(scrub_value(settings, secrets), ensure_ascii=False, indent=2),
        )

        if admin.SESSIONS_DIR.is_dir():
            for directory in sorted(admin.SESSIONS_DIR.iterdir(), reverse=True)[:20]:
                if not directory.is_dir():
                    continue
                session = admin.load_json(directory / "session.json", {})
                archive.writestr(
                    f"sessions/{directory.name}/session.redacted.json",
                    json.dumps(scrub_value(session, secrets), ensure_ascii=False, indent=2),
                )
                logs = directory / "logs"
                if logs.is_dir():
                    for log in sorted(logs.iterdir()):
                        if log.is_file() and log.suffix.lower() in {".log", ".txt", ".jsonl"}:
                            archive.writestr(
                                f"sessions/{directory.name}/logs/{log.name}.tail.txt",
                                tail_text(log, secrets),
                            )

        release_cache = admin.CACHE_DIR / "release-status.json"
        if release_cache.is_file():
            archive.writestr(
                "cache/release-status.json",
                tail_text(release_cache, secrets, 30_000),
            )

    with zipfile.ZipFile(destination, "r") as archive:
        for name in archive.namelist():
            data = archive.read(name).decode("utf-8", errors="ignore")
            for secret in secrets:
                if secret and secret in data:
                    destination.unlink(missing_ok=True)
                    raise RuntimeError(f"Known session secret survived redaction in {name}")
            if re.search(r'"apiKey"\s*:\s*"(?!\[REDACTED\])[^"\n]+"', data, re.IGNORECASE):
                destination.unlink(missing_ok=True)
                raise RuntimeError(f"Unredacted apiKey field found in {name}")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description="Create a redacted KaroX support bundle")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = create_support_bundle(args.output)
    print(f"Support bundle created: {result}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
