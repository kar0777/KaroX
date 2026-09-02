"""Universal user-managed connections: MCP client targets and model providers.

KaroX keeps two connection families separate, because they are different things:

1. **MCP client target** -- an *external client* that connects **into** KaroX's
   bridge MCP server (ClickUp, Notion, ChatGPT Web, Claude Web, PromptQL, a
   generic Streamable HTTP client, or any custom one). KaroX is the MCP *server*
   here; the external client is the MCP *client*. This is the bridge surface in
   ``proxy_server.py`` / ``web_bridge_launcher.py``.

2. **Model provider** -- an *API that serves an LLM* (OpenAI, Anthropic,
   OpenRouter, Z.ai, a local model, or any custom OpenAI/Anthropic-compatible
   endpoint). KaroX is the *client* here. This is the ``registry.py`` surface.

The two never collapse into one entity.  A preset for either family only
pre-fills the universal form; built-in and custom connections run through the
same code path, so there is no ``if clickup`` / ``if notion`` / ``if custom``
branching.  Secrets are kept out of the JSON configuration and stored in the OS
keyring under a dedicated ``KaroX/connection`` namespace for MCP client targets;
model-provider secrets keep using the existing ``os-keyring:provider/<name>``
namespace through :class:`~karox.credentials.CredentialStore`.

Only transports and auth schemes the current MCP server actually enforces are
offered: Streamable HTTP (the MCP wire) and OpenAPI, with ``bearer`` / ``api_key``
/ ``custom_header`` / ``oauth`` / ``none`` (the last only on explicit opt-in).
No second MCP server is created -- OAuth is one auth adapter over the same
bridge, exactly as the bridge already implements it.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import time
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional
from urllib.parse import urlsplit

from .credentials import (
    CredentialBackend,
    CredentialError,
    CredentialReference,
    KeyringBackend,
)


CONNECTIONS_SCHEMA_VERSION = 1

# Auth schemes the bridge actually enforces.  ``bearer`` and ``oauth`` are the
# MCP Streamable HTTP wire; ``api_key`` and ``custom_header`` are the OpenAPI
# wire (X-API-Key / a named header); ``none`` is permitted only when the user
# explicitly opts in and is reported as unsafe by the test path.
CONNECTION_AUTH_SCHEMES: tuple[str, ...] = (
    "bearer",
    "api_key",
    "custom_header",
    "oauth",
    "none",
)
# Wires the bridge serves.  ``streamable_http`` is the MCP wire (``/mcp``);
# ``openapi`` is the REST wire (``/openapi.json``).
CONNECTION_TRANSPORTS: tuple[str, ...] = ("streamable_http", "openapi")
# Tunnel layers the managed launcher supports, plus ``local`` for a loopback
# URL that the user reaches directly without a tunnel.
CONNECTION_TUNNELS: tuple[str, ...] = ("cloudflare", "tailscale", "custom", "local")
URL_STABILITY: tuple[str, ...] = ("stable", "temporary")

_CONNECTION_REFERENCE_PREFIX = "os-keyring:connection/"
_CONNECTION_SERVICE = "KaroX/connection"
# The bridge MCP server validates bearer tokens against its own ``KaroX/bridge``
# keyring namespace (``BridgeCredentialStore``), not the connection store.  When
# an auto-setup connection (ClickUp) launches the real bridge, its card points
# at a bridge credential via this prefix, so a single ``resolve_connection_secret``
# dispatcher serves both connection-owned and bridge-owned secrets.
_BRIDGE_REFERENCE_PREFIX = "os-keyring:bridge/"

# Saved connections in this family can be launched as their own guarded KaroX
# Streamable HTTP bridge.  Keeping the list explicit prevents a generic launcher
# from accidentally claiming OAuth/OpenAPI presets whose wire/auth contracts are
# different.  ``adapt`` is intentionally absent: its TUI alias may reuse the
# existing ChatGPT bridge, so spawning a second bearer bridge would violate that
# ownership contract.
MANAGED_STREAMABLE_BEARER_PRESETS: frozenset[str] = frozenset(
    {"clickup", "generic-mcp", "custom", "web-agent", "ide"}
)

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_HEADER_NAME = re.compile(r"^[A-Za-z0-9-]+$")
_PATH = re.compile(r"^/[A-Za-z0-9._~!$&'()*+,;=:@%-]*$")
_RESERVED_HEADERS = frozenset(
    {"authorization", "proxy-authorization", "cookie", "set-cookie"}
)


class ConnectionError(RuntimeError):
    """A user-facing connection operation failed without exposing a secret."""


class ConnectionConfigurationError(ConnectionError):
    pass


def _safe_id(value: str, label: str) -> str:
    if not isinstance(value, str) or _NAME.fullmatch(value) is None:
        raise ConnectionConfigurationError(
            f"{label} must contain 1-128 safe alphanumeric characters"
        )
    return value


def _validate_base_url(value: str, label: str) -> str:
    parts = urlsplit(value)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ConnectionConfigurationError(
            f"{label} must be HTTP(S) without credentials, query, or fragment"
        )
    return value


def _validate_endpoint_path(value: str) -> str:
    if not isinstance(value, str) or _PATH.fullmatch(value) is None:
        raise ConnectionConfigurationError(
            "endpoint path must start with '/' and be a valid URL path"
        )
    return value


@dataclass(frozen=True)
class McpClientPreset:
    """Declarative description of one known MCP client target.

    A preset is metadata only: it pre-fills the universal connection form.  The
    ``runtime_profile`` maps the connection onto a :class:`~karox.bridge.BridgeProfile`
    name so the bridge runtime path is unchanged for built-in and custom targets
    alike -- a preset never carries its own copy of the connection code.
    """

    preset_id: str
    display_name: str
    status: str
    runtime_profile: str
    transport: str
    auth_scheme: str = "bearer"
    description: str = ""
    instructions: str = ""
    limitations: tuple[str, ...] = ()
    persistent_url: bool = False
    endpoint_path: str = "/mcp"
    header_name: str = ""
    header_prefix: str = ""
    tunnel_default: str = "cloudflare"
    custom_only: bool = False

    def __post_init__(self) -> None:
        _safe_id(self.preset_id, "preset ID")
        if not isinstance(self.display_name, str) or not self.display_name.strip():
            raise ConnectionConfigurationError("preset display name is required")
        if self.status not in {"stable", "experimental", "documentation_required"}:
            raise ConnectionConfigurationError("preset status is invalid")
        if self.transport not in CONNECTION_TRANSPORTS:
            raise ConnectionConfigurationError("preset transport is unsupported")
        if self.auth_scheme not in CONNECTION_AUTH_SCHEMES:
            raise ConnectionConfigurationError("preset auth scheme is unsupported")
        if self.tunnel_default not in CONNECTION_TUNNELS:
            raise ConnectionConfigurationError("preset tunnel default is unsupported")
        _validate_endpoint_path(self.endpoint_path)
        if self.header_name and _HEADER_NAME.fullmatch(self.header_name) is None:
            raise ConnectionConfigurationError("preset header name is invalid")
        if not isinstance(self.limitations, tuple) or not all(
            isinstance(item, str) and item for item in self.limitations
        ):
            raise ConnectionConfigurationError("preset limitations must be strings")


# The built-in catalog.  Ordering is load-bearing for the picker: generic and
# ClickUp first (the most common custom-client cases), then the known hosted
# clients, then the "Custom" entry that starts from a blank form.
def _mcp_client_presets() -> tuple[McpClientPreset, ...]:
    return (
        McpClientPreset(
            preset_id="generic-mcp",
            display_name="Generic Streamable HTTP MCP",
            status="stable",
            runtime_profile="generic-streamable-http",
            transport="streamable_http",
            auth_scheme="bearer",
            description="Any Streamable HTTP MCP client over an authenticated tunnel.",
            instructions=(
                "Point the client at the bridge /mcp URL and send the bridge "
                "credential in the Authorization: Bearer header."
            ),
            limitations=("Behavior depends on the remote client implementation.",),
        ),
        McpClientPreset(
            preset_id="clickup",
            display_name="ClickUp",
            status="experimental",
            runtime_profile="generic-streamable-http",
            transport="streamable_http",
            auth_scheme="bearer",
            description=(
                "ClickUp connected to KaroX as a Streamable HTTP MCP client. "
                "KaroX is the MCP server; ClickUp connects in over the public "
                "tunnel URL with a bearer credential."
            ),
            instructions=(
                "In ClickUp: App Center -> MCP Servers -> Connect an MCP Server. "
                "Paste the KaroX bridge URL (ending /mcp). Then set "
                "'Authentication Method' to 'Authorization header' -- the form "
                "preselects OAuth, and this bridge authenticates with a static "
                "bearer token and serves no OAuth discovery metadata, so the "
                "default is rejected with 'Authentication method not supported by "
                "this MCP Server'. 'Authorization header' asks for the token "
                "alone; paste the secret from this card. The bridge accepts it "
                "either bare or with a 'Bearer ' prefix, so it does not matter "
                "which form ClickUp puts on the wire."
            ),
            limitations=(
                # Recorded from a live ClickUp workspace on 2026-08-01: the form
                # preselects OAuth and validates it against the server before
                # saving, so the bearer profile is only reachable by changing the
                # dropdown. The dropdown offers three items -- OAuth,
                # "Authorization header", "No Authentication" -- so the working
                # choice is named rather than described. Evidence, not a guess.
                "ClickUp's connect form defaults to OAuth and verifies the choice "
                "against the server; this bearer profile requires setting the "
                "Authentication Method to 'Authorization header'.",
                "A Cloudflare quick tunnel URL and token are reissued on restart, "
                "so create a new ClickUp connector after the bridge restarts.",
            ),
        ),
        McpClientPreset(
            preset_id="notion",
            display_name="Notion Custom Agent",
            status="stable",
            runtime_profile="notion",
            transport="streamable_http",
            auth_scheme="oauth",
            description="Notion Custom Agent OAuth bridge to the local KaroX runtime.",
            instructions=(
                "Paste the stable KaroX /mcp URL into Notion. KaroX publishes OAuth "
                "protected-resource and authorization-server metadata and supports "
                "Dynamic Client Registration, so no static bearer token, Client ID, "
                "or Client Secret is entered in Notion. Complete the KaroX approval "
                "page when the OAuth flow opens."
            ),
            limitations=(
                "A stable public HTTPS URL is required for the OAuth callback flow.",
            ),
            persistent_url=True,
            tunnel_default="tailscale",
        ),
        McpClientPreset(
            preset_id="chatgpt-web",
            display_name="ChatGPT Web",
            status="experimental",
            runtime_profile="chatgpt-web",
            transport="streamable_http",
            auth_scheme="oauth",
            description=(
                "OAuth remote MCP bridge from ChatGPT Web to selected KaroX tools."
            ),
            instructions=(
                "Publish the bridge on a stable HTTPS URL, then add its /mcp URL "
                "as a custom MCP app in ChatGPT developer mode. Complete the "
                "KaroX password approval page when ChatGPT starts OAuth."
            ),
            limitations=(
                "OAuth/DCR/PKCE is covered locally; no live ChatGPT run is recorded.",
            ),
            persistent_url=True,
            tunnel_default="tailscale",
        ),
        McpClientPreset(
            preset_id="claude-web",
            display_name="Claude Web",
            status="experimental",
            runtime_profile="claude-web",
            transport="streamable_http",
            auth_scheme="oauth",
            description=(
                "OAuth remote MCP bridge from Claude Web to selected KaroX tools."
            ),
            instructions=(
                "Publish the bridge on a stable HTTPS URL, add its /mcp URL under "
                "Claude Settings > Connectors, and complete the KaroX password "
                "approval page."
            ),
            limitations=(
                "OAuth/DCR/PKCE is covered locally; no live Claude run is recorded.",
            ),
            persistent_url=True,
            tunnel_default="tailscale",
        ),
        McpClientPreset(
            preset_id="hyperagent-web",
            display_name="Hyperagent",
            status="experimental",
            runtime_profile="hyperagent-web",
            transport="streamable_http",
            auth_scheme="oauth",
            description=(
                "OAuth remote MCP bridge from Hyperagent to selected KaroX tools."
            ),
            instructions=(
                "In Hyperagent open Settings > Integrations and add a custom MCP "
                "server using the stable KaroX /mcp URL. Let Hyperagent use the "
                "server's OAuth discovery/Dynamic Client Registration flow and "
                "complete the KaroX password approval page when it opens."
            ),
            limitations=(
                "Hyperagent publicly supports custom MCP servers; the KaroX "
                "OAuth/DCR/PKCE wire contract is covered locally, but a recorded "
                "live Hyperagent workspace run is still pending.",
            ),
            persistent_url=True,
            tunnel_default="tailscale",
        ),
        McpClientPreset(
            preset_id="adapt",
            display_name="Adapt",
            status="stable",
            runtime_profile="generic-streamable-http",
            transport="streamable_http",
            auth_scheme="bearer",
            description=(
                "Adapt custom integration connected to KaroX over the stable "
                "Streamable HTTP MCP bridge."
            ),
            instructions=(
                "In Adapt open Settings -> Integrations -> Custom Integration. "
                "Name it KaroX and describe the MCP endpoint as the stable KaroX "
                "public URL ending in /mcp. Add credential key "
                "KAROX_AUTHORIZATION and paste the KaroX Authorization value "
                "(Bearer <bridge secret>) into its protected Value field. "
                "Personal scope is recommended unless the bridge is intentionally "
                "shared with the whole Adapt organization. Tell the Adapt agent to "
                "initialize a KaroX workstream before project-scoped work and never "
                "invent local filesystem paths."
            ),
            limitations=(
                "Adapt stores this as a custom integration rather than a dedicated "
                "KaroX connector, so the MCP endpoint is supplied in the integration "
                "description/instructions while the bearer value stays in Adapt's "
                "protected credential field.",
            ),
            persistent_url=True,
            tunnel_default="tailscale",
        ),
        McpClientPreset(
            preset_id="promptql",
            display_name="PromptQL",
            status="stable",
            runtime_profile="promptql",
            transport="openapi",
            auth_scheme="api_key",
            description="PromptQL hosted agent bridge to selected KaroX Core tools.",
            instructions=(
                "Store the bridge credential in the PromptQL connector's "
                "protected Bearer or X-API-Key field first, then import the "
                "bridge /openapi.json URL."
            ),
            limitations=(
                "The OpenAPI wire path is tested locally; no live PromptQL run yet.",
            ),
        ),
        McpClientPreset(
            preset_id="web-agent",
            display_name="Web agent (generic)",
            status="stable",
            runtime_profile="generic-streamable-http",
            transport="streamable_http",
            auth_scheme="bearer",
            description="A hosted web agent connecting over Streamable HTTP.",
            instructions=(
                "Give the web agent the bridge /mcp URL and the bearer credential."
            ),
        ),
        McpClientPreset(
            preset_id="ide",
            display_name="IDE / editor",
            status="experimental",
            runtime_profile="generic-streamable-http",
            transport="streamable_http",
            auth_scheme="bearer",
            description="An IDE or editor that speaks Streamable HTTP MCP.",
            instructions=(
                "Point the editor's MCP client at the local bridge URL "
                "(http://127.0.0.1:<port>/mcp) and the bearer credential."
            ),
            limitations=("Loopback only unless a tunnel is started.",),
            tunnel_default="local",
        ),
        McpClientPreset(
            preset_id="custom",
            display_name="Custom MCP client",
            status="stable",
            runtime_profile="generic-streamable-http",
            transport="streamable_http",
            auth_scheme="bearer",
            description="A custom external client connecting to KaroX's MCP server.",
            instructions="Fill in the transport, auth, and tunnel for your client.",
            custom_only=True,
        ),
    )


MCP_CLIENT_PRESETS: Dict[str, McpClientPreset] = {
    item.preset_id: item for item in _mcp_client_presets()
}


def mcp_client_preset(preset_id: str) -> McpClientPreset:
    try:
        return MCP_CLIENT_PRESETS[preset_id]
    except KeyError as exc:
        raise ConnectionConfigurationError(
            f"unknown MCP client preset: {preset_id}"
        ) from exc


def mcp_client_presets() -> tuple[McpClientPreset, ...]:
    return _mcp_client_presets()


def _new_connection_id() -> str:
    # Stable and unique: an opaque token rather than the user-supplied name, so
    # renaming a connection does not invalidate it and two connections never
    # collide on a display name.
    return f"c-{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True)
class McpClientTarget:
    """A saved MCP client target -- a recipe for connecting an external client in."""

    connection_id: str
    name: str
    preset_id: str
    transport: str
    endpoint_path: str
    auth_scheme: str
    tunnel: str
    runtime_profile: str
    url_stability: str = "temporary"
    public_url: Optional[str] = None
    header_name: str = ""
    header_prefix: str = ""
    description: str = ""
    instructions: str = ""
    credential_ref: Optional[str] = None
    credential_fingerprint: Optional[str] = None
    port: int = 8765
    created_at: float = 0.0
    updated_at: float = 0.0
    # B5. Whether this saved connection may be started for new work.
    #
    # Recorded gap, closed here rather than simulated in the UI: the registry
    # previously had exactly two states, present and absent, so the only way to
    # stop using a connection was to remove it -- which also removes its saved
    # secret. A "disabled" drawn over that model would survive until the next
    # read of the file and no longer.
    #
    # It is a field on the record the registry already writes, not a second
    # store. Absent in a file written before B5, which therefore reads as
    # enabled -- the only honest reading of a file that predates the question.
    enabled: bool = True
    # Shared Bypass mode preference (see access_mode.py) for connections whose
    # runtime is started from this record rather than from a saved web-bridge
    # profile -- ClickUp, PromptQL, generic and custom MCP clients. Same
    # additive discipline as ``enabled``: absent in an older file, which
    # therefore reads OFF, and never coerced.
    bypass: bool = False

    def __post_init__(self) -> None:
        _safe_id(self.connection_id, "connection ID")
        if not isinstance(self.name, str) or not self.name.strip():
            raise ConnectionConfigurationError("connection name is required")
        if len(self.name) > 200 or any(ch in self.name for ch in "\r\n\x00"):
            raise ConnectionConfigurationError("connection name contains invalid characters")
        if self.preset_id not in MCP_CLIENT_PRESETS:
            raise ConnectionConfigurationError(f"unknown MCP client preset: {self.preset_id}")
        if self.transport not in CONNECTION_TRANSPORTS:
            raise ConnectionConfigurationError("connection transport is unsupported")
        _validate_endpoint_path(self.endpoint_path)
        if self.auth_scheme not in CONNECTION_AUTH_SCHEMES:
            raise ConnectionConfigurationError("connection auth scheme is unsupported")
        if self.tunnel not in CONNECTION_TUNNELS:
            raise ConnectionConfigurationError("connection tunnel is unsupported")
        if self.url_stability not in URL_STABILITY:
            raise ConnectionConfigurationError("connection URL stability is invalid")
        if self.runtime_profile and _NAME.fullmatch(self.runtime_profile) is None:
            raise ConnectionConfigurationError("connection runtime profile is invalid")
        if self.public_url is not None:
            _validate_base_url(self.public_url, "connection public URL")
        if self.header_name:
            if _HEADER_NAME.fullmatch(self.header_name) is None:
                raise ConnectionConfigurationError("connection header name is invalid")
            if self.header_name.lower() in _RESERVED_HEADERS and self.auth_scheme != "bearer":
                # Authorization is the bearer header, managed by the auth scheme.
                raise ConnectionConfigurationError(
                    "connection header name is reserved; use the auth scheme instead"
                )
        if self.header_prefix and not isinstance(self.header_prefix, str):
            raise ConnectionConfigurationError("connection header prefix is invalid")
        if any(ch in self.header_prefix for ch in "\r\n\x00"):
            raise ConnectionConfigurationError("connection header prefix contains control characters")
        if not isinstance(self.port, int) or isinstance(self.port, bool) or not 1 <= self.port <= 65535:
            raise ConnectionConfigurationError("connection port must be between 1 and 65535")
        if not isinstance(self.created_at, (int, float)) or self.created_at < 0:
            raise ConnectionConfigurationError("connection created_at is invalid")
        if not isinstance(self.updated_at, (int, float)) or self.updated_at < 0:
            raise ConnectionConfigurationError("connection updated_at is invalid")
        if not isinstance(self.enabled, bool):
            raise ConnectionConfigurationError(
                "connection enabled flag must be true or false"
            )
        if not isinstance(self.bypass, bool):
            raise ConnectionConfigurationError(
                "connection bypass flag must be true or false"
            )
        # OAuth requires a stable public URL by construction of the bridge; a
        # temporary cloudflare URL cannot complete the redirect dance, so the
        # form is prevented from saving that combination.
        if self.auth_scheme == "oauth" and self.url_stability != "stable":
            raise ConnectionConfigurationError(
                "OAuth MCP clients require a stable public URL"
            )
        # A credential reference is required for every authenticated scheme; the
        # value itself is never stored here, only the opaque reference.
        if self.auth_scheme != "none" and self.credential_ref is None:
            raise ConnectionConfigurationError(
                "this auth scheme requires a saved credential"
            )
        if self.credential_ref is not None:
            _validate_credential_ref(self.credential_ref)
        # ``none`` is only ever saved with an explicit opt-in flag at the form
        # layer; the data model records it but the test path reports it unsafe.
        if self.auth_scheme == "none" and self.credential_ref is not None:
            raise ConnectionConfigurationError("no-auth connections must not carry a credential")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "McpClientTarget":
        if not isinstance(value, dict):
            raise ConnectionConfigurationError("connection record must be an object")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(value).difference(allowed)
        if unknown:
            raise ConnectionConfigurationError(f"unknown connection fields: {sorted(unknown)}")
        return cls(
            connection_id=value["connection_id"],
            name=value["name"],
            preset_id=value["preset_id"],
            transport=value["transport"],
            endpoint_path=value["endpoint_path"],
            auth_scheme=value["auth_scheme"],
            tunnel=value["tunnel"],
            runtime_profile=value.get("runtime_profile", "generic-streamable-http"),
            url_stability=value.get("url_stability", "temporary"),
            public_url=value.get("public_url"),
            header_name=value.get("header_name", ""),
            header_prefix=value.get("header_prefix", ""),
            description=value.get("description", ""),
            instructions=value.get("instructions", ""),
            credential_ref=value.get("credential_ref"),
            credential_fingerprint=value.get("credential_fingerprint"),
            port=value.get("port", 8765),
            created_at=value.get("created_at", 0.0),
            updated_at=value.get("updated_at", 0.0),
            # Deliberately *not* ``bool(...)``. Coercing defeats the validation
            # boundary below: ``bool("false")`` is ``True``, so a corrupted or
            # hand-edited registry would silently read as enabled -- the one
            # direction a mistake must never fall, because it re-arms a
            # connection the user switched off. The raw value goes to
            # ``__post_init__``, which takes a JSON boolean and nothing else.
            enabled=value.get("enabled", True),
            # Same discipline, same reason: a missing key is the only default,
            # so a record written before the mode existed reads OFF.
            bypass=value.get("bypass", False),
        )

    @property
    def effective_url(self) -> Optional[str]:
        """The URL the external client should paste, when it is known.

        For ``custom``/``local`` tunnels the user provides a fixed public URL up
        front.  For cloudflare/tailscale the URL is not known until the bridge
        starts -- but once the launcher reports it back it is *saved onto the
        card*, and from that moment it is exactly as usable as a custom one.

        This used to gate on the tunnel kind instead of on the URL, so a saved
        ClickUp connection with a live ``public_url`` still answered ``None``:
        "Copy URL" claimed the URL was unknown while the card displayed it, and
        the connection test refused to run against an address it had.  Gating on
        the URL keeps the ephemeral case honest -- a cloudflare card with no URL
        yet is still ``None`` -- without denying one that has already been told.
        """
        if self.public_url:
            base = self.public_url.rstrip("/")
            return base if self.endpoint_path == "/" else f"{base}{self.endpoint_path}"
        return None


def _validate_credential_ref(reference: str) -> None:
    if not isinstance(reference, str):
        raise ConnectionConfigurationError("credential reference must be text")
    if reference.startswith(_CONNECTION_REFERENCE_PREFIX):
        name = reference[len(_CONNECTION_REFERENCE_PREFIX):]
        if _NAME.fullmatch(name) is None:
            raise ConnectionConfigurationError("connection credential name is invalid")
        return
    # An auto-setup connection (ClickUp) launches the real bridge server, which
    # validates bearer tokens against the ``KaroX/bridge`` keyring namespace
    # (``BridgeCredentialStore``).  Such a card carries an
    # ``os-keyring:bridge/<name>`` reference so the single ``resolve_connection_secret``
    # dispatcher reads the right store.  Accept that prefix here so the saved
    # connection can point at the live bridge credential.
    if reference.startswith(_BRIDGE_REFERENCE_PREFIX):
        name = reference[len(_BRIDGE_REFERENCE_PREFIX):]
        if _NAME.fullmatch(name) is None:
            raise ConnectionConfigurationError("bridge credential name is invalid")
        return
    # Allow the env-scheme so headless hosts without a keyring can still name a
    # saved connection secret, mirroring the provider credential store.
    try:
        CredentialReference.parse(reference)
    except ValueError as exc:
        raise ConnectionConfigurationError(str(exc)) from exc


@dataclass(frozen=True)
class ConnectionCredentialReference:
    name: str

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or _NAME.fullmatch(self.name) is None:
            raise ConnectionConfigurationError(
                "connection credential name must contain 1-128 safe characters"
            )

    def __str__(self) -> str:
        return f"{_CONNECTION_REFERENCE_PREFIX}{self.name}"

    @classmethod
    def parse(cls, value: str) -> "ConnectionCredentialReference":
        if not isinstance(value, str) or not value.startswith(_CONNECTION_REFERENCE_PREFIX):
            raise ConnectionConfigurationError(
                "connection credential reference must use os-keyring:connection/<name>"
            )
        return cls(value[len(_CONNECTION_REFERENCE_PREFIX):])


class ConnectionCredentialStore:
    """Saved connection secrets in a dedicated OS-keyring namespace.

    Distinct from :class:`~karox.credentials.CredentialStore` (providers) and
    :class:`~karox.bridge.BridgeCredentialStore` (per-session bridge tokens), so
    a saved MCP client target's reusable secret is rotated or revoked without
    touching any other credential.  As with the other stores, the value is never
    persisted in the JSON configuration -- only the opaque reference is.
    """

    TOKEN_BYTES = 32

    def __init__(self, backend: Optional[CredentialBackend] = None) -> None:
        self._backend = backend or KeyringBackend()

    @staticmethod
    def fingerprint(secret: str) -> str:
        import hashlib

        return f"sha256:{hashlib.sha256(secret.encode('utf-8')).hexdigest()[:12]}"

    def generate(self) -> str:
        return secrets.token_urlsafe(self.TOKEN_BYTES)

    def set(self, name: str, secret: Optional[str] = None) -> dict[str, str]:
        reference = ConnectionCredentialReference(name)
        value = secret if secret is not None else self.generate()
        if not isinstance(value, str) or not value:
            raise ConnectionConfigurationError("connection credential value must not be empty")
        if any(ch in value for ch in ("\x00", "\r", "\n")):
            raise ConnectionConfigurationError("connection credential contains control characters")
        if len(value) > 65536:
            raise ConnectionConfigurationError("connection credential exceeds 65536 characters")
        try:
            self._backend.set(_CONNECTION_SERVICE, reference.name, value)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(
                f"cannot write connection OS credential: {type(exc).__name__}"
            ) from exc
        result = {"reference": str(reference), "fingerprint": self.fingerprint(value)}
        if secret is None:
            result["secret"] = value
        return result

    def resolve(self, reference: str | ConnectionCredentialReference) -> str:
        parsed = (
            reference
            if isinstance(reference, ConnectionCredentialReference)
            else ConnectionCredentialReference.parse(reference)
        )
        try:
            value = self._backend.get(_CONNECTION_SERVICE, parsed.name)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(
                f"cannot read connection OS credential: {type(exc).__name__}"
            ) from exc
        if not isinstance(value, str) or not value:
            raise CredentialError(f"connection credential reference does not exist: {parsed}")
        return value

    def delete(self, name: str) -> dict[str, str]:
        reference = ConnectionCredentialReference(name)
        try:
            self._backend.delete(_CONNECTION_SERVICE, reference.name)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(
                f"cannot delete connection OS credential: {type(exc).__name__}"
            ) from exc
        return {"reference": str(reference), "status": "deleted"}

    def doctor(self) -> dict[str, str]:
        if isinstance(self._backend, KeyringBackend):
            self._backend._module()
        return {"status": "ok", "backend": "os-keyring", "scope": "connection"}


def managed_mcp_session_id(connection_id: str) -> str:
    """Return the durable repository-session ID owned by one managed MCP card."""

    _safe_id(connection_id, "connection ID")
    return f"mcp-{connection_id}"


def is_managed_streamable_bearer_target(target: "McpClientTarget") -> bool:
    """Whether ``target`` has the wire contract the generic launcher can serve."""

    runtime_matches = target.runtime_profile == "generic-streamable-http" or (
        target.preset_id == "clickup" and target.runtime_profile == "clickup"
    )
    return bool(
        target.preset_id in MANAGED_STREAMABLE_BEARER_PRESETS
        and runtime_matches
        and target.transport == "streamable_http"
        and target.auth_scheme == "bearer"
        and target.tunnel in {"cloudflare", "tailscale", "local"}
    )


def prepare_managed_mcp_binding(
    connection_id: str,
    repository: Path,
    *,
    secret: Optional[str] = None,
    bypass: bool = False,
    display_name: str = "Custom MCP client",
) -> dict[str, str]:
    """Bind a saved bearer MCP card to a guarded repository session and token.

    Manual connection credentials used to live only in ``KaroX/connection``.
    That is enough to *describe* a remote client, but not enough to launch a
    KaroX bridge later: a bridge also needs a durable repository-scoped session,
    and it validates its bearer against ``KaroX/bridge``.  This helper creates
    those two pieces together and returns only the opaque reference/fingerprint.

    The stable session ID is derived from the immutable connection ID, so a
    rename never changes the security boundary.  A retry after a failed registry
    write may find the rollback-revoked session; it is reactivated only for the
    same repository after repository validation.
    """

    from types import SimpleNamespace

    from .access_mode import provider_access_profile
    from .bridge import BridgeCredentialStore
    from .paths import session_dir
    from .sessions import SessionStore

    repository = repository.expanduser().resolve(strict=True)
    if not repository.is_dir():
        raise ConnectionConfigurationError(
            f"repository is not a directory: {repository}"
        )

    sid = managed_mcp_session_id(connection_id)
    sessions = SessionStore(session_dir())
    session_path = sessions.state_path(sid)
    access_profile = provider_access_profile(SimpleNamespace(bypass=bool(bypass)))
    created_or_reactivated = False

    if session_path.exists():
        record = sessions.load(sid)
        sessions.validate_repository(record, repository)
        if record.revoked or record.archived:
            sessions.reactivate(sid, repository, access_profile)
            created_or_reactivated = True
    else:
        sessions.create(
            repository,
            f"Managed MCP bridge: {display_name}",
            access_profile,
            session_id=sid,
            name=display_name,
        )
        created_or_reactivated = True

    bridge_store = BridgeCredentialStore()
    try:
        info = bridge_store.set(sid, secret)
    except Exception:
        if created_or_reactivated:
            try:
                sessions.revoke(sid)
            except Exception:
                pass
        raise

    # Never let a generated plaintext token escape this helper in a serializable
    # result.  Callers only need the reference and fingerprint; copy actions
    # resolve the value directly from the OS keyring when the user asks for it.
    return {
        "session_id": sid,
        "reference": info["reference"],
        "fingerprint": info["fingerprint"],
    }


def rollback_managed_mcp_binding(reference: Optional[str]) -> None:
    """Best-effort rollback for a bridge binding whose metadata was not saved."""

    if not reference or not reference.startswith(_BRIDGE_REFERENCE_PREFIX):
        return
    sid = reference[len(_BRIDGE_REFERENCE_PREFIX) :]
    if not sid.startswith("mcp-"):
        return
    try:
        from .bridge import BridgeCredentialStore

        BridgeCredentialStore().delete(sid)
    except Exception:
        pass
    try:
        from .paths import session_dir
        from .sessions import SessionStore

        sessions = SessionStore(session_dir())
        if sessions.state_path(sid).exists():
            record = sessions.load(sid)
            if not record.revoked:
                sessions.revoke(sid)
    except Exception:
        pass


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


class ConnectionRegistry:
    """File-backed, atomic, schema-versioned store of MCP client targets."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def _load(self) -> list[McpClientTarget]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ConnectionError(f"cannot read connection registry: {exc}") from exc
        if not isinstance(payload, dict):
            raise ConnectionError("connection registry is invalid")
        version = payload.get("schema_version")
        if version == CONNECTIONS_SCHEMA_VERSION:
            raw = payload.get("connections")
        elif version is None:
            # Migration from the pre-versioned first cut: the file held a bare
            # ``connections`` array.  Re-shape it in memory and persist on next
            # save; the source is left untouched until then.
            raw = payload.get("connections") if "connections" in payload else payload
        else:
            raise ConnectionError(
                f"connection registry has an unsupported schema (version {version})"
            )
        if not isinstance(raw, list):
            raise ConnectionError("connection registry connections must be an array")
        try:
            return [McpClientTarget.from_dict(item) for item in raw]
        except (KeyError, TypeError, ValueError) as exc:
            raise ConnectionError(f"connection registry is invalid: {exc}") from exc

    def _save(self, records: Iterable[McpClientTarget]) -> None:
        items = sorted(records, key=lambda item: item.name.lower())
        seen: set[str] = set()
        for item in items:
            if item.connection_id in seen:
                raise ConnectionError(
                    f"connection registry contains duplicate IDs: {item.connection_id}"
                )
            seen.add(item.connection_id)
        _atomic_json(
            self.path,
            {
                "schema_version": CONNECTIONS_SCHEMA_VERSION,
                "connections": [item.to_dict() for item in items],
            },
        )

    def list(self) -> list[McpClientTarget]:
        return sorted(self._load(), key=lambda item: item.name.lower())

    def get(self, connection_id: str) -> McpClientTarget:
        for item in self._load():
            if item.connection_id == connection_id:
                return item
        raise ConnectionError(f"connection does not exist: {connection_id}")

    def put(self, record: McpClientTarget) -> McpClientTarget:
        records = [item for item in self._load() if item.connection_id != record.connection_id]
        records.append(record)
        self._save(records)
        return record

    def set_bypass(self, connection_id: str, enabled: bool) -> McpClientTarget:
        """Persist the Bypass mode on one saved connection, changing nothing else.

        The endpoint, transport, tunnel, port, and credential reference are
        untouched, so switching the mode can never rotate a secret or move a
        URL. Nothing is started or stopped either: the record is what the next
        Start/Repair reads, and a live bridge keeps serving until somebody
        explicitly restarts it.
        """

        current = self.get(connection_id)
        if not isinstance(enabled, bool):
            raise ConnectionConfigurationError(
                "connection bypass flag must be true or false"
            )
        if current.bypass == enabled:
            # Idempotent, for the same reason ``set_enabled`` is.
            return current
        return self.put(replace(current, bypass=enabled))

    def set_enabled(self, connection_id: str, enabled: bool) -> McpClientTarget:
        """Persist the enabled flag on one saved connection, changing nothing else.

        B5. Disable is not delete and must not behave like it: this rewrites a
        single field on the record that already exists. The endpoint, the
        transport, the tunnel and the stored secret reference are all still
        there afterwards, which is exactly what lets re-enabling ask for
        nothing.

        It also does not touch a running process. Stopping one is a separate,
        visible decision the caller confirms with the user; a registry write
        that silently killed a live bridge would be the surprise this contract
        exists to prevent.
        """

        current = self.get(connection_id)
        if not isinstance(enabled, bool):
            raise ConnectionConfigurationError(
                "connection enabled flag must be true or false"
            )
        if current.enabled == enabled:
            # Idempotent by contract: setting the state it already has is not
            # an error and does not rewrite the file, so a double press cannot
            # churn `updated_at` or race a concurrent reader.
            return current
        return self.put(replace(current, enabled=enabled))

    def remove(self, connection_id: str) -> McpClientTarget:
        removed = self.get(connection_id)
        self._save([item for item in self._load() if item.connection_id != connection_id])
        return removed

    def doctor(self) -> dict[str, Any]:
        try:
            records = self.list()
        except ConnectionError as exc:
            return {"status": "error", "detail": str(exc)}
        return {
            "status": "ok",
            "count": len(records),
            "schema_version": CONNECTIONS_SCHEMA_VERSION,
        }


