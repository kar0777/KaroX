"""Built-in provider catalog: a dated models.dev snapshot.

OpenCode and many other coding agents read the community catalog at
https://models.dev to learn, for every provider, its endpoint and the
environment variable that usually holds the key, and for every model its
context window, output limit and published list price.  KaroX ships a compact
snapshot of that catalog (``karox/data/models_dev.json.gz``) so connecting a
provider needs nothing but its API key.

The same honesty rules as :mod:`karox.provider_presets` apply:

* A provider is *installable* only when KaroX implements its wire protocol.
  Everything else is still listed, with the reason it cannot be connected,
  instead of failing later with a confusing transport error.
* Hand-written presets win over the catalog for the same provider ID: their
  endpoints were verified for KaroX, the catalog's were not.
* Prices are the list prices models.dev recorded on the snapshot date.  They
  carry that provenance, are used for estimates only, and never replace a
  price the user entered.
* Nothing is fetched at runtime.  ``scripts/update_provider_catalog.py`` is the
  explicit refresh path.
"""

from __future__ import annotations

import gzip
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources
from typing import Any, Iterable, Mapping, Optional

from .provider_presets import PROVIDER_PRESETS

CATALOG_RESOURCE = "models_dev.json.gz"

# Wire protocols KaroX speaks, keyed by the AI SDK package models.dev names.
_NPM_ADAPTERS: Mapping[str, str] = {
    "@ai-sdk/openai-compatible": "openai_compatible_chat",
    "@openrouter/ai-sdk-provider": "openai_compatible_chat",
    "@ai-sdk/anthropic": "anthropic_messages",
    "@ai-sdk/openai": "openai_responses",
    "@ai-sdk/google": "gemini_generate_content",
    "@ai-sdk/xai": "openai_compatible_chat",
    "@ai-sdk/groq": "openai_compatible_chat",
    "@ai-sdk/mistral": "openai_compatible_chat",
    "@ai-sdk/deepinfra": "openai_compatible_chat",
    "@ai-sdk/togetherai": "openai_compatible_chat",
    "@ai-sdk/cerebras": "openai_compatible_chat",
    "@ai-sdk/perplexity": "openai_compatible_chat",
    "@ai-sdk/cohere": "openai_compatible_chat",
    "@aihubmix/ai-sdk-provider": "openai_compatible_chat",
    "venice-ai-sdk-provider": "openai_compatible_chat",
    "@ai-sdk/gateway": "openai_compatible_chat",
    # Azure OpenAI's v1 API (GA 2025) takes the plain OpenAI wire format and a
    # bearer API key at /openai/v1, without the api-version query or the
    # deployment path of the classic surface. Not verified live by KaroX yet.
    "@ai-sdk/azure": "openai_compatible_chat",
}

# Documented public endpoints for SDK packages whose catalog entry carries no
# ``api`` field (the SDK hard-codes it).  Only first-party documented URLs.
_DOCUMENTED_BASE_URLS: Mapping[str, str] = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
    "google": "https://generativelanguage.googleapis.com/v1beta",
    "xai": "https://api.x.ai/v1",
    "groq": "https://api.groq.com/openai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "deepinfra": "https://api.deepinfra.com/v1/openai",
    "togetherai": "https://api.together.xyz/v1",
    "cerebras": "https://api.cerebras.ai/v1",
    "perplexity": "https://api.perplexity.ai",
    "cohere": "https://api.cohere.ai/compatibility/v1",
    "aihubmix": "https://aihubmix.com/v1",
    "venice": "https://api.venice.ai/api/v1",
    "vercel": "https://ai-gateway.vercel.sh/v1",
    "azure": "https://${AZURE_RESOURCE_NAME}.openai.azure.com/openai/v1",
    "azure-cognitive-services": (
        "https://${AZURE_COGNITIVE_SERVICES_RESOURCE_NAME}.cognitiveservices.azure.com/openai/v1"
    ),
}

# Words that mark an environment variable as the secret rather than, say, the
# Azure resource name or a cloud region listed next to it.
_SECRET_ENV_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD")

