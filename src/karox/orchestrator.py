"""Role-based orchestration runtime for KaroX 5.

The runtime plans and coordinates work but does not create a second execution
sandbox.  Actual worker execution is supplied by registered adapters (native API
agent, guarded MCP target, supported subscription agent, local model, etc.).
Every adapter therefore remains behind the same KaroX policy/runtime boundary.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import time
import uuid
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Protocol

from .agent_protocol import (
    AgentMessage,
    EvidenceReference,
    HandoffStore,
    MESSAGE_ESCALATE,
    MESSAGE_REQUEST,
    MESSAGE_RESULT,
    MESSAGE_REVIEW_FAIL,
    MESSAGE_REVIEW_PASS,
    ReviewFinding,
    independent_review_exclusions,
)
from .context_bus import ContextBus, ContextDelta
from .cost_intelligence import CostGovernor
from .economy_engine import EconomyCounters, SavingsReceipt, build_savings_receipt
from .effort import normalize_effort
from .intelligence_pool import IntelligenceEndpoint, IntelligencePool
from .mission_control import AgentStatus, MissionControlStore, MissionSnapshot
from .orchestration_recipes import OrchestrationRecipe, RecipeStep
from .recipe_registry import RecipeRegistry
from .orchestration_recovery import (
    OrchestrationJournal,
    STEP_FAILED as JOURNAL_FAILED,
    STEP_PASSED as JOURNAL_PASSED,
)
from .orchestration_routing import RouteDecision, RouteRequest, RoutingTelemetry, VerifiedSmartRouter
from .risk_engine import RiskLevel
from .usage_analytics import UsageSummary
from .worktree_pool import WorktreePool


PRESET_MAX_QUALITY = "maximum_quality"
PRESET_BALANCED = "balanced"
PRESET_MAX_ECONOMY = "maximum_economy"
PRESET_CUSTOM = "custom"
PRESETS = frozenset({PRESET_MAX_QUALITY, PRESET_BALANCED, PRESET_MAX_ECONOMY, PRESET_CUSTOM})

# Orchestration reuses KaroX's existing first-class Effort ladder. Presets spend
# more reasoning budget on coordination/review than on rediscovery and cheap
# mechanical work; explicit per-role/per-step overrides always win.
_PRESET_ROLE_EFFORT: dict[str, dict[str, str]] = {
    PRESET_MAX_QUALITY: {
        "orchestrator": "extra-high",
        "planner": "extra-high",
        "scout": "high",
        "implementer": "extra-high",
        "tester": "high",
        "reviewer": "extra-high",
        "security": "ultra",
        "summarizer": "medium",
        "ui": "extra-high",
    },
    PRESET_BALANCED: {
        "orchestrator": "high",
        "planner": "high",
        "scout": "medium",
        "implementer": "high",
        "tester": "medium",
        "reviewer": "high",
        "security": "extra-high",
        "summarizer": "low",
        "ui": "high",
    },
    PRESET_MAX_ECONOMY: {
        "orchestrator": "high",
        "planner": "medium",
        "scout": "low",
        "implementer": "medium",
        "tester": "low",
        "reviewer": "high",
        "security": "high",
        "summarizer": "low",
        "ui": "medium",
    },
    PRESET_CUSTOM: {},
}

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_STOPPED = "stopped"
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_BLOCKED = "blocked"
TERMINAL_STATUSES = frozenset({STATUS_PASSED, STATUS_FAILED, STATUS_SKIPPED, STATUS_BLOCKED})


class OrchestrationError(RuntimeError):
    pass


@dataclasses.dataclass(frozen=True)
class OrchestrationPolicy:
    preset: str = PRESET_BALANCED
    max_parallel_workers: int = 4
    soft_budget_usd: float = 5.0
    hard_budget_usd: float = 10.0
    context_budget_chars: int = 60_000
    minimum_verified_samples: int = 3
    minimum_verified_success_rate: float = 0.80
    reserve_quota_fraction: float = 0.15
    require_independent_review: bool = True
    shadow_routing: bool = False

    def __post_init__(self) -> None:
        if self.preset not in PRESETS:
            raise ValueError(f"unsupported orchestration preset: {self.preset}")
        if not 1 <= self.max_parallel_workers <= 16:
            raise ValueError("max_parallel_workers must be between 1 and 16")
        if self.soft_budget_usd < 0 or self.hard_budget_usd < self.soft_budget_usd:
            raise ValueError("orchestration budgets are invalid")
        if not 1000 <= self.context_budget_chars <= 2_000_000:
            raise ValueError("context_budget_chars must be between 1000 and 2000000")
        if self.minimum_verified_samples < 0:
            raise ValueError("minimum_verified_samples must be non-negative")
        if not 0 <= self.minimum_verified_success_rate <= 1:
            raise ValueError("minimum_verified_success_rate must be between 0 and 1")
        if not 0 <= self.reserve_quota_fraction <= 1:
            raise ValueError("reserve_quota_fraction must be between 0 and 1")

    @classmethod
    def from_preset(cls, name: str) -> "OrchestrationPolicy":
        if name == PRESET_MAX_QUALITY:
            return cls(
                preset=name,
                max_parallel_workers=4,
                soft_budget_usd=20.0,
                hard_budget_usd=50.0,
                context_budget_chars=100_000,
                minimum_verified_samples=3,
                minimum_verified_success_rate=0.90,
                reserve_quota_fraction=0.05,
                require_independent_review=True,
            )
        if name == PRESET_MAX_ECONOMY:
            return cls(
                preset=name,
                max_parallel_workers=4,
                soft_budget_usd=2.0,
                hard_budget_usd=5.0,
                context_budget_chars=40_000,
                minimum_verified_samples=3,
                minimum_verified_success_rate=0.75,
                reserve_quota_fraction=0.25,
                require_independent_review=True,
            )
        if name == PRESET_BALANCED:
            return cls(preset=name)
        if name == PRESET_CUSTOM:
            return cls(preset=name)
        raise ValueError(f"unknown orchestration preset: {name}")

    @property
    def prefer_already_paid(self) -> bool:
        return self.preset != PRESET_MAX_QUALITY


@dataclasses.dataclass(frozen=True)
class PlannedStep:
    step: RecipeStep
    endpoint: IntelligenceEndpoint
    route: RouteDecision
    effort_level: str = "medium"

    def __post_init__(self) -> None:
        # Store canonical KaroX Effort names in plans so API and subscription
        # adapters consume one vocabulary. AUTO is allowed and resolves when the
        # worker is constructed.
        normalize_effort(self.effort_level)

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step.to_dict(),
            "endpoint": self.endpoint.to_dict(),
            "route": self.route.to_dict(),
            "effort_level": self.effort_level,
        }


@dataclasses.dataclass(frozen=True)
class OrchestrationPlan:
    run_id: str
    task_id: str
    objective: str
    recipe_name: str
    orchestrator_endpoint: IntelligenceEndpoint
    risk_level: RiskLevel
    policy: OrchestrationPolicy
    steps: tuple[PlannedStep, ...]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "objective": self.objective,
            "recipe_name": self.recipe_name,
            "orchestrator_endpoint": self.orchestrator_endpoint.to_dict(),
            "risk_level": self.risk_level.value,
            "policy": dataclasses.asdict(self.policy),
            "steps": [item.to_dict() for item in self.steps],
            "created_at": self.created_at,
        }


@dataclasses.dataclass(frozen=True)
class WorkerExecutionRequest:
    run_id: str
    task_id: str
    step_id: str
    idempotency_key: str
    role: str
    task_class: str
    objective: str
    endpoint: IntelligenceEndpoint
    context_delta: ContextDelta
    upstream_messages: tuple[AgentMessage, ...]
    max_cost_usd: float
    effort_level: str = "medium"
    # Explicit worker root. The runtime fills this with the assigned KaroX
    # worktree (or leaves it None for the main repository). Executors must not
    # infer or widen this path from model output.
    workspace_path: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class WorkerExecutionResult:
    ok: bool
    summary: str
    accepted: bool
    verified: bool
    cost_usd: float = 0.0
    total_tokens: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cache_metrics_reported: bool = False
    cache_savings_usd: Optional[float] = None
    latency_ms: float = 0.0
    changeset_ref: Optional[str] = None
    evidence: tuple[EvidenceReference, ...] = ()
    findings: tuple[ReviewFinding, ...] = ()
    context_updates: tuple[dict[str, Any], ...] = ()


class WorkerExecutor(Protocol):
    def __call__(self, request: WorkerExecutionRequest) -> WorkerExecutionResult: ...


@dataclasses.dataclass
class _CacheUsageAccumulator:
    prompt_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    metrics_reported: bool = False
    estimated_savings_usd: float = 0.0
    savings_reported: bool = False

    def add(self, result: WorkerExecutionResult) -> None:
        if result.cache_metrics_reported:
            self.metrics_reported = True
            self.prompt_tokens += max(0, result.prompt_tokens)
            self.cache_read_tokens += max(0, result.cache_read_tokens)
            self.cache_write_tokens += max(0, result.cache_write_tokens)
        if result.cache_savings_usd is not None:
            self.savings_reported = True
            self.estimated_savings_usd += max(0.0, float(result.cache_savings_usd))

    @property
    def hit_rate(self) -> Optional[float]:
        if not self.metrics_reported or self.prompt_tokens <= 0:
            return None
        return min(1.0, max(0.0, self.cache_read_tokens / self.prompt_tokens))

    @property
    def savings_usd(self) -> Optional[float]:
        return round(self.estimated_savings_usd, 12) if self.savings_reported else None


@dataclasses.dataclass(frozen=True)
class StepResult:
    step_id: str
    endpoint_id: str
    status: str
    summary: str
    cost_usd: float
    total_tokens: int
    latency_ms: float
    accepted: bool
    verified: bool
    findings: tuple[ReviewFinding, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["findings"] = [item.to_dict() for item in self.findings]
        return value


@dataclasses.dataclass(frozen=True)
class OrchestrationResult:
    plan: OrchestrationPlan
    status: str
    steps: tuple[StepResult, ...]
    total_cost_usd: float
    total_tokens: int
    independent_review_satisfied: bool
    workspaces: dict[str, str] = dataclasses.field(default_factory=dict)
    savings_receipt: Optional[SavingsReceipt] = None
    stopped_reason: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan": self.plan.to_dict(),
            "status": self.status,
            "steps": [item.to_dict() for item in self.steps],
            "total_cost_usd": self.total_cost_usd,
            "total_tokens": self.total_tokens,
            "independent_review_satisfied": self.independent_review_satisfied,
            "workspaces": dict(sorted(self.workspaces.items())),
            "savings_receipt": (
                None if self.savings_receipt is None else self.savings_receipt.to_dict()
            ),
            "stopped_reason": self.stopped_reason,
        }


class Orchestrator:
    def __init__(
        self,
        *,
        pool: Optional[IntelligencePool] = None,
        router: Optional[VerifiedSmartRouter] = None,
        telemetry: Optional[RoutingTelemetry] = None,
        recipe_registry: Optional[RecipeRegistry] = None,
    ) -> None:
        self.pool = pool or IntelligencePool()
        self.telemetry = telemetry or RoutingTelemetry()
        self.router = router or VerifiedSmartRouter(pool=self.pool, telemetry=self.telemetry)
        self.recipe_registry = recipe_registry or RecipeRegistry()

    def _route(
        self,
        *,
        step: RecipeStep,
        risk_level: RiskLevel,
        policy: OrchestrationPolicy,
        preferred_endpoint_id: Optional[str],
        preferred_is_advisory: bool = False,
        excluded: Iterable[str] = (),
    ) -> RouteDecision:
        return self.router.decide(
            RouteRequest(
                task_class=step.task_class,
                role=step.role,
                required_capabilities=step.required_capabilities,
                risk_level=risk_level,
                preferred_endpoint_id=preferred_endpoint_id,
                preferred_is_advisory=preferred_is_advisory,
                exclude_endpoint_ids=tuple(excluded),
                minimum_verified_samples=policy.minimum_verified_samples,
                minimum_verified_success_rate=policy.minimum_verified_success_rate,
                reserve_quota_fraction=policy.reserve_quota_fraction,
                prefer_already_paid=policy.prefer_already_paid,
            )
        )

    @staticmethod
    def _effort_for(
        step: RecipeStep,
        *,
        policy: OrchestrationPolicy,
        overrides: Mapping[str, str],
    ) -> str:
        raw = (
            overrides.get(step.step_id)
            or overrides.get(step.role)
            or _PRESET_ROLE_EFFORT.get(policy.preset, {}).get(step.role)
            or "medium"
        )
        return normalize_effort(raw)

    def plan(
        self,
        *,
        objective: str,
        recipe_name: str = "feature",
        risk_level: RiskLevel = RiskLevel.MEDIUM,
        policy: Optional[OrchestrationPolicy] = None,
        orchestrator_endpoint_id: Optional[str] = None,
        role_assignments: Optional[Mapping[str, str]] = None,
        advisory_assignment_targets: Optional[Iterable[str]] = None,
        effort_assignments: Optional[Mapping[str, str]] = None,
        task_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> OrchestrationPlan:
        if not isinstance(objective, str) or not objective.strip():
            raise ValueError("orchestration objective must be non-empty")
        if run_id is not None:
            if (
                not isinstance(run_id, str)
                or not run_id
                or len(run_id) > 100
                or any(
                    char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                    for char in run_id
                )
            ):
                raise ValueError("orchestration run id is invalid")
        selected_recipe: OrchestrationRecipe = self.recipe_registry.get(recipe_name)
        policy = policy or OrchestrationPolicy.from_preset(PRESET_BALANCED)
        role_assignments = dict(role_assignments or {})
        advisory_targets = frozenset(advisory_assignment_targets or ())
        unknown_advisory_targets = sorted(advisory_targets - set(role_assignments))
        if unknown_advisory_targets:
            raise ValueError(
                "advisory assignment target(s) are missing from role_assignments: "
                + ", ".join(unknown_advisory_targets)
            )
        effort_assignments = dict(effort_assignments or {})
        for key, value in effort_assignments.items():
            if not isinstance(key, str) or not key.strip():
                raise ValueError("effort assignment keys must be non-empty role or step names")
            normalize_effort(value)
        steps = list(selected_recipe.steps)
        if risk_level.rank >= RiskLevel.HIGH.rank and not any(
            step.role == "security" and step.independent_from for step in steps
        ):
            implementers = tuple(
                step.step_id for step in steps if step.role == "implementer"
            )
            dependencies = tuple(
                step.step_id
                for step in steps
                if not step.optional and step.role in {"implementer", "tester", "reviewer"}
            )
            if not dependencies:
                dependencies = tuple(
                    step.step_id for step in steps if not step.optional
                )
            steps.append(
                RecipeStep(
                    step_id="security-review",
                    role="security",
                    task_class="security",
                    required_capabilities=("reasoning", "code"),
                    depends_on=dependencies,
                    independent_from=implementers,
                )
            )

        orchestrator_step = RecipeStep(
            step_id="__orchestrator__",
            role="orchestrator",
            task_class="general",
            required_capabilities=("reasoning",),
        )
        orchestrator_route = self._route(
            step=orchestrator_step,
            risk_level=risk_level,
            policy=policy,
            preferred_endpoint_id=orchestrator_endpoint_id or role_assignments.get("orchestrator"),
        )
        orchestrator_endpoint = orchestrator_route.endpoint

        # The chosen orchestrator is a real worker, not a decorative label.  It
        # owns planning when the recipe has a planner and performs the final
        # evidence review after the independent reviewer/tester chain. Routing
        # remains deterministic and policy-bound; the model cannot add tools or
        # silently invent workers outside the Intelligence Pool.
        if not any(step.role == "orchestrator" for step in steps):
            judge_dependencies = tuple(
                step.step_id for step in steps if not step.optional
            )
            steps.append(
                RecipeStep(
                    step_id="orchestrator-judge",
                    role="orchestrator",
                    task_class="general",
                    required_capabilities=("reasoning",),
                    depends_on=judge_dependencies,
                )
            )

        valid_effort_keys = {"orchestrator"}
        valid_effort_keys.update(step.role for step in steps)
        valid_effort_keys.update(step.step_id for step in steps)
        unknown_effort_keys = sorted(set(effort_assignments) - valid_effort_keys)
        if unknown_effort_keys:
            raise ValueError(
                "unknown effort assignment target(s): " + ", ".join(unknown_effort_keys)
            )

        planned: list[PlannedStep] = []
        by_step: dict[str, PlannedStep] = {}
        all_endpoints = self.pool.list(include_disabled=False)
        for step in steps:
            assignment_target: Optional[str] = None
            preferred = role_assignments.get(step.step_id)
            if preferred is not None:
                assignment_target = step.step_id
            else:
                preferred = role_assignments.get(step.role)
                if preferred is not None:
                    assignment_target = step.role
            preferred_is_advisory = assignment_target in advisory_targets
            if preferred is None and step.role in {"planner", "orchestrator"}:
                preferred = orchestrator_endpoint.endpoint_id
                preferred_is_advisory = False
            excluded: set[str] = set()
            if policy.require_independent_review and step.independent_from:
                for dependency in step.independent_from:
                    prior = by_step.get(dependency)
                    if prior is None:
                        continue
                    excluded.update(
                        independent_review_exclusions(
                            implementer_endpoint_id=prior.endpoint.endpoint_id,
                            implementer_provider_id=prior.endpoint.provider_id,
                            candidates=all_endpoints,
                        )
                    )
            try:
                route = self._route(
                    step=step,
                    risk_level=risk_level,
                    policy=policy,
                    preferred_endpoint_id=preferred,
                    preferred_is_advisory=preferred_is_advisory,
                    excluded=excluded,
                )
            except Exception:
                if step.optional:
                    continue
                raise
            item = PlannedStep(
                step=step,
                endpoint=route.endpoint,
                route=route,
                effort_level=self._effort_for(
                    step,
                    policy=policy,
                    overrides=effort_assignments,
                ),
            )
            planned.append(item)
            by_step[step.step_id] = item

        return OrchestrationPlan(
            run_id=run_id or f"run-{uuid.uuid4().hex}",
            task_id=task_id or f"task-{uuid.uuid4().hex}",
            objective=objective.strip(),
            recipe_name=selected_recipe.name,
            orchestrator_endpoint=orchestrator_endpoint,
            risk_level=risk_level,
            policy=policy,
            steps=tuple(planned),
            created_at=time.time(),
        )


class OrchestrationRuntime:
    """Execute an orchestration plan using explicitly registered adapters."""

    def __init__(
        self,
        plan: OrchestrationPlan,
        *,
        executors: Mapping[str, WorkerExecutor],
        context_bus: ContextBus,
        handoffs: Optional[HandoffStore] = None,
        telemetry: Optional[RoutingTelemetry] = None,
        mission_control: Optional[MissionControlStore] = None,
        journal: Optional[OrchestrationJournal] = None,
        measured_baseline_cost_usd: Optional[float] = None,
        initial_cost_usd: float = 0.0,
        initial_tokens: int = 0,
        worktree_pool: Optional[WorktreePool] = None,
        isolate_implementers: bool = False,
    ) -> None:
        self.plan = plan
        self.executors = dict(executors)
        self.context_bus = context_bus
        self.handoffs = handoffs or HandoffStore(plan.run_id)
        self.telemetry = telemetry or RoutingTelemetry()
        self.mission_control = mission_control or MissionControlStore(plan.run_id)
        self.journal = journal or OrchestrationJournal(plan.run_id)
        if measured_baseline_cost_usd is not None and measured_baseline_cost_usd < 0:
            raise ValueError("measured baseline cost must be non-negative")
        self.measured_baseline_cost_usd = measured_baseline_cost_usd
        if (
            isinstance(initial_cost_usd, bool)
            or not isinstance(initial_cost_usd, (int, float))
            or not math.isfinite(float(initial_cost_usd))
            or initial_cost_usd < 0
        ):
            raise ValueError("initial orchestration cost must be finite and non-negative")
        if (
            isinstance(initial_tokens, bool)
            or not isinstance(initial_tokens, int)
            or initial_tokens < 0
        ):
            raise ValueError("initial orchestration tokens must be a non-negative integer")
        self.initial_cost_usd = float(initial_cost_usd)
        self.initial_tokens = initial_tokens
        if not isinstance(isolate_implementers, bool):
            raise ValueError("isolate_implementers must be boolean")
        if isolate_implementers and worktree_pool is None:
            raise ValueError("isolate_implementers requires a WorktreePool")
        self.worktree_pool = worktree_pool
        self.isolate_implementers = isolate_implementers
        self._workspace_by_step: dict[str, Path] = {}
        self._known_context: dict[str, dict[str, str]] = defaultdict(dict)
        self._paused = False
        self._stop_requested = False
        self._governor = CostGovernor(
            soft_budget_usd=plan.policy.soft_budget_usd,
            hard_budget_usd=plan.policy.hard_budget_usd,
            shadow_mode=False,
        )
        self.journal.initialize(
            {
                item.step.step_id: (
                    item.endpoint.endpoint_id,
                    {
                        "task_id": plan.task_id,
                        "objective": plan.objective,
                        "recipe": plan.recipe_name,
                        "step": item.step.to_dict(),
                        "endpoint_id": item.endpoint.endpoint_id,
                        "effort_level": item.effort_level,
                    },
                )
                for item in plan.steps
            }
        )

    @staticmethod
    def _executor_reuses_context(executor: WorkerExecutor) -> bool:
        # Endpoint identity is not conversation identity. Every built-in worker
        # is stateless across DAG steps today; reuse is an explicit adapter
        # capability so ContextBus never fabricates remote/provider memory.
        return getattr(executor, "reuses_context_between_requests", False) is True

    def _context_delta_for(
        self, item: PlannedStep, executor: WorkerExecutor
    ) -> ContextDelta:
        known_hashes: Mapping[str, str] = {}
        if self._executor_reuses_context(executor):
            known_hashes = self._known_context[item.endpoint.endpoint_id]
        return self.context_bus.delta(
            role=item.step.role,
            known_hashes=known_hashes,
            budget_chars=self.plan.policy.context_budget_chars,
        )

    def _remember_delivered_context(
        self, item: PlannedStep, executor: WorkerExecutor, delta: ContextDelta
    ) -> None:
        if not self._executor_reuses_context(executor):
            return
        known = self._known_context[item.endpoint.endpoint_id]
        for item_id in delta.removed_ids:
            known.pop(item_id, None)
        for context_item in delta.changed:
            known[context_item.item_id] = context_item.content_hash

    def _topological_order(self) -> list[PlannedStep]:
        steps = {item.step.step_id: item for item in self.plan.steps}
        indegree = {key: 0 for key in steps}
        outgoing: dict[str, list[str]] = defaultdict(list)
        for item in steps.values():
            for dep in item.step.depends_on:
                if dep not in steps:
                    continue
                indegree[item.step.step_id] += 1
                outgoing[dep].append(item.step.step_id)
        ready = deque(sorted(key for key, degree in indegree.items() if degree == 0))
        order: list[PlannedStep] = []
        while ready:
            key = ready.popleft()
            order.append(steps[key])
            for child in sorted(outgoing.get(key, [])):
                indegree[child] -= 1
                if indegree[child] == 0:
                    ready.append(child)
        if len(order) != len(steps):
            raise OrchestrationError("orchestration plan contains a dependency cycle")
        return order

    def _upstream_messages(self, item: PlannedStep) -> tuple[AgentMessage, ...]:
        dependencies = set(item.step.depends_on)
        return tuple(
            message
            for message in self.handoffs.list(task_id=self.plan.task_id)
            if message.source_role and any(
                step.step.step_id in dependencies and step.endpoint.endpoint_id == message.source_endpoint_id
                for step in self.plan.steps
            )
        )

    def _prepare_isolated_worktrees(self) -> None:
        # Worktrees are intentionally created lazily in dependency order. Eager
        # creation from HEAD made a downstream implementer silently miss the
        # uncommitted changes produced by its upstream worker. Lazy composition
        # lets independent writers still start in parallel while dependent
        # writers fork the exact isolated state they are supposed to continue.
        return

    def _workspace_for_step(self, item: PlannedStep) -> Optional[Path]:
        own = self._workspace_by_step.get(item.step.step_id)
        if own is not None:
            return own
        if not self.isolate_implementers or self.worktree_pool is None:
            return None

        dependency_paths = {
            self._workspace_by_step[dependency]
            for dependency in item.step.depends_on
            if dependency in self._workspace_by_step
        }
        try:
            sources = tuple(
                self.worktree_pool.worktree_for_path(path)
                for path in sorted(dependency_paths, key=str)
            )
            if item.step.role == "implementer":
                # Every writer owns a distinct workspace. With one or more
                # upstream workspaces the source state is composed into a fresh
                # child so sibling implementers can safely fork the same dirty
                # lineage instead of racing in a shared directory.
                if sources:
                    worktree = self.worktree_pool.compose(
                        run_id=self.plan.run_id,
                        worker_id=item.step.step_id,
                        sources=sources,
                    )
                else:
                    worktree = self.worktree_pool.ensure(
                        run_id=self.plan.run_id,
                        worker_id=item.step.step_id,
                    )
                self._workspace_by_step[item.step.step_id] = worktree.path
                return worktree.path

            if not sources:
                return None
            if len(sources) == 1:
                workspace = sources[0].path
                self._workspace_by_step[item.step.step_id] = workspace
                return workspace

            # A tester/reviewer joining several isolated writer lineages gets a
            # deterministic read workspace. Disjoint changes and byte-identical
            # shared ancestry are composed; divergent edits to the same path fail
            # closed rather than being guessed into a merge.
            identity = "\0".join(sorted(str(source.path) for source in sources))
            join_id = (
                "join-"
                + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
            )
            worktree = self.worktree_pool.compose(
                run_id=self.plan.run_id,
                worker_id=join_id,
                sources=sources,
            )
            self._workspace_by_step[item.step.step_id] = worktree.path
            return worktree.path
        except (OSError, ValueError) as exc:
            raise OrchestrationError(
                f"isolated workspace preparation failed for {item.step.step_id}: {exc}"
            ) from exc
        except Exception as exc:
            from .worktree_pool import WorktreePoolError

            if isinstance(exc, WorktreePoolError):
                raise OrchestrationError(str(exc)) from exc
            raise

    def _publish_mission(
        self,
        *,
        statuses: Mapping[str, str],
        activities: Optional[Mapping[str, str]] = None,
        total_cost: float = 0.0,
        total_tokens: int = 0,
        context_reused_chars: int = 0,
        cache_hit_rate: Optional[float] = None,
        run_status: str = STATUS_RUNNING,
    ) -> None:
        activities = dict(activities or {})
        now = time.time()
        agents = tuple(
            AgentStatus(
                step_id=item.step.step_id,
                role=item.step.role,
                endpoint_id=item.endpoint.endpoint_id,
                status=statuses.get(item.step.step_id, STATUS_PENDING),
                activity=activities.get(item.step.step_id, ""),
                updated_at=now,
            )
            for item in self.plan.steps
        )
        self.mission_control.update_snapshot(
            MissionSnapshot(
                run_id=self.plan.run_id,
                task_id=self.plan.task_id,
                objective=self.plan.objective,
                recipe=self.plan.recipe_name,
                orchestrator_endpoint_id=self.plan.orchestrator_endpoint.endpoint_id,
                status=run_status,
                agents=agents,
                actual_cost_usd=round(total_cost, 12),
                total_tokens=total_tokens,
                context_reused_chars=context_reused_chars,
                cache_hit_rate=cache_hit_rate,
                updated_at=now,
            )
        )

    def _consume_mobile_commands(self) -> Optional[str]:
        """Apply safe control commands between worker executions.

        Pause is a real suspended state, not a failed run: the coordinator stays
        alive and continues to accept resume/steer/stop commands. Stop is graceful
        at this same boundary, so it never terminates a worker in the middle of a
        tool call. approve_request still never mints a RiskEngine token.
        """

        def consume_once() -> None:
            for command in self.mission_control.pending_commands():
                if command.command_type == "pause":
                    self._paused = True
                elif command.command_type == "resume":
                    self._paused = False
                elif command.command_type == "stop":
                    self._stop_requested = True
                    self._paused = False
                elif command.command_type == "steer":
                    self.context_bus.put_text(
                        item_id=f"mobile-{command.command_id}",
                        kind="task",
                        content=command.text,
                        tags=("mobile", "steer"),
                        priority=100,
                        stable=False,
                        source_ref="mission-control",
                    )
                elif command.command_type == "approve_request":
                    self.context_bus.put_text(
                        item_id=f"mobile-{command.command_id}",
                        kind="decision",
                        content=(
                            "The paired mobile user requested approval review. "
                            "This is not an approval token; use the normal KaroX Smart Stop flow."
                        ),
                        tags=("mobile", "approval-request"),
                        priority=100,
                        stable=False,
                        source_ref="mission-control",
                    )
                self.mission_control.consume(command.command_id)

        consume_once()
        if self._stop_requested:
            return "stopped_by_user"
        if not self._paused:
            return None

        snapshot = self.mission_control.snapshot()
        if snapshot is not None:
            self.mission_control.update_snapshot(
                dataclasses.replace(snapshot, status=STATUS_PAUSED, updated_at=time.time())
            )
        # Stay alive while paused so resume is meaningful. Only Mission Control
        # state is touched in this loop; no worker/tool execution can occur here.
        while self._paused and not self._stop_requested:
            time.sleep(0.25)
            consume_once()
        if self._stop_requested:
            return "stopped_by_user"
        snapshot = self.mission_control.snapshot()
        if snapshot is not None:
            self.mission_control.update_snapshot(
                dataclasses.replace(snapshot, status=STATUS_RUNNING, updated_at=time.time())
            )
        return None

    def _savings_receipt(
        self,
        *,
        total_cost: float,
        total_tokens: int,
        context_reused_chars: int,
        context_sent_chars: int,
        cache_usage: _CacheUsageAccumulator,
        accepted: bool,
        verified: bool,
        results: Iterable[StepResult],
    ) -> SavingsReceipt:
        rows = tuple(results)
        gates = tuple(
            f"{item.step_id}:{item.status}"
            for item in rows
        )
        cache_savings = (
            {}
            if cache_usage.savings_usd is None
            else {"USD": cache_usage.savings_usd}
        )
        usage = UsageSummary(
            requests=len(rows),
            prompt_tokens=max(0, cache_usage.prompt_tokens),
            total_tokens=max(0, total_tokens),
            cache_read_tokens=max(0, cache_usage.cache_read_tokens),
            cache_write_tokens=max(0, cache_usage.cache_write_tokens),
            costs={"USD": max(0.0, total_cost)},
            cache_savings=cache_savings,
        )
        return build_savings_receipt(
            usage=usage,
            counters=EconomyCounters(
                context_chars_reused=max(0, context_reused_chars),
                context_chars_sent=max(0, context_sent_chars),
            ),
            measured_baseline_cost=self.measured_baseline_cost_usd,
            baseline_currency="USD",
            accepted=accepted,
            verified=verified,
            quality_gates=gates,
        )

    def _run_serial(self) -> OrchestrationResult:
        results: list[StepResult] = []
        statuses: dict[str, str] = {}
        total_cost = self.initial_cost_usd
        total_tokens = self.initial_tokens
        context_reused_chars = 0
        context_sent_chars = 0
        cache_usage = _CacheUsageAccumulator()
        stopped_reason: Optional[str] = None
        activities: dict[str, str] = {}

        recovery = self.journal.reconcile_after_restart()
        if recovery.reconcile_required:
            stopped_reason = "reconcile_required:" + ",".join(recovery.reconcile_required)
            self._publish_mission(
                statuses={item.step_id: item.status for item in recovery.steps},
                activities={step_id: "External work must be reconciled before retry" for step_id in recovery.reconcile_required},
                total_cost=total_cost,
                total_tokens=total_tokens,
                run_status=STATUS_BLOCKED,
            )
            return OrchestrationResult(
                plan=self.plan,
                status=STATUS_FAILED,
                steps=(),
                total_cost_usd=round(total_cost, 12),
                total_tokens=total_tokens,
                independent_review_satisfied=False,
                workspaces={key: str(value) for key, value in self._workspace_by_step.items()},
                stopped_reason=stopped_reason,
            )
        self._prepare_isolated_worktrees()
        journal_status = {item.step_id: item.status for item in recovery.steps}
        for step_id, state in journal_status.items():
            if state == JOURNAL_PASSED:
                statuses[step_id] = STATUS_PASSED
            elif state == JOURNAL_FAILED:
                statuses[step_id] = STATUS_FAILED
        self._publish_mission(
            statuses=statuses,
            activities=activities,
            total_cost=total_cost,
            total_tokens=total_tokens,
        )

        for item in self._topological_order():
            if statuses.get(item.step.step_id) == STATUS_PASSED:
                # A completed mutation is never replayed after restart.
                continue
            mobile_stop = self._consume_mobile_commands()
            if mobile_stop is not None:
                stopped_reason = mobile_stop
                break
            if any(statuses.get(dep) != STATUS_PASSED for dep in item.step.depends_on if dep in statuses):
                statuses[item.step.step_id] = STATUS_BLOCKED
                results.append(
                    StepResult(
                        step_id=item.step.step_id,
                        endpoint_id=item.endpoint.endpoint_id,
                        status=STATUS_BLOCKED,
                        summary="blocked by failed dependency",
                        cost_usd=0.0,
                        total_tokens=0,
                        latency_ms=0.0,
                        accepted=False,
                        verified=False,
                    )
                )
                continue

            budget = self._governor.evaluate(current_cost_usd=total_cost)
            if not budget.allowed:
                stopped_reason = budget.reason
                statuses[item.step.step_id] = STATUS_BLOCKED
                break

            try:
                workspace = self._workspace_for_step(item)
            except OrchestrationError as exc:
                stopped_reason = str(exc)
                statuses[item.step.step_id] = STATUS_BLOCKED
                activities[item.step.step_id] = stopped_reason
                results.append(
                    StepResult(
                        step_id=item.step.step_id,
                        endpoint_id=item.endpoint.endpoint_id,
                        status=STATUS_BLOCKED,
                        summary=stopped_reason,
                        cost_usd=0.0,
                        total_tokens=0,
                        latency_ms=0.0,
                        accepted=False,
                        verified=False,
                    )
                )
                break

            executor = self.executors.get(item.endpoint.endpoint_id)
            if executor is None:
                raise OrchestrationError(
                    f"no guarded executor registered for endpoint {item.endpoint.endpoint_id}"
                )

            delta = self._context_delta_for(item, executor)
            request_message = AgentMessage.create(
                message_type=MESSAGE_REQUEST,
                task_id=self.plan.task_id,
                source_endpoint_id=self.plan.orchestrator_endpoint.endpoint_id,
                target_endpoint_id=item.endpoint.endpoint_id,
                source_role="orchestrator",
                target_role=item.step.role,
                summary=f"Execute {item.step.step_id}: {self.plan.objective}",
            )
            self.handoffs.append(request_message)
            journal_step = self.journal.mark_started(item.step.step_id)
            statuses[item.step.step_id] = STATUS_RUNNING
            activities[item.step.step_id] = f"Running {item.step.task_class}"
            context_reused_chars += delta.reused_chars
            context_sent_chars += delta.sent_chars
            self._publish_mission(
                statuses=statuses,
                activities=activities,
                total_cost=total_cost,
                total_tokens=total_tokens,
                context_reused_chars=context_reused_chars,
            )
            execution_request = WorkerExecutionRequest(
                run_id=self.plan.run_id,
                task_id=self.plan.task_id,
                step_id=item.step.step_id,
                idempotency_key=journal_step.idempotency_key,
                role=item.step.role,
                task_class=item.step.task_class,
                objective=self.plan.objective,
                endpoint=item.endpoint,
                context_delta=delta,
                upstream_messages=self._upstream_messages(item),
                max_cost_usd=max(0.0, self.plan.policy.hard_budget_usd - total_cost),
                effort_level=item.effort_level,
                workspace_path=None if workspace is None else str(workspace),
            )

            started = time.monotonic()
            worker_result = executor(execution_request)
            elapsed_ms = max(0.0, (time.monotonic() - started) * 1000.0)
            latency_ms = worker_result.latency_ms if worker_result.latency_ms > 0 else elapsed_ms
            total_cost += max(0.0, worker_result.cost_usd)
            total_tokens += max(0, worker_result.total_tokens)
            cache_usage.add(worker_result)
            success = worker_result.ok and worker_result.accepted and worker_result.verified
            status = STATUS_PASSED if success else STATUS_FAILED
            statuses[item.step.step_id] = status
            activities[item.step.step_id] = worker_result.summary[:500]
            self.journal.mark_finished(
                item.step.step_id,
                passed=success,
                summary=worker_result.summary,
            )

            self.telemetry.observe(
                endpoint_id=item.endpoint.endpoint_id,
                task_class=item.step.task_class,
                accepted=worker_result.accepted,
                verified=worker_result.verified,
                latency_ms=latency_ms,
                cost_usd=max(0.0, worker_result.cost_usd),
                total_tokens=max(0, worker_result.total_tokens),
            )
            for update in worker_result.context_updates:
                if not isinstance(update, Mapping):
                    continue
                required = {"item_id", "kind", "content"}
                if not required.issubset(update):
                    continue
                self.context_bus.put_text(
                    item_id=str(update["item_id"]),
                    kind=str(update["kind"]),
                    content=str(update["content"]),
                    tags=tuple(update.get("tags") or ()),
                    priority=int(update.get("priority", 50)),
                    stable=bool(update.get("stable", False)),
                    source_ref=update.get("source_ref"),
                )
            self._remember_delivered_context(item, executor, delta)

            is_review = item.step.role in {"reviewer", "security"} and bool(item.step.independent_from)
            message_type = MESSAGE_RESULT
            if is_review:
                message_type = MESSAGE_REVIEW_PASS if success and not worker_result.findings else MESSAGE_REVIEW_FAIL
            if not success and item.step.role in {"reviewer", "security"}:
                message_type = MESSAGE_REVIEW_FAIL
            result_message = AgentMessage.create(
                message_type=message_type,
                task_id=self.plan.task_id,
                source_endpoint_id=item.endpoint.endpoint_id,
                target_endpoint_id=self.plan.orchestrator_endpoint.endpoint_id,
                source_role=item.step.role,
                target_role="orchestrator",
                summary=worker_result.summary,
                changeset_ref=worker_result.changeset_ref,
                evidence=worker_result.evidence,
                findings=worker_result.findings,
            )
            self.handoffs.append(result_message)
            results.append(
                StepResult(
                    step_id=item.step.step_id,
                    endpoint_id=item.endpoint.endpoint_id,
                    status=status,
                    summary=worker_result.summary,
                    cost_usd=max(0.0, worker_result.cost_usd),
                    total_tokens=max(0, worker_result.total_tokens),
                    latency_ms=latency_ms,
                    accepted=worker_result.accepted,
                    verified=worker_result.verified,
                    findings=worker_result.findings,
                )
            )
            self._publish_mission(
                statuses=statuses,
                activities=activities,
                total_cost=total_cost,
                total_tokens=total_tokens,
                context_reused_chars=context_reused_chars,
                cache_hit_rate=cache_usage.hit_rate,
            )
            if status == STATUS_FAILED:
                stopped_reason = f"step failed: {item.step.step_id}"
                self.handoffs.append(
                    AgentMessage.create(
                        message_type=MESSAGE_ESCALATE,
                        task_id=self.plan.task_id,
                        source_endpoint_id=item.endpoint.endpoint_id,
                        target_endpoint_id=self.plan.orchestrator_endpoint.endpoint_id,
                        source_role=item.step.role,
                        target_role="orchestrator",
                        summary=stopped_reason,
                        findings=worker_result.findings,
                    )
                )
                break

        review_steps = [
            item for item in self.plan.steps if item.step.independent_from and item.step.role in {"reviewer", "security"}
        ]
        independent_review_satisfied = all(statuses.get(item.step.step_id) == STATUS_PASSED for item in review_steps)
        completed = len(statuses) == len(self.plan.steps) and all(
            status in {STATUS_PASSED, STATUS_SKIPPED} for status in statuses.values()
        )
        if stopped_reason == "stopped_by_user":
            final_status = STATUS_STOPPED
        else:
            final_status = STATUS_PASSED if completed and (
                independent_review_satisfied or not self.plan.policy.require_independent_review
            ) else STATUS_FAILED
        self._publish_mission(
            statuses=statuses,
            activities=activities,
            total_cost=total_cost,
            total_tokens=total_tokens,
            context_reused_chars=context_reused_chars,
            cache_hit_rate=cache_usage.hit_rate,
            run_status=final_status,
        )
        receipt = self._savings_receipt(
            total_cost=total_cost,
            total_tokens=total_tokens,
            context_reused_chars=context_reused_chars,
            context_sent_chars=context_sent_chars,
            cache_usage=cache_usage,
            accepted=completed,
            verified=final_status == STATUS_PASSED,
            results=results,
        )
        return OrchestrationResult(
            plan=self.plan,
            status=final_status,
            steps=tuple(results),
            total_cost_usd=round(total_cost, 12),
            total_tokens=total_tokens,
            independent_review_satisfied=independent_review_satisfied,
            workspaces={key: str(value) for key, value in self._workspace_by_step.items()},
            savings_receipt=receipt,
            stopped_reason=stopped_reason,
        )

    def _run_parallel(self) -> OrchestrationResult:
        """Execute dependency-ready workers in bounded parallel waves.

        The shared control-plane state (journal, handoffs, context bus, routing
        telemetry and Mission Control) is mutated only on this coordinator
        thread. Worker threads receive immutable requests and execute only their
        registered adapter. This keeps file-backed stores deterministic while
        still allowing independent model/CLI work to overlap.

        Repository writers are parallel only when implementer isolation is on;
        otherwise an implementation step occupies a wave by itself. With
        isolation enabled every implementer has a detached KaroX worktree, so
        concurrent writes cannot race in the user's primary checkout.
        """
        results: list[StepResult] = []
        statuses: dict[str, str] = {}
        total_cost = self.initial_cost_usd
        total_tokens = self.initial_tokens
        context_reused_chars = 0
        context_sent_chars = 0
        cache_usage = _CacheUsageAccumulator()
        stopped_reason: Optional[str] = None
        activities: dict[str, str] = {}

        recovery = self.journal.reconcile_after_restart()
        if recovery.reconcile_required:
            stopped_reason = "reconcile_required:" + ",".join(recovery.reconcile_required)
            self._publish_mission(
                statuses={item.step_id: item.status for item in recovery.steps},
                activities={
                    step_id: "External work must be reconciled before retry"
                    for step_id in recovery.reconcile_required
                },
                total_cost=total_cost,
                total_tokens=total_tokens,
                run_status=STATUS_BLOCKED,
            )
            return OrchestrationResult(
                plan=self.plan,
                status=STATUS_FAILED,
                steps=(),
                total_cost_usd=round(total_cost, 12),
                total_tokens=total_tokens,
                independent_review_satisfied=False,
                workspaces={key: str(value) for key, value in self._workspace_by_step.items()},
                stopped_reason=stopped_reason,
            )

        self._prepare_isolated_worktrees()
        for entry in recovery.steps:
            if entry.status == JOURNAL_PASSED:
                statuses[entry.step_id] = STATUS_PASSED
            elif entry.status == JOURNAL_FAILED:
                # A recorded failed attempt is retryable. A running attempt is
                # handled above by reconciliation and is never blindly replayed.
                statuses[entry.step_id] = STATUS_PENDING

        order = self._topological_order()
        pending = {
            item.step.step_id
            for item in order
            if statuses.get(item.step.step_id) != STATUS_PASSED
        }
        self._publish_mission(
            statuses=statuses,
            activities=activities,
            total_cost=total_cost,
            total_tokens=total_tokens,
        )

        def add_blocked(item: PlannedStep, summary: str) -> None:
            statuses[item.step.step_id] = STATUS_BLOCKED
            activities[item.step.step_id] = summary
            pending.discard(item.step.step_id)
            results.append(
                StepResult(
                    step_id=item.step.step_id,
                    endpoint_id=item.endpoint.endpoint_id,
                    status=STATUS_BLOCKED,
                    summary=summary,
                    cost_usd=0.0,
                    total_tokens=0,
                    latency_ms=0.0,
                    accepted=False,
                    verified=False,
                )
            )

        while pending:
            mobile_stop = self._consume_mobile_commands()
            if mobile_stop is not None:
                stopped_reason = mobile_stop
                break

            # A dependency that terminated unsuccessfully can never become
            # runnable in this attempt. Mark the transitive block explicitly so
            # Mission Control does not leave phantom "pending" agents behind.
            changed = True
            while changed:
                changed = False
                for item in order:
                    step_id = item.step.step_id
                    if step_id not in pending:
                        continue
                    dependency_states = [
                        statuses.get(dep) for dep in item.step.depends_on
                    ]
                    if any(
                        state in {STATUS_FAILED, STATUS_BLOCKED, STATUS_SKIPPED}
                        for state in dependency_states
                    ):
                        add_blocked(item, "blocked by failed dependency")
                        changed = True

            if not pending:
                break

            ready = [
                item
                for item in order
                if item.step.step_id in pending
                and all(
                    statuses.get(dep) == STATUS_PASSED
                    for dep in item.step.depends_on
                )
            ]
            if not ready:
                stopped_reason = "no runnable orchestration steps remain"
                for item in order:
                    if item.step.step_id in pending:
                        add_blocked(item, stopped_reason)
                break

            budget = self._governor.evaluate(current_cost_usd=total_cost)
            if not budget.allowed:
                stopped_reason = budget.reason
                for item in ready:
                    add_blocked(item, stopped_reason)
                break

            # Without worktree isolation, never overlap a writer with any other
            # worker. Read/review roles can still fill the wave.
            if not self.isolate_implementers and any(
                item.step.role == "implementer" for item in ready
            ):
                wave = [next(item for item in ready if item.step.role == "implementer")]
            else:
                wave = ready[: self.plan.policy.max_parallel_workers]

            resolved: list[tuple[PlannedStep, WorkerExecutor, Optional[Path]]] = []
            for item in wave:
                try:
                    workspace = self._workspace_for_step(item)
                except OrchestrationError as exc:
                    stopped_reason = str(exc)
                    add_blocked(item, stopped_reason)
                    break
                worker_executor = self.executors.get(item.endpoint.endpoint_id)
                if worker_executor is None:
                    raise OrchestrationError(
                        f"no guarded executor registered for endpoint {item.endpoint.endpoint_id}"
                    )
                resolved.append((item, worker_executor, workspace))
            if stopped_reason is not None:
                break

            remaining_budget = max(
                0.0, self.plan.policy.hard_budget_usd - total_cost
            )
            per_worker_budget = (
                remaining_budget / len(resolved) if resolved else 0.0
            )
            prepared: list[
                tuple[PlannedStep, WorkerExecutor, WorkerExecutionRequest]
            ] = []
            for item, worker_executor, workspace in resolved:
                delta = self._context_delta_for(item, worker_executor)
                request_message = AgentMessage.create(
                    message_type=MESSAGE_REQUEST,
                    task_id=self.plan.task_id,
                    source_endpoint_id=self.plan.orchestrator_endpoint.endpoint_id,
                    target_endpoint_id=item.endpoint.endpoint_id,
                    source_role="orchestrator",
                    target_role=item.step.role,
                    summary=f"Execute {item.step.step_id}: {self.plan.objective}",
                )
                self.handoffs.append(request_message)
                journal_step = self.journal.mark_started(item.step.step_id)
                statuses[item.step.step_id] = STATUS_RUNNING
                activities[item.step.step_id] = f"Running {item.step.task_class}"
                context_reused_chars += delta.reused_chars
                context_sent_chars += delta.sent_chars
                prepared.append(
                    (
                        item,
                        worker_executor,
                        WorkerExecutionRequest(
                            run_id=self.plan.run_id,
                            task_id=self.plan.task_id,
                            step_id=item.step.step_id,
                            idempotency_key=journal_step.idempotency_key,
                            role=item.step.role,
                            task_class=item.step.task_class,
                            objective=self.plan.objective,
                            endpoint=item.endpoint,
                            context_delta=delta,
                            upstream_messages=self._upstream_messages(item),
                            max_cost_usd=per_worker_budget,
                            effort_level=item.effort_level,
                            workspace_path=(
                                None if workspace is None else str(workspace)
                            ),
                        ),
                    )
                )

            self._publish_mission(
                statuses=statuses,
                activities=activities,
                total_cost=total_cost,
                total_tokens=total_tokens,
                context_reused_chars=context_reused_chars,
            )

            def invoke(
                entry: tuple[PlannedStep, WorkerExecutor, WorkerExecutionRequest],
            ) -> tuple[PlannedStep, WorkerExecutionResult, float, ContextDelta]:
                item, worker_executor, request = entry
                started = time.monotonic()
                try:
                    worker_result = worker_executor(request)
                except Exception as exc:
                    # Adapter exceptions are converted to a typed failed result;
                    # their message may contain remote/provider data, so only the
                    # exception class crosses into user-facing orchestration state.
                    worker_result = WorkerExecutionResult(
                        ok=False,
                        summary=f"worker adapter failed: {type(exc).__name__}",
                        accepted=False,
                        verified=False,
                    )
                elapsed_ms = max(0.0, (time.monotonic() - started) * 1000.0)
                latency_ms = (
                    worker_result.latency_ms
                    if worker_result.latency_ms > 0
                    else elapsed_ms
                )
                return item, worker_result, latency_ms, request.context_delta

            if len(prepared) == 1:
                outcomes = [invoke(prepared[0])]
            else:
                with ThreadPoolExecutor(
                    max_workers=min(
                        self.plan.policy.max_parallel_workers, len(prepared)
                    ),
                    thread_name_prefix="karox-worker",
                ) as pool:
                    futures = [pool.submit(invoke, entry) for entry in prepared]
                    # Apply in plan order even if completion order differs. The
                    # parallelism is in execution, not in durable-state ordering.
                    outcomes = [future.result() for future in futures]

            wave_failed = False
            for item, worker_result, latency_ms, delivered_delta in outcomes:
                step_id = item.step.step_id
                total_cost += max(0.0, worker_result.cost_usd)
                total_tokens += max(0, worker_result.total_tokens)
                cache_usage.add(worker_result)
                success = (
                    worker_result.ok
                    and worker_result.accepted
                    and worker_result.verified
                )
                status = STATUS_PASSED if success else STATUS_FAILED
                statuses[step_id] = status
                pending.discard(step_id)
                activities[step_id] = worker_result.summary[:500]
                self.journal.mark_finished(
                    step_id,
                    passed=success,
                    summary=worker_result.summary,
                )
                self.telemetry.observe(
                    endpoint_id=item.endpoint.endpoint_id,
                    task_class=item.step.task_class,
                    accepted=worker_result.accepted,
                    verified=worker_result.verified,
                    latency_ms=latency_ms,
                    cost_usd=max(0.0, worker_result.cost_usd),
                    total_tokens=max(0, worker_result.total_tokens),
                )
                for update in worker_result.context_updates:
                    if not isinstance(update, Mapping):
                        continue
                    required = {"item_id", "kind", "content"}
                    if not required.issubset(update):
                        continue
                    self.context_bus.put_text(
                        item_id=str(update["item_id"]),
                        kind=str(update["kind"]),
                        content=str(update["content"]),
                        tags=tuple(update.get("tags") or ()),
                        priority=int(update.get("priority", 50)),
                        stable=bool(update.get("stable", False)),
                        source_ref=update.get("source_ref"),
                    )
                self._remember_delivered_context(
                    item, self.executors[item.endpoint.endpoint_id], delivered_delta
                )

                is_review = (
                    item.step.role in {"reviewer", "security"}
                    and bool(item.step.independent_from)
                )
                message_type = MESSAGE_RESULT
                if is_review:
                    message_type = (
                        MESSAGE_REVIEW_PASS
                        if success and not worker_result.findings
                        else MESSAGE_REVIEW_FAIL
                    )
                if not success and item.step.role in {"reviewer", "security"}:
                    message_type = MESSAGE_REVIEW_FAIL
                self.handoffs.append(
                    AgentMessage.create(
                        message_type=message_type,
                        task_id=self.plan.task_id,
                        source_endpoint_id=item.endpoint.endpoint_id,
                        target_endpoint_id=self.plan.orchestrator_endpoint.endpoint_id,
                        source_role=item.step.role,
                        target_role="orchestrator",
                        summary=worker_result.summary,
                        changeset_ref=worker_result.changeset_ref,
                        evidence=worker_result.evidence,
                        findings=worker_result.findings,
                    )
                )
                results.append(
                    StepResult(
                        step_id=step_id,
                        endpoint_id=item.endpoint.endpoint_id,
                        status=status,
                        summary=worker_result.summary,
                        cost_usd=max(0.0, worker_result.cost_usd),
                        total_tokens=max(0, worker_result.total_tokens),
                        latency_ms=latency_ms,
                        accepted=worker_result.accepted,
                        verified=worker_result.verified,
                        findings=worker_result.findings,
                    )
                )
                if status == STATUS_FAILED:
                    wave_failed = True
                    if stopped_reason is None:
                        stopped_reason = f"step failed: {step_id}"
                    self.handoffs.append(
                        AgentMessage.create(
                            message_type=MESSAGE_ESCALATE,
                            task_id=self.plan.task_id,
                            source_endpoint_id=item.endpoint.endpoint_id,
                            target_endpoint_id=self.plan.orchestrator_endpoint.endpoint_id,
                            source_role=item.step.role,
                            target_role="orchestrator",
                            summary=f"step failed: {step_id}",
                            findings=worker_result.findings,
                        )
                    )

            self._publish_mission(
                statuses=statuses,
                activities=activities,
                total_cost=total_cost,
                total_tokens=total_tokens,
                context_reused_chars=context_reused_chars,
                cache_hit_rate=cache_usage.hit_rate,
            )
            if wave_failed:
                break

        # Make dependency fallout explicit after fail-fast termination.
        if stopped_reason is not None:
            changed = True
            while changed:
                changed = False
                for item in order:
                    if item.step.step_id not in pending:
                        continue
                    if any(
                        statuses.get(dep) in {STATUS_FAILED, STATUS_BLOCKED}
                        for dep in item.step.depends_on
                    ):
                        add_blocked(item, "blocked by failed dependency")
                        changed = True

        review_steps = [
            item
            for item in self.plan.steps
            if item.step.independent_from
            and item.step.role in {"reviewer", "security"}
        ]
        independent_review_satisfied = all(
            statuses.get(item.step.step_id) == STATUS_PASSED
            for item in review_steps
        )
        completed = len(statuses) == len(self.plan.steps) and all(
            status in {STATUS_PASSED, STATUS_SKIPPED}
            for status in statuses.values()
        )
        if stopped_reason == "stopped_by_user":
            final_status = STATUS_STOPPED
        else:
            final_status = (
                STATUS_PASSED
                if completed
                and (
                    independent_review_satisfied
                    or not self.plan.policy.require_independent_review
                )
                else STATUS_FAILED
            )
        self._publish_mission(
            statuses=statuses,
            activities=activities,
            total_cost=total_cost,
            total_tokens=total_tokens,
            context_reused_chars=context_reused_chars,
            cache_hit_rate=cache_usage.hit_rate,
            run_status=final_status,
        )
        receipt = self._savings_receipt(
            total_cost=total_cost,
            total_tokens=total_tokens,
            context_reused_chars=context_reused_chars,
            context_sent_chars=context_sent_chars,
            cache_usage=cache_usage,
            accepted=completed,
            verified=final_status == STATUS_PASSED,
            results=results,
        )
        return OrchestrationResult(
            plan=self.plan,
            status=final_status,
            steps=tuple(results),
            total_cost_usd=round(total_cost, 12),
            total_tokens=total_tokens,
            independent_review_satisfied=independent_review_satisfied,
            workspaces={key: str(value) for key, value in self._workspace_by_step.items()},
            savings_receipt=receipt,
            stopped_reason=stopped_reason,
        )

    def run(self) -> OrchestrationResult:
        if self.plan.policy.max_parallel_workers <= 1:
            return self._run_serial()
        return self._run_parallel()


__all__ = [
    "OrchestrationError",
    "OrchestrationPlan",
    "OrchestrationPolicy",
    "OrchestrationResult",
    "OrchestrationRuntime",
    "Orchestrator",
    "PRESET_BALANCED",
    "PRESET_CUSTOM",
    "PRESET_MAX_ECONOMY",
    "PRESET_MAX_QUALITY",
    "PRESETS",
    "PlannedStep",
    "StepResult",
    "WorkerExecutionRequest",
    "WorkerExecutionResult",
    "WorkerExecutor",
]
