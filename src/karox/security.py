"""Shared credential redaction and child-process environment isolation."""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Mapping


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
        "APPDATA",
        "CI",
        "COLORTERM",
        "COMSPEC",
        "HOME",
        "LANG",
        "LANGUAGE",
        "LOCALAPPDATA",
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


def redact(
    value: Any,
    key: str = "",
    *,
    secrets: Iterable[str] = (),
    patterns: bool = True,
) -> Any:
    """Return a recursively redacted representation.

    ``patterns=True`` is the display form used for audit rows, error text and
    provider metadata: it rewrites anything matching a secret-shaped pattern and
    clamps very long strings.

    ``patterns=False`` is the transport form for material a caller has to
    reproduce byte for byte, such as file content. Rewriting a token-shaped
    literal inside real source makes an exact-match edit anchor unmatchable and
    turns a read-then-write into silent corruption, and clamping the string loses
    data while the surrounding metadata still describes the whole file. Known
    secret *values* passed in ``secrets`` are still removed in both forms, and
    key-name redaction still applies, so a credential KaroX actually holds never
    travels either way.
    """
    exact = tuple(item for item in secrets if isinstance(item, str) and item)
    token_counter = (
        key.lower().endswith("_tokens")
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value >= 0
    )
    if key and SECRET_NAME.search(key) and not token_counter:
        return "[REDACTED]"
    if isinstance(value, dict):
        return {
            str(k): redact(v, str(k), secrets=exact, patterns=patterns)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [
            redact(item, secrets=exact, patterns=patterns) for item in value[:500]
        ]
    if isinstance(value, tuple):
        return [
            redact(item, secrets=exact, patterns=patterns) for item in value[:500]
        ]
    if isinstance(value, str):
        result = value
        for secret in exact:
            result = result.replace(secret, "[REDACTED]")
        if not patterns:
            return result
        for pattern in SECRET_VALUE_PATTERNS:
            result = pattern.sub("[REDACTED]", result)
        return result[:1_000_000]
    return value


def redact_content(value: Any, *, secrets: Iterable[str] = ()) -> Any:
    """Redact known secret values without rewriting or clamping the payload."""
    return redact(value, secrets=secrets, patterns=False)


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
