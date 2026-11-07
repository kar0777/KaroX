"""Secure, session-bound MCP client for the KaroX Core Runtime."""

from __future__ import annotations

from .mcp_types import (  # noqa: F401
    REGISTRY_VERSION as REGISTRY_VERSION,
    _SAFE_ID as _SAFE_ID,
    _SAFE_TOOL as _SAFE_TOOL,
    _SAFE_ENV as _SAFE_ENV,
    _SAFE_HEADER as _SAFE_HEADER,
    _SAFE_SCHEME as _SAFE_SCHEME,
    _SECRET_NAME as _SECRET_NAME,
    _MCP_REFERENCE_PREFIX as _MCP_REFERENCE_PREFIX,
    _MCP_SERVICE as _MCP_SERVICE,
    _JSON_SERIALIZATION_ERRORS as _JSON_SERIALIZATION_ERRORS,
    _SCHEMA_TYPES as _SCHEMA_TYPES,
    McpFailureKind as McpFailureKind,
    McpError as McpError,
    McpConfigurationError as McpConfigurationError,
    McpAccessDenied as McpAccessDenied,
    McpProtocolError as McpProtocolError,
    McpTransportError as McpTransportError,
    McpTimeoutError as McpTimeoutError,
    McpRemoteToolError as McpRemoteToolError,
    McpUnknownOutcome as McpUnknownOutcome,
    McpCredentialReference as McpCredentialReference,
    McpProgress as McpProgress,
    McpCredentialStore as McpCredentialStore,
    _safe_identifier as _safe_identifier,
    _finite_number as _finite_number,
    _string_mapping as _string_mapping,
    _validate_url as _validate_url,
    _reject_json_constant as _reject_json_constant,
    _strict_json_clone as _strict_json_clone,
    _validate_schema as _validate_schema,
    _reject_persisted_credential as _reject_persisted_credential,
    McpServerRecord as McpServerRecord,
    McpToolDescriptor as McpToolDescriptor,
    _descriptor as _descriptor,
    _resolved_read_only_remote_names as _resolved_read_only_remote_names,
    _normalize_discovered_tools as _normalize_discovered_tools,
)
from .mcp_registry import (  # noqa: F401
    _atomic_json as _atomic_json,
    McpRegistry as McpRegistry,
    _canonical_digest as _canonical_digest,
    mcp_registry_digest as mcp_registry_digest,
    validate_mcp_selection_registry as validate_mcp_selection_registry,
)
from .mcp_transport import (  # noqa: F401
    _http_client_factory as _http_client_factory,
    streamable_http_transport as streamable_http_transport,
)

import json
import subprocess
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Dict, Iterable, Mapping, Optional

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from .credentials import CredentialError
from .models import Capability
from .security import child_process_environment, redact
from .sessions import SessionRecord




































































