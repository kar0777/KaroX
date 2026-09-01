from __future__ import annotations

import threading
from pathlib import Path

import pytest

from karox.model_grants import (
    BillingRate,
    DelegationDenied,
    DelegationGrantError,
    GrantBoundDelegator,
    GrantScope,
    LocalGrantApproval,
    ModelExecutionResult,
    ModelGrant,
    ModelGrantStore,
    ModelSelector,
    PreparedModelCall,
    PricingProof,
    hash_approval_text,
    usd_to_microusd,
)


NOW = 1_800_000_000.0
SCOPE = GrantScope(connection_id="chatgpt-web", session_id="s1", project_id="p1")


def approval() -> LocalGrantApproval:
    return LocalGrantApproval(
        approval_id="approval-1",
        channel="tui",
        approved_at=NOW - 1,
        approval_text_sha256=hash_approval_text("allow free models"),
    )


def free_proof(model: str = "stealth/ox-alpha", *, observed_at: float = NOW - 10) -> PricingProof:
    return PricingProof(
        provider_id="openrouter",
        model_id=model,
        rates=(
            BillingRate("input", "million_input_tokens", 0.0),
            BillingRate("output", "million_output_tokens", 0.0),
            BillingRate("request", "request", 0.0),
            BillingRate("image", "image", 0.0),
        ),
        source="openrouter-model-catalog",
        observed_at=observed_at,
        authoritative=True,
        dimensions_complete=True,
    )


def paid_proof(model: str = "vendor/paid") -> PricingProof:
    return PricingProof(
        provider_id="openrouter",
        model_id=model,
        rates=(
            BillingRate("input", "million_input_tokens", 1.0),
            BillingRate("output", "million_output_tokens", 2.0),
            BillingRate("request", "request", 0.0),
        ),
        source="openrouter-model-catalog",
        observed_at=NOW - 10,
        authoritative=True,
        dimensions_complete=True,
    )


def grant(
    *,
    free_only: bool = True,
    max_cost_microusd: int = 0,
    max_calls: int = 5,
    max_parallel: int = 2,
    selectors: tuple[ModelSelector, ...] = (ModelSelector("openrouter", "*"),),
    expires_at: float = NOW + 3600,
) -> ModelGrant:
    return ModelGrant(
        grant_id="grant-a",
        scope=SCOPE,
        selectors=selectors,
        free_only=free_only,
        max_cost_microusd=max_cost_microusd,
        max_calls=max_calls,
        max_input_tokens=10_000,
        max_output_tokens=5_000,
        max_request_output_tokens=2_000,
        max_parallel=max_parallel,
        allow_fallback=True,
        issued_at=NOW - 2,
        expires_at=expires_at,
        max_price_age_seconds=3600,
        approval=approval(),
        label="Free coding helpers",
    )


def call(
    *,
    model: str = "stealth/ox-alpha",
    proof: PricingProof | None = None,
    input_upper: int = 100,
    output_upper: int = 200,
    cost_upper: int = 0,
) -> PreparedModelCall:
    return PreparedModelCall(
        provider_id="openrouter",
        model_id=model,
        input_token_upper_bound=input_upper,
        output_token_upper_bound=output_upper,
        cost_upper_bound_microusd=cost_upper,
        pricing=proof or free_proof(model),
        payload={"task": "review the patch"},
    )


@pytest.fixture
def store(tmp_path: Path) -> ModelGrantStore:
    return ModelGrantStore(tmp_path / "grants.sqlite3", clock=lambda: NOW)


def test_issue_is_idempotent_only_for_identical_authority(store: ModelGrantStore) -> None:
    first = store.issue(grant())
    replay = store.issue(grant())
    assert replay.grant == first.grant
    assert len(store.list()) == 1

    with pytest.raises(DelegationGrantError, match="different authority"):
        store.issue(grant(max_calls=4))
    assert store.status("grant-a").grant.max_calls == 5