def connection_registry_path() -> Path:
    from .paths import config_dir

    return config_dir() / "vnext" / "connections.json"


def connection_registry() -> ConnectionRegistry:
    return ConnectionRegistry(connection_registry_path())


def remove_connection(
    connection_id: str,
    *,
    registry: Optional[ConnectionRegistry] = None,
    credentials: Optional[ConnectionCredentialStore] = None,
) -> McpClientTarget:
    """Remove a saved connection *and* its keyring secret in one operation.

    This is the single runtime path both the TUI and any future CLI use, so a
    deleted connection always takes its secret with it -- the registry removes
    only the secret-free metadata, and the credential store removes the value
    the metadata pointed at.  A missing or environment-named secret is left
    alone rather than raising.

    An auto-setup connection (ClickUp) points at a *bridge* credential in the
    ``os-keyring:bridge/<name>`` namespace, because the running bridge server
    validates bearer tokens against ``KaroX/bridge``.  That credential is
    removed through ``BridgeCredentialStore`` so the live secret disappears too,
    not only the metadata that named it.
    """
    reg = registry or connection_registry()
    removed = reg.remove(connection_id)
    ref = removed.credential_ref
    if ref and ref.startswith(_CONNECTION_REFERENCE_PREFIX):
        store = credentials or ConnectionCredentialStore()
        name = ref[len(_CONNECTION_REFERENCE_PREFIX):]
        try:
            store.delete(name)
        except CredentialError:
            pass
    elif ref and ref.startswith(_BRIDGE_REFERENCE_PREFIX):
        name = ref[len(_BRIDGE_REFERENCE_PREFIX):]
        if name.startswith("mcp-"):
            # Manual managed MCP cards own both this bridge credential and the
            # repository-scoped SessionStore record with the same name.  Revoke
            # them together so deleting a card cannot leave a reusable bridge
            # identity behind.
            rollback_managed_mcp_binding(ref)
        else:
            # Lazy import: ``bridge`` pulls uvicorn and the tool runtime, which the
            # connection module never imports otherwise. Importing at module load
            # would couple the connection registry to the bridge runtime for every
            # consumer, even ones that never start a bridge.
            from .bridge import BridgeCredentialStore

            try:
                BridgeCredentialStore().delete(name)
            except CredentialError:
                pass
    return removed


