"""Session-scoped browser access for hosted MCP clients.

The legacy :mod:`karox.browser_session` manager intentionally remains
localhost-only.  This module adds an explicit policy layer for a second mode:
public HTTPS navigation in an isolated Playwright context, with public-address
validation on every request, tab ownership, user takeover, conservative payment
blocking, and metadata-only network inspection.

Nothing in this module exposes cookies, storage, request headers, response
headers, password values, card data, or authentication tokens.
"""

from __future__ import annotations

import base64
import concurrent.futures
import functools
import ipaddress
import json
import queue
import re
import secrets
import select
import socket
import socketserver
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .artifacts import ArtifactStore
from .browser_bootstrap import BrowserBootstrapError, ensure_playwright_chromium
from .browser_credential_injection import (
    BrowserCredentialInjectionError,
    inject_browser_credential,
)
from .browser_credentials import BrowserCredentialReference, BrowserCredentialStore
from .browser_session import BrowserError, BrowserSecurityError, BrowserSessionError
from .security import redact
from .system_chrome import (
    SystemChromeError,
    SystemChromeLaunch,
    activate_chrome_window,
    launch_system_chrome,
    terminate_chrome_process,
)

_LOCAL_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})
_UNSAFE_SCHEMES = frozenset(
    {
        "about",
        "blob",
        "chrome",
        "chrome-extension",
        "data",
        "file",
        "ftp",
        "javascript",
    }
)
_METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",
        "100.100.100.200",
        "metadata.google.internal",
        "metadata.azure.internal",
    }
)
_SENSITIVE_FIELD = re.compile(
    r"(?i)(authorization|cookie|set-cookie|password|passwd|secret|token|csrf|xsrf|api[_-]?key|session|card|cvv|cvc|pan|iban|private[_-]?key|refresh|access[_-]?token)"
)
_ALLOWED_VALUE_FIELDS = frozenset(
    {
        "model",
        "model_id",
        "provider",
        "usage",
        "credits",
        "plan",
        "trial",
        "subscription",
    }
)
_SAFE_METADATA_KEYS = frozenset(
    {
        "active",
        "amount",
        "balance",
        "billing_period",
        "completion_tokens",
        "currency",
        "ends_at",
        "expires_at",
        "input_tokens",
        "interval",
        "limit",
        "name",
        "output_tokens",
        "price",
        "prompt_tokens",
        "remaining",
        "renewal_at",
        "starts_at",
        "status",
        "total_tokens",
        "used",
    }
)
_PAYMENT_TEXT = re.compile(
    r"(?i)\b(buy|purchase|pay|checkout|subscribe|upgrade|add credits|confirm order|place order|complete purchase|renew|auto.?renew)\b"
)
_FREE_TRIAL_TEXT = re.compile(r"(?i)\b(start|begin|activate|try)\b.{0,30}\bfree trial\b|\bfree trial\b")
_FREE_EVIDENCE = re.compile(
    r"(?i)(\$\s*0(?:\.00)?|€\s*0(?:[,.]00)?|0\s*(?:zł|pln|usd|eur)|no card required|without a card|card not required|bez karty)"
)
_SECRET_INPUT_HINT = re.compile(
    r"(?i)(password|passwd|secret|token|api[_-]?key|credential|cookie|card|cvv|cvc|iban)"
)
_PAYMENT_CREDENTIAL_HINT = re.compile(
    r"(?i)(card|cvv|cvc|iban|\bpan\b|billing|payment)"
)
_HIGH_ENTROPY = re.compile(r"[A-Za-z0-9_\-+/=]{24,}")
_DATA_URI = re.compile(r"(?is)data:[^\s'\"<>]{64,}")
_LONG_BLOB = re.compile(r"[A-Za-z0-9+/=_-]{200,}")
_MAX_NETWORK_ENTRIES = 400
_MAX_CONSOLE_ENTRIES = 200
_MAX_CONSOLE_TEXT_CHARS = 1_200
_MAX_TEXT_CHARS = 20_000
_MAX_SELECTOR_LEN = 2_000
_MAX_VALUE_LEN = 100_000
_MAX_JSON_BYTES = 1_000_000
_CONNECT_ATTEMPT_TIMEOUT_SECONDS = 3.0
_CONNECT_TOTAL_TIMEOUT_SECONDS = 10.0
_PLAYWRIGHT_MISSING = (
    "browser automation is part of KaroX v5, but the Playwright runtime is unavailable; "
    "repair or reinstall the KaroX package"
)


def _canonical_domain(value: str) -> str:
    raw = str(value or "").strip().lower().rstrip(".")
    if raw.startswith("*."):
        raw = raw[2:]
    if not raw or "/" in raw or ":" in raw or "@" in raw:
        raise ValueError("browser domain entries must be host names")
    try:
        return raw.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("browser domain entry is invalid") from exc


@dataclass(frozen=True)
class BrowserAccessPolicy:
    """Explicit, secret-free browser permissions for one KaroX session."""

    session_id: str
    localhost: bool = True
    external_https: bool = False
    allowed_domains: tuple[str, ...] = ()
    denied_domains: tuple[str, ...] = ()
    headed: bool = False
    user_takeover: bool = False
    network_inspection: bool = False
    payment_confirmation: bool = False
    allowed_emails: tuple[str, ...] = ()
    allowed_credential_refs: tuple[str, ...] = ()
    backend: str = "playwright"
    startup_url: str = "about:blank"
    # Secret-free durable identity of the saved bridge profile that owns this
    # browser. "ad-hoc" is the explicit fallback for non-saved bridge runs.
    # Extension hello verification binds this value together with session_id,
    # browser/bridge instance IDs, launch nonce and the proven Chrome process.
    saved_profile_id: str = "ad-hoc"

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("browser policy requires a session_id")
        if (
            not isinstance(self.saved_profile_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.saved_profile_id) is None
        ):
            raise ValueError("browser saved_profile_id must be a safe 1-128 character identifier")
        for field_name in (
            "localhost",
            "external_https",
            "headed",
            "user_takeover",
            "network_inspection",
            "payment_confirmation",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"browser policy {field_name} must be boolean")
        if self.backend not in {"playwright", "extension"}:
            raise ValueError("browser backend must be playwright or extension")
        if self.backend == "extension" and not (self.headed and self.user_takeover):
            raise ValueError("extension browser backend requires headed user takeover")
        allowed = tuple(dict.fromkeys(_canonical_domain(item) for item in self.allowed_domains))
        denied = tuple(dict.fromkeys(_canonical_domain(item) for item in self.denied_domains))
        if set(allowed).intersection(denied):
            raise ValueError("a browser domain cannot be both allowed and denied")
        emails: list[str] = []
        for item in self.allowed_emails:
            value = str(item or "").strip().lower()
            if not value or "@" not in value or len(value) > 320:
                raise ValueError("browser allowed email is invalid")
            emails.append(value)
        credential_refs: list[str] = []
        for item in self.allowed_credential_refs:
            try:
                credential_refs.append(str(BrowserCredentialReference.parse(str(item))))
            except ValueError as exc:
                raise ValueError("browser allowed credential reference is invalid") from exc
        object.__setattr__(self, "allowed_domains", allowed)
        object.__setattr__(self, "denied_domains", denied)
        object.__setattr__(self, "allowed_emails", tuple(dict.fromkeys(emails)))
        object.__setattr__(
            self,
            "allowed_credential_refs",
            tuple(dict.fromkeys(credential_refs)),
        )
        if self.user_takeover and not self.headed:
            raise ValueError("browser user takeover requires headed mode")
        if self.allowed_domains and not self.external_https:
            raise ValueError("browser allowed domains require external HTTPS permission")
        if self.network_inspection and not (self.localhost or self.external_https):
            raise ValueError("browser network inspection requires browser navigation")

    def to_diagnostics(self) -> dict[str, Any]:
        return {
            "read": True,
            "input": True,
            "localhost": self.localhost,
            "external_https": self.external_https,
            "user_takeover": self.user_takeover,
            "network_inspection": self.network_inspection,
            "payment_confirmation": self.payment_confirmation,
            "headed": self.headed,
            "allowed_domains": list(self.allowed_domains),
            "denied_domains": list(self.denied_domains),
            "allowed_email_count": len(self.allowed_emails),
            "allowed_credential_refs": list(self.allowed_credential_refs),
            "allowed_credential_count": len(self.allowed_credential_refs),
            "backend": self.backend,
            "saved_profile_id": self.saved_profile_id,
            "dns_pinning_proxy": self.backend == "playwright",
            "proxy_authentication": "per_session",
            "proxy_port": "random_loopback",
            "service_workers": "blocked",
            "downloads": "cancelled",
        }


