"""MCP proxy for hosted AI clients.

A hosted client (Notion, HyperAgent, PromptQL, or a generic Streamable HTTP
client) reaches the local runtime through an authenticated bridge.  When that
client needs external MCP tools, KaroX proxies only the *explicitly selected*
servers and tools -- never every installed MCP, and never with provider keys,
MCP credentials, or host environment attached.

The proxy sits in front of the Phase 5 ``McpClient`` and ``McpRuntimeBinding``
and enforces a second authorization boundary: both the hosted-client policy and
the external-MCP server selection must allow the call.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from .mcp_client import (
    McpAccessDenied,
    McpClient,
    McpProtocolError,
    McpRuntimeBinding,
    McpServerRecord,
    McpToolDescriptor,
)
from .models import Capability, Origin, OriginKind
from .policy import CapabilityPolicy
from .sessions import SessionRecord


class ProxyError(RuntimeError):
    pass


class ProxyAccessDenied(ProxyError, PermissionError):
    pass


@dataclass(frozen=True)
class ProxyToolDescriptor:
    """A secret-free descriptor exposed to a hosted client.

    Only the tool name, description, and input schema are forwarded.  Server
    credentials, command lines, URLs, and environment never leave the proxy.
    """

    name: str
    description: str
    input_schema: Dict[str, Any]
    read_only: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "read_only": self.read_only,
        }


class McpProxy:
    """Proxies a user-selected subset of session MCP servers to a hosted client.

    The proxy never auto-exposes every installed MCP.  The caller passes an
    explicit allowlist of ``server_id`` values that must already be selected in
    the session; only those servers' explicitly ``allow``-permitted tools become
    visible to the hosted client.
    """

    def __init__(
        self,
        client: McpClient,
        repository: Path,
        session: SessionRecord,
        allowed_server_ids: Sequence[str],
        *,
        hosted_origin: Optional[Origin] = None,
    ) -> None:
        if hosted_origin is None:
            hosted_origin = Origin(OriginKind.HOSTED_CLIENT, "bridge-proxy")
        if hosted_origin.kind is not OriginKind.HOSTED_CLIENT:
            raise ProxyAccessDenied("proxy origin must be a hosted client")
        self.client = client
        self.repository = repository.resolve(strict=True)
        self.session = session
        self.hosted_origin = hosted_origin
        self._allowed = list(dict.fromkeys(allowed_server_ids))
        if not self._allowed:
            raise ProxyAccessDenied("proxy allowlist must not be empty")
        self._binding: Optional[McpRuntimeBinding] = None
        self._descriptors: List[McpToolDescriptor] = []
        self._servers: List[McpServerRecord] = []
        self._policy: Optional[CapabilityPolicy] = None

    def _session_selection(self, server_id: str) -> dict[str, Any]:
        for raw in self.session.mcp_servers:
            if isinstance(raw, dict) and raw.get("server_id") == server_id:
                return raw
        raise ProxyAccessDenied(f"server is not selected for this session: {server_id}")

    def _build(self) -> None:
        if self._binding is not None:
            return
        seen: set[str] = set()
        servers: List[McpServerRecord] = []
        descriptors: List[McpToolDescriptor] = []
        for server_id in self._allowed:
            if server_id in seen:
                raise ProxyAccessDenied(f"duplicate server in proxy allowlist: {server_id}")
            seen.add(server_id)
            selection = self._session_selection(server_id)
            server = self.client.registry.get(server_id)
            discovered = {
                item.remote_name: item
                for item in self.client.discover_record(server, self.repository)
            }
            tools = selection.get("tools")
            if not isinstance(tools, dict):
                raise ProxyAccessDenied(f"server selection is malformed: {server_id}")
            for remote_name, stored in tools.items():
                if not isinstance(stored, dict):
                    continue
                if stored.get("permission") != "allow":
                    continue
                descriptor = discovered.get(remote_name)
                if descriptor is None:
                    raise ProxyAccessDenied(
                        f"allowed tool disappeared: {server_id}/{remote_name}"
                    )
                if (
                    stored.get("schema_digest") != descriptor.schema_digest
                    or stored.get("read_only") is not descriptor.read_only
                ):
                    raise ProxyAccessDenied(
                        f"allowed tool changed: {server_id}/{remote_name}"
                    )
                descriptors.append(descriptor)
            servers.append(server)
        if not descriptors:
            raise ProxyAccessDenied("proxy allowlist exposes no permitted tools")
        self._binding = McpRuntimeBinding(
            self.client, self.repository, descriptors, servers,
        )
        self._descriptors = descriptors
        self._servers = servers

    def descriptors(self) -> List[ProxyToolDescriptor]:
        """Return the secret-free tools visible to the hosted client."""
        self._build()
        assert self._descriptors is not None
        return [
            ProxyToolDescriptor(
                item.name, item.description, item.input_schema, item.read_only,
            )
            for item in self._descriptors
        ]

    def ensure_policy(self, policy: CapabilityPolicy) -> None:
        """Attach a hosted-client policy and verify the MCP_CALL capability."""
        decision = policy.require(self.hosted_origin, Capability.MCP_CALL)
        self._policy = policy

    def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        policy: Optional[CapabilityPolicy] = None,
    ) -> dict[str, Any]:
        """Run one proxied tool call under both authorization boundaries.

        The hosted-client policy (``MCP_CALL``) and the external-MCP server
        selection (explicit ``allow`` permission) must both permit the call.
        """
        self._build()
        assert self._binding is not None
        active = policy or self._policy
        if active is not None:
            active.require(self.hosted_origin, Capability.MCP_CALL)
        try:
            return self._binding.execute(
                tool_name, arguments, self.session,
            )
        except McpAccessDenied as exc:
            raise ProxyAccessDenied(str(exc)) from exc
        except McpProtocolError as exc:
            raise ProxyError(str(exc)) from exc

    def allowed_server_ids(self) -> List[str]:
        return list(self._allowed)