# Why a catalog provider cannot be connected with an API key alone.
_UNSUPPORTED_NPM: Mapping[str, str] = {
    "@ai-sdk/amazon-bedrock": "Amazon Bedrock needs AWS request signing",
    "@ai-sdk/google-vertex": "Vertex AI needs Google Cloud OAuth credentials",
    "@ai-sdk/google-vertex/anthropic": "Vertex AI needs Google Cloud OAuth credentials",
    "gitlab-ai-provider": "GitLab Duo needs a GitLab OAuth sign-in",
    "watsonx-ai-provider": "watsonx needs an IBM Cloud IAM token exchange",
    "@jerome-benoit/sap-ai-provider-v2": "SAP AI Core needs a service-key OAuth flow",
    "ai-gateway-provider": "Cloudflare AI Gateway needs an account and gateway ID",
}
_UNSUPPORTED_IDS: Mapping[str, str] = {
    "github-copilot": "GitHub Copilot needs its own device sign-in, not an API key",
}

_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_VALID_VARIABLE_VALUE = re.compile(r"^[A-Za-z0-9._:/-]{1,253}$")


class CatalogError(ValueError):
    """A catalog lookup or endpoint resolution failed."""


@dataclass(frozen=True)
class CatalogModel:
    model_id: str
    display_name: str
    context_window: Optional[int]
    max_output_tokens: Optional[int]
    vision: bool
    reasoning: bool
    input_per_million: Optional[float]
    output_per_million: Optional[float]
    cache_read_per_million: Optional[float]
    cache_write_per_million: Optional[float]
    release_date: str = ""
    status: str = ""

    @property
    def priced(self) -> bool:
        return self.input_per_million is not None and self.output_per_million is not None

    @property
    def deprecated(self) -> bool:
        return self.status == "deprecated"

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "display_name": self.display_name,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "vision": self.vision,
            "reasoning": self.reasoning,
            "pricing": (
                {
                    "currency": "USD",
                    "input_per_million": self.input_per_million,
                    "output_per_million": self.output_per_million,
                    "cache_read_per_million": self.cache_read_per_million,
                    "cache_write_per_million": self.cache_write_per_million,
                }
                if self.priced
                else None
            ),
            "release_date": self.release_date,
            "status": self.status,
        }


@dataclass(frozen=True)
class CatalogProvider:
    provider_id: str
    display_name: str
    adapter_kind: Optional[str]
    base_url: Optional[str]
    env: tuple[str, ...]
    doc: str
    models: tuple[CatalogModel, ...] = field(default_factory=tuple)
    unsupported_reason: str = ""
    preset: bool = False

    @property
    def installable(self) -> bool:
        return self.adapter_kind is not None and bool(self.base_url) and not self.unsupported_reason

    @property
    def key_env(self) -> tuple[str, ...]:
        """The listed variables that hold the credential, not endpoint parts."""

        secret = tuple(
            name for name in self.env if any(word in name.upper() for word in _SECRET_ENV_WORDS)
        )
        return secret or tuple(name for name in self.env if name not in self.url_variables)

    @property
    def url_variables(self) -> tuple[str, ...]:
        return tuple(dict.fromkeys(_VARIABLE.findall(self.base_url or "")))

    def resolved_base_url(self, values: Optional[Mapping[str, str]] = None) -> str:
        """Fill ``${NAME}`` placeholders; refuse anything that is not a plain host/path token."""
        if not self.base_url:
            raise CatalogError(f"{self.display_name} has no documented endpoint; pass --base-url")
        supplied = dict(values or {})
        missing = [name for name in self.url_variables if not supplied.get(name)]
        if missing:
            raise CatalogError(
                f"{self.display_name} endpoint needs: " + ", ".join(missing)
                + " (pass --var NAME=VALUE)"
            )
        for name in self.url_variables:
            if not _VALID_VARIABLE_VALUE.fullmatch(supplied[name]):
                raise CatalogError(f"{name} contains characters that cannot appear in an endpoint")
        return _VARIABLE.sub(lambda match: supplied[match.group(1)], self.base_url).rstrip("/")

    def model(self, model_id: str) -> Optional[CatalogModel]:
        wanted = model_id.strip()
        for item in self.models:
            if item.model_id == wanted:
                return item
        lowered = wanted.lower()
        for item in self.models:
            if item.model_id.lower() == lowered:
                return item
        return None

    def recommended_model(self) -> Optional[CatalogModel]:
        """Newest current model, preferring priced ones; deterministic on ties."""
        pool = [item for item in self.models if not item.deprecated] or list(self.models)
        if not pool:
            return None
        return max(
            pool,
            key=lambda item: (item.priced, item.release_date, item.context_window or 0, item.model_id),
        )

    def to_dict(self, *, include_models: bool = False) -> dict[str, Any]:
        value: dict[str, Any] = {
            "provider_id": self.provider_id,
            "display_name": self.display_name,
            "adapter_kind": self.adapter_kind,
            "base_url": self.base_url,
            "url_variables": list(self.url_variables),
            "env": list(self.env),
            "key_env": list(self.key_env),
            "doc": self.doc,
            "installable": self.installable,
            "unsupported_reason": self.unsupported_reason,
            "preset": self.preset,
            "model_count": len(self.models),
        }
        recommended = self.recommended_model()
        value["recommended_model"] = recommended.model_id if recommended else None
        if include_models:
            value["models"] = [item.to_dict() for item in self.models]
        return value


