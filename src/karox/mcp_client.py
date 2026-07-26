"""Secure, session-bound MCP client for the KaroX Core Runtime."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import subprocess
import uuid
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import timedelta
from enum import Enum
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Iterable, Mapping, Optional
from urllib.parse import urlsplit

import anyio
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
try:
    from mcp.client.streamable_http import streamable_http_client

    _MODERN_STREAMABLE_HTTP = True
except ImportError:  # compatibility for pre-1.27 development environments
    from mcp.client.streamable_http import streamablehttp_client as streamable_http_client

    _MODERN_STREAMABLE_HTTP = False

from .credentials import CredentialBackend, CredentialError, KeyringBackend
from .models import Capability
from .security import child_process_environment, contains_credential, redact
from .sessions import SessionRecord


REGISTRY_VERSION = 1
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SAFE_TOOL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_SAFE_ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
_SAFE_HEADER = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]{1,128}$")
_SAFE_SCHEME = re.compile(r"^(?:[A-Za-z][A-Za-z0-9+.-]{0,31})?$")
_SECRET_NAME = re.compile(
    r"(?i)(authorization|api[_-]?key|token|secret|password|credential|cookie)"
)
_MCP_REFERENCE_PREFIX = "os-keyring:mcp/"
_MCP_SERVICE = "KaroX/mcp"
_JSON_SERIALIZATION_ERRORS = (
    TypeError,
    ValueError,
    OverflowError,
    UnicodeError,
    RecursionError,
)
_SCHEMA_TYPES = frozenset(
    {"null", "boolean", "integer", "number", "string", "array", "object"}
)


class McpFailureKind(str, Enum):
    CONFIGURATION = "configuration"
    ACCESS = "access"
    PROTOCOL = "protocol"
    TRANSPORT = "transport"
    TIMEOUT = "timeout"
    REMOTE_TOOL = "remote_tool"
    UNKNOWN_OUTCOME = "unknown_outcome"


class McpError(RuntimeError):
    kind = McpFailureKind.TRANSPORT


class McpConfigurationError(McpError):
    kind = McpFailureKind.CONFIGURATION


class McpAccessDenied(McpError, PermissionError):
    kind = McpFailureKind.ACCESS


class McpProtocolError(McpError):
    kind = McpFailureKind.PROTOCOL


class McpTransportError(McpError):
    kind = McpFailureKind.TRANSPORT


class McpTimeoutError(McpError):
    kind = McpFailureKind.TIMEOUT


class McpRemoteToolError(McpError):
    kind = McpFailureKind.REMOTE_TOOL


class McpUnknownOutcome(McpError):
    kind = McpFailureKind.UNKNOWN_OUTCOME


@dataclass(frozen=True)
class McpCredentialReference:
    name: str

    def __post_init__(self) -> None:
        _safe_identifier(self.name, "MCP credential name")

    def __str__(self) -> str:
        return f"{_MCP_REFERENCE_PREFIX}{self.name}"

    @classmethod
    def parse(cls, value: str) -> "McpCredentialReference":
        if not isinstance(value, str) or not value.startswith(_MCP_REFERENCE_PREFIX):
            raise ValueError("MCP credential reference must use os-keyring:mcp/<name>")
        return cls(value[len(_MCP_REFERENCE_PREFIX) :])


class McpCredentialStore:
    """MCP-only credential namespace backed by the secure OS keyring."""

    def __init__(self, backend: Optional[CredentialBackend] = None) -> None:
        self._backend = backend or KeyringBackend()

    @staticmethod
    def _secret(value: str) -> str:
        if not isinstance(value, str) or not value:
            raise ValueError("MCP credential value must not be empty")
        if any(item in value for item in ("\x00", "\r", "\n")):
            raise ValueError("MCP credential contains invalid control characters")
        if len(value) > 65_536:
            raise ValueError("MCP credential exceeds 65536 characters")
        return value

    @staticmethod
    def fingerprint(secret: str) -> str:
        return f"sha256:{hashlib.sha256(secret.encode('utf-8')).hexdigest()[:12]}"

    def set(self, name: str, secret: str) -> dict[str, str]:
        reference = McpCredentialReference(name)
        value = self._secret(secret)
        try:
            self._backend.set(_MCP_SERVICE, reference.name, value)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(
                f"cannot write MCP OS credential: {type(exc).__name__}"
            ) from exc
        return {"reference": str(reference), "fingerprint": self.fingerprint(value)}

    def resolve(self, reference: str | McpCredentialReference) -> str:
        parsed = (
            reference
            if isinstance(reference, McpCredentialReference)
            else McpCredentialReference.parse(reference)
        )
        try:
            value = self._backend.get(_MCP_SERVICE, parsed.name)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(
                f"cannot read MCP OS credential: {type(exc).__name__}"
            ) from exc
        if not isinstance(value, str) or not value:
            raise CredentialError(f"MCP credential reference does not exist: {parsed}")
        return self._secret(value)

    def delete(self, name: str) -> dict[str, str]:
        reference = McpCredentialReference(name)
        try:
            self._backend.delete(_MCP_SERVICE, reference.name)
        except CredentialError:
            raise
        except Exception as exc:
            raise CredentialError(
                f"cannot delete MCP OS credential: {type(exc).__name__}"
            ) from exc
        return {"reference": str(reference), "status": "deleted"}

    def doctor(self) -> dict[str, str]:
        if isinstance(self._backend, KeyringBackend):
            self._backend._module()
        return {"status": "ok", "backend": "os-keyring", "scope": "mcp"}


def _safe_identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must contain 1-64 safe characters")
    return value


def _finite_number(value: Any, label: str, minimum: float, maximum: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or not minimum <= float(value) <= maximum
    ):
        raise ValueError(f"{label} must be between {minimum:g} and {maximum:g}")
    return float(value)


def _string_mapping(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    result: dict[str, str] = {}
    for name, item in value.items():
        if not isinstance(name, str) or not isinstance(item, str):
            raise ValueError(f"{label} names and values must be strings")
        result[name] = item
    return result


def _validate_url(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("MCP URL must be a non-empty string")
    parsed = urlsplit(value)
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("MCP URL must not contain user information")
    if parsed.query or parsed.fragment:
        raise ValueError("MCP URL must not contain a query or fragment")
    if not parsed.hostname or not parsed.path:
        raise ValueError("MCP URL must contain a host and path")
    loopback = parsed.hostname.lower() == "localhost"
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != "https" and not (parsed.scheme == "http" and loopback):
        raise ValueError("MCP URL must use HTTPS (HTTP is allowed only for loopback)")
    return value


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def _strict_json_clone(value: Any, label: str) -> Any:
    """Round-trip a value through strict JSON without accepting extensions."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return json.loads(encoded, parse_constant=_reject_json_constant)
    except _JSON_SERIALIZATION_ERRORS as exc:
        raise McpProtocolError(f"{label} is not valid strict JSON") from exc


