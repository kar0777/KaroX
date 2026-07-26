"""OAuth 2.1 facade for a hosted Streamable HTTP MCP bridge.

The implementation is intentionally narrow: Authorization Code with PKCE S256,
Dynamic Client Registration, refresh-token rotation, and RFC 9728 protected
resource metadata.  It is designed for web MCP clients such as ChatGPT and
Claude, not as a general-purpose identity provider.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from .hosted_bridge import HostedToolRuntime
from .proxy_server import build_proxy_asgi_app


_ACCESS_TTL_SECONDS = 3600
_CODE_TTL_SECONDS = 300
_PENDING_TTL_SECONDS = 600
_REFRESH_TTL_SECONDS = 30 * 24 * 3600
_MAX_BODY_BYTES = 65_536
_MAX_CLIENTS = 256
_SCOPES = frozenset({"mcp:tools", "offline_access"})


class OAuthBridgeError(RuntimeError):
    """Invalid or unsupported hosted OAuth configuration."""


def _token() -> str:
    return secrets.token_urlsafe(32)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _public_origin(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise OAuthBridgeError("OAuth public URL is required")
    parts = urlsplit(value.strip())
    if (
        parts.scheme.lower() != "https"
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise OAuthBridgeError(
            "OAuth public URL must be an HTTPS origin without credentials, query, or fragment"
        )
    if parts.path not in {"", "/"}:
        raise OAuthBridgeError("OAuth public URL must be an origin without a path")
    return urlunsplit(("https", parts.netloc, "", "", ""))


def _redirect_uri(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise OAuthBridgeError("redirect URI is invalid")
    parts = urlsplit(value)
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise OAuthBridgeError("redirect URI is invalid")
    host = (parts.hostname or "").lower()
    local = host in {"127.0.0.1", "::1", "localhost"}
    if not parts.netloc or (parts.scheme != "https" and not (parts.scheme == "http" and local)):
        raise OAuthBridgeError("redirect URI must use HTTPS (HTTP is allowed for loopback)")
    return value


def _single(values: Mapping[str, list[str]], name: str, *, required: bool = True) -> str:
    items = values.get(name, [])
    if not items and not required:
        return ""
    if len(items) != 1 or not items[0]:
        raise OAuthBridgeError(f"{name} must occur exactly once")
    return items[0]


def _scopes(value: str) -> tuple[str, ...]:
    requested = tuple(dict.fromkeys(value.split())) if value else ("mcp:tools",)
    if not requested or not set(requested).issubset(_SCOPES):
        raise OAuthBridgeError("requested OAuth scope is not supported")
    return requested


async def _body(request: Request) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > _MAX_BODY_BYTES:
                raise OAuthBridgeError("request body is too large")
        except ValueError as exc:
            raise OAuthBridgeError("content-length is invalid") from exc
    value = await request.body()
    if len(value) > _MAX_BODY_BYTES:
        raise OAuthBridgeError("request body is too large")
    return value


def _json_object(raw: bytes) -> dict[str, Any]:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in items:
            if key in value:
                raise OAuthBridgeError("client metadata contains duplicate fields")
            value[key] = item
        return value

    def constant(value: str) -> None:
        raise OAuthBridgeError(f"client metadata contains invalid constant {value}")

    try:
        decoded = raw.decode("utf-8")
        value = json.loads(
            decoded,
            object_pairs_hook=pairs,
            parse_constant=constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OAuthBridgeError("client metadata is invalid JSON") from exc
    if not isinstance(value, dict):
        raise OAuthBridgeError("client metadata must be a JSON object")
    return value


async def _form(request: Request) -> dict[str, list[str]]:
    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip()
    if content_type != "application/x-www-form-urlencoded":
        raise OAuthBridgeError("request must use application/x-www-form-urlencoded")
    raw = await _body(request)
    try:
        decoded = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise OAuthBridgeError("request body must be UTF-8") from exc
    return parse_qs(decoded, keep_blank_values=True, strict_parsing=True)


@dataclass(frozen=True)
class _Client:
    client_id: str
    redirect_uris: tuple[str, ...]
    client_name: str
    created_at: int


@dataclass(frozen=True)
class _Pending:
    client_id: str
    redirect_uri: str
    state: str
    challenge: str
    resource: str
    scopes: tuple[str, ...]
    expires_at: float


@dataclass(frozen=True)
class _Code:
    client_id: str
    redirect_uri: str
    challenge: str
    resource: str
    scopes: tuple[str, ...]
    expires_at: float


@dataclass(frozen=True)
class _Grant:
    client_id: str
    resource: str
    scopes: tuple[str, ...]
    family: str
    expires_at: float


class OAuthBridgeService:
    """Small in-process OAuth authorization server bound to one MCP resource."""

    def __init__(
        self,
        public_url: str,
        approval_secret: str | Callable[[], str],
        *,
        path: str = "/mcp",
    ) -> None:
        self.public_url = _public_origin(public_url)
        if not isinstance(path, str) or not path.startswith("/") or "?" in path:
            raise OAuthBridgeError("OAuth MCP path must be an absolute URL path")
        if not isinstance(approval_secret, str) and not callable(approval_secret):
            raise OAuthBridgeError("OAuth approval secret must be text or a resolver")
        self.resource = f"{self.public_url}{path}"
        self.path = path
        self._approval_secret = approval_secret
        self._lock = threading.RLock()
        self._clients: dict[str, _Client] = {}
        self._pending: dict[str, _Pending] = {}
        self._codes: dict[str, _Code] = {}
        self._access: dict[str, _Grant] = {}
        self._refresh: dict[str, _Grant] = {}
        self._used_refresh: dict[str, tuple[str, float]] = {}

    def _secret(self) -> str:
        value = (
            self._approval_secret()
            if callable(self._approval_secret)
            else self._approval_secret
        )
        if (
            not isinstance(value, str)
            or not value
            or len(value) > 65_536
            or any(char in value for char in ("\x00", "\r", "\n"))
        ):
            raise OAuthBridgeError("OAuth approval secret is invalid")
        return value

    def _prune(self) -> None:
        now = time.time()
        self._pending = {
            key: item for key, item in self._pending.items() if item.expires_at > now
        }
        self._codes = {
            key: item for key, item in self._codes.items() if item.expires_at > now
        }
        self._access = {
            key: item for key, item in self._access.items() if item.expires_at > now
        }
        self._refresh = {
            key: item for key, item in self._refresh.items() if item.expires_at > now
        }
        self._used_refresh = {
            key: item for key, item in self._used_refresh.items() if item[1] > now
        }

    def protected_resource_metadata(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "authorization_servers": [self.public_url],
            "bearer_methods_supported": ["header"],
            "scopes_supported": sorted(_SCOPES),
        }

    def authorization_server_metadata(self) -> dict[str, Any]:
        return {
            "issuer": self.public_url,
            "authorization_endpoint": f"{self.public_url}/oauth/authorize",
            "token_endpoint": f"{self.public_url}/oauth/token",
            "registration_endpoint": f"{self.public_url}/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": sorted(_SCOPES),
            "resource_indicators_supported": True,
        }

    def register(self, payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise OAuthBridgeError("client metadata must be a JSON object")
        raw_redirects = payload.get("redirect_uris")
        if (
            not isinstance(raw_redirects, list)
            or not raw_redirects
            or len(raw_redirects) > 16
        ):
            raise OAuthBridgeError("redirect_uris must contain 1-16 URLs")
        redirects = tuple(_redirect_uri(item) for item in raw_redirects)
        if len(set(redirects)) != len(redirects):
            raise OAuthBridgeError("redirect_uris must be unique")
        if payload.get("token_endpoint_auth_method", "none") != "none":
            raise OAuthBridgeError("only public PKCE clients are supported")
        if "scope" in payload:
            if not isinstance(payload["scope"], str):
                raise OAuthBridgeError("client scope must be text")
            _scopes(payload["scope"])
        grants = payload.get("grant_types", ["authorization_code", "refresh_token"])
        responses = payload.get("response_types", ["code"])
        if grants != ["authorization_code", "refresh_token"] or responses != ["code"]:
            raise OAuthBridgeError("client grant or response type is unsupported")
        client_name = payload.get("client_name", "Web MCP client")
        if (
            not isinstance(client_name, str)
            or not client_name.strip()
            or len(client_name) > 128
            or any(char in client_name for char in ("\x00", "\r", "\n"))
        ):
            raise OAuthBridgeError("client_name is invalid")
        client = _Client(_token(), redirects, client_name.strip(), int(time.time()))
        with self._lock:
            self._prune()
            if len(self._clients) >= _MAX_CLIENTS:
                raise OAuthBridgeError("dynamic client registry is full")
            self._clients[client.client_id] = client
        return {
            "client_id": client.client_id,
            "client_id_issued_at": client.created_at,
            "client_name": client.client_name,
            "redirect_uris": list(client.redirect_uris),
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
        }

    def begin_authorization(self, values: Mapping[str, list[str]]) -> tuple[str, _Client]:
        client_id = _single(values, "client_id")
        redirect_uri = _redirect_uri(_single(values, "redirect_uri"))
        if _single(values, "response_type") != "code":
            raise OAuthBridgeError("response_type must be code")
        state = _single(values, "state")
        challenge = _single(values, "code_challenge")
        if (
            _single(values, "code_challenge_method") != "S256"
            or not 43 <= len(challenge) <= 128
            or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~" for char in challenge)
        ):
            raise OAuthBridgeError("PKCE S256 code challenge is invalid")
        resource = _single(values, "resource")
        if resource != self.resource:
            raise OAuthBridgeError("OAuth resource does not match this MCP server")
        scopes = _scopes(_single(values, "scope", required=False))
        with self._lock:
            self._prune()
            client = self._clients.get(client_id)
            if client is None or redirect_uri not in client.redirect_uris:
                raise OAuthBridgeError("OAuth client or redirect URI is not registered")
            request_id = _token()
            self._pending[_digest(request_id)] = _Pending(
                client_id,
                redirect_uri,
                state,
                challenge,
                resource,
                scopes,
                time.time() + _PENDING_TTL_SECONDS,
            )
        return request_id, client

    def approve(self, request_id: str, password: str) -> str:
        if not request_id or not password:
            raise OAuthBridgeError("authorization request and password are required")
        with self._lock:
            self._prune()
            pending = self._pending.get(_digest(request_id))
            if pending is None:
                raise OAuthBridgeError("authorization request expired or is invalid")
        if not hmac.compare_digest(password, self._secret()):
            raise OAuthBridgeError("authorization password is incorrect")
        with self._lock:
            pending = self._pending.pop(_digest(request_id), None)
            if pending is None or pending.expires_at <= time.time():
                raise OAuthBridgeError("authorization request expired or is invalid")
            code = _token()
            self._codes[_digest(code)] = _Code(
                pending.client_id,
                pending.redirect_uri,
                pending.challenge,
                pending.resource,
                pending.scopes,
                time.time() + _CODE_TTL_SECONDS,
            )
        separator = "&" if urlsplit(pending.redirect_uri).query else "?"
        return (
            f"{pending.redirect_uri}{separator}"
            + urlencode({"code": code, "state": pending.state})
        )

    @staticmethod
    def _pkce(verifier: str) -> str:
        import base64

        return (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )

    def _issue(self, grant: _Grant) -> dict[str, Any]:
        access = _token()
        refresh = _token()
        with self._lock:
            self._access[_digest(access)] = grant
            self._refresh[_digest(refresh)] = _Grant(
                grant.client_id,
                grant.resource,
                grant.scopes,
                grant.family,
                time.time() + _REFRESH_TTL_SECONDS,
            )
        return {
            "access_token": access,
            "token_type": "Bearer",
            "expires_in": _ACCESS_TTL_SECONDS,
            "refresh_token": refresh,
            "scope": " ".join(grant.scopes),
        }

    def exchange_code(self, values: Mapping[str, list[str]]) -> dict[str, Any]:
        code = _single(values, "code")
        client_id = _single(values, "client_id")
        redirect_uri = _redirect_uri(_single(values, "redirect_uri"))
        verifier = _single(values, "code_verifier")
        resource = _single(values, "resource")
        if not 43 <= len(verifier) <= 128 or not verifier.isascii():
            raise OAuthBridgeError("PKCE code verifier is invalid")
        with self._lock:
            self._prune()
            record = self._codes.get(_digest(code))
            if record is None:
                raise OAuthBridgeError("authorization code is invalid or expired")
            if (
                record.client_id != client_id
                or record.redirect_uri != redirect_uri
                or record.resource != resource
                or not hmac.compare_digest(self._pkce(verifier), record.challenge)
            ):
                raise OAuthBridgeError("authorization code binding is invalid")
            self._codes.pop(_digest(code), None)
        return self._issue(
            _Grant(
                client_id,
                resource,
                record.scopes,
                _token(),
                time.time() + _ACCESS_TTL_SECONDS,
            )
        )

    def refresh(self, values: Mapping[str, list[str]]) -> dict[str, Any]:
        supplied = _single(values, "refresh_token")
        client_id = _single(values, "client_id")
        resource = _single(values, "resource")
        key = _digest(supplied)
        with self._lock:
            self._prune()
            record = self._refresh.pop(key, None)
            if record is None:
                replay = self._used_refresh.get(key)
                if replay is not None:
                    family = replay[0]
                    self._refresh = {
                        token: item
                        for token, item in self._refresh.items()
                        if item.family != family
                    }
                    self._access = {
                        token: item
                        for token, item in self._access.items()
                        if item.family != family
                    }
                raise OAuthBridgeError("refresh token is invalid or was already used")
            if record.client_id != client_id or record.resource != resource:
                self._refresh[key] = record
                raise OAuthBridgeError("refresh token binding is invalid")
            self._used_refresh[key] = (record.family, record.expires_at)
        return self._issue(
            _Grant(
                record.client_id,
                record.resource,
                record.scopes,
                record.family,
                time.time() + _ACCESS_TTL_SECONDS,
            )
        )

    def token(self, values: Mapping[str, list[str]]) -> dict[str, Any]:
        grant_type = _single(values, "grant_type")
        if grant_type == "authorization_code":
            return self.exchange_code(values)
        if grant_type == "refresh_token":
            return self.refresh(values)
        raise OAuthBridgeError("grant_type is unsupported")

    def authorize_access_token(self, token: str) -> bool:
        if not isinstance(token, str) or not token or len(token) > 512:
            return False
        with self._lock:
            self._prune()
            grant = self._access.get(_digest(token))
            return bool(grant and grant.resource == self.resource)


def _oauth_error(message: str, *, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": "invalid_request", "error_description": message},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _approval_page(service: OAuthBridgeService, request_id: str, client: _Client) -> str:
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Authorize KaroX</title>
<style>body{{font:16px system-ui;max-width:42rem;margin:4rem auto;padding:0 1rem;background:#111;color:#eee}}
main{{border:1px solid #555;border-radius:12px;padding:1.5rem}}input,button{{font:inherit;width:100%;box-sizing:border-box;padding:.75rem;margin-top:.75rem}}
code{{overflow-wrap:anywhere;color:#d9bd7b}}small{{color:#aaa}}</style></head>
<body><main><h1>Authorize KaroX tools</h1>
<p><strong>{html.escape(client.client_name)}</strong> requests access to the explicitly selected tools on this computer.</p>
<p><small>Redirect: {html.escape(client.redirect_uris[0])}<br>Resource: <code>{html.escape(service.resource)}</code></small></p>
<form method="post" action="/oauth/authorize">
<input type="hidden" name="request_id" value="{html.escape(request_id)}">
<label>Bridge approval password<input type="password" name="password" required autocomplete="current-password"></label>
<button type="submit">Authorize</button></form></main></body></html>"""


