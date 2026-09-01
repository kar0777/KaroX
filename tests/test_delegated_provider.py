from __future__ import annotations

from pathlib import Path
from typing import Iterator

import pytest

from karox.delegated_provider import GrantEnforcedProvider
from karox.model_grants import (
    BillingRate,
    DelegationDenied,
    GrantScope,
    LocalGrantApproval,
    ModelGrant,
    ModelGrantStore,
    ModelSelector,
    PreparedModelCall,
    PricingProof,
)
from karox.providers import (
    ModelEvent,
    ModelEventKind,
    ModelMessage,
    ModelRequest,
    ModelResponse,
)


NOW = 1_800_000_000.0
SCOPE = GrantScope(connection_id="chatgpt-web", session_id="session-a", project_id="project-a")


def proof(*, free: bool = True) -> PricingProof:
    rate = 0.0 if free else 1.0
    return PricingProof(
        provider_id="openrouter",
        model_id="stealth/ox-alpha" if free else "vendor/paid",
        rates=(
            BillingRate("input", "million_input_tokens", rate),
            BillingRate("output", "million_output_tokens", 0.0 if free else 2.0),
            BillingRate("request", "request", 0.0),
        ),
        source="provider-catalog",
        observed_at=NOW - 1,
        authoritative=True,
        dimensions_complete=True,
    )


def grant(*, free_only: bool = True, money: int = 0) -> ModelGrant:
    return ModelGrant(
        grant_id="grant-a",
        scope=SCOPE,
        selectors=(ModelSelector("openrouter", "*"),),
        free_only=free_only,
        max_cost_microusd=money,
        max_calls=10,
        max_input_tokens=10_000,
        max_output_tokens=10_000,
        max_request_output_tokens=2_000,
        max_parallel=2,
        allow_fallback=False,
        issued_at=NOW - 10,
        expires_at=NOW + 3600,
        max_price_age_seconds=600,
        approval=LocalGrantApproval("approval-a", "tui", NOW - 10),
    )


def request(*, model: str = "stealth/ox-alpha", correlation_id: str = "turn-a") -> ModelRequest:
    return ModelRequest(
        model=model,
        messages=(ModelMessage("user", "review this patch"),),
        max_output_tokens=200,
        correlation_id=correlation_id,
    )


class FakePricing:
    def __init__(self, *, free: bool = True, upper: int = 0, actual: int | None = 0) -> None:
        self.free = free
        self.upper = upper
        self.actual = actual
        self.prepared: list[ModelRequest] = []
        self.priced: list[ModelResponse] = []

    def prepare(self, item: ModelRequest) -> PreparedModelCall:
        self.prepared.append(item)
        return PreparedModelCall(
            provider_id="openrouter",
            model_id=item.model,
            input_token_upper_bound=100,
            output_token_upper_bound=item.max_output_tokens or 200,
            cost_upper_bound_microusd=self.upper,
            pricing=proof(free=self.free),
            payload=item,
        )

    def actual_cost_microusd(
        self, call: PreparedModelCall, response: ModelResponse
    ) -> tuple[int | None, str]:
        self.priced.append(response)
        return self.actual, "provider_reported" if self.actual is not None else "unavailable"


class FakeProvider:
    provider_name = "fake-openrouter"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.complete_calls: list[ModelRequest] = []
        self.stream_calls: list[ModelRequest] = []

    @staticmethod
    def response() -> ModelResponse:
        return ModelResponse(
            content="ok",
            tool_calls=(),
            finish_reason="stop",
            usage={"prompt_tokens": 80, "completion_tokens": 40},
            response_id="resp-a",
        )

    def complete(self, item: ModelRequest) -> ModelResponse:
        self.complete_calls.append(item)
        if self.fail:
            raise RuntimeError("connection reset")
        return self.response()

    def stream(self, item: ModelRequest) -> Iterator[ModelEvent]:
        self.stream_calls.append(item)
        if self.fail:
            raise RuntimeError("connection reset")
        yield ModelEvent(ModelEventKind.TEXT_DELTA, text_delta="ok")
        yield ModelEvent(ModelEventKind.USAGE, usage={"prompt_tokens": 80, "completion_tokens": 40})
        yield ModelEvent(
            ModelEventKind.COMPLETION,
            finish_reason="stop",
            response_id="resp-a",
            response=self.response(),
        )


