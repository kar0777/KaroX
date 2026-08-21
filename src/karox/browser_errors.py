"""Typed browser error taxonomy: one vocabulary for every engine.

Mandate: a browser failure must say *what kind* of failure it is --
``element_not_found`` is actionable (fix the locator), ``navigation_timeout``
is actionable (wait or retry), ``user_takeover_required`` is actionable (ask
the person). A generic "browser failure" is none of those. The classifier is
deterministic pattern matching over the message the engine already produced;
it never invents a kind and falls back to ``browser_error`` rather than
guessing.
"""

from __future__ import annotations

import re
from typing import Tuple

__all__ = [
    "BROWSER_ERROR_KINDS",
    "classify_browser_error",
    "prefix_browser_error",
]

ELEMENT_NOT_FOUND = "element_not_found"
AMBIGUOUS_TARGET = "ambiguous_target"
NAVIGATION_TIMEOUT = "navigation_timeout"
PAGE_CLOSED = "page_closed"
BROWSER_DISCONNECTED = "browser_disconnected"
NETWORK_ERROR = "network_error"
DOWNLOAD_FAILURE = "download_failure"
PERMISSION_DENIED = "permission_denied"
USER_TAKEOVER_REQUIRED = "user_takeover_required"
# The fail-soft kind: a failure nobody classified is still a failure, said
# honestly rather than mislabelled.
BROWSER_ERROR = "browser_error"

BROWSER_ERROR_KINDS: Tuple[str, ...] = (
    ELEMENT_NOT_FOUND,
    AMBIGUOUS_TARGET,
    NAVIGATION_TIMEOUT,
    PAGE_CLOSED,
    BROWSER_DISCONNECTED,
    NETWORK_ERROR,
    DOWNLOAD_FAILURE,
    PERMISSION_DENIED,
    USER_TAKEOVER_REQUIRED,
    BROWSER_ERROR,
)

# Ordered: the first matching kind wins, and the more specific patterns sit
# above the generic ones. Every pattern is matched case-insensitively against
# the engine's own message.
_PATTERNS: Tuple[Tuple[str, re.Pattern[str]], ...] = (
    (
        USER_TAKEOVER_REQUIRED,
        re.compile(
            r"takeover|user has control|captcha|two.?factor|2fa\b|oauth consent",
            re.IGNORECASE,
        ),
    ),
    (
        AMBIGUOUS_TARGET,
        re.compile(r"ambiguous", re.IGNORECASE),
    ),
    (
        ELEMENT_NOT_FOUND,
        re.compile(
            r"element not found|element_not_found|no element|not attached|"
            r"has no visible box|not a checkbox|not an input",
            re.IGNORECASE,
        ),
    ),
    (
        DOWNLOAD_FAILURE,
        re.compile(r"download", re.IGNORECASE),
    ),
    (
        NAVIGATION_TIMEOUT,
        re.compile(
            r"wait_for timed out|navigation.*time|timed? ?out|deadline",
            re.IGNORECASE,
        ),
    ),
    (
        PAGE_CLOSED,
        re.compile(
            r"tab does not exist|no tab with|tab was closed|page.*closed|"
            r"cannot close the last remaining tab|frame was detached",
            re.IGNORECASE,
        ),
    ),
    (
        BROWSER_DISCONNECTED,
        re.compile(
            r"not connected|disconnected|websocket|extension is not|"
            r"browser is not open|bridge stopped|connection (?:lost|closed|refused)",
            re.IGNORECASE,
        ),
    ),
    (
        PERMISSION_DENIED,
        re.compile(
            r"permission|not allowed|denied|not enabled|refuses|forbidden",
            re.IGNORECASE,
        ),
    ),
    (
        NETWORK_ERROR,
        re.compile(r"net::err|dns|network", re.IGNORECASE),
    ),
)


def classify_browser_error(message: str) -> str:
    """The typed kind for an engine failure message, or ``browser_error``."""

    text = str(message or "")
    # An engine that already speaks the taxonomy is believed verbatim.
    head = text.split(":", 1)[0].strip().lower()
    if head in BROWSER_ERROR_KINDS:
        return head
    for kind, pattern in _PATTERNS:
        if pattern.search(text):
            return kind
    return BROWSER_ERROR


def prefix_browser_error(message: str) -> str:
    """``kind: message`` with the kind stated exactly once."""

    text = str(message or "")
    head = text.split(":", 1)[0].strip().lower()
    if head in BROWSER_ERROR_KINDS:
        return text
    return f"{classify_browser_error(text)}: {text}"
