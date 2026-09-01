from __future__ import annotations

from pathlib import Path

from karox.agent_protocol import HandoffStore
from karox.context_bus import ContextBus
from karox.intelligence_pool import (
    CAP_CODE,
    CAP_REASONING,
    CAP_TOOLS,
    IntelligenceEndpoint,
    IntelligencePool,
    SOURCE_LOCAL,
    SOURCE_SUBSCRIPTION,
)
from karox.mission_control import MissionControlStore
from karox.orchestration_planning import build_orchestration_plan
from karox.orchestration_recovery import OrchestrationJournal
from karox.orchestration_routing import RoutingTelemetry
from karox.orchestrator import OrchestrationRuntime, WorkerExecutionResult
from karox.orchestrator_advisor import OrchestratorAdvice
from karox.orchestrator_delegation import DelegationProposal
from karox.quota_brain import QuotaBrain
from karox.risk_engine import RiskLevel


def _endpoint(
    endpoint_id: str,
    *,
    roles: tuple[str, ...],
    capabilities: tuple[str, ...],
    source_kind: str = SOURCE_SUBSCRIPTION,
) -> IntelligenceEndpoint:
    return IntelligenceEndpoint(
        endpoint_id=endpoint_id,
        display_name=endpoint_id,
        source_kind=source_kind,
        roles=roles,
        capabilities=capabilities,
        target_id=endpoint_id.replace(":", "-"),
        already_paid=True,
    )


def _pool(tmp_path: Path) -> IntelligencePool:
    pool = IntelligencePool(path=tmp_path / "pool.json")
    pool.put(
        _endpoint(
            "sub:orch",
            roles=("orchestrator", "planner"),
            capabilities=(CAP_REASONING,),
        )
    )
    pool.put(
        _endpoint(
            "sub:coder",
            roles=("scout", "implementer", "tester"),
            capabilities=(CAP_REASONING, CAP_CODE, CAP_TOOLS),
        )
    )
    pool.put(
        _endpoint(
            "local:coder",
            roles=("scout", "implementer", "tester"),
            capabilities=(CAP_REASONING, CAP_CODE, CAP_TOOLS),
            source_kind=SOURCE_LOCAL,
        )
    )
    pool.put(
        _endpoint(
            "sub:reviewer",
            roles=("reviewer",),
            capabilities=(CAP_REASONING, CAP_CODE, CAP_TOOLS),
        )
    )
    return pool


class _Advisor:
    def __init__(self, proposal: DelegationProposal) -> None:
        self.proposal = proposal
        self.calls: list[tuple[str, str, str]] = []

    def generate(self, endpoint, *, prompt: str, effort_level: str):
        self.calls.append((endpoint.endpoint_id, prompt, effort_level))
        return OrchestratorAdvice(
            endpoint_id=endpoint.endpoint_id,
            source_kind=endpoint.source_kind,
            proposal=self.proposal,
            total_tokens=17,
            cost_usd=0.0,
        )


