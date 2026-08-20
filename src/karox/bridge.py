"""Bridge profiles and credentials for hosted-client access to KaroX.

A bridge connects an external hosted AI client (ChatGPT Web, Claude Web,
Notion, HyperAgent, PromptQL, or a generic Streamable HTTP client) to the local
KaroX runtime.  Each known
client is described by a declarative profile with an honest status; KaroX never
claims support for a product that lacks a real end-to-end test.

Bridge credentials are stored in a dedicated OS-keyring namespace, distinct from
provider and MCP secrets, and can be rotated or emergency-revoked without
touching other credential stores.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from dataclasses import asdict, dataclass
from typing import Any, Iterable, List, Optional

from .credentials import CredentialBackend, CredentialError, KeyringBackend


BRIDGE_REGISTRY_VERSION = 1
_BRIDGE_REFERENCE_PREFIX = "os-keyring:bridge/"
_BRIDGE_SERVICE = "KaroX/bridge"
_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


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
            "bearer", "api_key", "oauth", "none",
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
        # ``tested_legacy`` counts as usable, but not because of its legacy
        # evidence. Every such profile is a plain authenticated Streamable HTTP
        # or OpenAPI client, and that wire is covered end to end by this
        # runtime's own tests -- the same ground on which
        # ``protocol_compatible`` is usable. Withholding it would deny a
        # connection the transport tests actually support.
        return self.status in {
            BridgeStatus.TESTED,
            BridgeStatus.TESTED_LEGACY,
            BridgeStatus.PROTOCOL_COMPATIBLE,
        }


# Declarative registry of known hosted clients.  Statuses are honest:
# - Notion: protocol-compatible.  The old live evidence still belongs to the
#   legacy server/notion_gateway.py, but the current Notion Custom Agent contract
#   is public HTTPS Streamable HTTP MCP with OAuth or header-based authentication.
#   KaroX now uses the same OAuth 2.1 facade as its ChatGPT/Claude bridges:
#   protected-resource discovery, Dynamic Client Registration, PKCE S256, refresh
#   rotation, and a local KaroX approval page.  This is still protocol evidence,
#   not a claim that a live Notion product run has completed.
# - Generic Streamable HTTP: protocol-compatible transport, client-dependent.
# - ChatGPT/Claude Web: OAuth/DCR/PKCE contract tested locally, no live account run.
# - PromptQL: local OpenAPI/Core wire E2E, no recorded live product run.
# - Hyperagent: OAuth/DCR/PKCE path is contract-tested and wired into /connect;
#   a real external Hyperagent workspace run is still pending. The old bearer
#   profile remains only for backward compatibility.
def known_bridge_profiles() -> List[BridgeProfile]:
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
                "Use a stable public HTTPS URL for a saved Notion connection; a "
                "Cloudflare Quick Tunnel URL changes after restart.",
                "The current KaroX OAuth wire is covered end to end; no live Notion "
                "Custom Agent run against this runtime is recorded yet.",
            ),
        ),
        BridgeProfile(
            name="generic-streamable-http",
            transport="streamable_http",
            status=BridgeStatus.PROTOCOL_COMPATIBLE,
            description="Any Streamable HTTP MCP client over an authenticated tunnel.",
            tunnel="cloudflare or tailscale funnel",
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
            tunnel="cloudflare or tailscale funnel",
            persistent_url=False,
            instructions=(
                # /openapi.json now costs the same credential as calling a tool,
                # because the schema names every exposed tool and its path. The
                # credential therefore has to be in place before the import, not
                # after it: the old order described a fetch that is now a 401.
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
            tunnel="cloudflare or tailscale funnel",
            persistent_url=False,
            instructions=(
                "Legacy profile only. New Hyperagent connections should use the "
                "hyperagent-web OAuth profile from /connect."
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

    def list(self) -> List[BridgeProfile]:
        return sorted(self._by_name.values(), key=lambda item: item.name)

    def get(self, name: str) -> BridgeProfile:
        try:
            return self._by_name[name]
        except KeyError as exc:
            raise BridgeConfigurationError(f"unknown bridge profile: {name}") from exc

    def usable(self) -> List[BridgeProfile]:
        return [item for item in self.list() if item.is_usable]


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
        return cls(value[len(_BRIDGE_REFERENCE_PREFIX):])


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
    secrets, and are never persisted in plaintext configuration.  Rotation
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

    def rotate(self, name: str) -> dict[str, str]:
        """Rotate the credential, returning only a safe reference + fingerprint.

        The new secret is deliberately absent from the returned mapping so
        that library callers, ``_emit`` and any log capture can never leak it.
        The previous value stays valid if the keyring write raises, because the
        backend either replaces the entry atomically or leaves the old one in
        place -- there is no intermediate deleted state.

        Callers that must hand the value somewhere (the clipboard) should call
        :meth:`generate` followed by :meth:`set` so the secret never has to
        round-trip through a serialisable structure.
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
