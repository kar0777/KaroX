"""Provider model discovery for OpenAI-compatible ``/models`` catalogs.

This module is the single discovery wire shared by ``karox model discover`` and
the TUI.  It normalizes whatever a provider catalog actually publishes -- ids,
token limits, capability lists, and per-token prices -- and never upgrades an
unpublished value into a guess: an absent context window stays ``None``, an
absent capability stays ``None`` (rendered as ``unknown`` in the registry), and
a missing price never becomes ``0``.

The module is deliberately transport-thin: a provider record, a credential
resolver, and an ``httpx`` client.  It imports the registry types for the
conversion helper but has no dependency on the old TUI surface.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping, Optional, Protocol, Sequence, Union
from urllib.parse import urlsplit

import httpx

from .registry import ModelPricing, ModelRecord, ProviderRecord
from .security import redact


DEFAULT_TIMEOUT_SECONDS = 15.0
DISCOVERED_PROVENANCE = "discovered"
_MAX_DETAIL = 500
_MAX_DISPLAY_NAME = 500
_MAX_MODEL_ID = 500

_OPENAI_ADAPTERS = frozenset({"openai_compatible_chat", "openai_responses"})


class CredentialResolver(Protocol):
    """The part of :class:`karox.credentials.CredentialStore` discovery needs."""

    def resolve(self, reference: str) -> str: ...


# Either a plain callable or an object with ``resolve(reference)``.  Both mirror
# how the CLI controller and the TUI own their credentials today.
CredentialSource = Union[Callable[[str], str], CredentialResolver, None]


class ModelDiscoveryError(RuntimeError):
    """A safe, structured failure from provider model discovery.

    The message never carries request headers or the credential: ``detail`` is
    redacted before it is stored, and only the status code and attempted
    endpoints are kept for the caller to explain the failure.
    """

    def __init__(
        self,
        kind: str,
        *,
        attempted_urls: Sequence[str] = (),
        status_code: Optional[int] = None,
        detail: str = "",
    ) -> None:
        safe_detail = ""
        if detail:
            safe_detail = str(redact(str(detail))).strip()[:_MAX_DETAIL]
        super().__init__(safe_detail or kind)
        self.kind = kind
        self.attempted_urls = tuple(attempted_urls)
        self.status_code = status_code
        self.detail = safe_detail


@dataclass(frozen=True)
class DiscoveredModel:
    """One catalog entry, with every unpublised value left unset.

    Capabilities are tri-state on purpose: ``True``/``False`` are published
    evidence, ``None`` means the catalog did not say.  Prices are USD per
    million tokens; ``None`` means the catalog published no usable price.
    """

    model_id: str
    display_name: Optional[str] = None
    context_window: Optional[int] = None
    max_output_tokens: Optional[int] = None
    input_price_per_million: Optional[float] = None
    output_price_per_million: Optional[float] = None
    supports_tools: Optional[bool] = None
    supports_vision: Optional[bool] = None
    supports_structured_output: Optional[bool] = None
    supports_streaming: Optional[bool] = None
    cache_read_price_per_million: Optional[float] = None
    provenance: str = DISCOVERED_PROVENANCE
    # The catalog entry exactly as received, kept for diagnostics and for
    # callers that need a field this module does not normalize.  Excluded from
    # equality/repr so two discoveries with the same normalized metadata compare
    # equal.
    raw: Mapping[str, Any] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if not _valid_model_id(self.model_id):
            raise ValueError("model ID must be short plain text")
        if self.display_name is not None:
            if not _valid_display_name(self.display_name):
                raise ValueError("model display name must be short plain text")
        _optional_positive_int(self.context_window, "context window")
        _optional_positive_int(self.max_output_tokens, "maximum output tokens")
        for label, value in (
            ("input price", self.input_price_per_million),
            ("output price", self.output_price_per_million),
            ("cache read price", self.cache_read_price_per_million),
        ):
            _optional_price(value, label)
        for label, value in (
            ("tools", self.supports_tools),
            ("vision", self.supports_vision),
            ("structured output", self.supports_structured_output),
            ("streaming", self.supports_streaming),
        ):
            if value is not None and not isinstance(value, bool):
                raise ValueError(f"{label} capability must be true, false, or unknown")
        if not isinstance(self.provenance, str) or not self.provenance.strip():
            raise ValueError("model provenance is required")

    @property
    def id(self) -> str:
        """The provider-facing model identifier (registry spelling alias)."""

        return self.model_id

    @property
    def free(self) -> Optional[bool]:
        """True/False only when both token prices were published."""

        input_price = self.input_price_per_million
        output_price = self.output_price_per_million
        if input_price is None or output_price is None:
            return None
        return input_price == 0 and output_price == 0

    def as_dict(self) -> dict[str, Any]:
        """A JSON-friendly payload for CLI output, without the raw catalog row."""

        return {
            "model_id": self.model_id,
            "display_name": self.display_name,
            "context_window": self.context_window,
            "max_output_tokens": self.max_output_tokens,
            "input_price_per_million": self.input_price_per_million,
            "output_price_per_million": self.output_price_per_million,
            "supports_tools": self.supports_tools,
            "supports_vision": self.supports_vision,
            "supports_structured_output": self.supports_structured_output,
            "supports_streaming": self.supports_streaming,
            "cache_read_price_per_million": self.cache_read_price_per_million,
            "free": self.free,
            "provenance": self.provenance,
        }


@dataclass(frozen=True)
class ModelDiscovery:
    """A successful discovery plus the base URL that answered."""

    models: tuple[DiscoveredModel, ...]
    base_url: str
    attempted_urls: tuple[str, ...]


def discover_models(
    provider: ProviderRecord,
    credential_resolver: CredentialSource = None,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    transport: Optional[httpx.BaseTransport] = None,
) -> list[DiscoveredModel]:
    """Discover a provider's models, sorted by id.

    ``credential_resolver`` may be a callable or an object with
    ``resolve(reference)`` (``CredentialStore`` satisfies both conventions).
    When the provider has no ``credential_ref`` no credential is read and no
    auth header is sent.

    ``transport`` exists for tests; production callers omit it.
    """

    return list(
        discover_models_result(
            provider,
            credential_resolver,
            timeout_seconds=timeout_seconds,
            transport=transport,
        ).models
    )


def discover_models_result(
    provider: ProviderRecord,
    credential_resolver: CredentialSource = None,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    transport: Optional[httpx.BaseTransport] = None,
) -> ModelDiscovery:
    """Discover models and report the endpoint that answered.

    OpenAI-compatible providers whose base URL does not already end in ``/v1``
    get the common ``/v1/models`` fallback after a 404, mirroring what the CLI
    and setup wizard have always done.  The returned ``base_url`` is the
    effective one, so a caller can correct the stored provider record.
    """

    timeout = _validated_timeout(timeout_seconds)
    base_url = _normalised_base_url(provider.base_url)
    adapter = str(getattr(provider, "adapter_kind", "") or "").strip()
    candidates = _candidate_bases(base_url, adapter)
    api_key = _resolved_credential(provider, credential_resolver)
    headers = _request_headers(provider, adapter, api_key)
    params = {
        str(name): str(value)
        for name, value in (getattr(provider, "query", None) or {}).items()
    }

    attempted: list[str] = []
    response: Optional[httpx.Response] = None
    effective_base = base_url
    try:
        with httpx.Client(timeout=timeout, transport=transport) as client:
            for index, (candidate_base, endpoint) in enumerate(candidates):
                attempted.append(endpoint)
                current = client.get(endpoint, headers=headers, params=params or None)
                if current.status_code == 404 and index + 1 < len(candidates):
                    continue
                response = current
                effective_base = candidate_base
                break
    except httpx.TimeoutException:
        raise ModelDiscoveryError(
            "timeout",
            attempted_urls=attempted,
            detail=f"the request timed out after {timeout:g} seconds",
        ) from None
    except httpx.HTTPError as exc:
        # The exception text can contain the full URL and transport internals;
        # the exception type is enough for an actionable, secret-free failure.
        raise ModelDiscoveryError(
            "network",
            attempted_urls=attempted,
            detail=f"the request failed ({type(exc).__name__})",
        ) from None

    if response is None:  # pragma: no cover - the candidate list is never empty
        raise ModelDiscoveryError("network", attempted_urls=attempted)
    if response.status_code >= 400:
        raise ModelDiscoveryError(
            "http",
            attempted_urls=attempted,
            status_code=response.status_code,
            detail=_response_error_detail(response, secret=api_key),
        )

    try:
        payload = response.json()
    except (ValueError, TypeError):
        raise ModelDiscoveryError(
            "invalid_response",
            attempted_urls=attempted,
            detail="the server response is not JSON",
        ) from None

    entries = _catalog_entries(payload, adapter)
    if entries is None:
        raise ModelDiscoveryError(
            "invalid_response",
            attempted_urls=attempted,
            detail="the response contains no model list",
        )

    discovered: list[DiscoveredModel] = []
    seen: set[str] = set()
    for entry in entries:
        item = _discovered_from_entry(entry, adapter)
        if item is None or item.model_id in seen:
            # Duplicate ids are common in aggregator catalogs; the first
            # occurrence wins so the result stays deterministic.
            continue
        seen.add(item.model_id)
        discovered.append(item)
    if not discovered:
        raise ModelDiscoveryError(
            "empty",
            attempted_urls=attempted,
            detail="the provider returned an empty model list",
        )

    discovered.sort(key=lambda item: item.model_id.casefold())
    return ModelDiscovery(
        models=tuple(discovered),
        base_url=effective_base,
        attempted_urls=tuple(attempted),
    )


def metadata_for_registry(discovered: DiscoveredModel) -> dict[str, Any]:
    """Registry ``ModelRecord`` fields for one discovered model.

    Capabilities keep the discovery's tri-state verdicts exactly; pricing is
    attached only when the catalog published both token prices, with source
    ``provider-reported`` so provenance stays next to the number.
    """

    return {
        "context_window": discovered.context_window,
        "max_output_tokens": discovered.max_output_tokens,
        "tools": _registry_capability(discovered.supports_tools),
        "vision": _registry_capability(discovered.supports_vision),
        "structured_output": _registry_capability(
            discovered.supports_structured_output
        ),
        "streaming": _registry_capability(discovered.supports_streaming),
        "pricing": _pricing_record(discovered),
        "provenance": discovered.provenance or DISCOVERED_PROVENANCE,
        "display_name": discovered.display_name,
    }


def model_record(provider_id: str, discovered: DiscoveredModel) -> ModelRecord:
    """Build a registry record from a discovered model."""

    return ModelRecord(
        provider_id=provider_id,
        model_id=discovered.model_id,
        **metadata_for_registry(discovered),
    )


def _pricing_record(discovered: DiscoveredModel) -> Optional[ModelPricing]:
    input_price = discovered.input_price_per_million
    output_price = discovered.output_price_per_million
    if input_price is None or output_price is None:
        # A half-published price is not a price; leave it to manual entry
        # instead of inventing the missing half.
        return None
    return ModelPricing(
        version="discovered",
        currency="USD",
        input_per_million=float(input_price),
        output_per_million=float(output_price),
        cache_read_per_million=(
            float(discovered.cache_read_price_per_million)
            if discovered.cache_read_price_per_million is not None
            else None
        ),
        source="provider-reported",
    )


def _registry_capability(value: Optional[bool]) -> str:
    if value is None:
        return "unknown"
    return "true" if value else "false"


def _validated_timeout(value: float) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError("timeout_seconds must be a positive number")
    return float(value)


def _normalised_base_url(value: str) -> str:
    if not isinstance(value, str):
        raise ModelDiscoveryError(
            "invalid_base_url",
            detail="the provider base URL is missing",
        )
    base_url = value.strip().rstrip("/")
    parts = urlsplit(base_url)
    if (
        parts.scheme not in {"http", "https"}
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
    ):
        raise ModelDiscoveryError(
            "invalid_base_url",
            detail="the provider base URL must be HTTP(S) without credentials, "
            "query, or fragment",
        )
    return base_url


def _candidate_bases(
    base_url: str, adapter: str
) -> list[tuple[str, str]]:
    candidates = [(base_url, _models_endpoint(base_url))]
    if adapter in _OPENAI_ADAPTERS and not base_url.lower().endswith("/v1"):
        if not base_url.lower().endswith("/models"):
            versioned = f"{base_url}/v1"
            candidates.append((versioned, _models_endpoint(versioned)))
    return candidates


def _models_endpoint(base_url: str) -> str:
    if base_url.lower().endswith("/models"):
        return base_url
    return f"{base_url}/models"


def _resolved_credential(
    provider: ProviderRecord, resolver: CredentialSource
) -> str:
    reference = getattr(provider, "credential_ref", None)
    if not reference:
        return ""
    if resolver is None:
        raise ModelDiscoveryError(
            "credential",
            detail="the provider has a stored credential but no resolver was provided",
        )
    try:
        value = (
            resolver(reference)
            if callable(resolver)
            else resolver.resolve(reference)
        )
    except Exception as exc:
        # Never surface the resolver's own message: it may include the
        # reference, and a backend error must not become a leak vector.
        raise ModelDiscoveryError(
            "credential",
            detail=f"the credential could not be resolved ({type(exc).__name__})",
        ) from None
    if not isinstance(value, str) or not value:
        raise ModelDiscoveryError(
            "credential",
            detail="the credential resolver returned no usable value",
        )
    return value


def _request_headers(
    provider: ProviderRecord, adapter: str, api_key: str
) -> dict[str, str]:
    headers = {
        str(name): str(value)
        for name, value in (getattr(provider, "headers", None) or {}).items()
    }
    if not _has_header(headers, "accept"):
        headers["Accept"] = "application/json"
    if not api_key:
        return headers
    if adapter == "anthropic_messages":
        headers["x-api-key"] = api_key
        if not _has_header(headers, "anthropic-version"):
            headers["anthropic-version"] = "2023-06-01"
    elif adapter == "gemini_generate_content":
        headers["x-goog-api-key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _has_header(headers: Mapping[str, str], name: str) -> bool:
    lowered = name.lower()
    return any(existing.lower() == lowered for existing in headers)


def _response_error_detail(response: httpx.Response, *, secret: str) -> str:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None
    detail = ""
    if isinstance(payload, dict):
        raw_error = payload.get("error")
        if isinstance(raw_error, Mapping):
            value = raw_error.get("message") or raw_error.get("detail")
        else:
            value = raw_error or payload.get("message") or payload.get("detail")
        if isinstance(value, str):
            detail = value
    if not detail:
        text = response.text.strip()
        if text and "<html" not in text.lower():
            detail = text
    if not detail:
        return ""
    secrets = (secret,) if secret else ()
    return str(redact(detail, secrets=secrets)).strip()[:_MAX_DETAIL]


def _catalog_entries(payload: Any, adapter: str) -> Optional[Sequence[Any]]:
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, Mapping):
        return None
    if adapter == "gemini_generate_content":
        models = payload.get("models")
        if isinstance(models, list):
            return models
    data = payload.get("data")
    if isinstance(data, list):
        return data
    models = payload.get("models")
    if isinstance(models, list):
        return models
    return None


def _discovered_from_entry(entry: Any, adapter: str) -> Optional[DiscoveredModel]:
    if not isinstance(entry, Mapping):
        return None
    raw_id = (
        entry.get("name")
        if adapter == "gemini_generate_content"
        else entry.get("id")
    )
    if not isinstance(raw_id, str):
        return None
    raw_id = raw_id.strip()
    if not _valid_model_id(raw_id):
        return None
    model_id = (
        raw_id.removeprefix("models/")
        if adapter == "gemini_generate_content"
        else raw_id
    )
    if not _valid_model_id(model_id):
        return None

    input_price, output_price, cache_price = _pricing_metadata(entry)
    return DiscoveredModel(
        model_id=model_id,
        display_name=_display_name(entry, raw_id),
        context_window=_catalog_context_window(entry),
        max_output_tokens=_catalog_output_tokens(entry),
        input_price_per_million=input_price,
        output_price_per_million=output_price,
        supports_tools=_tools_capability(entry),
        supports_vision=_vision_capability(entry),
        supports_structured_output=_structured_output_capability(entry),
        supports_streaming=_explicit_capability(entry, "supports_streaming", "streaming"),
        cache_read_price_per_million=cache_price,
        raw=dict(entry),
    )


def _display_name(entry: Mapping[str, Any], raw_id: str) -> Optional[str]:
    name = entry.get("displayName") or entry.get("display_name") or entry.get("name")
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name or name in {raw_id, raw_id.removeprefix("models/")}:
        return None
    return name[:200]


def _catalog_context_window(entry: Mapping[str, Any]) -> Optional[int]:
    top_provider = entry.get("top_provider")
    nested = (
        top_provider.get("context_length")
        if isinstance(top_provider, Mapping)
        else None
    )
    return _positive_int(
        entry.get("inputTokenLimit"),
        entry.get("context_length"),
        entry.get("context_window"),
        entry.get("max_model_len"),
        entry.get("max_input_tokens"),
        entry.get("max_context_length"),
        nested,
    )


def _catalog_output_tokens(entry: Mapping[str, Any]) -> Optional[int]:
    top_provider = entry.get("top_provider")
    nested = (
        top_provider.get("max_completion_tokens")
        if isinstance(top_provider, Mapping)
        else None
    )
    return _positive_int(
        entry.get("outputTokenLimit"),
        entry.get("max_output_tokens"),
        entry.get("max_completion_tokens"),
        nested,
    )


def _pricing_metadata(
    entry: Mapping[str, Any],
) -> tuple[Optional[float], Optional[float], Optional[float]]:
    """USD-per-million prices from an OpenRouter-style or explicit catalog.

    ``pricing.prompt`` / ``pricing.completion`` are per-token values (the
    OpenRouter shape) and are scaled by one million.  Explicit
    ``*_per_million`` fields are already scaled.  Unparseable, negative, or
    non-finite values are unpublished, never zero.
    """

    input_price: Optional[float] = None
    output_price: Optional[float] = None
    cache_price: Optional[float] = None
    pricing = entry.get("pricing")
    if isinstance(pricing, Mapping):
        input_price = _per_token_price(pricing.get("prompt"))
        if input_price is None:
            input_price = _per_token_price(pricing.get("input_cost_per_token"))
        if input_price is None:
            input_price = _per_million_price(pricing.get("input_per_million"))
        output_price = _per_token_price(pricing.get("completion"))
        if output_price is None:
            output_price = _per_token_price(pricing.get("output_cost_per_token"))
        if output_price is None:
            output_price = _per_million_price(pricing.get("output_per_million"))
        cache_price = _per_token_price(pricing.get("input_cache_read"))
        if cache_price is None:
            cache_price = _per_million_price(pricing.get("cache_read_per_million"))
    if input_price is None:
        input_price = _per_million_price(entry.get("input_price_per_million"))
    if output_price is None:
        output_price = _per_million_price(entry.get("output_price_per_million"))
    return input_price, output_price, cache_price


def _per_token_price(value: Any) -> Optional[float]:
    # Decimal keeps the published precision: 0.0000001 USD/token must read as
    # exactly 0.1 USD/M, not 0.09999999999999999 from binary multiplication.
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value.strip()
    else:
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0:
        return None
    scaled = float(number * Decimal(1_000_000))
    if not math.isfinite(scaled) or scaled < 0:
        return None
    return scaled


def _per_million_price(value: Any) -> Optional[float]:
    return _non_negative_number(value)


def _non_negative_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _positive_int(*values: Any) -> Optional[int]:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, float) and math.isfinite(value) and value > 0:
            if float(value).is_integer():
                return int(value)
    return None


def _tools_capability(entry: Mapping[str, Any]) -> Optional[bool]:
    explicit = _explicit_capability(
        entry, "supports_tools", "tools", "function_calling"
    )
    if explicit is not None:
        return explicit
    return _parameter_capability(entry, "tools", "tool_choice")


def _structured_output_capability(entry: Mapping[str, Any]) -> Optional[bool]:
    explicit = _explicit_capability(
        entry,
        "supports_structured_output",
        "structured_output",
        "structured_outputs",
    )
    if explicit is not None:
        return explicit
    return _parameter_capability(entry, "structured_outputs", "response_format")


def _vision_capability(entry: Mapping[str, Any]) -> Optional[bool]:
    explicit = _explicit_capability(entry, "supports_vision", "vision")
    if explicit is not None:
        return explicit
    architecture = entry.get("architecture")
    modalities: Any = entry.get("input_modalities")
    modality_text: Any = None
    if isinstance(architecture, Mapping):
        if modalities is None:
            modalities = architecture.get("input_modalities")
        modality_text = architecture.get("modality")
    if isinstance(modalities, (list, tuple)):
        published = {str(item).strip().lower() for item in modalities}
        return "image" in published
    if isinstance(modality_text, str) and modality_text.strip():
        return "image" in modality_text.lower()
    return None


def _parameter_capability(
    entry: Mapping[str, Any], *names: str
) -> Optional[bool]:
    parameters = entry.get("supported_parameters")
    if not isinstance(parameters, (list, tuple)):
        return None
    published = {str(item).strip().lower() for item in parameters}
    return any(name in published for name in names)


def _explicit_capability(
    entry: Mapping[str, Any], *names: str
) -> Optional[bool]:
    for name in names:
        value = entry.get(name)
        if isinstance(value, bool):
            return value
    capabilities = entry.get("capabilities")
    if isinstance(capabilities, Mapping):
        for name in names:
            value = capabilities.get(name)
            if isinstance(value, bool):
                return value
    return None


def _valid_model_id(value: str) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= _MAX_MODEL_ID
        and not any(char in value for char in "\r\n\x00")
    )


def _valid_display_name(value: str) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= _MAX_DISPLAY_NAME
        and not any(char in value for char in "\r\n\x00")
    )


def _optional_positive_int(value: Optional[int], label: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label} must be a positive integer")


def _optional_price(value: Optional[float], label: str) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) < 0
    ):
        raise ValueError(f"{label} must be finite and non-negative")


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "DISCOVERED_PROVENANCE",
    "CredentialResolver",
    "CredentialSource",
    "DiscoveredModel",
    "ModelDiscovery",
    "ModelDiscoveryError",
    "discover_models",
    "discover_models_result",
    "metadata_for_registry",
    "model_record",
]