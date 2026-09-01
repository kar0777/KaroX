"""Strict OpenRouter pricing and routing for grant-bound model delegation.

OpenRouter's ordinary routing intentionally optimizes uptime and can fall back to
another provider endpoint.  That is useful for interactive chat but incompatible
with a *hard* user spending grant unless the fallback is independently bounded.
This module therefore combines two controls:

* a fresh authenticated user-model pricing snapshot used for local worst-case
  reservation; and
* OpenRouter's request-level ``provider.max_price`` with hidden fallbacks
  disabled, so the server cannot silently choose an endpoint above the unit
  price KaroX authorized.

The API credential remains a lazy local callable.  It is used only to build the
Authorization header inside this module/concrete provider and is never returned,
logged, stored in a grant, or accepted from the hosted caller.
"""

from __future__ import annotations

import json
import threading
import time
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from typing import Any, Callable, Iterator, Mapping, Optional
from urllib.parse import urlsplit, urlunsplit

import httpx

from .model_grants import BillingRate, PreparedModelCall, PricingProof
from .providers import (
    ModelEvent,
    ModelRequest,
    ModelResponse,
    OpenAIChatCompletionsProvider,
    ProviderError,
    ProviderErrorKind,
)


_MICRO_USD = Decimal("1000000")
_KNOWN_PRICING_FIELDS = frozenset(
    {
        "prompt",
        "completion",
        "request",
        "image",
        "web_search",
        "internal_reasoning",
        "input_cache_read",
        "input_cache_write",
    }
)
_PRICE_CEILING_FIELDS = frozenset({"prompt", "completion", "request", "image"})


class OpenRouterDelegationError(RuntimeError):
    pass


class OpenRouterPricingError(OpenRouterDelegationError):
    pass


def _api_root(base_url: str) -> str:
    split = urlsplit(base_url)
    if split.scheme != "https" or not split.netloc:
        raise ValueError("OpenRouter delegation requires an HTTPS API base URL")
    path = split.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")]
    elif path.endswith("/v1"):
        pass
    elif "/v1/" in path:
        path = path.split("/v1/", 1)[0] + "/v1"
    else:
        path = path + "/v1"
    return urlunsplit((split.scheme, split.netloc, path, "", "")).rstrip("/")


def _decimal_price(value: Any, *, label: str) -> Decimal:
    if isinstance(value, bool):
        raise OpenRouterPricingError(f"invalid OpenRouter {label} price")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise OpenRouterPricingError(f"invalid OpenRouter {label} price") from exc
    if not result.is_finite() or result < 0:
        raise OpenRouterPricingError(f"invalid OpenRouter {label} price")
    return result


def _microusd(value: Decimal) -> int:
    if value <= 0:
        return 0
    return int((value * _MICRO_USD).to_integral_value(rounding=ROUND_CEILING))


def _usage_int(usage: Mapping[str, Any], *names: str) -> int:
    for name in names:
        value = usage.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _image_count(request: ModelRequest) -> int:
    return sum(len(message.images) for message in request.messages)


def _uses_server_web(request: ModelRequest) -> bool:
    model = request.model.lower()
    if model.endswith(":online"):
        return True
    return any(tool.name.lower().startswith("openrouter:web_") for tool in request.tools)


def _conservative_input_tokens(request: ModelRequest) -> int:
    """Upper-bound ordinary tokenizer tokens by the exact UTF-8 wire bytes.

    OpenAI-compatible tokenizers tokenize byte-backed text; one token per wire
    byte plus a structural margin is intentionally pessimistic.  Images are in
    the payload as base64 data URLs, so their wire expansion is included too.
    This is a financial reservation bound, not a context-quality estimate.
    """

    payload = OpenAIChatCompletionsProvider._request_payload(request)
    wire = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return max(1, len(wire) + 1024)


