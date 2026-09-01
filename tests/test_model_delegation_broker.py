from __future__ import annotations

from pathlib import Path
from typing import Optional

import pytest

from karox.credentials import CredentialStore
from karox.delegated_provider import GrantEnforcedProvider
from karox.model_delegation_broker import (
    ModelDelegationBroker,
    ModelDelegationBrokerError,
    OpenRouterDelegationAdapter,
)
from karox.model_grants import (
    BillingRate,
    GrantScope,
    LocalGrantApproval,
    ModelGrant,
    ModelGrantStore,
    ModelSelector,
    PreparedModelCall,
    PricingProof,
)
from karox.providers import ModelMessage, ModelRequest, ModelResponse
from karox.registry import ModelRecord, ProviderRecord, ProviderRegistry


NOW = 1_800_000_000.0
SCOPE = GrantScope(connection_id="host-a", session_id="session-a", project_id="project-a")


class MemoryCredentialBackend:
    def __init__(self) -> None:
        self.values: dict[tuple[str, str], str] = {}

    def set(self, service: str, account: str, secret: str) -> None:
        self.values[(service, account)] = secret

    def get(self, service: str, account: str) -> Optional[str]:
        return self.values.get((service, account))

    def delete(self, service: str, account: str) -> None:
        self.values.pop((service, account), None)


class FakeProvider:
    provider_name = "fake"

    def complete(self, request: ModelRequest) -> ModelResponse:
        return ModelResponse(
            content=f"used:{request.model}",
            tool_calls=(),
            finish_reason="stop",
            usage={"prompt_tokens": 5, "completion_tokens": 3},
            response_id="response-a",
        )

    def stream(self, request: ModelRequest):
        raise AssertionError("broker complete test should use complete")


class FakePricing:
    def __init__(self, provider_id: str) -> None:
        self.provider_id = provider_id

    def prepare(self, request: ModelRequest) -> PreparedModelCall:
        return PreparedModelCall(
            provider_id=self.provider_id,
            model_id=request.model,
            input_token_upper_bound=100,
            output_token_upper_bound=request.max_output_tokens or 100,
            cost_upper_bound_microusd=0,
            pricing=PricingProof(
                provider_id=self.provider_id,
                model_id=request.model,
                rates=(
                    BillingRate("prompt", "token", 0.0),
                    BillingRate("completion", "token", 0.0),
                ),
                source="fake-authoritative-catalog",
                observed_at=NOW,
                authoritative=True,
                dimensions_complete=True,
            ),
            payload=request,
        )

    def actual_cost_microusd(
        self, call: PreparedModelCall, response: ModelResponse
    ) -> tuple[int, str]:
        return 0, "provider_reported"


class FakeAdapter:
    def __init__(self, provider_id: str = "fake-provider") -> None:
        self.provider_id = provider_id
        self.seen_model: Optional[str] = None

    def supports(self, provider: ProviderRecord) -> bool:
        return provider.provider_id == self.provider_id

    def build(
        self,
        *,
        provider: ProviderRecord,
        model: ModelRecord,
        credentials: CredentialStore,
        grant_store: ModelGrantStore,
        grant_id: str,
        scope: GrantScope,
        operation_id: str,
    ) -> GrantEnforcedProvider:
        self.seen_model = model.model_id
        return GrantEnforcedProvider(
            FakeProvider(),
            provider_id=provider.provider_id,
            grant_store=grant_store,
            grant_id=grant_id,
            scope=scope,
            pricing=FakePricing(provider.provider_id),
            idempotency_prefix=operation_id,
        )


class NeverAdapter(FakeAdapter):
    def supports(self, provider: ProviderRecord) -> bool:
        return False


def setup_registry(tmp_path: Path, *, enabled: bool = True) -> ProviderRegistry:
    registry = ProviderRegistry(tmp_path / "providers.json")
    registry.put_provider(
        ProviderRecord(
            provider_id="fake-provider",
            adapter_kind="openai_compatible_chat",
            base_url="https://example.invalid/v1",
            enabled=enabled,
        )
    )
    registry.put_model(
        ModelRecord(
            provider_id="fake-provider",
            model_id="vendor/model-a",
            aliases=("model-a",),
            context_window=100_000,
            max_output_tokens=1_000,
            tools="true",
            streaming="true",
        )
    )
    return registry


def issue_grant(store: ModelGrantStore) -> None:
    store.issue(
        ModelGrant(
            grant_id="grant-a",
            scope=SCOPE,
            selectors=(ModelSelector("fake-provider", "vendor/*"),),
            free_only=True,
            max_cost_microusd=0,
            max_calls=5,
            max_input_tokens=10_000,
            max_output_tokens=10_000,
            max_request_output_tokens=1_000,
            max_parallel=1,
            allow_fallback=False,
            issued_at=NOW - 1,
            expires_at=NOW + 3600,
            max_price_age_seconds=300,
            approval=LocalGrantApproval("approval-a", "tui", NOW - 1),
        )
    )


