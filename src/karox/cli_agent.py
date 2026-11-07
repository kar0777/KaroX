"""The `agent run` command: session resolution, policy/risk construction, Core assembly and the engine kernel report."""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
from typing import Any
from .action_execution import CapabilityCoreRuntime
from .cli_common import (
    json_output_value as _json,
    verification_argv as _verification_argv,
    write as _write,
)
from .action_policy import ActionDecisionEngine
from .agent import (
    AgentEvent,
    AgentEventKind,
    AgentKernel,
    AgentLimits,
    AgentReport,
    SYSTEM_PROMPT,
)
from .agent_modes import mode_policy, mode_prompt_delta
from .core import CoreRuntime
from .disk_maintenance import is_drive_root, workspace_system_reason
from .effort import budget_for as effort_budget_for
from .models import AccessProfile, Capability, Origin, OriginKind
from .paths import runtime_dir, session_dir
from .policy import CapabilityPolicy
from .risk_engine import RiskEngine
from .security import redact
from .sessions import SessionError, SessionStore
from .skill_runtime import SkillSession, SkillSessionRuntime
from .skills import SkillError
from .verification import discover_verification_commands

from .cli_context import _registry
from .cli_present import (
    _load_pricing_registry,
    _print_agent_report,
    _stream_json,
    _stream_progress,
)
from .cli_runtime import (
    _attach_mcp_servers,
    _project_instructions,
    _resolve_runtime,
    _run_vision_capability,
    _selected_mcp_runtime,
    _skill_catalog,
)

def _agent_profile(args: argparse.Namespace, selected_provider: Any = None) -> AccessProfile:
    if getattr(args, "access_profile", None):
        return AccessProfile(args.access_profile)
    if selected_provider is not None and bool(getattr(selected_provider, "bypass", False)):
        return AccessProfile.ELEVATED
    return AccessProfile.WORKSPACE_WRITE


def _agent_grants(profile: AccessProfile, mode: str) -> set[Capability]:
    grants = {
        Capability.REPO_READ,
        Capability.REPO_WRITE,
        Capability.DISK_READ,
        Capability.PROCESS_RUN,
        Capability.CHECKS_RUN,
        Capability.GIT_READ,
        Capability.DIAGNOSTICS_READ,
        Capability.MCP_CALL,
        Capability.CODE_FETCH,
        # Loopback browser control travels with the write tier: the browser
        # runtime's own policy is localhost-only and headless by default, and
        # leaving the machine still needs Capability.NETWORK on top.
        Capability.BROWSER_READ,
        Capability.BROWSER_INPUT,
    }
    if profile == AccessProfile.ELEVATED:
        grants.update(
            {
                Capability.DEV_COMMAND,
                Capability.GIT_COMMIT,
                Capability.NETWORK,
                Capability.DESKTOP_INPUT,
            }
        )
    if mode in {"plan", "ideate"}:
        # Browser input drives a UI, so plan mode observes but never actuates,
        # exactly as it never writes repository files.
        grants -= {Capability.REPO_WRITE, Capability.GIT_COMMIT, Capability.BROWSER_INPUT}
    return grants