class OpenRouterPricingResolver:
    """Fresh request-specific pricing proof for OpenRouter Chat Completions."""

    def __init__(
        self,
        *,
        base_url: str,
        credential: Callable[[], str],
        cache_seconds: float = 120.0,
        timeout_seconds: float = 10.0,
        clock: Callable[[], float] = time.time,
        client_factory: Callable[[], httpx.Client] = httpx.Client,
    ) -> None:
        if not callable(credential):
            raise ValueError("OpenRouter pricing requires a local credential accessor")
        if not 1 <= float(cache_seconds) <= 3600:
            raise ValueError("OpenRouter pricing cache must be between 1s and 1h")
        if not 0.5 <= float(timeout_seconds) <= 60:
            raise ValueError("OpenRouter pricing timeout must be between 0.5s and 60s")
        self.api_root = _api_root(base_url)
        self._credential = credential
        self.cache_seconds = float(cache_seconds)
        self.timeout_seconds = float(timeout_seconds)
        self.clock = clock
        self.client_factory = client_factory
        self._lock = threading.RLock()
        self._catalog_at = 0.0
        self._catalog: dict[str, Mapping[str, Any]] = {}
        self._request_rates: dict[str, dict[str, Decimal]] = {}

    def _headers(self) -> dict[str, str]:
        secret = self._credential()
        if not isinstance(secret, str) or not secret:
            raise OpenRouterPricingError("OpenRouter credential is unavailable")
        return {"Authorization": f"Bearer {secret}", "Accept": "application/json"}

    def _get_json(self, path: str, *, params: Optional[Mapping[str, str]] = None) -> Mapping[str, Any]:
        try:
            with self.client_factory() as client:
                response = client.get(
                    self.api_root + path,
                    headers=self._headers(),
                    params=params,
                    timeout=self.timeout_seconds,
                )
        except (httpx.HTTPError, OSError) as exc:
            raise OpenRouterPricingError("OpenRouter pricing request failed") from exc
        if response.status_code != 200:
            # Never include the provider body: it may echo identifiers or account
            # state, and an authorization failure needs no secret-bearing detail.
            raise OpenRouterPricingError(
                f"OpenRouter pricing request returned HTTP {response.status_code}"
            )
        try:
            value = response.json()
        except (ValueError, TypeError) as exc:
            raise OpenRouterPricingError("OpenRouter pricing response was not JSON") from exc
        if not isinstance(value, Mapping):
            raise OpenRouterPricingError("OpenRouter pricing response root is invalid")
        return value

    def _refresh_catalog(self) -> None:
        now = float(self.clock())
        with self._lock:
            if self._catalog and now - self._catalog_at <= self.cache_seconds:
                return
        value = self._get_json("/models/user")
        raw_models = value.get("data")
        if not isinstance(raw_models, list) or not raw_models:
            raise OpenRouterPricingError("OpenRouter user model catalog is empty")
        catalog: dict[str, Mapping[str, Any]] = {}
        for item in raw_models:
            if not isinstance(item, Mapping):
                continue
            model_id = item.get("id")
            if isinstance(model_id, str) and model_id:
                catalog[model_id] = item
        if not catalog:
            raise OpenRouterPricingError("OpenRouter user model catalog contained no valid models")
        with self._lock:
            self._catalog = catalog
            self._catalog_at = now

    def _raw_pricing(self, model_id: str) -> Mapping[str, Any]:
        self._refresh_catalog()
        with self._lock:
            model = self._catalog.get(model_id)
        if model is None:
            raise OpenRouterPricingError("model is unavailable under current OpenRouter account policy")
        pricing = model.get("pricing")
        if not isinstance(pricing, Mapping) or not pricing:
            raise OpenRouterPricingError("OpenRouter model has no usable pricing object")
        return pricing

    def _request_pricing(self, request: ModelRequest) -> dict[str, Decimal]:
        raw = self._raw_pricing(request.model)
        parsed: dict[str, Decimal] = {}
        for name, value in raw.items():
            if not isinstance(name, str) or not name:
                raise OpenRouterPricingError("OpenRouter pricing field name is invalid")
            parsed[name] = _decimal_price(value, label=name)

        # Prompt and completion are the standard Chat Completions billables. A
        # catalog that omits either cannot safely authorize a spend.
        if "prompt" not in parsed or "completion" not in parsed:
            raise OpenRouterPricingError("OpenRouter pricing omits prompt/completion rates")

        relevant = {"prompt", "completion"}
        # These can occur on an ordinary KaroX agent request even when the user
        # did not explicitly ask for a provider feature.
        relevant.update(name for name in ("request", "internal_reasoning", "input_cache_read", "input_cache_write") if name in parsed)
        if _image_count(request):
            if "image" not in parsed:
                raise OpenRouterPricingError("image request has no explicit image price")
            relevant.add("image")
        if _uses_server_web(request):
            if "web_search" not in parsed:
                raise OpenRouterPricingError("web-enabled request has no explicit web-search price")
            relevant.add("web_search")

        # Future non-zero pricing fields are not silently ignored. Zero-valued
        # unknown fields are harmless; a positive one means KaroX does not yet
        # know how this request could be charged and must fail closed.
        unknown_positive = [
            name for name, price in parsed.items() if name not in _KNOWN_PRICING_FIELDS and price > 0
        ]
        if unknown_positive:
            raise OpenRouterPricingError(
                "OpenRouter introduced an unsupported non-zero billing dimension"
            )
        return {name: parsed.get(name, Decimal("0")) for name in relevant}

    def _cost_upper_bound(
        self,
        request: ModelRequest,
        rates: Mapping[str, Decimal],
        *,
        input_upper: int,
        output_upper: int,
    ) -> Decimal:
        total = Decimal("0")
        total += rates.get("prompt", Decimal("0")) * input_upper
        total += rates.get("completion", Decimal("0")) * output_upper
        total += rates.get("request", Decimal("0"))
        total += rates.get("image", Decimal("0")) * _image_count(request)
        # Treat reasoning as additional to completion and cache read/write as
        # additional to ordinary prompt price. Providers often replace rather
        # than add these rates; summing is deliberately more conservative.
        total += rates.get("internal_reasoning", Decimal("0")) * output_upper
        total += rates.get("input_cache_read", Decimal("0")) * input_upper
        total += rates.get("input_cache_write", Decimal("0")) * input_upper
        web_price = rates.get("web_search", Decimal("0"))
        if web_price > 0:
            # Server-side agentic web tools may execute 0..N times. ModelRequest
            # has no deterministic maximum, so a hard-money grant cannot safely
            # authorize a positive per-search price yet.
            raise OpenRouterPricingError(
                "positive server web-search pricing has no deterministic request bound"
            )
        return total

    def prepare(self, request: ModelRequest) -> PreparedModelCall:
        if request.max_output_tokens is None:
            raise OpenRouterPricingError("delegated OpenRouter call requires an output token cap")
        rates = self._request_pricing(request)
        input_upper = _conservative_input_tokens(request)
        output_upper = int(request.max_output_tokens)
        upper = self._cost_upper_bound(
            request,
            rates,
            input_upper=input_upper,
            output_upper=output_upper,
        )
        observed_at = float(self.clock())
        proof = PricingProof(
            provider_id="openrouter",
            model_id=request.model,
            rates=tuple(
                BillingRate(name, _rate_unit(name), float(value))
                for name, value in sorted(rates.items())
            ),
            source="openrouter:/models/user",
            observed_at=observed_at,
            authoritative=True,
            dimensions_complete=True,
            currency="USD",
        )
        with self._lock:
            self._request_rates[request.correlation_id] = dict(rates)
        return PreparedModelCall(
            provider_id="openrouter",
            model_id=request.model,
            input_token_upper_bound=input_upper,
            output_token_upper_bound=output_upper,
            cost_upper_bound_microusd=_microusd(upper),
            pricing=proof,
            payload=request,
        )

    def provider_guard(self, request: ModelRequest) -> dict[str, Any]:
        """Return OpenRouter routing constraints for the exact prepared call."""
        with self._lock:
            rates = self._request_rates.get(request.correlation_id)
        if rates is None:
            # This path is local and safe; it exists so direct use cannot bypass
            # pricing preparation just by skipping GrantEnforcedProvider.
            self.prepare(request)
            with self._lock:
                rates = self._request_rates[request.correlation_id]
        max_price: dict[str, float] = {}
        for name in _PRICE_CEILING_FIELDS:
            if name not in rates:
                continue
            price = rates[name]
            if name in {"prompt", "completion"}:
                # OpenRouter max_price expresses token rates as $/million while
                # its catalog pricing object is USD per token.
                max_price[name] = float(price * Decimal("1000000"))
            else:
                max_price[name] = float(price)
        return {
            "sort": "price",
            "allow_fallbacks": False,
            "require_parameters": True,
            "max_price": max_price,
        }

    def actual_cost_microusd(
        self, call: PreparedModelCall, response: ModelResponse
    ) -> tuple[Optional[int], str]:
        """Ask OpenRouter for measured generation cost when an id is available."""
        response_id = response.response_id
        if not isinstance(response_id, str) or not response_id:
            return None, "unavailable"
        try:
            value = self._get_json("/generation", params={"id": response_id})
        except OpenRouterPricingError:
            # The ledger will conservatively retain the preflight upper bound.
            return None, "unavailable"
        data = value.get("data")
        if not isinstance(data, Mapping):
            return None, "unavailable"
        raw_cost = data.get("total_cost")
        try:
            cost = _decimal_price(raw_cost, label="generation total")
        except OpenRouterPricingError:
            return None, "unavailable"
        return _microusd(cost), "provider_reported"


