"""OAuth 2.1 facade for a hosted Streamable HTTP MCP bridge.

The implementation is intentionally narrow: Authorization Code with PKCE S256,
OAuth Client ID Metadata Documents (CIMD), Dynamic Client Registration fallback,
refresh-token rotation, and RFC 9728 protected resource metadata.  It is designed
for web MCP clients such as ChatGPT and Claude, not as a general-purpose identity
provider.
"""

from __future__ import annotations

import hashlib
import hmac
import html
import json
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response

from .hosted_bridge import DEFAULT_HOSTED_DEADLINE_SECONDS, HostedToolRuntime
from .proxy_server import (
    build_proxy_asgi_app,
    normalize_host,
    rebinding_rejection,
    resolve_allowed_hosts,
)


_ACCESS_TTL_SECONDS = 3600
_CODE_TTL_SECONDS = 300
_PENDING_TTL_SECONDS = 600
_REFRESH_TTL_SECONDS = 30 * 24 * 3600
_MAX_BODY_BYTES = 65_536
_MAX_CLIENT_METADATA_BYTES = 5 * 1024
_CLIENT_METADATA_TIMEOUT_SECONDS = 5.0
_MAX_CLIENTS = 256
_SCOPES = frozenset({"mcp:tools", "offline_access"})
_STATE_VERSION = 1
# A rename loses to any concurrent reader on Windows. Retry briefly, because
# dropping this write is invisible until the next restart and then costs the user
# a re-add of every connector: the registration would have lived only in RAM.
_STATE_REPLACE_TIMEOUT_SECONDS = 5.0
_STATE_REPLACE_INITIAL_DELAY_SECONDS = 0.02
_STATE_REPLACE_MAX_DELAY_SECONDS = 0.25


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


def _redirect_uri(
    value: object, allowed_hosts: Optional[frozenset[str]] = None
) -> str:
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise OAuthBridgeError("redirect URI is invalid")
    parts = urlsplit(value)
    if parts.username is not None or parts.password is not None or parts.fragment:
        raise OAuthBridgeError("redirect URI is invalid")
    # Wildcards, scheme tricks, and userinfo have to be rejected structurally
    # before the host is ever compared: a registered ``https://*.example/cb`` is an
    # open redirect, and ``javascript:``, ``data:``, and ``file:`` are not web
    # callbacks at all.
    if parts.scheme not in {"https", "http"}:
        raise OAuthBridgeError("redirect URI must use HTTPS (HTTP is allowed for loopback)")
    host = (parts.hostname or "").lower()
    if not host or "*" in host:
        raise OAuthBridgeError("redirect URI host is invalid")
    local = host in {"127.0.0.1", "::1", "localhost"}
    if parts.scheme == "http" and not local:
        raise OAuthBridgeError("redirect URI must use HTTPS (HTTP is allowed for loopback)")
    # A strict profile pins the exact client hosts it will redirect to, so a
    # server registered with an arbitrary HTTPS host cannot collect a KaroX
    # authorization code. Loopback stays open for native clients that run on the
    # same machine as the browser flow. ``None`` keeps the original permissive
    # behaviour for chatgpt-web/claude-web and the library callers behind it.
    if allowed_hosts is not None and not local and host not in allowed_hosts:
        raise OAuthBridgeError("redirect URI host is not allowed for this profile")
    return value


def _client_metadata_url(value: object, allowed_hosts: Optional[frozenset[str]]) -> str:
    """Validate a CIMD client_id URL before any network request is made."""
    if allowed_hosts is None:
        raise OAuthBridgeError("client metadata documents are not enabled for this profile")
    if not isinstance(value, str) or not value or len(value) > 2048:
        raise OAuthBridgeError("client_id metadata URL is invalid")
    parts = urlsplit(value)
    if (
        parts.scheme.lower() != "https"
        or not parts.netloc
        or parts.username is not None
        or parts.password is not None
        or parts.fragment
        or parts.query
        or parts.path in {"", "/"}
        or "/./" in parts.path
        or "/../" in parts.path
    ):
        raise OAuthBridgeError("client_id metadata URL must be a stable HTTPS document URL")
    host = normalize_host(parts.hostname or "")
    if not host or host not in allowed_hosts:
        raise OAuthBridgeError("client_id metadata host is not allowed for this profile")
    return value


def _form_action(form_origin: str) -> str:
    """Allow the approval form to POST only to this KaroX origin.

    The external OAuth callback is intentionally not part of this form submission.
    After approval KaroX returns a separate completion document which performs an
    ordinary browser navigation to the exact registered redirect URI. This avoids
    Chromium applying ``form-action`` to a cross-origin 303 redirect chain.
    """
    return f"'self' {form_origin}"


def _single(values: Mapping[str, list[str]], name: str, *, required: bool = True) -> str:
    items = values.get(name, [])
    if not items and not required:
        return ""
    if len(items) != 1 or not items[0]:
        raise OAuthBridgeError(f"{name} must occur exactly once")
    return items[0]