def wrapper(
    tmp_path: Path,
    *,
    local_grant: ModelGrant,
    provider: FakeProvider | None = None,
    pricing: FakePricing | None = None,
) -> tuple[GrantEnforcedProvider, ModelGrantStore, FakeProvider, FakePricing]:
    store = ModelGrantStore(tmp_path / "grants.sqlite3", clock=lambda: NOW)
    store.issue(local_grant)
    provider = provider or FakeProvider()
    pricing = pricing or FakePricing(free=local_grant.free_only)
    wrapped = GrantEnforcedProvider(
        provider,
        provider_id="openrouter",
        grant_store=store,
        grant_id=local_grant.grant_id,
        scope=SCOPE,
        pricing=pricing,
        idempotency_prefix="hosted-op-1",
    )
    return wrapped, store, provider, pricing


def test_complete_reserves_before_provider_and_settles_usage(tmp_path: Path) -> None:
    wrapped, store, provider, pricing = wrapper(tmp_path, local_grant=grant())
    response = wrapped.complete(request())
    assert response.content == "ok"
    assert len(provider.complete_calls) == 1
    assert len(pricing.prepared) == 1
    assert len(pricing.priced) == 1
    state = store.status("grant-a")
    assert state.used_calls == 1
    assert state.used_input_tokens == 80
    assert state.used_output_tokens == 40
    assert state.active_reservations == 0
    assert wrapped.last_receipt is not None
    assert wrapped.last_receipt.spent_microusd == 0


def test_denied_free_only_call_never_reaches_provider(tmp_path: Path) -> None:
    provider = FakeProvider()
    pricing = FakePricing(free=False, upper=500, actual=500)
    wrapped, store, _, _ = wrapper(
        tmp_path, local_grant=grant(), provider=provider, pricing=pricing
    )
    with pytest.raises(DelegationDenied) as caught:
        wrapped.complete(request(model="vendor/paid"))
    assert caught.value.code == "not_free"
    assert provider.complete_calls == []
    assert store.status("grant-a").used_calls == 0


def test_duplicate_correlation_id_does_not_dispatch_twice(tmp_path: Path) -> None:
    wrapped, store, provider, _ = wrapper(tmp_path, local_grant=grant())
    wrapped.complete(request(correlation_id="same"))
    with pytest.raises(DelegationDenied) as caught:
        wrapped.complete(request(correlation_id="same"))
    assert caught.value.code == "already_settled"
    assert len(provider.complete_calls) == 1
    assert store.status("grant-a").used_calls == 1


def test_provider_failure_after_dispatch_consumes_reserved_upper_bound(tmp_path: Path) -> None:
    local = grant(free_only=False, money=1000)
    provider = FakeProvider(fail=True)
    pricing = FakePricing(free=False, upper=700, actual=None)
    wrapped, store, _, _ = wrapper(
        tmp_path, local_grant=local, provider=provider, pricing=pricing
    )
    with pytest.raises(RuntimeError):
        wrapped.complete(request(model="vendor/paid"))
    state = store.status("grant-a")
    assert state.used_calls == 1
    assert state.spent_microusd == 700
    assert state.used_input_tokens == 100
    assert state.used_output_tokens == 200


def test_stream_holds_terminal_event_until_grant_is_settled(tmp_path: Path) -> None:
    wrapped, store, _, _ = wrapper(tmp_path, local_grant=grant())
    events = wrapped.stream(request())
    first = next(events)
    assert first.kind is ModelEventKind.TEXT_DELTA
    mid = store.status("grant-a")
    assert mid.active_reservations == 1
    rest = list(events)
    assert [event.kind for event in rest] == [ModelEventKind.USAGE, ModelEventKind.COMPLETION]
    final = store.status("grant-a")
    assert final.active_reservations == 0
    assert final.used_input_tokens == 80
    assert final.used_output_tokens == 40
    assert wrapped.last_receipt is not None


def test_stream_consumer_cancel_is_conservatively_accounted(tmp_path: Path) -> None:
    local = grant(free_only=False, money=1000)
    pricing = FakePricing(free=False, upper=700, actual=500)
    wrapped, store, _, _ = wrapper(tmp_path, local_grant=local, pricing=pricing)
    events = wrapped.stream(request(model="vendor/paid"))
    assert next(events).kind is ModelEventKind.TEXT_DELTA
    events.close()
    state = store.status("grant-a")
    assert state.active_reservations == 0
    assert state.spent_microusd == 700
    assert state.used_input_tokens == 100
    assert state.used_output_tokens == 200


def test_wrapper_contract_has_no_credential_or_keyring_field(tmp_path: Path) -> None:
    wrapped, _, _, _ = wrapper(tmp_path, local_grant=grant())
    assert "credential" not in vars(wrapped)
    assert "keyring" not in vars(wrapped)
    assert wrapped.provider_name == "grant:openrouter"
