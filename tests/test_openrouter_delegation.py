from __future__ import annotations

from typing import Any

import pytest

from karox.model_grants import PreparedModelCall
from karox.openrouter_delegation import (
    OpenRouterPriceCappedChatProvider,
    OpenRouterPricingError,
    OpenRouterPricingResolver,
)
from karox.providers import ModelMessage, ModelRequest, ModelResponse


NOW = 1_800_000_000.0


class FakeResponse:
    def __init__(self, payload: dict[str, Any], status_code: int = 200) -> None:
        self.payload = payload
        self.status_code = status_code

    def json(self) -> dict[str, Any]:
        return self.payload


class FakeClient:
    def __init__(self, owner: "FakeHTTP") -> None:
        self.owner = owner

    def __enter__(self) -> "FakeClient":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.owner.calls.append((url, kwargs))
        if url.endswith("/models/user"):
            return FakeResponse({"data": self.owner.models})
        if url.endswith("/generation"):
            return FakeResponse({"data": {"total_cost": self.owner.total_cost}})
        return FakeResponse({}, status_code=404)


class FakeHTTP:
    def __init__(self, models: list[dict[str, Any]], *, total_cost: str = "0") -> None:
        self.models = models
        self.total_cost = total_cost
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def factory(self) -> FakeClient:
        return FakeClient(self)


def model(model_id: str, pricing: dict[str, str]) -> dict[str, Any]:
    return {"id": model_id, "pricing": pricing}


def request(
    model_id: str = "stealth/ox-alpha",
    *,
    correlation_id: str = "turn-a",
    max_output: int | None = 200,
) -> ModelRequest:
    return ModelRequest(
        model=model_id,
        messages=(ModelMessage("user", "review this diff"),),
        max_output_tokens=max_output,
        correlation_id=correlation_id,
    )


def resolver(http: FakeHTTP) -> OpenRouterPricingResolver:
    return OpenRouterPricingResolver(
        base_url="https://openrouter.ai/api/v1",
        credential=lambda: "opaque-local-test-value",
        cache_seconds=120,
        timeout_seconds=2,
        clock=lambda: NOW,
        client_factory=http.factory,
    )


def test_free_model_builds_zero_cost_fresh_request_specific_proof() -> None:
    http = FakeHTTP(
        [
            model(
                "stealth/ox-alpha",
                {
                    "prompt": "0",
                    "completion": "0",
                    "request": "0",
                    "image": "0",
                    "web_search": "0.005",
                    "internal_reasoning": "0",
                    "input_cache_read": "0",
                },
            )
        ]
    )
    pricing = resolver(http)
    item = request()
    prepared = pricing.prepare(item)
    assert isinstance(prepared, PreparedModelCall)
    assert prepared.payload is item
    assert prepared.cost_upper_bound_microusd == 0
    assert prepared.pricing.verified_free(now=NOW, max_age_seconds=300)
    assert "web_search" not in {rate.name for rate in prepared.pricing.rates}
    assert len(http.calls) == 1
    assert http.calls[0][0].endswith("/models/user")


def test_catalog_cache_avoids_repeated_authenticated_pricing_round_trips() -> None:
    http = FakeHTTP([model("stealth/ox-alpha", {"prompt": "0", "completion": "0"})])
    pricing = resolver(http)
    pricing.prepare(request(correlation_id="one"))
    pricing.prepare(request(correlation_id="two"))
    assert len(http.calls) == 1


def test_paid_request_upper_bound_includes_prompt_output_request_and_cache() -> None:
    http = FakeHTTP(
        [
            model(
                "vendor/paid",
                {
                    "prompt": "0.000001",
                    "completion": "0.000002",
                    "request": "0.0001",
                    "input_cache_read": "0.00000025",
                    "input_cache_write": "0.00000125",
                    "internal_reasoning": "0.000002",
                },
            )
        ]
    )
    prepared = resolver(http).prepare(request("vendor/paid", max_output=300))
    assert prepared.cost_upper_bound_microusd > 0
    assert prepared.input_token_upper_bound > len("review this diff")
    assert prepared.output_token_upper_bound == 300


