"""Bridge profiles, saved launch profiles, and bridge credentials.

A bridge connects an external hosted AI client (ChatGPT Web, Claude Web,
Notion, Hyperagent, or Adapt) to the local KaroX runtime. Each known client is
described by a declarative profile with an honest status; KaroX never claims
support for a product that lacks a real end-to-end test.

This module also owns the secret-free saved launch profiles that make
``karox bridge connect --saved NAME`` repeatable. The on-disk JSON stays
compatible with the KaroX 5 ``%APPDATA%\\KaroX\\vnext\\web-bridge-profiles.json``
shape: unknown fields are preserved on read and written back unchanged, so a
newer or older KaroX build never destroys configuration it does not understand.

Bridge credentials live in a dedicated OS-keyring namespace, distinct from
provider and MCP secrets, and can be rotated or emergency-revoked without
touching other credential stores.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from .credentials import CredentialBackend, CredentialError, KeyringBackend
from .models import AccessProfile
from .paths import config_dir, runtime_dir, session_dir


BRIDGE_REGISTRY_VERSION = 1
DEFAULT_BRIDGE_DEADLINE_SECONDS = 600.0
BRIDGE_PROFILE_STORE_VERSION = 1
_BRIDGE_REFERENCE_PREFIX = "os-keyring:bridge/"
_BRIDGE_SERVICE = "KaroX/bridge"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_MCP_SERVER_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_PROJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

WEB_BRIDGE_PROFILES: tuple[str, ...] = (
    "chatgpt-web",
    "claude-web",
    "notion",
    "hyperagent-web",
    "adapt",
)
_TARGET_PROFILES = frozenset(WEB_BRIDGE_PROFILES)
_TUNNELS = frozenset({"cloudflare", "tailscale", "custom"})
_LANGUAGES = frozenset({"en", "ru"})
_SAVED_PROFILE_FIELDS = frozenset(
    {
        "name",
        "target_profile",
        "tools",
        "mcp_servers",
        "repository",
        "projects",
        "default_project_id",
        "verification_commands",
        "server_profiles",
        "browser_external_https",
        "browser_allowed_domains",
        "browser_denied_domains",
        "browser_headed",
        "browser_user_takeover",
        "browser_network_inspection",
        "browser_payment_confirmation",
        "browser_allowed_emails",
        "browser_credential_refs",
        "deadline_seconds",
        "tunnel",
        "public_url",
        "language",
        "access_profile",
        "bypass",
        "port",
        "tunnel_timeout_seconds",
    }
)


# --------------------------------------------------------------------------- #
# Declarative profiles                                                         #
# --------------------------------------------------------------------------- #


class BridgeError(RuntimeError):
    pass


class BridgeConfigurationError(BridgeError):
    pass


class BridgeAccessDenied(BridgeError, PermissionError):
    pass


class BridgeStatus(str):
    TESTED = "tested"
    # Evidence exists, but it exercises the legacy ``server/`` gateway rather
    # than this runtime. That is a different HTTP server, so it cannot stand in
    # for a vNext end-to-end run; calling it ``tested`` overstated what the
    # repository can show. Kept as its own label instead of being flattened into
    # ``tested`` or ``experimental``, because neither of those is the truth.
    TESTED_LEGACY = "tested_legacy"
    EXPERIMENTAL = "experimental"
    PROTOCOL_COMPATIBLE = "protocol_compatible"
    PLANNED = "planned"


_VALID_STATUSES = frozenset(
    {
        BridgeStatus.TESTED,
        BridgeStatus.TESTED_LEGACY,
        BridgeStatus.EXPERIMENTAL,
        BridgeStatus.PROTOCOL_COMPATIBLE,
        BridgeStatus.PLANNED,
    }
)

# Statuses that must name the versions their evidence was recorded against.
_EVIDENCE_BACKED = frozenset({BridgeStatus.TESTED, BridgeStatus.TESTED_LEGACY})


@dataclass(frozen=True)
class BridgeProfile:
    """Declarative description of one external hosted client.

    A profile is metadata, not an integration: it records how a client is
    expected to connect and an honest status that reflects real test coverage.
    KaroX never invents a ``tested`` status for a product without an E2E.
    """

    name: str
    transport: str
    status: str
    auth_scheme: str = "bearer"
    description: str = ""
    tunnel: str = ""
    persistent_url: bool = False
    instructions: str = ""
    limitations: tuple[str, ...] = ()
    verified_versions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _SAFE_NAME.fullmatch(self.name) is None:
            raise ValueError("bridge profile name must contain 1-64 safe characters")
        if self.transport not in {"streamable_http", "openapi", "stdio"}:
            raise ValueError(
                "bridge transport must be streamable_http, openapi, or stdio"
            )
        if self.status not in _VALID_STATUSES:
            raise ValueError(
                "bridge status must be tested, tested_legacy, experimental, "
                "protocol_compatible, or planned"
            )
        if self.status == BridgeStatus.PLANNED and self.transport == "stdio":
            raise ValueError("planned bridge profiles cannot use stdio")
        if not isinstance(self.auth_scheme, str) or self.auth_scheme not in {
            "bearer",
            "api_key",
            "oauth",
            "none",
        }:
            raise ValueError("bridge auth scheme is invalid")
        if not isinstance(self.limitations, tuple) or not all(
            isinstance(item, str) and item for item in self.limitations
        ):
            raise ValueError("bridge limitations must be non-empty strings")
        if not isinstance(self.verified_versions, tuple) or not all(
            isinstance(item, str) and item for item in self.verified_versions
        ):
            raise ValueError("bridge verified versions must be non-empty strings")
        if self.status in _EVIDENCE_BACKED and not self.verified_versions:
            raise ValueError("tested bridge profiles require verified versions")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_usable(self) -> bool:
        return self.status in {
            BridgeStatus.TESTED,
            BridgeStatus.TESTED_LEGACY,
            BridgeStatus.PROTOCOL_COMPATIBLE,
        }


# Declarative registry of known hosted clients. Statuses are honest:
# - Notion: protocol-compatible. The current Notion Custom Agent contract is
#   public HTTPS Streamable HTTP MCP with OAuth or header-based authentication.
# - Generic Streamable HTTP: protocol-compatible transport, client-dependent.
# - ChatGPT/Claude Web: OAuth/DCR/PKCE contract tested locally.
# - PromptQL: local OpenAPI/Core wire E2E, no recorded live product run.
# - Hyperagent: OAuth/DCR/PKCE path is contract-tested; a real external
#   workspace run is still pending.
def known_bridge_profiles() -> list[BridgeProfile]:
    return [
        BridgeProfile(
            name="notion",
            transport="streamable_http",
            status=BridgeStatus.PROTOCOL_COMPATIBLE,
            auth_scheme="oauth",
            description="Notion Custom Agent OAuth bridge to the local KaroX runtime.",
            tunnel="tailscale funnel or stable public HTTPS",
            persistent_url=True,
            instructions=(
                "Configure the Notion Custom Agent MCP endpoint to the stable KaroX "
                "bridge URL. Notion discovers KaroX OAuth metadata and registers "
                "itself through Dynamic Client Registration; complete the KaroX "
                "approval page when Notion opens it."
            ),
            limitations=(
                "Notion custom MCP connections require a workspace where custom MCP "
                "servers are enabled by an administrator.",
                "OAuth uses authorization-code + PKCE S256, Dynamic Client Registration, "
                "resource indicators, and rotating refresh tokens.",
                "Use a stable public HTTPS URL for a saved Notion connection.",
                "The current KaroX OAuth wire is covered end to end; no live Notion "
                "Custom Agent run against this runtime is recorded yet.",
            ),
        ),
        BridgeProfile(
            name="generic-streamable-http",
            transport="streamable_http",
            status=BridgeStatus.PROTOCOL_COMPATIBLE,
            description="Any Streamable HTTP MCP client over an authenticated tunnel.",
            tunnel="tailscale funnel or stable public HTTPS",
            persistent_url=False,
            instructions=(
                "Point a Streamable HTTP MCP client at the bridge URL and supply "
                "the bridge credential in the Authorization header."
            ),
            limitations=("Behavior depends on the remote client implementation.",),
        ),
        BridgeProfile(
            name="chatgpt-web",
            transport="streamable_http",
            status=BridgeStatus.EXPERIMENTAL,
            auth_scheme="oauth",
            description=(
                "OAuth remote MCP bridge from ChatGPT Web to selected KaroX tools."
            ),
            tunnel="public HTTPS URL or supported secure MCP tunnel",
            persistent_url=True,
            instructions=(
                "Publish the bridge on a stable HTTPS URL, then add its /mcp URL "
                "as a custom MCP app in ChatGPT developer mode. Complete the "
                "KaroX password approval page when ChatGPT starts OAuth."
            ),
            limitations=(
                "OAuth/DCR/PKCE is covered locally; no live ChatGPT workspace run is recorded.",
                "Dynamic clients, pending approvals, authorization codes, and refresh grants persist for the same stable MCP resource; changing the public origin requires reconnection.",
            ),
        ),
        BridgeProfile(
            name="claude-web",
            transport="streamable_http",
            status=BridgeStatus.EXPERIMENTAL,
            auth_scheme="oauth",
            description=(
                "OAuth remote MCP bridge from Claude Web to selected KaroX tools."
            ),
            tunnel="public HTTPS URL",
            persistent_url=True,
            instructions=(
                "Publish the bridge on a stable HTTPS URL, add its /mcp URL under "
                "Claude Settings > Connectors, and complete the KaroX password "
                "approval page."
            ),
            limitations=(
                "OAuth/DCR/PKCE is covered locally; no live Claude account run is recorded.",
                "Dynamic clients, pending approvals, authorization codes, and refresh grants persist for the same stable MCP resource; changing the public origin requires reconnection.",
            ),
        ),
        BridgeProfile(
            name="adapt",
            transport="streamable_http",
            status=BridgeStatus.PROTOCOL_COMPATIBLE,
            auth_scheme="bearer",
            description=(
                "Adapt Custom Integration bridge to selected KaroX tools over "
                "authenticated Streamable HTTP MCP."
            ),
            tunnel="stable public HTTPS URL or supported secure MCP tunnel",
            persistent_url=True,
            instructions=(
                "In Adapt create a Custom Integration named KaroX, describe the "
                "stable /mcp endpoint, and store the KaroX Authorization value "
                "under the protected KAROX_AUTHORIZATION credential key."
            ),
            limitations=(
                "Adapt exposes KaroX through its generic Custom Integration surface; "
                "KaroX can verify the bridge locally, while the Adapt side is proven "
                "only after Adapt actually calls the endpoint.",
            ),
        ),
        BridgeProfile(
            name="promptql",
            transport="openapi",
            status=BridgeStatus.EXPERIMENTAL,
            description="PromptQL hosted agent bridge to selected KaroX Core tools.",
            tunnel="tailscale funnel or stable public HTTPS",
            persistent_url=False,
            instructions=(
                "Store the bridge credential in the PromptQL connector's protected "
                "Bearer or X-API-Key field first, then import the bridge "
                "/openapi.json URL -- the schema itself requires that credential, "
                "and only the discovery document at / is public."
            ),
            limitations=(
                "The OpenAPI wire path is tested locally; no recorded live PromptQL run yet.",
            ),
        ),
        BridgeProfile(
            name="hyperagent",
            transport="streamable_http",
            status=BridgeStatus.EXPERIMENTAL,
            description="Legacy bearer Hyperagent bridge kept for compatibility.",
            tunnel="tailscale funnel or stable public HTTPS",
            persistent_url=False,
            instructions=(
                "Legacy profile only. New Hyperagent connections should use the "
                "hyperagent-web OAuth profile."
            ),
            limitations=(
                "This compatibility profile does not use the dedicated hosted OAuth flow.",
            ),
        ),
        BridgeProfile(
            name="hyperagent-web",
            transport="streamable_http",
            status=BridgeStatus.EXPERIMENTAL,
            auth_scheme="oauth",
            description=(
                "OAuth remote MCP bridge from Hyperagent to selected KaroX tools."
            ),
            tunnel="stable public HTTPS URL or supported secure MCP tunnel",
            persistent_url=True,
            instructions=(
                "Publish KaroX on a stable HTTPS URL, then in Hyperagent open "
                "Settings > Integrations and add a Custom MCP server using the "
                "/mcp URL. Use the server-provided OAuth flow when offered; KaroX "
                "publishes OAuth discovery metadata and Dynamic Client Registration. "
                "Complete approval only on the KaroX page."
            ),
            limitations=(
                "OAuth/DCR/PKCE and the redirect-host allowlist are covered "
                "locally; no live HyperAgent workspace run is recorded yet.",
                "Dynamic clients and grants are process-local and require "
                "reconnection after restart unless a state directory is in use.",
            ),
        ),
    ]


class BridgeRegistry:
    """Read-only registry of bridge profiles with honest status reporting."""

    def __init__(self, profiles: Optional[Iterable[BridgeProfile]] = None) -> None:
        items = list(profiles) if profiles is not None else known_bridge_profiles()
        self._by_name = {item.name: item for item in items}
        if len(self._by_name) != len(items):
            raise BridgeConfigurationError("bridge profile names must be unique")

    def list(self) -> list[BridgeProfile]:
        return sorted(self._by_name.values(), key=lambda item: item.name)

    def get(self, name: str) -> BridgeProfile:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise BridgeConfigurationError(f"unknown bridge profile: {name}") from exc

    def usable(self) -> list[BridgeProfile]:
        return [item for item in self.list() if item.is_usable]


def is_launchable_profile(name: str) -> bool:
    """The profiles the managed web-bridge launcher can serve."""
    return name in _TARGET_PROFILES


# The redirect hosts a strict profile will send authorization codes to. ``None``
# means "any HTTPS host" -- the permissive default chatgpt-web/claude-web and
# library callers rely on. hyperagent-web pins the one verified canonical host.
# The exact callback path is deliberately not hardcoded: the documented endpoint
# has moved, so the host is pinned and any path on it is accepted.
NOTION_OAUTH_CLIENT_HOSTS = frozenset(
    {"notion.so", "www.notion.so", "notion.com", "www.notion.com", "app.notion.com"}
)

PROFILE_REDIRECT_HOSTS: dict[str, Optional[frozenset[str]]] = {
    "notion": NOTION_OAUTH_CLIENT_HOSTS,
    "hyperagent-web": frozenset({"hyperagent.com"}),
}


def profile_redirect_hosts(profile: str) -> Optional[frozenset[str]]:
    """Return the pinned redirect-host allowlist for a strict profile, or None."""
    return PROFILE_REDIRECT_HOSTS.get(profile)


def profile_tailscale_https_port(profile: str) -> int:
    """Give parallel hosted clients distinct Funnel listeners on one device."""
    return {"notion": 8443, "hyperagent-web": 10000, "adapt": 10001}.get(profile, 443)


def web_bridge_mcp_path(profile: str) -> str:
    """Return the canonical public MCP resource path for a hosted client."""
    return "/mcp"


def web_bridge_mcp_endpoint(public_url: str, profile: str) -> str:
    return f"{public_url.rstrip('/')}{web_bridge_mcp_path(profile)}"


# --------------------------------------------------------------------------- #
# OS-keyring credentials                                                       #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class BridgeCredentialReference:
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _SAFE_NAME.fullmatch(self.name) is None:
            raise ValueError("bridge credential name must contain 1-64 safe characters")

    def __str__(self) -> str:
        return f"{_BRIDGE_REFERENCE_PREFIX}{self.name}"

    @classmethod
    def parse(cls, value: str) -> "BridgeCredentialReference":
        if not isinstance(value, str) or not value.startswith(_BRIDGE_REFERENCE_PREFIX):
            raise ValueError("bridge credential reference must use os-keyring:bridge/<name>")
        return cls(value[len(_BRIDGE_REFERENCE_PREFIX) :])


class BridgeCredentialMissing(CredentialError):
    """The reference resolved cleanly to "no such credential".

    Distinct from a plain :class:`CredentialError`, which also covers a backend
    that is momentarily unreadable. Callers that mint a replacement secret on
    absence must not do so on unavailability: for a durable saved profile that
    would silently invalidate the bearer token of an already-configured
    connector, so absence has to be proven rather than assumed.
    """


class BridgeCredentialStore:
    """Bridge credentials in a dedicated OS-keyring namespace.

    Credentials are high-entropy random tokens, distinct from provider and MCP
    secrets, and are never persisted in plaintext configuration. Rotation
    replaces the value; revocation deletes it.
    """

    TOKEN_BYTES = 32

    def __init__(self, backend: Optional[CredentialBackend] = None) -> None:
        self._backend = backend or KeyringBackend()

    @staticmethod
    def fingerprint(secret: str) -> str:
        return f"sha256:{hashlib.sha256(secret.encode('utf-8')).hexdigest()[:12]}"

    def generate(self) -> str:
        return secrets.token_urlsafe(self.TOKEN_BYTES)

    def set(self, name: str, secret: Optional[str] = None) -> dict[str, str]:
        reference = BridgeCredentialReference(name)
        value = secret if secret is not None else self.generate()
        if not isinstance(value, str) or not value:
            raise ValueError("bridge credential value must not be empty")
        if any(char in value for char in ("\x00", "\r", "\n")):
            raise ValueError("bridge credential contains invalid control characters")
        if len(value) > 65536:
            raise ValueError("bridge credential exceeds 65536 characters")
        try:
            self._backend.set(_BRIDGE_SERVICE, reference.name, value)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(
                f"cannot write bridge OS credential: {type(exc).__name__}"
            ) from exc
        result = {"reference": str(reference), "fingerprint": self.fingerprint(value)}
        if secret is None:
            result["secret"] = value
        return result

    def resolve(self, reference: str | BridgeCredentialReference) -> str:
        parsed = (
            reference
            if isinstance(reference, BridgeCredentialReference)
            else BridgeCredentialReference.parse(reference)
        )
        try:
            value = self._backend.get(_BRIDGE_SERVICE, parsed.name)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(
                f"cannot read bridge OS credential: {type(exc).__name__}"
            ) from exc
        if not isinstance(value, str) or not value:
            raise BridgeCredentialMissing(
                f"bridge credential reference does not exist: {parsed}"
            )
        return value

    def status(self, reference: str | BridgeCredentialReference) -> dict[str, str]:
        """Report availability and fingerprint without ever returning the secret."""
        parsed = (
            reference
            if isinstance(reference, BridgeCredentialReference)
            else BridgeCredentialReference.parse(reference)
        )
        try:
            value = self.resolve(parsed)
        except BridgeCredentialMissing:
            return {"status": "missing", "reference": str(parsed), "fingerprint": ""}
        except CredentialError:
            return {"status": "error", "reference": str(parsed), "fingerprint": ""}
        return {
            "status": "available",
            "reference": str(parsed),
            "fingerprint": self.fingerprint(value),
        }

    def rotate(self, name: str) -> dict[str, str]:
        """Rotate the credential, returning only a safe reference + fingerprint.

        The new secret is deliberately absent from the returned mapping so that
        library callers, ``_emit`` and any log capture can never leak it. The
        previous value stays valid if the keyring write raises, because the
        backend either replaces the entry atomically or leaves the old one in
        place -- there is no intermediate deleted state.
        """
        new_secret = self.generate()
        self.set(name, new_secret)
        return {
            "reference": str(BridgeCredentialReference(name)),
            "fingerprint": self.fingerprint(new_secret),
            "status": "rotated",
        }

    def delete(self, name: str) -> dict[str, str]:
        reference = BridgeCredentialReference(name)
        try:
            self._backend.delete(_BRIDGE_SERVICE, reference.name)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(
                f"cannot delete bridge OS credential: {type(exc).__name__}"
            ) from exc
        return {"reference": str(reference), "status": "revoked"}

    def doctor(self) -> dict[str, str]:
        if isinstance(self._backend, KeyringBackend):
            self._backend._module()
        return {"status": "ok", "backend": "os-keyring", "scope": "bridge"}


# --------------------------------------------------------------------------- #
# Durable session identity                                                     #
# --------------------------------------------------------------------------- #


def watchdog_dir() -> Path:
    """Where owner watchdog records live (shared with port ownership)."""
    return runtime_dir() / "web-bridge"


def saved_web_bridge_session_id(profile_name: str) -> str:
    """Return the durable session/keyring identity for one saved profile name.

    The saved profile name is the registry identity. Target profile, tool
    bundle, tunnel, and language are editable configuration; including any of
    them in the digest would strand the previous session and credential after an
    ordinary edit. This digest and its ``web-saved-`` prefix are compatibility
    contract with KaroX 5: the same name must always resolve to the same OS
    keyring account.
    """
    if not isinstance(profile_name, str) or not profile_name:
        raise ValueError("saved web bridge profile name must not be empty")
    digest = hashlib.sha256(profile_name.encode("utf-8")).hexdigest()[:24]
    return f"web-saved-{digest}"


def _legacy_saved_web_bridge_session_id(
    profile_name: str,
    target_profile: str,
) -> str:
    """Identity emitted by the first durable-profile implementation."""
    digest = hashlib.sha256(
        f"{target_profile}\0{profile_name}".encode("utf-8")
    ).hexdigest()[:24]
    return f"web-saved-{digest}"


def saved_web_bridge_session_candidates(profile_name: str) -> tuple[str, ...]:
    """Current identity followed by every legacy target-profile identity."""
    ordered = [saved_web_bridge_session_id(profile_name)]
    ordered.extend(
        _legacy_saved_web_bridge_session_id(profile_name, target)
        for target in WEB_BRIDGE_PROFILES
    )
    return tuple(dict.fromkeys(ordered))


def saved_web_bridge_identity_exists(profile_name: str) -> bool:
    """Return whether any current/legacy durable session or watchdog exists."""
    session_root = session_dir()
    watchdog_root = watchdog_dir()
    return any(
        (session_root / session_id / "session.json").exists()
        or (watchdog_root / f"{session_id}.json").exists()
        for session_id in saved_web_bridge_session_candidates(profile_name)
    )


# --------------------------------------------------------------------------- #
# Saved launch profiles (secret-free)                                          #
# --------------------------------------------------------------------------- #


class BridgeProfileError(ValueError):
    """A saved bridge profile is missing, malformed, or unsafe to use."""


def default_bridge_profile_path() -> Path:
    return config_dir() / "vnext" / "web-bridge-profiles.json"


def _profile_name(value: Any) -> str:
    name = str(value or "").strip()
    if not _SAFE_NAME.fullmatch(name):
        raise BridgeProfileError(
            "saved bridge profile names must use 1-64 letters, digits, dots, "
            "underscores, or hyphens"
        )
    return name


def _optional_https_origin(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    origin = value.strip().rstrip("/")
    parsed = urlsplit(origin)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise BridgeProfileError(
            "saved custom public URL must be an HTTPS origin without path, query, "
            "fragment, or user information"
        )
    return origin


def _json_boolean(value: dict[str, Any], name: str, default: bool = False) -> bool:
    raw = value.get(name, default)
    if not isinstance(raw, bool):
        raise BridgeProfileError(f"saved {name} must be boolean")
    return raw


def _json_string_tuple(value: dict[str, Any], name: str) -> tuple[str, ...]:
    raw = value.get(name, [])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise BridgeProfileError(f"saved {name} must be a string list")
    return tuple(raw)


def _command(value: Any) -> tuple[str, ...]:
    if (
        not isinstance(value, (list, tuple))
        or not value
        or len(value) > 100
        or not all(isinstance(item, str) and item for item in value)
    ):
        raise BridgeProfileError(
            "saved verification commands must contain 1-100 non-empty strings"
        )
    return tuple(value)


def _server_profile(value: Any) -> dict[str, Any]:
    """Validate one server-profile dict without importing the hosted runtime.

    KaroX 6 does not launch managed dev servers, but a saved profile written by
    KaroX 5 may still carry these recipes. They stay in the JSON untouched; only
    their shape is validated so a malformed entry cannot masquerade as a valid
    recipe if a future build consumes it again.
    """
    if not isinstance(value, dict):
        raise BridgeProfileError("saved server profile must be an object")
    argv = value.get("argv")
    if (
        not isinstance(argv, (list, tuple))
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
    ):
        raise BridgeProfileError(
            "saved server profile argv must contain non-empty strings"
        )
    name = value.get("name")
    if not isinstance(name, str) or not _SAFE_NAME.fullmatch(name):
        raise BridgeProfileError("saved server profile name is invalid")
    return dict(value)


def _project_entry(value: Any) -> dict[str, Any]:
    """Validate the security-relevant shape of one saved project entry.

    Canonicalization belongs to :class:`karox.project_registry.ProjectRegistry`
    at launch time. Loading a profile must not fail merely because a project
    directory is temporarily offline, so paths are preserved verbatim here.
    """
    if not isinstance(value, dict):
        raise BridgeProfileError("saved projects must be objects")
    project_id = value.get("project_id")
    path = value.get("path")
    if not isinstance(project_id, str) or not _PROJECT_ID.fullmatch(project_id):
        raise BridgeProfileError("saved project_id must be a safe identifier")
    if not isinstance(path, str) or not path.strip():
        raise BridgeProfileError("saved project path must be a non-empty string")
    return dict(value)


@dataclass(frozen=True)
class SavedBridgeProfile:
    """Only non-secret launch policy; credentials remain in the OS keyring."""

    name: str
    target_profile: str
    tools: tuple[str, ...]
    mcp_servers: tuple[str, ...] = ()
    repository: Optional[str] = None
    projects: tuple[dict[str, Any], ...] = ()
    default_project_id: Optional[str] = None
    verification_commands: tuple[tuple[str, ...], ...] = ()
    # Secret-free recipes kept for forward/backward JSON compatibility. KaroX 6
    # does not serve the hosted dev-server tools, so these are never launched.
    server_profiles: tuple[dict[str, Any], ...] = ()
    browser_external_https: bool = False
    browser_allowed_domains: tuple[str, ...] = ()
    browser_denied_domains: tuple[str, ...] = ()
    browser_headed: bool = False
    browser_user_takeover: bool = False
    browser_network_inspection: bool = False
    browser_payment_confirmation: bool = False
    browser_allowed_emails: tuple[str, ...] = ()
    browser_credential_refs: tuple[str, ...] = ()
    deadline_seconds: float = DEFAULT_BRIDGE_DEADLINE_SECONDS
    tunnel: str = "tailscale"
    public_url: Optional[str] = None
    language: str = "en"
    access_profile: AccessProfile = AccessProfile.READ_ONLY
    # Explicit autonomy switch. Elevated/Advanced capability alone is not Bypass;
    # only this flag authorizes autonomous destructive workspace deletion.
    bypass: bool = False
    port: int = 8765
    tunnel_timeout_seconds: float = 30.0
    # Unknown keys read from a newer/older KaroX build. They are written back
    # unchanged so an ordinary edit never destroys configuration this build does
    # not understand.
    extra: dict[str, Any] = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "name", _profile_name(self.name))
        target = self.target_profile
        if not isinstance(target, str) or not _SAFE_NAME.fullmatch(target):
            raise BridgeProfileError("saved target profile must be a safe profile name")
        if not self.tools or len(set(self.tools)) != len(self.tools) or not all(
            isinstance(item, str) and _SAFE_NAME.fullmatch(item) for item in self.tools
        ):
            raise BridgeProfileError(
                "saved bridge tools must be non-empty, unique, safe names"
            )
        if len(set(self.mcp_servers)) != len(self.mcp_servers) or not all(
            isinstance(item, str) and _MCP_SERVER_ID.fullmatch(item)
            for item in self.mcp_servers
        ):
            raise BridgeProfileError(
                "saved MCP server IDs must be unique safe identifiers"
            )
        commands = tuple(_command(value) for value in self.verification_commands)
        object.__setattr__(self, "verification_commands", commands)
        if "karox.checks.run" in self.tools and not commands:
            raise BridgeProfileError(
                "a profile exposing karox.checks.run needs at least one approved "
                "verification command"
            )
        profiles = tuple(_server_profile(value) for value in self.server_profiles)
        object.__setattr__(self, "server_profiles", profiles)
        seen: set[str] = set()
        for profile in profiles:
            name = str(profile["name"])
            if name in seen:
                raise BridgeProfileError(f"duplicate saved server profile: {name}")
            seen.add(name)
        projects = tuple(_project_entry(value) for value in self.projects)
        object.__setattr__(self, "projects", projects)
        if self.default_project_id is not None:
            if (
                not isinstance(self.default_project_id, str)
                or not _PROJECT_ID.fullmatch(self.default_project_id)
            ):
                raise BridgeProfileError("saved default_project_id is invalid")
            if projects and self.default_project_id not in {
                str(entry["project_id"]) for entry in projects
            }:
                raise BridgeProfileError(
                    "saved default_project_id is not in the project registry"
                )
            if not projects and self.repository is None:
                raise BridgeProfileError(
                    "an empty project registry cannot have a default project"
                )
        if self.repository is not None and not str(self.repository).strip():
            raise BridgeProfileError("saved repository path must not be empty")
        if self.tunnel not in _TUNNELS:
            raise BridgeProfileError(
                "saved bridge tunnel must be cloudflare, tailscale, or custom"
            )
        origin = _optional_https_origin(self.public_url)
        object.__setattr__(self, "public_url", origin)
        if self.tunnel == "custom" and origin is None:
            raise BridgeProfileError("saved custom tunnel requires a public URL")
        if self.tunnel != "custom" and origin is not None:
            raise BridgeProfileError(
                "saved public URL is only valid with the custom tunnel"
            )
        if self.language not in _LANGUAGES:
            raise BridgeProfileError("saved bridge language must be en or ru")
        if not isinstance(self.access_profile, AccessProfile):
            try:
                profile = AccessProfile(str(self.access_profile))
            except ValueError as exc:
                raise BridgeProfileError("saved access profile is invalid") from exc
            object.__setattr__(self, "access_profile", profile)
        if not 1 <= int(self.port) <= 65_535:
            raise BridgeProfileError("saved bridge port must be between 1 and 65535")
        if not 0.1 <= float(self.deadline_seconds) <= 3600.0:
            raise BridgeProfileError(
                "saved bridge deadline must be between 0.1 and 3600 seconds"
            )
        if not 1.0 <= float(self.tunnel_timeout_seconds) <= 300.0:
            raise BridgeProfileError(
                "saved tunnel timeout must be between 1 and 300 seconds"
            )
        if not isinstance(self.extra, dict):
            raise BridgeProfileError("saved bridge profile extra fields must be an object")

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value.pop("extra", None)
        value["access_profile"] = self.access_profile.value
        value["tools"] = list(self.tools)
        value["mcp_servers"] = list(self.mcp_servers)
        value["projects"] = [dict(item) for item in self.projects]
        value["verification_commands"] = [
            list(command) for command in self.verification_commands
        ]
        value["server_profiles"] = [dict(item) for item in self.server_profiles]
        value["browser_allowed_domains"] = list(self.browser_allowed_domains)
        value["browser_denied_domains"] = list(self.browser_denied_domains)
        value["browser_allowed_emails"] = list(self.browser_allowed_emails)
        value["browser_credential_refs"] = list(self.browser_credential_refs)
        # Preserve unknown fields verbatim. Known keys always win: the extra map
        # was built from keys that were not known at read time.
        for key, item in self.extra.items():
            if key not in _SAVED_PROFILE_FIELDS:
                value[key] = item
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "SavedBridgeProfile":
        if not isinstance(value, dict):
            raise BridgeProfileError("saved bridge profile must be a JSON object")
        extra = {
            key: item for key, item in value.items() if key not in _SAVED_PROFILE_FIELDS
        }
        try:
            tools = tuple(value["tools"])
            raw_projects = value.get("projects", [])
            if not isinstance(raw_projects, list):
                raise BridgeProfileError("saved projects must be a project list")
            projects = tuple(raw_projects)
            commands = tuple(
                _command(command) for command in value.get("verification_commands", [])
            )
            server_profiles = tuple(
                _server_profile(item) for item in value.get("server_profiles", [])
            )
            access_profile = AccessProfile(
                value.get("access_profile", AccessProfile.READ_ONLY.value)
            )
            # Legacy stores had no explicit bypass bit. Their elevated profile
            # was historically the Bypass toggle, so preserve that meaning on load.
            legacy_bypass = (
                "bypass" not in value and access_profile == AccessProfile.ELEVATED
            )
            bypass = (
                _json_boolean(value, "bypass") if "bypass" in value else legacy_bypass
            )
            return cls(
                name=value["name"],
                target_profile=value["target_profile"],
                tools=tools,
                mcp_servers=_json_string_tuple(value, "mcp_servers"),
                repository=value.get("repository"),
                projects=projects,
                default_project_id=value.get("default_project_id"),
                verification_commands=commands,
                server_profiles=server_profiles,
                browser_external_https=_json_boolean(value, "browser_external_https"),
                browser_allowed_domains=_json_string_tuple(
                    value, "browser_allowed_domains"
                ),
                browser_denied_domains=_json_string_tuple(
                    value, "browser_denied_domains"
                ),
                browser_headed=_json_boolean(value, "browser_headed"),
                browser_user_takeover=_json_boolean(value, "browser_user_takeover"),
                browser_network_inspection=_json_boolean(
                    value, "browser_network_inspection"
                ),
                browser_payment_confirmation=_json_boolean(
                    value, "browser_payment_confirmation"
                ),
                browser_allowed_emails=_json_string_tuple(
                    value, "browser_allowed_emails"
                ),
                browser_credential_refs=_json_string_tuple(
                    value, "browser_credential_refs"
                ),
                deadline_seconds=float(
                    value.get("deadline_seconds", DEFAULT_BRIDGE_DEADLINE_SECONDS)
                ),
                tunnel=value.get("tunnel", "tailscale"),
                public_url=value.get("public_url"),
                language=value.get("language", "en"),
                access_profile=access_profile,
                bypass=bypass,
                port=int(value.get("port", 8765)),
                tunnel_timeout_seconds=float(value.get("tunnel_timeout_seconds", 30.0)),
                extra=extra,
            )
        except (KeyError, TypeError, ValueError) as exc:
            if isinstance(exc, BridgeProfileError):
                raise
            raise BridgeProfileError(
                "saved bridge profile is missing a required or correctly typed field"
            ) from exc


class BridgeProfileStore:
    """Atomic JSON registry for secret-free bridge launch profiles."""

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = (path or default_bridge_profile_path()).expanduser().resolve()
        self._top_level_extra: dict[str, Any] = {}

    def _load(self) -> dict[str, SavedBridgeProfile]:
        self._top_level_extra = {}
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BridgeProfileError(
                f"cannot read saved bridge profiles from {self.path}"
            ) from exc
        if not isinstance(payload, dict) or payload.get("version") != BRIDGE_PROFILE_STORE_VERSION:
            raise BridgeProfileError("saved bridge profile store has an unknown format")
        raw_profiles = payload.get("profiles")
        if not isinstance(raw_profiles, list):
            raise BridgeProfileError("saved bridge profile store has no profile list")
        self._top_level_extra = {
            key: item
            for key, item in payload.items()
            if key not in {"version", "profiles"}
        }
        profiles: dict[str, SavedBridgeProfile] = {}
        for raw in raw_profiles:
            profile = SavedBridgeProfile.from_dict(raw)
            if profile.name in profiles:
                raise BridgeProfileError(
                    f"duplicate saved bridge profile: {profile.name}"
                )
            profiles[profile.name] = profile
        return profiles

    def _write(self, profiles: dict[str, SavedBridgeProfile]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload: dict[str, Any] = {
            "version": BRIDGE_PROFILE_STORE_VERSION,
            "profiles": [profiles[name].to_dict() for name in sorted(profiles)],
        }
        for key, item in self._top_level_extra.items():
            payload.setdefault(key, item)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
        )
        descriptor = os.open(
            str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            with contextlib.suppress(OSError):
                os.chmod(self.path, 0o600)
        finally:
            with contextlib.suppress(OSError):
                temporary.unlink()

    def list(self) -> tuple[SavedBridgeProfile, ...]:
        profiles = self._load()
        return tuple(profiles[name] for name in sorted(profiles))

    def get(self, name: str) -> SavedBridgeProfile:
        normalized = _profile_name(name)
        profiles = self._load()
        try:
            return profiles[normalized]
        except KeyError as exc:
            raise BridgeProfileError(
                f"saved bridge profile does not exist: {normalized}"
            ) from exc

    def put(
        self, profile: SavedBridgeProfile, *, replace_existing: bool = True
    ) -> SavedBridgeProfile:
        profiles = self._load()
        if not replace_existing and profile.name in profiles:
            raise BridgeProfileError(
                f"saved bridge profile already exists: {profile.name}"
            )
        profiles[profile.name] = profile
        self._write(profiles)
        return profile

    def delete(self, name: str) -> SavedBridgeProfile:
        normalized = _profile_name(name)
        profiles = self._load()
        try:
            removed = profiles.pop(normalized)
        except KeyError as exc:
            raise BridgeProfileError(
                f"saved bridge profile does not exist: {normalized}"
            ) from exc
        self._write(profiles)
        return removed

    def doctor(self) -> dict[str, Any]:
        try:
            profiles = self.list()
        except BridgeProfileError as exc:
            # Doctor is the recovery surface. Normal list/get/start paths stay
            # strict, but diagnostics must remain usable when a saved project
            # was moved or deleted and one profile can no longer load.
            return {
                "status": "degraded",
                "path": str(self.path),
                "secret_fields": [],
                "profile_count": None,
                "profiles": [],
                "error": str(exc)[:400],
            }
        return {
            "status": "ok",
            "path": str(self.path),
            "secret_fields": [],
            "profile_count": len(profiles),
            "profiles": [profile.name for profile in profiles],
        }


# KaroX 5 spellings remain importable so callers and on-disk data need no
# rename. The JSON file name ("web-bridge-profiles.json") is unchanged.
SavedWebBridgeProfile = SavedBridgeProfile
WebBridgeProfileStore = BridgeProfileStore
WebBridgeProfileError = BridgeProfileError
default_web_bridge_profile_path = default_bridge_profile_path


__all__ = [
    "BRIDGE_PROFILE_STORE_VERSION",
    "BRIDGE_REGISTRY_VERSION",
    "DEFAULT_BRIDGE_DEADLINE_SECONDS",
    "NOTION_OAUTH_CLIENT_HOSTS",
    "PROFILE_REDIRECT_HOSTS",
    "WEB_BRIDGE_PROFILES",
    "BridgeAccessDenied",
    "BridgeConfigurationError",
    "BridgeCredentialMissing",
    "BridgeCredentialReference",
    "BridgeCredentialStore",
    "BridgeError",
    "BridgeProfile",
    "BridgeProfileError",
    "BridgeProfileStore",
    "BridgeRegistry",
    "BridgeStatus",
    "SavedBridgeProfile",
    "SavedWebBridgeProfile",
    "WebBridgeProfileError",
    "WebBridgeProfileStore",
    "default_bridge_profile_path",
    "default_web_bridge_profile_path",
    "is_launchable_profile",
    "known_bridge_profiles",
    "profile_redirect_hosts",
    "profile_tailscale_https_port",
    "saved_web_bridge_identity_exists",
    "saved_web_bridge_session_candidates",
    "saved_web_bridge_session_id",
    "watchdog_dir",
    "web_bridge_mcp_endpoint",
    "web_bridge_mcp_path",
]