@dataclass(frozen=True)
class Catalog:
    fetched_at: str
    source: str
    license: str
    providers: tuple[CatalogProvider, ...]

    @property
    def pricing_version(self) -> str:
        return "models-dev-" + self.fetched_at.replace("-", "")

    @property
    def pricing_source(self) -> str:
        return f"models.dev snapshot {self.fetched_at} (published list price)"

    def provider(self, provider_id: str) -> CatalogProvider:
        wanted = provider_id.strip().lower()
        for item in self.providers:
            if item.provider_id.lower() == wanted:
                return item
        raise CatalogError(f"unknown catalog provider: {provider_id}")

    def search(self, query: str = "", *, installable_only: bool = False) -> list[CatalogProvider]:
        needle = query.strip().lower()
        found = []
        for item in self.providers:
            if installable_only and not item.installable:
                continue
            if needle and needle not in item.provider_id.lower() and needle not in item.display_name.lower():
                continue
            found.append(item)
        return found

    def provider_for_base_url(self, base_url: str) -> Optional[CatalogProvider]:
        wanted = _normalised_url(base_url)
        if not wanted:
            return None
        for item in self.providers:
            if item.base_url and not item.url_variables and _normalised_url(item.base_url) == wanted:
                return item
        return None


def _normalised_url(value: str) -> str:
    text = (value or "").strip().lower().rstrip("/")
    return re.sub(r"^https?://", "", text)


def _number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if number >= 0 else None