class McpClient:
    """Bounded MCP transport facade; one supervised connection per operation."""

    def __init__(
        self,
        registry: McpRegistry,
        credentials: Optional[McpCredentialStore] = None,
    ) -> None:
        self.registry = registry
        self.credentials = credentials or McpCredentialStore()

    def _credential(self, record: McpServerRecord) -> Optional[str]:
        if record.credential_ref is None:
            return None
        try:
            return self.credentials.resolve(record.credential_ref)
        except (CredentialError, TypeError, ValueError) as exc:
            raise McpConfigurationError(
                f"cannot resolve MCP credential for {record.server_id}: "
                f"{type(exc).__name__}"
            ) from exc

    @staticmethod
    def _validate_discovery_size(record: McpServerRecord, response: Any) -> None:
        try:
            payload = response.model_dump(mode="json", by_alias=True, exclude_none=True)
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except _JSON_SERIALIZATION_ERRORS as exc:
            raise McpProtocolError("MCP tool discovery response is not valid JSON") from exc
        if len(encoded) > record.max_message_bytes:
            raise McpProtocolError(
                "MCP tool discovery response exceeds the configured message limit"
            )

    @asynccontextmanager
    async def _session(
        self, record: McpServerRecord, repository: Path, secret: Optional[str] = None
    ) -> AsyncIterator[ClientSession]:
        if secret is None:
            secret = self._credential(record)
        read_timeout = timedelta(seconds=record.timeout_seconds)
        if record.transport == "stdio":
            environment = child_process_environment()
            environment.update(record.environment)
            if secret is not None:
                assert record.credential_target is not None
                environment[record.credential_target] = secret
            parameters = StdioServerParameters(
                command=record.command or "",
                args=list(record.args),
                env=environment,
                cwd=repository,
            )
            async with stdio_client(parameters, errlog=subprocess.DEVNULL) as streams:
                async with ClientSession(
                    streams[0], streams[1], read_timeout_seconds=read_timeout
                ) as session:
                    yield session
            return
        headers = dict(record.headers)
        if secret is not None:
            assert record.credential_target is not None
            value = secret
            if record.credential_scheme:
                value = f"{record.credential_scheme} {secret}"
            headers[record.credential_target] = value
        oauth_auth: Optional[httpx.Auth] = None
        if record.oauth:
            from .mcp_oauth import noninteractive_oauth_provider

            oauth_auth = noninteractive_oauth_provider(
                record.server_id,
                record.url or "",
                timeout_seconds=max(30.0, record.timeout_seconds),
            )
        async with streamable_http_transport(
            record.url or "",
            headers=headers,
            timeout_seconds=record.timeout_seconds,
            auth=oauth_auth,
        ) as streams:
            async with ClientSession(
                streams[0], streams[1], read_timeout_seconds=read_timeout
            ) as session:
                yield session

    async def _discover_once(
        self, record: McpServerRecord, repository: Path, secret: Optional[str]
    ) -> list[McpToolDescriptor]:
        with anyio.fail_after(record.timeout_seconds):
            async with self._session(record, repository, secret) as session:
                await session.initialize()
                response = await session.list_tools()
        self._validate_discovery_size(record, response)
        return _normalize_discovered_tools(
            record, (_descriptor(record, item) for item in response.tools)
        )

    @staticmethod
    def _classified(
        exc: Exception, *, mutation: bool, secrets: tuple[str, ...] = ()
    ) -> McpError:
        if isinstance(exc, McpError):
            return exc
        from .mcp_oauth import McpOAuthAuthorizationRequired

        if isinstance(exc, McpOAuthAuthorizationRequired):
            return McpAccessDenied(str(exc))
        message = str(redact(str(exc), secrets=secrets))
        if mutation:
            return McpUnknownOutcome(
                f"MCP mutating call outcome is unknown ({type(exc).__name__}): {message}"
            )
        if isinstance(exc, TimeoutError):
            return McpTimeoutError("MCP operation timed out")
        return McpTransportError(
            f"MCP transport failed ({type(exc).__name__}): {message}"
        )

    def discover_record(
        self, record: McpServerRecord, repository: Path
    ) -> list[McpToolDescriptor]:
        attempts = record.max_transport_retries + 1
        last: Optional[McpError] = None
        secret = self._credential(record)
        exact = (secret,) if secret else ()
        for _ in range(attempts):
            try:
                return anyio.run(
                    self._discover_once, record, repository.resolve(strict=True), secret
                )
            except Exception as exc:
                last = self._classified(exc, mutation=False, secrets=exact)
                if isinstance(
                    last,
                    (McpAccessDenied, McpConfigurationError, McpProtocolError),
                ):
                    break
        assert last is not None
        raise last

    def discover(
        self, server_id: str, repository: Path
    ) -> list[McpToolDescriptor]:
        return self.discover_record(self.registry.get(server_id), repository)

    async def _call_once(
        self,
        record: McpServerRecord,
        descriptor: McpToolDescriptor,
        arguments: Dict[str, Any],
        repository: Path,
        secret: Optional[str],
        progress: Optional[Callable[[McpProgress], None]] = None,
    ) -> dict[str, Any]:
        try:
            encoded = json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                allow_nan=False,
            ).encode("utf-8")
        except _JSON_SERIALIZATION_ERRORS as exc:
            raise McpProtocolError("MCP tool arguments are not valid strict JSON") from exc
        if len(encoded) > record.max_message_bytes:
            raise McpProtocolError("MCP tool arguments exceed the configured message limit")
        progress_callback: Optional[Callable[..., Any]] = None
        if progress is not None:

            async def _on_progress(
                value: float, total: Optional[float], message: Optional[str]
            ) -> None:
                progress(McpProgress(float(value), total, message))

            progress_callback = _on_progress
        with anyio.fail_after(record.timeout_seconds):
            async with self._session(record, repository, secret) as session:
                await session.initialize()
                listed = await session.list_tools()
                self._validate_discovery_size(record, listed)
                discovered = _normalize_discovered_tools(
                    record, (_descriptor(record, tool) for tool in listed.tools)
                )
                current = {
                    item.remote_name: item for item in discovered
                }.get(descriptor.remote_name)
                if current is None or current.schema_digest != descriptor.schema_digest:
                    raise McpAccessDenied(
                        "MCP tool schema changed; inspect and reselect the server"
                    )
                # Call with the server's advertised spelling. A KaroX bridge
                # advertises ``karox_repo_read_file`` while our normalized
                # identity stays ``karox.repo.read_file``; sending the dotted
                # alias made the SDK's client session warn 'Tool ... not listed
                # by server' and skip structured-content validation on every
                # normal KaroX-to-KaroX call. The alias is still accepted by
                # the server, so this changes only which spelling goes on the
                # wire, never which tool runs.
                listed_names = {tool.name for tool in listed.tools}
                call_name = descriptor.remote_name
                if call_name not in listed_names:
                    wire_spelling = call_name.replace(".", "_")
                    if wire_spelling in listed_names:
                        call_name = wire_spelling
                result = await session.call_tool(
                    call_name, arguments, progress_callback=progress_callback
                )
        try:
            payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            payload = redact(payload, secrets=(secret,) if secret else ())
            result_bytes = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except _JSON_SERIALIZATION_ERRORS as exc:
            raise McpProtocolError("MCP tool result is not valid strict JSON") from exc
        if len(result_bytes) > record.max_result_bytes:
            raise McpProtocolError("MCP tool result exceeds the configured result limit")
        if result.isError:
            # The server's own words are the useful part: without them the
            # agent only learns that something failed and retries blindly.
            detail = _error_result_text(payload)
            raise McpRemoteToolError(
                f"MCP tool {descriptor.name} returned an error result"
                + (f": {detail}" if detail else "")
            )
        return {
            "server_id": descriptor.server_id,
            "tool": descriptor.name,
            "result": payload,
            "bytes": len(result_bytes),
        }

    def call_record(
        self,
        record: McpServerRecord,
        descriptor: McpToolDescriptor,
        arguments: Dict[str, Any],
        repository: Path,
        progress: Optional[Callable[[McpProgress], None]] = None,
    ) -> dict[str, Any]:
        if (
            record.server_id != descriptor.server_id
            or record.namespace != descriptor.namespace
        ):
            raise McpAccessDenied("MCP server identity does not match the bound tool")
        attempts = (record.max_transport_retries + 1) if descriptor.read_only else 1
        last: Optional[McpError] = None
        secret = self._credential(record)
        exact = (secret,) if secret else ()
        for _ in range(attempts):
            try:
                return anyio.run(
                    self._call_once,
                    record,
                    descriptor,
                    dict(arguments),
                    repository.resolve(strict=True),
                    secret,
                    progress,
                )
            except Exception as exc:
                last = self._classified(
                    exc, mutation=descriptor.mutates, secrets=exact
                )
                if isinstance(
                    last,
                    (McpAccessDenied, McpConfigurationError, McpProtocolError, McpRemoteToolError),
                ):
                    break
        assert last is not None
        raise last

    def call(
        self,
        descriptor: McpToolDescriptor,
        arguments: Dict[str, Any],
        repository: Path,
        progress: Optional[Callable[[McpProgress], None]] = None,
    ) -> dict[str, Any]:
        return self.call_record(
            self.registry.get(descriptor.server_id),
            descriptor,
            arguments,
            repository,
            progress,
        )