def _validate_schema(schema: Any, label: str) -> None:
    """Validate the recursive JSON-Schema subset enforced by Core."""
    if not isinstance(schema, dict):
        raise McpProtocolError(f"{label} must be an object")
    if "type" in schema:
        declared = schema["type"]
        if isinstance(declared, str):
            type_names = [declared]
        elif (
            isinstance(declared, list)
            and declared
            and all(isinstance(item, str) for item in declared)
            and len(set(declared)) == len(declared)
        ):
            type_names = declared
        else:
            raise McpProtocolError(f"{label} type is malformed")
        unsupported = set(type_names).difference(_SCHEMA_TYPES)
        if unsupported:
            raise McpProtocolError(
                f"{label} uses unsupported types: {sorted(unsupported)}"
            )
    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, dict):
            raise McpProtocolError(f"{label} properties must be an object")
        for name, child in properties.items():
            if not isinstance(name, str):
                raise McpProtocolError(f"{label} property names must be strings")
            _validate_schema(child, f"{label}.properties[{name!r}]")
    if "required" in schema:
        required = schema["required"]
        if (
            not isinstance(required, list)
            or not all(isinstance(item, str) for item in required)
            or len(set(required)) != len(required)
        ):
            raise McpProtocolError(f"{label} required must contain unique strings")
    if "items" in schema:
        _validate_schema(schema["items"], f"{label}.items")
    if "additionalProperties" in schema:
        additional = schema["additionalProperties"]
        if not isinstance(additional, bool):
            _validate_schema(additional, f"{label}.additionalProperties")


def _reject_persisted_credential(value: Optional[str], label: str) -> None:
    if value is not None and contains_credential(value):
        raise ValueError(f"{label} must use an MCP credential reference")


