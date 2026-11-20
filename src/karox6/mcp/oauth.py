"""OAuth 2.1 sign-in for the HTTP MCP server, for web clients that insist on it.

ChatGPT's connector form defaults to OAuth and reads a missing OAuth server as an error,
so the server also speaks the small part of OAuth those clients use: discovery metadata
(RFC 9728 protected resource, RFC 8414 authorization server, under every spelling the
clients probe), dynamic client registration (RFC 7591) and client ID metadata documents,
the authorization code flow with PKCE S256, and refresh-token rotation.

The user approves a sign-in on a KaroX page in the browser by typing the approval code
KaroX shows in its terminal (a new code each start, a few tries per request). Access and
refresh tokens are random; only their SHA-256 is kept, in <home>/mcp/oauth.json, so a
connector keeps working across restarts. The token-in-the-URL access stays available.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

ACCESS_TTL_S = 3600
REFRESH_TTL_S = 90 * 24 * 3600
CODE_TTL_S = 300
PENDING_TTL_S = 900
PIN_TRIES = 5
MAX_CLIENTS = 256
MAX_GRANTS = 2048
METADATA_MAX_BYTES = 8 * 1024
METADATA_TIMEOUT_S = 5.0
PIN_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
SCOPE = "mcp"

DISCOVERY = ("oauth-protected-resource", "oauth-authorization-server", "openid-configuration")

Response = tuple[int, dict[str, str], bytes]


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def _json(status: int, payload: Any, headers: dict[str, str] | None = None) -> Response:
    body = json.dumps(payload).encode()
    return status, {**(headers or {}), "Content-Type": "application/json"}, body


def _error(code: str, text: str, status: int = 400) -> Response:
    return _json(status, {"error": code, "error_description": text})


def new_pin() -> str:
    return "".join(secrets.choice(PIN_ALPHABET) for _ in range(8))


@dataclass
class _Pending:
    client_id: str
    redirect_uri: str
    state: str | None
    challenge: str
    resource: str
    expires: float
    tries: int = 0


@dataclass
class _Code:
    client_id: str
    redirect_uri: str
    challenge: str
    resource: str
    expires: float


@dataclass
class OAuth:
    """The OAuth side of one HTTP MCP server. ``mcp_path`` is the protected endpoint."""

    state_path: Path
    mcp_path: str = "/mcp"
    prefix: str = ""  # the public path this server is published under, e.g. "/karox6"
    pin: str = field(default_factory=new_pin)
    on_event: Any = None  # callable(str): one line per sign-in step, for the log
    _pending: dict[str, _Pending] = field(default_factory=dict)
    _codes: dict[str, _Code] = field(default_factory=dict)
    _data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        try:
            self._data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self._data = {}
        for key in ("clients", "access", "refresh"):
            if not isinstance(self._data.get(key), dict):
                self._data[key] = {}

    # -- routing -------------------------------------------------------------------------

    def handles(self, path: str) -> bool:
        if path.startswith("/oauth/"):
            return True
        return any(name in path for name in DISCOVERY) and ".well-known" in path

    def respond(self, method: str, target: str, headers: dict[str, str], body: bytes) -> Response:
        split = urlsplit(target)
        path = split.path.rstrip("/") or "/"
        base = self.base_url(headers)
        if ".well-known" in path:
            if method != "GET":
                return 405, {"Allow": "GET"}, b""
            if "oauth-protected-resource" in path:
                return _json(200, self.resource_metadata(base))
            return _json(200, self.server_metadata(base))
        if path == "/oauth/register" and method == "POST":
            return self.register(body)
        if path == "/oauth/authorize" and method == "GET":
            return self.authorize_page(parse_qs(split.query), base)
        if path == "/oauth/authorize" and method == "POST":
            return self.approve(parse_qs(body.decode("utf-8", "replace")))
        if path == "/oauth/token" and method == "POST":
            return self.token(parse_qs(body.decode("utf-8", "replace")), base)
        return _json(404, {"error": "not found"})

    def base_url(self, headers: dict[str, str]) -> str:
        # Behind Tailscale Funnel the Host header is the public name (with :8443 / :10000).
        host = headers.get("x-forwarded-host") or headers.get("host") or "localhost"
        local = host.split(":")[0] in ("localhost", "127.0.0.1", "[::1]")
        proto = headers.get("x-forwarded-proto") or ("http" if local else "https")
        return f"{proto}://{host}{self.prefix}"

    def challenge_header(self, headers: dict[str, str]) -> dict[str, str]:
        meta = f"{self.base_url(headers)}/.well-known/oauth-protected-resource{self.mcp_path}"
        return {"WWW-Authenticate": f'Bearer resource_metadata="{meta}"'}

    # -- metadata ------------------------------------------------------------------------

    def resource_metadata(self, base: str) -> dict[str, Any]:
        return {
            "resource": base + self.mcp_path,
            "authorization_servers": [base],
            "scopes_supported": [SCOPE],
            "bearer_methods_supported": ["header"],
            "resource_name": "KaroX",
        }

    def server_metadata(self, base: str) -> dict[str, Any]:
        return {
            "issuer": base,
            "authorization_endpoint": base + "/oauth/authorize",
            "token_endpoint": base + "/oauth/token",
            "registration_endpoint": base + "/oauth/register",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "scopes_supported": [SCOPE],
            "client_id_metadata_document_supported": True,
        }

    # -- registration --------------------------------------------------------------------

    def register(self, body: bytes) -> Response:
        try:
            meta = json.loads(body or b"{}")
        except ValueError:
            return _error("invalid_client_metadata", "the body is not JSON")
        if not isinstance(meta, dict):
            return _error("invalid_client_metadata", "the body is not a JSON object")
        uris = meta.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(map(_redirect_ok, uris)):
            return _error("invalid_redirect_uri", "redirect_uris must be https URLs")
        grants = meta.get("grant_types") or ["authorization_code"]
        if not isinstance(grants, list) or "authorization_code" not in grants:
            return _error("invalid_client_metadata", "the authorization_code grant is required")
        clients = self._data["clients"]
        if len(clients) >= MAX_CLIENTS:
            oldest = min(clients, key=lambda k: clients[k].get("created", 0))
            clients.pop(oldest)
        client_id = "karox-" + secrets.token_urlsafe(16)
        name = str(meta.get("client_name") or "client")[:100]
        clients[client_id] = {"redirect_uris": uris, "name": name, "created": time.time()}
        self._save()
        self._log(f"registered client {name}")
        return _json(
            201,
            {
                "client_id": client_id,
                "client_id_issued_at": int(time.time()),
                "client_name": name,
                "redirect_uris": uris,
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )

    def _client(self, client_id: str) -> dict[str, Any] | None:
        known = self._data["clients"].get(client_id)
        if known is not None:
            return dict(known)
        if client_id.startswith("https://"):
            return _fetch_client_metadata(client_id)
        return None

    # -- authorization -------------------------------------------------------------------

    def authorize_page(self, q: dict[str, list[str]], base: str) -> Response:
        def one(name: str) -> str | None:
            values = q.get(name)
            return values[0] if values else None

        client_id, redirect = one("client_id") or "", one("redirect_uri") or ""
        client = self._client(client_id)
        if client is None:
            return _page(400, "Unknown client", "This client is not registered with KaroX.")
        if redirect not in client.get("redirect_uris", []):
            return _page(400, "Wrong redirect", "The redirect address does not match the client.")
        if one("response_type") != "code" or one("code_challenge_method") != "S256":
            return _page(
                400, "Unsupported request", "KaroX needs response_type=code and PKCE S256."
            )
        challenge = one("code_challenge") or ""
        if not 43 <= len(challenge) <= 128:
            return _page(400, "Unsupported request", "The PKCE code challenge is missing.")
        self._prune()
        request_id = secrets.token_urlsafe(18)
        self._pending[request_id] = _Pending(
            client_id=client_id,
            redirect_uri=redirect,
            state=one("state"),
            challenge=challenge,
            resource=one("resource") or base + self.mcp_path,
            expires=time.time() + PENDING_TTL_S,
        )
        self._log(f"sign-in requested by {client.get('name', 'client')}: waiting for the code")
        return _approval_page(request_id, str(client.get("name") or "client"), "")

    def approve(self, form: dict[str, list[str]]) -> Response:
        request_id = (form.get("request_id") or [""])[0]
        typed = (form.get("code") or [""])[0].strip().upper().replace(" ", "").replace("-", "")
        pending = self._pending.get(request_id)
        if pending is None or pending.expires < time.time():
            return _page(400, "Expired", "This sign-in expired. Start it again from the client.")
        if not secrets.compare_digest(typed.encode(), self.pin.encode()):
            pending.tries += 1
            if pending.tries >= PIN_TRIES:
                self._pending.pop(request_id, None)
                self._log("sign-in refused: too many wrong codes")
                return _page(403, "Refused", "Too many wrong codes. Start the sign-in again.")
            name = str((self._client(pending.client_id) or {}).get("name") or "client")
            return _approval_page(request_id, name, "Wrong code. Check the KaroX terminal.")
        self._pending.pop(request_id, None)
        code = secrets.token_urlsafe(32)
        self._codes[code] = _Code(
            client_id=pending.client_id,
            redirect_uri=pending.redirect_uri,
            challenge=pending.challenge,
            resource=pending.resource,
            expires=time.time() + CODE_TTL_S,
        )
        params = {"code": code}
        if pending.state is not None:
            params["state"] = pending.state
        sep = "&" if "?" in pending.redirect_uri else "?"
        self._log("sign-in approved")
        return 302, {"Location": pending.redirect_uri + sep + urlencode(params)}, b""

    # -- tokens --------------------------------------------------------------------------

    def token(self, form: dict[str, list[str]], base: str) -> Response:
        def one(name: str) -> str:
            values = form.get(name)
            return values[0] if values else ""

        grant = one("grant_type")
        if grant == "authorization_code":
            code = self._codes.pop(one("code"), None)
            if code is None or code.expires < time.time():
                return _error("invalid_grant", "unknown or expired code")
            if one("client_id") and one("client_id") != code.client_id:
                return _error("invalid_grant", "the code belongs to another client")
            if one("redirect_uri") and one("redirect_uri") != code.redirect_uri:
                return _error("invalid_grant", "redirect_uri does not match")
            verifier = one("code_verifier")
            digest = hashlib.sha256(verifier.encode()).digest()
            expected = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
            if not verifier or not secrets.compare_digest(expected, code.challenge):
                return _error("invalid_grant", "PKCE verification failed")
            self._log("access granted")
            return self._issue(code.client_id, code.resource)
        if grant == "refresh_token":
            old = self._data["refresh"].pop(_hash(one("refresh_token")), None)
            if old is None or old.get("expires", 0) < time.time():
                self._save()
                return _error("invalid_grant", "unknown or expired refresh token")
            if one("client_id") and one("client_id") != old.get("client_id"):
                return _error("invalid_grant", "the token belongs to another client")
            return self._issue(str(old.get("client_id")), str(old.get("resource") or base))
        return _error("unsupported_grant_type", f"grant_type {grant!r} is not supported")

    def _issue(self, client_id: str, resource: str) -> Response:
        now = time.time()
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        self._data["access"][_hash(access)] = {
            "client_id": client_id,
            "expires": now + ACCESS_TTL_S,
        }
        self._data["refresh"][_hash(refresh)] = {
            "client_id": client_id,
            "resource": resource,
            "expires": now + REFRESH_TTL_S,
        }
        self._prune()
        self._save()
        return _json(
            200,
            {
                "access_token": access,
                "token_type": "Bearer",
                "expires_in": ACCESS_TTL_S,
                "refresh_token": refresh,
                "scope": SCOPE,
            },
        )

    def valid_access(self, token: str) -> bool:
        entry = self._data["access"].get(_hash(token))
        return bool(entry) and entry.get("expires", 0) > time.time()

    # -- upkeep --------------------------------------------------------------------------

    def _prune(self) -> None:
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if v.expires > now}
        self._codes = {k: v for k, v in self._codes.items() if v.expires > now}
        for key in ("access", "refresh"):
            table = {k: v for k, v in self._data[key].items() if v.get("expires", 0) > now}
            if len(table) > MAX_GRANTS:  # keep the newest
                keep = sorted(table, key=lambda k: table[k].get("expires", 0))[-MAX_GRANTS:]
                table = {k: table[k] for k in keep}
            self._data[key] = table

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_name(self.state_path.name + f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._data, fh)
        os.replace(tmp, self.state_path)

    def _log(self, line: str) -> None:
        if self.on_event is not None:
            self.on_event(line)


def _redirect_ok(uri: object) -> bool:
    if not isinstance(uri, str):
        return False
    parts = urlsplit(uri)
    if parts.scheme == "https" and parts.hostname:
        return not parts.fragment
    return parts.scheme == "http" and parts.hostname in ("localhost", "127.0.0.1")


def _fetch_client_metadata(url: str) -> dict[str, Any] | None:
    """A client ID metadata document: the client_id is an https URL serving its metadata."""
    try:
        with urllib.request.urlopen(url, timeout=METADATA_TIMEOUT_S) as resp:
            raw = resp.read(METADATA_MAX_BYTES + 1)
        if len(raw) > METADATA_MAX_BYTES:
            return None
        meta = json.loads(raw)
    except Exception:
        return None
    if not isinstance(meta, dict) or meta.get("client_id") != url:
        return None
    uris = meta.get("redirect_uris")
    if not isinstance(uris, list) or not all(map(_redirect_ok, uris)):
        return None
    return {"redirect_uris": uris, "name": str(meta.get("client_name") or urlsplit(url).hostname)}


_STYLE = (
    "body{font-family:system-ui,sans-serif;background:#0f1115;color:#e6e6e6;display:flex;"
    "justify-content:center;padding-top:12vh;margin:0}main{background:#181b22;padding:32px 36px;"
    "border-radius:14px;max-width:420px;width:90%;box-shadow:0 8px 30px #0008}"
    "h1{font-size:20px;margin:0 0 8px}p{color:#aab;line-height:1.5}"
    "input{font:600 22px ui-monospace,monospace;letter-spacing:4px;width:100%;"
    "box-sizing:border-box;"
    "padding:10px 12px;border-radius:8px;border:1px solid #333a48;background:#0f1115;color:#fff;"
    "text-transform:uppercase}button{margin-top:16px;width:100%;padding:12px;border:0;"
    "border-radius:8px;background:#4f7cff;color:#fff;font-size:16px;cursor:pointer}"
    ".err{color:#ff7b7b}"
)


def _page(status: int, title: str, text: str) -> Response:
    body = (
        f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>KaroX</title><style>{_STYLE}</style><main><h1>{html.escape(title)}</h1>"
        f"<p>{html.escape(text)}</p></main>"
    )
    return status, {"Content-Type": "text/html; charset=utf-8"}, body.encode()


def _approval_page(request_id: str, client: str, error: str) -> Response:
    err = f"<p class=err>{html.escape(error)}</p>" if error else ""
    body = (
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        f"<title>Connect to KaroX</title><style>{_STYLE}</style><main>"
        f"<h1>Connect {html.escape(client)} to KaroX</h1>"
        "<p>It will be able to read, edit and run commands in your project on this computer. "
        "Type the approval code shown in your KaroX terminal.</p>"
        f"{err}<form method=post action=authorize>"
        f"<input type=hidden name=request_id value='{html.escape(request_id, quote=True)}'>"
        "<input name=code autocomplete=off autofocus placeholder='ABCD-EFGH' maxlength=12>"
        "<button type=submit>Approve</button></form></main>"
    )
    return 200, {"Content-Type": "text/html; charset=utf-8"}, body.encode()
