from __future__ import annotations

import dataclasses

import pytest

from karox.intelligence_pool import (
    CAP_CODE,
    CAP_REASONING,
    CAP_TOOLS,
    IntelligenceEndpoint,
    IntelligencePool,
    SOURCE_SUBSCRIPTION,
)
from karox.orchestration_routing import RoutingTelemetry, VerifiedSmartRouter
from karox.orchestrator import OrchestrationPolicy, Orchestrator
from karox.orchestrator_delegation import (
    DelegationProposal,
    DelegationProposalError,
    OrchestratorDelegationBroker,
)
from karox.quota_brain import QuotaBrain
from karox.registry import ProviderRegistry
from karox.risk_engine import RiskLevel


def _endpoint(endpoint_id: str, *, roles: tuple[str, ...], caps: tuple[str, ...]) -> IntelligenceEndpoint:
    return IntelligenceEndpoint(
        endpoint_id=endpoint_id,
        display_name=endpoint_id,
        source_kind=SOURCE_SUBSCRIPTION,
        capabilities=caps,
        roles=roles,
        target_id=endpoint_id.replace(":", "-"),
        already_paid=True,
    )


def _broker(tmp_path):
    registry = ProviderRegistry(tmp_path / "providers.json")
    pool = IntelligencePool(path=tmp_path / "pool.json", provider_registry=registry)
    pool.put(
        _endpoint(
            "sub:orchestrator",
            roles=("orchestrator", "planner"),
            caps=(CAP_REASONING,),
        )
    )
    pool.put(
        _endpoint(
            "sub:coder",
            roles=("implementer", "scout", "tester"),
            caps=(CAP_REASONING, CAP_CODE, CAP_TOOLS),
        )
    )
    pool.put(
        _endpoint(
            "sub:reviewer",
            roles=("reviewer", "security"),
            caps=(CAP_REASONING, CAP_CODE),
        )
    )
    telemetry = RoutingTelemetry(tmp_path / "routing.json")
    router = VerifiedSmartRouter(
        pool=pool,
        telemetry=telemetry,
        provider_registry=registry,
        quota_brain=QuotaBrain(tmp_path / "quota.json"),
    )
    orchestrator = Orchestrator(pool=pool, router=router, telemetry=telemetry)
    return OrchestratorDelegationBroker(orchestrator=orchestrator, pool=pool), pool


def test_delegation_contract_rejects_execution_and_permission_fields() -> None:
    with pytest.raises(DelegationProposalError, match="unsupported delegation proposal fields"):
        DelegationProposal.parse(
            {
                "assignments": {},
                "command": ["python", "-m", "pytest"],
                "permissions": ["workspace.write"],
            }
        )


def test_delegation_rationale_rejects_credentials() -> None:
    with pytest.raises(DelegationProposalError, match="cannot contain credentials"):
        DelegationProposal.parse(
            {
                "assignments": {},
                "rationale": "api_key=super-secret-value",
            }
        )


def test_delegation_cannot_replace_user_selected_orchestrator(tmp_path) -> None:
    broker, _ = _broker(tmp_path)
    with pytest.raises(DelegationProposalError, match="cannot replace the user-selected orchestrator"):
        broker.apply(
            {"assignments": {"orchestrator": "sub:coder"}},
            objective="Implement a guarded feature",
            recipe_name="feature",
            risk_level=RiskLevel.MEDIUM,
            policy=OrchestrationPolicy.from_preset("balanced"),
            orchestrator_endpoint_id="sub:orchestrator",
        )


def test_delegation_proposal_cannot_bypass_role_or_capability_router_floors(tmp_path) -> None:
    broker, _ = _broker(tmp_path)
    result = broker.apply(
        {
            # The reviewer is not an implementer and does not expose tool capability.
            # The proposal is advisory, so the verified router must refuse that choice
            # and keep implementation on the eligible coder endpoint.
            "assignments": {"implementer": "sub:reviewer"},
            "rationale": "Prefer independent review intelligence for implementation.",
        },
        objective="Implement a guarded feature",
        recipe_name="feature",
        risk_level=RiskLevel.MEDIUM,
        policy=OrchestrationPolicy.from_preset("balanced"),
        orchestrator_endpoint_id="sub:orchestrator",
    )
    implementers = [step for step in result.plan.steps if step.step.role == "implementer"]
    assert implementers
    assert all(step.endpoint.endpoint_id == "sub:coder" for step in implementers)


def test_delegation_keeps_user_orchestrator_and_normalizes_worker_effort(tmp_path) -> None:
    broker, _ = _broker(tmp_path)
    result = broker.apply(
        {
            "assignments": {
                "planner": "sub:orchestrator",
                "implementer": "sub:coder",
                "reviewer": "sub:reviewer",
            },
            "effort_assignments": {"implementer": "extra_high"},
            "rationale": "Use paid capacity and reserve the reviewer for independent review.",
        },
        objective="Implement a guarded feature",
        recipe_name="feature",
        risk_level=RiskLevel.MEDIUM,
        policy=OrchestrationPolicy.from_preset("balanced"),
        orchestrator_endpoint_id="sub:orchestrator",
    )
    assert result.plan.orchestrator_endpoint.endpoint_id == "sub:orchestrator"
    implementers = [step for step in result.plan.steps if step.step.role == "implementer"]
    assert implementers
    assert all(step.effort_level == "extra-high" for step in implementers)
    assert result.catalog_sha256
    assert result.proposal_sha256


def test_public_catalog_is_secret_free_and_does_not_expose_adapter_target(tmp_path) -> None:
    broker, pool = _broker(tmp_path)
    original = pool.get("sub:coder")
    pool.put(dataclasses.replace(original, target_id="private-adapter-target"))
    catalog = broker.public_catalog()
    coder = next(item for item in catalog if item["endpoint_id"] == "sub:coder")
    assert "target_id" not in coder
    assert "provider_id" not in coder
    assert "model_id" not in coder
    assert "credential" not in str(coder).lower()
