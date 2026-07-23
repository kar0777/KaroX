"""Shared credential redaction and child-process environment isolation."""

from __future__ import annotations

import os
import re
from typing import Any, Mapping


SECRET_NAME = re.compile(
    r"(?i)(authorization|api[_-]?key|token|secret|password|credential|cookie)"
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,})\b"),
    re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,})\b"),
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{12,}=*"),
)

_SAFE_CHILD_ENVIRONMENT = frozenset(
    {
        "CI",
        "COLORTERM",
        "COMSPEC",
        "HOME",
        "LANG",
        "LANGUAGE",
        "NO_COLOR",
        "NUMBER_OF_PROCESSORS",
        "OS",
        "PATH",
        "PATHEXT",
        "SYSTEMDRIVE",
        "SYSTEMROOT",
        "TEMP",
        "TERM",
        "TMP",
        "TMPDIR",
        "USERPROFILE",
        "WINDIR",
    }
)


def contains_credential(value: str) -> bool:
    return any(pattern.search(value) for pattern in SECRET_VALUE_PATTERNS)


def redact(value: Any, key: str = "") -> Any:
    """Return a bounded, recursively redacted representation."""
    if key and SECRET_NAME.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(k): redact(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value[:500]]
    if isinstance(value, tuple):
        return [redact(item) for item in value[:500]]
    if isinstance(value, str):
        result = value
        for pattern in SECRET_VALUE_PATTERNS:
            result = pattern.sub("[REDACTED]", result)
        return result[:1_000_000]
    return value


def child_process_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal environment instead of forwarding arbitrary host secrets."""
    values = os.environ if source is None else source
    return {
        key: value
        for key, value in values.items()
        if key.upper() in _SAFE_CHILD_ENVIRONMENT or key.upper().startswith("LC_")
    }