def build_oauth_proxy_asgi_app(
    proxy: HostedToolRuntime,
    approval_secret: str | Callable[[], str],
    *,
    public_url: str,
    path: str = "/mcp",
    deadline_seconds: float = 30.0,
) -> Any:
    """Expose an MCP bridge with OAuth discovery, DCR, PKCE, and refresh."""

    service = OAuthBridgeService(public_url, approval_secret, path=path)
    metadata_url = (
        f"{service.public_url}/.well-known/oauth-protected-resource{service.path}"
    )
    mcp_app = build_proxy_asgi_app(
        proxy,
        path=path,
        deadline_seconds=deadline_seconds,
        bearer_authorizer=service.authorize_access_token,
        unauthorized_headers={
            "WWW-Authenticate": f'Bearer resource_metadata="{metadata_url}"'
        },
    )

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "lifespan":
            await mcp_app(scope, receive, send)
            return
        if scope.get("type") != "http":
            await Response("not found", status_code=404)(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        request_path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        try:
            if request_path in {
                "/.well-known/oauth-protected-resource",
                f"/.well-known/oauth-protected-resource{service.path}",
            } and method == "GET":
                response: Response = JSONResponse(
                    service.protected_resource_metadata(),
                    headers={"Cache-Control": "no-store"},
                )
            elif request_path == "/.well-known/oauth-authorization-server" and method == "GET":
                response = JSONResponse(
                    service.authorization_server_metadata(),
                    headers={"Cache-Control": "no-store"},
                )
            elif request_path == "/oauth/register" and method == "POST":
                if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
                    raise OAuthBridgeError("client registration must use application/json")
                raw = await _body(request)
                payload = _json_object(raw)
                response = JSONResponse(
                    service.register(payload),
                    status_code=201,
                    headers={"Cache-Control": "no-store"},
                )
            elif request_path == "/oauth/authorize" and method == "GET":
                values: dict[str, list[str]] = {}
                for key, value in request.query_params.multi_items():
                    values.setdefault(key, []).append(value)
                request_id, client = service.begin_authorization(values)
                response = HTMLResponse(
                    _approval_page(service, request_id, client),
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'",
                        "X-Frame-Options": "DENY",
                        "Referrer-Policy": "no-referrer",
                    },
                )
            elif request_path == "/oauth/authorize" and method == "POST":
                values = await _form(request)
                location = service.approve(
                    _single(values, "request_id"),
                    _single(values, "password"),
                )
                response = RedirectResponse(location, status_code=303)
            elif request_path == "/oauth/token" and method == "POST":
                response = JSONResponse(
                    service.token(await _form(request)),
                    headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
                )
            else:
                await mcp_app(scope, receive, send)
                return
        except OAuthBridgeError as exc:
            response = _oauth_error(str(exc))
        await response(scope, receive, send)

    app.oauth_service = service
    return app
