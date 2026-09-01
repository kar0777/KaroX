"""Shared KaroX orchestration planning service.

Both CLI and TUI need the same planning semantics.  In normal mode the verified
router builds the plan directly.  In delegated mode KaroX first selects and pins
one orchestrator, asks it for one bounded worker-selection proposal, overlays any
explicit human choices, and then sends the proposal through the normal verified
router and independence policy.  Model advice never becomes execution authority.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Optional

from .effort import normalize_effort
from .intelligence_pool import CAP_REASONING, IntelligencePool
from .orchestration_routing import RouteRequest, RoutingTelemetry, VerifiedSmartRouter
from .orchestrator import OrchestrationPlan, OrchestrationPolicy, Orchestrator
from .orchestrator_advisor import OrchestratorAdvice, OrchestratorAdvisor
from .orchestrator_delegation import DelegationProposal, OrchestratorDelegationBroker
from .quota_brain import QuotaBrain
from .risk_engine import RiskLevel


@dataclasses.dataclass(frozen=True)
class PlanSelection:
    plan: OrchestrationPlan
    advice: Optional[OrchestratorAdvice] = None
    validated_proposal: Optional[DelegationProposal] = None

    @property
    def delegated(self) -> bool:
        return self.advice is not None and self.validated_proposal is not None

    def delegation_dict(self) -> Optional[dict[str, Any]]:
        if self.advice is None or self.validated_proposal is None:
            return None
        value = self.advice.to_dict()
        value["validated_proposal"] = self.validated_proposal.to_dict()
        return value


def _delegation_effort(preset: str, overrides: Mapping[str, str]) -> str:
    explicit = overrides.get("orchestrator")
    if explicit:
        return normalize_effort(explicit)
    defaults = {
        "maximum_quality": "extra-high",
        "balanced": "high",
        "maximum_economy": "high",
        "custom": "medium",
    }
    return normalize_effort(defaults.get(preset, "high"))


def build_orchestration_plan(
    *,
    repository: Path,
    objective: str,
    recipe_name: str,
    preset: str,
    risk_level: RiskLevel,
    orchestrator_endpoint_id: Optional[str] = None,
    role_assignments: Optional[Mapping[str, str]] = None,
    effort_assignments: Optional[Mapping[str, str]] = None,
    run_id: Optional[str] = None,
    delegate_workers: bool = False,
    pool: Optional[IntelligencePool] = None,
    telemetry: Optional[RoutingTelemetry] = None,
    quota_brain: Optional[QuotaBrain] = None,
    advisor_factory: Optional[Callable[[Path], OrchestratorAdvisor]] = None,
) -> PlanSelection:
    """Build one deterministic or orchestrator-guided plan under KaroX policy."""

    selected_pool = pool or IntelligencePool()
    selected_telemetry = telemetry or RoutingTelemetry()
    quota = quota_brain or QuotaBrain()
    router = VerifiedSmartRouter(
        pool=selected_pool,
        telemetry=selected_telemetry,
        quota_brain=quota,
    )
    orchestrator = Orchestrator(
        pool=selected_pool,
        router=router,
        telemetry=selected_telemetry,
    )
    policy = OrchestrationPolicy.from_preset(preset)
    human_assignments = dict(role_assignments or {})
    human_efforts = {
        key: normalize_effort(value)
        for key, value in dict(effort_assignments or {}).items()
    }

    if not delegate_workers:
        return PlanSelection(
            plan=orchestrator.plan(
                objective=objective,
                recipe_name=recipe_name,
                risk_level=risk_level,
                policy=policy,
                orchestrator_endpoint_id=orchestrator_endpoint_id,
                role_assignments=human_assignments,
                effort_assignments=human_efforts,
                run_id=run_id,
            )
        )

    preferred = orchestrator_endpoint_id or human_assignments.get("orchestrator")
    route = router.decide(
        RouteRequest(
            task_class="general",
            role="orchestrator",
            required_capabilities=(CAP_REASONING,),
            risk_level=risk_level,
            preferred_endpoint_id=preferred,
            minimum_verified_samples=policy.minimum_verified_samples,
            minimum_verified_success_rate=policy.minimum_verified_success_rate,
            reserve_quota_fraction=policy.reserve_quota_fraction,
            prefer_already_paid=policy.prefer_already_paid,
        )
    )
    selected = route.endpoint
    broker = OrchestratorDelegationBroker(orchestrator=orchestrator, pool=selected_pool)
    prompt = broker.proposal_prompt(
        objective=objective,
        recipe_name=recipe_name,
        risk_level=risk_level,
        policy=policy,
        orchestrator_endpoint_id=selected.endpoint_id,
    )
    advisor = (advisor_factory or (lambda root: OrchestratorAdvisor(root)))(repository)
    advice = advisor.generate(
        selected,
        prompt=prompt,
        effort_level=_delegation_effort(preset, human_efforts),
    )

    # Human choices are immutable policy inputs. The model can fill empty roles,
    # but it cannot replace explicit assignments/effort or replace itself.
    assignments = dict(advice.proposal.assignments)
    assignments.update(human_assignments)
    assignments["orchestrator"] = selected.endpoint_id
    efforts = dict(advice.proposal.effort_assignments)
    efforts.update(human_efforts)
    validated_input = DelegationProposal(
        assignments=assignments,
        effort_assignments=efforts,
        rationale=advice.proposal.rationale,
    )
    guided = broker.apply(
        validated_input,
        objective=objective,
        recipe_name=recipe_name,
        risk_level=risk_level,
        policy=policy,
        orchestrator_endpoint_id=selected.endpoint_id,
        trusted_assignment_targets=human_assignments.keys(),
        run_id=run_id,
    )
    return PlanSelection(
        plan=guided.plan,
        advice=advice,
        validated_proposal=guided.proposal,
    )


__all__ = ["PlanSelection", "build_orchestration_plan"]