@dataclass(frozen=True)
class McpServerRecord:
    server_id: str
    namespace: str
    transport: str
    command: Optional[str] = None
    args: tuple[str, ...] = ()
    url: Optional[str] = None
    environment: Dict[str, str] = field(default_factory=dict)
    headers: Dict[str, str] = field(default_factory=dict)
    credential_ref: Optional[str] = None
    credential_target: Optional[str] = None
    credential_scheme: str = "Bearer"
    read_only_tools: tuple[str, ...] = ()
    timeout_seconds: float = 30.0
    max_result_bytes: int = 1_000_000
    max_message_bytes: int = 1_000_000
    max_transport_retries: int = 1

    def __post_init__(self) -> None:
        _safe_identifier(self.server_id, "MCP server ID")
        _safe_identifier(self.namespace, "MCP namespace")
        if self.transport not in {"stdio", "streamable_http"}:
            raise ValueError("MCP transport must be stdio or streamable_http")
        if not isinstance(self.args, tuple) or not all(
            isinstance(item, str) and "\x00" not in item for item in self.args
        ):
            raise ValueError("MCP command arguments must be strings")
        if not isinstance(self.read_only_tools, tuple) or not all(
            isinstance(item, str) and _SAFE_TOOL.fullmatch(item)
            for item in self.read_only_tools
        ):
            raise ValueError("MCP read-only tool names are invalid")
        if len(set(self.read_only_tools)) != len(self.read_only_tools):
            raise ValueError("MCP read-only tool names must be unique")
        _reject_persisted_credential(self.command, "MCP command")
        _reject_persisted_credential(self.url, "MCP URL")
        for item in self.args:
            _reject_persisted_credential(item, "MCP command argument")
        environment = _string_mapping(self.environment, "MCP environment")
        headers = _string_mapping(self.headers, "MCP headers")
        for name, value in environment.items():
            if _SAFE_ENV.fullmatch(name) is None:
                raise ValueError(f"invalid MCP environment name: {name!r}")
            if _SECRET_NAME.search(name) or contains_credential(value):
                raise ValueError("secret-like MCP environment values require a credential reference")
            if any(char in value for char in ("\x00", "\r", "\n")):
                raise ValueError("MCP environment values contain control characters")
        for name, value in headers.items():
            if _SAFE_HEADER.fullmatch(name) is None:
                raise ValueError(f"invalid MCP header name: {name!r}")
            if _SECRET_NAME.search(name) or contains_credential(value):
                raise ValueError("secret-like MCP headers require a credential reference")
            if any(char in value for char in ("\x00", "\r", "\n")):
                raise ValueError("MCP header values contain control characters")
        if self.credential_ref is not None:
            McpCredentialReference.parse(self.credential_ref)
            if not isinstance(self.credential_target, str):
                raise ValueError("MCP credential target is required with a credential reference")
            if self.transport == "stdio":
                if _SAFE_ENV.fullmatch(self.credential_target) is None:
                    raise ValueError("MCP credential environment target is invalid")
                if self.credential_target in environment:
                    raise ValueError("MCP credential target collides with environment")
            else:
                if _SAFE_HEADER.fullmatch(self.credential_target) is None:
                    raise ValueError("MCP credential header target is invalid")
                if self.credential_target.lower() in {name.lower() for name in headers}:
                    raise ValueError("MCP credential target collides with headers")
        elif self.credential_target is not None:
            raise ValueError("MCP credential target requires a credential reference")
        if (
            not isinstance(self.credential_scheme, str)
            or _SAFE_SCHEME.fullmatch(self.credential_scheme) is None
        ):
            raise ValueError("MCP credential scheme is invalid")
        _finite_number(self.timeout_seconds, "MCP timeout", 0.1, 3600.0)
        for value, label in (
            (self.max_result_bytes, "MCP result limit"),
            (self.max_message_bytes, "MCP message limit"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1024 <= value <= 16_000_000:
                raise ValueError(f"{label} must be between 1024 and 16000000 bytes")
        if (
            isinstance(self.max_transport_retries, bool)
            or not isinstance(self.max_transport_retries, int)
            or not 0 <= self.max_transport_retries <= 5
        ):
            raise ValueError("MCP transport retries must be between 0 and 5")
        if self.transport == "stdio":
            if not isinstance(self.command, str) or not self.command or "\x00" in self.command:
                raise ValueError("stdio MCP server requires a command")
            if self.url is not None or headers:
                raise ValueError("stdio MCP server cannot define a URL or headers")
        else:
            if self.command is not None or self.args or environment:
                raise ValueError("HTTP MCP server cannot define command or environment")
            if self.url is None:
                raise ValueError("HTTP MCP server requires a URL")
            _validate_url(self.url)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Any) -> "McpServerRecord":
        if not isinstance(value, dict):
            raise ValueError("MCP server record must be an object")
        allowed = set(cls.__dataclass_fields__)
        unknown = set(value).difference(allowed)
        if unknown:
            raise ValueError(f"unknown MCP server fields: {sorted(unknown)}")
        missing = {"server_id", "namespace", "transport"}.difference(value)
        if missing:
            raise ValueError(f"missing MCP server fields: {sorted(missing)}")
        payload = dict(value)
        for name in ("args", "read_only_tools"):
            raw = payload.get(name, ())
            if not isinstance(raw, (list, tuple)):
                raise ValueError(f"MCP {name} must be an array")
            payload[name] = tuple(raw)
        return cls(**payload)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temp.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


class McpRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def _load(self) -> dict[str, McpServerRecord]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(
                self.path.read_text(encoding="utf-8"),
                parse_constant=_reject_json_constant,
            )
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise McpConfigurationError(f"cannot read MCP registry: {exc}") from exc
        if not isinstance(value, dict) or set(value) != {"schema_version", "servers"}:
            raise McpConfigurationError("MCP registry has invalid top-level fields")
        if value.get("schema_version") != REGISTRY_VERSION or not isinstance(value.get("servers"), list):
            raise McpConfigurationError("MCP registry has an unsupported schema")
        try:
            records = [McpServerRecord.from_dict(item) for item in value["servers"]]
            return self._validated(records)
        except (TypeError, ValueError) as exc:
            raise McpConfigurationError(f"MCP registry is invalid: {exc}") from exc

    @staticmethod
    def _validated(records: Iterable[McpServerRecord]) -> dict[str, McpServerRecord]:
        items = list(records)
        by_id = {item.server_id: item for item in items}
        if len(by_id) != len(items):
            raise McpConfigurationError("MCP registry contains duplicate server IDs")
        namespaces = {item.namespace for item in items}
        if len(namespaces) != len(items):
            raise McpConfigurationError("MCP registry contains duplicate namespaces")
        return by_id

    def _save(self, records: Iterable[McpServerRecord]) -> None:
        by_id = self._validated(records)
        _atomic_json(
            self.path,
            {
                "schema_version": REGISTRY_VERSION,
                "servers": [
                    item.to_dict()
                    for item in sorted(by_id.values(), key=lambda record: record.server_id)
                ],
            },
        )

    def list(self) -> list[McpServerRecord]:
        return sorted(self._load().values(), key=lambda item: item.server_id)

    def get(self, server_id: str) -> McpServerRecord:
        try:
            return self._load()[server_id]
        except KeyError as exc:
            raise McpConfigurationError(f"MCP server does not exist: {server_id}") from exc

    def put(self, record: McpServerRecord) -> McpServerRecord:
        records = self._load()
        for current in records.values():
            if current.namespace == record.namespace and current.server_id != record.server_id:
                raise McpConfigurationError(
                    f"MCP namespace is already used by {current.server_id}: {record.namespace}"
                )
        records[record.server_id] = record
        self._save(records.values())
        return record

    def remove(self, server_id: str) -> McpServerRecord:
        records = self._load()
        try:
            removed = records.pop(server_id)
        except KeyError as exc:
            raise McpConfigurationError(f"MCP server does not exist: {server_id}") from exc
        self._save(records.values())
        return removed


def _canonical_digest(value: Any) -> str:
    try:
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()
    except _JSON_SERIALIZATION_ERRORS as exc:
        raise McpProtocolError("MCP digest input is not valid strict JSON") from exc


def mcp_registry_digest(server: McpServerRecord) -> str:
    """Bind an approval to the exact non-secret server configuration."""
    return _canonical_digest(server.to_dict())


def validate_mcp_selection_registry(
    server: McpServerRecord, selection: Mapping[str, Any]
) -> None:
    """Reject stale or malformed selections before contacting a server."""
    if (
        selection.get("server_id") != server.server_id
        or selection.get("namespace") != server.namespace
    ):
        raise McpAccessDenied("MCP server identity changed; reselect the server")
    if selection.get("registry_digest") != mcp_registry_digest(server):
        raise McpAccessDenied("MCP server configuration changed; reselect the server")


@dataclass(frozen=True)
class McpToolDescriptor:
    server_id: str
    namespace: str
    remote_name: str
    description: str
    input_schema: Dict[str, Any]
    schema_digest: str
    read_only: bool

    @property
    def name(self) -> str:
        return f"mcp.{self.namespace}.{self.remote_name}"

    @property
    def mutates(self) -> bool:
        return not self.read_only

    def to_dict(self) -> dict[str, Any]:
        return {
            "server_id": self.server_id,
            "namespace": self.namespace,
            "remote_name": self.remote_name,
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
            "schema_digest": self.schema_digest,
            "read_only": self.read_only,
        }


def _descriptor(record: McpServerRecord, tool: Any) -> McpToolDescriptor:
    name = getattr(tool, "name", None)
    schema = getattr(tool, "inputSchema", None)
    description = getattr(tool, "description", None) or "MCP tool"
    if not isinstance(name, str) or _SAFE_TOOL.fullmatch(name) is None:
        raise McpProtocolError("MCP server returned an unsafe tool name")
    if not isinstance(description, str):
        raise McpProtocolError(f"MCP tool {name} has an invalid description")
    safe_schema = _strict_json_clone(schema, f"MCP tool {name} input schema")
    _validate_schema(safe_schema, f"MCP tool {name} input schema")
    if safe_schema.get("type", "object") != "object":
        raise McpProtocolError(f"MCP tool {name} has a non-object input schema")
    digest = _canonical_digest({"name": name, "input_schema": safe_schema})
    return McpToolDescriptor(
        record.server_id,
        record.namespace,
        name,
        str(redact(description))[:4096],
        safe_schema,
        digest,
        name in record.read_only_tools,
    )


def _http_client_factory(
    headers: Optional[dict[str, str]] = None,
    timeout: Optional[httpx.Timeout] = None,
    auth: Optional[httpx.Auth] = None,
) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        auth=auth,
        follow_redirects=False,
        trust_env=False,
    )


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
        if _MODERN_STREAMABLE_HTTP:
            async with _http_client_factory(
                headers=headers,
                timeout=httpx.Timeout(record.timeout_seconds),
            ) as http_client:
                async with streamable_http_client(
                    record.url or "", http_client=http_client
                ) as streams:
                    try:
                        async with ClientSession(
                            streams[0], streams[1], read_timeout_seconds=read_timeout
                        ) as session:
                            yield session
                    finally:
                        await streams[0].aclose()
                        await streams[1].aclose()
            return
        async with streamable_http_client(
            record.url or "",
            headers=headers,
            timeout=record.timeout_seconds,
            sse_read_timeout=record.timeout_seconds,
            httpx_client_factory=_http_client_factory,
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
        tools = [_descriptor(record, item) for item in response.tools]
        names = [item.name for item in tools]
        if len(names) != len(set(names)):
            raise McpProtocolError("MCP server returned duplicate tool names")
        unknown_read_only = set(record.read_only_tools).difference(
            item.remote_name for item in tools
        )
        if unknown_read_only:
            raise McpProtocolError(
                f"configured read-only MCP tools do not exist: {sorted(unknown_read_only)}"
            )
        return tools

    @staticmethod
    def _classified(
        exc: Exception, *, mutation: bool, secrets: tuple[str, ...] = ()
    ) -> McpError:
        if isinstance(exc, McpError):
            return exc
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
        with anyio.fail_after(record.timeout_seconds):
            async with self._session(record, repository, secret) as session:
                await session.initialize()
                listed = await session.list_tools()
                self._validate_discovery_size(record, listed)
                discovered = [_descriptor(record, tool) for tool in listed.tools]
                names = [item.remote_name for item in discovered]
                if len(names) != len(set(names)):
                    raise McpProtocolError(
                        "MCP server returned duplicate tool names"
                    )
                current = {
                    item.remote_name: item for item in discovered
                }.get(descriptor.remote_name)
                if current is None or current.schema_digest != descriptor.schema_digest:
                    raise McpAccessDenied(
                        "MCP tool schema changed; inspect and reselect the server"
                    )
                result = await session.call_tool(descriptor.remote_name, arguments)
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
            raise McpRemoteToolError(
                f"MCP tool {descriptor.name} returned an error result"
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
    ) -> dict[str, Any]:
        return self.call_record(
            self.registry.get(descriptor.server_id),
            descriptor,
            arguments,
            repository,
        )


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
            current, descriptor, arguments, self.repository
        )
