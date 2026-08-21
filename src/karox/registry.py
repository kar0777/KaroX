"""Validated, atomic provider and model registry for native-agent routing."""

from __future__ import annotations

import json
import math
import os
import re
import uuid
from dataclasses import asdict, dataclass, field, replace
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
    # A prompt served from cache bills at a fraction of the input rate, and one
    # written to cache bills at a premium. Left unset, the published multipliers
    # are used rather than pretending the two cost the same as ordinary input,
    # which would overstate a cached run several times over.
    cache_read_per_million: Optional[float] = None
    cache_write_per_million: Optional[float] = None

    # Multipliers on the input rate, used when a record does not name the two
    # cache rates directly. They match what the providers publish today.
    CACHE_READ_MULTIPLIER = 0.1
    CACHE_WRITE_MULTIPLIER = 1.25

    def __post_init__(self) -> None:
        _safe_id(self.version, "pricing version")
        if not isinstance(self.currency, str) or not re.fullmatch(
            r"[A-Z]{3}", self.currency
        ):
            raise ValueError("pricing currency must be a three-letter uppercase code")
        for label, value in (
            ("input price", self.input_per_million),
            ("output price", self.output_per_million),
            ("cache read price", self.cache_read_per_million),
            ("cache write price", self.cache_write_per_million),
        ):
            if value is None:
                continue
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0
            ):
                raise ValueError(f"{label} must be finite and non-negative")
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("pricing source is required")

    @property
    def cache_read_rate(self) -> float:
        if self.cache_read_per_million is not None:
            return float(self.cache_read_per_million)
        return float(self.input_per_million) * self.CACHE_READ_MULTIPLIER

    @property
    def cache_write_rate(self) -> float:
        if self.cache_write_per_million is not None:
            return float(self.cache_write_per_million)
        return float(self.input_per_million) * self.CACHE_WRITE_MULTIPLIER

    def estimate(self, usage: Mapping[str, Any]) -> float:
        prompt_tokens = _usage_count(usage, "prompt_tokens", "input_tokens")
        output_tokens = _usage_count(usage, "completion_tokens", "output_tokens")
        cache_read = _usage_count(usage, "cache_read_tokens")
        cache_write = _usage_count(usage, "cache_write_tokens")
        # prompt_tokens is the whole prompt, of which the cached parts are a
        # subset. Charging all of it at the input rate is what made every cached
        # run report a cost it did not incur.
        full_rate_tokens = max(0, prompt_tokens - cache_read - cache_write)
        return round(
            full_rate_tokens * float(self.input_per_million) / 1_000_000
            + cache_read * self.cache_read_rate / 1_000_000
            + cache_write * self.cache_write_rate / 1_000_000
            + output_tokens * float(self.output_per_million) / 1_000_000,
            12,
        )

    def estimate_uncached(self, usage: Mapping[str, Any]) -> float:
        """Price the same response as if every prompt token billed at full rate.

        This uses the exact same versioned input/output prices as ``estimate``;
        only the cache assumption changes. The difference is therefore an
        auditable cache effect rather than a guessed saving.
        """

        prompt_tokens = _usage_count(usage, "prompt_tokens", "input_tokens")
        output_tokens = _usage_count(usage, "completion_tokens", "output_tokens")
        return round(
            prompt_tokens * float(self.input_per_million) / 1_000_000
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
    # B5. Whether this provider may be chosen for new work.
    #
    # Recorded gap, closed here rather than simulated in the UI: before B5 the
    # registry had no way to say "keep this configuration and this credential
    # but do not use it". The only reversible state was ``selected_model``,
    # which is a single global pointer, and the only way to take a provider out
    # of service was to delete it -- taking its key with it. "Disable" drawn in
    # the TUI over that model would have been a lie that survived until the
    # next process start.
    #
    # This is deliberately a field on the record that already exists, written
    # through the registry that already owns it. No second store, no parallel
    # disabled-table to fall out of step with the providers it describes.
    # Absent in a file written before B5, so an older registry reads as enabled.
    enabled: bool = True
    # Shared Bypass mode preference (see access_mode.py). The record only
    # persists the preference; enforcement happens in the KaroX agent session
    # created for this provider -- never in provider HTTP auth. Absent in a
    # registry written before the mode existed, so an older file reads OFF.
    bypass: bool = False

    def __post_init__(self) -> None:
        _safe_id(self.provider_id, "provider ID")
        if not isinstance(self.enabled, bool):
            raise ValueError("provider enabled flag must be true or false")
        if not isinstance(self.bypass, bool):
            raise ValueError("provider bypass flag must be true or false")
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
            # Deliberately *not* ``bool(...)``. Coercing here defeats the
            # validation boundary below: ``bool("false")`` and ``bool(0.1)``
            # are both ``True``, so a corrupted or hand-edited registry would
            # silently read as enabled -- the one direction a mistake must
            # never fall, because it re-arms a provider the user switched off.
            # The raw value is handed to ``__post_init__``, which accepts a
            # JSON boolean and rejects everything else. A missing key is the
            # only permitted default, for files written before B5.
            enabled=value.get("enabled", True),
            # Same discipline as ``enabled``: no coercion, and a missing key
            # is the only permitted default so a legacy registry reads OFF.
            bypass=value.get("bypass", False),
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
    # The provider's published display name. None when the catalog did not
    # publish one; never synthesized from the model id.
    display_name: Optional[str] = None

    def __post_init__(self) -> None:
        _safe_id(self.provider_id, "provider ID")
        if not isinstance(self.model_id, str) or not self.model_id.strip():
            raise ValueError("model ID is required")
        if len(self.model_id) > 500 or any(
            char in self.model_id for char in "\r\n\x00"
        ):
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
        if self.display_name is not None:
            if (
                not isinstance(self.display_name, str)
                or not self.display_name.strip()
                or len(self.display_name) > 500
                or any(char in self.display_name for char in "\r\n\x00")
            ):
                raise ValueError("model display name must be short plain text")

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
            display_name=value.get("display_name"),
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
        if (
            not isinstance(value, dict)
            or value.get("schema_version") != REGISTRY_VERSION
        ):
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

    def map_alias(self, provider_id: str, alias: str, model_id: str) -> ModelRecord:
        """Move one provider-scoped alias to a model, creating it when needed."""
        _safe_id(alias, "model alias")
        providers, models, selected = self._load()
        if provider_id not in providers:
            raise RegistryError(f"provider does not exist: {provider_id}")
        target: Optional[ModelRecord] = None
        updated: list[ModelRecord] = []
        for item in models:
            if item.provider_id != provider_id:
                updated.append(item)
                continue
            aliases = tuple(value for value in item.aliases if value != alias)
            if item.model_id == model_id:
                target = replace(item, aliases=tuple(dict.fromkeys((*aliases, alias))))
                updated.append(target)
            elif aliases != item.aliases:
                updated.append(replace(item, aliases=aliases))
            else:
                updated.append(item)
        if target is None:
            target = ModelRecord(
                provider_id=provider_id,
                model_id=model_id,
                aliases=(alias,),
                provenance="user-alias-map",
            )
            updated.append(target)
        self._save(providers.values(), updated, selected)
        return target

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
            raise RegistryError(f"model does not exist: {provider_id}/{model_or_alias}")
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

    def set_provider_enabled(self, provider_id: str, enabled: bool) -> ProviderRecord:
        """Take a provider out of service, or put it back, without deleting it.

        B5. The whole point of the distinction: the record stays, the models
        stay, and the credential reference stays, so re-enabling needs no key.
        What changes is the one thing disabling is *for* -- the provider can no
        longer be the selection new work reads.

        Disabling therefore clears the global selection when it pointed here.
        That is the persisted half of the promise; leaving the pointer in place
        and filtering it out at read time would make every future reader
        responsible for remembering, and one that forgot would quietly send the
        next request through a provider the user switched off.

        No replacement is chosen. Picking "some other model" on the user's
        behalf is a product decision nobody made, so the selection simply
        becomes empty and the surface above says so.
        """

        providers, models, selected = self._load()
        try:
            current = providers[provider_id]
        except KeyError as exc:
            raise RegistryError(f"provider does not exist: {provider_id}") from exc
        # No ``bool(...)`` here either: ``replace`` re-runs ``__post_init__``,
        # so a caller passing something that merely looks boolean is rejected
        # rather than quietly rounded to True.
        updated = replace(current, enabled=enabled)
        providers[provider_id] = updated
        if not updated.enabled and selected is not None and selected[0] == provider_id:
            selected = None
        self._save(providers.values(), models, selected)
        return updated

    def select_model(self, provider_id: str, model_or_alias: str) -> ModelRecord:
        selected_model = self.model(provider_id, model_or_alias)
        providers, models, _ = self._load()
        # A disabled provider is not a candidate. Refusing here rather than in
        # the caller means every path -- TUI, CLI, selection repair -- inherits
        # the guarantee instead of each having to re-check it.
        owner = providers.get(provider_id)
        if owner is not None and not owner.enabled:
            raise RegistryError(f"provider is disabled: {provider_id}")
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
