"""CLI surface for KaroX 5 intelligence, orchestration and Mission Control."""

from __future__ import annotations

import argparse
import dataclasses
import json
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional

from .context_bus import ContextBus
from .economy_engine import EconomyCounters, build_savings_receipt, shadow_route
from .effort import normalize_effort
from .intelligence_pool import (
    CAPABILITIES,
    ROLE_KINDS,
    SOURCE_API,
    SOURCE_EXTERNAL,
    SOURCE_KINDS,
    SOURCE_LOCAL,
    SOURCE_SUBSCRIPTION,
    IntelligenceEndpoint,
    IntelligencePool,
)
from .mission_control import COMMAND_TYPES, MissionControlStore
from .mission_control_server import MissionControlServer
from .orchestration_native import NativeApiWorkerFactory
from .orchestration_planning import PlanSelection, build_orchestration_plan
from .orchestration_presenter import (
    render_intelligence_list,
    render_mission,
    render_plan,
    render_run,
    render_started,
)
from .orchestration_recovery import OrchestrationJournal
from .orchestration_routing import (
    TASK_CLASSES,
    RouteRequest,
    RoutingTelemetry,
    VerifiedSmartRouter,
)
from .orchestrator import PRESETS, OrchestrationRuntime
from .project_context import discover_project_context
from .quota_brain import QuotaBrain
from .recipe_registry import RecipeRegistry
from .risk_engine import RiskLevel
from .shadow_economy import ShadowEconomyLedger
from .subscription_cli import (
    SubscriptionCliExecutor,
    discover_subscription_clis,
    register_discovered_subscription_endpoints,
)
from .usage_analytics import UsageSummary
from .verification import discover_verification_commands
from .worker_adapters import NativeAgentExecutor
from .worktree_pool import WorktreePool


def _emit(value: Any, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False))
        return
    if isinstance(value, list):
        for item in value:
            print(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, sort_keys=True))
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                print(f"{key}: {json.dumps(item, ensure_ascii=False, sort_keys=True)}")
            else:
                print(f"{key}: {item}")
    else:
        print(value)


