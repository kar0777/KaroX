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
        # Browser/tool cache directory pointer. This is not a credential: it is
        # only a filesystem path a verification command (e.g. a Playwright-based
        # smoke test) uses to locate its own already-downloaded browser
        # binaries. Without forwarding it, a hosted ``checks.run`` of
        # ``npm run test:smoke`` falls back to the default per-user cache, misses
        # the browser, and reports ``Executable doesn't exist`` even though the
        # same command passes in a direct shell. It is path-only, never
        # secret-shaped, and ``redact`` still scans its value for anything that
        # matches a credential pattern before it is shown in a result.
        # NOTE: deliberately NOT forwarding ``NODE_OPTIONS`` (can inject modules
        # via ``--require``) or ``PLAYWRIGHT_DOWNLOAD_HOST`` (can redirect the
        # browser-binary source): both are code-injection vectors a hosted
        # allowlist must not hand to a child.
        "PLAYWRIGHT_BROWSERS_PATH",
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


"""Encoding forced on every guarded child, rather than inherited from the host.

The captured output of a check is decoded as UTF-8 by the caller. A Python child
that inherits a non-UTF-8 console code page -- the default on a Russian-locale
Windows install, where it is cp866 -- writes its diagnostics in that code page
instead, so the failing line the agent needs arrives as bytes the decoder cannot
read. Forcing the child's side of the contract is the only fix that works for
output KaroX does not control the formatting of.

These are set rather than forwarded: an inherited ``PYTHONIOENCODING`` would put
the host back in charge of an invariant the runtime depends on. Neither is a
credential and neither can inject code, which is why they may be added here
while ``NODE_OPTIONS`` may not.
"""
_FORCED_CHILD_ENCODING = {
    "PYTHONIOENCODING": "utf-8",
    "PYTHONUTF8": "1",
}


def child_process_environment(
    source: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build a minimal environment instead of forwarding arbitrary host secrets."""
    values = os.environ if source is None else source
    environment = {
        key: value
        for key, value in values.items()
        if key.upper() in _SAFE_CHILD_ENVIRONMENT or key.upper().startswith("LC_")
    }
    environment.update(_FORCED_CHILD_ENCODING)
    return environment
