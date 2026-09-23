from __future__ import annotations

import dataclasses
import subprocess
import threading
import time
from pathlib import Path

import pytest

from karox.agent_protocol import (
    AgentMessage,
    HandoffStore,
    MESSAGE_REVIEW_FAIL,
    ReviewFinding,
    independent_review_exclusions,
)
from karox.agent_protocol import SUMMARY_LIMIT, bounded_summary
from karox.context_bus import ContextBus, ContextItem
from karox.economy_engine import (
    EconomyCounters,
    ReplayCase,
    ReplayLab,
    build_savings_receipt,
    shadow_route,
)
from karox.intelligence_pool import (
    CAP_CODE,
    CAP_REASONING,
    CAP_TOOLS,
    IntelligenceEndpoint,
    IntelligencePool,
    SOURCE_LOCAL,
    SOURCE_SUBSCRIPTION,
)
from karox.mission_control import AgentStatus, MissionControlStore, MissionSnapshot
from karox.mission_control_server import PairingAuthority
from karox.orchestration_recipes import OrchestrationRecipe, RecipeStep, recipe
from karox.orchestration_recovery import (
    OrchestrationJournal,
    STEP_PASSED,
    STEP_RECONCILE,
)
from karox.prompt_cache_plan import build_prompt_envelope
from karox.orchestration_routing import (
    RouteRequest,
    RoutingError,
    RoutingTelemetry,
    TASK_DISCOVERY,
    TASK_IMPLEMENTATION,
    VerifiedSmartRouter,
)
from karox.orchestrator import (
    OrchestrationPlan,
    OrchestrationPolicy,
    OrchestrationRuntime,
    Orchestrator,
    PlannedStep,
    WorkerExecutionRequest,
    WorkerExecutionResult,
)
from karox.quota_brain import QuotaBrain
from karox.recipe_registry import RecipeRegistry, RecipeRegistryError
from karox.registry import ModelPricing, ModelRecord, ProviderRecord, ProviderRegistry
from karox.risk_engine import RiskLevel
from karox.shadow_economy import ShadowEconomyLedger
from karox.usage_analytics import UsageSummary
from karox.worker_adapter_registry import WorkerAdapterRegistry, WorkerAdapterRegistryError
from karox.worker_adapters import CallbackWorkerExecutor, PromptTargetResult
from karox.worktree_pool import WorktreePool, WorktreePoolError


def _registry(tmp_path: Path) -> ProviderRegistry:
    registry = ProviderRegistry(tmp_path / "providers.json")
    registry.put_provider(
        ProviderRecord(
            provider_id="openai",
            adapter_kind="openai_responses",
            base_url="https://api.example.test/v1",
        )
    )
    registry.put_model(
        ModelRecord(
            provider_id="openai",
            model_id="strong",
            tools="true",
            vision="true",
            structured_output="true",
            pricing=ModelPricing(
                version="v1",
                currency="USD",
                input_per_million=2.0,
                output_per_million=8.0,
                source="test",
            ),
        )
    )
    return registry


def _custom(
    endpoint_id: str,
    *,
    roles: tuple[str, ...],
    source: str = SOURCE_SUBSCRIPTION,
    caps: tuple[str, ...] = (CAP_REASONING, CAP_CODE, CAP_TOOLS),
    paid: bool = True,
) -> IntelligenceEndpoint:
    return IntelligenceEndpoint(
        endpoint_id=endpoint_id,
        display_name=endpoint_id,
        source_kind=source,
        capabilities=caps,
        roles=roles,
        target_id=endpoint_id.replace(":", "-"),
        already_paid=paid,
    )


def _pool(tmp_path: Path) -> IntelligencePool:
    registry = _registry(tmp_path)
    pool = IntelligencePool(path=tmp_path / "pool.json", provider_registry=registry)
    pool.put(_custom("sub:orchestrator", roles=("orchestrator", "planner")))
    pool.put(_custom("sub:coder", roles=("implementer", "scout", "tester")))
    pool.put(_custom("sub:reviewer", roles=("reviewer", "security")))
    return pool