def test_delegated_plan_uses_model_assignment_under_router_policy(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    advisor = _Advisor(
        DelegationProposal(
            assignments={
                "scout": "sub:coder",
                "implementer": "local:coder",
                "tester": "sub:coder",
                "reviewer": "sub:reviewer",
            },
            effort_assignments={"scout": "low", "implementer": "medium"},
            rationale="Use paid/local capacity for mechanical work.",
        )
    )
    selection = build_orchestration_plan(
        repository=tmp_path,
        objective="implement a feature",
        recipe_name="feature",
        preset="balanced",
        risk_level=RiskLevel.MEDIUM,
        orchestrator_endpoint_id="sub:orch",
        delegate_workers=True,
        pool=pool,
        telemetry=RoutingTelemetry(tmp_path / "routing.json"),
        quota_brain=QuotaBrain(tmp_path / "quota.json"),
        advisor_factory=lambda _root: advisor,  # type: ignore[arg-type]
    )
    assert selection.delegated
    by_step = {item.step.step_id: item for item in selection.plan.steps}
    assert by_step["implement"].endpoint.endpoint_id == "local:coder"
    assert by_step["review"].endpoint.endpoint_id == "sub:reviewer"
    assert selection.plan.orchestrator_endpoint.endpoint_id == "sub:orch"
    assert advisor.calls and advisor.calls[0][2] == "high"


def test_explicit_human_assignment_and_effort_override_model(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    advisor = _Advisor(
        DelegationProposal(
            assignments={
                "scout": "sub:coder",
                "implementer": "local:coder",
                "tester": "sub:coder",
                "reviewer": "sub:reviewer",
                "orchestrator": "sub:reviewer",
            },
            effort_assignments={"implementer": "low", "orchestrator": "low"},
        )
    )
    selection = build_orchestration_plan(
        repository=tmp_path,
        objective="implement a feature",
        recipe_name="feature",
        preset="balanced",
        risk_level=RiskLevel.MEDIUM,
        orchestrator_endpoint_id="sub:orch",
        role_assignments={"implementer": "sub:coder"},
        effort_assignments={"implementer": "extra-high", "orchestrator": "extra-high"},
        delegate_workers=True,
        pool=pool,
        telemetry=RoutingTelemetry(tmp_path / "routing.json"),
        quota_brain=QuotaBrain(tmp_path / "quota.json"),
        advisor_factory=lambda _root: advisor,  # type: ignore[arg-type]
    )
    by_step = {item.step.step_id: item for item in selection.plan.steps}
    assert by_step["implement"].endpoint.endpoint_id == "sub:coder"
    assert by_step["implement"].effort_level == "extra-high"
    assert selection.plan.orchestrator_endpoint.endpoint_id == "sub:orch"
    assert selection.validated_proposal is not None
    assert selection.validated_proposal.assignments["orchestrator"] == "sub:orch"
    assert advisor.calls[0][2] == "extra-high"


def test_non_delegated_plan_never_constructs_advisor(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    called = False

    def factory(_root: Path):
        nonlocal called
        called = True
        raise AssertionError("advisor should not be constructed")

    selection = build_orchestration_plan(
        repository=tmp_path,
        objective="implement a feature",
        recipe_name="feature",
        preset="balanced",
        risk_level=RiskLevel.MEDIUM,
        orchestrator_endpoint_id="sub:orch",
        role_assignments={
            "scout": "sub:coder",
            "implementer": "sub:coder",
            "tester": "sub:coder",
            "reviewer": "sub:reviewer",
        },
        delegate_workers=False,
        pool=pool,
        telemetry=RoutingTelemetry(tmp_path / "routing.json"),
        quota_brain=QuotaBrain(tmp_path / "quota.json"),
        advisor_factory=factory,
    )
    assert not called
    assert not selection.delegated


def test_runtime_accounts_for_delegation_usage_before_workers(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    selection = build_orchestration_plan(
        repository=tmp_path,
        objective="implement a feature",
        recipe_name="feature",
        preset="balanced",
        risk_level=RiskLevel.MEDIUM,
        orchestrator_endpoint_id="sub:orch",
        role_assignments={
            "scout": "sub:coder",
            "implementer": "sub:coder",
            "tester": "sub:coder",
            "reviewer": "sub:reviewer",
        },
        delegate_workers=False,
        pool=pool,
        telemetry=RoutingTelemetry(tmp_path / "routing.json"),
        quota_brain=QuotaBrain(tmp_path / "quota.json"),
    )

    def execute(_request):
        return WorkerExecutionResult(
            ok=True,
            summary="ok",
            accepted=True,
            verified=True,
        )

    plan = selection.plan
    executors = {item.endpoint.endpoint_id: execute for item in plan.steps}
    executors[plan.orchestrator_endpoint.endpoint_id] = execute
    result = OrchestrationRuntime(
        plan,
        executors=executors,
        context_bus=ContextBus(plan.run_id, path=tmp_path / "context.json"),
        handoffs=HandoffStore(plan.run_id, path=tmp_path / "handoffs.json"),
        mission_control=MissionControlStore(plan.run_id, path=tmp_path / "mission.json"),
        journal=OrchestrationJournal(plan.run_id, path=tmp_path / "journal.json"),
        telemetry=RoutingTelemetry(tmp_path / "runtime-routing.json"),
        initial_cost_usd=1.25,
        initial_tokens=17,
    ).run()
    assert result.status == "passed"
    assert result.total_cost_usd == 1.25
    assert result.total_tokens == 17
    assert result.savings_receipt is not None
    assert result.savings_receipt.actual_cost.amount == 1.25
