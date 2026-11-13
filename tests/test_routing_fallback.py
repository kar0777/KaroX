"""Routing retries on the only usable route (audit 2026-11-13)."""

from __future__ import annotations

from types import SimpleNamespace as NS

from karox.provider_types import ModelMessage
from karox.providers import ModelRequest, ProviderError, ProviderErrorKind
from karox.registry import ModelPricing
from karox.routing import RoutedProvider, RouteTarget, RoutingPolicy

PRICING = ModelPricing(
    version="v1", currency="USD", input_per_million=1.0, output_per_million=2.0, source="x"
)


def test_a_429_retries_when_the_fallback_cannot_run() -> None:
    """Route b exceeds the privacy limit, so a 429 on a must retry a and say 429."""

    records = {
        "a": NS(provider_id="a", privacy_class="local"),
        "b": NS(provider_id="b", privacy_class="public"),
    }

    class Registry:
        def provider(self, provider_id: str) -> NS:
            return records[provider_id]

        def model(self, provider_id: str, model: str) -> NS:
            return NS(model_id=model, tools="true", streaming="true", pricing=PRICING, max_output_tokens=100)

    calls: list[str] = []

    class Provider:
        def __init__(self, record: NS) -> None:
            self.record = record

        def complete(self, request: ModelRequest) -> None:
            calls.append(self.record.provider_id)
            raise ProviderError(ProviderErrorKind.RATE_LIMIT, "slow down", status_code=429, retry_after=2.0)

    policy = RoutingPolicy(routes=(RouteTarget("a", "m"), RouteTarget("b", "m")), privacy_limit="private")
    routed = RoutedProvider(Registry(), Provider, policy, sleep=lambda _delay: None)
    request = ModelRequest(model="m", messages=(ModelMessage(role="user", content="hi"),))
    try:
        routed.complete(request)
    except ProviderError as exc:
        assert exc.kind is ProviderErrorKind.RATE_LIMIT and exc.status_code == 429
        assert len(calls) > 1 and set(calls) == {"a"}
    else:
        raise AssertionError("every attempt was refused; the call must fail")
