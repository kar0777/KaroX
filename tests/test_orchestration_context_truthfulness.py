from __future__ import annotations

from pathlib import Path

from karox.context_bus import ContextBus
from karox.intelligence_pool import CAP_REASONING, IntelligenceEndpoint
from karox.mission_control import MissionControlStore
from karox.orchestration_recovery import OrchestrationJournal
from karox.orchestration_recipes import RecipeStep
from karox.orchestration_routing import (
    RouteCandidate,
    RouteDecision,
    RoutingTelemetry,
    TASK_DISCOVERY,
    TASK_REVIEW,
)
from karox.orchestrator import (
    OrchestrationPlan,
    OrchestrationPolicy,
    OrchestrationRuntime,
    PlannedStep,
    WorkerExecutionResult,
)
from karox.risk_engine import RiskLevel
from karox.worker_adapters import CallbackWorkerExecutor


def _endpoint() -> IntelligenceEndpoint:
    return IntelligenceEndpoint(
        endpoint_id="sub:shared-worker",
        display_name="Shared worker",
        source_kind="subscription",
        capabilities=(CAP_REASONING,),
        roles=("scout", "reviewer"),
        target_id="test-worker",
        already_paid=True,
    )


def _planned(
    *,
    endpoint: IntelligenceEndpoint,
    telemetry: RoutingTelemetry,
    step: RecipeStep,
) -> PlannedStep:
    performance = telemetry.performance(endpoint.endpoint_id, step.task_class)
    candidate = RouteCandidate(endpoint, performance, 0.0, 10.0, True, ("test",))
    route = RouteDecision(endpoint, ("test",), (candidate,), False)
    return PlannedStep(step, endpoint, route)


def _plan(tmp_path: Path) -> tuple[OrchestrationPlan, IntelligenceEndpoint, RoutingTelemetry]:
    endpoint = _endpoint()
    telemetry = RoutingTelemetry(tmp_path / "routes.json")
    first = RecipeStep(
        "inspect",
        "scout",
        TASK_DISCOVERY,
        (CAP_REASONING,),
    )
    second = RecipeStep(
        "review",
        "reviewer",
        TASK_REVIEW,
        (CAP_REASONING,),
        depends_on=("inspect",),
    )
    plan = OrchestrationPlan(
        run_id="run-context-truth",
        task_id="task-context-truth",
        objective="Inspect and review",
        recipe_name="custom",
        orchestrator_endpoint=endpoint,
        risk_level=RiskLevel.LOW,
        policy=OrchestrationPolicy(max_parallel_workers=1),
        steps=(
            _planned(endpoint=endpoint, telemetry=telemetry, step=first),
            _planned(endpoint=endpoint, telemetry=telemetry, step=second),
        ),
        created_at=1.0,
    )
    return plan, endpoint, telemetry


def _runtime(
    tmp_path: Path,
    *,
    executor: CallbackWorkerExecutor,
) -> OrchestrationRuntime:
    plan, endpoint, telemetry = _plan(tmp_path)
    bus = ContextBus("run-context-truth", path=tmp_path / "context.json")
    bus.put_text(
        item_id="task",
        kind="task",
        content="Inspect and review",
        priority=100,
        stable=True,
    )
    return OrchestrationRuntime(
        plan,
        executors={endpoint.endpoint_id: executor},
        context_bus=bus,
        mission_control=MissionControlStore(
            plan.run_id, path=tmp_path / "mission.json"
        ),
        journal=OrchestrationJournal(plan.run_id, path=tmp_path / "journal.json"),
        telemetry=telemetry,
    )