def _error_result_text(payload: Mapping[str, Any], limit: int = 1200) -> str:
    """The text a server put into an ``isError`` result, flattened and bounded.

    ``payload`` is the already-redacted result document, so nothing here can
    reintroduce a credential.
    """

    parts: list[str] = []
    content = payload.get("content")
    for item in content if isinstance(content, list) else []:
        if isinstance(item, Mapping) and item.get("type") == "text":
            text = str(item.get("text") or "").strip()
            if text:
                parts.append(text)
    structured = payload.get("structuredContent")
    if not parts and isinstance(structured, Mapping) and structured:
        parts.append(json.dumps(structured, ensure_ascii=False, sort_keys=True))
    flat = " ".join(" ".join(parts).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def mcp_selection(
    server: McpServerRecord,
    tools: Iterable[McpToolDescriptor],
    decisions: Mapping[str, str],
    previous: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    items = list(tools)
    unknown = set(decisions).difference(item.remote_name for item in items)
    if unknown:
        raise ValueError(f"unknown MCP tool permission(s): {sorted(unknown)}")
    current_digest = mcp_registry_digest(server)
    old_tools = (
        previous.get("tools", {})
        if isinstance(previous, Mapping)
        and previous.get("registry_digest") == current_digest
        else {}
    )
    selected: dict[str, Any] = {}
    for item in items:
        permission = decisions.get(item.remote_name)
        old = old_tools.get(item.remote_name) if isinstance(old_tools, dict) else None
        if permission is None and isinstance(old, dict) and old.get("schema_digest") == item.schema_digest:
            permission = old.get("permission")
        if permission is None:
            permission = "ask"
        if permission not in {"allow", "ask", "deny"}:
            raise ValueError("MCP tool permission must be allow, ask, or deny")
        selected[item.remote_name] = {
            "name": item.name,
            "schema_digest": item.schema_digest,
            "permission": permission,
            "read_only": item.read_only,
        }
    return {
        "server_id": server.server_id,
        "namespace": server.namespace,
        "registry_digest": current_digest,
        "tools": selected,
    }


class McpRuntimeBinding:
    """Maps selected MCP descriptors onto dynamic Core tools."""

    def __init__(
        self,
        client: McpClient,
        repository: Path,
        descriptors: Iterable[McpToolDescriptor],
        servers: Iterable[McpServerRecord],
    ) -> None:
        self.client = client
        self.repository = repository.resolve(strict=True)
        items = list(descriptors)
        self._descriptors = {item.name: item for item in items}
        if len(self._descriptors) != len(items):
            raise McpProtocolError("MCP tool namespace collision")
        server_items = list(servers)
        self._servers = {item.server_id: item for item in server_items}
        if len(self._servers) != len(server_items):
            raise McpConfigurationError("duplicate bound MCP server IDs")
        for item in items:
            server = self._servers.get(item.server_id)
            if server is None or server.namespace != item.namespace:
                raise McpConfigurationError(
                    f"MCP tool {item.name} has no matching bound server"
                )

    def definitions(self) -> list[Any]:
        # Import lazily to keep Core's base layer importable without MCP startup.
        from .core import ToolDefinition

        return [
            ToolDefinition(
                item.name,
                item.description,
                Capability.MCP_CALL,
                item.mutates,
                item.input_schema,
                external_schema=True,
            )
            for item in sorted(self._descriptors.values(), key=lambda value: value.name)
        ]

    @staticmethod
    def _selection(record: SessionRecord, server_id: str) -> Optional[dict[str, Any]]:
        for item in record.mcp_servers:
            if isinstance(item, dict) and item.get("server_id") == server_id:
                return item
        return None

    def execute(
        self,
        name: str,
        arguments: Dict[str, Any],
        record: SessionRecord,
        progress: Optional[Callable[[McpProgress], None]] = None,
    ) -> dict[str, Any]:
        try:
            descriptor = self._descriptors[name]
        except KeyError as exc:
            raise McpAccessDenied(f"MCP tool is not bound: {name}") from exc
        selection = self._selection(record, descriptor.server_id)
        if selection is None:
            raise McpAccessDenied("MCP server is not selected for this session")
        # The binding may outlive a registry edit. Re-read the record at the
        # authorization boundary so an approved tool cannot run against stale
        # command, URL, credential, or transport configuration.
        current = self.client.registry.get(descriptor.server_id)
        validate_mcp_selection_registry(current, selection)
        tools = selection.get("tools")
        tool = tools.get(descriptor.remote_name) if isinstance(tools, dict) else None
        if not isinstance(tool, dict) or tool.get("schema_digest") != descriptor.schema_digest:
            raise McpAccessDenied("MCP tool schema is not selected for this session")
        permission = tool.get("permission")
        if permission != "allow":
            suffix = "explicit approval is required" if permission == "ask" else "tool is denied"
            raise McpAccessDenied(f"MCP tool {name} cannot run: {suffix}")
        if bool(tool.get("read_only")) != descriptor.read_only:
            raise McpAccessDenied("MCP tool mutation classification changed; reselect it")
        return self.client.call_record(
            current, descriptor, arguments, self.repository, progress
        )