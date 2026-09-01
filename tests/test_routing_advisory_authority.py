from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from karox.intelligence_pool import (
    CAP_CODE,
    CAP_REASONING,
    IntelligenceEndpoint,
    IntelligencePool,
    SOURCE_SUBSCRIPTION,
)
from karox.orchestration_routing import (
    RouteRequest,
    RoutingError,
    RoutingTelemetry,
    TASK_IMPLEMENTATION,
    VerifiedSmartRouter,
)
from karox.orchestrator import OrchestrationPolicy, Orchestrator
from karox.orchestrator_delegation import DelegationProposal, OrchestratorDelegationBroker
from karox.quota_brain import QuotaBrain
from karox.registry import ProviderRegistry
from karox.risk_engine import RiskLevel


def _endpoint(endpoint_id: str, *, roles: tuple[str, ...]) -> IntelligenceEndpoint:
    return IntelligenceEndpoint(
        endpoint_id=endpoint_id,
        display_name=endpoint_id,
        source_kind=SOURCE_SUBSCRIPTION,
        capabilities=(CAP_CODE, CAP_REASONING),
        roles=roles,
        target_id=endpoint_id.replace(":", "-"),
        already_paid=True,
    )


def _router(tmp_path: Path, *endpoints: IntelligenceEndpoint) -> tuple[VerifiedSmartRouter, RoutingTelemetry]:
    registry = ProviderRegistry(tmp_path / "providers.json")
    pool = IntelligencePool(path=tmp_path / "pool.json", provider_registry=registry)
    for endpoint in endpoints:
        pool.put(endpoint)
    telemetry = RoutingTelemetry(tmp_path / "routing.json")
    return (
        VerifiedSmartRouter(
            pool=pool,
            telemetry=telemetry,
            provider_registry=registry,
            quota_brain=QuotaBrain(tmp_path / "quota.json"),
        ),
        telemetry,
    )


def test_auto_discovered_role_metadata_does_not_authorize_high_risk_first_use(
    tmp_path: Path,
) -> None:
    router, _ = _router(
        tmp_path,
        _endpoint("sub:auto-codex", roles=("implementer",)),
    )
    with pytest.raises(RoutingError, match="no intelligence endpoint"):
        router.decide(
            RouteRequest(
                task_class=TASK_IMPLEMENTATION,
                role="implementer",
                required_capabilities=(CAP_CODE,),
                risk_level=RiskLevel.HIGH,
            )
        )


def test_explicit_human_preference_can_authorize_high_risk_first_use(
    tmp_path: Path,
) -> None:
    endpoint = _endpoint("sub:human-choice", roles=("implementer",))
    router, _ = _router(tmp_path, endpoint)
    decision = router.decide(
        RouteRequest(
            task_class=TASK_IMPLEMENTATION,
            role="implementer",
            required_capabilities=(CAP_CODE,),
            risk_level=RiskLevel.HIGH,
            preferred_endpoint_id=endpoint.endpoint_id,
        )
    )
    assert decision.endpoint.endpoint_id == endpoint.endpoint_id
    assert "high_risk_explicit_first_use" in decision.reasons
    assert "explicit_preference" in decision.reasons


def test_model_advisory_preference_cannot_authorize_high_risk_first_use(
    tmp_path: Path,
) -> None:
    endpoint = _endpoint("sub:model-choice", roles=("implementer",))
    router, _ = _router(tmp_path, endpoint)
    with pytest.raises(RoutingError, match="no intelligence endpoint"):
        router.decide(
            RouteRequest(
                task_class=TASK_IMPLEMENTATION,
                role="implementer",
                required_capabilities=(CAP_CODE,),
                risk_level=RiskLevel.HIGH,
                preferred_endpoint_id=endpoint.endpoint_id,
                preferred_is_advisory=True,
            )
        )


def test_model_advice_is_only_a_tie_breaker_against_verified_quality(
    tmp_path: Path,
) -> None:
    weak = _endpoint("sub:weak-advice", roles=("implementer",))
    proven = _endpoint("sub:proven", roles=("implementer",))
    router, telemetry = _router(tmp_path, weak, proven)
    for _ in range(4):
        telemetry.observe(
            endpoint_id=proven.endpoint_id,
            task_class=TASK_IMPLEMENTATION,
            accepted=True,
            verified=True,
        )
    decision = router.decide(
        RouteRequest(
            task_class=TASK_IMPLEMENTATION,
            role="implementer",
            required_capabilities=(CAP_CODE,),
            risk_level=RiskLevel.MEDIUM,
            preferred_endpoint_id=weak.endpoint_id,
            preferred_is_advisory=True,
            minimum_verified_samples=3,
        )
    )
    assert decision.endpoint.endpoint_id == proven.endpoint_id
    weak_candidate = next(
        item for item in decision.candidates if item.endpoint.endpoint_id == weak.endpoint_id
    )
    assert "advisory_preference" in weak_candidate.reasons
    assert "explicit_preference" not in weak_candidate.reasons


def test_delegation_marks_model_assignments_advisory_but_keeps_human_targets_trusted(
    tmp_path: Path,
) -> None:
    registry = ProviderRegistry(tmp_path / "providers.json")
    pool = IntelligencePool(path=tmp_path / "pool.json", provider_registry=registry)
    pool.put(_endpoint("sub:orch", roles=("orchestrator", "planner")))
    pool.put(_endpoint("sub:worker", roles=("implementer",)))
    telemetry = RoutingTelemetry(tmp_path / "routing.json")
    router = VerifiedSmartRouter(
        pool=pool,
        telemetry=telemetry,
        provider_registry=registry,
        quota_brain=QuotaBrain(tmp_path / "quota.json"),
    )
    orchestrator = Orchestrator(pool=pool, router=router, telemetry=telemetry)
    broker = OrchestratorDelegationBroker(orchestrator=orchestrator, pool=pool)
    proposal = DelegationProposal(assignments={"implementer": "sub:worker"})
    policy = OrchestrationPolicy.from_preset("balanced")

    with patch.object(orchestrator, "plan", return_value=object()) as plan:
        broker.apply(
            proposal,
            objective="Implement authentication",
            recipe_name="feature",
            risk_level=RiskLevel.MEDIUM,
            policy=policy,
            orchestrator_endpoint_id="sub:orch",
        )
    kwargs = plan.call_args.kwargs
    assert kwargs["advisory_assignment_targets"] == frozenset({"implementer"})

    with patch.object(orchestrator, "plan", return_value=object()) as plan:
        broker.apply(
            proposal,
            objective="Implement authentication",
            recipe_name="feature",
            risk_level=RiskLevel.MEDIUM,
            policy=policy,
            orchestrator_endpoint_id="sub:orch",
            trusted_assignment_targets=("implementer",),
        )
    kwargs = plan.call_args.kwargs
    assert kwargs["advisory_assignment_targets"] == frozenset()