def _single_resource(values: Mapping[str, list[str]]) -> str:
    items = values.get("resource", [])
    if not items or any(not item for item in items):
        raise OAuthBridgeError("resource must occur at least once")
    unique = tuple(dict.fromkeys(items))
    if len(unique) != 1:
        raise OAuthBridgeError("resource must identify exactly one MCP server")
    return unique[0]


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


def _grant_from_state(
    key: object, entry: object, resource: str, now: float
) -> Optional[_Grant]:
    """Rebuild one stored grant, or ``None`` if it is not exactly what it claims.

    The file is trusted no further than any other input: a hand-edited or
    truncated entry must be dropped, not turned into a grant for a scope or a
    resource nobody issued.
    """
    if not isinstance(key, str) or not key or not isinstance(entry, dict):
        return None
    client_id = entry.get("client_id")
    scopes = entry.get("scopes")
    family = entry.get("family")
    expires_at = entry.get("expires_at")
    if (
        not isinstance(client_id, str)
        or not client_id
        or entry.get("resource") != resource
        or not isinstance(family, str)
        or not family
        or not isinstance(scopes, list)
        or not all(isinstance(item, str) and item in _SCOPES for item in scopes)
        or not isinstance(expires_at, (int, float))
        or expires_at <= now
    ):
        return None
    return _Grant(client_id, resource, tuple(scopes), family, float(expires_at))


def _stored_digest(value: object) -> Optional[str]:
    if not isinstance(value, str) or len(value) != 64:
        return None
    try:
        int(value, 16)
    except ValueError:
        return None
    return value.lower()


def _stored_scopes(value: object) -> Optional[tuple[str, ...]]:
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item in _SCOPES for item in value)
    ):
        return None
    return tuple(value)


def _valid_challenge(value: object) -> bool:
    return bool(
        isinstance(value, str)
        and 43 <= len(value) <= 128
        and all(
            char in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
            for char in value
        )
    )


def _pending_from_state(
    entry: object,
    *,
    resource: str,
    now: float,
    clients: Mapping[str, _Client],
    allowed_redirect_hosts: Optional[frozenset[str]],
) -> Optional[_Pending]:
    if not isinstance(entry, dict):
        return None
    client_id = entry.get("client_id")
    if not isinstance(client_id, str):
        return None
    client = clients.get(client_id)
    if client is None:
        return None
    try:
        redirect_uri = _redirect_uri(
            entry.get("redirect_uri"), allowed_redirect_hosts
        )
    except OAuthBridgeError:
        return None
    state = entry.get("state")
    challenge = entry.get("challenge")
    scopes = _stored_scopes(entry.get("scopes"))
    expires_at = entry.get("expires_at")
    if (
        redirect_uri not in client.redirect_uris
        or not isinstance(state, str)
        or not state
        or not _valid_challenge(challenge)
        or entry.get("resource") != resource
        or scopes is None
        or not isinstance(expires_at, (int, float))
        or expires_at <= now
    ):
        return None
    return _Pending(
        client_id,
        redirect_uri,
        state,
        str(challenge),
        resource,
        scopes,
        float(expires_at),
    )


def _code_from_state(
    entry: object,
    *,
    resource: str,
    now: float,
    clients: Mapping[str, _Client],
    allowed_redirect_hosts: Optional[frozenset[str]],
) -> Optional[_Code]:
    if not isinstance(entry, dict):
        return None
    client_id = entry.get("client_id")
    if not isinstance(client_id, str):
        return None
    client = clients.get(client_id)
    if client is None:
        return None
    try:
        redirect_uri = _redirect_uri(
            entry.get("redirect_uri"), allowed_redirect_hosts
        )
    except OAuthBridgeError:
        return None
    challenge = entry.get("challenge")
    scopes = _stored_scopes(entry.get("scopes"))
    expires_at = entry.get("expires_at")
    if (
        redirect_uri not in client.redirect_uris
        or not _valid_challenge(challenge)
        or entry.get("resource") != resource
        or scopes is None
        or not isinstance(expires_at, (int, float))
        or expires_at <= now
    ):
        return None
    return _Code(
        client_id,
        redirect_uri,
        str(challenge),
        resource,
        scopes,
        float(expires_at),
    )


