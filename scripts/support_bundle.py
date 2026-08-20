#!/usr/bin/env python3
"""Create a source-free, fail-closed KaroX support bundle."""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any, Iterable, Optional

import karox_admin as admin

_MAX_SESSIONS = 20
_MAX_LOGS_PER_SESSION = 8
_MAX_STRUCTURED_LOG_BYTES = 60_000
_MAX_STRUCTURED_LOG_LINES = 250
_MAX_BUNDLE_BYTES = 4_000_000

# These fields can contain user-authored text, browser form values, clipboard
# contents, source/file bodies, prompts, or captured process output.  A support
# bundle is diagnostics, not a transcript, so they are always removed even when
# they do not look like credentials.
_PRIVATE_CONTENT_KEY_RE = re.compile(
    r"(?i)(task|prompt|message|content|clipboard|instruction|"
    r"form(?:data|value)|input(?:data|value)|requestbody|responsebody|"
    r"stdout|stderr|file(?:body|content))"
)
_EVIDENCE_KEY_RE = re.compile(r"(?i)^evidence(?:_ids?|ids?)$")
_URL_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_TOKEN_CANDIDATE_RE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9._~+=-]{32,}(?![A-Za-z0-9])")

# Only fields with stable diagnostic meaning are retained from structured log
# records.  In particular, arbitrary ``data`` payloads are intentionally not
# copied because tool results can contain repository files or user messages.
_LOG_FIELD_ALLOWLIST = frozenset(
    {
        "ts",
        "timestamp",
        "version",
        "mode",
        "branch",
        "action",
        "status",
        "ok",
        "error_code",
        "errorCode",
        "request_id",
        "requestId",
        "correlation_id",
        "correlationId",
        "evidence_id",
        "evidence_ids",
        "duration_ms",
        "durationMs",
        "exit_code",
        "exitCode",
        "pid",
        "serverPid",
        "tunnelPid",
    }
)
_PRIVACY_ASSERTION_KEYS = frozenset(
    {
        "sourceCodeIncluded",
        "rawUserMessagesIncluded",
        "rawProcessOutputIncluded",
        "unstructuredLogContentIncluded",
        "contentIncluded",
    }
)
_SESSION_FIELD_ALLOWLIST = frozenset(
    {
        "id",
        "mode",
        "branch",
        "aiClient",
        "tunnelProvider",
        "startedAt",
        "status",
        "serverAlive",
        "tunnelAlive",
    }
)


def _iter_sensitive_values(value: Any, key: str = "") -> Iterable[str]:
    if key and admin.SECRET_KEY_RE.search(key):
        if isinstance(value, str) and len(value.strip()) >= 8:
            yield value.strip()
        elif isinstance(value, (list, tuple)):
            for item in value:
                if isinstance(item, str) and len(item.strip()) >= 8:
                    yield item.strip()
        return
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            yield from _iter_sensitive_values(child_value, str(child_key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_sensitive_values(item)


def _known_support_secrets() -> set[str]:
    """Collect credential values that the local diagnostics sources may mention.

    Values are used only for in-memory replacement and are never written to the
    bundle.  Environment variables are included because exception/log text can
    echo a host credential even though the environment itself is not exported.
    """
    secrets: set[str] = set()
    settings = admin.load_json(admin.CONFIG_DIR / "settings.json", {})
    secrets.update(_iter_sensitive_values(settings))
    if admin.SESSIONS_DIR.is_dir():
        for directory in admin.SESSIONS_DIR.iterdir():
            if directory.is_dir():
                secrets.update(
                    _iter_sensitive_values(admin.load_json(directory / "session.json", {}))
                )
    for name, value in os.environ.items():
        if admin.SECRET_KEY_RE.search(name) and isinstance(value, str) and len(value.strip()) >= 8:
            secrets.add(value.strip())
    return secrets


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts: dict[str, int] = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _looks_high_entropy(value: str) -> bool:
    if len(value) < 32 or any(char.isspace() for char in value):
        return False
    classes = sum(
        (
            any(char.islower() for char in value),
            any(char.isupper() for char in value),
            any(char.isdigit() for char in value),
            any(not char.isalnum() for char in value),
        )
    )
    entropy = _entropy(value)
    if len(value) >= 40 and re.fullmatch(r"[0-9A-Fa-f]+", value) and entropy >= 3.5:
        return True
    return (classes >= 3 and entropy >= 3.5) or (len(value) >= 48 and entropy >= 4.2)


def _scrub_url(raw: str) -> str:
    try:
        parts = urllib.parse.urlsplit(raw)
        path_parts = []
        for segment in parts.path.split("/"):
            decoded = urllib.parse.unquote(segment)
            path_parts.append("[REDACTED]" if _looks_high_entropy(decoded) else segment)
        query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        safe_query = urllib.parse.urlencode(
            [(name[:100], "[REDACTED_QUERY_VALUE]") for name, _value in query],
            doseq=True,
        )
        fragment = "[REDACTED_FRAGMENT]" if parts.fragment else ""
        return urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, "/".join(path_parts), safe_query, fragment)
        )
    except Exception:
        return "[REDACTED_URL]"


