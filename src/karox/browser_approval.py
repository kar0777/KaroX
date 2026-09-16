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
import html
import json
import secrets
import threading
import time
from http.cookies import SimpleCookie
from typing import Any, Callable, Mapping, Optional
from urllib.parse import parse_qs, quote

from starlette.responses import HTMLResponse, Response

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


def _b64_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 4096:
        raise ValueError("trusted approval cookie is invalid")
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.urlsafe_b64decode((value + padding).encode("ascii"))
    except (ValueError, UnicodeEncodeError) as exc:
        raise ValueError("trusted approval cookie is invalid") from exc
    if _b64_encode(decoded) != value:
        raise ValueError("trusted approval cookie is not canonical base64url")
    return decoded


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


def validate_trusted_approval_cookie(
    value: str,
    secret_resolver: SecretResolver,
    *,
    now: Optional[float] = None,
) -> bool:
    try:
        parts = value.split(".")
        if len(parts) != 3 or parts[0] != "v1":
            return False
        raw = _b64_decode(parts[1])
        signature = _b64_decode(parts[2])
        expected = hmac.new(
            _resolve_secret(secret_resolver).encode("utf-8"), raw, hashlib.sha256
        ).digest()
        if not hmac.compare_digest(signature, expected):
            return False
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict) or payload.get("v") != 1:
            return False
        expires_at = payload.get("exp")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int):
            return False
        moment = time.time() if now is None else float(now)
        if expires_at < moment:
            return False
        nonce = payload.get("nonce")
        return isinstance(nonce, str) and 8 <= len(nonce) <= 128
    except (TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False


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


class BrowserApprovalBroker:
    """Short-lived exact-action approvals completed by a human in a browser."""

    def __init__(
        self,
        *,
        path: str,
        secret_resolver: Optional[SecretResolver],
        base_url: Optional[str] = None,
        ttl_seconds: float = 300.0,
    ) -> None:
        self.path = path.rstrip("/") or "/mcp"
        self.approval_path = f"{self.path}/approval"
        self.secret_resolver = secret_resolver
        normalized_base = (base_url or "").rstrip("/")
        if normalized_base and not normalized_base.startswith(("https://", "http://")):
            raise ValueError("browser approval base URL must use HTTP(S)")
        self.base_url = normalized_base or None
        self.ttl_seconds = max(30.0, min(float(ttl_seconds), 600.0))
        self._lock = threading.Lock()
        self._records: dict[str, dict[str, Any]] = {}

    @property
    def available(self) -> bool:
        return self.secret_resolver is not None

    @staticmethod
    def _headers(scope: Mapping[str, Any]) -> dict[str, str]:
        result: dict[str, str] = {}
        for raw_name, raw_value in scope.get("headers", ()):  # type: ignore[assignment]
            try:
                name = bytes(raw_name).decode("latin-1").lower()
                value = bytes(raw_value).decode("latin-1")
            except (TypeError, UnicodeDecodeError):
                continue
            result[name] = value
        return result

    def _secret(self) -> str:
        return _resolve_secret(self.secret_resolver)

    def _trusted_browser(self, scope: Mapping[str, Any]) -> bool:
        if self.secret_resolver is None:
            return False
        raw_cookie = self._headers(scope).get("cookie", "")
        if not raw_cookie:
            return False
        jar = SimpleCookie()
        try:
            jar.load(raw_cookie)
        except Exception:
            return False
        morsel = jar.get(TRUST_COOKIE_NAME)
        if morsel is None:
            return False
        return validate_trusted_approval_cookie(morsel.value, self.secret_resolver)

    @staticmethod
    def _fingerprint(wire_name: str, arguments_sha256: str, action_digest: str) -> str:
        return hashlib.sha256(
            f"{wire_name}\0{arguments_sha256}\0{action_digest}".encode("utf-8")
        ).hexdigest()

    def _prune_locked(self, now: Optional[float] = None) -> None:
        moment = time.time() if now is None else float(now)
        for key, record in tuple(self._records.items()):
            expires = record.get("expires_at")
            if not isinstance(expires, (int, float)) or float(expires) < moment:
                self._records.pop(key, None)

    def ensure_pending(
        self,
        *,
        wire_name: str,
        arguments_sha256: str,
        action_digest: str,
        request_state: str,
        message: str,
        preview: Mapping[str, Any],
    ) -> dict[str, Any]:
        fingerprint = self._fingerprint(wire_name, arguments_sha256, action_digest)
        now = time.time()
        with self._lock:
            self._prune_locked(now)
            record = self._records.get(fingerprint)
            if not isinstance(record, dict):
                record = {
                    "request_state": request_state,
                    "wire_name": wire_name,
                    "arguments_sha256": arguments_sha256,
                    "action_digest": action_digest,
                    "message": str(message)[:1000],
                    "preview": dict(preview),
                    "expires_at": now + self.ttl_seconds,
                    "approved": False,
                    "attempts": 0,
                }
                self._records[fingerprint] = record
            return dict(record)

    def take_if_approved(
        self,
        *,
        wire_name: str,
        arguments_sha256: str,
        action_digest: str,
    ) -> bool:
        fingerprint = self._fingerprint(wire_name, arguments_sha256, action_digest)
        with self._lock:
            self._prune_locked()
            record = self._records.get(fingerprint)
            if not isinstance(record, dict) or record.get("approved") is not True:
                return False
            self._records.pop(fingerprint, None)
            return True

    def approval_url(self, scope: Mapping[str, Any], request_state: str) -> str:
        if self.base_url is not None:
            origin = self.base_url
        else:
            host = self._headers(scope).get("host", "")
            if not host:
                raise ValueError("approval request host is unavailable")
            scheme = str(scope.get("scheme") or "https").lower()
            if scheme not in {"http", "https"}:
                scheme = "https"
            origin = f"{scheme}://{host}"
        return f"{origin}{self.approval_path}?state={quote(request_state, safe='')}"

    def _find_by_state(self, request_state: str) -> tuple[str, dict[str, Any]]:
        if not request_state or len(request_state) > 16_384:
            raise ValueError("approval request state is invalid")
        with self._lock:
            self._prune_locked()
            for fingerprint, record in self._records.items():
                if record.get("request_state") == request_state:
                    return fingerprint, dict(record)
        raise ValueError("approval request state expired or is invalid")

    def _page(
        self,
        record: Mapping[str, Any],
        request_state: str,
        *,
        trusted: bool,
    ) -> str:
        message = html.escape(str(record.get("message") or "Approve this one action?"))
        preview = html.escape(
            json.dumps(record.get("preview") or {}, ensure_ascii=False, sort_keys=True)
        )
        state = html.escape(request_state, quote=True)
        action = html.escape(self.approval_path, quote=True)
        verification = (
            "This browser is already trusted from a previous KaroX OAuth approval. "
            "Confirm this exact action with one click."
            if trusted
            else (
                "Enter the same KaroX approval password used for OAuth. "
                "It is never returned to the MCP client or model."
            )
        )
        password_field = (
            ""
            if trusted
            else (
                '<label>Approval password<input type="password" name="password" required '
                'autocomplete="current-password"></label>'
            )
        )
        return (
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width">'
            '<title>KaroX action approval</title>'
            '<style>body{font:16px system-ui;max-width:44rem;margin:4rem auto;padding:0 1rem;'
            'background:#111;color:#eee}main{border:1px solid #555;border-radius:12px;padding:1.5rem}'
            'input,button{font:inherit;width:100%;box-sizing:border-box;padding:.75rem;margin-top:.75rem}'
            'code{overflow-wrap:anywhere;color:#d9bd7b}small{color:#aaa}</style></head>'
            f'<body><main><h1>Approve one KaroX action</h1><p>{message}</p>'
            f'<p><small>Exact action preview:</small><br><code>{preview}</code></p>'
            f'<p>{verification}</p>'
            f'<form method="post" action="{action}">'
            f'<input type="hidden" name="state" value="{state}">'
            f'{password_field}'
            '<button type="submit">Approve this one action</button></form>'
            '</main></body></html>'
        )

    async def handle(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        """Serve GET/POST for one pending approval. Caller already checked Host/Origin."""
        method = str(scope.get("method") or "").upper()
        if method == "GET":
            try:
                raw_query = bytes(scope.get("query_string", b"")).decode("ascii")
                query = parse_qs(raw_query, keep_blank_values=True)
            except (TypeError, UnicodeDecodeError, ValueError):
                await Response("invalid approval request", status_code=400)(scope, receive, send)
                return
            states = query.get("state", [])
            request_state = states[0] if len(states) == 1 else ""
            try:
                _fingerprint, record = self._find_by_state(request_state)
            except ValueError:
                await Response("approval request expired or invalid", status_code=400)(scope, receive, send)
                return
            await HTMLResponse(
                self._page(
                    record,
                    request_state,
                    trusted=self._trusted_browser(scope),
                ),
                headers={
                    "Cache-Control": "no-store",
                    "Content-Security-Policy": (
                        "default-src 'none'; style-src 'unsafe-inline'; "
                        "form-action 'self'; base-uri 'none'; frame-ancestors 'none'"
                    ),
                    "Referrer-Policy": "no-referrer",
                    "X-Frame-Options": "DENY",
                },
            )(scope, receive, send)
            return

        if method != "POST":
            await Response("method not allowed", status_code=405)(scope, receive, send)
            return

        chunks: list[bytes] = []
        total = 0
        while True:
            event = await receive()
            if event.get("type") == "http.disconnect":
                return
            if event.get("type") != "http.request":
                continue
            chunk = event.get("body", b"")
            if isinstance(chunk, (bytes, bytearray, memoryview)):
                rendered = bytes(chunk)
                total += len(rendered)
                if total > 32 * 1024:
                    await Response("approval form too large", status_code=413)(scope, receive, send)
                    return
                chunks.append(rendered)
            if not bool(event.get("more_body", False)):
                break

        try:
            form = parse_qs(b"".join(chunks).decode("utf-8"), keep_blank_values=True)
        except (UnicodeDecodeError, ValueError):
            await Response("invalid approval form", status_code=400)(scope, receive, send)
            return
        states = form.get("state", [])
        passwords = form.get("password", [])
        request_state = states[0] if len(states) == 1 else ""
        password = passwords[0] if len(passwords) == 1 else ""
        try:
            fingerprint, _record = self._find_by_state(request_state)
        except ValueError:
            await Response("approval request expired or invalid", status_code=400)(scope, receive, send)
            return

        trusted_browser = self._trusted_browser(scope)
        if not trusted_browser:
            try:
                expected = self._secret()
            except Exception:
                await Response("approval secret is unavailable", status_code=503)(scope, receive, send)
                return
            if not hmac.compare_digest(password.encode("utf-8"), expected.encode("utf-8")):
                with self._lock:
                    current = self._records.get(fingerprint)
                    if isinstance(current, dict):
                        current["attempts"] = int(current.get("attempts") or 0) + 1
                        if int(current["attempts"]) >= 5:
                            self._records.pop(fingerprint, None)
                await Response("approval password is incorrect", status_code=403)(scope, receive, send)
                return

        with self._lock:
            current = self._records.get(fingerprint)
            if not isinstance(current, dict) or current.get("request_state") != request_state:
                await Response("approval request expired or invalid", status_code=400)(scope, receive, send)
                return
            current["approved"] = True
            current["approved_at"] = time.time()

        response = HTMLResponse(
            '<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width"><title>KaroX action approved</title>'
            '<style>body{font:16px system-ui;max-width:42rem;margin:4rem auto;padding:0 1rem;'
            'background:#111;color:#eee}main{border:1px solid #555;border-radius:12px;padding:1.5rem}</style>'
            '</head><body><main><h1>Action approved</h1>'
            '<p>Return to ChatGPT. The next exact retry may execute this action once.</p>'
            '</main></body></html>',
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": (
                    "default-src 'none'; style-src 'unsafe-inline'; "
                    "base-uri 'none'; frame-ancestors 'none'"
                ),
                "Referrer-Policy": "no-referrer",
                "X-Frame-Options": "DENY",
            },
        )
        if self.secret_resolver is not None:
            set_trusted_approval_cookie(response, self.secret_resolver)
        await response(scope, receive, send)


__all__ = [
    "BrowserApprovalBroker",
    "SecretResolver",
    "TRUST_COOKIE_MAX_AGE_SECONDS",
    "TRUST_COOKIE_NAME",
    "issue_trusted_approval_cookie",
    "set_trusted_approval_cookie",
    "validate_trusted_approval_cookie",
]