def _domain_matches(host: str, domains: Sequence[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _resolved_addresses(host: str, port: int) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
    try:
        direct = ipaddress.ip_address(host)
    except ValueError:
        direct = None
    if direct is not None:
        return (direct,)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise BrowserSecurityError("browser host did not resolve") from exc
    values: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for info in infos:
        try:
            address = ipaddress.ip_address(info[4][0])
        except ValueError:
            continue
        if address not in values:
            values.append(address)
    if not values:
        raise BrowserSecurityError("browser host resolved to no usable addresses")
    return tuple(values)


def _is_public_address(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def validate_browser_url(value: Any, policy: BrowserAccessPolicy) -> str:
    """Validate one navigation/request URL against the session policy.

    Validation is repeated for every Playwright route and after navigation.  A
    public host that changes DNS to a private address, or redirects to one, is
    therefore refused at the point it would cross the boundary.
    """

    if not isinstance(value, str) or not value or len(value) > 4096:
        raise BrowserSecurityError("url must be a non-empty string")
    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme in _UNSAFE_SCHEMES or scheme not in {"http", "https"}:
        raise BrowserSecurityError("blocked URL scheme")
    if parsed.username is not None or parsed.password is not None:
        raise BrowserSecurityError("URLs with embedded credentials are not allowed")
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        raise BrowserSecurityError("browser URL has no host")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise BrowserSecurityError("browser URL host is invalid") from exc
    if host in _METADATA_HOSTS:
        raise BrowserSecurityError("cloud metadata endpoints are blocked")
    port = parsed.port or (443 if scheme == "https" else 80)

    if host in _LOCAL_HOSTS:
        if not policy.localhost:
            raise BrowserSecurityError("localhost browser access is not enabled")
        addresses = _resolved_addresses(host, port)
        if not all(address.is_loopback for address in addresses):
            raise BrowserSecurityError("localhost URL resolved to a non-loopback address")
        return value

    if scheme != "https":
        raise BrowserSecurityError("external browser access requires HTTPS")
    if not policy.external_https:
        raise BrowserSecurityError("browser tools are limited to localhost URLs")
    if _domain_matches(host, policy.denied_domains):
        raise BrowserSecurityError("browser domain is denied for this session")
    if policy.allowed_domains and not _domain_matches(host, policy.allowed_domains):
        raise BrowserSecurityError("browser domain is not in the session allowlist")
    addresses = _resolved_addresses(host, port)
    if not all(_is_public_address(address) for address in addresses):
        raise BrowserSecurityError("browser target resolved to a private or reserved address")
    return value


def _is_local_browser_url(value: str) -> bool:
    try:
        return (urlsplit(value).hostname or "").lower().rstrip(".") in _LOCAL_HOSTS
    except Exception:
        return False


def _same_navigation_target(left: str, right: str) -> bool:
    def normalized(value: str) -> tuple[str, str, int, str, str]:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port or (443 if scheme == "https" else 80)
        path = parsed.path or "/"
        return scheme, host, port, path, parsed.query

    try:
        return normalized(left) == normalized(right)
    except Exception:
        return False


def _proxy_target_url(scheme: str, host: str, port: int) -> str:
    literal = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{literal}:{port}/"


def _connect_to_pinned_address(
    scheme: str,
    host: str,
    port: int,
    policy: BrowserAccessPolicy,
) -> socket.socket:
    """Resolve, validate, then connect to the validated IP literal.

    The browser never performs the final external DNS lookup: it talks to a
    per-session loopback proxy, and this function chooses the concrete address
    that the upstream socket uses. That closes the DNS-rebinding gap between a
    policy lookup and Chromium's own later resolver lookup.
    """

    validate_browser_url(_proxy_target_url(scheme, host, port), policy)
    addresses = _resolved_addresses(host, port)
    local = host.lower().rstrip(".") in _LOCAL_HOSTS
    usable = [
        address
        for address in addresses
        if (address.is_loopback if local else _is_public_address(address))
    ]
    if not usable or len(usable) != len(addresses):
        raise BrowserSecurityError(
            "browser proxy refused a private, reserved, or rebound address"
        )
    # Windows machines commonly resolve Google and other large CDNs to IPv6
    # first even when the local network has no working IPv6 route. Spending the
    # old 15-second timeout on each unusable address made a single page resource
    # take roughly 30 seconds and left OAuth forms apparently loading forever.
    # Prefer IPv4, cap each attempt, and also cap the whole address set.
    ordered = sorted(
        usable,
        key=lambda address: 0 if isinstance(address, ipaddress.IPv4Address) else 1,
    )
    deadline = time.monotonic() + _CONNECT_TOTAL_TIMEOUT_SECONDS
    last: Optional[OSError] = None
    for address in ordered:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        timeout = max(0.25, min(_CONNECT_ATTEMPT_TIMEOUT_SECONDS, remaining))
        try:
            return socket.create_connection((str(address), port), timeout=timeout)
        except OSError as exc:
            last = exc
    raise BrowserSecurityError("browser proxy could not connect to the validated target") from last


class _PolicyProxyServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = False
    daemon_threads = True


class SessionPinnedProxy:
    """Authenticated per-session HTTP CONNECT proxy with DNS/IP pinning."""

    MAX_HEADER_BYTES = 65_536

    def __init__(self, policy: BrowserAccessPolicy) -> None:
        self.policy = policy
        self.username = "karox"
        self.password = secrets.token_urlsafe(32)
        self._expected_authorization = "Basic " + base64.b64encode(
            f"{self.username}:{self.password}".encode("utf-8")
        ).decode("ascii")
        owner = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                owner._handle_client(self.request)

        self._server = _PolicyProxyServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name=f"karox-browser-proxy-{policy.session_id[:24]}",
            daemon=True,
        )
        self._started = False

    @property
    def server_url(self) -> str:
        # ``server_address`` is typed as a generic address tuple, so the host
        # can be bytes for some families. Decode rather than interpolate, or a
        # loopback URL renders as b'127.0.0.1' and no client can use it.
        host, port = self._server.server_address[:2]
        if isinstance(host, (bytes, bytearray)):
            host = host.decode("ascii", "replace")
        return f"http://{host}:{port}"

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._thread.start()

    def stop(self) -> None:
        if not self._started:
            self._server.server_close()
            return
        self._started = False
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=3.0)

    @staticmethod
    def _send(client: socket.socket, status: int, reason: str, headers: bytes = b"") -> None:
        body = b"" if status == 200 else reason.encode("ascii", errors="replace")
        response = (
            f"HTTP/1.1 {status} {reason}\r\n".encode("ascii")
            + headers
            + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode("ascii")
            + body
        )
        try:
            client.sendall(response)
        except OSError:
            pass

    def _read_head(self, client: socket.socket) -> tuple[bytes, bytes]:
        data = bytearray()
        while b"\r\n\r\n" not in data:
            chunk = client.recv(4096)
            if not chunk:
                raise BrowserSecurityError("browser proxy received an incomplete request")
            data.extend(chunk)
            if len(data) > self.MAX_HEADER_BYTES:
                raise BrowserSecurityError("browser proxy request headers are too large")
        head, remainder = bytes(data).split(b"\r\n\r\n", 1)
        return head, remainder

    @staticmethod
    def _parse_headers(lines: list[bytes]) -> tuple[list[tuple[str, str]], dict[str, str]]:
        ordered: list[tuple[str, str]] = []
        lookup: dict[str, str] = {}
        for raw in lines:
            if b":" not in raw:
                raise BrowserSecurityError("browser proxy received a malformed header")
            raw_name, raw_value = raw.split(b":", 1)
            name = raw_name.decode("latin-1").strip()
            value = raw_value.decode("latin-1").strip()
            lowered = name.lower()
            lookup[lowered] = value
            ordered.append((name, value))
        return ordered, lookup

    def _authorized(self, headers: Mapping[str, str]) -> bool:
        supplied = headers.get("proxy-authorization", "")
        return secrets.compare_digest(supplied, self._expected_authorization)

    @staticmethod
    def _connect_target(target: str) -> tuple[str, int]:
        parsed = urlsplit("//" + target)
        host = parsed.hostname or ""
        if not host:
            raise BrowserSecurityError("browser proxy CONNECT target has no host")
        try:
            port = parsed.port or 443
        except ValueError as exc:
            raise BrowserSecurityError("browser proxy CONNECT target has an invalid port") from exc
        if not 1 <= port <= 65_535:
            raise BrowserSecurityError("browser proxy CONNECT port is invalid")
        return host, port

    @staticmethod
    def _relay(left: socket.socket, right: socket.socket) -> None:
        left.settimeout(None)
        right.settimeout(None)
        sockets = (left, right)
        while True:
            try:
                readable, _, exceptional = select.select(sockets, (), sockets, 30.0)
            except (OSError, ValueError):
                return
            if exceptional:
                return
            if not readable:
                # Idle authenticated tunnels remain valid across user takeover
                # and resume. Closing this session's browser/context closes the
                # sockets; inactivity alone is not a security failure.
                continue
            for source in readable:
                destination = right if source is left else left
                try:
                    chunk = source.recv(65_536)
                    if not chunk:
                        return
                    destination.sendall(chunk)
                except OSError:
                    return

    def _handle_connect(self, client: socket.socket, target: str) -> None:
        host, port = self._connect_target(target)
        upstream = _connect_to_pinned_address("https", host, port, self.policy)
        try:
            # CONNECT has no response body; the tunnel begins immediately after
            # the blank line. Avoid the generic helper's Connection: close.
            client.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            self._relay(client, upstream)
        finally:
            upstream.close()

    def _handle_http(
        self,
        client: socket.socket,
        method: str,
        target: str,
        version: str,
        ordered_headers: list[tuple[str, str]],
        lookup: Mapping[str, str],
        remainder: bytes,
    ) -> None:
        parsed = urlsplit(target)
        if not parsed.scheme:
            host_header = lookup.get("host", "")
            parsed = urlsplit("http://" + host_header + target)
        if parsed.scheme.lower() != "http":
            raise BrowserSecurityError("browser proxy accepts plain requests only for HTTP localhost")
        validated = validate_browser_url(parsed.geturl(), self.policy)
        parsed = urlsplit(validated)
        host = parsed.hostname or ""
        try:
            port = parsed.port or 80
        except ValueError as exc:
            raise BrowserSecurityError("browser proxy HTTP target has an invalid port") from exc
        upstream = _connect_to_pinned_address("http", host, port, self.policy)
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query
        forwarded = [f"{method} {path} {version}\r\n".encode("latin-1")]
        for name, value in ordered_headers:
            if name.lower() in {"proxy-authorization", "proxy-connection"}:
                continue
            forwarded.append(f"{name}: {value}\r\n".encode("latin-1"))
        forwarded.append(b"\r\n")
        try:
            upstream.sendall(b"".join(forwarded) + remainder)
            self._relay(client, upstream)
        finally:
            upstream.close()

    def _handle_client(self, client: socket.socket) -> None:
        client.settimeout(15.0)
        try:
            head, remainder = self._read_head(client)
            lines = head.split(b"\r\n")
            first = lines[0].decode("latin-1")
            parts = first.split(" ", 2)
            if len(parts) != 3:
                raise BrowserSecurityError("browser proxy received a malformed request line")
            method, target, version = parts
            ordered, headers = self._parse_headers(lines[1:])
            if not self._authorized(headers):
                self._send(
                    client,
                    407,
                    "Proxy Authentication Required",
                    b'Proxy-Authenticate: Basic realm="KaroX browser session"\r\n',
                )
                return
            if method.upper() == "CONNECT":
                self._handle_connect(client, target)
                return
            self._handle_http(
                client,
                method,
                target,
                version,
                ordered,
                headers,
                remainder,
            )
        except BrowserSecurityError:
            self._send(client, 403, "Forbidden")
        except (OSError, UnicodeError, ValueError):
            self._send(client, 502, "Bad Gateway")
        finally:
            try:
                client.close()
            except OSError:
                pass


def _safe_network_url(value: str) -> str:
    """Return a URL with query values and token-like path segments removed."""
    try:
        parsed = urlsplit(value)
    except Exception:
        return ""
    safe_segments = []
    for segment in parsed.path.split("/"):
        safe_segments.append("[REDACTED]" if _HIGH_ENTROPY.fullmatch(segment or "") else segment)
    query_names = []
    for name, _value in parse_qsl(parsed.query, keep_blank_values=True):
        safe_name = "[REDACTED]" if _SENSITIVE_FIELD.search(name) else name[:100]
        query_names.append((safe_name, "[REDACTED]"))
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, "/".join(safe_segments), urlencode(query_names), ""))