def test_free_only_grant_accepts_fresh_complete_zero_price(store: ModelGrantStore) -> None:
    store.issue(grant())
    reservation = store.reserve("grant-a", scope=SCOPE, call=call(), idempotency_key="one")
    assert reservation.cost_upper_bound_microusd == 0
    state = store.status("grant-a")
    assert state.used_calls == 1
    assert state.reserved_input_tokens == 100
    assert state.reserved_output_tokens == 200
    assert state.reserved_microusd == 0


@pytest.mark.parametrize(
    ("proof", "code"),
    [
        (
            PricingProof(
                provider_id="openrouter",
                model_id="stealth/ox-alpha",
                rates=(BillingRate("input", "million_input_tokens", 0.0),),
                source="catalog",
                observed_at=NOW - 10,
                authoritative=True,
                dimensions_complete=False,
            ),
            "free_price_unproven",
        ),
        (free_proof(observed_at=NOW - 7200), "free_price_unproven"),
        (
            PricingProof(
                provider_id="openrouter",
                model_id="stealth/ox-alpha",
                rates=(BillingRate("input", "million_input_tokens", 0.0),),
                source="guess",
                observed_at=NOW - 10,
                authoritative=False,
                dimensions_complete=True,
            ),
            "free_price_unproven",
        ),
    ],
)
def test_unknown_stale_or_non_authoritative_free_price_is_denied(
    store: ModelGrantStore, proof: PricingProof, code: str
) -> None:
    store.issue(grant())
    with pytest.raises(DelegationDenied) as caught:
        store.reserve("grant-a", scope=SCOPE, call=call(proof=proof), idempotency_key="one")
    assert caught.value.code == code


def test_free_only_grant_never_falls_through_to_paid_candidate(store: ModelGrantStore) -> None:
    store.issue(grant(selectors=(ModelSelector("openrouter", "*"),)))
    with pytest.raises(DelegationDenied) as caught:
        store.reserve(
            "grant-a",
            scope=SCOPE,
            call=call(model="vendor/paid", proof=paid_proof(), cost_upper=1),
            idempotency_key="paid-fallback",
        )
    assert caught.value.code == "not_free"
    assert store.status("grant-a").used_calls == 0


def test_scope_and_model_selectors_are_hard_boundaries(store: ModelGrantStore) -> None:
    store.issue(grant(selectors=(ModelSelector("openrouter", "stealth/*"),)))
    with pytest.raises(DelegationDenied) as wrong_scope:
        store.reserve(
            "grant-a",
            scope=GrantScope(connection_id="other", session_id="s1", project_id="p1"),
            call=call(),
            idempotency_key="scope",
        )
    assert wrong_scope.value.code == "scope_mismatch"
    with pytest.raises(DelegationDenied) as wrong_model:
        store.reserve(
            "grant-a",
            scope=SCOPE,
            call=call(model="vendor/free", proof=free_proof("vendor/free")),
            idempotency_key="model",
        )
    assert wrong_model.value.code == "model_not_allowed"


def test_expiry_and_revocation_stop_new_calls(store: ModelGrantStore) -> None:
    store.issue(grant(expires_at=NOW + 1))
    store.clock = lambda: NOW + 2
    with pytest.raises(DelegationDenied) as expired:
        store.reserve("grant-a", scope=SCOPE, call=call(), idempotency_key="expired")
    assert expired.value.code == "expired"

    other = ModelGrantStore(store.path, clock=lambda: NOW)
    other.revoke("grant-a", reason="user_pressed_stop")
    with pytest.raises(DelegationDenied) as revoked:
        other.reserve("grant-a", scope=SCOPE, call=call(), idempotency_key="revoked")
    assert revoked.value.code == "revoked"