def test_intelligence_pool_merges_api_and_non_api_without_credentials(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    items = {item.endpoint_id: item for item in pool.list()}
    assert "api:openai:strong" in items
    assert items["api:openai:strong"].provider_id == "openai"
    assert CAP_TOOLS in items["api:openai:strong"].capabilities
    assert items["sub:coder"].already_paid is True
    assert "credential" not in str(items["sub:coder"].to_dict()).lower()


def test_intelligence_pool_custom_endpoint_persists(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    pool.put(
        _custom(
            "local:qwen",
            roles=("summarizer", "scout"),
            source=SOURCE_LOCAL,
            caps=(CAP_REASONING,),
        )
    )
    reloaded = IntelligencePool(path=tmp_path / "pool.json", provider_registry=pool.provider_registry)
    assert reloaded.get("local:qwen").source_kind == SOURCE_LOCAL
    assert reloaded.get("local:qwen").already_paid is True


def test_api_endpoint_cannot_be_persisted_in_pool(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    endpoint = pool.get("api:openai:strong")
    with pytest.raises(Exception, match="provider registry"):
        pool.put(endpoint)


def test_quota_brain_overrides_stale_or_static_endpoint_quota(tmp_path: Path) -> None:
    brain = QuotaBrain(tmp_path / "quota.json")
    endpoint = _custom("sub:coder", roles=("implementer",))
    brain.observe(
        endpoint.endpoint_id,
        remaining_fraction=0.42,
        source="test-adapter",
        observed_at=100.0,
    )
    assert brain.effective(endpoint, max_age_seconds=10_000_000_000).remaining_fraction == 0.42


def test_router_prefers_already_paid_capacity_when_quality_is_unknown(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    telemetry = RoutingTelemetry(tmp_path / "routing.json")
    router = VerifiedSmartRouter(
        pool=pool,
        telemetry=telemetry,
        provider_registry=pool.provider_registry,
        quota_brain=QuotaBrain(tmp_path / "quota.json"),
    )
    decision = router.decide(
        RouteRequest(
            task_class=TASK_IMPLEMENTATION,
            role="implementer",
            required_capabilities=(CAP_CODE, CAP_TOOLS),
            risk_level=RiskLevel.MEDIUM,
            estimated_input_tokens=100_000,
            estimated_output_tokens=10_000,
        )
    )
    assert decision.endpoint.endpoint_id == "sub:coder"
    assert decision.verified_quality_used is False


def test_router_uses_only_verified_local_outcomes_for_quality_score(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    reviewer = pool.get("sub:reviewer")
    pool.put(dataclasses.replace(reviewer, roles=("reviewer", "security", "implementer")))
    telemetry = RoutingTelemetry(tmp_path / "routing.json")
    for _ in range(5):
        telemetry.observe(
            endpoint_id="sub:reviewer",
            task_class=TASK_IMPLEMENTATION,
            accepted=True,
            verified=True,
        )
    for _ in range(5):
        telemetry.observe(
            endpoint_id="sub:coder",
            task_class=TASK_IMPLEMENTATION,
            accepted=True,
            verified=False,
        )
    router = VerifiedSmartRouter(
        pool=pool,
        telemetry=telemetry,
        quota_brain=QuotaBrain(tmp_path / "quota.json"),
    )
    decision = router.decide(
        RouteRequest(
            task_class=TASK_IMPLEMENTATION,
            role="implementer",
            required_capabilities=(CAP_CODE,),
            risk_level=RiskLevel.MEDIUM,
            minimum_verified_samples=3,
        )
    )
    assert decision.endpoint.endpoint_id == "sub:reviewer"
    assert decision.verified_quality_used is True


def test_high_risk_auto_route_refuses_unverified_unassigned_endpoint(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    pool = IntelligencePool(path=tmp_path / "pool.json", provider_registry=registry)
    # The API endpoint has no explicit role and no verified local evidence.
    router = VerifiedSmartRouter(
        pool=pool,
        telemetry=RoutingTelemetry(tmp_path / "routing.json"),
        quota_brain=QuotaBrain(tmp_path / "quota.json"),
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


def test_router_reserves_low_subscription_quota(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    reviewer = pool.get("sub:reviewer")
    pool.put(dataclasses.replace(reviewer, roles=("reviewer", "security", "implementer")))
    brain = QuotaBrain(tmp_path / "quota.json")
    brain.observe("sub:coder", remaining_fraction=0.05, source="test")
    brain.observe("sub:reviewer", remaining_fraction=0.80, source="test")
    router = VerifiedSmartRouter(pool=pool, telemetry=RoutingTelemetry(tmp_path / "routing.json"), quota_brain=brain)
    decision = router.decide(
        RouteRequest(
            task_class=TASK_IMPLEMENTATION,
            role="implementer",
            required_capabilities=(CAP_CODE,),
            reserve_quota_fraction=0.15,
        )
    )
    assert decision.endpoint.endpoint_id == "sub:reviewer"


def test_context_bus_avoids_resending_unchanged_content(tmp_path: Path) -> None:
    bus = ContextBus("test", path=tmp_path / "context.json")
    first = bus.put_text(
        item_id="architecture",
        kind="architecture",
        content="service -> repository",
        stable=True,
        priority=90,
    )
    assert first.changed is True
    second = bus.put_text(
        item_id="architecture",
        kind="architecture",
        content="service -> repository",
        stable=True,
        priority=90,
    )
    assert second.changed is False
    assert second.chars_avoided > 0
    projection = bus.projection(role="planner")
    delta = bus.delta(role="planner", known_hashes={})
    assert delta.sent_chars == projection.total_chars
    reused = bus.delta(role="planner", known_hashes=bus.manifest())
    assert reused.sent_chars == 0
    assert reused.reused_chars == projection.total_chars


def test_context_bus_role_projection_prioritizes_review_evidence(tmp_path: Path) -> None:
    bus = ContextBus("test", path=tmp_path / "context.json")
    bus.upsert(ContextItem.build(item_id="random-file", kind="file", content="x" * 1000, priority=10))
    bus.upsert(ContextItem.build(item_id="diff", kind="diff", content="D" * 1000, priority=50))
    bus.upsert(ContextItem.build(item_id="evidence", kind="evidence", content="E" * 1000, priority=50))
    projection = bus.projection(role="reviewer", budget_chars=2000)
    assert {item.item_id for item in projection.items} == {"diff", "evidence"}


def test_context_bus_redacts_secret_like_content(tmp_path: Path) -> None:
    bus = ContextBus("test", path=tmp_path / "context.json")
    bus.put_text(item_id="log", kind="log_summary", content="api_key=super-secret-value")
    stored = bus.projection(role="reviewer").items[0].content
    assert "super-secret-value" not in stored


def test_agent_handoff_is_bounded_and_persistent(tmp_path: Path) -> None:
    store = HandoffStore("run-test", path=tmp_path / "handoffs.json")
    message = AgentMessage.create(
        message_type=MESSAGE_REVIEW_FAIL,
        task_id="task-1",
        source_endpoint_id="sub:reviewer",
        target_endpoint_id="sub:orchestrator",
        source_role="reviewer",
        target_role="orchestrator",
        summary="review failed",
        findings=(ReviewFinding("high", "race condition", path="src/auth.py", line=20),),
    )
    store.append(message)
    loaded = store.latest("task-1")
    assert loaded is not None
    assert loaded.findings[0].severity == "high"
    assert loaded.findings[0].path == "src/auth.py"


def test_independent_review_prefers_different_provider_source() -> None:
    implementer = IntelligenceEndpoint(
        endpoint_id="api:p:m",
        display_name="impl",
        source_kind="api",
        capabilities=(CAP_CODE,),
        provider_id="p",
        model_id="m",
    )
    same = IntelligenceEndpoint(
        endpoint_id="api:p:r",
        display_name="same",
        source_kind="api",
        capabilities=(CAP_CODE,),
        provider_id="p",
        model_id="r",
    )
    different = _custom("sub:review", roles=("reviewer",))
    excluded = independent_review_exclusions(
        implementer_endpoint_id=implementer.endpoint_id,
        implementer_provider_id=implementer.provider_id,
        candidates=(implementer, same, different),
    )
    assert implementer.endpoint_id in excluded
    assert same.endpoint_id in excluded
    assert different.endpoint_id not in excluded


def test_builtin_recipes_require_independent_review() -> None:
    feature = recipe("feature")
    review = next(step for step in feature.steps if step.step_id == "review")
    assert review.independent_from == ("implement",)
    assert review.depends_on == ("implement", "test")


def test_recipe_rejects_unknown_dependency() -> None:
    with pytest.raises(ValueError, match="unknown steps"):
        OrchestrationRecipe(
            "bad",
            "bad",
            (RecipeStep("a", "planner", "architecture", (CAP_REASONING,), depends_on=("missing",)),),
        )


def test_orchestrator_plan_assigns_independent_reviewer(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    router = VerifiedSmartRouter(
        pool=pool,
        telemetry=RoutingTelemetry(tmp_path / "routing.json"),
        quota_brain=QuotaBrain(tmp_path / "quota.json"),
    )
    plan = Orchestrator(pool=pool, router=router).plan(
        objective="Fix the bug",
        recipe_name="bug-fix",
        role_assignments={
            "orchestrator": "sub:orchestrator",
            "scout": "sub:coder",
            "implementer": "sub:coder",
            "tester": "sub:coder",
            "reviewer": "sub:reviewer",
        },
    )
    assigned = {item.step.step_id: item.endpoint.endpoint_id for item in plan.steps}
    assert assigned["implement"] == "sub:coder"
    assert assigned["review"] == "sub:reviewer"


def _simple_plan(tmp_path: Path) -> tuple[OrchestrationPlan, IntelligenceEndpoint]:
    endpoint = _custom("sub:worker", roles=("scout", "orchestrator"))
    policy = OrchestrationPolicy.from_preset("balanced")
    step = RecipeStep("inspect", "scout", TASK_DISCOVERY, (CAP_REASONING,))
    telemetry = RoutingTelemetry(tmp_path / "routes.json")
    performance = telemetry.performance(endpoint.endpoint_id, TASK_DISCOVERY)
    from karox.orchestration_routing import RouteCandidate, RouteDecision

    candidate = RouteCandidate(endpoint, performance, 0.0, 10.0, True, ("explicit",))
    route = RouteDecision(endpoint, ("explicit",), (candidate,), False)
    return (
        OrchestrationPlan(
            run_id="run-simple",
            task_id="task-simple",
            objective="Inspect project",
            recipe_name="custom",
            orchestrator_endpoint=endpoint,
            risk_level=RiskLevel.LOW,
            policy=policy,
            steps=(PlannedStep(step, endpoint, route),),
            created_at=1.0,
        ),
        endpoint,
    )


def test_orchestration_runtime_journals_and_publishes_mission_control(tmp_path: Path) -> None:
    plan, endpoint = _simple_plan(tmp_path)
    bus = ContextBus("run-simple", path=tmp_path / "context.json")
    bus.put_text(item_id="task", kind="task", content="Inspect project", priority=100)
    mission = MissionControlStore("run-simple", path=tmp_path / "mission.json")
    journal = OrchestrationJournal("run-simple", path=tmp_path / "journal.json")
    seen_keys: list[str] = []

    def worker(request):
        seen_keys.append(request.idempotency_key)
        return WorkerExecutionResult(
            ok=True,
            summary="done",
            accepted=True,
            verified=True,
            cost_usd=0.5,
            total_tokens=123,
            context_updates=(
                {"item_id": "evidence", "kind": "evidence", "content": "verified", "priority": 80},
            ),
        )

    runtime = OrchestrationRuntime(
        plan,
        executors={endpoint.endpoint_id: CallbackWorkerExecutor(worker)},
        context_bus=bus,
        mission_control=mission,
        journal=journal,
        telemetry=RoutingTelemetry(tmp_path / "telemetry.json"),
        measured_baseline_cost_usd=2.0,
    )
    result = runtime.run()
    assert result.status == "passed"
    assert result.savings_receipt is not None
    assert result.savings_receipt.actual_cost.amount == 0.5
    assert result.savings_receipt.savings.amount == 1.5
    assert result.savings_receipt.savings_percent == 75.0
    assert seen_keys == ["run-simple:inspect:1"]
    assert journal.snapshot().steps[0].status == STEP_PASSED
    snapshot = mission.snapshot()
    assert snapshot is not None
    assert snapshot.status == "passed"
    assert snapshot.agents[0].status == "passed"
    assert bus.manifest()["evidence"]


def test_orchestration_runtime_does_not_replay_passed_step(tmp_path: Path) -> None:
    plan, endpoint = _simple_plan(tmp_path)
    bus = ContextBus("run-simple", path=tmp_path / "context.json")
    mission = MissionControlStore("run-simple", path=tmp_path / "mission.json")
    journal = OrchestrationJournal("run-simple", path=tmp_path / "journal.json")
    calls = 0

    def worker(_request):
        nonlocal calls
        calls += 1
        return WorkerExecutionResult(ok=True, summary="done", accepted=True, verified=True)

    runtime = OrchestrationRuntime(
        plan,
        executors={endpoint.endpoint_id: CallbackWorkerExecutor(worker)},
        context_bus=bus,
        mission_control=mission,
        journal=journal,
        telemetry=RoutingTelemetry(tmp_path / "telemetry.json"),
    )
    assert runtime.run().status == "passed"
    assert calls == 1
    # Re-create runtime from the same durable journal: the worker must not run again.
    runtime2 = OrchestrationRuntime(
        plan,
        executors={endpoint.endpoint_id: CallbackWorkerExecutor(worker)},
        context_bus=bus,
        mission_control=mission,
        journal=journal,
        telemetry=RoutingTelemetry(tmp_path / "telemetry.json"),
    )
    assert runtime2.run().status == "passed"
    assert calls == 1


def test_recovery_requires_reconciliation_for_interrupted_running_step(tmp_path: Path) -> None:
    journal = OrchestrationJournal("run-x", path=tmp_path / "journal.json")
    journal.initialize({"write": ("sub:coder", {"task": "write"})})
    journal.mark_started("write")
    snapshot = journal.reconcile_after_restart()
    assert snapshot.reconcile_required == ("write",)
    assert snapshot.steps[0].status == STEP_RECONCILE
    journal.resolve_reconciliation("write", completed=False, passed=False, summary="no live side effect")
    restarted = journal.mark_started("write")
    assert restarted.idempotency_key.endswith(":2")


def test_mobile_steer_is_consumed_at_safe_boundary(tmp_path: Path) -> None:
    plan, endpoint = _simple_plan(tmp_path)
    bus = ContextBus("run-simple", path=tmp_path / "context.json")
    mission = MissionControlStore("run-simple", path=tmp_path / "mission.json")
    mission.enqueue("steer", text="focus on retry logic")

    def worker(request):
        texts = [item.content for item in request.context_delta.changed]
        assert any("retry logic" in text for text in texts)
        return WorkerExecutionResult(ok=True, summary="done", accepted=True, verified=True)

    result = OrchestrationRuntime(
        plan,
        executors={endpoint.endpoint_id: CallbackWorkerExecutor(worker)},
        context_bus=bus,
        mission_control=mission,
        journal=OrchestrationJournal("run-simple", path=tmp_path / "journal.json"),
        telemetry=RoutingTelemetry(tmp_path / "telemetry.json"),
    ).run()
    assert result.status == "passed"
    assert mission.pending_commands() == []


def test_pause_keeps_runtime_alive_until_resume_and_accepts_steer(tmp_path: Path) -> None:
    plan, endpoint = _simple_plan(tmp_path)
    bus = ContextBus("run-simple", path=tmp_path / "context.json")
    mission = MissionControlStore("run-simple", path=tmp_path / "mission.json")
    mission.enqueue("pause")
    seen: list[str] = []

    def worker(request):
        seen.extend(item.content for item in request.context_delta.changed)
        return WorkerExecutionResult(ok=True, summary="done", accepted=True, verified=True)

    runtime = OrchestrationRuntime(
        plan,
        executors={endpoint.endpoint_id: CallbackWorkerExecutor(worker)},
        context_bus=bus,
        mission_control=mission,
        journal=OrchestrationJournal("run-simple", path=tmp_path / "journal.json"),
        telemetry=RoutingTelemetry(tmp_path / "telemetry.json"),
    )
    result_box: list[object] = []
    thread = threading.Thread(target=lambda: result_box.append(runtime.run()), daemon=True)
    thread.start()
    deadline = time.time() + 3
    while time.time() < deadline:
        snapshot = mission.snapshot()
        if snapshot is not None and snapshot.status == "paused":
            break
        time.sleep(0.02)
    else:
        pytest.fail("runtime did not enter paused state")
    assert thread.is_alive()
    mission.enqueue("steer", text="check the paused retry path")
    mission.enqueue("resume")
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert result_box and getattr(result_box[0], "status") == "passed"
    assert any("paused retry path" in text for text in seen)


def test_mobile_stop_finishes_at_boundary_without_starting_worker(tmp_path: Path) -> None:
    plan, endpoint = _simple_plan(tmp_path)
    bus = ContextBus("run-simple", path=tmp_path / "context.json")
    mission = MissionControlStore("run-simple", path=tmp_path / "mission.json")
    mission.enqueue("stop")
    calls = 0

    def worker(_request):
        nonlocal calls
        calls += 1
        return WorkerExecutionResult(ok=True, summary="done", accepted=True, verified=True)

    result = OrchestrationRuntime(
        plan,
        executors={endpoint.endpoint_id: CallbackWorkerExecutor(worker)},
        context_bus=bus,
        mission_control=mission,
        journal=OrchestrationJournal("run-simple", path=tmp_path / "journal.json"),
        telemetry=RoutingTelemetry(tmp_path / "telemetry.json"),
    ).run()
    assert result.status == "stopped"
    assert result.stopped_reason == "stopped_by_user"
    assert calls == 0
    snapshot = mission.snapshot()
    assert snapshot is not None and snapshot.status == "stopped"


def test_mobile_approval_request_does_not_mint_confirmation_token(tmp_path: Path) -> None:
    store = MissionControlStore("run", path=tmp_path / "mission.json")
    command = store.enqueue("approve_request", text="approve deploy")
    assert command.command_type == "approve_request"
    payload = command.to_dict()
    assert "token" not in payload
    assert "confirmation" not in payload


def test_pairing_authority_is_short_lived_and_session_based(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr("karox.mission_control_server.time.time", lambda: now[0])
    pairing = PairingAuthority(ttl_seconds=10)
    assert pairing.pair("wrong") is None
    token = pairing.pair(pairing.code)
    assert token is not None
    assert pairing.valid(token)
    now[0] = 111.0
    assert pairing.pair(pairing.code) is None
    # Existing paired session outlives the pairing window.
    assert pairing.valid(token)


def test_savings_receipt_never_invents_baseline_money() -> None:
    usage = UsageSummary(
        requests=1,
        prompt_tokens=1000,
        total_tokens=1100,
        cache_read_tokens=800,
        costs={"USD": 0.5},
        cache_savings={"USD": 0.3},
    )
    receipt = build_savings_receipt(
        usage=usage,
        counters=EconomyCounters(context_chars_reused=5000),
        accepted=True,
        verified=True,
        quality_gates=("tests:pass",),
    )
    assert receipt.actual_cost.amount == 0.5
    assert receipt.baseline_cost.amount is None
    assert receipt.savings.amount is None
    assert receipt.cache_savings.amount == 0.3


def test_savings_receipt_uses_only_explicit_measured_baseline() -> None:
    usage = UsageSummary(costs={"USD": 2.0})
    receipt = build_savings_receipt(
        usage=usage,
        measured_baseline_cost=8.0,
        accepted=True,
        verified=True,
    )
    assert receipt.savings.amount == 6.0
    assert receipt.savings_percent == 75.0


def test_shadow_route_and_replay_label_counterfactual_as_projection(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    router = VerifiedSmartRouter(
        pool=pool,
        telemetry=RoutingTelemetry(tmp_path / "routing.json"),
        quota_brain=QuotaBrain(tmp_path / "quota.json"),
    )
    request = RouteRequest(
        task_class=TASK_IMPLEMENTATION,
        role="implementer",
        required_capabilities=(CAP_CODE,),
        estimated_input_tokens=1_000_000,
        estimated_output_tokens=100_000,
    )
    report = shadow_route(router=router, request=request, actual_endpoint_id="api:openai:strong")
    assert report.recommended_endpoint_id == "sub:coder"
    replayed = ReplayLab(router).replay((ReplayCase("case", request, "api:openai:strong"),))
    assert replayed.cases == 1
    assert replayed.route_changes == 1
    assert "does not claim" in replayed.to_dict()["warning"]


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return completed.stdout.strip()


def test_worktree_pool_isolates_workers_and_detects_overlap(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "KaroX Test")
    (repo / "shared.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "shared.txt")
    _git(repo, "commit", "-m", "base")

    pool = WorktreePool(repo, root=tmp_path / "worktrees")
    a = pool.create(run_id="run", worker_id="a")
    b = pool.create(run_id="run", worker_id="b")
    (a.path / "shared.txt").write_text("a\n", encoding="utf-8")
    (b.path / "shared.txt").write_text("b\n", encoding="utf-8")
    status_a = pool.status(a)
    status_b = pool.status(b)
    report = pool.overlap((status_a, status_b))
    assert report.merge_safe_by_path is False
    assert report.overlapping_files["shared.txt"] == ("a", "b")
    with pytest.raises(WorktreePoolError, match="with changes"):
        pool.remove(a)
    listed = pool.list_statuses(run_id="run")
    assert {item.worktree.worker_id for item in listed} == {"a", "b"}
    assert all(not item.clean for item in listed)


def test_worktree_pool_composes_disjoint_changes_without_touching_primary(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "compose-repo")
    pool = WorktreePool(repo, root=tmp_path / "compose-worktrees")
    left = pool.create(run_id="run", worker_id="left")
    right = pool.create(run_id="run", worker_id="right")
    (left.path / "left.txt").write_text("left\n", encoding="utf-8")
    (right.path / "right.txt").write_text("right\n", encoding="utf-8")
    joined = pool.compose(run_id="run", worker_id="joined", sources=(left, right))
    assert (joined.path / "left.txt").read_text(encoding="utf-8") == "left\n"
    assert (joined.path / "right.txt").read_text(encoding="utf-8") == "right\n"
    assert not (repo / "left.txt").exists()
    assert not (repo / "right.txt").exists()


def test_worktree_pool_allows_identical_ancestry_but_rejects_divergence(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "ancestry-repo")
    pool = WorktreePool(repo, root=tmp_path / "ancestry-worktrees")
    left = pool.create(run_id="run", worker_id="left")
    right = pool.create(run_id="run", worker_id="right")
    for worktree in (left, right):
        (worktree.path / "base.txt").write_text("shared parent\n", encoding="utf-8")
    (left.path / "left.txt").write_text("left\n", encoding="utf-8")
    (right.path / "right.txt").write_text("right\n", encoding="utf-8")
    joined = pool.compose(run_id="run", worker_id="joined", sources=(left, right))
    assert (joined.path / "base.txt").read_text(encoding="utf-8") == "shared parent\n"
    (right.path / "base.txt").write_text("divergent child\n", encoding="utf-8")
    with pytest.raises(WorktreePoolError, match="conflicting changes"):
        pool.compose(run_id="run", worker_id="conflict", sources=(left, right))


def test_worktree_pool_composition_preserves_worker_local_commits(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "committed-repo")
    pool = WorktreePool(repo, root=tmp_path / "committed-worktrees")
    worker = pool.create(run_id="run", worker_id="writer")
    (worker.path / "committed.txt").write_text("committed\n", encoding="utf-8")
    _git(worker.path, "add", "committed.txt")
    _git(worker.path, "commit", "-m", "worker commit")
    reopened = pool.open(run_id="run", worker_id="writer")
    assert reopened.base_revision == worker.base_revision
    assert "committed.txt" in pool.status(reopened).changed_files
    joined = pool.compose(run_id="run", worker_id="joined", sources=(reopened,))
    assert (joined.path / "committed.txt").read_text(encoding="utf-8") == "committed\n"
    assert not (repo / "committed.txt").exists()


def test_mission_snapshot_round_trip(tmp_path: Path) -> None:
    store = MissionControlStore("run", path=tmp_path / "mission.json")
    snapshot = MissionSnapshot(
        run_id="run",
        task_id="task",
        objective="ship",
        recipe="feature",
        orchestrator_endpoint_id="sub:orchestrator",
        status="running",
        agents=(),
        actual_cost_usd=1.25,
        total_tokens=100,
        context_reused_chars=500,
        updated_at=1.0,
    )
    store.update_snapshot(snapshot)
    loaded = store.snapshot()
    assert loaded is not None
    assert loaded.actual_cost_usd == 1.25
    assert loaded.context_reused_chars == 500


def test_prompt_envelope_keeps_stable_prefix_deterministic() -> None:
    stable_b = ContextItem.build(
        item_id="b", kind="architecture", content="B", stable=True
    )
    stable_a = ContextItem.build(
        item_id="a", kind="architecture", content="A", stable=True
    )
    volatile = ContextItem.build(
        item_id="task", kind="task", content="current", stable=False
    )
    first = build_prompt_envelope((stable_b, volatile, stable_a))
    second = build_prompt_envelope((volatile, stable_a, stable_b))
    assert first.stable_prefix == second.stable_prefix
    assert first.stable_prefix_hash == second.stable_prefix_hash
    assert "current" not in first.stable_prefix
    assert "current" in first.volatile_context


def test_high_risk_feature_plan_adds_independent_security_review(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    router = VerifiedSmartRouter(
        pool=pool,
        telemetry=RoutingTelemetry(tmp_path / "routing.json"),
        quota_brain=QuotaBrain(tmp_path / "quota.json"),
    )
    plan = Orchestrator(pool=pool, router=router).plan(
        objective="Change authentication semantics",
        recipe_name="feature",
        risk_level=RiskLevel.HIGH,
        role_assignments={
            "orchestrator": "sub:orchestrator",
            "scout": "sub:coder",
            "planner": "sub:orchestrator",
            "implementer": "sub:coder",
            "tester": "sub:coder",
            "reviewer": "sub:reviewer",
            "security": "sub:reviewer",
        },
    )
    security = next(item for item in plan.steps if item.step.step_id == "security-review")
    assert security.step.role == "security"
    assert "implement" in security.step.independent_from
    assert security.endpoint.endpoint_id == "sub:reviewer"


def test_feature_ui_step_requires_real_browser_capability(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    router = VerifiedSmartRouter(
        pool=pool,
        telemetry=RoutingTelemetry(tmp_path / "routing.json"),
        quota_brain=QuotaBrain(tmp_path / "quota.json"),
    )
    plan = Orchestrator(pool=pool, router=router).plan(
        objective="Build a page",
        recipe_name="feature",
        role_assignments={
            "orchestrator": "sub:orchestrator",
            "scout": "sub:coder",
            "planner": "sub:orchestrator",
            "implementer": "sub:coder",
            "tester": "sub:coder",
            "reviewer": "sub:reviewer",
        },
    )
    assert all(item.step.step_id != "ui" for item in plan.steps)


def test_cli_parser_exposes_v5_orchestration_surfaces() -> None:
    from karox.cli import _parser

    parser = _parser()
    assert parser.parse_args(["intelligence", "list"]).command == "intelligence"
    assert parser.parse_args(["orchestrate", "recipes"]).command == "orchestrate"
    assert parser.parse_args(["mission-control", "show", "run-x"]).command == "mission-control"
    assert parser.parse_args(
        [
            "economy",
            "receipt",
            "--usage-json",
            "usage.json",
            "--accepted",
            "--verified",
        ]
    ).command == "economy"


def test_cli_recipes_runs_without_provider_credentials(capsys) -> None:
    from karox.cli import main

    assert main(["orchestrate", "recipes", "--json"]) == 0
    payload = __import__("json").loads(capsys.readouterr().out)
    assert {item["name"] for item in payload} >= {"feature", "bug-fix", "security"}


def _explicit_planned_step(
    tmp_path: Path,
    *,
    step_id: str,
    role: str,
    endpoint: IntelligenceEndpoint,
    depends_on: tuple[str, ...] = (),
) -> PlannedStep:
    from karox.orchestration_routing import RouteCandidate, RouteDecision

    task_class = TASK_IMPLEMENTATION if role == "implementer" else TASK_DISCOVERY
    step = RecipeStep(
        step_id,
        role,
        task_class,
        (CAP_CODE,) if role == "implementer" else (CAP_REASONING,),
        depends_on=depends_on,
    )
    telemetry = RoutingTelemetry(tmp_path / f"route-{step_id}.json")
    performance = telemetry.performance(endpoint.endpoint_id, task_class)
    candidate = RouteCandidate(endpoint, performance, 0.0, 10.0, True, ("explicit",))
    route = RouteDecision(endpoint, ("explicit",), (candidate,), False)
    return PlannedStep(step, endpoint, route)


def test_parallel_runtime_overlaps_independent_read_workers(tmp_path: Path) -> None:
    a = _custom("sub:parallel-a", roles=("scout", "orchestrator"))
    b = _custom("sub:parallel-b", roles=("scout",))
    policy = dataclasses.replace(
        OrchestrationPolicy.from_preset("balanced"),
        max_parallel_workers=2,
        require_independent_review=False,
    )
    plan = OrchestrationPlan(
        run_id="run-parallel-reads",
        task_id="task-parallel-reads",
        objective="Inspect two independent areas",
        recipe_name="parallel-test",
        orchestrator_endpoint=a,
        risk_level=RiskLevel.LOW,
        policy=policy,
        steps=(
            _explicit_planned_step(tmp_path, step_id="left", role="scout", endpoint=a),
            _explicit_planned_step(tmp_path, step_id="right", role="scout", endpoint=b),
        ),
        created_at=1.0,
    )
    barrier = threading.Barrier(2, timeout=2.0)
    lock = threading.Lock()
    active = 0
    maximum_active = 0

    def worker(_request):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            barrier.wait()
            time.sleep(0.03)
            return WorkerExecutionResult(
                ok=True,
                summary="parallel read complete",
                accepted=True,
                verified=True,
            )
        finally:
            with lock:
                active -= 1

    executor = CallbackWorkerExecutor(worker)
    result = OrchestrationRuntime(
        plan,
        executors={a.endpoint_id: executor, b.endpoint_id: executor},
        context_bus=ContextBus("run-parallel-reads", path=tmp_path / "context-parallel.json"),
        mission_control=MissionControlStore("run-parallel-reads", path=tmp_path / "mission-parallel.json"),
        journal=OrchestrationJournal("run-parallel-reads", path=tmp_path / "journal-parallel.json"),
        telemetry=RoutingTelemetry(tmp_path / "telemetry-parallel.json"),
    ).run()
    assert result.status == "passed"
    assert maximum_active == 2
    assert [item.step_id for item in result.steps] == ["left", "right"]


def test_parallel_runtime_serializes_unisolated_implementers(tmp_path: Path) -> None:
    a = _custom("sub:writer-a", roles=("implementer", "orchestrator"))
    b = _custom("sub:writer-b", roles=("implementer",))
    policy = dataclasses.replace(
        OrchestrationPolicy.from_preset("balanced"),
        max_parallel_workers=2,
        require_independent_review=False,
    )
    plan = OrchestrationPlan(
        run_id="run-serial-writers",
        task_id="task-serial-writers",
        objective="Run two bounded writers without worktree isolation",
        recipe_name="parallel-test",
        orchestrator_endpoint=a,
        risk_level=RiskLevel.MEDIUM,
        policy=policy,
        steps=(
            _explicit_planned_step(tmp_path, step_id="writer-a", role="implementer", endpoint=a),
            _explicit_planned_step(tmp_path, step_id="writer-b", role="implementer", endpoint=b),
        ),
        created_at=1.0,
    )
    lock = threading.Lock()
    active = 0
    maximum_active = 0

    def worker(_request):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        try:
            time.sleep(0.04)
            return WorkerExecutionResult(
                ok=True,
                summary="writer complete",
                accepted=True,
                verified=True,
            )
        finally:
            with lock:
                active -= 1

    executor = CallbackWorkerExecutor(worker)
    result = OrchestrationRuntime(
        plan,
        executors={a.endpoint_id: executor, b.endpoint_id: executor},
        context_bus=ContextBus("run-serial-writers", path=tmp_path / "context-serial-writers.json"),
        mission_control=MissionControlStore("run-serial-writers", path=tmp_path / "mission-serial-writers.json"),
        journal=OrchestrationJournal("run-serial-writers", path=tmp_path / "journal-serial-writers.json"),
        telemetry=RoutingTelemetry(tmp_path / "telemetry-serial-writers.json"),
    ).run()
    assert result.status == "passed"
    assert maximum_active == 1


def test_isolated_implementers_overlap_and_disjoint_lineages_are_composed(tmp_path: Path) -> None:
    repo = tmp_path / "parallel-repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "test@example.test")
    _git(repo, "config", "user.name", "KaroX Test")
    (repo / "base.txt").write_text("base\n", encoding="utf-8")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")

    a = _custom("sub:isolated-a", roles=("implementer", "orchestrator"))
    b = _custom("sub:isolated-b", roles=("implementer",))
    judge = _custom("sub:judge", roles=("orchestrator",))
    policy = dataclasses.replace(
        OrchestrationPolicy.from_preset("balanced"),
        max_parallel_workers=2,
        require_independent_review=False,
    )
    plan = OrchestrationPlan(
        run_id="run-isolated-writers",
        task_id="task-isolated-writers",
        objective="Run two isolated writers then integrate",
        recipe_name="parallel-test",
        orchestrator_endpoint=judge,
        risk_level=RiskLevel.MEDIUM,
        policy=policy,
        steps=(
            _explicit_planned_step(tmp_path, step_id="isolated-a", role="implementer", endpoint=a),
            _explicit_planned_step(tmp_path, step_id="isolated-b", role="implementer", endpoint=b),
            _explicit_planned_step(
                tmp_path,
                step_id="integrate",
                role="scout",
                endpoint=judge,
                depends_on=("isolated-a", "isolated-b"),
            ),
        ),
        created_at=1.0,
    )
    barrier = threading.Barrier(2, timeout=2.0)
    lock = threading.Lock()
    active = 0
    maximum_active = 0
    workspaces: set[str] = set()

    def worker(request):
        nonlocal active, maximum_active
        assert request.workspace_path is not None
        workspace = Path(request.workspace_path)
        if request.role == "implementer":
            workspaces.add(request.workspace_path)
            marker = "a.txt" if request.step_id == "isolated-a" else "b.txt"
            (workspace / marker).write_text(request.step_id, encoding="utf-8")
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
            try:
                barrier.wait()
                time.sleep(0.03)
            finally:
                with lock:
                    active -= 1
        else:
            assert (workspace / "a.txt").read_text(encoding="utf-8") == "isolated-a"
            assert (workspace / "b.txt").read_text(encoding="utf-8") == "isolated-b"
        return WorkerExecutionResult(
            ok=True,
            summary="isolated worker complete",
            accepted=True,
            verified=True,
        )

    executor = CallbackWorkerExecutor(worker)
    pool = WorktreePool(repo, root=tmp_path / "parallel-worktrees")
    result = OrchestrationRuntime(
        plan,
        executors={
            a.endpoint_id: executor,
            b.endpoint_id: executor,
            judge.endpoint_id: executor,
        },
        context_bus=ContextBus("run-isolated-writers", path=tmp_path / "context-isolated.json"),
        mission_control=MissionControlStore("run-isolated-writers", path=tmp_path / "mission-isolated.json"),
        journal=OrchestrationJournal("run-isolated-writers", path=tmp_path / "journal-isolated.json"),
        telemetry=RoutingTelemetry(tmp_path / "telemetry-isolated.json"),
        worktree_pool=pool,
        isolate_implementers=True,
    ).run()
    assert maximum_active == 2
    assert len(workspaces) == 2
    assert result.status == "passed"
    assert any(item.step_id == "integrate" and item.status == "passed" for item in result.steps)


def test_recipe_registry_accepts_only_data_contract(tmp_path: Path) -> None:
    registry = RecipeRegistry(tmp_path / "recipes.json")
    registry.put_dict(
        {
            "name": "lean-fix",
            "description": "Scout, implement, verify.",
            "steps": [
                {
                    "step_id": "scout",
                    "role": "scout",
                    "task_class": "repo_discovery",
                    "required_capabilities": ["reasoning"],
                },
                {
                    "step_id": "implement",
                    "role": "implementer",
                    "task_class": "implementation",
                    "required_capabilities": ["code", "tools"],
                    "depends_on": ["scout"],
                },
            ],
        }
    )
    assert registry.get("lean-fix").steps[1].depends_on == ("scout",)
    assert {item.name for item in registry.list()} >= {"feature", "lean-fix"}
    with pytest.raises(ValueError, match="unsupported fields"):
        registry.put_dict(
            {
                "name": "unsafe",
                "description": "must fail",
                "steps": [
                    {
                        "step_id": "x",
                        "role": "scout",
                        "task_class": "repo_discovery",
                        "required_capabilities": ["reasoning"],
                        "argv": ["powershell", "-Command", "whoami"],
                    }
                ],
            }
        )
    with pytest.raises(RecipeRegistryError, match="built-in"):
        registry.remove("feature")


def test_orchestrator_can_plan_custom_data_recipe(tmp_path: Path) -> None:
    recipes = RecipeRegistry(tmp_path / "recipes.json")
    recipes.put_dict(
        {
            "name": "inspect-only",
            "description": "One scout.",
            "steps": [
                {
                    "step_id": "inspect",
                    "role": "scout",
                    "task_class": "repo_discovery",
                    "required_capabilities": ["reasoning", "tools"],
                }
            ],
        }
    )
    pool = _pool(tmp_path)
    router = VerifiedSmartRouter(
        pool=pool,
        telemetry=RoutingTelemetry(tmp_path / "routing.json"),
        quota_brain=QuotaBrain(tmp_path / "quota.json"),
    )
    plan = Orchestrator(pool=pool, router=router, recipe_registry=recipes).plan(
        objective="Inspect",
        recipe_name="inspect-only",
        role_assignments={"orchestrator": "sub:orchestrator", "scout": "sub:coder"},
    )
    assert [item.step.step_id for item in plan.steps] == ["inspect", "orchestrator-judge"]
    assert plan.steps[-1].endpoint.endpoint_id == "sub:orchestrator"


def test_orchestration_effort_presets_and_overrides_are_real_plan_data(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    router = VerifiedSmartRouter(
        pool=pool,
        telemetry=RoutingTelemetry(tmp_path / "routing-effort.json"),
        quota_brain=QuotaBrain(tmp_path / "quota-effort.json"),
    )
    assignments = {
        "orchestrator": "sub:orchestrator",
        "scout": "sub:coder",
        "implementer": "sub:coder",
        "tester": "sub:coder",
        "reviewer": "sub:reviewer",
    }
    economy = Orchestrator(pool=pool, router=router).plan(
        objective="Fix retry semantics",
        recipe_name="bug-fix",
        policy=OrchestrationPolicy.from_preset("maximum_economy"),
        role_assignments=assignments,
    )
    efforts = {item.step.step_id: item.effort_level for item in economy.steps}
    assert efforts["investigate"] == "low"
    assert efforts["implement"] == "medium"
    assert efforts["regression"] == "low"
    assert efforts["review"] == "high"
    assert efforts["orchestrator-judge"] == "high"

    overridden = Orchestrator(pool=pool, router=router).plan(
        objective="Fix retry semantics",
        recipe_name="bug-fix",
        policy=OrchestrationPolicy.from_preset("maximum_economy"),
        role_assignments=assignments,
        effort_assignments={"implementer": "ultra", "review": "low"},
    )
    override_efforts = {item.step.step_id: item.effort_level for item in overridden.steps}
    assert override_efforts["implement"] == "ultra"
    assert override_efforts["review"] == "low"
    assert overridden.to_dict()["steps"][1]["effort_level"] == "ultra"

    with pytest.raises(ValueError, match="unknown effort assignment"):
        Orchestrator(pool=pool, router=router).plan(
            objective="Fix retry semantics",
            recipe_name="bug-fix",
            role_assignments=assignments,
            effort_assignments={"typo-role": "high"},
        )


def test_shadow_economy_ledger_is_passive_and_summarizes_projection(tmp_path: Path) -> None:
    pool = _pool(tmp_path)
    router = VerifiedSmartRouter(
        pool=pool,
        telemetry=RoutingTelemetry(tmp_path / "routing.json"),
        quota_brain=QuotaBrain(tmp_path / "quota.json"),
    )
    request = RouteRequest(
        task_class=TASK_IMPLEMENTATION,
        role="implementer",
        required_capabilities=(CAP_CODE,),
        estimated_input_tokens=1_000_000,
        estimated_output_tokens=100_000,
    )
    ledger = ShadowEconomyLedger(tmp_path / "shadow.json")
    event = ledger.observe(
        router=router,
        task_id="task-1",
        request=request,
        actual_endpoint_id="api:openai:strong",
        accepted=True,
        verified=True,
        actual_cost_usd=1.25,
        timestamp=10.0,
    )
    assert event.route_report.recommended_endpoint_id == "sub:coder"
    assert event.actual_endpoint_id == "api:openai:strong"
    summary = ledger.summary()
    assert summary.events == 1
    assert summary.accepted_verified_events == 1
    assert summary.route_changes == 1
    assert summary.projected_avoidable_cost_usd > 0


def test_worker_adapter_registry_maps_metadata_to_guarded_callable(tmp_path: Path) -> None:
    endpoint = _custom("sub:coder", roles=("implementer",))
    captured: dict[str, object] = {}

    def target(**kwargs):
        captured.update(kwargs)
        return PromptTargetResult(
            ok=True,
            summary="done",
            accepted=True,
            verified=True,
        )

    registry = WorkerAdapterRegistry()
    registry.register(endpoint.target_id or "", target)
    executor = registry.executor_for(endpoint)
    bus = ContextBus("adapter", path=tmp_path / "context.json")
    bus.put_text(item_id="task", kind="task", content="do it")
    request = WorkerExecutionRequest(
        run_id="run",
        task_id="task",
        step_id="implement",
        idempotency_key="run:implement:1",
        role="implementer",
        task_class=TASK_IMPLEMENTATION,
        objective="do it",
        endpoint=endpoint,
        context_delta=bus.delta(role="implementer", known_hashes={}),
        upstream_messages=(),
        max_cost_usd=1.0,
        workspace_path="C:/safe/worktree",
    )
    result = executor(request)
    assert result.ok
    assert captured["idempotency_key"] == "run:implement:1"
    assert captured["workspace_path"] == "C:/safe/worktree"
    with pytest.raises(WorkerAdapterRegistryError, match="no guarded worker adapter"):
        WorkerAdapterRegistry().executor_for(endpoint)


def test_mission_snapshot_reports_progress() -> None:
    snapshot = MissionSnapshot(
        run_id="run",
        task_id="task",
        objective="ship",
        recipe="feature",
        orchestrator_endpoint_id="sub:orchestrator",
        status="running",
        agents=(
            AgentStatus("a", "scout", "one", "passed"),
            AgentStatus("b", "implementer", "two", "running"),
            AgentStatus("c", "reviewer", "three", "pending"),
        ),
    )
    payload = snapshot.to_dict()
    assert payload["completed_agents"] == 1
    assert payload["running_agents"] == 1
    assert payload["progress_percent"] == 33.3


def _multi_step_plan(
    endpoint: IntelligenceEndpoint,
    *,
    run_id: str,
    steps: tuple[RecipeStep, ...],
) -> OrchestrationPlan:
    from karox.orchestration_routing import RouteCandidate, RouteDecision

    planned: list[PlannedStep] = []
    for step in steps:
        performance = RoutingTelemetry(Path("unused-routing.json")).performance(
            endpoint.endpoint_id, step.task_class
        )
        candidate = RouteCandidate(endpoint, performance, 0.0, 10.0, True, ("explicit",))
        route = RouteDecision(endpoint, ("explicit",), (candidate,), False)
        planned.append(PlannedStep(step, endpoint, route))
    return OrchestrationPlan(
        run_id=run_id,
        task_id=f"task-{run_id}",
        objective="worktree test",
        recipe_name="custom",
        orchestrator_endpoint=endpoint,
        risk_level=RiskLevel.LOW,
        policy=OrchestrationPolicy.from_preset("balanced"),
        steps=tuple(planned),
        created_at=1.0,
    )


def _init_git_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.test")
    _git(path, "config", "user.name", "KaroX Test")
    (path / "base.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "base.txt")
    _git(path, "commit", "-m", "base")
    return path


def test_runtime_worktree_lineage_flows_implementer_to_test_and_review(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    endpoint = _custom("sub:worker", roles=("implementer", "tester", "reviewer", "orchestrator"))
    steps = (
        RecipeStep("implement", "implementer", TASK_IMPLEMENTATION, (CAP_CODE,)),
        RecipeStep("test", "tester", "testing", (CAP_TOOLS,), depends_on=("implement",)),
        RecipeStep(
            "review",
            "reviewer",
            "review",
            (CAP_REASONING,),
            depends_on=("implement", "test"),
            independent_from=("implement",),
        ),
    )
    plan = _multi_step_plan(endpoint, run_id="run-lineage", steps=steps)
    seen: dict[str, str | None] = {}

    def worker(request: WorkerExecutionRequest) -> WorkerExecutionResult:
        seen[request.step_id] = request.workspace_path
        assert request.workspace_path is not None
        workspace = Path(request.workspace_path)
        if request.step_id == "implement":
            (workspace / "feature.txt").write_text("implemented\n", encoding="utf-8")
        else:
            assert (workspace / "feature.txt").read_text(encoding="utf-8") == "implemented\n"
        return WorkerExecutionResult(ok=True, summary="done", accepted=True, verified=True)

    result = OrchestrationRuntime(
        plan,
        executors={endpoint.endpoint_id: CallbackWorkerExecutor(worker)},
        context_bus=ContextBus("lineage", path=tmp_path / "context.json"),
        handoffs=HandoffStore("run-lineage", path=tmp_path / "handoffs.json"),
        telemetry=RoutingTelemetry(tmp_path / "telemetry.json"),
        mission_control=MissionControlStore("run-lineage", path=tmp_path / "mission.json"),
        journal=OrchestrationJournal("run-lineage", path=tmp_path / "journal.json"),
        worktree_pool=WorktreePool(repo, root=tmp_path / "worktrees"),
        isolate_implementers=True,
    ).run()
    assert result.status == "passed"
    assert seen["implement"] == seen["test"] == seen["review"]
    assert result.workspaces["implement"] == result.workspaces["review"]


def test_runtime_dependent_implementer_forks_upstream_dirty_lineage(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "dependent-repo")
    endpoint = _custom("sub:worker", roles=("implementer", "tester", "orchestrator"))
    steps = (
        RecipeStep("parent", "implementer", TASK_IMPLEMENTATION, (CAP_CODE,)),
        RecipeStep(
            "child",
            "implementer",
            TASK_IMPLEMENTATION,
            (CAP_CODE,),
            depends_on=("parent",),
        ),
        RecipeStep("verify", "tester", "testing", (CAP_TOOLS,), depends_on=("child",)),
    )
    plan = _multi_step_plan(endpoint, run_id="run-dependent", steps=steps)
    seen: dict[str, str] = {}

    def worker(request: WorkerExecutionRequest) -> WorkerExecutionResult:
        assert request.workspace_path is not None
        seen[request.step_id] = request.workspace_path
        workspace = Path(request.workspace_path)
        if request.step_id == "parent":
            (workspace / "parent.txt").write_text("parent\n", encoding="utf-8")
        elif request.step_id == "child":
            assert (workspace / "parent.txt").read_text(encoding="utf-8") == "parent\n"
            (workspace / "child.txt").write_text("child\n", encoding="utf-8")
        else:
            assert (workspace / "parent.txt").read_text(encoding="utf-8") == "parent\n"
            assert (workspace / "child.txt").read_text(encoding="utf-8") == "child\n"
        return WorkerExecutionResult(ok=True, summary="done", accepted=True, verified=True)

    result = OrchestrationRuntime(
        plan,
        executors={endpoint.endpoint_id: CallbackWorkerExecutor(worker)},
        context_bus=ContextBus("dependent", path=tmp_path / "context-dependent.json"),
        handoffs=HandoffStore("run-dependent", path=tmp_path / "handoffs-dependent.json"),
        telemetry=RoutingTelemetry(tmp_path / "telemetry-dependent.json"),
        mission_control=MissionControlStore("run-dependent", path=tmp_path / "mission-dependent.json"),
        journal=OrchestrationJournal("run-dependent", path=tmp_path / "journal-dependent.json"),
        worktree_pool=WorktreePool(repo, root=tmp_path / "worktrees-dependent"),
        isolate_implementers=True,
    ).run()
    assert result.status == "passed"
    assert seen["parent"] != seen["child"]
    assert seen["verify"] == seen["child"]


def test_runtime_refuses_conflicting_parallel_worktrees(tmp_path: Path) -> None:
    repo = _init_git_repo(tmp_path / "repo")
    endpoint = _custom("sub:worker", roles=("implementer", "tester", "orchestrator"))
    steps = (
        RecipeStep("impl-a", "implementer", TASK_IMPLEMENTATION, (CAP_CODE,)),
        RecipeStep("impl-b", "implementer", TASK_IMPLEMENTATION, (CAP_CODE,)),
        RecipeStep(
            "integration",
            "tester",
            "testing",
            (CAP_TOOLS,),
            depends_on=("impl-a", "impl-b"),
        ),
    )
    plan = _multi_step_plan(endpoint, run_id="run-parallel", steps=steps)
    calls: list[str] = []

    def worker(request: WorkerExecutionRequest) -> WorkerExecutionResult:
        calls.append(request.step_id)
        if request.role == "implementer":
            assert request.workspace_path is not None
            Path(request.workspace_path, "base.txt").write_text(
                request.step_id + "\n",
                encoding="utf-8",
            )
        return WorkerExecutionResult(ok=True, summary="done", accepted=True, verified=True)

    result = OrchestrationRuntime(
        plan,
        executors={endpoint.endpoint_id: CallbackWorkerExecutor(worker)},
        context_bus=ContextBus("parallel", path=tmp_path / "context.json"),
        handoffs=HandoffStore("run-parallel", path=tmp_path / "handoffs.json"),
        telemetry=RoutingTelemetry(tmp_path / "telemetry.json"),
        mission_control=MissionControlStore("run-parallel", path=tmp_path / "mission.json"),
        journal=OrchestrationJournal("run-parallel", path=tmp_path / "journal.json"),
        worktree_pool=WorktreePool(repo, root=tmp_path / "worktrees"),
        isolate_implementers=True,
    ).run()
    assert calls == ["impl-a", "impl-b"]
    assert result.status == "failed"
    assert result.stopped_reason is not None
    assert "conflicting changes" in result.stopped_reason
    assert len({result.workspaces["impl-a"], result.workspaces["impl-b"]}) == 2


def test_a_long_worker_summary_is_clipped_not_fatal(tmp_path: Path) -> None:
    """A worker that answers at length must not abort the mission at the handoff.

    The summary is model output; the handoff message has a hard bound. Raising
    there failed a mission whose work was already done and verified, with
    "message summary exceeds 3000 characters".
    """
    plan, endpoint = _simple_plan(tmp_path)
    long_summary = "verified the change. " * 400

    def worker(_request):
        return WorkerExecutionResult(
            ok=True, summary=long_summary, accepted=True, verified=True
        )

    runtime = OrchestrationRuntime(
        plan,
        executors={endpoint.endpoint_id: CallbackWorkerExecutor(worker)},
        context_bus=ContextBus("run-simple", path=tmp_path / "context-long.json"),
        mission_control=MissionControlStore(
            "run-simple", path=tmp_path / "mission-long.json"
        ),
        journal=OrchestrationJournal("run-simple", path=tmp_path / "journal-long.json"),
        telemetry=RoutingTelemetry(tmp_path / "telemetry.json"),
    )

    result = runtime.run()

    assert result.status == "passed"
    handoff = runtime.handoffs.latest(plan.task_id)
    assert handoff is not None
    assert len(handoff.summary) <= SUMMARY_LIMIT
    assert handoff.summary.endswith("…")
    # The durable journal still records what the worker actually said.
    assert result.steps[0].summary == long_summary


def test_bounded_summary_normalises_then_clips() -> None:
    assert bounded_summary("  short  ") == "short"
    assert bounded_summary("x" * 10, limit=5) == "xxxx…"
    assert len(bounded_summary("x" * 5000)) == SUMMARY_LIMIT
    # The clip is applied to the same normalised text the validator sees, so
    # the result always passes that validation; an empty string stays empty.
    assert bounded_summary("") == ""
    assert bounded_summary("x" * 4000).rstrip("…") != "x" * 4000
