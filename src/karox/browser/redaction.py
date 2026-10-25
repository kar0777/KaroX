"""Secret-free evidence helpers shared by every browser surface.

Ported from the proven KaroX v5 ``browser_access`` sanitizers. Nothing that
reaches a tool result, an artifact row, or the audit log passes through raw:
URLs lose query values and token-shaped path segments, console text loses
secret-shaped substrings and base64 blobs, and JSON bodies are reduced to an
allowlist of non-sensitive metadata fields.
"""

from __future__ import annotations

import re
from typing import Any, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from ..security import redact

__all__ = [
    "SENSITIVE_FIELD",
    "allowed_json_values",
    "safe_console_text",
    "safe_field_names",
    "safe_network_url",
]

SENSITIVE_FIELD = re.compile(
    r"(?i)(authorization|cookie|set-cookie|password|passwd|secret|token|csrf|xsrf|"
    r"api[_-]?key|session|card|cvv|cvc|pan|iban|private[_-]?key|refresh|access[_-]?token)"
)
_HIGH_ENTROPY = re.compile(r"[A-Za-z0-9_\-+/=]{24,}")
# Improvement over the v5 console sanitizer: a secret-named ``key=value`` pair
# loses its value even when the value is short or not secret-shaped, and a
# standalone high-entropy token (letters *and* digits, 24+ characters) is
# replaced outright. Console text is diagnostics, not research material, so
# the false-positive cost is acceptable and the leak cost is not.
_KV_SECRET = re.compile(
    r"(?i)\b([A-Za-z0-9_.\-]*(?:authorization|cookie|password|passwd|secret|token|"
    r"csrf|xsrf|api[_-]?key|session|card|cvv|cvc|pan|iban|private[_-]?key|refresh|"
    r"access[_-]?token)[A-Za-z0-9_.\-]*)([=:]\s*)([^\s,;\"']{1,512})"
)
_ENTROPY_TOKEN = re.compile(
    r"\b(?=[A-Za-z0-9_\-+/=]*[0-9])(?=[A-Za-z0-9_\-+/=]*[A-Za-z])[A-Za-z0-9_\-+/=]{24,}\b"
)
_DATA_URI = re.compile(r"(?is)data:[^\s'\"<>]{64,}")
_LONG_BLOB = re.compile(r"[A-Za-z0-9+/=_-]{200,}")
_MAX_CONSOLE_TEXT_CHARS = 1_200

# Metadata keys a JSON response may surface verbatim (billing/usage research
# needs them; everything else stays a field *name* only).
_ALLOWED_VALUE_FIELDS = frozenset(
    {
        "model",
        "model_id",
        "provider",
        "usage",
        "credits",
        "plan",
        "trial",
        "subscription",
    }
)
_SAFE_METADATA_KEYS = frozenset(
    {
        "active",
        "amount",
        "balance",
        "billing_period",
        "completion_tokens",
        "currency",
        "ends_at",
        "expires_at",
        "input_tokens",
        "interval",
        "limit",
        "name",
        "output_tokens",
        "price",
        "prompt_tokens",
        "remaining",
        "renewal_at",
        "starts_at",
        "status",
        "total_tokens",
        "used",
    }
)


def safe_network_url(value: str) -> str:
    """Return a URL with query values and token-like path segments removed."""

    try:
        parsed = urlsplit(value)
    except Exception:
        return ""
    safe_segments = []
    for segment in parsed.path.split("/"):
        safe_segments.append(
            "[REDACTED]" if _HIGH_ENTROPY.fullmatch(segment or "") else segment
        )
    query_names = []
    for name, _value in parse_qsl(parsed.query, keep_blank_values=True):
        safe_name = "[REDACTED]" if SENSITIVE_FIELD.search(name) else name[:100]
        query_names.append((safe_name, "[REDACTED]"))
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit(
        (parsed.scheme, netloc, "/".join(safe_segments), urlencode(query_names), "")
    )


def _sanitize_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > 300 or _HIGH_ENTROPY.fullmatch(value):
            return "[REDACTED]"
        return str(redact(value))
    return str(redact(str(value)))[:300]


def _sanitize_allowed_value(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in list(value.items())[:50]:
            name = str(key).lower()[:120]
            if SENSITIVE_FIELD.search(name) or name not in _SAFE_METADATA_KEYS:
                continue
            result[name] = _sanitize_allowed_value(child, depth + 1)
        return result
    if isinstance(value, list):
        return [_sanitize_allowed_value(item, depth + 1) for item in value[:50]]
    return _sanitize_scalar(value)


def allowed_json_values(value: Any) -> dict[str, Any]:
    """Only the allowlisted, non-sensitive metadata of a decoded JSON body."""

    found: dict[str, Any] = {}

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                name = str(key).lower()
                if SENSITIVE_FIELD.search(name):
                    continue
                if name in _ALLOWED_VALUE_FIELDS and name not in found:
                    found[name] = _sanitize_allowed_value(child)
                visit(child)
        elif isinstance(item, list):
            for child in item[:50]:
                visit(child)

    visit(value)
    return found


def safe_field_names(value: Any, *, limit: int = 200) -> list[str]:
    """Field *names* (never values) of a JSON body, sensitive names excluded."""

    names: list[str] = []

    def visit(item: Any) -> None:
        if len(names) >= limit:
            return
        if isinstance(item, Mapping):
            for key, child in item.items():
                name = str(key)[:120]
                if not SENSITIVE_FIELD.search(name) and name not in names:
                    names.append(name)
                if not SENSITIVE_FIELD.search(name):
                    visit(child)
        elif isinstance(item, list):
            for child in item[:20]:
                visit(child)

    visit(value)
    return names


def safe_console_text(value: Any) -> str:
    """Redact secrets and collapse embedded data/base64 blobs before storage."""

    text = str(redact(str(value)))
    text = _KV_SECRET.sub(
        lambda match: match.group(1) + match.group(2) + "[REDACTED]", text
    )
    text = _ENTROPY_TOKEN.sub("[REDACTED]", text)
    text = _DATA_URI.sub("data:[REDACTED]", text)
    text = _LONG_BLOB.sub("[REDACTED_BLOB]", text)
    return text[:_MAX_CONSOLE_TEXT_CHARS]