def resolve_connection_secret(
    target: "McpClientTarget",
    *,
    connection_store: Optional[ConnectionCredentialStore] = None,
    bridge_store: Optional[Any] = None,
) -> str:
    """Resolve a connection's secret from whichever store actually holds it.

    Connections the user fills in by hand store their secret in the dedicated
    ``KaroX/connection`` namespace.  An auto-setup connection that launches the
    real bridge stores its secret in ``KaroX/bridge`` (the server validates
    against that store), so its card carries an ``os-keyring:bridge/<name>``
    reference.  This dispatcher reads the right one and is the single place the
    TUI calls when it copies a secret or runs a handshake, keeping both
    connection families on one runtime path.
    """
    ref = target.credential_ref or ""
    if ref.startswith(_BRIDGE_REFERENCE_PREFIX):
        from .bridge import BridgeCredentialStore

        store = bridge_store or BridgeCredentialStore()
        return store.resolve(ref)
    store = connection_store or ConnectionCredentialStore()
    return store.resolve(ref)


def connection_test_endpoint(target: "McpClientTarget") -> Optional[str]:
    """Return the URL a connection test should contact, or ``None``.

    A saved connection is reachable at whichever address is actually known: the
    published URL once one exists (a ``custom`` tunnel has it from the start, a
    ``cloudflare``/``tailscale`` one after the launcher reports it back), and
    otherwise the loopback endpoint of a ``local`` bridge.  ``None`` means no
    address is known yet, which the caller reports as "start the bridge first".

    The TUI and the CLI both call this so a connection cannot be testable on one
    surface and untestable on the other.
    """
    if target.effective_url:
        return target.effective_url
    if target.tunnel == "local":
        return f"http://127.0.0.1:{target.port}{target.endpoint_path}"
    return None


