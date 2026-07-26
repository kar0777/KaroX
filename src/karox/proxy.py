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
import hashlib
import os
from typing import Any, Dict, List, Optional, Sequence

from .core import CoreRuntime
from .mcp_client import (
    McpAccessDenied,
    McpClient,
    McpProtocolError,
    McpRuntimeBinding,
    McpServerRecord,
    McpToolDescriptor,
)
from .models import Capability, CoreCommand, Origin, OriginKind
from .policy import CapabilityPolicy
from .sessions import SessionStore


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
        sessions: SessionStore,
        session_id: str,
        allowed_server_ids: Sequence[str],
        *,
        policy: CapabilityPolicy,
        hosted_origin: Optional[Origin] = None,
        proxied_origin: Optional[Origin] = None,
        audit_path: Optional[Path] = None,
    ) -> None:
        if hosted_origin is None:
            hosted_origin = Origin(OriginKind.HOSTED_CLIENT, "bridge-proxy")
        if hosted_origin.kind is not OriginKind.HOSTED_CLIENT:
            raise ProxyAccessDenied("proxy origin must be a hosted client")
        if proxied_origin is None:
            proxied_origin = Origin(OriginKind.PROXIED_MCP, f"bridge-proxy-{session_id}")
        if proxied_origin.kind is not OriginKind.PROXIED_MCP:
            raise ProxyAccessDenied("external proxy origin must be proxied MCP")
        self.client = client
        self.repository = repository.resolve(strict=True)
        self.sessions = sessions
        self.session_id = session_id
        self.policy = policy
        self.hosted_origin = hosted_origin
        self.proxied_origin = proxied_origin
        self.audit_path = audit_path
        self._allowed = list(allowed_server_ids)
        if not self._allowed:
            raise ProxyAccessDenied("proxy allowlist must not be empty")
        if len(self._allowed) != len(set(self._allowed)):
            raise ProxyAccessDenied("proxy allowlist contains duplicate servers")
        self._binding: Optional[McpRuntimeBinding] = None
        self._descriptors: List[McpToolDescriptor] = []
        self._servers: List[McpServerRecord] = []
        self._session_revision: Optional[int] = None

    def _session_selection(self, server_id: str) -> dict[str, Any]:
        session = self.sessions.load(self.session_id)
        self.sessions.validate_repository(session, self.repository)
        if session.revoked:
            raise ProxyAccessDenied("session access has been revoked")
        for raw in session.mcp_servers:
            if isinstance(raw, dict) and raw.get("server_id") == server_id:
                return raw
        raise ProxyAccessDenied(f"server is not selected for this session: {server_id}")

    def _build(self) -> None:
        self.policy.require(self.hosted_origin, Capability.MCP_CALL)
        self.policy.require(self.proxied_origin, Capability.MCP_CALL)
        session = self.sessions.load(self.session_id)
        self.sessions.validate_repository(session, self.repository)
        if session.revoked:
            raise ProxyAccessDenied("session access has been revoked")
        if self._binding is not None and self._session_revision == session.revision:
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
        self._session_revision = session.revision

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

    def execute(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = 30.0,
    ) -> dict[str, Any]:
        """Run one proxied tool call under both authorization boundaries.

        The hosted-client policy (``MCP_CALL``) and the external-MCP server
        selection (explicit ``allow`` permission) must both permit the call.
        """
        self._build()
        assert self._binding is not None
        descriptor = next(
            (item for item in self._descriptors if item.name == tool_name), None
        )
        if descriptor is None:
            raise ProxyAccessDenied(f"MCP tool is not exposed: {tool_name}")
        if descriptor.mutates and not idempotency_key:
            raise ProxyAccessDenied("mutating proxy calls require an idempotency key")
        correlation = hashlib.sha256(
            f"{self.session_id}\0{tool_name}\0{idempotency_key or ''}".encode("utf-8")
        ).hexdigest()[:32]
        core = CoreRuntime(
            self.repository,
            self.policy,
            self.sessions,
            self.audit_path,
            mcp_binding=self._binding,
        )
        command = CoreCommand(
            name=tool_name,
            arguments=dict(arguments),
            session_id=self.session_id,
            origin=self.proxied_origin,
            correlation_id=f"proxy-{correlation}",
            idempotency_key=idempotency_key,
            deadline_seconds=deadline_seconds,
        )
        lease = None
        if descriptor.mutates:
            ttl = max(30.0, min(3600.0, float(deadline_seconds) + 10.0))
            lease = self.sessions.acquire(
                self.session_id, f"proxy-{os.getpid()}", ttl_seconds=ttl
            )
        try:
            result = core.execute(command, lease=lease)
            if result.idempotent_replay:
                # `data` is the stored outcome of the first call, so without this
                # the caller is told a mutation just happened when it was served
                # from the idempotency record and the working tree was not touched.
                return {**result.data, "idempotent_replay": True}
            return result.data
        except McpAccessDenied as exc:
            raise ProxyAccessDenied(str(exc)) from exc
        except McpProtocolError as exc:
            raise ProxyError(str(exc)) from exc
        finally:
            if lease is not None:
                self.sessions.release(lease)

    def allowed_server_ids(self) -> List[str]:
        return list(self._allowed)
