"""MCP wire types, credentials, records, descriptors and schema validation."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
from dataclasses import asdict, dataclass, field, replace
from enum import Enum
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlsplit
from .credentials import CredentialBackend, CredentialError, KeyringBackend
from .security import contains_credential, redact



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

@dataclass(frozen=True)
class McpProgress:
    """One progress notification a remote MCP server sent during a call.

    ``total``/``message`` are optional on the wire and stay ``None`` when the
    server did not send them. The sink receives these inside the transport's
    event loop, so it must stay cheap and non-blocking.
    """

    progress: float
    total: Optional[float] = None
    message: Optional[str] = None

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
    oauth: bool = False
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
        if not isinstance(self.oauth, bool):
            raise ValueError("MCP OAuth flag must be boolean")
        if self.oauth and self.transport != "streamable_http":
            raise ValueError("MCP OAuth requires Streamable HTTP transport")
        if self.oauth and (self.credential_ref is not None or self.credential_target is not None):
            raise ValueError("MCP OAuth cannot be combined with a static credential")
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

def _resolved_read_only_remote_names(
    record: McpServerRecord, remote_names: Iterable[str]
) -> frozenset[str]:
    """Resolve configured tool IDs against their MCP wire spellings.

    KaroX keeps dotted internal identifiers such as ``karox.repo.read_file``,
    while its public MCP wire replaces dots with underscores because model tool
    APIs reject dotted function names.  A saved MCP record still uses the
    internal identifier in ``read_only_tools``.  Prefer an exact server name;
    only fall back to the dot-to-underscore spelling when the exact name is not
    present, so an unrelated external tool is never shadowed by an alias.
    """
    discovered = tuple(remote_names)
    resolved: set[str] = set()
    missing: list[str] = []
    for configured in record.read_only_tools:
        if configured in discovered:
            resolved.add(configured)
            continue
        wire_name = configured.replace(".", "_")
        if wire_name in discovered:
            resolved.add(wire_name)
            continue
        missing.append(configured)
    if missing:
        raise McpProtocolError(
            f"configured read-only MCP tools do not exist: {sorted(missing)}"
        )
    return frozenset(resolved)

def _normalize_discovered_tools(
    record: McpServerRecord, tools: Iterable[McpToolDescriptor]
) -> list[McpToolDescriptor]:
    """Restore KaroX's dotted IDs while preserving arbitrary MCP names.

    The KaroX wire encodes dots as underscores for model API compatibility, but
    its registry, permission selections, and retry-safe call path use the dotted
    identifiers.  Detect that contract only for the canonical ``karox-core``
    record or when its configured read-only IDs prove the alias relationship.
    Other MCP servers keep their names byte-for-byte.
    """
    items = list(tools)
    # The release does not ship the hosted bridge, so it has no hosted tool
    # catalogue to un-mangle. Wire names are preserved byte-for-byte; the
    # dotted-name restoration in the v5 runtime is unnecessary here.

    names = [item.remote_name for item in items]
    if len(names) != len(set(names)):
        raise McpProtocolError("MCP server returned duplicate tool names")
    read_only_remote_names = _resolved_read_only_remote_names(record, names)
    return [
        replace(item, read_only=item.remote_name in read_only_remote_names)
        for item in items
    ]