def test_paid_budget_is_reserved_before_network_and_released_to_actual(store: ModelGrantStore) -> None:
    store.issue(grant(free_only=False, max_cost_microusd=1000))
    first = store.reserve(
        "grant-a",
        scope=SCOPE,
        call=call(model="vendor/paid", proof=paid_proof(), cost_upper=700),
        idempotency_key="first",
    )
    with pytest.raises(DelegationDenied) as blocked:
        store.reserve(
            "grant-a",
            scope=SCOPE,
            call=call(model="vendor/paid", proof=paid_proof(), cost_upper=400),
            idempotency_key="second",
        )
    assert blocked.value.code == "money_budget"

    store.mark_network_started(first.reservation_id)
    settlement = store.settle(
        first.reservation_id,
        input_tokens=80,
        output_tokens=100,
        actual_cost_microusd=500,
        cost_evidence="provider_reported",
    )
    assert settlement.spent_microusd == 500
    second = store.reserve(
        "grant-a",
        scope=SCOPE,
        call=call(model="vendor/paid", proof=paid_proof(), cost_upper=400),
        idempotency_key="second",
    )
    assert second.cost_upper_bound_microusd == 400
    state = store.status("grant-a")
    assert state.spent_microusd == 500
    assert state.reserved_microusd == 400
    assert state.remaining_cost_microusd == 100