class OAuthBridgeService:
    """Small in-process OAuth authorization server bound to one MCP resource."""

    def __init__(
        self,
        public_url: str,
        approval_secret: str | Callable[[], str],
        *,
        path: str = "/mcp",
        state_dir: Optional[Path] = None,
        allowed_redirect_hosts: Optional[frozenset[str]] = None,
        allowed_client_metadata_hosts: Optional[frozenset[str]] = None,
        client_metadata_get: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.public_url = _public_origin(public_url)
        if not isinstance(path, str) or not path.startswith("/") or "?" in path:
            raise OAuthBridgeError("OAuth MCP path must be an absolute URL path")
        if not isinstance(approval_secret, str) and not callable(approval_secret):
            raise OAuthBridgeError("OAuth approval secret must be text or a resolver")
        if allowed_redirect_hosts is not None:
            if not isinstance(allowed_redirect_hosts, frozenset):
                raise OAuthBridgeError("allowed redirect hosts must be a frozenset")
            normalized = frozenset(
                normalize_host(item) for item in allowed_redirect_hosts if normalize_host(item)
            )
            if not normalized:
                raise OAuthBridgeError("allowed redirect hosts must not be empty")
            allowed_redirect_hosts = normalized
        self.allowed_redirect_hosts = allowed_redirect_hosts
        if allowed_client_metadata_hosts is not None:
            if not isinstance(allowed_client_metadata_hosts, frozenset):
                raise OAuthBridgeError("allowed client metadata hosts must be a frozenset")
            normalized_client_hosts = frozenset(
                normalize_host(item)
                for item in allowed_client_metadata_hosts
                if normalize_host(item)
            )
            if not normalized_client_hosts:
                raise OAuthBridgeError("allowed client metadata hosts must not be empty")
            allowed_client_metadata_hosts = normalized_client_hosts
        self.allowed_client_metadata_hosts = allowed_client_metadata_hosts
        self._client_metadata_get = client_metadata_get
        self.resource = f"{self.public_url}{path}"
        self.path = path
        self._approval_secret = approval_secret
        self._lock = threading.RLock()
        self._clients: dict[str, _Client] = {}
        self._pending: dict[str, _Pending] = {}
        self._codes: dict[str, _Code] = {}
        # Browser clients may submit an approval form twice. Keep the first
        # redirect briefly so a duplicate POST is idempotent.
        self._approved: dict[str, tuple[str, float]] = {}
        self._access: dict[str, _Grant] = {}
        self._refresh: dict[str, _Grant] = {}
        self._used_refresh: dict[str, tuple[str, float]] = {}
        self.state_path = (
            None
            if state_dir is None
            else Path(state_dir) / f"{_digest(self.resource)[:32]}.json"
        )
        self._load()

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

    def _load(self) -> None:
        """Restore registrations and unexpired access/refresh grants of an earlier run.

        Everything here used to live only in RAM, so restarting the bridge made
        every connector's stored ``client_id`` unknown and every refresh token
        invalid: the user had to delete the connector in ChatGPT or Claude and add
        it again, for a restart. Nothing stored is a bearer secret -- tokens are
        keyed by their SHA-256 digest, exactly as in memory -- so what survives is
        the ability to recognise a token, never the token itself.

        Pending approvals and authorization codes are restored for short-lived browser flows.
        They live for minutes and belong to a browser flow that a restart has
        already interrupted.
        """
        path = self.state_path
        if path is None or not path.exists():
            return
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            # Starting empty costs the user one re-add; refusing to start costs
            # them the bridge. Say which happened, because the re-add is otherwise
            # unexplained -- the launcher mirrors this to the console.
            print(
                f"Warning: KaroX could not read its saved OAuth state ({path}): "
                f"{type(exc).__name__}. Connected clients will have to be added "
                "again.",
                flush=True,
            )
            return
        if not isinstance(payload, dict) or payload.get("version") != _STATE_VERSION:
            return
        # A Quick Tunnel hands out a new host on every start, and a grant minted
        # for one resource must never be honoured for another.
        if payload.get("resource") != self.resource:
            return
        now = time.time()
        clients = payload.get("clients")
        if isinstance(clients, dict):
            for client_id, entry in list(clients.items())[:_MAX_CLIENTS]:
                if not isinstance(client_id, str) or not isinstance(entry, dict):
                    continue
                redirects = entry.get("redirect_uris")
                name = entry.get("client_name")
                created = entry.get("created_at")
                if (
                    not isinstance(redirects, list)
                    or not redirects
                    or not all(isinstance(item, str) for item in redirects)
                    or not isinstance(name, str)
                    or not isinstance(created, int)
                ):
                    continue
                try:
                    parsed = tuple(
                        _redirect_uri(item, self.allowed_redirect_hosts)
                        for item in redirects
                    )
                except OAuthBridgeError:
                    continue
                self._clients[client_id] = _Client(client_id, parsed, name, created)
        pending = payload.get("pending")
        if isinstance(pending, dict):
            for key, entry in pending.items():
                digest = _stored_digest(key)
                item = _pending_from_state(
                    entry,
                    resource=self.resource,
                    now=now,
                    clients=self._clients,
                    allowed_redirect_hosts=self.allowed_redirect_hosts,
                )
                if digest is not None and item is not None:
                    self._pending[digest] = item
        codes = payload.get("codes")
        if isinstance(codes, dict):
            for key, entry in codes.items():
                digest = _stored_digest(key)
                item = _code_from_state(
                    entry,
                    resource=self.resource,
                    now=now,
                    clients=self._clients,
                    allowed_redirect_hosts=self.allowed_redirect_hosts,
                )
                if digest is not None and item is not None:
                    self._codes[digest] = item
        access_grants = payload.get("access")
        if isinstance(access_grants, dict):
            for key, entry in access_grants.items():
                grant = _grant_from_state(key, entry, self.resource, now)
                if grant is not None:
                    self._access[key] = grant
        grants = payload.get("refresh")
        if isinstance(grants, dict):
            for key, entry in grants.items():
                grant = _grant_from_state(key, entry, self.resource, now)
                if grant is not None:
                    self._refresh[key] = grant
        used = payload.get("used_refresh")
        if isinstance(used, dict):
            for key, entry in used.items():
                if (
                    not isinstance(key, str)
                    or not isinstance(entry, list)
                    or len(entry) != 2
                    or not isinstance(entry[0], str)
                    or not isinstance(entry[1], (int, float))
                    or entry[1] <= now
                ):
                    continue
                self._used_refresh[key] = (entry[0], float(entry[1]))

    def _persist(self) -> None:
        """Write the state a restart must not lose, atomically and privately."""
        path = self.state_path
        if path is None:
            return
        with self._lock:
            payload = {
                "version": _STATE_VERSION,
                "resource": self.resource,
                "clients": {
                    client_id: {
                        "redirect_uris": list(client.redirect_uris),
                        "client_name": client.client_name,
                        "created_at": client.created_at,
                    }
                    for client_id, client in self._clients.items()
                },
                "pending": {
                    key: {
                        "client_id": item.client_id,
                        "redirect_uri": item.redirect_uri,
                        "state": item.state,
                        "challenge": item.challenge,
                        "resource": item.resource,
                        "scopes": list(item.scopes),
                        "expires_at": item.expires_at,
                    }
                    for key, item in self._pending.items()
                },
                "codes": {
                    key: {
                        "client_id": item.client_id,
                        "redirect_uri": item.redirect_uri,
                        "challenge": item.challenge,
                        "resource": item.resource,
                        "scopes": list(item.scopes),
                        "expires_at": item.expires_at,
                    }
                    for key, item in self._codes.items()
                },
                "access": {
                    key: {
                        "client_id": grant.client_id,
                        "resource": grant.resource,
                        "scopes": list(grant.scopes),
                        "family": grant.family,
                        "expires_at": grant.expires_at,
                    }
                    for key, grant in self._access.items()
                },
                "refresh": {
                    key: {
                        "client_id": grant.client_id,
                        "resource": grant.resource,
                        "scopes": list(grant.scopes),
                        "family": grant.family,
                        "expires_at": grant.expires_at,
                    }
                    for key, grant in self._refresh.items()
                },
                "used_refresh": {
                    key: [family, expires_at]
                    for key, (family, expires_at) in self._used_refresh.items()
                },
            }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        # Windows time_ns() can repeat across threads at the filesystem clock's
        # coarser resolution. Use cryptographic randomness and O_EXCL so two
        # concurrent registrations can never share a temporary state file.
        temporary = path.with_name(
            f"{path.name}.{os.getpid()}.{secrets.token_hex(16)}.tmp"
        )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            # 0600 before a byte is written: the digests here recognise a live
            # bearer token, so another local account must never read them.
            descriptor = os.open(
                str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            deadline = time.monotonic() + _STATE_REPLACE_TIMEOUT_SECONDS
            delay = _STATE_REPLACE_INITIAL_DELAY_SECONDS
            while True:
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(delay)
                    delay = min(delay * 2.0, _STATE_REPLACE_MAX_DELAY_SECONDS)
        except OSError as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            # A bridge that is serving must not die because its cache could not be
            # written; the cost of the failure is a re-add after the next restart.
            print(
                f"Warning: KaroX could not save its OAuth state ({path}): "
                f"{type(exc).__name__}.",
                flush=True,
            )

    def _prune(self) -> None:
        now = time.time()
        self._pending = {
            key: item for key, item in self._pending.items() if item.expires_at > now
        }
        self._approved = {
            key: item for key, item in self._approved.items() if item[1] > now
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

    def _client_from_metadata(self, client_id: str) -> _Client:
        """Resolve one allowlisted OAuth Client ID Metadata Document."""
        url = _client_metadata_url(client_id, self.allowed_client_metadata_hosts)
        get = self._client_metadata_get
        if get is None:
            import httpx

            get = httpx.get
        try:
            response = get(
                url,
                timeout=_CLIENT_METADATA_TIMEOUT_SECONDS,
                follow_redirects=False,
                headers={"Accept": "application/json"},
            )
        except Exception as exc:
            raise OAuthBridgeError("client metadata document could not be fetched") from exc
        if int(getattr(response, "status_code", 0)) != 200:
            raise OAuthBridgeError("client metadata document did not return 200")
        raw = getattr(response, "content", b"")
        if isinstance(raw, str):
            raw = raw.encode("utf-8")
        if not isinstance(raw, (bytes, bytearray)) or len(raw) > _MAX_CLIENT_METADATA_BYTES:
            raise OAuthBridgeError("client metadata document is too large")
        headers = getattr(response, "headers", {})
        content_type = str(headers.get("content-type", "")).split(";", 1)[0].strip().lower()
        if content_type and content_type != "application/json" and not content_type.endswith("+json"):
            raise OAuthBridgeError("client metadata document must be JSON")
        payload = _json_object(bytes(raw))
        if payload.get("client_id") != url:
            raise OAuthBridgeError("client metadata document client_id does not match its URL")
        client_name = payload.get("client_name")
        if (
            not isinstance(client_name, str)
            or not client_name.strip()
            or len(client_name) > 128
            or any(char in client_name for char in ("\x00", "\r", "\n"))
        ):
            raise OAuthBridgeError("client metadata document client_name is invalid")
        raw_redirects = payload.get("redirect_uris")
        if (
            not isinstance(raw_redirects, list)
            or not raw_redirects
            or len(raw_redirects) > 16
        ):
            raise OAuthBridgeError("client metadata document redirect_uris are invalid")
        redirects = tuple(
            _redirect_uri(item, self.allowed_redirect_hosts) for item in raw_redirects
        )
        if len(set(redirects)) != len(redirects):
            raise OAuthBridgeError("client metadata document redirect_uris must be unique")
        if payload.get("token_endpoint_auth_method", "none") != "none":
            raise OAuthBridgeError("client metadata document must describe a public client")
        grants = payload.get("grant_types", ["authorization_code"])
        if (
            not isinstance(grants, list)
            or "authorization_code" not in grants
            or any(item not in {"authorization_code", "refresh_token"} for item in grants)
        ):
            raise OAuthBridgeError("client metadata document grant type is unsupported")
        responses = payload.get("response_types", ["code"])
        if responses != ["code"]:
            raise OAuthBridgeError("client metadata document response type is unsupported")
        return _Client(url, redirects, client_name.strip(), int(time.time()))

    def protected_resource_metadata(self) -> dict[str, Any]:
        return {
            "resource": self.resource,
            "resource_name": "KaroX MCP",
            "authorization_servers": [self.public_url],
            "bearer_methods_supported": ["header"],
            "scopes_supported": sorted(_SCOPES),
        }

    def authorization_server_metadata(self) -> dict[str, Any]:
        return {
            "issuer": self.public_url,
            "resource": self.resource,
            "resource_metadata": f"{self.public_url}/.well-known/oauth-protected-resource{self.path}",
            "authorization_endpoint": f"{self.public_url}/oauth/authorize",
            "token_endpoint": f"{self.public_url}/oauth/token",
            "registration_endpoint": f"{self.public_url}/oauth/register",
            "response_types_supported": ["code"],
            "response_modes_supported": ["query"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none"],
            "client_id_metadata_document_supported": self.allowed_client_metadata_hosts is not None,
            "scopes_supported": sorted(_SCOPES),
            "resource_indicators_supported": True,
        }

    def register(self, payload: object) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise OAuthBridgeError("client metadata must be a JSON object")
        # Temporary compatibility probe for the live Notion MCP OAuth detector.
        # It records only schema-level metadata and redirect hostnames — never
        # client IDs, authorization codes, tokens, cookies, or raw redirect URLs.
        if self.state_path is not None:
            raw_probe_redirects = payload.get("redirect_uris")
            probe_hosts = []
            if isinstance(raw_probe_redirects, list):
                for raw_uri in raw_probe_redirects[:16]:
                    if isinstance(raw_uri, str):
                        probe_hosts.append(urlsplit(raw_uri).hostname or "")
            probe_redirect_parts = [
                urlsplit(raw_uri)
                for raw_uri in (raw_probe_redirects or [])[:16]
                if isinstance(raw_uri, str)
            ] if isinstance(raw_probe_redirects, list) else []
            raw_client_name = payload.get("client_name")
            probe = {
                "keys": sorted(str(key) for key in payload.keys()),
                "redirect_hosts": sorted(set(probe_hosts)),
                "redirect_paths": [item.path for item in probe_redirect_parts],
                "redirect_has_query": [bool(item.query) for item in probe_redirect_parts],
                "redirect_has_fragment": [bool(item.fragment) for item in probe_redirect_parts],
                "redirect_count": len(raw_probe_redirects) if isinstance(raw_probe_redirects, list) else None,
                "client_name_type": type(raw_client_name).__name__,
                "client_name_length": len(raw_client_name) if isinstance(raw_client_name, str) else None,
                "token_endpoint_auth_method": payload.get("token_endpoint_auth_method"),
                "grant_types": payload.get("grant_types"),
                "response_types": payload.get("response_types"),
                "scope_present": "scope" in payload,
            }
            try:
                self.state_path.with_suffix(".register-probe.json").write_text(
                    json.dumps(probe, indent=2, sort_keys=True), encoding="utf-8"
                )
            except OSError:
                pass
        raw_redirects = payload.get("redirect_uris")
        if (
            not isinstance(raw_redirects, list)
            or not raw_redirects
            or len(raw_redirects) > 16
        ):
            raise OAuthBridgeError("redirect_uris must contain 1-16 URLs")
        redirects = tuple(_redirect_uri(item, self.allowed_redirect_hosts) for item in raw_redirects)
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
        # RFC 7591: the registration metadata states what the client wants; the
        # authorization server decides what it grants. ChatGPT registers public
        # PKCE clients with ``["authorization_code"]`` only, so any subset that
        # includes the code grant is acceptable. The bridge still issues only
        # authorization-code with an optional refresh token, and tokens are
        # never granted beyond what the exchange implements.
        if (
            not isinstance(grants, list)
            or "authorization_code" not in grants
            or any(grant not in ("authorization_code", "refresh_token") for grant in grants)
            or len(set(grants)) != len(grants)
            or responses != ["code"]
        ):
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
            # An MCP client that restarts its OAuth flow re-POSTs the same
            # metadata rather than storing the first client_id. Re-registering a
            # byte-identical client must return the existing client_id, not mint
            # a second one: two registrations for one redirect would otherwise
            # each issue codes the other cannot redeem.
            existing = next(
                (
                    item
                    for item in self._clients.values()
                    if item.redirect_uris == redirects
                    and item.client_name == client.client_name
                ),
                None,
            )
            if existing is not None:
                client = existing
            else:
                self._clients[client.client_id] = client
        self._persist()
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
        redirect_uri = _redirect_uri(
            _single(values, "redirect_uri"), self.allowed_redirect_hosts
        )
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
        resource = _single_resource(values) if values.get("resource") else self.resource
        if resource != self.resource:
            raise OAuthBridgeError("OAuth resource does not match this MCP server")
        scopes = _scopes(_single(values, "scope", required=False))
        metadata_client: Optional[_Client] = None
        if self.allowed_client_metadata_hosts is not None and client_id.startswith("https://"):
            metadata_client = self._client_from_metadata(client_id)
        with self._lock:
            self._prune()
            if metadata_client is not None:
                if client_id not in self._clients and len(self._clients) >= _MAX_CLIENTS:
                    raise OAuthBridgeError("OAuth client registry is full")
                self._clients[client_id] = metadata_client
            client = metadata_client or self._clients.get(client_id)
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
        # Persist before the approval page is returned. If the bridge restarts
        # while the user is typing, the hidden request_id remains valid.
        self._persist()
        return request_id, client

    def approve(self, request_id: str, password: str) -> str:
        if not request_id or not password:
            raise OAuthBridgeError("authorization request and password are required")
        request_key = _digest(request_id)
        with self._lock:
            self._prune()
            pending = self._pending.get(request_key)
            approved = self._approved.get(request_key)
            if pending is None and approved is None:
                raise OAuthBridgeError("authorization request expired or is invalid")
        # compare_digest rejects a non-ASCII str with TypeError, which turned a
        # typed password into an unauthenticated 500 on this open endpoint.
        if not hmac.compare_digest(
            password.encode("utf-8"), self._secret().encode("utf-8")
        ):
            raise OAuthBridgeError("authorization password is incorrect")
        with self._lock:
            self._prune()
            approved = self._approved.get(request_key)
            if approved is not None:
                return approved[0]
            pending = self._pending.pop(request_key, None)
            if pending is None or pending.expires_at <= time.time():
                raise OAuthBridgeError("authorization request expired or is invalid")
            code = _token()
            code_expires_at = time.time() + _CODE_TTL_SECONDS
            self._codes[_digest(code)] = _Code(
                pending.client_id,
                pending.redirect_uri,
                pending.challenge,
                pending.resource,
                pending.scopes,
                code_expires_at,
            )
        separator = "&" if urlsplit(pending.redirect_uri).query else "?"
        location = (
            f"{pending.redirect_uri}{separator}"
            + urlencode({"code": code, "state": pending.state})
        )
        with self._lock:
            self._approved[request_key] = (
                location,
                min(code_expires_at, time.time() + 30.0),
            )
        self._persist()
        return location

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
        # Before the token is handed out, so a crash cannot leave a client holding
        # a refresh token this bridge will not recognise after a restart.
        self._persist()
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
        redirect_uri = _redirect_uri(
            _single(values, "redirect_uri"), self.allowed_redirect_hosts
        )
        verifier = _single(values, "code_verifier")
        resource = _single_resource(values) if values.get("resource") else self.resource
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
            # Do not let a consumed one-time code reappear after a crash.
            self._persist()
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
        resource = _single_resource(values) if values.get("resource") else self.resource
        key = _digest(supplied)
        with self._lock:
            self._prune()
            record = self._refresh.pop(key, None)
            if record is None and self.state_path is not None and self.state_path.exists():
                self._load()
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
                    # The revocation this replay triggered has to outlive the
                    # process too, or a restart would resurrect the family a
                    # stolen token just got killed for.
                    self._persist()
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
        # OAuth access tokens are recognized by their persisted digest grant and
        # do not depend on the approval password after the authorization flow.
        # The old order resolved the approval secret from the OS keyring before
        # consulting this table on *every* MCP request. A transient keyring error
        # could therefore let tools/list pass and turn the immediately following
        # tools/call into a 401; concurrent clients also serialized on the slow
        # credential backend. Keep the direct approval-secret bearer as a legacy
        # fallback, but only for tokens that are not valid OAuth grants.
        key = _digest(token)
        with self._lock:
            self._prune()
            grant = self._access.get(key)
            if not grant and self.state_path is not None and self.state_path.exists():
                self._load()
                self._prune()
                grant = self._access.get(key)
            if grant and grant.resource == self.resource:
                return True
        try:
            if hmac.compare_digest(
                token.encode("utf-8"), self._secret().encode("utf-8")
            ):
                return True
        except (OAuthBridgeError, UnicodeError, OSError, RuntimeError):
            return False
        return False


def _oauth_error(message: str, *, status_code: int = 400) -> JSONResponse:
    return JSONResponse(
        {"error": "invalid_request", "error_description": message},
        status_code=status_code,
        headers={"Cache-Control": "no-store"},
    )


def _approval_language(values: Mapping[str, list[str]]) -> str:
    """Choose one of the approval page's supported languages from OIDC ui_locales."""
    for value in values.get("ui_locales", []):
        for tag in value.split():
            language = tag.split("-", 1)[0].lower()
            if language in {"ru", "en"}:
                return language
    return "en"


def _approval_page(
    service: OAuthBridgeService,
    request_id: str,
    client: _Client,
    *,
    language: str = "en",
) -> str:
    russian = language == "ru"
    title = "Подключение KaroX" if russian else "Authorize KaroX"
    heading = "Разрешить доступ к инструментам KaroX" if russian else "Authorize KaroX tools"
    request_text = (
        "запрашивает доступ к выбранным инструментам на этом компьютере."
        if russian
        else "requests access to the explicitly selected tools on this computer."
    )
    password_label = (
        "Пароль подтверждения из окна KaroX"
        if russian
        else "Approval password from the KaroX window"
    )
    help_text = (
        "Скопируйте строку «OAuth approval password» из окна KaroX и вставьте её сюда. "
        "Не закрывайте KaroX до завершения подключения."
        if russian
        else "Copy the “OAuth approval password” from the KaroX window and paste it here. "
        "Keep KaroX open until the connection finishes."
    )
    button = "Разрешить" if russian else "Authorize"
    return f"""<!doctype html>
<html lang="{language}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>{title}</title>
<style>body{{font:16px system-ui;max-width:42rem;margin:4rem auto;padding:0 1rem;background:#111;color:#eee}}
main{{border:1px solid #555;border-radius:12px;padding:1.5rem}}input,button{{font:inherit;width:100%;box-sizing:border-box;padding:.75rem;margin-top:.75rem}}
code{{overflow-wrap:anywhere;color:#d9bd7b}}small{{color:#aaa}}</style></head>
<body><main><h1>{heading}</h1>
<p><strong>{html.escape(client.client_name)}</strong> {request_text}</p>
<p><small>Redirect: {html.escape(client.redirect_uris[0])}<br>Resource: <code>{html.escape(service.resource)}</code></small></p>
<p>{help_text}</p>
<form method="post" action="/oauth/authorize">
<input type="hidden" name="request_id" value="{html.escape(request_id)}">
<label>{password_label}<input type="password" name="password" required autocomplete="current-password"></label>
<button type="submit">{button}</button></form></main></body></html>"""


def _approval_complete_page(location: str) -> str:
    """Return a new document that navigates to the exact registered callback.

    A 303 directly from the form POST makes Chromium apply the approval page's
    ``form-action`` policy to the whole external redirect chain. A separate 200
    document ends the form submission first; its meta refresh is then an ordinary
    navigation. The visible link is a no-script fallback.
    """
    target = html.escape(location, quote=True)
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<meta http-equiv="refresh" content="0;url={target}"><title>OAuth approved</title>
<style>body{{font:16px system-ui;max-width:42rem;margin:4rem auto;padding:0 1rem;background:#111;color:#eee}}
main{{border:1px solid #555;border-radius:12px;padding:1.5rem}}a{{color:#d9bd7b}}</style></head>
<body><main><h1>Authorization approved</h1><p>Returning to the client…</p>
<p><a href="{target}" rel="noreferrer">Continue</a></p></main></body></html>"""


def build_oauth_proxy_asgi_app(
    proxy: HostedToolRuntime,
    approval_secret: str | Callable[[], str],
    *,
    public_url: str,
    path: str = "/mcp",
    deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
    state_dir: Optional[Path] = None,
    allowed_redirect_hosts: Optional[frozenset[str]] = None,
    allowed_client_metadata_hosts: Optional[frozenset[str]] = None,
) -> Any:
    """Expose an MCP bridge with OAuth discovery, DCR, PKCE, and refresh.

    ``state_dir`` is where registrations and refresh grants survive a restart.
    Omitting it keeps every one of them in RAM, which means a connector added in
    ChatGPT or Claude stops working the moment this process exits.

    ``allowed_redirect_hosts`` pins the exact client hostnames a strict profile
    (such as ``hyperagent-web``) will redirect authorization codes to. ``None``
    keeps the permissive default that admits any HTTPS redirect, which the
    chatgpt-web/claude-web profiles and library callers rely on.
    """

    effective_client_metadata_hosts = allowed_client_metadata_hosts
    if effective_client_metadata_hosts is None and path == "/karox/mcp":
        effective_client_metadata_hosts = allowed_redirect_hosts
    service = OAuthBridgeService(
        public_url,
        approval_secret,
        path=path,
        state_dir=state_dir,
        allowed_redirect_hosts=allowed_redirect_hosts,
        allowed_client_metadata_hosts=effective_client_metadata_hosts,
    )
    metadata_url = (
        f"{service.public_url}/.well-known/oauth-protected-resource{service.path}"
    )
    # This app already validated the public origin, so the MCP wire underneath it
    # need not be told the tunnel host name a second time through the environment.
    public_host = urlsplit(service.public_url).hostname or ""
    allowed = resolve_allowed_hosts((public_host,))
    mcp_app = build_proxy_asgi_app(
        proxy,
        path=path,
        deadline_seconds=deadline_seconds,
        bearer_authorizer=service.authorize_access_token,
        unauthorized_headers={
            "WWW-Authenticate": (
                'Bearer realm="OAuth", '
                f'resource_metadata="{metadata_url}", '
                'error="invalid_token", '
                'error_description="Missing or invalid access token"'
            )
        },
        allowed_hosts=(public_host,),
    )

    async def app(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "lifespan":
            await mcp_app(scope, receive, send)
            return
        if scope.get("type") != "http":
            await Response("not found", status_code=404)(scope, receive, send)
            return
        # The OAuth endpoints are mounted beside /mcp on the same public tunnel,
        # so the rebinding guard has to cover them too: registration and the
        # approval page are reachable without any credential.
        rejection = rebinding_rejection(scope, allowed)
        if rejection is not None:
            await rejection(scope, receive, send)
            return
        request = Request(scope, receive=receive)
        request_path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        # Interoperability trace for the OAuth discovery edge. It records only
        # the HTTP method and URL path: never query strings, headers, bodies,
        # cookies, client IDs, authorization codes, or tokens. Live connector
        # platforms report broad OAuth failures ("does not implement OAuth")
        # with no other server-side evidence, so this probe is the record of
        # what actually reached the bridge.
        if service.state_path is not None:
            try:
                probe_path = service.state_path.with_suffix(".request-probe.jsonl")
                if probe_path.is_file() and probe_path.stat().st_size > 262_144:
                    keep = probe_path.read_text(encoding="utf-8").splitlines()[-512:]
                    probe_path.write_text(
                        "".join(line + "\n" for line in keep), encoding="utf-8"
                    )
                with probe_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps({"method": method, "path": str(request_path)}) + "\n")
            except OSError:
                pass
        try:
            if request_path in {
                "/.well-known/oauth-protected-resource",
                f"/.well-known/oauth-protected-resource{service.path}",
                f"{service.path}/.well-known/oauth-protected-resource",
            } and method == "GET":
                response: Response = JSONResponse(
                    service.protected_resource_metadata(),
                    headers={"Cache-Control": "no-store"},
                )
            elif request_path in {
                "/.well-known/oauth-authorization-server",
                # RFC 8414 path-inserted discovery: several connector platforms
                # probe the authorization-server metadata at the MCP path suffix,
                # and one probe failure reads as "does not implement OAuth".
                f"/.well-known/oauth-authorization-server{service.path}",
                f"{service.path}/.well-known/oauth-authorization-server",
                # OpenID-style alias: HyperAgent's connector discovers the
                # authorization server through this path, and its removal left
                # the hyperagent-web profile with no working discovery route.
                "/.well-known/openid-configuration",
                f"/.well-known/openid-configuration{service.path}",
                f"{service.path}/.well-known/openid-configuration",
            } and method == "GET":
                response = JSONResponse(
                    service.authorization_server_metadata(),
                    headers={"Cache-Control": "no-store"},
                )
            elif request_path in {"/register", "/oauth/register"} and method == "POST":
                if request.headers.get("content-type", "").split(";", 1)[0].strip() != "application/json":
                    raise OAuthBridgeError("client registration must use application/json")
                raw = await _body(request)
                payload = _json_object(raw)
                response = JSONResponse(
                    service.register(payload),
                    status_code=201,
                    headers={"Cache-Control": "no-store"},
                )
            elif request_path in {"/authorize", "/oauth/authorize"} and method == "GET":
                values: dict[str, list[str]] = {}
                for key, value in request.query_params.multi_items():
                    values.setdefault(key, []).append(value)
                request_id, client = service.begin_authorization(values)
                response = HTMLResponse(
                    _approval_page(
                        service,
                        request_id,
                        client,
                        language=_approval_language(values),
                    ),
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Security-Policy": (
                            "default-src 'none'; style-src 'unsafe-inline'; "
                            f"form-action {_form_action(service.public_url)}; "
                            "base-uri 'none'; frame-ancestors 'none'"
                        ),
                        "X-Frame-Options": "DENY",
                        # Chromium may serialize Origin as ``null`` for a basic
                        # form POST under ``no-referrer``. The rebinding guard
                        # then rejects KaroX's own approval form before it can
                        # check the password. ``same-origin`` preserves the
                        # real origin for this POST and still sends no referrer
                        # to ChatGPT/Claude on the cross-origin OAuth redirect.
                        "Referrer-Policy": "same-origin",
                    },
                )
            elif request_path in {"/authorize", "/oauth/authorize"} and method == "POST":
                values = await _form(request)
                location = service.approve(
                    _single(values, "request_id"),
                    _single(values, "password"),
                )
                response = HTMLResponse(
                    _approval_complete_page(location),
                    headers={
                        "Cache-Control": "no-store",
                        "Content-Security-Policy": (
                            "default-src 'none'; style-src 'unsafe-inline'; "
                            "base-uri 'none'; frame-ancestors 'none'"
                        ),
                        "Referrer-Policy": "no-referrer",
                        "Refresh": f"0; url={location}",
                        "X-Frame-Options": "DENY",
                    },
                )
            elif request_path in {"/token", "/oauth/token"} and method == "POST":
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