def _run_agent(args: argparse.Namespace) -> AgentReport:
    """Resolve one agent run end to end and return its report.

    Everything a run needs -- session resolution, the provider, the policy,
    the Core with its verification allowlist, the kernel (the vendored
    karo-core engine) and the mode artifact -- happens here, so a caller
    that only wants the outcome can take it without the output layer.
    """
    repository = args.repository.expanduser().resolve(strict=True)
    if not repository.is_dir():
        raise ValueError(f"repository is not a directory: {repository}")
    system_reason = workspace_system_reason(repository)
    if system_reason is not None:
        raise ValueError(
            f"refusing to run the agent in a protected system folder ({system_reason}): "
            f"{repository}"
        )
    if is_drive_root(repository):
        raise ValueError(
            "refusing to run the agent at a drive root; select a project directory"
        )
    store = SessionStore(session_dir())
    record = None
    if args.session_id and store.state_path(args.session_id).exists():
        record = store.load(args.session_id)
        store.validate_repository(record, repository)
        if record.access_profile not in {
            AccessProfile.WORKSPACE_WRITE.value,
            AccessProfile.ELEVATED.value,
        }:
            raise SessionError("the agent requires a workspace_write session")
        if args.continue_task:
            record = store.continue_task(args.session_id, args.task)
        elif record.task != str(redact(args.task)).strip():
            raise SessionError(
                "resume task differs from the existing session task; "
                "use --continue-task to append a new turn explicitly"
            )
    provider, model, context_window, max_output_tokens, bypass = _resolve_runtime(
        args, timeout_seconds=120.0, record=record
    )
    if record is None:
        selected_provider = None
        if not args.model:
            selected = _registry().selected_model()
            if selected is not None:
                selected_provider = _registry().provider(selected.provider_id)
        profile = _agent_profile(args, selected_provider)
        record = store.create(
            repository,
            args.task,
            profile,
            session_id=args.session_id,
        )
    else:
        profile = AccessProfile(record.access_profile)
    # Bypass is destructive-autonomy authority, not a capability grant: it may
    # only take effect when the effective profile is already elevated.
    bypass = bool(bypass and profile == AccessProfile.ELEVATED)

    verification: tuple[tuple[str, ...], ...]
    discovered: tuple[tuple[str, ...], ...] = ()
    if args.verification_command:
        verification = tuple(_verification_argv(item) for item in args.verification_command)
    else:
        # A command found in the repository is a suggestion, not user approval:
        # a hostile package.json must not become verification evidence.
        discovered = discover_verification_commands(repository)
        verification = (
            discovered if getattr(args, "accept_discovered_checks", False) else ()
        )
    effort_signals = None
    effort_reasons: list[str] = []
    if args.effort_level == "auto":
        try:
            from .effort import recommend_effort
            from .effort_signals import derive_task_signals

            derived = derive_task_signals(
                args.task, repository=repository, mode=args.mode
            )
            effort_signals = derived.signals
            recommendation = recommend_effort(effort_signals)
            effort_reasons = list(recommendation.reasons)
        except (ImportError, OSError, ValueError):
            effort_signals = None
    budget = effort_budget_for(args.effort_level, effort_signals)
    limits = AgentLimits(
        max_steps=args.max_steps or budget.agent_limits.max_steps,
        max_seconds=args.max_seconds or budget.agent_limits.max_seconds,
    )
    context = budget.context
    if context_window is not None:
        context = context.with_window(context_window, source="advertised")

    origin = Origin(OriginKind.NATIVE_AGENT, f"cli-{record.session_id}")
    policy = CapabilityPolicy(profile)
    policy.set_grants(origin, _agent_grants(profile, args.mode))
    risk = RiskEngine()
    decisions = ActionDecisionEngine(risk)
    if getattr(args, "mcp_server", None):
        record = _attach_mcp_servers(store, record, repository, args.mcp_server)
    mcp_binding, _ = _selected_mcp_runtime(record, repository)
    catalog, skill_block = _skill_catalog(repository)
    visual_checker = None
    try:
        from .visual_check import VisualChecker, is_visual_task
        from .web_check import default_pages

        vision = _run_vision_capability(args, _registry)
        wants_look = is_visual_task(args.task)
        if not wants_look:
            with contextlib.suppress(Exception):
                wants_look = bool(default_pages(repository))
        if vision and wants_look:
            from .browser_bootstrap import playwright_chromium_status

            if playwright_chromium_status().ready:
                visual_checker = VisualChecker(
                    provider=provider,
                    model=model,
                    artifacts=store.state_path(record.session_id).parent / "visual",
                    vision=True,
                    goal=args.task,
                    serve_root=repository,
                )
    except Exception:
        visual_checker = None
    skill_session = None
    if catalog is not None:
        try:
            skill_session = SkillSession(
                catalog,
                record.session_id,
                policy=policy,
                invoker_origin=origin,
                audit_path=runtime_dir()
                / "vnext"
                / "skill-audit"
                / f"{record.session_id}.jsonl",
            )
        except (SkillError, OSError):
            skill_session = None
    runtime_options: dict[str, Any] = dict(
        mcp_binding=mcp_binding,
        verification_commands=verification,
        risk=risk,
        action_decisions=decisions,
        bypass_mode=bypass,
        skill_catalog=catalog,
        visual_checker=visual_checker,
        allow_probes=not getattr(args, "no_probes", False),
    )
    if skill_session is not None:
        # The b-skills wiring: the session-bound runtime exposes skill.run as
        # a leased, idempotent Core tool next to the catalogue's load/read.
        core: CoreRuntime = SkillSessionRuntime(
            repository,
            policy,
            store,
            runtime_dir() / "vnext" / "audit.jsonl",
            skill_session=skill_session,
            **runtime_options,
        )
    else:
        core = CapabilityCoreRuntime(
            repository,
            policy,
            store,
            runtime_dir() / "vnext" / "audit.jsonl",
            **runtime_options,
        )
    # The b-browser/b-desktop wiring: their engine-neutral adapters become
    # typed Core tools next to the built-in ones, so the default engine run
    # advertises and executes them through the same policy/risk gate. Browser
    # and desktop resources stay lazily initialized; registration never
    # launches Chromium or touches a window.
    from .engine_tools import register_browser_tool, register_desktop_tools

    browser_runtime = register_browser_tool(
        core,
        capability_policy=policy,
        origin=origin,
        session_id=record.session_id,
        plain_credentials=bool(
            getattr(args, "browser_plain_credentials", False)
        ),
    )
    register_desktop_tools(core, session_id=record.session_id)
    project_metadata: dict[str, Any]
    if getattr(args, "no_project_context", False):
        project_suffix, project_metadata = "", {"enabled": False}
    else:
        try:
            from .project_context import discover_project_context

            project = discover_project_context(
                repository,
                branch=record.branch,
                verification_commands=verification,
                goal=args.task,
                session_id=record.session_id,
                probes=core.probe_interpreters,
            )
            project_suffix = project.prompt_suffix
            project_metadata = {"enabled": True, **project.to_dict()}
        except Exception:
            project_suffix, project_metadata = _project_instructions(repository)
    project_metadata["skills"] = (
        [item.name for item in catalog.discover()] if catalog is not None else []
    )
    project_metadata["verification"] = {
        "approved": [" ".join(item) for item in verification],
        "discovered": [" ".join(item) for item in discovered],
    }
    if discovered and not verification:
        print(
            "karox: discovered checks are suggestions only; pass an explicit "
            "verification command (or --accept-discovered-checks) to count them "
            "as evidence",
            file=sys.stderr,
        )
    if effort_reasons:
        project_metadata["effort_auto"] = {
            "level": budget.level,
            "reasons": effort_reasons,
        }
    system_prompt = SYSTEM_PROMPT
    if args.mode != "build":
        system_prompt = system_prompt + "\n\n" + mode_prompt_delta(args.mode)
    if project_suffix:
        system_prompt += project_suffix
    memory_block = None
    if not getattr(args, "no_memory", False):
        try:
            from .memory import KaroXMemory, MemoryScope
            from .project_registry import _generated_project_id

            memory_block = (
                KaroXMemory(session_dir() / "memory").context(
                    scopes=(
                        (MemoryScope.USER, "default"),
                        (MemoryScope.PROJECT, _generated_project_id(repository)),
                        (MemoryScope.SESSION, record.session_id),
                    ),
                    budget_chars=1500,
                    task=args.task,
                    fresh_only=True,
                )
                or None
            )
        except Exception:
            memory_block = None
    if memory_block:
        system_prompt += "\n\n" + memory_block
        project_metadata["memory"] = True
    if skill_block:
        system_prompt += "\n\n" + skill_block
    if skill_session is not None:
        try:
            if skill_session.activate_automatic(args.task):
                system_prompt += "\n\n" + skill_session.active_prompt()
        except (SkillError, OSError):
            pass
    request_images: tuple[Any, ...] = ()
    if args.image:
        if args.model or args.base_url or args.api_key_env:
            raise ValueError(
                "image attachments require a selected registry model with vision=true"
            )
        selected = _registry().selected_model()
        if selected is None or selected.vision != "true":
            raise ValueError("the selected model does not advertise vision support")
        from .image_attachments import load_image_attachments

        loaded = load_image_attachments(repository, args.image)
        request_images = tuple(item.image for item in loaded)
        project_metadata["images"] = [item.public_dict() for item in loaded]
    observer = _stream_json() if args.stream_json else (
        _stream_progress() if args.stream else None
    )
    # AgentKernel is the facade over the vendored karo-core engine: the
    # run executes src/karox/engine through typed Core tools, with the
    # selected MCP tools and the skill surface advertised from core.tools().
    kernel = AgentKernel(
        provider=provider,
        model=model,
        core=core,
        sessions=store,
        origin=origin,
        limits=limits,
        system_prompt=system_prompt,
        context=context,
        project_context=project_metadata,
        require_change=args.expect == "change",
        max_output_tokens=max_output_tokens,
        pricing_registry=_load_pricing_registry(),
        reasoning_effort=budget.reasoning_effort,
        mode=args.mode,
        request_images=request_images,
        on_event=observer,
    )
    try:
        report = kernel.run(record.session_id)
    finally:
        # The browser session this run lazily created is torn down with the
        # run; the loopback-only policy means no detached instance survives it.
        with contextlib.suppress(Exception):
            browser_runtime.close_all()
        close = getattr(provider, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()
    rules = mode_policy(args.mode)
    if rules.default_artifact and isinstance(report.provider_message, str) and (
        report.provider_message.strip()
    ):
        from .mode_artifacts import save_mode_artifact

        try:
            saved = save_mode_artifact(
                session_id=record.session_id,
                kind=rules.default_artifact,
                task=args.task,
                content=report.provider_message,
            )
        except (OSError, ValueError) as exc:
            report.project_context["mode_artifact"] = {
                "saved": False,
                "kind": rules.default_artifact,
                "error": type(exc).__name__,
            }
        else:
            report.project_context["mode_artifact"] = {"saved": True, **saved}
            if observer is not None:
                with contextlib.suppress(Exception):
                    observer(
                        AgentEvent(
                            AgentEventKind.ARTIFACT_CREATED,
                            report.steps,
                            path=str(saved.get("path") or "") or None,
                            detail=rules.default_artifact,
                        )
                    )
    return report


def _handle_agent_run(args: argparse.Namespace) -> int:
    report = _run_agent(args)
    if args.stream_json:
        _write(json.dumps({"type": "result", "report": report.to_dict()}, ensure_ascii=False) + "\n")
    elif args.json:
        _json(report.to_dict())
    else:
        _print_agent_report(report)
    return 0 if report.verified else 1