def scrub_text(
    value: str,
    secrets: Iterable[str],
    limit: int = 120_000,
    *,
    preserve_identifier: bool = False,
) -> str:
    # Keep the admin redactor's provider-specific patterns, but disable its
    # truncation here so the support layer owns the single deterministic cap.
    text = admin.redact_string(value, limit=max(limit, len(value)))
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED_KNOWN_SECRET]")
    text = _URL_RE.sub(lambda match: _scrub_url(match.group(0)), text)
    if not preserve_identifier:
        text = _TOKEN_CANDIDATE_RE.sub(
            lambda match: "[REDACTED_HIGH_ENTROPY]"
            if _looks_high_entropy(match.group(0))
            else match.group(0),
            text,
        )
    if len(text) > limit:
        text = text[:limit] + f"… [truncated {len(text) - limit} chars]"
    return text


def scrub_value(value: Any, secrets: Iterable[str], key: str = "") -> Any:
    if key and admin.SECRET_KEY_RE.search(key):
        return "[REDACTED]"
    if key and _PRIVATE_CONTENT_KEY_RE.search(key) and key not in _PRIVACY_ASSERTION_KEYS:
        return "[REDACTED_PRIVATE_CONTENT]"
    preserve_identifier = bool(key and _EVIDENCE_KEY_RE.match(key))
    if isinstance(value, dict):
        return {str(k): scrub_value(v, secrets, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub_value(item, secrets, key if preserve_identifier else "") for item in value[:500]]
    if isinstance(value, tuple):
        return [scrub_value(item, secrets, key if preserve_identifier else "") for item in value[:500]]
    if isinstance(value, str):
        return scrub_text(
            value,
            secrets,
            limit=8_000,
            preserve_identifier=preserve_identifier,
        )
    return value


def _collect_evidence_ids(value: Any, key: str = "") -> set[str]:
    found: set[str] = set()
    if key and _EVIDENCE_KEY_RE.match(key):
        candidates = value if isinstance(value, (list, tuple)) else [value]
        for candidate in candidates:
            if isinstance(candidate, str):
                clean = candidate.strip()
                if clean and len(clean) <= 160 and not any(char.isspace() for char in clean):
                    found.add(clean)
        return found
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            found.update(_collect_evidence_ids(child_value, str(child_key)))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.update(_collect_evidence_ids(item))
    return found


def _safe_session_metadata(raw: dict[str, Any], live: dict[str, Any], secrets: Iterable[str]) -> dict[str, Any]:
    merged = dict(raw)
    merged.update({key: value for key, value in live.items() if key in _SESSION_FIELD_ALLOWLIST})
    selected = {key: merged.get(key) for key in sorted(_SESSION_FIELD_ALLOWLIST) if key in merged}
    evidence_ids = sorted(_collect_evidence_ids(raw))[:100]
    if evidence_ids:
        selected["evidence_ids"] = evidence_ids
    return dict(scrub_value(selected, secrets))


def _structured_log_tail(path: Path, secrets: Iterable[str]) -> str:
    """Return only allowlisted fields from a bounded JSONL tail.

    Unparseable/plain-text rows are represented by a count, never copied.  This
    prevents an arbitrary user message or file body from becoming support data.
    """
    omitted = 0
    rows: list[dict[str, Any]] = []
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            start = max(0, size - _MAX_STRUCTURED_LOG_BYTES)
            handle.seek(start)
            raw = handle.read().decode("utf-8", errors="replace")
        lines = raw.splitlines()
        if start and lines:
            lines = lines[1:]
        for line in lines[-_MAX_STRUCTURED_LOG_LINES:]:
            try:
                item = json.loads(line)
            except Exception:
                omitted += 1
                continue
            if not isinstance(item, dict):
                omitted += 1
                continue
            selected = {
                str(key): item[key]
                for key in sorted(_LOG_FIELD_ALLOWLIST)
                if key in item
            }
            # Some legacy audit records nest diagnostic identifiers under data;
            # copy only explicitly allowlisted scalar identifiers from there.
            nested = item.get("data")
            if isinstance(nested, dict):
                for key in sorted(_LOG_FIELD_ALLOWLIST):
                    if key in nested and key not in selected:
                        selected[key] = nested[key]
            if selected:
                rows.append(dict(scrub_value(selected, secrets)))
            else:
                omitted += 1
    except Exception as exc:
        return json.dumps(
            {"records": [], "omitted": omitted, "readError": type(exc).__name__},
            ensure_ascii=False,
            indent=2,
        )
    return json.dumps(
        {"records": rows, "omitted": omitted, "source": "bounded structured tail"},
        ensure_ascii=False,
        indent=2,
    )


def _assert_safe_payload(value: Any, secrets: Iterable[str], key: str = "") -> None:
    if key and admin.SECRET_KEY_RE.search(key):
        if value != "[REDACTED]":
            raise RuntimeError(f"Sensitive field survived support-bundle redaction: {key}")
        return
    if key and _PRIVATE_CONTENT_KEY_RE.search(key) and key not in _PRIVACY_ASSERTION_KEYS:
        if value != "[REDACTED_PRIVATE_CONTENT]":
            raise RuntimeError(f"Private-content field survived support-bundle redaction: {key}")
        return
    preserve_identifier = bool(key and _EVIDENCE_KEY_RE.match(key))
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            _assert_safe_payload(child_value, secrets, str(child_key))
        return
    if isinstance(value, list):
        for item in value:
            _assert_safe_payload(item, secrets, key if preserve_identifier else "")
        return
    if isinstance(value, str):
        expected = scrub_text(
            value,
            secrets,
            limit=max(8_000, len(value) + 1),
            preserve_identifier=preserve_identifier,
        )
        if expected != value:
            raise RuntimeError("Support bundle final scan found redactable text")


def _validate_bundle(destination: Path, secrets: Iterable[str]) -> None:
    if destination.stat().st_size > _MAX_BUNDLE_BYTES:
        destination.unlink(missing_ok=True)
        raise RuntimeError("Support bundle exceeded the hard size limit")
    with zipfile.ZipFile(destination, "r") as archive:
        for name in archive.namelist():
            for secret in secrets:
                if secret and secret in name:
                    destination.unlink(missing_ok=True)
                    raise RuntimeError("Known secret survived in support-bundle filename")
            if any(_looks_high_entropy(part) for part in name.split("/")):
                destination.unlink(missing_ok=True)
                raise RuntimeError("High-entropy support-bundle filename refused")
            data = archive.read(name).decode("utf-8", errors="ignore")
            for secret in secrets:
                if secret and secret in data:
                    destination.unlink(missing_ok=True)
                    raise RuntimeError(f"Known secret survived redaction in {name}")
            try:
                parsed = json.loads(data)
            except Exception:
                # All generated entries are JSON today.  Fail closed rather than
                # silently accepting a future unstructured export path.
                destination.unlink(missing_ok=True)
                raise RuntimeError(f"Unstructured support-bundle payload refused: {name}")
            try:
                _assert_safe_payload(parsed, secrets)
            except RuntimeError:
                destination.unlink(missing_ok=True)
                raise


def create_support_bundle(output: Optional[Path] = None) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    destination = (output or (Path.cwd() / f"KaroX-support-{timestamp}.zip")).expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    secrets = _known_support_secrets()
    report = admin.doctor_report(include_update=False)
    live_sessions = {
        str(item.get("id")): item
        for item in admin.sessions()
        if isinstance(item, dict) and item.get("id") is not None
    }

    session_records: list[tuple[Path, dict[str, Any]]] = []
    evidence_ids: set[str] = set()
    if admin.SESSIONS_DIR.is_dir():
        for directory in sorted(admin.SESSIONS_DIR.iterdir(), reverse=True)[:_MAX_SESSIONS]:
            if not directory.is_dir():
                continue
            raw = admin.load_json(directory / "session.json", {})
            if not isinstance(raw, dict):
                raw = {}
            evidence_ids.update(_collect_evidence_ids(raw))
            live = live_sessions.get(str(raw.get("id") or directory.name), {})
            session_records.append((directory, _safe_session_metadata(raw, live, secrets)))

    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        summary = {
            "generatedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
            "version": admin.read_version(),
            "platform": admin.platform.platform(),
            "python": sys.version,
            "appRoot": str(admin.APP_ROOT),
            "configDir": str(admin.CONFIG_DIR),
            "runtimeDir": str(admin.RUNTIME_DIR),
            "sessionCount": len(session_records),
            "evidence_ids": sorted(evidence_ids)[:500],
            "doctor": report,
            "privacy": {
                "sourceCodeIncluded": False,
                "rawUserMessagesIncluded": False,
                "rawProcessOutputIncluded": False,
                "unstructuredLogContentIncluded": False,
                "knownValuesRemoved": len(secrets),
                "logMode": "allowlisted structured diagnostics only",
                "maxBundleBytes": _MAX_BUNDLE_BYTES,
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

        for session_index, (directory, metadata) in enumerate(session_records, start=1):
            prefix = f"sessions/session-{session_index:03d}"
            archive.writestr(
                f"{prefix}/session.redacted.json",
                json.dumps(metadata, ensure_ascii=False, indent=2),
            )
            logs = directory / "logs"
            if not logs.is_dir():
                continue
            log_files = [path for path in sorted(logs.iterdir()) if path.is_file()][:_MAX_LOGS_PER_SESSION]
            manifest: list[dict[str, Any]] = []
            structured_index = 0
            for log in log_files:
                suffix = log.suffix.lower()
                entry = {
                    "kind": "structured" if suffix == ".jsonl" else "unstructured",
                    "sizeBytes": min(log.stat().st_size, 2**63 - 1),
                    "contentIncluded": suffix == ".jsonl",
                }
                manifest.append(entry)
                if suffix == ".jsonl":
                    structured_index += 1
                    archive.writestr(
                        f"{prefix}/logs/structured-{structured_index:03d}.json",
                        _structured_log_tail(log, secrets),
                    )
            archive.writestr(
                f"{prefix}/logs/manifest.json",
                json.dumps(scrub_value(manifest, secrets), ensure_ascii=False, indent=2),
            )

        release_cache = admin.CACHE_DIR / "release-status.json"
        if release_cache.is_file():
            release = admin.load_json(release_cache, {})
            archive.writestr(
                "cache/release-status.redacted.json",
                json.dumps(scrub_value(release, secrets), ensure_ascii=False, indent=2),
            )

    _validate_bundle(destination, secrets)
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