def test_stateless_worker_never_claims_remote_context_reuse(tmp_path: Path) -> None:
    deltas: list[tuple[int, int, tuple[str, ...]]] = []

    def worker(request):
        deltas.append(
            (
                request.context_delta.sent_chars,
                request.context_delta.reused_chars,
                tuple(item.item_id for item in request.context_delta.changed),
            )
        )
        return WorkerExecutionResult(
            ok=True, summary="done", accepted=True, verified=True
        )

    result = _runtime(
        tmp_path, executor=CallbackWorkerExecutor(worker)
    ).run()

    assert result.status == "passed"
    assert len(deltas) == 2
    assert all(sent > 0 for sent, _reused, _ids in deltas)
    assert all(reused == 0 for _sent, reused, _ids in deltas)
    assert all("task" in ids for _sent, _reused, ids in deltas)
    assert result.savings_receipt is not None
    assert result.savings_receipt.counters.context_chars_reused == 0


def test_opt_in_continuity_remembers_only_context_actually_delivered(
    tmp_path: Path,
) -> None:
    calls = 0
    second_changed: tuple[str, ...] = ()
    second_unchanged: tuple[str, ...] = ()

    def worker(request):
        nonlocal calls, second_changed, second_unchanged
        calls += 1
        if calls == 1:
            return WorkerExecutionResult(
                ok=True,
                summary="inspected",
                accepted=True,
                verified=True,
                context_updates=(
                    {
                        "item_id": "fresh-diff",
                        "kind": "diff",
                        "content": "auth.py changed after inspection",
                        "priority": 100,
                    },
                ),
            )
        second_changed = tuple(item.item_id for item in request.context_delta.changed)
        second_unchanged = request.context_delta.unchanged_ids
        return WorkerExecutionResult(
            ok=True, summary="reviewed", accepted=True, verified=True
        )

    result = _runtime(
        tmp_path,
        executor=CallbackWorkerExecutor(
            worker, reuses_context_between_requests=True
        ),
    ).run()

    assert result.status == "passed"
    assert calls == 2
    assert "task" in second_unchanged
    # The first worker created this item after its input was delivered. It is in
    # the shared bus, but it was never sent to the persistent context, so the
    # reviewer must receive it instead of KaroX pretending it is already known.
    assert "fresh-diff" in second_changed
    assert result.savings_receipt is not None
    assert result.savings_receipt.counters.context_chars_reused > 0


def test_provider_reported_cache_reaches_mission_and_receipt(tmp_path: Path) -> None:
    def worker(_request):
        return WorkerExecutionResult(
            ok=True,
            summary="cached",
            accepted=True,
            verified=True,
            total_tokens=120,
            prompt_tokens=100,
            completion_tokens=20,
            cache_read_tokens=80,
            cache_metrics_reported=True,
            cache_savings_usd=0.12,
        )

    runtime = _runtime(tmp_path, executor=CallbackWorkerExecutor(worker))
    result = runtime.run()
    snapshot = runtime.mission_control.snapshot()

    assert result.status == "passed"
    assert snapshot is not None
    assert snapshot.cache_hit_rate is not None
    assert abs(snapshot.cache_hit_rate - 0.8) < 1e-12
    assert result.savings_receipt is not None
    # The test plan has two provider calls with the same reported usage.
    assert result.savings_receipt.counters.cache_read_tokens == 160
    assert result.savings_receipt.cache_savings.amount is not None
    assert abs(result.savings_receipt.cache_savings.amount - 0.24) < 1e-12
    assert result.savings_receipt.cache_savings.evidence == "estimated"


def test_unreported_cache_stays_unavailable_in_mission_and_receipt(
    tmp_path: Path,
) -> None:
    def worker(_request):
        return WorkerExecutionResult(
            ok=True,
            summary="usage without cache fields",
            accepted=True,
            verified=True,
            total_tokens=120,
            prompt_tokens=100,
            completion_tokens=20,
            cache_metrics_reported=False,
        )

    runtime = _runtime(tmp_path, executor=CallbackWorkerExecutor(worker))
    result = runtime.run()
    snapshot = runtime.mission_control.snapshot()

    assert result.status == "passed"
    assert snapshot is not None
    assert snapshot.cache_hit_rate is None
    assert result.savings_receipt is not None
    assert result.savings_receipt.counters.cache_read_tokens == 0
    assert result.savings_receipt.cache_savings.amount is None
    assert result.savings_receipt.cache_savings.evidence == "unavailable"