def test_openrouter_server_guard_disables_hidden_fallbacks_and_caps_unit_prices() -> None:
    http = FakeHTTP(
        [
            model(
                "vendor/paid",
                {
                    "prompt": "0.000001",
                    "completion": "0.000002",
                    "request": "0.0001",
                    "image": "0",
                },
            )
        ]
    )
    pricing = resolver(http)
    item = request("vendor/paid")
    pricing.prepare(item)
    guard = pricing.provider_guard(item)
    assert guard["allow_fallbacks"] is False
    assert guard["sort"] == "price"
    assert guard["require_parameters"] is True
    assert guard["max_price"] == {
        "prompt": 1.0,
        "completion": 2.0,
        "request": 0.0001,
    }


def test_price_capped_transport_injects_guard_and_forces_zero_hidden_retries() -> None:
    http = FakeHTTP([model("stealth/ox-alpha", {"prompt": "0", "completion": "0"})])
    pricing = resolver(http)
    provider = OpenRouterPriceCappedChatProvider(
        "https://openrouter.ai/api/v1",
        credential=lambda: "opaque-local-test-value",
        pricing_resolver=pricing,
        max_transport_retries=9,
    )
    item = request()
    payload = provider._guarded_request_payload(item)
    assert provider.max_transport_retries == 0
    assert payload["provider"]["allow_fallbacks"] is False
    assert payload["provider"]["max_price"]["prompt"] == 0.0
    assert payload["provider"]["max_price"]["completion"] == 0.0


def test_positive_unknown_billing_dimension_fails_closed() -> None:
    http = FakeHTTP(
        [
            model(
                "stealth/ox-alpha",
                {"prompt": "0", "completion": "0", "future_magic_sku": "0.01"},
            )
        ]
    )
    with pytest.raises(OpenRouterPricingError, match="unsupported non-zero billing"):
        resolver(http).prepare(request())


def test_server_web_call_with_positive_unbounded_search_price_is_refused() -> None:
    http = FakeHTTP(
        [
            model(
                "stealth/ox-alpha:online",
                {"prompt": "0", "completion": "0", "web_search": "0.005"},
            )
        ]
    )
    with pytest.raises(OpenRouterPricingError, match="web-search pricing"):
        resolver(http).prepare(request("stealth/ox-alpha:online"))


def test_missing_output_cap_is_refused_before_catalog_or_provider_call() -> None:
    http = FakeHTTP([model("stealth/ox-alpha", {"prompt": "0", "completion": "0"})])
    with pytest.raises(OpenRouterPricingError, match="output token cap"):
        resolver(http).prepare(request(max_output=None))
    assert http.calls == []


def test_generation_metadata_supplies_measured_cost_without_exposing_credential() -> None:
    http = FakeHTTP(
        [model("vendor/paid", {"prompt": "0.000001", "completion": "0.000002"})],
        total_cost="0.0007",
    )
    pricing = resolver(http)
    prepared = pricing.prepare(request("vendor/paid"))
    response = ModelResponse(
        content="ok",
        tool_calls=(),
        finish_reason="stop",
        usage={"prompt_tokens": 10, "completion_tokens": 20},
        response_id="gen-test",
    )
    cost, evidence = pricing.actual_cost_microusd(prepared, response)
    assert cost == 700
    assert evidence == "provider_reported"
    generation_call = http.calls[-1]
    assert generation_call[0].endswith("/generation")
    assert generation_call[1]["params"] == {"id": "gen-test"}
    assert "credential" not in prepared.__dataclass_fields__


def test_http_failure_body_is_never_reflected_in_error() -> None:
    class DeniedClient(FakeClient):
        def get(self, url: str, **kwargs: Any) -> FakeResponse:
            self.owner.calls.append((url, kwargs))
            return FakeResponse({"error": "sensitive-account-detail"}, status_code=401)

    http = FakeHTTP([])
    pricing = OpenRouterPricingResolver(
        base_url="https://openrouter.ai/api/v1",
        credential=lambda: "opaque-local-test-value",
        clock=lambda: NOW,
        client_factory=lambda: DeniedClient(http),
    )
    with pytest.raises(OpenRouterPricingError) as caught:
        pricing.prepare(request())
    assert "sensitive-account-detail" not in str(caught.value)
    assert "401" in str(caught.value)