def _roles(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    for raw in values:
        for value in raw.split(","):
            value = value.strip()
            if not value:
                continue
            if value not in ROLE_KINDS:
                raise ValueError(f"unsupported role: {value}")
            if value not in result:
                result.append(value)
    return tuple(result)


def _capabilities(values: list[str]) -> tuple[str, ...]:
    result: list[str] = []
    for raw in values:
        for value in raw.split(","):
            value = value.strip()
            if not value:
                continue
            if value not in CAPABILITIES:
                raise ValueError(f"unsupported capability: {value}")
            if value not in result:
                result.append(value)
    return tuple(result)


def _assignments(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise ValueError("--assign must be ROLE_OR_STEP=ENDPOINT_ID")
        name, endpoint = raw.split("=", 1)
        name = name.strip()
        endpoint = endpoint.strip()
        if not name or not endpoint:
            raise ValueError("--assign must be ROLE_OR_STEP=ENDPOINT_ID")
        result[name] = endpoint
    return result


def _effort_assignments(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw in values:
        if "=" not in raw:
            raise ValueError("--worker-effort must be ROLE_OR_STEP=EFFORT")
        name, effort = raw.split("=", 1)
        name = name.strip()
        effort = effort.strip()
        if not name or not effort:
            raise ValueError("--worker-effort must be ROLE_OR_STEP=EFFORT")
        result[name] = normalize_effort(effort)
    return result


def _verification_commands(values: list[str]) -> tuple[tuple[str, ...], ...]:
    commands: list[tuple[str, ...]] = []
    for raw in values:
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"--verification-command must be a JSON array: {exc.msg}") from exc
        if not isinstance(decoded, list) or not decoded or not all(isinstance(item, str) and item for item in decoded):
            raise ValueError("--verification-command must be a non-empty JSON array of strings")
        commands.append(tuple(decoded))
    return tuple(commands)


def _verification_commands_for_args(
    args: argparse.Namespace,
) -> tuple[tuple[str, ...], ...]:
    """Return explicit verification argv or KaroX's bounded safe defaults."""

    explicit = _verification_commands(list(getattr(args, "verification_command", ()) or ()))
    if explicit:
        return explicit
    return discover_verification_commands(Path(args.repository))


def _usage_summary(path: Path) -> UsageSummary:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read usage JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("usage JSON must be an object")
    fields = {field.name for field in dataclasses.fields(UsageSummary)}
    payload = {key: value[key] for key in fields if key in value}
    return UsageSummary(**payload)


def register_orchestration_commands(commands: Any) -> None:
    intelligence = commands.add_parser(
        "intelligence", help="manage the unified API/subscription/local agent pool"
    )
    intel = intelligence.add_subparsers(dest="intelligence_command", required=True)
    for name in ("list", "show"):
        parser = intel.add_parser(name)
        if name == "show":
            parser.add_argument("endpoint_id")
        else:
            parser.add_argument(
                "--details",
                action="store_true",
                help="show every endpoint plus internal ids, roles, and capabilities",
            )
        parser.add_argument("--json", action="store_true")
    add = intel.add_parser("add", help="register a non-API intelligence endpoint")
    add.add_argument("endpoint_id")
    add.add_argument("--name", required=True)
    add.add_argument(
        "--source",
        choices=sorted({SOURCE_SUBSCRIPTION, SOURCE_LOCAL, SOURCE_EXTERNAL}),
        required=True,
    )
    add.add_argument("--target-id", required=True)
    add.add_argument("--model", help="optional model/alias passed to a guarded subscription adapter")
    add.add_argument("--role", action="append", default=[])
    add.add_argument("--capability", action="append", default=[])
    add.add_argument("--already-paid", action=argparse.BooleanOptionalAction, default=True)
    add.add_argument("--json", action="store_true")
    discover_agents = intel.add_parser(
        "discover-agents",
        help="discover installed subscription CLIs; optionally add the guarded built-in adapters to the pool",
    )
    discover_agents.add_argument("--apply", action="store_true")
    discover_agents.add_argument(
        "--include-manual-adapter-targets",
        action="store_true",
        help="also register installed CLIs that still require an application-supplied guarded adapter",
    )
    discover_agents.add_argument("--json", action="store_true")
    remove = intel.add_parser("remove")
    remove.add_argument("endpoint_id")
    remove.add_argument("--json", action="store_true")
    for name in ("enable", "disable"):
        parser = intel.add_parser(name)
        parser.add_argument("endpoint_id")
        parser.add_argument("--json", action="store_true")
    quota = intel.add_parser("quota", help="record measured/known quota telemetry")
    quota.add_argument("endpoint_id")
    quota.add_argument("--remaining-fraction", type=float)
    quota.add_argument("--remaining-units", type=float)
    quota.add_argument("--unit")
    quota.add_argument("--resets-at", type=float)
    quota.add_argument("--source", required=True)
    quota.add_argument("--json", action="store_true")

    orchestrate = commands.add_parser("orchestrate", help="plan and run role-based KaroX workers")
    orch = orchestrate.add_subparsers(dest="orchestrate_command", required=True)
    recipes = orch.add_parser("recipes")
    recipes.add_argument("--json", action="store_true")
    recipe_add = orch.add_parser("recipe-add", help="install a data-only JSON orchestration recipe")
    recipe_add.add_argument("path", type=Path)
    recipe_add.add_argument("--json", action="store_true")
    recipe_remove = orch.add_parser("recipe-remove", help="remove a custom orchestration recipe")
    recipe_remove.add_argument("name")
    recipe_remove.add_argument("--json", action="store_true")
    for name in ("plan", "run", "start"):
        parser = orch.add_parser(
            name,
            help=(
                "start a detached Mission Control run"
                if name == "start"
                else None
            ),
        )
        parser.add_argument("--repository", type=Path, default=Path.cwd())
        parser.add_argument("--objective", required=True)
        parser.add_argument("--recipe", default="feature")
        parser.add_argument("--preset", choices=sorted(PRESETS), default="balanced")
        parser.add_argument("--risk", choices=[item.value for item in RiskLevel], default="medium")
        parser.add_argument("--orchestrator")
        parser.add_argument(
            "--delegate-workers",
            action=argparse.BooleanOptionalAction,
            default=True,
            help=(
                "let the selected orchestrator propose worker/effort assignments in one guarded "
                "tool-less/read-only turn (default); KaroX still validates every choice through "
                "the normal router. Use --no-delegate-workers for deterministic router-only planning"
            ),
        )
        parser.add_argument("--run-id", help=argparse.SUPPRESS)
        if name == "start":
            parser.add_argument("--label", default="", help="short Mission Control label")
        parser.add_argument("--assign", action="append", default=[])
        parser.add_argument(
            "--worker-effort",
            action="append",
            default=[],
            help="override KaroX Effort as ROLE_OR_STEP=low|medium|high|extra-high|ultra|auto",
        )
        parser.add_argument("--json", action="store_true")
        if name in {"run", "start"}:
            parser.add_argument(
                "--verification-command",
                action="append",
                default=[],
                help=(
                    "approved command as JSON argv; repeatable. If omitted, KaroX "
                    "discovers a bounded safe verification set for the repository"
                ),
            )
            parser.add_argument(
                "--max-steps",
                type=int,
                default=96,
                help="hard safety cap; each worker's KaroX Effort normally chooses a lower limit",
            )
            parser.add_argument(
                "--max-seconds",
                type=float,
                default=3600.0,
                help="hard safety cap; each worker's KaroX Effort normally chooses a lower limit",
            )
            parser.add_argument(
                "--isolate-implementers",
                action=argparse.BooleanOptionalAction,
                default=True,
                help=(
                    "run implementers in detached KaroX worktrees by default; downstream "
                    "single-lineage review/test inherits the same worktree. Use "
                    "--no-isolate-implementers only for an intentional single-workspace run"
                ),
            )
            parser.add_argument(
                "--baseline-cost",
                type=float,
                help="measured OFF-vs-ON baseline cost in USD for the automatic Savings Receipt",
            )
            parser.add_argument(
                "--hosted-project-run",
                action="store_true",
                help=argparse.SUPPRESS,
            )
    observe = orch.add_parser("observe", help="record one accepted/verified local routing outcome")
    observe.add_argument("endpoint_id")
    observe.add_argument("--task-class", choices=sorted(TASK_CLASSES), required=True)
    observe.add_argument("--accepted", action=argparse.BooleanOptionalAction, required=True)
    observe.add_argument("--verified", action=argparse.BooleanOptionalAction, required=True)
    observe.add_argument("--cost-usd", type=float)
    observe.add_argument("--latency-ms", type=float, default=0.0)
    observe.add_argument("--tokens", type=int, default=0)
    observe.add_argument("--json", action="store_true")
    shadow = orch.add_parser("shadow-route", help="show what verified routing would choose without executing")
    shadow.add_argument("actual_endpoint_id")
    shadow.add_argument("--task-class", choices=sorted(TASK_CLASSES), required=True)
    shadow.add_argument("--role", required=True)
    shadow.add_argument("--capability", action="append", default=[])
    shadow.add_argument("--risk", choices=[item.value for item in RiskLevel], default="medium")
    shadow.add_argument("--input-tokens", type=int, default=0)
    shadow.add_argument("--output-tokens", type=int, default=0)
    shadow.add_argument("--json", action="store_true")
    status = orch.add_parser("status")
    status.add_argument("run_id")
    status.add_argument("--json", action="store_true")
    recover = orch.add_parser("recover")
    recover.add_argument("run_id")
    recover.add_argument("--json", action="store_true")
    resolve = orch.add_parser("resolve-recovery")
    resolve.add_argument("run_id")
    resolve.add_argument("step_id")
    resolve.add_argument("--completed", action=argparse.BooleanOptionalAction, required=True)
    resolve.add_argument("--passed", action=argparse.BooleanOptionalAction, required=True)
    resolve.add_argument("--summary", default="")
    resolve.add_argument("--json", action="store_true")

    mission = commands.add_parser("mission-control", help="local/mobile orchestration status and steering")
    mc = mission.add_subparsers(dest="mission_control_command", required=True)
    show = mc.add_parser("show")
    show.add_argument("run_id")
    show.add_argument(
        "--details",
        action="store_true",
        help="show run ids, endpoint ids, token counts, and other runtime details",
    )
    show.add_argument("--json", action="store_true")
    command = mc.add_parser("command")
    command.add_argument("run_id")
    command.add_argument("command_type", choices=sorted(COMMAND_TYPES))
    command.add_argument("--target", default="orchestrator")
    command.add_argument("--text", default="")
    command.add_argument("--json", action="store_true")
    serve = mc.add_parser("serve")
    serve.add_argument("run_id")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8766)
    serve.add_argument("--json", action="store_true")

    economy = commands.add_parser("economy", help="measured savings receipts")
    eco = economy.add_subparsers(dest="economy_command", required=True)
    receipt = eco.add_parser("receipt")
    receipt.add_argument("--usage-json", type=Path, required=True)
    receipt.add_argument("--baseline-cost", type=float)
    receipt.add_argument("--currency", default="USD")
    receipt.add_argument("--context-reused-chars", type=int, default=0)
    receipt.add_argument("--context-sent-chars", type=int, default=0)
    receipt.add_argument("--tool-schema-bytes-avoided", type=int, default=0)
    receipt.add_argument("--round-trips-avoided", type=int, default=0)
    receipt.add_argument("--premium-calls-avoided", type=int, default=0)
    receipt.add_argument("--accepted", action=argparse.BooleanOptionalAction, required=True)
    receipt.add_argument("--verified", action=argparse.BooleanOptionalAction, required=True)
    receipt.add_argument("--gate", action="append", default=[])
    receipt.add_argument("--json", action="store_true")
    shadow_observe = eco.add_parser(
        "shadow-observe",
        help="record a passive what-would-KaroX-have-routed observation without changing execution",
    )
    shadow_observe.add_argument("actual_endpoint_id")
    shadow_observe.add_argument("--task-id", required=True)
    shadow_observe.add_argument("--task-class", choices=sorted(TASK_CLASSES), required=True)
    shadow_observe.add_argument("--role", required=True)
    shadow_observe.add_argument("--capability", action="append", default=[])
    shadow_observe.add_argument("--risk", choices=[item.value for item in RiskLevel], default="medium")
    shadow_observe.add_argument("--input-tokens", type=int, default=0)
    shadow_observe.add_argument("--output-tokens", type=int, default=0)
    shadow_observe.add_argument("--accepted", action=argparse.BooleanOptionalAction, required=True)
    shadow_observe.add_argument("--verified", action=argparse.BooleanOptionalAction, required=True)
    shadow_observe.add_argument("--actual-cost-usd", type=float)
    shadow_observe.add_argument("--json", action="store_true")
    shadow_summary = eco.add_parser("shadow-summary")
    shadow_summary.add_argument("--json", action="store_true")


def _plan_selection_from_args(args: argparse.Namespace) -> PlanSelection:
    return build_orchestration_plan(
        repository=args.repository,
        objective=args.objective,
        recipe_name=args.recipe,
        preset=args.preset,
        risk_level=RiskLevel(args.risk),
        orchestrator_endpoint_id=args.orchestrator,
        role_assignments=_assignments(args.assign),
        effort_assignments=_effort_assignments(args.worker_effort),
        run_id=getattr(args, "run_id", None),
        delegate_workers=bool(getattr(args, "delegate_workers", False)),
    )


def _plan_from_args(args: argparse.Namespace):
    return _plan_selection_from_args(args).plan


def _validate_cli_execution_plan(plan: Any, *, isolate_implementers: bool) -> dict[str, IntelligenceEndpoint]:
    """Ensure the detached/foreground CLI can execute every selected endpoint."""

    endpoints = {item.endpoint.endpoint_id: item.endpoint for item in plan.steps}
    endpoints.setdefault(
        plan.orchestrator_endpoint.endpoint_id, plan.orchestrator_endpoint
    )
    unsupported = [
        endpoint_id
        for endpoint_id, endpoint in endpoints.items()
        if endpoint.source_kind != SOURCE_API
        and not (
            endpoint.source_kind == SOURCE_SUBSCRIPTION
            and SubscriptionCliExecutor.supports(endpoint)
        )
    ]
    if unsupported:
        raise ValueError(
            "CLI run selected endpoint(s) without a built-in guarded adapter: "
            + ", ".join(sorted(unsupported))
            + ". Register an application adapter or choose an API/Codex/Claude subscription endpoint."
        )
    if not isolate_implementers and any(
        item.step.role == "implementer"
        and item.endpoint.source_kind == SOURCE_SUBSCRIPTION
        for item in plan.steps
    ):
        raise ValueError(
            "subscription implementers require --isolate-implementers so KaroX can bind writes to a detached worktree"
        )
    return endpoints


def _background_run_argv(args: argparse.Namespace, run_id: str) -> list[str]:
    """Build the one authoritative foreground run executed by the detached child.

    ``start`` deliberately does not pre-plan in the parent process. Otherwise a
    delegated run would spend an extra orchestrator call and a normal run could
    route twice against changing quota/telemetry. The child owns planning and
    execution exactly once.
    """

    argv = [
        "orchestrate",
        "run",
        "--repository",
        str(args.repository),
        "--objective",
        args.objective,
        "--recipe",
        args.recipe,
        "--preset",
        args.preset,
        "--risk",
        args.risk,
        "--run-id",
        run_id,
    ]
    if args.orchestrator:
        argv.extend(("--orchestrator", args.orchestrator))
    if bool(getattr(args, "delegate_workers", False)):
        argv.append("--delegate-workers")
    for value in args.assign:
        argv.extend(("--assign", value))
    for value in args.worker_effort:
        argv.extend(("--worker-effort", value))
    for value in args.verification_command:
        argv.extend(("--verification-command", value))
    argv.extend(("--max-steps", str(args.max_steps)))
    argv.extend(("--max-seconds", str(args.max_seconds)))
    if args.isolate_implementers:
        argv.append("--isolate-implementers")
    if args.baseline_cost is not None:
        argv.extend(("--baseline-cost", str(args.baseline_cost)))
    argv.append("--json")
    return argv


def _handle_intelligence(args: argparse.Namespace) -> int:
    pool = IntelligencePool()
    command = args.intelligence_command
    payload: Any
    if command == "list":
        payload = [item.to_dict() for item in pool.list()]
        if not args.json:
            print(
                render_intelligence_list(
                    payload,
                    "en",
                    details=bool(getattr(args, "details", False)),
                )
            )
            return 0
    elif command == "show":
        payload = pool.get(args.endpoint_id).to_dict()
    elif command == "add":
        if args.source not in SOURCE_KINDS or args.source == "api":
            raise ValueError("API endpoints are added through karox provider/model")
        endpoint = IntelligenceEndpoint(
            endpoint_id=args.endpoint_id,
            display_name=args.name,
            source_kind=args.source,
            capabilities=_capabilities(args.capability),
            roles=_roles(args.role),
            model_id=args.model,
            target_id=args.target_id,
            already_paid=args.already_paid,
        )
        payload = pool.put(endpoint).to_dict()
    elif command == "discover-agents":
        discovered = discover_subscription_clis()
        applied = (
            register_discovered_subscription_endpoints(
                pool,
                include_manual_adapter_targets=args.include_manual_adapter_targets,
            )
            if args.apply
            else ()
        )
        payload = {
            "discovered": [item.to_dict() for item in discovered],
            "applied": [item.to_dict() for item in applied],
        }
    elif command == "remove":
        payload = pool.remove(args.endpoint_id).to_dict()
    elif command in {"enable", "disable"}:
        payload = pool.set_enabled(args.endpoint_id, command == "enable").to_dict()
    elif command == "quota":
        quota = QuotaBrain().observe(
            args.endpoint_id,
            remaining_fraction=args.remaining_fraction,
            remaining_units=args.remaining_units,
            unit=args.unit,
            resets_at=args.resets_at,
            source=args.source,
        )
        payload = {"endpoint_id": args.endpoint_id, "quota": quota.to_dict()}
    else:
        raise ValueError(f"unsupported intelligence command: {command}")
    _emit(payload, json_output=args.json)
    return 0


def _handle_orchestrate(args: argparse.Namespace) -> int:
    command = args.orchestrate_command
    payload: Any
    if command == "recipes":
        payload = [item.to_dict() for item in RecipeRegistry().list()]
    elif command == "recipe-add":
        payload = RecipeRegistry().put_file(args.path).to_dict()
    elif command == "recipe-remove":
        payload = RecipeRegistry().remove(args.name).to_dict()
    elif command == "plan":
        selection = _plan_selection_from_args(args)
        payload = selection.plan.to_dict()
        delegation = selection.delegation_dict()
        if delegation is not None:
            payload["delegation"] = delegation
    elif command == "start":
        from .background_orchestration import BackgroundOrchestrationRegistry

        run_id = args.run_id or f"run-{uuid.uuid4().hex}"
        args.run_id = run_id
        record = BackgroundOrchestrationRegistry().start(
            run_id=run_id,
            repository=args.repository,
            cli_argv=_background_run_argv(args, run_id),
            recipe=args.recipe,
            preset=args.preset,
            label=args.label or args.objective,
        )
        payload = {
            "status": "started",
            "run_id": run_id,
            "pid": record.pid,
            "launch_mechanism": record.launch_mechanism,
            "identity_provable": record.process_identity.provable,
            "mission_command": f"karox mission-control show {run_id}",
        }
    elif command == "run":
        selection = _plan_selection_from_args(args)
        plan = selection.plan
        verification_commands = _verification_commands_for_args(args)
        factory = NativeApiWorkerFactory(
            args.repository,
            verification_commands=verification_commands,
            max_steps=args.max_steps,
            max_seconds=args.max_seconds,
            economy_mode=True,
        )
        native_executor = NativeAgentExecutor(factory)
        subscription_executor = SubscriptionCliExecutor(
            args.repository,
            verification_commands=verification_commands,
            timeout_seconds=args.max_seconds,
        )
        endpoints = _validate_cli_execution_plan(
            plan, isolate_implementers=bool(args.isolate_implementers)
        )
        executors: dict[str, Any] = {}
        for endpoint_id, endpoint in endpoints.items():
            if endpoint.source_kind == SOURCE_API:
                executors[endpoint_id] = native_executor
            else:
                executors[endpoint_id] = subscription_executor
        context = ContextBus(plan.run_id)
        context.put_text(
            item_id="task",
            kind="task",
            content=plan.objective,
            priority=100,
            stable=False,
            source_ref="cli",
        )
        project_context = discover_project_context(
            args.repository,
            verification_commands=verification_commands,
            goal=plan.objective,
            session_id=plan.run_id,
            recursive_context="auto",
        )
        if project_context.environment:
            context.put_text(
                item_id="environment",
                kind="environment",
                content=project_context.environment,
                priority=90,
                stable=True,
                source_ref="project-context",
            )
        if project_context.project_map:
            context.put_text(
                item_id="project-map",
                kind="project_map",
                content=project_context.project_map,
                priority=95,
                stable=True,
                source_ref="project-context",
            )
        if project_context.instructions:
            context.put_text(
                item_id="project-instructions",
                kind="constitution",
                content=project_context.instructions,
                priority=95,
                stable=True,
                source_ref="project-context",
            )
        advice = selection.advice
        runtime_type = OrchestrationRuntime
        if bool(getattr(args, "hosted_project_run", False)):
            # ChatGPT Project runs keep the generic orchestration machinery but
            # add target-aware Mission Control steering. Import lazily so the
            # ordinary CLI/TUI path has no hosted-only dependency.
            from .hosted_project_orchestration import HostedProjectOrchestrationRuntime

            runtime_type = HostedProjectOrchestrationRuntime
        payload = runtime_type(
            plan,
            executors=executors,
            context_bus=context,
            telemetry=RoutingTelemetry(),
            measured_baseline_cost_usd=args.baseline_cost,
            initial_cost_usd=(
                0.0 if advice is None or advice.cost_usd is None else advice.cost_usd
            ),
            initial_tokens=(0 if advice is None else advice.total_tokens),
            worktree_pool=(WorktreePool(args.repository) if args.isolate_implementers else None),
            isolate_implementers=args.isolate_implementers,
        ).run().to_dict()
        delegation = selection.delegation_dict()
        if delegation is not None:
            payload["delegation"] = delegation
    elif command == "observe":
        payload = RoutingTelemetry().observe(
            endpoint_id=args.endpoint_id,
            task_class=args.task_class,
            accepted=args.accepted,
            verified=args.verified,
            latency_ms=args.latency_ms,
            cost_usd=args.cost_usd,
            total_tokens=args.tokens,
        ).to_dict()
    elif command == "shadow-route":
        router = VerifiedSmartRouter(quota_brain=QuotaBrain())
        request = RouteRequest(
            task_class=args.task_class,
            role=args.role,
            required_capabilities=_capabilities(args.capability),
            risk_level=RiskLevel(args.risk),
            estimated_input_tokens=args.input_tokens,
            estimated_output_tokens=args.output_tokens,
        )
        payload = shadow_route(
            router=router,
            request=request,
            actual_endpoint_id=args.actual_endpoint_id,
        ).to_dict()
    elif command == "status":
        snapshot = MissionControlStore(args.run_id).snapshot()
        payload = {} if snapshot is None else snapshot.to_dict()
    elif command == "recover":
        payload = OrchestrationJournal(args.run_id).reconcile_after_restart().to_dict()
    elif command == "resolve-recovery":
        payload = OrchestrationJournal(args.run_id).resolve_reconciliation(
            args.step_id,
            completed=args.completed,
            passed=args.passed,
            summary=args.summary,
        ).to_dict()
    else:
        raise ValueError(f"unsupported orchestrate command: {command}")
    if not args.json and command == "plan" and isinstance(payload, Mapping):
        print(render_plan(payload))
    elif not args.json and command == "run" and isinstance(payload, Mapping):
        print(render_run(payload))
    elif not args.json and command == "start" and isinstance(payload, Mapping):
        print(render_started(payload, objective=args.label or args.objective))
    else:
        _emit(payload, json_output=args.json)
    return 0


def _handle_mission_control(args: argparse.Namespace) -> int:
    store = MissionControlStore(args.run_id)
    command = args.mission_control_command
    if command == "show":
        snapshot = store.snapshot()
        payload = {} if snapshot is None else snapshot.to_dict()
        if args.json:
            _emit(payload, json_output=True)
            return 0
        if snapshot is None:
            print(f"Mission not found: {args.run_id}")
            return 0
        endpoint_ids = {snapshot.orchestrator_endpoint_id}
        endpoint_ids.update(item.endpoint_id for item in snapshot.agents)
        names: dict[str, str] = {}
        try:
            for endpoint in IntelligencePool().list():
                if endpoint.endpoint_id in endpoint_ids:
                    names[endpoint.endpoint_id] = endpoint.display_name
        except Exception:
            # Mission state remains useful even if optional model metadata is
            # temporarily unavailable or its registry needs repair.
            names = {}
        print(
            render_mission(
                payload,
                endpoint_names=names,
                details=bool(getattr(args, "details", False)),
            )
        )
        return 0
    if command == "command":
        payload = store.enqueue(args.command_type, target=args.target, text=args.text).to_dict()
        _emit(payload, json_output=args.json)
        return 0
    if command == "serve":
        server = MissionControlServer(store, host=args.host, port=args.port)
        info = {
            "status": "running",
            "host": args.host,
            "port": args.port,
            "pairing_code": server.pairing.code,
            "pairing_expires_at": server.pairing.expires_at,
            "note": "Use loopback or a private Tailscale address. The pairing code is short-lived and is never put in the URL.",
        }
        _emit(info, json_output=args.json)
        # Keep the server on the foreground thread so Ctrl+C is honest and no
        # hidden background service survives the CLI process.
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            server.shutdown()
        return 0
    raise ValueError(f"unsupported Mission Control command: {command}")


def _handle_economy(args: argparse.Namespace) -> int:
    command = args.economy_command
    if command == "receipt":
        usage = _usage_summary(args.usage_json)
        receipt = build_savings_receipt(
            usage=usage,
            counters=EconomyCounters(
                context_chars_reused=args.context_reused_chars,
                context_chars_sent=args.context_sent_chars,
                tool_schema_bytes_avoided=args.tool_schema_bytes_avoided,
                round_trips_avoided=args.round_trips_avoided,
                premium_calls_avoided=args.premium_calls_avoided,
            ),
            measured_baseline_cost=args.baseline_cost,
            baseline_currency=args.currency,
            accepted=args.accepted,
            verified=args.verified,
            quality_gates=args.gate,
        )
        payload: Any = receipt.to_dict() if args.json else receipt.render()
    elif command == "shadow-observe":
        router = VerifiedSmartRouter(quota_brain=QuotaBrain())
        request = RouteRequest(
            task_class=args.task_class,
            role=args.role,
            required_capabilities=_capabilities(args.capability),
            risk_level=RiskLevel(args.risk),
            estimated_input_tokens=args.input_tokens,
            estimated_output_tokens=args.output_tokens,
        )
        payload = ShadowEconomyLedger().observe(
            router=router,
            task_id=args.task_id,
            request=request,
            actual_endpoint_id=args.actual_endpoint_id,
            accepted=args.accepted,
            verified=args.verified,
            actual_cost_usd=args.actual_cost_usd,
        ).to_dict()
    elif command == "shadow-summary":
        payload = ShadowEconomyLedger().summary().to_dict()
    else:
        raise ValueError(f"unsupported economy command: {command}")
    _emit(payload, json_output=args.json)
    return 0


def handle_orchestration_command(args: argparse.Namespace) -> Optional[int]:
    if args.command == "intelligence":
        return _handle_intelligence(args)
    if args.command == "orchestrate":
        return _handle_orchestrate(args)
    if args.command == "mission-control":
        return _handle_mission_control(args)
    if args.command == "economy":
        return _handle_economy(args)
    return None


__all__ = ["handle_orchestration_command", "register_orchestration_commands"]