def build_target_from_preset(
    preset_id: str,
    *,
    name: str,
    credential_ref: Optional[str] = None,
    credential_fingerprint: Optional[str] = None,
    tunnel: Optional[str] = None,
    public_url: Optional[str] = None,
    url_stability: str = "temporary",
    auth_scheme: Optional[str] = None,
    header_name: str = "",
    header_prefix: str = "",
    description: str = "",
    instructions: str = "",
    port: int = 8765,
    connection_id: Optional[str] = None,
) -> McpClientTarget:
    """Pre-fill a target from a preset and apply the user's overrides.

    This is the only place a preset turns into a record, so built-in and custom
    targets share one construction path: there is no per-preset branch anywhere
    else.  ``preset_id="custom"`` starts from a neutral bearer/Streamable HTTP
    baseline.
    """
    preset = mcp_client_preset(preset_id)
    now = time.time()
    return McpClientTarget(
        connection_id=connection_id or _new_connection_id(),
        name=name,
        preset_id=preset.preset_id,
        transport=preset.transport,
        endpoint_path=preset.endpoint_path,
        auth_scheme=auth_scheme or preset.auth_scheme,
        tunnel=tunnel or preset.tunnel_default,
        runtime_profile=preset.runtime_profile,
        url_stability=url_stability,
        public_url=public_url,
        header_name=header_name or preset.header_name,
        header_prefix=header_prefix or preset.header_prefix,
        description=description or preset.description,
        instructions=instructions or preset.instructions,
        credential_ref=credential_ref,
        credential_fingerprint=credential_fingerprint,
        port=port,
        created_at=now,
        updated_at=now,
    )


