"""OAuth bridge state codecs, wire models and small helpers."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Any, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit
from .bridge_proxy import (
    normalize_host,
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
