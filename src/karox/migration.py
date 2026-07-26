"""Secret-safe import of explicitly supported legacy metadata."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Iterable
from urllib.parse import urlsplit

from .security import contains_credential, redact


class MigrationError(RuntimeError):
    pass


_SOURCE_FILES = ("settings.json", "config.json", "providers.json")
_SAFE_FIELDS = {
    "language": "language",
    "locale": "locale",
    "theme": "theme",
    "provider": "default_provider",
    "defaultprovider": "default_provider",
    "default_provider": "default_provider",
    "model": "default_model",
    "defaultmodel": "default_model",
    "default_model": "default_model",
    "baseurl": "base_url",
    "base_url": "base_url",
    "accessprofile": "access_profile",
    "access_profile": "access_profile",
}
_SECRET_FIELD = re.compile(
    r"(?i)(authorization|api.?key|bearer|cookie|credential|password|private.?key|secret|token)"
)
def _field_key(value: str) -> str:
    return value.replace("-", "").replace(" ", "").lower()


def _safe_label(value: str) -> str:
    return str(redact(value[:200]))


def _safe_value(name: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("metadata value must be a string")
    if not value.strip() or len(value) > 2000 or "\x00" in value:
        raise ValueError("metadata value is empty, too large, or invalid")
    if contains_credential(value):
        raise ValueError("metadata value resembles a credential")
    if name == "base_url":
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("provider base URL must use HTTP or HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("provider base URL must not contain credentials or query data")
    if name == "access_profile" and value not in {
        "read_only",
        "workspace_write",
        "elevated",
    }:
        raise ValueError("unsupported access profile")
    return value


def _is_link_or_reparse(path: Path) -> bool:
    metadata = path.lstat()
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return path.is_symlink() or bool(
        getattr(metadata, "st_file_attributes", 0) & reparse_flag
    )


def _atomic_json(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _source_digest(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def migrate_legacy_metadata(
    source: Path,
    destination: Path,
    *,
    dry_run: bool = True,
) -> Dict[str, Any]:
    """Import whitelisted metadata while leaving the legacy source untouched."""
    source = source.expanduser().resolve(strict=True)
    if not source.is_dir():
        raise MigrationError(f"legacy config is not a directory: {source}")
    destination = destination.expanduser().resolve(strict=False)
    if destination == source or source in destination.parents:
        raise MigrationError("migration destination must be outside the legacy source")

    candidates: list[Path] = []
    unsafe_sources: list[Dict[str, str]] = []
    for name in _SOURCE_FILES:
        candidate = source / name
        if not candidate.exists():
            continue
        if _is_link_or_reparse(candidate):
            unsafe_sources.append({"file": name, "reason": "link_or_reparse_point"})
            continue
        if candidate.is_file():
            candidates.append(candidate)
    before_digest = _source_digest(candidates)
    imported: Dict[str, Any] = {}
    imported_from: Dict[str, str] = {}
    skipped_secrets: list[Dict[str, str]] = []
    skipped_unknown: list[Dict[str, str]] = []
    invalid: list[Dict[str, str]] = list(unsafe_sources)

    for path in candidates:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            invalid.append({"file": path.name, "reason": type(exc).__name__})
            continue
        if not isinstance(payload, dict):
            invalid.append({"file": path.name, "reason": "root_not_object"})
            continue
        for field, value in payload.items():
            field_name = str(field)
            if _SECRET_FIELD.search(field_name):
                skipped_secrets.append(
                    {"file": path.name, "field": "[SECRET_FIELD]"}
                )
                continue
            canonical = _SAFE_FIELDS.get(_field_key(field_name))
            if canonical is None:
                skipped_unknown.append(
                    {"file": path.name, "field": _safe_label(field_name)}
                )
                continue
            try:
                safe = _safe_value(canonical, value)
            except ValueError as exc:
                invalid.append(
                    {
                        "file": path.name,
                        "field": _safe_label(field_name),
                        "reason": str(exc),
                    }
                )
                continue
            if canonical in imported and imported[canonical] != safe:
                invalid.append(
                    {
                        "file": path.name,
                        "field": _safe_label(field_name),
                        "reason": f"conflicts with {imported_from[canonical]}",
                    }
                )
                continue
            imported[canonical] = safe
            imported_from[canonical] = f"{path.name}:{field_name}"

    unrecognized_files = sorted(
        _safe_label(path.name)
        for path in source.iterdir()
        if path.is_file() and path.name not in _SOURCE_FILES
    )
    report: Dict[str, Any] = {
        "schema_version": 1,
        "dry_run": dry_run,
        "source": _safe_label(str(source)),
        "destination": _safe_label(str(destination)),
        "source_files": [path.name for path in candidates],
        "source_digest": before_digest,
        "imported_fields": sorted(imported),
        "skipped_secret_fields": skipped_secrets,
        "skipped_unknown_fields": skipped_unknown,
        "unrecognized_files": unrecognized_files,
        "invalid_fields": invalid,
        "source_untouched": True,
        "applied": False,
    }
    if not candidates:
        report["diagnostic"] = "no_supported_source_files"
    if not dry_run:
        if not candidates:
            raise MigrationError("no supported legacy metadata files were found")
        settings_path = destination / "imported-settings.json"
        report_path = destination / "migration-report.json"
        if settings_path.exists() or report_path.exists():
            raise MigrationError(
                f"refusing to overwrite an existing migration in {destination}"
            )
        if _source_digest(candidates) != before_digest:
            raise MigrationError("legacy source changed during migration")
        created: list[Path] = []
        try:
            _atomic_json(
                settings_path,
                {
                    "schema_version": 1,
                    "migrated_at": time.time(),
                    "settings": imported,
                    "source_digest": before_digest,
                },
            )
            created.append(settings_path)
            report["applied"] = True
            report["settings_file"] = _safe_label(str(settings_path))
            _atomic_json(report_path, report)
            created.append(report_path)
            if _source_digest(candidates) != before_digest:
                raise MigrationError("legacy source changed during migration")
        except Exception:
            for created_path in reversed(created):
                try:
                    created_path.unlink()
                except FileNotFoundError:
                    pass
            raise

    if _source_digest(candidates) != before_digest:
        raise MigrationError("legacy source changed during migration")
    return report