def mask_secret(secret: str, *, visible: int = 4) -> str:
    """Render a secret as ``••••••••abcd`` for display, never the full value."""
    if not secret:
        return ""
    tail = secret[-visible:] if len(secret) >= visible else secret[-1:]
    return f"{'•' * 8}{tail}"


def auth_headers(target: McpClientTarget, secret: str) -> dict[str, str]:
    """Build the auth headers an external client must send, for display and test.

    Mirrors what the bridge actually enforces: ``bearer`` -> ``Authorization:
    Bearer``; ``api_key`` -> ``X-API-Key``; ``custom_header`` -> the named header
    with an optional prefix; ``oauth``/``none`` -> no static header (OAuth is a
    flow, not a header; ``none`` is deliberately empty).
    """
    if target.auth_scheme == "bearer":
        return {"Authorization": f"Bearer {secret}"}
    if target.auth_scheme == "api_key":
        return {"X-API-Key": secret}
    if target.auth_scheme == "custom_header":
        name = target.header_name or "X-API-Key"
        value = f"{target.header_prefix}{secret}" if target.header_prefix else secret
        return {name: value}
    return {}


@dataclass(frozen=True)
class ClickupDefaults:
    """Auto-resolved effective configuration for a ClickUp preset connection.

    Every field the long form used to ask for is computed here so the user
    never has to fill it in: the transport/auth the bridge enforces for the
    preset, a free local port, the standard MCP endpoint path, the URL
    stability implied by the chosen tunnel, and whether the secret will be
    generated.  These are *defaults* -- the Advanced override screen can
    replace any of them, and ``overrides`` records only the ones the user
    actually changed so ``Reset to automatic`` clears them again.
    """

    transport: str
    auth_scheme: str
    tunnel: str
    port: int
    endpoint_path: str
    url_stability: str
    secret_source: str  # "generate" (auto) or "paste" (user supplied an Advanced secret)
    # Tunnel provisioning decisions, surfaced in Advanced but not asked in the flow:
    tunnel_reason: str  # why this tunnel was chosen (e.g. "active tailscale funnel")
    tunnel_action: str  # "none" | "install_cloudflared" | "login_tailscale" | "configure_custom"
    # The keys the user actually overrode in Advanced, as sorted pairs, so the
    # override screen can offer ``Reset to automatic`` (clear this and re-resolve).
    overrides: tuple[tuple[str, str], ...] = ()


