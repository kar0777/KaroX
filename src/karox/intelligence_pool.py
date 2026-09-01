"""Unified intelligence pool for KaroX 5.

The pool is deliberately *not* another credential store and it is not an
executor.  It gives orchestration one provider-neutral inventory containing API
models, already-paid subscription agents, local models, and explicitly attached
external agents.  Credentials remain in the existing provider/keyring layer and
external execution remains behind KaroX policy/adapters.

API endpoints are derived live from :class:`ProviderRegistry`; non-API targets
are stored as secret-free metadata under the active KaroX config channel.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .paths import config_dir
from .registry import ModelRecord, ProviderRecord, ProviderRegistry
from .security import contains_credential


SOURCE_API = "api"
SOURCE_SUBSCRIPTION = "subscription"
SOURCE_LOCAL = "local"
SOURCE_EXTERNAL = "external"
SOURCE_KINDS = frozenset({SOURCE_API, SOURCE_SUBSCRIPTION, SOURCE_LOCAL, SOURCE_EXTERNAL})

ROLE_PLANNER = "planner"
ROLE_IMPLEMENTER = "implementer"
ROLE_REVIEWER = "reviewer"
ROLE_SCOUT = "scout"
ROLE_TESTER = "tester"
ROLE_SUMMARIZER = "summarizer"
ROLE_ORCHESTRATOR = "orchestrator"
ROLE_UI = "ui"
ROLE_SECURITY = "security"
ROLE_KINDS = frozenset(
    {
        ROLE_PLANNER,
        ROLE_IMPLEMENTER,
        ROLE_REVIEWER,
        ROLE_SCOUT,
        ROLE_TESTER,
        ROLE_SUMMARIZER,
        ROLE_ORCHESTRATOR,
        ROLE_UI,
        ROLE_SECURITY,
    }
)

CAP_CODE = "code"
CAP_REASONING = "reasoning"
CAP_TOOLS = "tools"
CAP_VISION = "vision"
CAP_BROWSER = "browser"
CAP_MCP = "mcp"
CAP_LOCAL = "local"
CAP_STRUCTURED = "structured_output"
CAP_STREAMING = "streaming"
CAPABILITIES = frozenset(
    {
        CAP_CODE,
        CAP_REASONING,
        CAP_TOOLS,
        CAP_VISION,
        CAP_BROWSER,
        CAP_MCP,
        CAP_LOCAL,
        CAP_STRUCTURED,
        CAP_STREAMING,
    }
)

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,255}$")
_SAFE_TEXT_LIMIT = 500
_SCHEMA_VERSION = 1


class IntelligencePoolError(RuntimeError):
    pass


def _safe_id(value: str, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{label} must contain 1-256 safe characters")
    return value


def _safe_model_id(value: str) -> str:
    """Validate a provider model identifier using the ProviderRegistry contract.

    Provider model IDs are opaque protocol values, not KaroX object IDs. Real
    providers legitimately use characters such as ``~`` and may expose names
    longer than KaroX's 256-character endpoint-key limit, so applying ``_safe_id``
    here made the Intelligence Pool unable to represent models the registry had
    already accepted.
    """

    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > 500
        or any(char in value for char in "\r\n\x00")
    ):
        raise ValueError("model id must be a non-empty provider identifier up to 500 characters")
    return value


def _api_endpoint_id(provider_id: str, model_id: str) -> str:
    """Build a stable KaroX-safe key without changing the provider model ID."""

    prefix = f"api:{_safe_id(provider_id, 'provider id')}:"
    raw = prefix + _safe_model_id(model_id)
    if len(raw) <= 256 and _SAFE_ID.fullmatch(raw) is not None:
        return raw

    digest = hashlib.sha256(model_id.encode("utf-8")).hexdigest()[:16]
    sanitized = re.sub(r"[^A-Za-z0-9._:@/+\-]", "_", model_id)
    room = 256 - len(prefix) - len(digest) - 1
    if room < 1:
        raise ValueError("provider id is too long to form an intelligence endpoint id")
    candidate = f"{prefix}{sanitized[:room]}-{digest}"
    return _safe_id(candidate, "endpoint id")


def _safe_text(value: str, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > _SAFE_TEXT_LIMIT
        or any(char in value for char in "\r\n\x00")
        or contains_credential(value)
    ):
        raise ValueError(f"{label} must be short secret-free text")
    return value.strip()


def _safe_string_tuple(values: Iterable[str], *, allowed: frozenset[str], label: str) -> tuple[str, ...]:
    result: list[str] = []
    for raw in values:
        if not isinstance(raw, str) or raw not in allowed:
            raise ValueError(f"unsupported {label}: {raw!r}")
        if raw not in result:
            result.append(raw)
    return tuple(result)


@dataclasses.dataclass(frozen=True)
class QuotaSnapshot:
    """Best-known quota state for one endpoint.

    ``remaining_fraction`` is optional because many subscriptions do not expose
    a machine-readable quota.  Unknown is intentionally different from zero.
    KaroX never guesses usage limits in order to make routing look smarter.
    """

    remaining_fraction: Optional[float] = None
    remaining_units: Optional[float] = None
    unit: Optional[str] = None
    resets_at: Optional[float] = None
    observed_at: float = 0.0
    source: str = "unknown"

    def __post_init__(self) -> None:
        if self.remaining_fraction is not None:
            if (
                isinstance(self.remaining_fraction, bool)
                or not isinstance(self.remaining_fraction, (int, float))
                or not math.isfinite(float(self.remaining_fraction))
                or not 0 <= float(self.remaining_fraction) <= 1
            ):
                raise ValueError("quota remaining_fraction must be between 0 and 1")
        if self.remaining_units is not None:
            if (
                isinstance(self.remaining_units, bool)
                or not isinstance(self.remaining_units, (int, float))
                or not math.isfinite(float(self.remaining_units))
                or float(self.remaining_units) < 0
            ):
                raise ValueError("quota remaining_units must be non-negative")
        if self.unit is not None:
            _safe_text(self.unit, "quota unit")
        if self.resets_at is not None and (
            isinstance(self.resets_at, bool)
            or not isinstance(self.resets_at, (int, float))
            or not math.isfinite(float(self.resets_at))
        ):
            raise ValueError("quota resets_at must be finite")
        if (
            isinstance(self.observed_at, bool)
            or not isinstance(self.observed_at, (int, float))
            or not math.isfinite(float(self.observed_at))
            or float(self.observed_at) < 0
        ):
            raise ValueError("quota observed_at must be non-negative")
        _safe_text(self.source, "quota source")

    @property
    def known(self) -> bool:
        return self.remaining_fraction is not None or self.remaining_units is not None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "QuotaSnapshot":
        return cls(
            remaining_fraction=value.get("remaining_fraction"),
            remaining_units=value.get("remaining_units"),
            unit=value.get("unit"),
            resets_at=value.get("resets_at"),
            observed_at=value.get("observed_at", 0.0),
            source=value.get("source", "unknown"),
        )


@dataclasses.dataclass(frozen=True)
class IntelligenceEndpoint:
    endpoint_id: str
    display_name: str
    source_kind: str
    capabilities: tuple[str, ...] = ()
    roles: tuple[str, ...] = ()
    provider_id: Optional[str] = None
    model_id: Optional[str] = None
    target_id: Optional[str] = None
    enabled: bool = True
    already_paid: bool = False
    quota: QuotaSnapshot = dataclasses.field(default_factory=QuotaSnapshot)
    provenance: str = "manual"

    def __post_init__(self) -> None:
        _safe_id(self.endpoint_id, "endpoint id")
        _safe_text(self.display_name, "endpoint display name")
        if self.source_kind not in SOURCE_KINDS:
            raise ValueError(f"unsupported intelligence source: {self.source_kind!r}")
        _safe_string_tuple(self.capabilities, allowed=CAPABILITIES, label="capability")
        _safe_string_tuple(self.roles, allowed=ROLE_KINDS, label="role")
        if self.provider_id is not None:
            _safe_id(self.provider_id, "provider id")
        if self.model_id is not None:
            _safe_model_id(self.model_id)
        if self.target_id is not None:
            _safe_id(self.target_id, "target id")
        if self.source_kind == SOURCE_API and (self.provider_id is None or self.model_id is None):
            raise ValueError("API endpoint requires provider_id and model_id")
        if self.source_kind != SOURCE_API and not self.target_id:
            raise ValueError("non-API endpoint requires target_id")
        if not isinstance(self.enabled, bool) or not isinstance(self.already_paid, bool):
            raise ValueError("endpoint flags must be booleans")
        _safe_text(self.provenance, "endpoint provenance")

    @property
    def identity(self) -> str:
        return self.endpoint_id

    @property
    def metered_api(self) -> bool:
        return self.source_kind == SOURCE_API and not self.already_paid

    def supports(self, required: Iterable[str]) -> bool:
        owned = set(self.capabilities)
        return all(item in owned for item in required)

    def to_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["quota"] = self.quota.to_dict()
        return value

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "IntelligenceEndpoint":
        raw_quota = value.get("quota")
        return cls(
            endpoint_id=value["endpoint_id"],
            display_name=value["display_name"],
            source_kind=value["source_kind"],
            capabilities=tuple(value.get("capabilities") or ()),
            roles=tuple(value.get("roles") or ()),
            provider_id=value.get("provider_id"),
            model_id=value.get("model_id"),
            target_id=value.get("target_id"),
            enabled=value.get("enabled", True),
            already_paid=value.get("already_paid", False),
            quota=(
                QuotaSnapshot.from_dict(raw_quota)
                if isinstance(raw_quota, Mapping)
                else QuotaSnapshot()
            ),
            provenance=value.get("provenance", "manual"),
        )


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(raw_temp)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _model_capabilities(model: ModelRecord) -> tuple[str, ...]:
    caps = [CAP_CODE, CAP_REASONING]
    if model.tools == "true":
        caps.extend((CAP_TOOLS, CAP_MCP))
    if model.vision == "true":
        caps.append(CAP_VISION)
    if model.structured_output == "true":
        caps.append(CAP_STRUCTURED)
    if model.streaming == "true":
        caps.append(CAP_STREAMING)
    return tuple(dict.fromkeys(caps))


def _provider_endpoint(provider: ProviderRecord, model: ModelRecord) -> IntelligenceEndpoint:
    model_name = model.display_name or model.model_id
    return IntelligenceEndpoint(
        endpoint_id=_api_endpoint_id(provider.provider_id, model.model_id),
        display_name=f"{model_name} · {provider.provider_id}",
        source_kind=SOURCE_API,
        capabilities=_model_capabilities(model),
        roles=(),
        provider_id=provider.provider_id,
        model_id=model.model_id,
        enabled=provider.enabled,
        already_paid=False,
        quota=QuotaSnapshot(source="provider-registry"),
        provenance=f"provider-registry:{model.provenance}",
    )


class IntelligencePool:
    """Persistent non-secret inventory merged with the live provider registry."""

    def __init__(
        self,
        *,
        path: Optional[Path] = None,
        provider_registry: Optional[ProviderRegistry] = None,
    ) -> None:
        self.path = (path or (config_dir() / "vnext" / "intelligence-pool.json")).expanduser().resolve()
        self.provider_registry = provider_registry or ProviderRegistry(
            config_dir() / "vnext" / "providers.json"
        )

    def _load_custom(self) -> dict[str, IntelligenceEndpoint]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IntelligencePoolError(f"cannot read intelligence pool: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != _SCHEMA_VERSION:
            raise IntelligencePoolError("intelligence pool has unsupported schema")
        raw = value.get("endpoints")
        if not isinstance(raw, list):
            raise IntelligencePoolError("intelligence pool endpoints must be an array")
        result: dict[str, IntelligenceEndpoint] = {}
        try:
            for item in raw:
                if not isinstance(item, Mapping):
                    raise ValueError("endpoint entry must be an object")
                endpoint = IntelligenceEndpoint.from_dict(item)
                if endpoint.source_kind == SOURCE_API:
                    raise ValueError("API endpoints are derived from provider registry, not persisted here")
                if endpoint.endpoint_id in result:
                    raise ValueError(f"duplicate endpoint: {endpoint.endpoint_id}")
                result[endpoint.endpoint_id] = endpoint
        except (KeyError, TypeError, ValueError) as exc:
            raise IntelligencePoolError(f"intelligence pool is invalid: {exc}") from exc
        return result

    def _save_custom(self, endpoints: Mapping[str, IntelligenceEndpoint]) -> None:
        _atomic_json(
            self.path,
            {
                "schema_version": _SCHEMA_VERSION,
                "endpoints": [endpoints[key].to_dict() for key in sorted(endpoints)],
            },
        )

    def api_endpoints(self) -> list[IntelligenceEndpoint]:
        providers = {item.provider_id: item for item in self.provider_registry.providers()}
        result: list[IntelligenceEndpoint] = []
        for model in self.provider_registry.models():
            provider = providers.get(model.provider_id)
            if provider is None:
                continue
            result.append(_provider_endpoint(provider, model))
        return sorted(result, key=lambda item: item.endpoint_id)

    def list(self, *, include_disabled: bool = True) -> list[IntelligenceEndpoint]:
        merged = {item.endpoint_id: item for item in self.api_endpoints()}
        merged.update(self._load_custom())
        result = [merged[key] for key in sorted(merged)]
        return result if include_disabled else [item for item in result if item.enabled]

    def get(self, endpoint_id: str) -> IntelligenceEndpoint:
        _safe_id(endpoint_id, "endpoint id")
        for endpoint in self.list():
            if endpoint.endpoint_id == endpoint_id:
                return endpoint
        raise IntelligencePoolError(f"intelligence endpoint does not exist: {endpoint_id}")

    def put(self, endpoint: IntelligenceEndpoint) -> IntelligenceEndpoint:
        if endpoint.source_kind == SOURCE_API:
            raise IntelligencePoolError("API endpoints are managed through the provider registry")
        custom = self._load_custom()
        custom[endpoint.endpoint_id] = endpoint
        self._save_custom(custom)
        return endpoint

    def remove(self, endpoint_id: str) -> IntelligenceEndpoint:
        custom = self._load_custom()
        try:
            removed = custom.pop(endpoint_id)
        except KeyError as exc:
            raise IntelligencePoolError(f"custom intelligence endpoint does not exist: {endpoint_id}") from exc
        self._save_custom(custom)
        return removed

    def update_quota(
        self,
        endpoint_id: str,
        *,
        remaining_fraction: Optional[float] = None,
        remaining_units: Optional[float] = None,
        unit: Optional[str] = None,
        resets_at: Optional[float] = None,
        source: str = "user",
        observed_at: Optional[float] = None,
    ) -> IntelligenceEndpoint:
        endpoint = self.get(endpoint_id)
        if endpoint.source_kind == SOURCE_API:
            raise IntelligencePoolError(
                "API quota observations are runtime facts; persist them in routing telemetry, not provider config"
            )
        quota = QuotaSnapshot(
            remaining_fraction=remaining_fraction,
            remaining_units=remaining_units,
            unit=unit,
            resets_at=resets_at,
            observed_at=time.time() if observed_at is None else observed_at,
            source=source,
        )
        updated = dataclasses.replace(endpoint, quota=quota)
        return self.put(updated)

    def set_enabled(self, endpoint_id: str, enabled: bool) -> IntelligenceEndpoint:
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        endpoint = self.get(endpoint_id)
        if endpoint.source_kind == SOURCE_API:
            raise IntelligencePoolError("enable or disable API providers through ProviderController")
        return self.put(dataclasses.replace(endpoint, enabled=enabled))


__all__ = [
    "CAPABILITIES",
    "CAP_BROWSER",
    "CAP_CODE",
    "CAP_LOCAL",
    "CAP_MCP",
    "CAP_REASONING",
    "CAP_STREAMING",
    "CAP_STRUCTURED",
    "CAP_TOOLS",
    "CAP_VISION",
    "IntelligenceEndpoint",
    "IntelligencePool",
    "IntelligencePoolError",
    "QuotaSnapshot",
    "ROLE_IMPLEMENTER",
    "ROLE_KINDS",
    "ROLE_ORCHESTRATOR",
    "ROLE_PLANNER",
    "ROLE_REVIEWER",
    "ROLE_SCOUT",
    "ROLE_SECURITY",
    "ROLE_SUMMARIZER",
    "ROLE_TESTER",
    "ROLE_UI",
    "SOURCE_API",
    "SOURCE_EXTERNAL",
    "SOURCE_KINDS",
    "SOURCE_LOCAL",
    "SOURCE_SUBSCRIPTION",
]
