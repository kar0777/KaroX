"""OAuth 2.1 facade for a hosted Streamable HTTP MCP bridge.

The implementation is intentionally narrow: Authorization Code with PKCE S256,
OAuth Client ID Metadata Documents (CIMD), Dynamic Client Registration fallback,
refresh-token rotation, and RFC 9728 protected resource metadata.  It is designed
for web MCP clients such as ChatGPT and Claude, not as a general-purpose identity
provider.
"""

from __future__ import annotations

from .bridge_oauth_app import (  # noqa: F401
    _REQUEST_PROBE_LIMITER as _REQUEST_PROBE_LIMITER,
    _approval_complete_page as _approval_complete_page,
    _approval_language as _approval_language,
    _approval_page as _approval_page,
    _oauth_error as _oauth_error,
    _record_request_probe as _record_request_probe,
    build_oauth_proxy_asgi_app as build_oauth_proxy_asgi_app,
)

from .bridge_oauth_types import (  # noqa: F401
    OAuthBridgeError as OAuthBridgeError,
    _ACCESS_TTL_SECONDS as _ACCESS_TTL_SECONDS,
    _CLIENT_METADATA_TIMEOUT_SECONDS as _CLIENT_METADATA_TIMEOUT_SECONDS,
    _CODE_TTL_SECONDS as _CODE_TTL_SECONDS,
    _Client as _Client,
    _Code as _Code,
    _Grant as _Grant,
    _MAX_BODY_BYTES as _MAX_BODY_BYTES,
    _MAX_CLIENTS as _MAX_CLIENTS,
    _MAX_CLIENT_METADATA_BYTES as _MAX_CLIENT_METADATA_BYTES,
    _PENDING_TTL_SECONDS as _PENDING_TTL_SECONDS,
    _Pending as _Pending,
    _REFRESH_TTL_SECONDS as _REFRESH_TTL_SECONDS,
    _SCOPES as _SCOPES,
    _STATE_VERSION as _STATE_VERSION,
    _client_metadata_url as _client_metadata_url,
    _code_from_state as _code_from_state,
    _digest as _digest,
    _form_action as _form_action,
    _grant_from_state as _grant_from_state,
    _json_object as _json_object,
    _pending_from_state as _pending_from_state,
    _public_origin as _public_origin,
    _redirect_uri as _redirect_uri,
    _scopes as _scopes,
    _single as _single,
    _single_resource as _single_resource,
    _stored_digest as _stored_digest,
    _stored_scopes as _stored_scopes,
    _token as _token,
    _valid_challenge as _valid_challenge,
)

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Optional
from urllib.parse import urlencode, urlsplit


from .bridge_proxy import (
    normalize_host,
)


# A rename loses to any concurrent reader on Windows. Retry briefly, because
# dropping this write is invisible until the next restart and then costs the user
# a re-add of every connector: the registration would have lived only in RAM.
_STATE_REPLACE_TIMEOUT_SECONDS = 5.0
_STATE_REPLACE_INITIAL_DELAY_SECONDS = 0.02
_STATE_REPLACE_MAX_DELAY_SECONDS = 0.25
















































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