# Fixed default the bridge uses everywhere; auto-setup only deviates to find a
# free one.  Kept here (not in web_bridge_launcher) so the resolver has no
# dependency on the launcher module for a constant the whole project shares.
_CLICKUP_DEFAULT_PORT = 8765


def resolve_clickup_defaults(
    environment: Optional[Mapping[str, Any]] = None,
    *,
    overrides: Optional[Mapping[str, str]] = None,
) -> ClickupDefaults:
    """Compute the effective configuration for a ClickUp connection.

    This mirrors the style of the rest of the project: a small pure function
    that turns the *current environment* into the values the bridge needs, so
    the TUI never asks for them.  ``environment`` is a snapshot the TUI takes
    once (tailscale ready + funnel active, cloudflared installed, an existing
    active tunnel) -- passing it in keeps this function pure and unit-testable
    without spawning processes.

    Tunnel selection, in priority order:
      1. an already-active tunnel the user has running (``active_tunnel``);
      2. a stable tailscale funnel when tailscale is ready and funnel-capable;
      3. an ephemeral cloudflare quick tunnel when ``cloudflared`` is installed;
      4. local-only when no tunnel is available (the card then offers to
         configure one).  ``tunnel_action`` says what the setup screen should
         prompt for next.
    """
    preset = mcp_client_preset("clickup")
    env = dict(environment or {})
    user = dict(overrides or {})

    transport = user.get("transport", preset.transport)
    auth_scheme = user.get("auth_scheme", preset.auth_scheme)
    endpoint_path = user.get("endpoint_path", preset.endpoint_path)

    # Port: respect an override, else find a free one near the project default.
    if user.get("port"):
        try:
            port = int(user["port"])
        except (TypeError, ValueError) as exc:
            raise ConnectionConfigurationError("port must be an integer") from exc
    else:
        port = _pick_free_port(env.get("port_probe"), _CLICKUP_DEFAULT_PORT)

    # Tunnel selection.  ``active_tunnel`` wins because reusing a running one
    # avoids orphaning a second URL the user would have to clean up.
    active_tunnel = env.get("active_tunnel")
    if active_tunnel in CONNECTION_TUNNELS:
        tunnel = active_tunnel
        tunnel_reason = "reusing the active tunnel"
        tunnel_action = "none"
    elif env.get("tailscale_ready") and env.get("tailscale_funnel_available"):
        tunnel = "tailscale"
        tunnel_reason = "stable tailscale funnel available"
        tunnel_action = "none"
    elif env.get("cloudflared_installed"):
        tunnel = "cloudflare"
        tunnel_reason = "ephemeral cloudflare tunnel available"
        tunnel_action = "none"
    elif env.get("tailscale_ready") is False or env.get("cloudflared_installed") is False:
        # A probe ran and the only tunnel providers are absent; fall back to
        # local and tell the setup screen to offer configuration.  cloudflared
        # is the lighter install, so prefer suggesting it over tailscale login.
        tunnel = "local"
        tunnel_reason = "no tunnel installed"
        tunnel_action = "install_cloudflared"
    else:
        # No probe data: default to cloudflare (the launcher will fail clearly
        # if cloudflared is missing, which the setup screen reports as an
        # actionable error rather than a silent local-only connection).
        tunnel = "cloudflare"
        tunnel_reason = "default tunnel (probe not run)"
        tunnel_action = "none"

    # OAuth presets need a stable URL; ClickUp is bearer so the preset is
    # temporary by default, stable only when a tailscale/custom tunnel gives a
    # durable host.  A user override always wins.
    if user.get("url_stability"):
        url_stability = user["url_stability"]
    elif tunnel in {"tailscale", "custom"}:
        url_stability = "stable"
    else:
        url_stability = "temporary"

    secret_source = "paste" if user.get("secret") else "generate"
    return ClickupDefaults(
        transport=transport,
        auth_scheme=auth_scheme,
        tunnel=tunnel,
        port=port,
        endpoint_path=endpoint_path,
        url_stability=url_stability,
        secret_source=secret_source,
        tunnel_reason=tunnel_reason,
        tunnel_action=tunnel_action,
        # Secret material controls only the source flag and is never retained in
        # the serializable/debuggable override metadata.
        overrides=tuple(sorted((key, value) for key, value in user.items() if key != "secret")),
    )