def test_parallel_race_cannot_double_spend_one_call_budget(tmp_path: Path) -> None:
    path = tmp_path / "race.sqlite3"
    ModelGrantStore(path, clock=lambda: NOW).issue(grant(max_calls=1, max_parallel=2))
    barrier = threading.Barrier(2)
    results: list[str] = []
    lock = threading.Lock()

    def worker(key: str) -> None:
        local = ModelGrantStore(path, clock=lambda: NOW)
        barrier.wait()
        try:
            local.reserve("grant-a", scope=SCOPE, call=call(), idempotency_key=key)
            value = "allowed"
        except DelegationDenied as exc:
            value = exc.code
        with lock:
            results.append(value)

    threads = [threading.Thread(target=worker, args=("a",)), threading.Thread(target=worker, args=("b",))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert sorted(results) == ["allowed", "call_budget"]
    assert ModelGrantStore(path, clock=lambda: NOW).status("grant-a").used_calls == 1


def test_idempotency_replay_does_not_consume_budget_twice(store: ModelGrantStore) -> None:
    store.issue(grant())
    first = store.reserve("grant-a", scope=SCOPE, call=call(), idempotency_key="same")
    replay = store.reserve("grant-a", scope=SCOPE, call=call(), idempotency_key="same")
    assert replay.reservation_id == first.reservation_id
    assert store.status("grant-a").used_calls == 1


def test_pre_dispatch_abort_releases_tokens_but_not_attempt_count(store: ModelGrantStore) -> None:
    store.issue(grant())
    reservation = store.reserve("grant-a", scope=SCOPE, call=call(), idempotency_key="x")
    settlement = store.abort(reservation.reservation_id, reason="local_prepare_failed")
    assert settlement.spent_microusd == 0
    state = store.status("grant-a")
    assert state.used_calls == 1
    assert state.reserved_input_tokens == 0
    assert state.reserved_output_tokens == 0


def test_ambiguous_post_dispatch_failure_consumes_upper_bound(store: ModelGrantStore) -> None:
    store.issue(grant(free_only=False, max_cost_microusd=1000))
    reservation = store.reserve(
        "grant-a",
        scope=SCOPE,
        call=call(model="vendor/paid", proof=paid_proof(), cost_upper=700),
        idempotency_key="x",
    )
    store.mark_network_started(reservation.reservation_id)
    settlement = store.abort(reservation.reservation_id, reason="socket_reset")
    assert settlement.spent_microusd == 700
    assert settlement.input_tokens == 100
    assert settlement.output_tokens == 200
    assert settlement.cost_evidence == "reserved_upper_bound"
    assert store.status("grant-a").remaining_cost_microusd == 300


def test_usage_or_cost_above_reserved_bound_revokes_grant(store: ModelGrantStore) -> None:
    store.issue(grant(free_only=False, max_cost_microusd=2000))
    reservation = store.reserve(
        "grant-a",
        scope=SCOPE,
        call=call(model="vendor/paid", proof=paid_proof(), cost_upper=700),
        idempotency_key="x",
    )
    store.mark_network_started(reservation.reservation_id)
    settlement = store.settle(
        reservation.reservation_id,
        input_tokens=100,
        output_tokens=200,
        actual_cost_microusd=701,
        cost_evidence="provider_reported",
    )
    assert settlement.breached is True
    assert settlement.breach_reason == "cost_exceeded_reservation"
    state = store.status("grant-a")
    assert state.revoked is True
    assert state.revoke_reason == "cost_exceeded_reservation"


class FakeExecutor:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[PreparedModelCall] = []

    def execute(self, prepared: PreparedModelCall) -> ModelExecutionResult:
        self.calls.append(prepared)
        if self.fail:
            raise RuntimeError("provider failed")
        return ModelExecutionResult(
            payload={"answer": "ok"},
            input_tokens=80,
            output_tokens=100,
            actual_cost_microusd=0,
            cost_evidence="provider_reported",
        )


def test_grant_bound_delegator_executes_without_any_credential_field(store: ModelGrantStore) -> None:
    store.issue(grant())
    prepared = call()
    # The broker contract contains task/model/pricing/bounds only. Credentials
    # stay behind the local provider executor/keyring boundary.
    assert "credential" not in prepared.__dataclass_fields__
    executor = FakeExecutor()
    result, settlement = GrantBoundDelegator(store, executor).invoke(
        "grant-a", scope=SCOPE, call=prepared, idempotency_key="broker"
    )
    assert result.payload == {"answer": "ok"}
    assert settlement.spent_microusd == 0
    assert store.status("grant-a").used_calls == 1


def test_broker_failure_after_dispatch_is_not_free_retry_budget(store: ModelGrantStore) -> None:
    store.issue(grant(free_only=False, max_cost_microusd=1000))
    executor = FakeExecutor(fail=True)
    prepared = call(model="vendor/paid", proof=paid_proof(), cost_upper=700)
    with pytest.raises(RuntimeError):
        GrantBoundDelegator(store, executor).invoke(
            "grant-a", scope=SCOPE, call=prepared, idempotency_key="broker"
        )
    state = store.status("grant-a")
    assert state.spent_microusd == 700
    assert state.used_calls == 1


def test_paid_budget_rejects_non_usd_without_trusted_fx_normalization(store: ModelGrantStore) -> None:
    store.issue(grant(free_only=False, max_cost_microusd=1000))
    eur = PricingProof(
        provider_id="openrouter",
        model_id="vendor/paid",
        rates=(BillingRate("input", "million_input_tokens", 1.0),),
        source="provider-catalog",
        observed_at=NOW - 10,
        authoritative=True,
        dimensions_complete=True,
        currency="EUR",
    )
    with pytest.raises(DelegationDenied) as caught:
        store.reserve(
            "grant-a",
            scope=SCOPE,
            call=call(model="vendor/paid", proof=eur, cost_upper=500),
            idempotency_key="eur",
        )
    assert caught.value.code == "currency_unproven"


def test_broker_does_not_duplicate_an_idempotent_call_already_in_flight(store: ModelGrantStore) -> None:
    store.issue(grant())
    prepared = call()
    reservation = store.reserve(
        "grant-a", scope=SCOPE, call=prepared, idempotency_key="same-flight"
    )
    store.mark_network_started(reservation.reservation_id)
    executor = FakeExecutor()
    with pytest.raises(DelegationDenied) as caught:
        GrantBoundDelegator(store, executor).invoke(
            "grant-a", scope=SCOPE, call=prepared, idempotency_key="same-flight"
        )
    assert caught.value.code == "in_progress"
    assert executor.calls == []


def test_usd_conversion_rounds_up_for_hard_caps() -> None:
    assert usd_to_microusd(0.0) == 0
    assert usd_to_microusd(0.0000001) == 1
    assert usd_to_microusd(0.01) == 10_000