def _count(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return None
    return int(value)


def _models(raw: Mapping[str, Any]) -> tuple[CatalogModel, ...]:
    models = []
    for model_id, row in raw.items():
        if not isinstance(row, list) or len(row) < 11:
            continue
        models.append(
            CatalogModel(
                model_id=model_id,
                display_name=str(row[0] or model_id),
                context_window=_count(row[1]),
                max_output_tokens=_count(row[2]),
                vision=bool(row[3]),
                reasoning=bool(row[4]),
                input_per_million=_number(row[5]),
                output_per_million=_number(row[6]),
                cache_read_per_million=_number(row[7]),
                cache_write_per_million=_number(row[8]),
                release_date=str(row[9] or ""),
                status=str(row[10] or ""),
            )
        )
    return tuple(models)


def _provider(provider_id: str, raw: Mapping[str, Any]) -> CatalogProvider:
    npm = str(raw.get("npm") or "")
    preset = PROVIDER_PRESETS.get(provider_id)
    alias = {"google": "gemini"}.get(provider_id)
    if preset is None and alias is not None:
        preset = PROVIDER_PRESETS.get(alias)
    reason = _UNSUPPORTED_IDS.get(provider_id) or _UNSUPPORTED_NPM.get(npm, "")
    adapter = _NPM_ADAPTERS.get(npm)
    base_url = str(raw.get("api") or "") or _DOCUMENTED_BASE_URLS.get(provider_id, "")
    from_preset = False
    if preset is not None and preset.installable and preset.base_url:
        adapter, base_url, from_preset = preset.adapter_kind, preset.base_url, True
    if not reason and adapter is None:
        reason = f"KaroX has no adapter for the {npm or 'unknown'} protocol yet"
    if not reason and not base_url:
        reason = "the catalog does not publish this provider's endpoint"
    return CatalogProvider(
        provider_id=provider_id,
        display_name=str(raw.get("name") or provider_id),
        adapter_kind=adapter if not reason else None,
        base_url=base_url or None,
        env=tuple(str(item) for item in raw.get("env") or () if isinstance(item, str)),
        doc=str(raw.get("doc") or ""),
        models=_models(raw.get("models") or {}),
        unsupported_reason=reason,
        preset=from_preset,
    )


def load_catalog_document(document: Mapping[str, Any]) -> Catalog:
    if document.get("schema") != 1 or not isinstance(document.get("providers"), dict):
        raise CatalogError("provider catalog snapshot has an unsupported schema")
    providers = tuple(
        _provider(provider_id, raw)
        for provider_id, raw in sorted(document["providers"].items())
        if isinstance(raw, Mapping)
    )
    return Catalog(
        fetched_at=str(document.get("fetched_at") or "unknown"),
        source=str(document.get("source") or ""),
        license=str(document.get("license") or ""),
        providers=providers,
    )


@lru_cache(maxsize=1)
def provider_catalog() -> Catalog:
    """The shipped snapshot, parsed once per process."""
    blob = resources.files("karox.data").joinpath(CATALOG_RESOURCE).read_bytes()
    return load_catalog_document(json.loads(gzip.decompress(blob).decode("utf-8")))


def plausible_output_limit(context_window: Optional[int], max_output: Optional[int]) -> Optional[int]:
    """The catalog's output limit, or None when it cannot be a real one.

    Many catalog rows copy the context window into the output limit.  KaroX
    reserves the output limit inside the window when it compacts history, so
    taking such a value literally would shrink the usable prompt to nothing.
    A limit above half the window is treated as unknown (provider default).
    """
    if not max_output:
        return None
    if context_window and max_output > context_window // 2:
        return None
    return max_output


def catalog_model_records(
    provider: CatalogProvider,
    registry_provider_id: str,
    catalog: Optional[Catalog] = None,
    *,
    include_deprecated: bool = False,
    only: Optional[Iterable[str]] = None,
) -> list[Any]:
    """Registry ``ModelRecord`` values for a catalog provider's models."""
    from .registry import ModelPricing, ModelRecord

    catalog = catalog or provider_catalog()
    wanted = set(only) if only is not None else None
    records = []
    for item in provider.models:
        if wanted is not None and item.model_id not in wanted:
            continue
        if item.deprecated and not include_deprecated and (wanted is None or item.model_id not in wanted):
            continue
        pricing = None
        if item.priced:
            pricing = ModelPricing(
                version=catalog.pricing_version,
                currency="USD",
                input_per_million=float(item.input_per_million or 0.0),
                output_per_million=float(item.output_per_million or 0.0),
                source=catalog.pricing_source,
                cache_read_per_million=item.cache_read_per_million,
                cache_write_per_million=item.cache_write_per_million,
            )
        records.append(
            ModelRecord(
                provider_id=registry_provider_id,
                model_id=item.model_id,
                context_window=item.context_window,
                max_output_tokens=plausible_output_limit(item.context_window, item.max_output_tokens),
                tools="true",
                vision="true" if item.vision else "unknown",
                streaming="true",
                pricing=pricing,
                provenance=f"catalog:models.dev@{catalog.fetched_at}",
                display_name=item.display_name if item.display_name != item.model_id else None,
            )
        )
    return records


__all__ = [
    "Catalog",
    "CatalogError",
    "CatalogModel",
    "CatalogProvider",
    "catalog_model_records",
    "load_catalog_document",
    "plausible_output_limit",
    "provider_catalog",
]