def _pick_free_port(probe: Any, default_port: int) -> int:
    """Pick a free local port at or above ``default_port``.

    ``probe`` is a callable returning ``True``/``False`` for "port is bindable"
    (the TUI passes the launcher's ``_port_is_available``).  When no probe is
    supplied (pure unit tests, headless hosts) the default port is used
    verbatim -- the launcher re-checks before binding and reports a clear
    "port in use" error if it is taken, so silence here is safe.
    """
    if probe is None:
        return default_port
    port = default_port
    # Try the default first, then walk up to default+100 before giving up; this
    # keeps the predictable port when free and only deviates on a real clash.
    while port < default_port + 100:
        try:
            if bool(probe(port)):
                return port
        except Exception:
            pass
        port += 1
    return default_port


def apply_clickup_overrides(
    defaults: ClickupDefaults, overrides: Mapping[str, str]
) -> ClickupDefaults:
    """Layer explicit Advanced values over one resolved environment snapshot.

    Environment probing is intentionally *not* repeated here.  Re-running the
    resolver with an empty environment used to turn a Tailscale/8766 automatic
    result into Cloudflare/8765 when the user changed only the endpoint path.
    Untouched fields now remain exactly as resolved; only explicit keys move.

    Secret material is never retained in ``ClickupDefaults.overrides``.  Older
    callers may still pass a ``secret`` key to indicate the source, but the value
    is discarded here and must travel separately to the setup orchestrator.
    """
    supplied = dict(overrides)
    base_overrides = dict(defaults.overrides)
    has_supplied_secret = bool(supplied.pop("secret", "")) or bool(
        base_overrides.pop("secret", "")
    )
    merged = {**base_overrides, **supplied}

    try:
        port = int(merged["port"]) if merged.get("port") else defaults.port
    except (TypeError, ValueError) as exc:
        raise ConnectionConfigurationError("port must be an integer") from exc

    tunnel = merged.get("tunnel", defaults.tunnel)
    resolved = ClickupDefaults(
        transport=merged.get("transport", defaults.transport),
        auth_scheme=merged.get("auth_scheme", defaults.auth_scheme),
        tunnel=tunnel,
        port=port,
        endpoint_path=merged.get("endpoint_path", defaults.endpoint_path),
        url_stability=merged.get("url_stability", defaults.url_stability),
        secret_source="paste" if has_supplied_secret else defaults.secret_source,
        tunnel_reason=(
            defaults.tunnel_reason
            if tunnel == defaults.tunnel
            else "user override"
        ),
        tunnel_action=(
            defaults.tunnel_action
            if tunnel == defaults.tunnel
            else "none"
        ),
        overrides=tuple(sorted((str(key), str(value)) for key, value in merged.items())),
    )

    if resolved.transport != "streamable_http":
        raise ConnectionConfigurationError(
            "the ClickUp preset currently serves Streamable HTTP MCP only"
        )
    if resolved.auth_scheme != "bearer":
        raise ConnectionConfigurationError(
            "the ClickUp preset currently supports Authorization header bearer auth only"
        )
    if resolved.tunnel == "custom":
        raise ConnectionConfigurationError(
            "custom tunnel setup is not available in the automatic ClickUp flow"
        )
    if resolved.url_stability == "stable" and resolved.tunnel != "tailscale":
        raise ConnectionConfigurationError(
            "a stable ClickUp URL currently requires the tailscale tunnel"
        )
    _validate_endpoint_path(resolved.endpoint_path)
    if not 1 <= resolved.port <= 65535:
        raise ConnectionConfigurationError("port must be between 1 and 65535")
    return resolved


__all__ = [
    "CONNECTIONS_SCHEMA_VERSION",
    "CONNECTION_AUTH_SCHEMES",
    "CONNECTION_TRANSPORTS",
    "CONNECTION_TUNNELS",
    "URL_STABILITY",
    "McpClientPreset",
    "McpClientTarget",
    "ConnectionRegistry",
    "ConnectionCredentialStore",
    "ConnectionCredentialReference",
    "ConnectionError",
    "ConnectionConfigurationError",
    "MCP_CLIENT_PRESETS",
    "mcp_client_preset",
    "mcp_client_presets",
    "build_target_from_preset",
    "connection_registry",
    "connection_registry_path",
    "remove_connection",
    "resolve_connection_secret",
    "mask_secret",
    "auth_headers",
    "ClickupDefaults",
    "resolve_clickup_defaults",
    "apply_clickup_overrides",
]
