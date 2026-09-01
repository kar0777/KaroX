from __future__ import annotations

from pathlib import Path

import pytest

from karox.model_grant_requests import (
    ApprovalLimits,
    GrantRequestDenied,
    GrantRequestError,
    ModelGrantRequest,
    ModelGrantRequestStore,
    free_helper_request,
)
from karox.model_grants import (
    GrantScope,
    LocalGrantApproval,
    ModelGrantStore,
    ModelSelector,
)


NOW = 1_800_000_000.0
SCOPE = GrantScope(connection_id="chatgpt-web", session_id="s1", project_id="p1")


def request(*, request_id: str = "request-a", expires: float = NOW + 600) -> ModelGrantRequest:
    return ModelGrantRequest(
        request_id=request_id,
        requester_id="chatgpt-web:connection-a",
        scope=SCOPE,
        selectors=(ModelSelector("openrouter", "stealth/*"),),
        free_only=True,
        max_cost_microusd=0,
        max_calls=20,
        max_input_tokens=2_000_000,
        max_output_tokens=200_000,
        max_request_output_tokens=32_000,
        max_parallel=2,
        allow_fallback=True,
        grant_ttl_seconds=7200,
        request_expires_at=expires,
        max_price_age_seconds=300,
        created_at=NOW,
        label="Free coding helpers",
    )


def stores(tmp_path: Path) -> tuple[ModelGrantRequestStore, ModelGrantStore]:
    grants = ModelGrantStore(tmp_path / "grants.sqlite3", clock=lambda: NOW)
    requests = ModelGrantRequestStore(
        tmp_path / "requests.sqlite3", grant_store=grants, clock=lambda: NOW
    )
    return requests, grants


def approval() -> LocalGrantApproval:
    return LocalGrantApproval("local-click-1", "tui", NOW + 1)


def test_hosted_request_has_zero_authority_until_local_approval(tmp_path: Path) -> None:
    requests, grants = stores(tmp_path)
    state = requests.create(request(), idempotency_key="host-op-1")
    assert state.state == "pending"
    assert state.grant_id is None
    assert grants.list() == ()


def test_local_approval_mints_project_scoped_grant(tmp_path: Path) -> None:
    requests, grants = stores(tmp_path)
    requests.create(request(), idempotency_key="host-op-1")
    state = requests.approve("request-a", approval=approval())
    assert state.state == "approved"
    assert state.grant_id is not None
    grant_state = grants.status(state.grant_id)
    grant = grant_state.grant
    assert grant.scope == SCOPE
    assert grant.free_only is True
    assert grant.max_cost_microusd == 0
    assert grant.max_calls == 20
    assert grant.approval.channel == "tui"


def test_local_approval_can_only_reduce_requested_limits(tmp_path: Path) -> None:
    requests, grants = stores(tmp_path)
    requests.create(request(), idempotency_key="host-op-1")
    state = requests.approve(
        "request-a",
        approval=approval(),
        limits=ApprovalLimits(
            max_calls=5,
            max_input_tokens=500_000,
            max_output_tokens=50_000,
            max_request_output_tokens=10_000,
            max_parallel=1,
            grant_ttl_seconds=900,
        ),
    )
    grant = grants.status(state.grant_id or "").grant
    assert grant.max_calls == 5
    assert grant.max_input_tokens == 500_000
    assert grant.max_output_tokens == 50_000
    assert grant.max_request_output_tokens == 10_000
    assert grant.max_parallel == 1
    assert grant.expires_at == NOW + 900


def test_local_approval_cannot_widen_agent_requested_authority(tmp_path: Path) -> None:
    requests, grants = stores(tmp_path)
    requests.create(request(), idempotency_key="host-op-1")
    with pytest.raises(GrantRequestDenied, match="cannot widen"):
        requests.approve(
            "request-a",
            approval=approval(),
            limits=ApprovalLimits(max_calls=21),
        )
    assert requests.status("request-a").state == "pending"
    assert grants.list() == ()


def test_deny_is_terminal_and_never_mints_grant(tmp_path: Path) -> None:
    requests, grants = stores(tmp_path)
    requests.create(request(), idempotency_key="host-op-1")
    denied = requests.deny("request-a", reason="not_now")
    assert denied.state == "denied"
    assert denied.decision_reason == "not_now"
    with pytest.raises(GrantRequestDenied):
        requests.approve("request-a", approval=approval())
    assert grants.list() == ()


def test_pending_request_expires_without_user_action(tmp_path: Path) -> None:
    grants = ModelGrantStore(tmp_path / "grants.sqlite3", clock=lambda: NOW + 20)
    requests = ModelGrantRequestStore(
        tmp_path / "requests.sqlite3", grant_store=grants, clock=lambda: NOW + 20
    )
    requests.create(request(expires=NOW + 10), idempotency_key="host-op-1")
    state = requests.status("request-a")
    assert state.state == "expired"
    assert state.decision_reason == "approval_timeout"
    assert requests.pending() == ()
    assert grants.list() == ()


def test_request_idempotency_replay_cannot_change_requested_authority(tmp_path: Path) -> None:
    requests, _ = stores(tmp_path)
    first = requests.create(request(), idempotency_key="host-op-1")
    replay = requests.create(request(), idempotency_key="host-op-1")
    assert replay.request == first.request
    altered = request(request_id="request-b")
    with pytest.raises(GrantRequestError, match="different authority"):
        requests.create(altered, idempotency_key="host-op-1")


def test_free_helper_product_default_is_bounded_not_unlimited() -> None:
    item = free_helper_request(
        requester_id="chatgpt-web:connection-a",
        scope=SCOPE,
        selectors=(ModelSelector("openrouter", "stealth/ox-alpha"),),
        created_at=NOW,
    )
    assert item.free_only is True
    assert item.max_cost_microusd == 0
    assert item.max_calls == 20
    assert item.max_parallel == 2
    assert item.max_input_tokens == 2_000_000
    assert item.max_output_tokens == 200_000
    assert item.grant_ttl_seconds == 7200
    assert item.request_expires_at == NOW + 600


def test_request_contract_contains_no_credential_or_secret_field() -> None:
    fields = ModelGrantRequest.__dataclass_fields__
    assert "credential" not in fields
    assert "api_key" not in fields
    assert "secret" not in fields
