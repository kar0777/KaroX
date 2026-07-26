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
    EXPERIMENTAL = "experimental"
    PROTOCOL_COMPATIBLE = "protocol_compatible"
    PLANNED = "planned"


_VALID_STATUSES = frozenset(
    {BridgeStatus.TESTED, BridgeStatus.EXPERIMENTAL, BridgeStatus.PROTOCOL_COMPATIBLE, BridgeStatus.PLANNED}
)


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
                "bridge status must be tested, experimental, protocol_compatible, or planned"
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
        if self.status == BridgeStatus.TESTED and not self.verified_versions:
            raise ValueError("tested bridge profiles require verified versions")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_usable(self) -> bool:
        return self.status in {BridgeStatus.TESTED, BridgeStatus.PROTOCOL_COMPATIBLE}


# Declarative registry of known hosted clients.  Statuses are honest:
# - Notion: tested legacy integration covered by existing regression scripts.
# - Generic Streamable HTTP: protocol-compatible transport, client-dependent.
# - ChatGPT/Claude Web: OAuth/DCR/PKCE contract tested locally, no live account run.
# - PromptQL: local OpenAPI/Core wire E2E, no recorded live product run.
# - HyperAgent: experimental (no verified dedicated path in repository).
def known_bridge_profiles() -> List[BridgeProfile]:
    return [
        BridgeProfile(
            name="notion",
            transport="streamable_http",
            status=BridgeStatus.TESTED,
            description="Notion Custom Agent bridge to the local KaroX runtime.",
            tunnel="cloudflare or tailscale funnel",
            persistent_url=False,
            instructions=(
                "Configure the Notion Custom Agent MCP endpoint to the KaroX "
                "bridge URL and use the bridge credential as the bearer token."
            ),
            limitations=("Per-session key protects both the MCP and REST paths.",),
            verified_versions=("4.x",),
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
                "Dynamic clients and grants are process-local and require reconnection after restart.",
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
                "Dynamic clients and grants are process-local and require reconnection after restart.",
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
                "Import the bridge /openapi.json URL as a PromptQL connector and "
                "store the bridge credential in its protected Bearer or X-API-Key field."
            ),
            limitations=(
                "The OpenAPI wire path is tested locally; no recorded live PromptQL run yet.",
            ),
        ),
        BridgeProfile(
            name="hyperagent",
            transport="streamable_http",
            status=BridgeStatus.EXPERIMENTAL,
            description="HyperAgent hosted agent bridge.",
            tunnel="cloudflare or tailscale funnel",
            persistent_url=False,
            instructions="Connect HyperAgent to the bridge URL with the bridge credential.",
            limitations=("No verified dedicated path in the repository yet.",),
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
            raise CredentialError(f"bridge credential reference does not exist: {parsed}")
        return value

    def rotate(self, name: str) -> dict[str, str]:
        return self.set(name)

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
