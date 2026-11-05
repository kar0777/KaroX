"""Explicit, secret-free browser permissions and the network boundary.

Ported from the proven KaroX v5 ``browser_access`` policy layer and tightened
for this line:

* ``BrowserAccessPolicy`` is the one object that says what a session may
  reach: loopback, external HTTPS with a domain allowlist, headed mode, user
  takeover, network inspection, payment confirmation, explicitly supplied
  test credentials, downloads, and detached instances.
* ``validate_browser_url`` re-checks every URL on every navigation *and* on
  every routed request, so a public host that rebinds DNS or redirects to a
  private address is refused at the boundary it would cross.
* ``SessionPinnedProxy`` keeps the final DNS lookup inside KaroX: the browser
  talks to a per-session authenticated loopback proxy that resolves,
  validates, and pins the upstream IP, which closes the rebinding gap between
  the policy lookup and Chromium's own later resolver lookup.

The extension backend and system-Chrome launch of v5 are out of scope for the
clean release line; headed sessions use the managed Playwright Chromium.
"""

from __future__ import annotations

import base64
import ipaddress
import re
import secrets
import select
import socket
import socketserver
import threading
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence
from urllib.parse import urlsplit

from .credentials import BrowserCredentialReference
from .errors import BrowserSecurityError

__all__ = [
    "BrowserAccessPolicy",
    "SessionPinnedProxy",
    "validate_browser_url",
]

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
_CONNECT_ATTEMPT_TIMEOUT_SECONDS = 3.0
_CONNECT_TOTAL_TIMEOUT_SECONDS = 10.0


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
    # Login to a test account with credentials the user explicitly supplied as
    # plain tool arguments. Off by default: password-shaped fills then require
    # OS-keyring credential references or user takeover. Values filled under
    # this flag are redacted from every result, snapshot, and log.
    plain_credentials: bool = False
    downloads: bool = False
    allow_detach: bool = False
    # Serve GET subresources of a local page from the public code-source
    # allowlist (``web_fetch``'s hosts) through KaroX; see ``code_sources``.
    # Off by default; the agent's browser turns it on only when the session
    # may use ``code.fetch``.
    code_sources: bool = False
    allowed_emails: tuple[str, ...] = ()
    allowed_credential_refs: tuple[str, ...] = ()
    saved_profile_id: str = "ad-hoc"

    def __post_init__(self) -> None:
        if not isinstance(self.session_id, str) or not self.session_id.strip():
            raise ValueError("browser policy requires a session_id")
        if (
            not isinstance(self.saved_profile_id, str)
            or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.saved_profile_id)
            is None
        ):
            raise ValueError(
                "browser saved_profile_id must be a safe 1-128 character identifier"
            )
        for field_name in (
            "localhost",
            "external_https",
            "headed",
            "user_takeover",
            "network_inspection",
            "payment_confirmation",
            "plain_credentials",
            "downloads",
            "allow_detach",
            "code_sources",
        ):
            if not isinstance(getattr(self, field_name), bool):
                raise ValueError(f"browser policy {field_name} must be boolean")
        allowed = tuple(
            dict.fromkeys(_canonical_domain(item) for item in self.allowed_domains)
        )
        denied = tuple(
            dict.fromkeys(_canonical_domain(item) for item in self.denied_domains)
        )
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
                raise ValueError(
                    "browser allowed credential reference is invalid"
                ) from exc
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
        if self.plain_credentials and not (self.localhost or self.external_https):
            raise ValueError("plain test credentials require a navigation permission")

    def to_diagnostics(self) -> dict[str, Any]:
        return {
            "read": True,
            "input": True,
            "localhost": self.localhost,
            "external_https": self.external_https,
            "user_takeover": self.user_takeover,
            "network_inspection": self.network_inspection,
            "payment_confirmation": self.payment_confirmation,
            "plain_credentials": self.plain_credentials,
            "downloads": self.downloads,
            "allow_detach": self.allow_detach,
            "code_sources": self.code_sources,
            "headed": self.headed,
            "allowed_domains": list(self.allowed_domains),
            "denied_domains": list(self.denied_domains),
            "allowed_email_count": len(self.allowed_emails),
            "allowed_credential_refs": list(self.allowed_credential_refs),
            "dns_pinning_proxy": True,
            "proxy_authentication": "per_session",
            "proxy_port": "random_loopback",
            "service_workers": "blocked",
            "saved_profile_id": self.saved_profile_id,
        }


def _domain_matches(host: str, domains: Sequence[str]) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in domains)


def _resolved_addresses(
    host: str, port: int
) -> tuple[ipaddress.IPv4Address | ipaddress.IPv6Address, ...]:
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


def is_local_browser_url(value: str) -> bool:
    try:
        return (urlsplit(value).hostname or "").lower().rstrip(".") in _LOCAL_HOSTS
    except Exception:
        return False


def validate_browser_url(value: Any, policy: BrowserAccessPolicy) -> str:
    """Validate one navigation/request URL against the session policy.

    Validation is repeated for every Playwright route and after navigation. A
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
    try:
        port = parsed.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise BrowserSecurityError("browser URL port is invalid") from exc

    if host in _LOCAL_HOSTS:
        if not policy.localhost:
            raise BrowserSecurityError("localhost browser access is not enabled")
        addresses = _resolved_addresses(host, port)
        if not all(address.is_loopback for address in addresses):
            raise BrowserSecurityError(
                "localhost URL resolved to a non-loopback address"
            )
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
        raise BrowserSecurityError(
            "browser target resolved to a private or reserved address"
        )
    return value


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
    # Windows machines commonly resolve large CDNs to IPv6 first even when the
    # local network has no working IPv6 route. Prefer IPv4, cap each attempt,
    # and also cap the whole address set.
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
    raise BrowserSecurityError(
        "browser proxy could not connect to the validated target"
    ) from last


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
    def _send(
        client: socket.socket, status: int, reason: str, headers: bytes = b""
    ) -> None:
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
    def _parse_headers(
        lines: list[bytes],
    ) -> tuple[list[tuple[str, str]], dict[str, str]]:
        ordered: list[tuple[str, str]] = []
        lookup: dict[str, str] = {}
        for raw in lines:
            if b":" not in raw:
                raise BrowserSecurityError("browser proxy received a malformed header")
            raw_name, raw_value = raw.split(b":", 1)
            name = raw_name.decode("latin-1").strip()
            value = raw_value.decode("latin-1").strip()
            lookup[name.lower()] = value
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
            raise BrowserSecurityError(
                "browser proxy CONNECT target has an invalid port"
            ) from exc
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
            raise BrowserSecurityError(
                "browser proxy accepts plain requests only for HTTP localhost"
            )
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


def same_navigation_target(left: str, right: str) -> bool:
    """Whether two URLs describe the same navigation target (public helper)."""

    return _same_navigation_target(left, right)