class OpenRouterPriceCappedChatProvider(OpenAIChatCompletionsProvider):
    """Chat transport that applies the resolver's server-side price ceiling."""

    def __init__(self, *args: Any, pricing_resolver: OpenRouterPricingResolver, **kwargs: Any) -> None:
        # Grant-aware policy owns retries. A hidden transport retry would be a
        # second potentially billable attempt under one reservation.
        kwargs["max_transport_retries"] = 0
        super().__init__(*args, **kwargs)
        self.pricing_resolver = pricing_resolver

    def _guarded_request_payload(self, request: ModelRequest) -> dict[str, Any]:
        payload = super()._request_payload(request)
        payload["provider"] = self.pricing_resolver.provider_guard(request)
        return payload

    def stream(self, request: ModelRequest) -> Iterator[ModelEvent]:
        """Send exactly one price-capped transport attempt.

        The parent transport owns mature SSE parsing, credential isolation and
        HTTP-error normalization.  We reuse those helpers but intentionally do
        not reuse its retry loop: every potentially billable retry must return
        to the grant ledger for a new reservation first.
        """

        payload = self._guarded_request_payload(request)
        headers = self._request_headers()
        deadline = time.monotonic() + float(request.deadline_seconds)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError(
                ProviderErrorKind.TRANSPORT,
                "provider request deadline expired",
            )
        response_started = False
        try:
            with httpx.Client(follow_redirects=False) as client:
                with client.stream(
                    "POST",
                    self.endpoint,
                    json=payload,
                    headers=headers,
                    timeout=min(self.timeout_seconds, max(remaining, 0.05)),
                ) as response:
                    response_started = True
                    if response.status_code >= 400:
                        raise self._http_error(response)
                    try:
                        yield from self._stream_events(response, 1, deadline)
                    except httpx.TransportError as exc:
                        raise ProviderError(
                            ProviderErrorKind.TRANSPORT,
                            f"provider stream interrupted: {type(exc).__name__}",
                            status_code=response.status_code,
                        ) from exc
        except ProviderError:
            raise
        except httpx.TransportError as exc:
            message = (
                "provider stream interrupted"
                if response_started
                else "provider transport failed"
            )
            raise ProviderError(
                ProviderErrorKind.TRANSPORT,
                f"{message}: {type(exc).__name__}",
            ) from exc


def _rate_unit(name: str) -> str:
    if name in {"prompt", "completion", "internal_reasoning", "input_cache_read", "input_cache_write"}:
        return "token"
    if name == "image":
        return "image"
    if name == "request":
        return "request"
    if name == "web_search":
        return "web_search"
    return "unknown"


__all__ = [
    "OpenRouterDelegationError",
    "OpenRouterPriceCappedChatProvider",
    "OpenRouterPricingError",
    "OpenRouterPricingResolver",
]