def _safe_field_names(value: Any, *, limit: int = 200) -> list[str]:
    names: list[str] = []

    def visit(item: Any) -> None:
        if len(names) >= limit:
            return
        if isinstance(item, Mapping):
            for key, child in item.items():
                name = str(key)[:120]
                if not _SENSITIVE_FIELD.search(name) and name not in names:
                    names.append(name)
                if not _SENSITIVE_FIELD.search(name):
                    visit(child)
        elif isinstance(item, list):
            for child in item[:20]:
                visit(child)

    visit(value)
    return names


def _sanitize_scalar(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if len(value) > 300 or _HIGH_ENTROPY.fullmatch(value):
            return "[REDACTED]"
        return str(redact(value))
    return str(redact(str(value)))[:300]


def _sanitize_allowed_value(value: Any, depth: int = 0) -> Any:
    if depth > 4:
        return "[TRUNCATED]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in list(value.items())[:50]:
            name = str(key).lower()[:120]
            if _SENSITIVE_FIELD.search(name) or name not in _SAFE_METADATA_KEYS:
                continue
            result[name] = _sanitize_allowed_value(child, depth + 1)
        return result
    if isinstance(value, list):
        return [_sanitize_allowed_value(item, depth + 1) for item in value[:50]]
    return _sanitize_scalar(value)


def _allowed_json_values(value: Any) -> dict[str, Any]:
    found: dict[str, Any] = {}

    def visit(item: Any) -> None:
        if isinstance(item, Mapping):
            for key, child in item.items():
                name = str(key).lower()
                if _SENSITIVE_FIELD.search(name):
                    continue
                if name in _ALLOWED_VALUE_FIELDS and name not in found:
                    found[name] = _sanitize_allowed_value(child)
                visit(child)
        elif isinstance(item, list):
            for child in item[:50]:
                visit(child)

    visit(value)
    return found


def _safe_console_text(value: Any) -> str:
    """Redact secrets and collapse embedded data/base64 blobs before storage."""
    text = str(redact(str(value)))
    text = _DATA_URI.sub("data:[REDACTED]", text)
    text = _LONG_BLOB.sub("[REDACTED_BLOB]", text)
    return text[:_MAX_CONSOLE_TEXT_CHARS]


@dataclass
class _BrowserCommand:
    callback: Any
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    future: concurrent.futures.Future[Any]


_AUTO_RECOVER_BROWSER_METHODS = frozenset(
    {
        "tabs",
        "request_user_takeover",
        "click",
        "fill",
        "select",
        "press",
        "wait_for",
        "get_text",
        "screenshot",
        "snapshot",
    }
)


def _deadline_from_browser_call(args: tuple[Any, ...], kwargs: Mapping[str, Any]) -> float:
    value = kwargs.get("deadline_seconds")
    if isinstance(value, (int, float)):
        return max(1.0, float(value))
    for item in reversed(args):
        if isinstance(item, (int, float)):
            return max(1.0, float(item))
    return 15.0


def _on_browser_owner_thread(method: Any) -> Any:
    """Serialize Playwright calls and heal a late about:blank before page work."""

    @functools.wraps(method)
    def wrapped(self: "SecureBrowserSessionManager", *args: Any, **kwargs: Any) -> Any:
        # Chromium may switch the page to about:blank a moment *after* takeover
        # resume/reconnect returned. Recover immediately before every operation
        # that needs the active page, not only inside resume/tabs. The recovery
        # itself runs through the same owner queue, so it cannot race the command
        # that follows or cross Playwright's thread boundary.
        if (
            method.__name__ in _AUTO_RECOVER_BROWSER_METHODS
            and self.is_open
            and not self.takeover_active
        ):
            self.recover_if_blank(_deadline_from_browser_call(args, kwargs))
        return self._call_on_owner(method, *args, **kwargs)

    return wrapped


@dataclass
class _ConsoleEntry:
    type: str
    text: str
    location: str
    tab_id: str


@dataclass
class _BrowserHandle:
    playwright_ctx: Any
    browser: Any
    context: Any
    context_id: str = field(default_factory=lambda: f"ctx-{uuid.uuid4().hex[:16]}")
    proxy: Optional[SessionPinnedProxy] = None
    tabs: dict[str, Any] = field(default_factory=dict)
    active_tab_id: str = ""
    console: list[_ConsoleEntry] = field(default_factory=list)
    network: list[dict[str, Any]] = field(default_factory=list)
    request_started: dict[int, float] = field(default_factory=dict)
    last_safe_urls: dict[str, str] = field(default_factory=dict)
    takeover_active: bool = False
    started_at: float = field(default_factory=time.time)
    engine: str = "playwright_chromium"
    system_chrome: Optional[SystemChromeLaunch] = None
    branding_tab_id: Optional[str] = None


class SecureBrowserSessionManager:
    """Own one Playwright browser/context for exactly one KaroX session."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        policy: BrowserAccessPolicy,
        credential_store: Optional[BrowserCredentialStore] = None,
    ) -> None:
        if artifacts.session_id != policy.session_id:
            raise ValueError("browser policy and artifact store session IDs differ")
        self._artifacts = artifacts
        self.policy = policy
        self._credential_store = credential_store or BrowserCredentialStore()
        self._handle: Optional[_BrowserHandle] = None
        self._explicit_navigation_target: Optional[str] = None

        # Playwright's synchronous API is greenlet-based and every object it
        # creates is bound to the thread that started sync_playwright(). Hosted
        # MCP requests may arrive on different ASGI worker threads, so a normal
        # lock is insufficient: even perfectly serialized calls still fail when
        # the next caller owns a different thread. Marshal every Playwright call
        # to this one permanent, session-owned worker instead.
        self._owner_queue: queue.Queue[_BrowserCommand] = queue.Queue()
        self._owner_ready = threading.Event()
        self._owner_thread_id: Optional[int] = None
        self._owner_thread = threading.Thread(
            target=self._owner_loop,
            name=f"karox-browser-owner-{policy.session_id[:24]}",
            daemon=True,
        )
        self._owner_thread.start()
        if not self._owner_ready.wait(timeout=5.0):
            raise BrowserSessionError("browser owner thread did not start")

    def _owner_loop(self) -> None:
        self._owner_thread_id = threading.get_ident()
        self._owner_ready.set()
        while True:
            command = self._owner_queue.get()
            try:
                if not command.future.set_running_or_notify_cancel():
                    continue
                try:
                    result = command.callback(self, *command.args, **command.kwargs)
                except BaseException as exc:
                    command.future.set_exception(exc)
                else:
                    command.future.set_result(result)
            finally:
                self._owner_queue.task_done()

    def _call_on_owner(self, callback: Any, *args: Any, **kwargs: Any) -> Any:
        if threading.get_ident() == self._owner_thread_id:
            return callback(self, *args, **kwargs)
        if not self._owner_thread.is_alive():
            raise BrowserSessionError("browser owner thread is not running")
        future: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self._owner_queue.put(
            _BrowserCommand(
                callback=callback,
                args=tuple(args),
                kwargs=dict(kwargs),
                future=future,
            )
        )
        return future.result()

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    @property
    def takeover_active(self) -> bool:
        return bool(self._handle and self._handle.takeover_active)

    @property
    def context_id(self) -> Optional[str]:
        """Stable identifier for the live context, safe to expose in diagnostics."""
        return self._handle.context_id if self._handle is not None else None

    def _ensure_open(self) -> _BrowserHandle:
        if self._handle is None:
            raise BrowserSessionError("no browser session is open; call karox.browser.open first")
        return self._handle

    def _active_page(self) -> Any:
        handle = self._ensure_open()
        page = handle.tabs.get(handle.active_tab_id)
        if page is None:
            raise BrowserSessionError("the active browser tab no longer exists")
        return page

    def _remember_safe_url(self, tab_id: str, page: Any) -> Optional[str]:
        try:
            value = str(page.url)
        except Exception:
            return None
        if not value.startswith(("http://", "https://")):
            return None
        try:
            validated = validate_browser_url(value, self.policy)
        except BrowserSecurityError:
            return None
        self._ensure_open().last_safe_urls[tab_id] = validated
        return validated

    def recover_if_blank(self, deadline_seconds: float) -> dict[str, Any]:
        """Restore an unexpected about:blank without replacing the browser context.

        A hosted client reconnect can leave Chromium focused on a fresh blank page
        even though the durable session, cookies and context are still alive.  The
        last successful URL is remembered per tab and replayed into that same Page.
        """
        handle = self._ensure_open()
        page = self._active_page()
        current = str(getattr(page, "url", "") or "")
        if current.startswith(("http://", "https://")):
            self._remember_safe_url(handle.active_tab_id, page)
            return {
                "recovered": False,
                "tab_id": handle.active_tab_id,
                "context_id": handle.context_id,
                "url": self._safe_url(page),
            }
        if current not in {"", "about:blank"}:
            raise BrowserSecurityError("active browser tab is on an unsupported internal page")
        target = handle.last_safe_urls.get(handle.active_tab_id)
        if not target:
            return {
                "recovered": False,
                "recoverable": False,
                "tab_id": handle.active_tab_id,
                "context_id": handle.context_id,
                "url": current or "about:blank",
            }
        self._navigate(page, target, deadline_seconds)
        return {
            "recovered": True,
            "recoverable": True,
            "tab_id": handle.active_tab_id,
            "context_id": handle.context_id,
            "url": self._safe_url(page),
        }

    def _timeout_ms(self, deadline_seconds: float, *, default: float = 15.0) -> int:
        return max(1000, min(int(min(default, deadline_seconds) * 1000), 30_000))

    @staticmethod
    def _selector(value: Any) -> str:
        if not isinstance(value, str) or not value or len(value) > _MAX_SELECTOR_LEN:
            raise BrowserError("selector must be a 1-2000 character string")
        return value

    @staticmethod
    def _text(value: Any) -> str:
        if not isinstance(value, str) or len(value) > _MAX_VALUE_LEN:
            raise BrowserError("value must be a string up to 100000 characters")
        return value

    def _assert_agent_input_allowed(self) -> None:
        if self._ensure_open().takeover_active:
            raise BrowserSecurityError(
                "browser input is paused while the user has control; call resume_after_user_takeover after the user finishes"
            )

    def _route(self, route: Any) -> None:
        request = route.request
        try:
            target = validate_browser_url(request.url, self.policy)
            if _is_local_browser_url(target) and not (
                self._explicit_navigation_target
                and _same_navigation_target(target, self._explicit_navigation_target)
            ):
                sources: list[str] = []
                redirected = getattr(request, "redirected_from", None)
                seen = 0
                while redirected is not None and seen < 20:
                    sources.append(str(getattr(redirected, "url", "")))
                    redirected = getattr(redirected, "redirected_from", None)
                    seen += 1
                try:
                    frame_url = str(request.frame.url)
                except Exception:
                    frame_url = ""
                if frame_url:
                    sources.append(frame_url)
                if any(
                    urlsplit(source).scheme.lower() in {"http", "https"}
                    and not _is_local_browser_url(source)
                    for source in sources
                ):
                    raise BrowserSecurityError(
                        "external pages cannot redirect or issue subrequests to localhost"
                    )
        except BrowserSecurityError:
            route.abort()
            return
        route.continue_()

    def _register_page(self, page: Any, *, make_active: bool = True) -> str:
        handle = self._ensure_open()
        for tab_id, existing in handle.tabs.items():
            if existing is page:
                if make_active:
                    handle.active_tab_id = tab_id
                return tab_id
        tab_id = f"tab-{uuid.uuid4().hex[:12]}"
        handle.tabs[tab_id] = page
        if make_active or not handle.active_tab_id:
            handle.active_tab_id = tab_id
        self._attach_page_collectors(page, tab_id)
        self._remember_safe_url(tab_id, page)

        def remember_navigation(frame: Any = None) -> None:
            try:
                if frame is not None and frame is not page.main_frame:
                    return
            except Exception:
                pass
            self._remember_safe_url(tab_id, page)

        try:
            page.on("framenavigated", remember_navigation)
            page.on("domcontentloaded", lambda: remember_navigation())
            page.on("download", lambda download: download.cancel())
        except Exception:
            pass
        return tab_id

    def _attach_page_collectors(self, page: Any, tab_id: str) -> None:
        handle = self._ensure_open()

        def on_console(msg: Any) -> None:
            if len(handle.console) >= _MAX_CONSOLE_ENTRIES:
                handle.console.pop(0)
            try:
                location = msg.location
                loc = f"{_safe_network_url(str(location.get('url', '')))}:{location.get('lineNumber', '')}"
            except Exception:
                loc = ""
            handle.console.append(
                _ConsoleEntry(
                    type=str(getattr(msg, "type", "")).lower(),
                    text=_safe_console_text(getattr(msg, "text", "")),
                    location=loc,
                    tab_id=tab_id,
                )
            )

        def on_request(request: Any) -> None:
            handle.request_started[id(request)] = time.monotonic()

        def on_response(response: Any) -> None:
            request = getattr(response, "request", None)
            started = handle.request_started.pop(id(request), None) if request is not None else None
            duration_ms = round((time.monotonic() - started) * 1000, 2) if started else None
            try:
                status = int(response.status)
            except Exception:
                status = 0
            try:
                headers = response.headers
                content_type = str(headers.get("content-type", ""))[:200]
                content_length = int(headers.get("content-length", "0") or 0)
            except Exception:
                content_type = ""
                content_length = 0
            entry: dict[str, Any] = {
                "url": _safe_network_url(str(getattr(response, "url", ""))),
                "method": str(getattr(request, "method", ""))[:20],
                "status": status,
                "content_type": content_type,
                "resource_type": str(getattr(request, "resource_type", ""))[:50],
                "duration_ms": duration_ms,
                "tab_id": tab_id,
                "field_names": [],
                "values": {},
            }
            if (
                self.policy.network_inspection
                and "json" in content_type.lower()
                # Unknown/chunked response sizes are metadata-only. Reading an
                # unbounded body merely to discover its size would defeat the
                # inspection limit.
                and 0 < content_length <= _MAX_JSON_BYTES
            ):
                try:
                    raw = response.body()
                    if len(raw) <= _MAX_JSON_BYTES:
                        decoded = json.loads(raw.decode("utf-8"))
                        entry["field_names"] = _safe_field_names(decoded)
                        entry["values"] = _allowed_json_values(decoded)
                except Exception:
                    pass
            if len(handle.network) >= _MAX_NETWORK_ENTRIES:
                handle.network.pop(0)
            handle.network.append(entry)

        def on_request_failed(request: Any) -> None:
            started = handle.request_started.pop(id(request), None)
            duration_ms = round((time.monotonic() - started) * 1000, 2) if started else None
            try:
                failure = str(request.failure or "request failed")
            except Exception:
                failure = "request failed"
            if len(handle.network) >= _MAX_NETWORK_ENTRIES:
                handle.network.pop(0)
            handle.network.append(
                {
                    "url": _safe_network_url(str(getattr(request, "url", ""))),
                    "method": str(getattr(request, "method", ""))[:20],
                    "status": 0,
                    "content_type": "",
                    "resource_type": str(getattr(request, "resource_type", ""))[:50],
                    "duration_ms": duration_ms,
                    "tab_id": tab_id,
                    "error": str(redact(failure))[:500],
                    "field_names": [],
                    "values": {},
                }
            )

        page.on("console", on_console)
        page.on("request", on_request)
        page.on("response", on_response)
        page.on("requestfailed", on_request_failed)

    def _launch_browser_context(
        self,
        ctx: Any,
        proxy: SessionPinnedProxy,
        *,
        width: int,
        height: int,
    ) -> tuple[Any, Any, Optional[SystemChromeLaunch], str]:
        # A visible takeover session uses the installed Google Chrome with a
        # dedicated persistent KaroX profile.  CDP attaches after Chrome starts,
        # so identity providers see a normal system browser rather than
        # Playwright's bundled automation Chromium.  The profile is KaroX-only;
        # the user's everyday Chrome cookies and tabs are never opened.
        if self.policy.headed and self.policy.user_takeover:
            try:
                launch = launch_system_chrome(
                    ctx,
                    proxy_server=proxy.server_url,
                    proxy_username=proxy.username,
                    proxy_password=proxy.password,
                    width=width,
                    height=height,
                )
            except SystemChromeError as exc:
                raise BrowserError(str(exc)) from exc
            return launch.browser, launch.context, launch, launch.engine

        browser = ctx.chromium.launch(
            headless=not self.policy.headed,
            proxy={
                "server": proxy.server_url,
                "username": proxy.username,
                "password": proxy.password,
            },
        )
        context = browser.new_context(
            viewport={"width": width, "height": height},
            accept_downloads=False,
            service_workers="block",
        )
        return browser, context, None, "playwright_chromium"

    def open(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        url = validate_browser_url(arguments.get("url"), self.policy)
        width = int(arguments.get("width", 1440))
        height = int(arguments.get("height", 900))
        if not 320 <= width <= 3840 or not 240 <= height <= 2160:
            raise BrowserError("browser viewport is outside safe bounds (320-3840 x 240-2160)")
        if self._handle is not None:
            self._assert_agent_input_allowed()
            page = self._active_page()
            self._navigate(page, url, deadline_seconds)
            handle = self._ensure_open()
            return {
                "open": True,
                "reused": True,
                "url": self._safe_url(page),
                "tab_id": handle.active_tab_id,
                "session_id": self.policy.session_id,
                "context_id": handle.context_id,
                "context_preserved": True,
                "engine": handle.engine,
                "profile_persistent": handle.system_chrome is not None,
            }
        # Headless/non-takeover work uses Playwright's managed Chromium. Provision
        # its matching binary lazily on first use so a normal KaroX installation
        # never requires a separate `playwright install chromium` command. A
        # headed user-takeover session launches the installed system Chrome
        # instead, so it deliberately skips the Chromium download.
        if not (self.policy.headed and self.policy.user_takeover):
            try:
                ensure_playwright_chromium()
            except BrowserBootstrapError as exc:
                raise BrowserError(str(exc)) from None
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as exc:
            raise BrowserError(_PLAYWRIGHT_MISSING) from exc
        ctx = sync_playwright().start()
        browser = None
        context = None
        proxy: Optional[SessionPinnedProxy] = None
        try:
            proxy = SessionPinnedProxy(self.policy)
            proxy.start()
            browser, context, system_chrome, engine = self._launch_browser_context(
                ctx,
                proxy,
                width=width,
                height=height,
            )
            # The session proxy validates and pins every upstream connection.
            # Context routing remains as a second policy boundary for ordinary
            # page requests and redirects.
            context.route("**/*", self._route)
            handle = _BrowserHandle(
                playwright_ctx=ctx,
                browser=browser,
                context=context,
                proxy=proxy,
                engine=engine,
                system_chrome=system_chrome,
            )
            self._handle = handle
            context.on("page", lambda page: self._register_page(page, make_active=True))

            # System Chrome may start with an ordinary new-tab page. Register
            # any pre-existing pages so they join the controlled tab set, then
            # open the actual work in a fresh tab. There is no longer a
            # protected branded tab: status is shown only through the in-page
            # indicator, extension badge, and side panel.
            for existing_page in list(context.pages):
                self._register_page(existing_page, make_active=False)
            page = context.new_page()
            tab_id = self._register_page(page, make_active=True)
            self._navigate(page, url, deadline_seconds)
        except Exception:
            self._handle = None
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            try:
                if browser is not None:
                    browser.close()
            except Exception:
                pass
            try:
                if proxy is not None:
                    proxy.stop()
            except Exception:
                pass
            ctx.stop()
            raise
        return {
            "open": True,
            "url": self._safe_url(page),
            "viewport": {"width": width, "height": height},
            "session_id": self.policy.session_id,
            "tab_id": tab_id,
            "context_id": handle.context_id,
            "headed": self.policy.headed,
            "context_isolated": True,
            "context_preserved": True,
            "engine": handle.engine,
            "profile_persistent": handle.system_chrome is not None,
        }

    def _navigate(self, page: Any, url: str, deadline_seconds: float) -> None:
        timeout = self._timeout_ms(deadline_seconds, default=30.0)
        self._explicit_navigation_target = url
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout)
            validate_browser_url(str(page.url), self.policy)
            handle = self._ensure_open()
            for tab_id, candidate in handle.tabs.items():
                if candidate is page:
                    self._remember_safe_url(tab_id, page)
                    break
        finally:
            self._explicit_navigation_target = None

    @staticmethod
    def _safe_url(page: Any) -> str:
        try:
            return _safe_network_url(str(page.url))
        except Exception:
            return ""

    def tabs(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        handle = self._ensure_open()
        rows: list[dict[str, Any]] = []
        for tab_id, page in list(handle.tabs.items()):
            try:
                if page.is_closed():
                    handle.tabs.pop(tab_id, None)
                    continue
            except Exception:
                pass
            try:
                title = str(redact(page.title()))[:300]
            except Exception:
                title = ""
            rows.append(
                {
                    "tab_id": tab_id,
                    "active": tab_id == handle.active_tab_id,
                    "url": self._safe_url(page),
                    "title": title,
                }
            )
        return {
            "tabs": rows,
            "count": len(rows),
            "active_tab_id": handle.active_tab_id,
            "context_id": handle.context_id,
            "takeover_active": handle.takeover_active,
            "engine": handle.engine,
            "profile_persistent": handle.system_chrome is not None,
        }

    def new_tab(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        handle = self._ensure_open()
        url = arguments.get("url")
        page = handle.context.new_page()
        tab_id = self._register_page(page, make_active=True)
        if url is not None:
            self._navigate(page, validate_browser_url(url, self.policy), deadline_seconds)
        return {"created": True, "tab_id": tab_id, "url": self._safe_url(page)}

    def switch_tab(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        handle = self._ensure_open()
        tab_id = arguments.get("tab_id")
        if not isinstance(tab_id, str) or tab_id not in handle.tabs:
            raise BrowserSecurityError("browser tab does not belong to this KaroX session")
        page = handle.tabs[tab_id]
        try:
            if page.is_closed():
                raise BrowserSessionError("browser tab is closed")
            page.bring_to_front()
        except BrowserSessionError:
            raise
        except Exception as exc:
            raise BrowserError("browser tab could not be activated") from exc
        handle.active_tab_id = tab_id
        return {"switched": True, "tab_id": tab_id, "url": self._safe_url(page)}

    def close_tab(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        handle = self._ensure_open()
        tab_id = arguments.get("tab_id")
        if not isinstance(tab_id, str) or tab_id not in handle.tabs:
            raise BrowserSecurityError("browser tab does not belong to this KaroX session")
        if len(handle.tabs) <= 1:
            raise BrowserSecurityError("cannot close the last tab; use browser.close")
        page = handle.tabs.pop(tab_id)
        page.close()
        if handle.active_tab_id == tab_id:
            handle.active_tab_id = next(iter(handle.tabs))
            handle.tabs[handle.active_tab_id].bring_to_front()
        return {"closed": True, "tab_id": tab_id, "active_tab_id": handle.active_tab_id}

    def request_user_takeover(
        self, arguments: Mapping[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        handle = self._ensure_open()
        if not self.policy.user_takeover:
            raise BrowserSecurityError("user takeover is not enabled for this browser session")
        if not self.policy.headed:
            raise BrowserSecurityError("user takeover requires a visible headed browser")
        handle.takeover_active = True
        reason = arguments.get("reason") or "sensitive browser action"
        if not isinstance(reason, str) or len(reason) > 500:
            raise BrowserError("takeover reason must be a string up to 500 characters")
        page = self._active_page()
        try:
            page.bring_to_front()
        except Exception:
            pass
        window_activated = False
        if handle.system_chrome is not None:
            window_activated = activate_chrome_window(handle.system_chrome.process)
        return {
            "takeover": True,
            "agent_input_paused": True,
            "window_activated": window_activated,
            "session_id": self.policy.session_id,
            "tab_id": handle.active_tab_id,
            "context_id": handle.context_id,
            "context_preserved": True,
            "reason": str(redact(reason)),
            "instruction": (
                "Complete login, CAPTCHA, password, 2FA, consent, or payment review in the visible browser window. "
                "Then explicitly resume this same browser session."
            ),
        }

    def resume_after_user_takeover(
        self, arguments: Mapping[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        handle = self._ensure_open()
        if not handle.takeover_active:
            raise BrowserSecurityError("browser session is not in user takeover mode")
        handle.takeover_active = False
        recovery = self.recover_if_blank(deadline_seconds)
        page = self._active_page()
        validate_browser_url(str(page.url), self.policy)
        return {
            "resumed": True,
            "agent_input_paused": False,
            "session_id": self.policy.session_id,
            "tab_id": handle.active_tab_id,
            "context_id": handle.context_id,
            "context_preserved": True,
            "recovered_blank": bool(recovery.get("recovered")),
            "url": self._safe_url(page),
        }

    def _locator_metadata(self, locator: Any) -> dict[str, str]:
        try:
            data = locator.evaluate(
                """el => {
                    const scope = el.closest('form,section,article,[role=dialog]');
                    const karoxSecret = el.getAttribute('data-karox-secret') === 'true' || el.dataset.karoxSecret === 'true';
                    return {
                        type: (el.getAttribute('type') || el.tagName || '').toLowerCase(),
                        name: el.getAttribute('name') || '',
                        id: el.id || '',
                        aria: el.getAttribute('aria-label') || '',
                        placeholder: el.getAttribute('placeholder') || '',
                        secret: karoxSecret ? 'true' : '',
                        text: karoxSecret ? '' : (el.innerText || el.value || '').trim().slice(0, 500),
                        context: (scope && scope.innerText || '').trim().slice(0, 2000)
                    };
                }"""
            )
            return {str(k): str(v) for k, v in (data or {}).items()}
        except Exception:
            return {}

    def _assert_action_safe(self, locator: Any, deadline_seconds: float) -> None:
        metadata = self._locator_metadata(locator)
        if not metadata or not any(metadata.values()):
            raise BrowserSecurityError(
                "browser target could not be inspected safely; use user takeover"
            )
        text = " ".join(metadata.values())
        payment_like = bool(_PAYMENT_TEXT.search(text))
        free_trial = bool(_FREE_TRIAL_TEXT.search(text))
        if payment_like and not free_trial and not self.policy.payment_confirmation:
            raise BrowserSecurityError(
                "payment or subscription action requires explicit browser payment_confirmation permission or user takeover"
            )
        if free_trial and not self.policy.payment_confirmation:
            # Evidence must be near the control, not somewhere unrelated in the
            # page footer or another pricing card.
            evidence_text = f"{metadata.get('context', '')}\n{text}"
            if not _FREE_EVIDENCE.search(evidence_text):
                raise BrowserSecurityError(
                    "free-trial action is ambiguous: its surrounding form or section does not clearly show zero price or no-card-required evidence; use user takeover"
                )

    def click(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        selector = self._selector(arguments.get("selector"))
        page = self._active_page()
        locator = page.locator(selector).first
        self._assert_action_safe(locator, deadline_seconds)
        locator.click(timeout=self._timeout_ms(deadline_seconds))
        return {"clicked": True, "selector": selector}

    def fill(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        selector = self._selector(arguments.get("selector"))
        value = self._text(arguments.get("value"))
        locator = self._active_page().locator(selector).first
        metadata = self._locator_metadata(locator)
        if not metadata or not metadata.get("type"):
            raise BrowserSecurityError(
                "input field could not be inspected safely; use user takeover"
            )
        descriptor = " ".join(
            metadata.get(key, "")
            for key in ("type", "name", "id", "aria", "placeholder")
        )
        if (
            metadata.get("secret") == "true"
            or metadata.get("type") == "password"
            or _SECRET_INPUT_HINT.search(descriptor)
        ):
            raise BrowserSecurityError(
                "password, token, credential, or payment fields must be completed through user takeover or local credential injection"
            )
        if metadata.get("type") == "email":
            if not self.policy.allowed_emails:
                raise BrowserSecurityError(
                    "email fill requires an email explicitly allowed for this browser session"
                )
            if value.strip().lower() not in self.policy.allowed_emails:
                raise BrowserSecurityError("email is not allowed for this browser session")
        locator.fill(value, timeout=self._timeout_ms(deadline_seconds))
        return {"filled": True, "selector": selector, "value_length": len(value)}

    def fill_credential(
        self, arguments: Mapping[str, Any], deadline_seconds: float
    ) -> dict[str, Any]:
        """Fill one login field from an opaque OS-keyring browser reference.

        The hosted caller can choose only the target element, credential
        reference, and field name. The resolved value never enters tool
        arguments/results, logs, snapshots, or exception text.
        """

        self._assert_agent_input_allowed()
        selector = self._selector(arguments.get("selector"))
        reference = arguments.get("reference")
        field = arguments.get("field")
        if not isinstance(reference, str) or not reference.startswith("os-keyring:browser/"):
            raise BrowserError(
                "browser credential reference must use os-keyring:browser/<name>"
            )
        if field not in {"username", "password"}:
            raise BrowserError("browser credential field must be username or password")
        if (
            self.policy.allowed_credential_refs
            and reference not in self.policy.allowed_credential_refs
        ):
            raise BrowserSecurityError(
                "browser credential reference is not approved for this browser session"
            )

        locator = self._active_page().locator(selector).first
        metadata = self._locator_metadata(locator)
        if not metadata or not metadata.get("type"):
            raise BrowserSecurityError(
                "credential input field could not be inspected safely; use user takeover"
            )
        descriptor = " ".join(
            metadata.get(key, "")
            for key in ("type", "name", "id", "aria", "placeholder")
        )
        field_type = metadata.get("type", "").lower()
        if _PAYMENT_CREDENTIAL_HINT.search(descriptor):
            raise BrowserSecurityError(
                "browser credential injection never fills payment, card, CVV/CVC, IBAN, or billing fields; use user takeover"
            )
        if field == "password":
            if field_type != "password" and not _SECRET_INPUT_HINT.search(descriptor):
                raise BrowserSecurityError(
                    "password credential may only be injected into a password/credential field"
                )
        else:
            if field_type == "password" or _SECRET_INPUT_HINT.search(descriptor):
                raise BrowserSecurityError(
                    "username credential may not be injected into a password/secret field"
                )
            if field_type not in {"email", "text", "input", "tel"}:
                raise BrowserSecurityError(
                    "username credential target must be a text, email, or username-like input"
                )
            # A locally stored username/email is authorized by the profile's
            # explicit credential-reference allowlist. ``allowed_emails`` is a
            # separate guard for model-supplied plain-text browser.fill values;
            # requiring both made detach leave a second, stale authority behind.

        try:
            result = inject_browser_credential(
                locator=locator,
                credential_store=self._credential_store,
                reference=reference,
                field=field,
                timeout_ms=self._timeout_ms(deadline_seconds),
            )
        except BrowserCredentialInjectionError:
            raise
        return {"selector": selector, **result.to_dict()}

    def select(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        selector = self._selector(arguments.get("selector"))
        raw = arguments.get("value")
        if isinstance(raw, str):
            values = [raw]
        elif isinstance(raw, list) and raw and all(isinstance(item, str) for item in raw):
            values = list(raw)
        else:
            raise BrowserError("select value must be a non-empty string or string array")
        self._active_page().locator(selector).first.select_option(
            values, timeout=self._timeout_ms(deadline_seconds)
        )
        return {"selected": True, "selector": selector, "count": len(values)}

    def press(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        self._assert_agent_input_allowed()
        selector = self._selector(arguments.get("selector"))
        key = self._text(arguments.get("key"))
        locator = self._active_page().locator(selector).first
        if key.lower() in {"enter", "numpadenter", "space"}:
            self._assert_action_safe(locator, deadline_seconds)
        locator.press(key, timeout=self._timeout_ms(deadline_seconds))
        return {"pressed": True, "selector": selector, "key": key}

    def wait_for(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        page = self._active_page()
        selector = arguments.get("selector")
        state = arguments.get("state", "visible")
        if state not in {"attached", "detached", "hidden", "visible"}:
            raise BrowserError("state must be attached, detached, hidden, or visible")
        timeout = max(1000, min(int(deadline_seconds * 1000), 60_000))
        if selector is not None:
            selector = self._selector(selector)
            page.locator(selector).first.wait_for(state=state, timeout=timeout)
            return {"waited": True, "selector": selector, "state": state}
        milliseconds = arguments.get("milliseconds", 250)
        if not isinstance(milliseconds, (int, float)) or not 0 < milliseconds <= 10_000:
            raise BrowserError("milliseconds must be between 0 and 10000")
        page.wait_for_timeout(int(milliseconds))
        return {"waited": True, "milliseconds": int(milliseconds)}

    def get_text(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        selector = self._selector(arguments.get("selector"))
        text = self._active_page().locator(selector).first.inner_text(
            timeout=self._timeout_ms(deadline_seconds)
        )
        clipped = text[:_MAX_TEXT_CHARS]
        return {"text": str(redact(clipped)), "truncated": len(text) > _MAX_TEXT_CHARS}

    def screenshot(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        page = self._active_page()
        full_page = bool(arguments.get("full_page", True))
        name = arguments.get("name") or "screenshot"
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9._\-]{1,128}", name):
            raise BrowserError("name may contain 1-128 letters, digits, dots, hyphens, or underscores")
        png = page.screenshot(full_page=full_page, type="png")
        if not isinstance(png, (bytes, bytearray)) or not png:
            raise BrowserError("screenshot produced no bytes")
        record = self._artifacts.put(bytes(png), name=name, mime="image/png")
        viewport = page.viewport_size or {}
        return {
            "artifact_id": record.artifact_id,
            "name": record.name,
            "mime": record.mime,
            "size": record.size,
            "sha256": record.sha256,
            "width": viewport.get("width"),
            "height": viewport.get("height"),
            "full_page": full_page,
            "tab_id": self._ensure_open().active_tab_id,
        }

    def console(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        handle = self._ensure_open()
        tab_id = arguments.get("tab_id")
        entries = [
            {
                "type": item.type,
                "text": item.text,
                "location": item.location,
                "tab_id": item.tab_id,
            }
            for item in handle.console
            if tab_id is None or item.tab_id == tab_id
        ]
        return {"entries": entries, "count": len(entries)}

    def network_failures(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        handle = self._ensure_open()
        entries = [item for item in handle.network if item.get("status", 0) >= 400 or item.get("error")]
        return {"failed_requests": entries, "count": len(entries)}

    def network_requests(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        if not self.policy.network_inspection:
            raise BrowserSecurityError("network inspection is not enabled for this browser session")
        url_contains = arguments.get("url_contains")
        method = arguments.get("method")
        resource_type = arguments.get("resource_type")
        fields = arguments.get("fields")
        if url_contains is not None and (not isinstance(url_contains, str) or len(url_contains) > 500):
            raise BrowserError("url_contains must be a string up to 500 characters")
        if method is not None and not isinstance(method, str):
            raise BrowserError("method must be a string")
        if resource_type is not None and not isinstance(resource_type, str):
            raise BrowserError("resource_type must be a string")
        if fields is not None:
            if not isinstance(fields, list) or not all(isinstance(item, str) for item in fields):
                raise BrowserError("fields must be a string array")
            requested_fields = {item.lower() for item in fields}
        else:
            requested_fields = set(_ALLOWED_VALUE_FIELDS)
        rows: list[dict[str, Any]] = []
        for item in self._ensure_open().network:
            if url_contains and url_contains.lower() not in str(item.get("url", "")).lower():
                continue
            if method and str(item.get("method", "")).lower() != method.lower():
                continue
            if resource_type and str(item.get("resource_type", "")).lower() != resource_type.lower():
                continue
            row = dict(item)
            raw_values = row.get("values")
            values: dict[Any, Any] = raw_values if isinstance(raw_values, dict) else {}
            row["values"] = {
                key: value for key, value in values.items() if key.lower() in requested_fields
            }
            rows.append(row)
        return {"requests": rows, "count": len(rows), "filters_applied": True}

    def snapshot(self, arguments: Mapping[str, Any], deadline_seconds: float) -> dict[str, Any]:
        page = self._active_page()
        probe = r"""
        () => {
          const SECRET_HINT = /password|secret|token|api[_-]?key|credential|cookie|card|cvv|cvc/i;
          const out = {headings: [], buttons: [], inputs: [], links: [], dialogs: [], tabs: [], text: ''};
          const text = [];
          for (const el of Array.from(document.querySelectorAll('h1,h2,h3,h4,h5,h6,button,input,select,textarea,a,[role=dialog],[role=tab]')).slice(0, 500)) {
            const tag = el.tagName.toLowerCase();
            const label = (el.getAttribute('aria-label') || el.innerText || el.placeholder || '').trim().slice(0, 200);
            if (/^h[1-6]$/.test(tag)) out.headings.push({level: tag, text: label});
            else if (tag === 'button' || el.getAttribute('role') === 'button') out.buttons.push({name: label, disabled: !!el.disabled});
            else if (['input','select','textarea'].includes(tag)) {
              const type = (el.getAttribute('type') || tag).toLowerCase();
              const secret = el.getAttribute('data-karox-secret') === 'true' || el.dataset.karoxSecret === 'true' || type === 'password' || SECRET_HINT.test([el.name, el.id, label].join(' '));
              out.inputs.push({label, type, disabled: !!el.disabled, secret, value_length: secret ? null : (el.value || '').length});
            } else if (tag === 'a') out.links.push({text: label, href: (el.getAttribute('href') || '').slice(0, 300)});
            else if (el.getAttribute('role') === 'dialog') out.dialogs.push({name: label});
            else if (el.getAttribute('role') === 'tab') out.tabs.push({text: label, selected: el.getAttribute('aria-selected') === 'true'});
            if (label) text.push(label);
          }
          out.text = text.join(' ').slice(0, 20000);
          out.scroll = {width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight,
                        viewport_width: window.innerWidth, viewport_height: window.innerHeight,
                        horizontal_overflow: document.documentElement.scrollWidth > window.innerWidth + 1};
          return out;
        }
        """
        try:
            data = page.evaluate(probe)
        except Exception as exc:
            raise BrowserError("snapshot could not be captured") from exc
        try:
            title = str(redact(page.title()))
        except Exception:
            title = ""
        try:
            focused = page.evaluate("() => document.activeElement ? document.activeElement.tagName.toLowerCase() : null")
        except Exception:
            focused = None
        handle = self._ensure_open()
        return {
            "url": self._safe_url(page),
            "title": title,
            "viewport": page.viewport_size,
            "focused_element": focused,
            "tab_id": handle.active_tab_id,
            "context_id": handle.context_id,
            "takeover_active": handle.takeover_active,
            "context_preserved": True,
            "snapshot": redact(data),
        }

    def close(self, *, force: bool = False) -> dict[str, Any]:
        handle = self._handle
        if handle is not None and handle.takeover_active and not force:
            raise BrowserSecurityError(
                "browser cannot be closed while user takeover is active; resume the session first"
            )
        self._handle = None
        if handle is None:
            return {"closed": True, "was_open": False, "session_id": self.policy.session_id}
        context_closed = False
        browser_closed = False
        proxy_stopped = False
        try:
            if handle.system_chrome is not None:
                # The default persistent CDP context belongs to the external
                # Chrome process. Closing the browser is the authoritative
                # teardown; context.close() is not supported consistently for
                # that default context across Chrome/Playwright versions.
                try:
                    handle.browser.close()
                    browser_closed = True
                    context_closed = True
                except Exception:
                    pass
                finally:
                    terminate_chrome_process(handle.system_chrome.process)
            else:
                try:
                    handle.context.close()
                    context_closed = True
                except Exception:
                    pass
                try:
                    handle.browser.close()
                    browser_closed = True
                except Exception:
                    pass
            try:
                if handle.proxy is not None:
                    handle.proxy.stop()
                    proxy_stopped = True
            except Exception:
                pass
        finally:
            try:
                handle.playwright_ctx.stop()
            except Exception:
                pass
        return {
            "closed": True,
            "was_open": True,
            "context_closed": context_closed,
            "browser_stopped": browser_closed,
            "dns_pinning_proxy_stopped": proxy_stopped,
            "session_id": self.policy.session_id,
        }


# The public browser methods below may be called concurrently by the MCP ASGI
# server. Wrapping them after the class is defined keeps their implementations
# readable while guaranteeing that sync Playwright never crosses threads. The
# queue also gives close a strict ordering behind every earlier operation, so it
# cannot tear down a page while new_page/title/goto is still in flight.
_BROWSER_OWNER_METHODS = (
    "open",
    "recover_if_blank",
    "tabs",
    "new_tab",
    "switch_tab",
    "close_tab",
    "request_user_takeover",
    "resume_after_user_takeover",
    "click",
    "fill",
    "select",
    "press",
    "wait_for",
    "get_text",
    "screenshot",
    "console",
    "network_failures",
    "network_requests",
    "snapshot",
    "close",
)
for _browser_method_name in _BROWSER_OWNER_METHODS:
    setattr(
        SecureBrowserSessionManager,
        _browser_method_name,
        _on_browser_owner_thread(
            getattr(SecureBrowserSessionManager, _browser_method_name)
        ),
    )
