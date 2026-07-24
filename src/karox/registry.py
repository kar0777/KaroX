"""Validated, atomic provider and model registry for native-agent routing."""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional
from urllib.parse import urlsplit

from .credentials import CredentialReference
from .security import contains_credential


REGISTRY_VERSION = 1
ADAPTER_KINDS = frozenset(
    {
        "openai_compatible_chat",
        "openai_responses",
        "anthropic_messages",
        "gemini_generate_content",
    }
)
PRIVACY_CLASSES = frozenset({"local", "private", "public"})
CAPABILITY_VALUES = frozenset({"true", "false", "unknown"})
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RESERVED_HEADERS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "x-api-key",
        "api-key",
        "x-goog-api-key",
    }
)
_RESERVED_QUERY = frozenset({"access_token", "api_key", "apikey", "key", "token"})


class RegistryError(RuntimeError):
    pass


def _safe_id(value: str, label: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise ValueError(f"{label} must contain 1-128 safe alphanumeric characters")
    return value


def _positive_int(value: Optional[int], label: str) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True)
class ModelPricing:
    version: str
    currency: str
    input_per_million: float
    output_per_million: float
    source: str

    def __post_init__(self) -> None:
        _safe_id(self.version, "pricing version")
        if not isinstance(self.currency, str) or not re.fullmatch(
            r"[A-Z]{3}", self.currency
        ):
            raise ValueError("pricing currency must be a three-letter uppercase code")
        for label, value in (
            ("input price", self.input_per_million),
            ("output price", self.output_per_million),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ValueError(f"{label} must be finite and non-negative")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("pricing source is required")

    def estimate(self, usage: Mapping[str, Any]) -> float:
        input_tokens = _usage_count(usage, "prompt_tokens", "input_tokens")
        output_tokens = _usage_count(usage, "completion_tokens", "output_tokens")
        return round(
            input_tokens * float(self.input_per_million) / 1_000_000
            + output_tokens * float(self.output_per_million) / 1_000_000,
            12,
        )


def _usage_count(usage: Mapping[str, Any], *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


@dataclass(frozen=True)
class ProviderRecord:
    provider_id: str
    adapter_kind: str
    base_url: str
    credential_ref: Optional[str] = None
    headers: Dict[str, str] = field(default_factory=dict)
    query: Dict[str, str] = field(default_factory=dict)
    privacy_class: str = "public"
    timeout_seconds: float = 60.0
    max_transport_retries: int = 2

    def __post_init__(self) -> None:
        _safe_id(self.provider_id, "provider ID")
        if self.adapter_kind not in ADAPTER_KINDS:
            raise ValueError(f"unsupported provider adapter: {self.adapter_kind!r}")
        parts = urlsplit(self.base_url)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.query
            or parts.fragment
        ):
            raise ValueError(
                "provider base URL must be HTTP(S) without credentials, query, or fragment"
            )
        if self.credential_ref is not None:
            CredentialReference.parse(self.credential_ref)
        if self.privacy_class not in PRIVACY_CLASSES:
            raise ValueError(f"unsupported privacy class: {self.privacy_class!r}")
        if (
            isinstance(self.timeout_seconds, bool)
            or not isinstance(self.timeout_seconds, (int, float))
            or not math.isfinite(float(self.timeout_seconds))
            or not 0.05 <= float(self.timeout_seconds) <= 3600
        ):
            raise ValueError("provider timeout must be between 0.05 and 3600 seconds")
        if (
            isinstance(self.max_transport_retries, bool)
            or not isinstance(self.max_transport_retries, int)
            or not 0 <= self.max_transport_retries <= 10
        ):
            raise ValueError("transport retries must be between 0 and 10")
        for raw_name, raw_value in self.headers.items():
            if not isinstance(raw_name, str) or not isinstance(raw_value, str):
                raise ValueError("provider headers must contain only text")
            name, value = raw_name, raw_value
            if (
                not name
                or name.lower() in _RESERVED_HEADERS
                or "\r" in name
                or "\n" in name
                or "\r" in value
                or "\n" in value
                or contains_credential(value)
            ):
                raise ValueError("provider headers contain invalid or credential data")
        for raw_name, raw_value in self.query.items():
            if not isinstance(raw_name, str) or not isinstance(raw_value, str):
                raise ValueError("provider query must contain only text")
            name, value = raw_name, raw_value
            if (
                not name
                or name.lower() in _RESERVED_QUERY
                or contains_credential(value)
            ):
                raise ValueError("provider query contains invalid or credential data")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ProviderRecord":
        return cls(
            provider_id=value["provider_id"],
            adapter_kind=value["adapter_kind"],
            base_url=value["base_url"],
            credential_ref=value.get("credential_ref"),
            headers=dict(value.get("headers") or {}),
            query=dict(value.get("query") or {}),
            privacy_class=value.get("privacy_class", "public"),
            timeout_seconds=value.get("timeout_seconds", 60.0),
            max_transport_retries=value.get("max_transport_retries", 2),
        )


@dataclass(frozen=True)
class ModelRecord:
    provider_id: str
    model_id: str
    aliases: tuple[str, ...] = ()
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    tools: str = "unknown"
    vision: str = "unknown"
    structured_output: str = "unknown"
    streaming: str = "unknown"
    pricing: Optional[ModelPricing] = None
    provenance: str = "manual"

    def __post_init__(self) -> None:
        _safe_id(self.provider_id, "provider ID")
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("model ID is required")
        if len(self.model_id) > 500 or any(char in self.model_id for char in "\r\n\x00"):
            raise ValueError("model ID contains invalid characters")
        for alias in self.aliases:
            _safe_id(alias, "model alias")
        if len(set(self.aliases)) != len(self.aliases):
            raise ValueError("model aliases must be unique")
        _positive_int(self.context_window, "context window")
        _positive_int(self.max_output_tokens, "maximum output tokens")
        for label, value in (
            ("tools", self.tools),
            ("vision", self.vision),
            ("structured output", self.structured_output),
            ("streaming", self.streaming),
        ):
            if value not in CAPABILITY_VALUES:
                raise ValueError(f"{label} capability must be true, false, or unknown")
        if not isinstance(self.provenance, str) or not self.provenance.strip():
            raise ValueError("model provenance is required")

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ModelRecord":
        raw_pricing = value.get("pricing")
        pricing = ModelPricing(**raw_pricing) if isinstance(raw_pricing, dict) else None
        return cls(
            provider_id=value["provider_id"],
            model_id=value["model_id"],
            aliases=tuple(value.get("aliases") or ()),
            context_window=value.get("context_window"),
            max_output_tokens=value.get("max_output_tokens"),
            tools=value.get("tools", "unknown"),
            vision=value.get("vision", "unknown"),
            structured_output=value.get("structured_output", "unknown"),
            streaming=value.get("streaming", "unknown"),
            pricing=pricing,
            provenance=value.get("provenance", "manual"),
        )


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


class ProviderRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    def _load(
        self,
    ) -> tuple[
        Dict[str, ProviderRecord],
        list[ModelRecord],
        Optional[tuple[str, str]],
    ]:
        if not self.path.exists():
            return {}, [], None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RegistryError(f"cannot read provider registry: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != REGISTRY_VERSION:
            raise RegistryError("provider registry has an unsupported schema")
        raw_providers = value.get("providers")
        raw_models = value.get("models")
        if not isinstance(raw_providers, list) or not isinstance(raw_models, list):
            raise RegistryError("provider registry providers and models must be arrays")
        try:
            provider_items = [ProviderRecord.from_dict(raw) for raw in raw_providers]
            models = [ModelRecord.from_dict(raw) for raw in raw_models]
        except (KeyError, TypeError, ValueError) as exc:
            raise RegistryError(f"provider registry is invalid: {exc}") from exc
        providers, model_items = self._validated(provider_items, models)
        raw_selected = value.get("selected_model")
        selected: Optional[tuple[str, str]] = None
        if raw_selected is not None:
            if not isinstance(raw_selected, dict) or set(raw_selected) != {
                "provider_id",
                "model_id",
            }:
                raise RegistryError("provider registry selected model is invalid")
            provider_id = raw_selected.get("provider_id")
            model_id = raw_selected.get("model_id")
            if not isinstance(provider_id, str) or not isinstance(model_id, str):
                raise RegistryError("provider registry selected model is invalid")
            if not any(
                item.provider_id == provider_id and item.model_id == model_id
                for item in model_items
            ):
                raise RegistryError("provider registry selected model does not exist")
            selected = (provider_id, model_id)
        return providers, model_items, selected

    @staticmethod
    def _validated(
        providers: Iterable[ProviderRecord], models: Iterable[ModelRecord]
    ) -> tuple[Dict[str, ProviderRecord], list[ModelRecord]]:
        provider_items = list(providers)
        model_items = list(models)
        if any(not isinstance(item, ProviderRecord) for item in provider_items):
            raise RegistryError("provider registry contains an invalid provider record")
        if any(not isinstance(item, ModelRecord) for item in model_items):
            raise RegistryError("provider registry contains an invalid model record")

        provider_map = {item.provider_id: item for item in provider_items}
        if len(provider_map) != len(provider_items):
            raise RegistryError("provider registry contains duplicate provider IDs")
        known = set(provider_map)
        if any(item.provider_id not in known for item in model_items):
            raise RegistryError("provider registry contains an orphaned model")

        names: Dict[str, Dict[str, str]] = {}
        for item in model_items:
            provider_names = names.setdefault(item.provider_id, {})
            for model_name in (item.model_id, *item.aliases):
                owner = provider_names.get(model_name)
                if owner is not None:
                    raise RegistryError(
                        "provider registry contains colliding model IDs or aliases: "
                        f"{item.provider_id}/{model_name}"
                    )
                provider_names[model_name] = item.model_id
        return provider_map, model_items

    def _save(
        self,
        providers: Iterable[ProviderRecord],
        models: Iterable[ModelRecord],
        selected: Optional[tuple[str, str]] = None,
    ) -> None:
        provider_map, model_items = self._validated(providers, models)
        if selected is not None and not any(
            item.provider_id == selected[0] and item.model_id == selected[1]
            for item in model_items
        ):
            raise RegistryError("selected model does not exist")
        value = {
            "schema_version": REGISTRY_VERSION,
            "providers": [
                asdict(item)
                for item in sorted(
                    provider_map.values(), key=lambda item: item.provider_id
                )
            ],
            "models": [
                asdict(item)
                for item in sorted(
                    model_items, key=lambda item: (item.provider_id, item.model_id)
                )
            ],
        }
        if selected is not None:
            value["selected_model"] = {
                "provider_id": selected[0],
                "model_id": selected[1],
            }
        _atomic_json(self.path, value)

    def providers(self) -> list[ProviderRecord]:
        providers, _, _ = self._load()
        return sorted(providers.values(), key=lambda item: item.provider_id)

    def models(self, provider_id: Optional[str] = None) -> list[ModelRecord]:
        _, models, _ = self._load()
        return sorted(
            (
                item
                for item in models
                if provider_id is None or item.provider_id == provider_id
            ),
            key=lambda item: (item.provider_id, item.model_id),
        )

    def put_provider(self, record: ProviderRecord) -> ProviderRecord:
        providers, models, selected = self._load()
        providers[record.provider_id] = record
        self._save(providers.values(), models, selected)
        return record

    def put_model(self, record: ModelRecord) -> ModelRecord:
        providers, models, selected = self._load()
        if record.provider_id not in providers:
            raise RegistryError(f"provider does not exist: {record.provider_id}")
        retained = [
            item
            for item in models
            if (item.provider_id, item.model_id)
            != (record.provider_id, record.model_id)
        ]
        retained.append(record)
        self._save(providers.values(), retained, selected)
        return record

    def provider(self, provider_id: str) -> ProviderRecord:
        providers, _, _ = self._load()
        try:
            return providers[provider_id]
        except KeyError as exc:
            raise RegistryError(f"provider does not exist: {provider_id}") from exc

    def model(self, provider_id: str, model_or_alias: str) -> ModelRecord:
        candidates = [
            item
            for item in self.models(provider_id)
            if item.model_id == model_or_alias or model_or_alias in item.aliases
        ]
        if not candidates:
            raise RegistryError(
                f"model does not exist: {provider_id}/{model_or_alias}"
            )
        if len(candidates) > 1:
            raise RegistryError(
                f"model alias is ambiguous: {provider_id}/{model_or_alias}"
            )
        return candidates[0]

    def remove_provider(
        self, provider_id: str, *, cascade: bool = False
    ) -> ProviderRecord:
        providers, models, selected = self._load()
        try:
            removed = providers[provider_id]
        except KeyError as exc:
            raise RegistryError(f"provider does not exist: {provider_id}") from exc
        dependent = [item for item in models if item.provider_id == provider_id]
        if dependent and not cascade:
            raise RegistryError(
                f"provider has {len(dependent)} model(s); use cascade to remove them"
            )
        del providers[provider_id]
        if cascade:
            models = [item for item in models if item.provider_id != provider_id]
        if selected is not None and selected[0] == provider_id:
            selected = None
        self._save(providers.values(), models, selected)
        return removed

    def remove_model(self, provider_id: str, model_or_alias: str) -> ModelRecord:
        removed = self.model(provider_id, model_or_alias)
        providers, models, selected = self._load()
        models = [
            item
            for item in models
            if (item.provider_id, item.model_id)
            != (removed.provider_id, removed.model_id)
        ]
        if selected == (removed.provider_id, removed.model_id):
            selected = None
        self._save(providers.values(), models, selected)
        return removed

    def select_model(self, provider_id: str, model_or_alias: str) -> ModelRecord:
        selected_model = self.model(provider_id, model_or_alias)
        providers, models, _ = self._load()
        selected = (selected_model.provider_id, selected_model.model_id)
        self._save(providers.values(), models, selected)
        return selected_model

    def selected_model(self) -> Optional[ModelRecord]:
        _, models, selected = self._load()
        if selected is None:
            return None
        for item in models:
            if (item.provider_id, item.model_id) == selected:
                return item
        raise RegistryError("selected model does not exist")
