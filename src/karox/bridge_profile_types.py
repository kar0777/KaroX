"""Bridge profile catalog, status and public descriptor helpers."""

from __future__ import annotations

import re

from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional


BRIDGE_REGISTRY_VERSION = 1

DEFAULT_BRIDGE_DEADLINE_SECONDS = 600.0

BRIDGE_PROFILE_STORE_VERSION = 1

WEB_BRIDGE_PROFILES: tuple[str, ...] = (
    "chatgpt-web",
    "claude-web",
    "notion",
    "hyperagent-web",
    "adapt",
)

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

_TARGET_PROFILES = frozenset(WEB_BRIDGE_PROFILES)

NOTION_OAUTH_CLIENT_HOSTS = frozenset(
    {"notion.so", "www.notion.so", "notion.com", "www.notion.com", "app.notion.com"}
)

PROFILE_REDIRECT_HOSTS: dict[str, Optional[frozenset[str]]] = {
    "notion": NOTION_OAUTH_CLIENT_HOSTS,
    "hyperagent-web": frozenset({"hyperagent.com"}),
}


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