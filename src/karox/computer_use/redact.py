"""Redaction helpers for desktop evidence.

Observations shown to a model may carry window text, but anything that is
*persisted* (evidence records, audit rows, approval headlines) goes through
these helpers first: typed text becomes a length and a digest, and free text
gets credential-shaped values masked.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

_SECRET_KEYS = (
    r"password|passwd|pwd|token|secret|api[_-]?key|apikey|authorization|auth|"
    r"bearer|cookie|credential[s]?|access[_-]?key|private[_-]?key|otp|cvv"
)
_SECRET_PATTERN = re.compile(
    rf"(?i)\b({_SECRET_KEYS})\b(\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|\S+)"
)
_CLIP_MARKER = "[redacted:clipped]"


def redact_text(text: str) -> dict[str, Any]:
    """A content-free, deterministic description of one text payload."""

    if not isinstance(text, str):
        raise ValueError("redact_text requires a string")
    return {
        "chars": len(text),
        "sha256_16": hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()[:16],
    }


def scrub(text: str, limit: int = 500) -> str:
    """Mask credential-shaped ``key=value``/``key: value`` pairs and clip."""

    if not isinstance(text, str):
        return ""
    masked = _SECRET_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
    if limit > 0 and len(masked) > limit:
        masked = masked[:limit] + _CLIP_MARKER
    return masked


__all__ = ["redact_text", "scrub"]
