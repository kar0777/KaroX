"""Password-backed one-shot approval fallback for MCP clients without elicitation.

Modern MCP can carry an explicit human approval round through
``elicitation/create``. Some hosted clients (notably current ChatGPT connector
sessions) do not advertise that capability. This module keeps the exact same
one-shot boundary by moving only the *human input* to a tiny password-protected
web page. The model receives an approval URL, never the approval password.

Approval records are process-local, short-lived, bound to the exact tool name,
arguments digest and action digest, and consumed on the first matching retry.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Callable, Optional

from starlette.responses import Response

SecretResolver = str | Callable[[], str]
TRUST_COOKIE_NAME = "__Secure-karox-approval"
TRUST_COOKIE_MAX_AGE_SECONDS = 30 * 24 * 60 * 60


def _resolve_secret(secret_resolver: Optional[SecretResolver]) -> str:
    if secret_resolver is None:
        raise ValueError("human approval secret is not configured")
    value = secret_resolver() if callable(secret_resolver) else secret_resolver
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 65_536
        or any(char in value for char in ("\x00", "\r", "\n"))
    ):
        raise ValueError("human approval secret is invalid")
    return value


def _b64_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")




def issue_trusted_approval_cookie(
    secret_resolver: SecretResolver,
    *,
    now: Optional[float] = None,
    max_age_seconds: int = TRUST_COOKIE_MAX_AGE_SECONDS,
) -> str:
    moment = time.time() if now is None else float(now)
    ttl = max(60, min(int(max_age_seconds), TRUST_COOKIE_MAX_AGE_SECONDS))
    payload = {
        "v": 1,
        "exp": int(moment + ttl),
        "nonce": secrets.token_urlsafe(18),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    signature = hmac.new(
        _resolve_secret(secret_resolver).encode("utf-8"), raw, hashlib.sha256
    ).digest()
    return f"v1.{_b64_encode(raw)}.{_b64_encode(signature)}"




def set_trusted_approval_cookie(
    response: Response,
    secret_resolver: SecretResolver,
) -> None:
    response.set_cookie(
        TRUST_COOKIE_NAME,
        issue_trusted_approval_cookie(secret_resolver),
        max_age=TRUST_COOKIE_MAX_AGE_SECONDS,
        path="/",
        secure=True,
        httponly=True,
        # Lax is required for a trusted browser to carry the cookie when the
        # user opens an approval link from ChatGPT/Claude. Cross-site POSTs are
        # still excluded, while the approval form's own POST is same-site.
        samesite="lax",
    )




__all__ = [
    "SecretResolver",
    "TRUST_COOKIE_MAX_AGE_SECONDS",
    "TRUST_COOKIE_NAME",
    "issue_trusted_approval_cookie",
    "set_trusted_approval_cookie",
]