def broker(
    tmp_path: Path, *, adapters: tuple[object, ...], enabled: bool = True
) -> tuple[ModelDelegationBroker, ModelGrantStore]:
    grants = ModelGrantStore(tmp_path / "grants.sqlite3", clock=lambda: NOW)
    issue_grant(grants)
    result = ModelDelegationBroker(
        registry=setup_registry(tmp_path, enabled=enabled),
        credentials=CredentialStore(MemoryCredentialBackend()),
        grant_store=grants,
        adapters=adapters,  # type: ignore[arg-type]
    )
    return result, grants


def test_broker_resolves_alias_to_canonical_model_before_authorization(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    subject, grants = broker(tmp_path, adapters=(adapter,))
    outcome = subject.complete(
        provider_id="fake-provider",
        request=ModelRequest(
            model="model-a",
            messages=(ModelMessage("user", "review"),),
            max_output_tokens=100,
            correlation_id="turn-a",
        ),
        grant_id="grant-a",
        scope=SCOPE,
        operation_id="host-op-a",
    )
    assert adapter.seen_model == "vendor/model-a"
    assert outcome.model_id == "vendor/model-a"
    assert outcome.response.content == "used:vendor/model-a"
    assert outcome.receipt.spent_microusd == 0
    assert outcome.grant_usage.used_calls == 1
    assert grants.status("grant-a").used_output_tokens == 3


def test_disabled_provider_is_refused_before_adapter_or_credential_use(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    subject, _ = broker(tmp_path, adapters=(adapter,), enabled=False)
    with pytest.raises(ModelDelegationBrokerError, match="disabled"):
        subject.complete(
            provider_id="fake-provider",
            request=ModelRequest(
                model="vendor/model-a",
                messages=(ModelMessage("user", "review"),),
                max_output_tokens=100,
                correlation_id="turn-a",
            ),
            grant_id="grant-a",
            scope=SCOPE,
            operation_id="host-op-a",
        )
    assert adapter.seen_model is None


def test_unimplemented_provider_family_fails_closed(tmp_path: Path) -> None:
    subject, grants = broker(tmp_path, adapters=(NeverAdapter(),))
    with pytest.raises(ModelDelegationBrokerError, match="trustworthy pricing"):
        subject.complete(
            provider_id="fake-provider",
            request=ModelRequest(
                model="vendor/model-a",
                messages=(ModelMessage("user", "review"),),
                max_output_tokens=100,
                correlation_id="turn-a",
            ),
            grant_id="grant-a",
            scope=SCOPE,
            operation_id="host-op-a",
        )
    assert grants.status("grant-a").used_calls == 0


def test_multiple_matching_adapters_are_refused_as_ambiguous_authority(tmp_path: Path) -> None:
    subject, grants = broker(tmp_path, adapters=(FakeAdapter(), FakeAdapter()))
    with pytest.raises(ModelDelegationBrokerError, match="multiple"):
        subject.complete(
            provider_id="fake-provider",
            request=ModelRequest(
                model="vendor/model-a",
                messages=(ModelMessage("user", "review"),),
                max_output_tokens=100,
                correlation_id="turn-a",
            ),
            grant_id="grant-a",
            scope=SCOPE,
            operation_id="host-op-a",
        )
    assert grants.status("grant-a").used_calls == 0


def test_openrouter_adapter_selection_is_host_and_protocol_specific() -> None:
    adapter = OpenRouterDelegationAdapter(clock=lambda: NOW)
    assert adapter.supports(
        ProviderRecord(
            provider_id="openrouter",
            adapter_kind="openai_compatible_chat",
            base_url="https://openrouter.ai/api/v1",
        )
    )
    assert not adapter.supports(
        ProviderRecord(
            provider_id="lookalike",
            adapter_kind="openai_compatible_chat",
            base_url="https://openrouter.example.com/v1",
        )
    )


def test_broker_outcome_contract_never_contains_credential_fields(tmp_path: Path) -> None:
    subject, _ = broker(tmp_path, adapters=(FakeAdapter(),))
    outcome = subject.complete(
        provider_id="fake-provider",
        request=ModelRequest(
            model="vendor/model-a",
            messages=(ModelMessage("user", "review"),),
            max_output_tokens=100,
            correlation_id="turn-a",
        ),
        grant_id="grant-a",
        scope=SCOPE,
        operation_id="host-op-a",
    )
    fields = outcome.__dataclass_fields__
    assert "credential" not in fields
    assert "api_key" not in fields
    assert "secret" not in fields
