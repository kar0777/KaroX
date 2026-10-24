"""The release command surface: agent, sessions, providers, MCP, skills.

Deliberately smaller than the v5 CLI: every command here is part of a local
coding agent. Hosted bridges, browser profiles, orchestration and the web
dashboard are not smuggled back in through a subcommand.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import shlex
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, Sequence

from . import __version__
from .action_execution import CapabilityCoreRuntime
from .action_policy import ActionDecisionEngine
from .agent import (
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentKernel,
    AgentLimits,
    AgentReport,
    SYSTEM_PROMPT,
)
from .agent_modes import mode_prompt_delta
from .core import CoreCommand, CoreError, CoreRuntime
from .credentials import CredentialError, CredentialStore
from .effort import budget_for as effort_budget_for
from .mcp_client import (
    McpAccessDenied,
    McpClient,
    McpConfigurationError,
    McpCredentialStore,
    McpError,
    McpRegistry,
    McpRuntimeBinding,
    McpServerRecord,
    McpToolDescriptor,
    mcp_selection,
    validate_mcp_selection_registry,
)
from .mcp_status import (
    LIVENESS_FAILED,
    LIVENESS_LIVE,
    McpLiveness,
    build_mcp_status,
)
from .models import AccessProfile, Capability, Origin, OriginKind
from .paths import config_dir, runtime_dir, session_dir
from .policy import CapabilityPolicy
from .provider_adapters import (
    AnthropicMessagesProvider,
    GeminiGenerateContentProvider,
    OpenAIResponsesProvider,
)
from .provider_controller import ProviderController
from .provider_factory import ProviderFactory
from .provider_presets import provider_preset, provider_presets
from .providers import (
    ModelMessage,
    ModelRequest,
    OpenAIChatCompletionsProvider,
)
from .registry import ModelRecord, ProviderRecord, ProviderRegistry, RegistryError
from .risk_engine import RiskEngine
from .security import redact
from .sessions import SessionError, SessionStore
from .skills import SkillCatalog, SkillError
from .verification import discover_verification_commands

FIRST_RUN_HINT = """KaroX is installed, but no model is configured yet.

Add a provider and a model:
  karox provider setup openai --model gpt-5 --stdin-key
  karox provider presets

Then start the terminal client:
  karox tui
"""


# --------------------------------------------------------------------- helpers


def _registry() -> ProviderRegistry:
    return ProviderRegistry(config_dir() / "vnext" / "providers.json")


def _controller() -> ProviderController:
    return ProviderController(registry=_registry(), credentials=CredentialStore())


def _mcp_registry() -> McpRegistry:
    return McpRegistry(config_dir() / "vnext" / "mcp-servers.json")


def _json(value: Any) -> None:
    _write(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def _write(text: str) -> None:
    try:
        sys.stdout.write(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        sys.stdout.write(text.encode(encoding, "replace").decode(encoding, "replace"))
    sys.stdout.flush()


def _emit(value: Any, *, json_output: bool) -> None:
    if json_output:
        _json(value)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _write(f"{key}: {item}\n")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                identifier = item.get("provider_id") or ""
                if item.get("model_id") is not None:
                    identifier = f"{identifier}/{item['model_id']}"
                _write((identifier or json.dumps(item, ensure_ascii=False)) + "\n")
            else:
                _write(f"{item}\n")
    else:
        _write(f"{value}\n")


def _record_summary(record: Any) -> dict[str, Any]:
    usage = record.usage if isinstance(record.usage, dict) else {}
    checks = [item for item in record.checks if isinstance(item, dict)]
    return {
        "session_id": record.session_id,
        "name": record.name,
        "parent_session_id": record.parent_session_id,
        "archived": record.archived,
        "repository": record.repository,
        "branch": record.branch,
        "access_profile": record.access_profile,
        "task": record.task,
        "status": record.status,
        "phase": record.phase,
        "revision": record.revision,
        "updated_at": record.updated_at,
        "revoked": record.revoked,
        "changed_files": len(record.changed_files),
        "checks": len(checks),
        "checks_failed": sum(1 for item in checks if item.get("ok") is False),
        "requests": usage.get("requests"),
        "total_tokens": usage.get("total_tokens"),
        "costs": dict(usage.get("costs") or {}),
    }


def _pairs(values: Sequence[str], label: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"{label} must use NAME=VALUE")
        name, item = value.split("=", 1)
        if not name:
            raise ValueError(f"{label} name must not be empty")
        if name in result:
            raise ValueError(f"duplicate {label} name: {name}")
        result[name] = item
    return result


def _verification_argv(value: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(value)
    except ValueError:
        decoded = None
    if isinstance(decoded, list) and decoded and all(
        isinstance(item, str) and item for item in decoded
    ):
        return tuple(decoded)
    parts = tuple(shlex.split(value))
    if not parts:
        raise ValueError("a verification command must not be empty")
    return parts


def _stream_progress() -> Any:
    text_open = [False]
    reasoning = [0]
    last = [time.monotonic()]
    with contextlib.suppress(Exception):
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    def emit(text: str) -> None:
        try:
            sys.stderr.write(text)
        except UnicodeEncodeError:
            encoding = getattr(sys.stderr, "encoding", None) or "ascii"
            sys.stderr.write(text.encode(encoding, "replace").decode(encoding, "replace"))
        sys.stderr.flush()

    def line(text: str) -> None:
        if text_open[0]:
            emit("\n")
            text_open[0] = False
        emit(text + "\n")

    def observe(event: AgentEvent) -> None:
        if event.kind is AgentEventKind.TEXT_DELTA:
            emit(event.text_delta or "")
            text_open[0] = True
        elif event.kind is AgentEventKind.REASONING_SUMMARY_DELTA:
            emit(event.summary or "")
            text_open[0] = True
        elif event.kind is AgentEventKind.REASONING_DELTA:
            reasoning[0] += len(event.reasoning_delta or "")
            now = time.monotonic()
            if now - last[0] >= 15.0:
                last[0] = now
                line(f"  ~ reasoning {reasoning[0]} chars")
        elif event.kind is AgentEventKind.STEP_STARTED:
            line(f"[step {event.step}]")
        elif event.kind is AgentEventKind.COMPACTED:
            line(f"[context: {event.summary}]")
        elif event.kind is AgentEventKind.TOOL_STARTED:
            line(f"  -> {event.tool}")
        elif event.kind is AgentEventKind.TOOL_FINISHED:
            mark = "ok" if event.ok else "failed"
            line(f"  <- {event.tool} {mark} ({event.summary})")
        elif event.kind is AgentEventKind.FINISHED:
            line(f"[{event.status}: {event.reason}]")

    return observe


def _stream_json() -> Any:
    def observe(event: AgentEvent) -> None:
        payload = {"type": "event", **asdict(event)}
        _write(json.dumps(payload, ensure_ascii=False) + "\n")

    return observe


def _print_agent_report(report: AgentReport) -> None:
    from .security import redact

    _write(f"status: {report.status}\n")
    _write(f"reason: {report.reason}\n")
    _write(f"steps: {report.steps}\n")
    if report.changed_files:
        _write("changed files: " + ", ".join(report.changed_files) + "\n")
    passed = sum(1 for item in report.checks if item.get("ok") is True)
    if report.checks:
        _write(f"checks: {passed}/{len(report.checks)}\n")
    tokens = report.usage.get("total_tokens")
    if isinstance(tokens, int) and tokens:
        _write(f"tokens: {tokens}\n")
    answer = redact(report.provider_message or "").strip()
    if answer:
        _write("\n" + answer + "\n")


# -------------------------------------------------------------------- agent run


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
    }
    if profile == AccessProfile.ELEVATED:
        grants.update(
            {Capability.DEV_COMMAND, Capability.GIT_COMMIT, Capability.NETWORK}
        )
    if mode in {"plan", "ideate"}:
        grants -= {Capability.REPO_WRITE, Capability.GIT_COMMIT}
    return grants


def _resolve_runtime(
    args: argparse.Namespace,
    *,
    timeout_seconds: float,
) -> tuple[Any, str, Optional[int], Optional[int], bool]:
    """Return provider, model id, context window, output ceiling, bypass."""

    direct = bool(args.model or args.base_url or args.api_key_env)
    provider: Any
    if direct:
        if not args.model or not args.base_url:
            raise ValueError("direct provider mode requires --model and --base-url")
        credential = None
        if args.api_key_env:
            environment = args.api_key_env

            def credential() -> str:
                return os.environ.get(environment, "")

            if not os.environ.get(environment, "").strip():
                raise ValueError(
                    "provider API key environment variable is not set: " + environment
                )
        provider = OpenAIChatCompletionsProvider(
            args.base_url,
            credential=credential,
            timeout_seconds=min(60.0, timeout_seconds),
        )
        return (
            provider,
            args.model,
            args.context_window,
            args.max_output_tokens,
            False,
        )
    registry = _registry()
    selected = registry.selected_model()
    if selected is None:
        raise ValueError("no model is selected; run karox model select or provider setup")
    record = registry.provider(selected.provider_id)
    provider = ProviderFactory().create(record)
    return (
        provider,
        selected.model_id,
        args.context_window or selected.context_window,
        args.max_output_tokens or selected.max_output_tokens,
        bool(record.bypass),
    )


def _mcp_repository(value: Path) -> Path:
    repository = value.expanduser().resolve(strict=True)
    if not repository.is_dir():
        raise ValueError(f"repository is not a directory: {repository}")
    return repository


def _stored_mcp(record: Any, server_id: str) -> Optional[dict[str, Any]]:
    matches = [
        item
        for item in record.mcp_servers
        if isinstance(item, dict) and item.get("server_id") == server_id
    ]
    if len(matches) > 1:
        raise McpAccessDenied(f"session contains duplicate MCP selections: {server_id}")
    return matches[0] if matches else None


def _replace_stored_mcp(
    record: Any, server_id: str, selection: Optional[dict[str, Any]]
) -> bool:
    existing = _stored_mcp(record, server_id)
    if existing is None and selection is None:
        return False
    retained = [item for item in record.mcp_servers if item is not existing]
    if selection is not None:
        retained.append(selection)
        retained.sort(key=lambda item: str(item.get("server_id", "")))
    record.mcp_servers = retained
    return True


def _selected_mcp_runtime(
    record: Any, repository: Path
) -> tuple[Optional[McpRuntimeBinding], list[McpToolDescriptor]]:
    if not record.mcp_servers:
        return None, []
    registry = _mcp_registry()
    selections: list[tuple[McpServerRecord, dict[str, Any]]] = []
    seen: set[str] = set()
    for raw in record.mcp_servers:
        if not isinstance(raw, dict):
            raise McpAccessDenied("stored MCP selection must be an object")
        server_id = raw.get("server_id")
        if not isinstance(server_id, str) or not server_id:
            raise McpAccessDenied("stored MCP selection has no valid server ID")
        if server_id in seen:
            raise McpAccessDenied(f"duplicate stored MCP selection: {server_id}")
        seen.add(server_id)
        server = registry.get(server_id)
        validate_mcp_selection_registry(server, raw)
        selections.append((server, raw))

    client = McpClient(registry)
    descriptors: list[McpToolDescriptor] = []
    servers: list[McpServerRecord] = []
    for server, selection in selections:
        discovered = {
            item.remote_name: item
            for item in client.discover_record(server, repository)
        }
        selected_tools = selection.get("tools")
        if not isinstance(selected_tools, dict):
            raise McpAccessDenied("stored MCP tool permissions are malformed")
        for remote_name, stored in selected_tools.items():
            if not isinstance(remote_name, str) or not isinstance(stored, dict):
                raise McpAccessDenied("stored MCP tool permission is malformed")
            descriptor = discovered.get(remote_name)
            if descriptor is None:
                raise McpAccessDenied(
                    f"selected MCP tool disappeared: {server.server_id}/{remote_name}"
                )
            if (
                stored.get("name") != descriptor.name
                or stored.get("schema_digest") != descriptor.schema_digest
                or stored.get("read_only") is not descriptor.read_only
            ):
                raise McpAccessDenied(
                    f"selected MCP tool changed: {server.server_id}/{remote_name}"
                )
            permission = stored.get("permission")
            if permission not in {"allow", "ask", "deny"}:
                raise McpAccessDenied(
                    f"selected MCP tool permission is malformed: {remote_name}"
                )
            if permission == "allow":
                descriptors.append(descriptor)
        servers.append(server)
    return McpRuntimeBinding(client, repository, descriptors, servers), descriptors


def _project_instructions(
    repository: Path,
) -> tuple[str, dict[str, Any]]:
    """Read the repository instruction files, bounded and attributed.

    The v5 project map and research pass are not part of the release yet, but
    the files a repository owner wrote for agents are the one piece of context
    that must never be lost: they are appended to the system prompt and named
    in the report so a reader can see what shaped the run.
    """

    parts: list[str] = []
    sources: list[str] = []
    total = 0
    for name in ("AGENTS.md", "CLAUDE.md", "KAROX.md"):
        path = repository / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if not text:
            continue
        if len(text) > 20_000:
            text = text[:20_000] + "\n...[truncated]"
        parts.append(f"--- {name} ---\n{text}")
        sources.append(name)
        total += len(text)
    metadata: dict[str, Any] = {
        "enabled": True,
        "sources": sources,
        "instruction_bytes": total,
    }
    return "\n\n".join(parts), metadata


def _skill_catalog(repository: Path) -> tuple[Any, str]:
    try:
        catalog = SkillCatalog(repository)
        skills = catalog.discover()
    except (SkillError, OSError):
        return None, ""
    if not skills:
        return catalog, ""
    lines = [f"- {item.name}: {item.description}" for item in skills[:20]]
    block = (
        "AVAILABLE SKILLS (load one by name with the skill_load tool when it "
        "matches the task):\n" + "\n".join(lines)
    )
    return catalog, block


def _handle_agent_run(args: argparse.Namespace) -> int:
    repository = args.repository.expanduser().resolve(strict=True)
    if not repository.is_dir():
        raise ValueError(f"repository is not a directory: {repository}")
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
        args, timeout_seconds=120.0
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
        if bypass and profile == AccessProfile.WORKSPACE_WRITE:
            profile = AccessProfile.ELEVATED

    verification: tuple[tuple[str, ...], ...]
    if args.verification_command:
        verification = tuple(_verification_argv(item) for item in args.verification_command)
    else:
        verification = discover_verification_commands(repository)

    budget = effort_budget_for(args.effort_level)
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
    mcp_binding, _ = _selected_mcp_runtime(record, repository)
    catalog, skill_block = _skill_catalog(repository)
    core = CapabilityCoreRuntime(
        repository,
        policy,
        store,
        runtime_dir() / "vnext" / "audit.jsonl",
        mcp_binding=mcp_binding,
        verification_commands=verification,
        risk=risk,
        action_decisions=decisions,
        bypass_mode=bool(bypass),
        skill_catalog=catalog,
    )
    project_suffix, project_metadata = _project_instructions(repository)
    project_metadata["skills"] = (
        [item.name for item in catalog.discover()] if catalog is not None else []
    )
    system_prompt = SYSTEM_PROMPT
    if args.mode != "build":
        system_prompt = system_prompt + "\n\n" + mode_prompt_delta(args.mode)
    if project_suffix:
        system_prompt += (
            "\n\nPROJECT INSTRUCTIONS. The repository owner wrote these for "
            "agents; follow them unless they conflict with the task or the "
            "safety boundary.\n\n" + project_suffix
        )
    if skill_block:
        system_prompt += "\n\n" + skill_block
    observer = _stream_json() if args.stream_json else (
        _stream_progress() if args.stream else None
    )
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
        reasoning_effort=budget.reasoning_effort,
        mode=args.mode,
        on_event=observer,
    )
    try:
        report = kernel.run(record.session_id)
    finally:
        close = getattr(provider, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()
    if args.stream_json:
        _write(json.dumps({"type": "result", "report": report.to_dict()}, ensure_ascii=False) + "\n")
    elif args.json:
        _json(report.to_dict())
    else:
        _print_agent_report(report)
    return 0 if report.verified else 1


# ------------------------------------------------------------------ sessions


def _handle_session(args: argparse.Namespace) -> int:
    store = SessionStore(session_dir())
    command = args.session_command
    if command == "create":
        record = store.create(
            args.repository.expanduser().resolve(strict=True),
            args.task,
            AccessProfile(args.access_profile),
            branch=args.branch,
            session_id=args.id,
            name=args.name,
        )
        _emit(_record_summary(record), json_output=args.json)
        return 0
    if command == "list":
        records = [
            _record_summary(item)
            for item in store.list(include_archived=bool(args.all))
        ]
        if args.json:
            _json(records)
        else:
            for item in records:
                label = item.get("name") or item["session_id"]
                archived = "\tarchived" if item.get("archived") else ""
                _write(
                    f"{item['session_id']}\t{label}\t{item['status']}\t"
                    f"{item['access_profile']}\t{item['repository']}{archived}\n"
                )
        return 0
    if command == "fork":
        record = store.fork(
            args.session_id,
            session_id_new=args.new_session_id,
            task=args.task,
            name=args.name or "",
        )
        _emit(_record_summary(record), json_output=args.json)
        return 0
    if command == "rename":
        _emit(_record_summary(store.rename(args.session_id, args.name)), json_output=args.json)
        return 0
    if command == "archive":
        _emit(_record_summary(store.archive(args.session_id)), json_output=args.json)
        return 0
    if command == "unarchive":
        _emit(_record_summary(store.unarchive(args.session_id)), json_output=args.json)
        return 0
    if command == "delete":
        if args.confirm != args.session_id:
            raise ValueError("--confirm must exactly match the session ID")
        store.delete_archived(args.session_id)
        _emit({"session_id": args.session_id, "status": "deleted"}, json_output=args.json)
        return 0
    if command == "revoke":
        _emit(_record_summary(store.revoke(args.session_id)), json_output=args.json)
        return 0
    if command == "lock":
        record = store.load(args.session_id)
        lease_path = store.lease_path(args.session_id)
        info: dict[str, Any] = {
            "session_id": record.session_id,
            "locked": lease_path.exists(),
        }
        if lease_path.exists():
            try:
                raw = json.loads(lease_path.read_text(encoding="utf-8"))
                info["owner"] = raw.get("owner")
                info["expires_at"] = raw.get("expires_at")
                info["expired"] = float(raw.get("expires_at", 0)) < time.time()
            except (OSError, ValueError):
                info["locked"] = False
        _emit(info, json_output=args.json)
        return 0
    record = store.load(args.session_id)
    _emit(record.to_dict(), json_output=args.json)
    return 0


# ------------------------------------------------------------------ providers


def _probe_candidate(
    adapter_kind: str,
    base_url: str,
    model_id: str,
    *,
    credential: Any,
    timeout_seconds: float,
) -> dict[str, Any]:
    constructors = {
        "openai_compatible_chat": OpenAIChatCompletionsProvider,
        "openai_responses": OpenAIResponsesProvider,
        "anthropic_messages": AnthropicMessagesProvider,
        "gemini_generate_content": GeminiGenerateContentProvider,
    }
    try:
        constructor = constructors[adapter_kind]
    except KeyError as exc:
        raise ValueError(f"unsupported provider adapter: {adapter_kind}") from exc
    runtime: Any = constructor(
        base_url,
        credential=credential,
        timeout_seconds=min(60.0, timeout_seconds),
    )
    try:
        response = runtime.complete(
            ModelRequest(
                model=model_id,
                messages=(ModelMessage("user", "Reply with exactly OK."),),
                max_output_tokens=8,
                deadline_seconds=min(60.0, timeout_seconds),
            )
        )
    finally:
        close = getattr(runtime, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()
    return {
        "status": "ok",
        "provider_id": getattr(runtime, "endpoint", ""),
        "model_id": model_id,
        "finish_reason": response.finish_reason,
        "usage": response.usage,
        "transport_attempts": response.transport_attempts,
    }


def _privacy_class(base_url: str, fallback: str) -> str:
    from urllib.parse import urlsplit

    host = (urlsplit(base_url).hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return "local"
    return fallback


def _handle_provider_setup(args: argparse.Namespace) -> int:
    preset = provider_preset(args.preset_id)
    if not preset.installable:
        raise ValueError(
            f"provider preset requires a specialized adapter: {preset.display_name}"
        )
    base_url = (args.base_url or preset.base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError(f"{preset.display_name} requires --base-url")
    provider_id = (args.provider_id or preset.preset_id).strip()
    model_id = args.model.strip()
    if not model_id:
        raise ValueError("--model is required")
    secret: Optional[str] = None
    probe_secret: Optional[str] = None
    if args.stdin_key:
        secret = sys.stdin.readline().rstrip("\r\n")
        if not secret.strip():
            raise ValueError("stdin did not contain an API key")
        probe_secret = secret
    elif args.credential_ref:
        probe_secret = CredentialStore().resolve(args.credential_ref)
    verification: dict[str, Any] = {"status": "skipped"}
    if not args.no_test:
        if probe_secret is None:
            raise ValueError("a live test requires --stdin-key or --credential-ref")
        key = probe_secret

        verification = _probe_candidate(
            str(preset.adapter_kind),
            base_url,
            model_id,
            credential=lambda: key,
            timeout_seconds=60.0,
        )
    controller = _controller()
    provider = ProviderRecord(
        provider_id=provider_id,
        adapter_kind=str(preset.adapter_kind),
        base_url=base_url,
        credential_ref=args.credential_ref,
        privacy_class=_privacy_class(base_url, preset.privacy_class),
    )
    model = ModelRecord(
        provider_id=provider_id,
        model_id=model_id,
        context_window=args.context_window,
        max_output_tokens=args.max_output_tokens,
        tools="true",
        streaming="true",
        provenance=f"preset-setup:{preset.preset_id}",
    )
    mutation = controller.configure_provider_model(
        provider,
        model,
        secret=secret,
        activate=not args.no_activate,
    )
    payload = {
        **mutation.to_dict(),
        "preset": preset.preset_id,
        "verification": verification,
    }
    _emit(payload, json_output=args.json)
    return 0


def _handle_provider(args: argparse.Namespace) -> int:
    controller = _controller()
    command = args.provider_command
    if command == "presets":
        _emit([item.to_dict() for item in provider_presets()], json_output=args.json)
        return 0
    if command == "setup":
        return _handle_provider_setup(args)
    if command == "add":
        provider = ProviderRecord(
            provider_id=args.provider_id,
            adapter_kind=args.adapter,
            base_url=args.base_url,
            credential_ref=args.credential_ref,
            privacy_class=args.privacy_class,
            timeout_seconds=args.timeout_seconds,
            max_transport_retries=args.max_transport_retries,
            headers=_pairs(args.header, "header"),
            query=_pairs(args.query, "query"),
        )
        _emit(controller.put_provider(provider).to_dict(), json_output=args.json)
        return 0
    if command == "edit":
        mutation = controller.edit_provider(
            args.provider_id,
            adapter_kind=args.adapter,
            base_url=args.base_url,
            credential_ref=args.credential_ref,
            credential_ref_supplied=bool(args.credential_ref),
            privacy_class=args.privacy_class,
            timeout_seconds=args.timeout_seconds,
            max_transport_retries=args.max_transport_retries,
        )
        _emit(mutation.to_dict(), json_output=args.json)
        return 0
    if command == "remove":
        mutation = controller.remove_provider(
            args.provider_id,
            cascade=bool(args.cascade),
            delete_credential=bool(args.delete_credential),
        )
        _emit(mutation.to_dict(), json_output=args.json)
        return 0
    if command == "list":
        _emit([item.to_dict() for item in controller.list()], json_output=args.json)
        return 0
    if command in {"show", "details"}:
        _emit(controller.details(args.provider_id).to_dict(), json_output=args.json)
        return 0
    if command == "test":
        _emit(
            controller.test_provider(args.provider_id, model_or_alias=args.model),
            json_output=args.json,
        )
        return 0
    if command == "credential-set":
        secret = (
            sys.stdin.readline().rstrip("\r\n")
            if args.stdin
            else getpass.getpass("Provider credential: ")
        )
        mutation = controller.set_credential(
            args.provider_id, secret, credential_name=args.name
        )
        _emit(mutation.to_dict(), json_output=args.json)
        return 0
    mutation = controller.clear_credential(
        args.provider_id, delete_stored=bool(args.delete_stored)
    )
    _emit(mutation.to_dict(), json_output=args.json)
    return 0


# --------------------------------------------------------------------- models


def _handle_model(args: argparse.Namespace) -> int:
    controller = _controller()
    registry = controller.registry
    command = args.model_command
    if command == "list":
        selected = registry.selected_model()
        payload = []
        for item in registry.models(args.provider):
            value = asdict(item)
            value["selected"] = selected is not None and (
                item.provider_id,
                item.model_id,
            ) == (selected.provider_id, selected.model_id)
            payload.append(value)
        _emit(payload, json_output=args.json)
        return 0
    if command == "inspect":
        _emit(asdict(registry.model(args.provider_id, args.model)), json_output=args.json)
        return 0
    if command == "select":
        _emit(
            controller.select_model(args.provider_id, args.model).to_dict(),
            json_output=args.json,
        )
        return 0
    if command == "add":
        record = ModelRecord(
            provider_id=args.provider_id,
            model_id=args.model_id,
            aliases=tuple(args.alias),
            context_window=args.context_window,
            max_output_tokens=args.max_output_tokens,
            tools=args.tools,
            vision=args.vision,
            structured_output=args.structured_output,
            streaming=args.streaming,
            provenance=args.provenance,
        )
        _emit(controller.put_model(record).to_dict(), json_output=args.json)
        return 0
    if command == "remove":
        _emit(
            controller.remove_model(args.provider_id, args.model).to_dict(),
            json_output=args.json,
        )
        return 0
    _emit(
        controller.test_provider(args.provider_id, model_or_alias=args.model),
        json_output=args.json,
    )
    return 0


# ------------------------------------------------------------------------ MCP


def _handle_mcp_server(args: argparse.Namespace) -> int:
    registry = _mcp_registry()
    command = args.mcp_server_command
    if command == "add":
        record = McpServerRecord(
            server_id=args.server_id,
            namespace=args.namespace,
            transport=args.transport,
            command=args.mcp_stdio_command,
            args=tuple(args.arg),
            url=args.url,
            environment=_pairs(args.env, "MCP environment"),
            headers=_pairs(args.header, "MCP header"),
            credential_ref=args.credential_ref,
            credential_target=args.credential_target,
            credential_scheme=args.credential_scheme,
            oauth=bool(args.oauth),
            read_only_tools=tuple(args.read_only_tool),
            timeout_seconds=args.timeout_seconds,
            max_result_bytes=args.max_result_bytes,
            max_message_bytes=args.max_message_bytes,
            max_transport_retries=args.max_transport_retries,
        )
        _emit(registry.put(record).to_dict(), json_output=args.json)
        return 0
    if command == "remove":
        removed = registry.remove(args.server_id)
        _emit(
            {"server_id": removed.server_id, "status": "removed"},
            json_output=args.json,
        )
        return 0
    if command == "list":
        _emit([item.to_dict() for item in registry.list()], json_output=args.json)
        return 0
    if command == "show":
        _emit(registry.get(args.server_id).to_dict(), json_output=args.json)
        return 0
    if command == "authorize":
        record = registry.get(args.server_id)
        if not record.oauth or record.transport != "streamable_http" or record.url is None:
            raise McpConfigurationError(
                f"MCP server is not configured for OAuth Streamable HTTP: {record.server_id}"
            )
        from .mcp_oauth import authorize_mcp_oauth

        authorization = authorize_mcp_oauth(
            record.server_id,
            record.url,
            timeout_seconds=max(60.0, record.timeout_seconds),
            force=bool(args.force),
        )
        _emit(
            {
                "server_id": record.server_id,
                "status": "authorized",
                "server_name": authorization.server_name,
                "tool_count": len(authorization.tool_names),
                "tools": list(authorization.tool_names),
            },
            json_output=args.json,
        )
        return 0
    repository = _mcp_repository(args.repository)
    record = registry.get(args.server_id)
    tools = McpClient(registry).discover_record(record, repository)
    payload: dict[str, Any] = {
        "server": record.to_dict(),
        "status": "ok",
        "tool_count": len(tools),
        "tools": [item.to_dict() for item in tools],
    }
    if command == "doctor":
        payload = {
            "server_id": record.server_id,
            "transport": record.transport,
            "status": "ok",
            "tool_count": len(tools),
        }
    _emit(payload, json_output=args.json)
    return 0


def _handle_mcp_session(args: argparse.Namespace) -> int:
    repository = _mcp_repository(args.repository)
    store = SessionStore(session_dir())
    command = args.mcp_session_command
    if command == "permissions":
        record = store.load(args.session_id)
        store.validate_repository(record, repository)
        selection = _stored_mcp(record, args.server_id)
        if selection is None:
            raise McpAccessDenied(f"server is not selected: {args.server_id}")
        _emit(selection, json_output=args.json)
        return 0
    if command == "deselect":
        with store.mutate(
            args.session_id, f"mcp-deselect-{os.getpid()}", ttl_seconds=5.0
        ) as record:
            store.validate_repository(record, repository)
            removed = _replace_stored_mcp(record, args.server_id, None)
        _emit(
            {
                "server_id": args.server_id,
                "session_id": args.session_id,
                "status": "deselected" if removed else "not_selected",
            },
            json_output=args.json,
        )
        return 0
    registry = _mcp_registry()
    server = registry.get(args.server_id)
    tools = McpClient(registry).discover_record(server, repository)
    decisions = _pairs(args.permission, "MCP permission")
    invalid = {
        name: value
        for name, value in decisions.items()
        if value not in {"allow", "ask", "deny"}
    }
    if invalid:
        raise ValueError("MCP permission decisions must use allow, ask, or deny")
    with store.mutate(
        args.session_id, f"mcp-select-{os.getpid()}", ttl_seconds=5.0
    ) as record:
        store.validate_repository(record, repository)
        selection = mcp_selection(
            server,
            tools,
            decisions,
            previous=_stored_mcp(record, server.server_id),
        )
        _replace_stored_mcp(record, server.server_id, selection)
    _emit(
        {"session_id": args.session_id, "selection": selection},
        json_output=args.json,
    )
    return 0


def _handle_mcp_status(args: argparse.Namespace) -> int:
    registry = _mcp_registry()
    servers = registry.list()
    try:
        credentials: Optional[McpCredentialStore] = McpCredentialStore()
    except CredentialError:
        credentials = None
    selections: list[Any] = []
    if args.session_id:
        repository = _mcp_repository(args.repository)
        store = SessionStore(session_dir())
        session = store.load(args.session_id)
        store.validate_repository(session, repository)
        selections = list(session.mcp_servers)
    liveness: dict[str, McpLiveness] = {}
    if args.probe:
        repository = _mcp_repository(args.repository)
        client = McpClient(registry, credentials)
        for server in servers:
            checked_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            try:
                tools = client.discover_record(server, repository)
            except (McpError, CredentialError) as exc:
                kind = getattr(exc, "kind", None)
                liveness[server.server_id] = McpLiveness(
                    LIVENESS_FAILED,
                    failure_kind=str(getattr(kind, "value", kind)) if kind else None,
                    detail=str(exc),
                    checked_at=checked_at,
                )
            else:
                liveness[server.server_id] = McpLiveness(
                    LIVENESS_LIVE,
                    tool_count=len(tools),
                    checked_at=checked_at,
                )
    _emit(
        build_mcp_status(
            servers,
            session_id=args.session_id,
            selections=selections,
            resolve_credential=credentials.resolve if credentials is not None else None,
            liveness=liveness,
        ),
        json_output=args.json,
    )
    return 0


def _handle_mcp_call(args: argparse.Namespace) -> int:
    repository = _mcp_repository(args.repository)

    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    try:
        arguments = json.loads(args.arguments, parse_constant=reject_constant)
    except ValueError as exc:
        raise ValueError(f"MCP arguments are not valid JSON: {exc}") from exc
    if not isinstance(arguments, dict):
        raise ValueError("MCP arguments must be a JSON object")
    store = SessionStore(session_dir())
    record = store.load(args.session_id)
    store.validate_repository(record, repository)
    binding, descriptors = _selected_mcp_runtime(record, repository)
    if binding is None:
        raise McpAccessDenied("session has no selected MCP servers")
    matches = [item for item in descriptors if item.name == args.tool]
    if len(matches) != 1:
        raise McpAccessDenied(f"MCP tool is not selected: {args.tool}")
    descriptor = matches[0]
    if descriptor.mutates and not args.idempotency_key:
        raise ValueError("mutating MCP calls require --idempotency-key")
    profile = AccessProfile(record.access_profile)
    policy = CapabilityPolicy(profile)
    origin = Origin(OriginKind.USER, f"cli-mcp-{record.session_id}")
    policy.set_grants(origin, {Capability.MCP_CALL})
    core = CoreRuntime(
        repository,
        policy,
        store,
        runtime_dir() / "vnext" / "audit.jsonl",
        mcp_binding=binding,
    )
    command = CoreCommand(
        name=descriptor.name,
        arguments=arguments,
        session_id=record.session_id,
        origin=origin,
        idempotency_key=args.idempotency_key,
        deadline_seconds=args.deadline_seconds,
    )
    result = core.execute(command)
    _emit(result.to_dict() if hasattr(result, "to_dict") else result, json_output=args.json)
    return 0


def _handle_mcp_credential(args: argparse.Namespace) -> int:
    store = McpCredentialStore()
    command = args.mcp_credential_command
    if command == "set":
        secret = (
            sys.stdin.readline().rstrip("\r\n")
            if args.stdin
            else getpass.getpass("MCP credential: ")
        )
        payload = store.set(args.name, secret)
    elif command == "show":
        reference = f"os-keyring:mcp/{args.name}"
        secret = store.resolve(reference)
        payload = {
            "reference": reference,
            "fingerprint": store.fingerprint(secret),
            "status": "available",
        }
    elif command == "delete":
        payload = store.delete(args.name)
    else:
        payload = store.doctor()
    _emit(payload, json_output=args.json)
    return 0


def _handle_mcp(args: argparse.Namespace) -> int:
    if args.mcp_command == "server":
        return _handle_mcp_server(args)
    if args.mcp_command == "session":
        return _handle_mcp_session(args)
    if args.mcp_command == "credential":
        return _handle_mcp_credential(args)
    if args.mcp_command == "status":
        return _handle_mcp_status(args)
    return _handle_mcp_call(args)


# --------------------------------------------------------------------- skills


def _handle_skill(args: argparse.Namespace) -> int:
    repository = args.repository.expanduser().resolve(strict=True)
    catalog = SkillCatalog(repository, extra_directories=tuple(args.skill_dir))
    command = args.skill_command
    payload: dict[str, Any]
    if command == "list":
        payload = {
            "skills": [item.to_dict() for item in catalog.discover()],
            "diagnostics": [item.to_dict() for item in catalog.diagnostics],
        }
    elif command == "show":
        payload = {
            "skill": catalog.get(args.name).to_dict(),
            "diagnostics": [item.to_dict() for item in catalog.diagnostics],
        }
    else:
        payload = {
            "skill": catalog.load(args.name).to_dict(),
            "diagnostics": [item.to_dict() for item in catalog.diagnostics],
        }
    _emit(payload, json_output=args.json)
    return 0


# ---------------------------------------------------------------- credentials


def _handle_credential(args: argparse.Namespace) -> int:
    store = CredentialStore()
    if args.credential_command == "set":
        secret = (
            sys.stdin.readline().rstrip("\r\n")
            if args.stdin
            else getpass.getpass("Provider credential: ")
        )
        payload = store.set(args.name, secret)
    elif args.credential_command == "show":
        reference = f"os-keyring:provider/{args.name}"
        secret = store.resolve(reference)
        payload = {
            "reference": reference,
            "fingerprint": store.fingerprint(secret),
            "status": "available",
        }
    elif args.credential_command == "delete":
        payload = store.delete(args.name)
    else:
        try:
            payload = store.doctor(getattr(args, "reference", None))
        except CredentialError as exc:
            payload = {
                "status": "unavailable",
                "error": str(exc),
                "recovery": "Run 'karox credential setup'.",
            }
    _emit(payload, json_output=args.json)
    return 0 if payload.get("status") not in {"unavailable", "missing"} else 1


# ---------------------------------------------------------------------- parser


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="karox",
        description="Local coding agent with a compact terminal client.",
    )
    parser.add_argument("--version", action="version", version=f"karox {__version__}")
    commands = parser.add_subparsers(dest="command")

    tui = commands.add_parser("tui", help="run the compact terminal client")
    tui.add_argument("--session-id")
    tui.add_argument("--repository", type=Path, default=Path.cwd())
    tui.add_argument("--language", choices=("ru", "en"))

    agent = commands.add_parser("agent", help="run the native agent")
    agents = agent.add_subparsers(dest="agent_command", required=True)
    run = agents.add_parser("run", help="run or resume a session")
    run.add_argument("--repository", type=Path, default=Path.cwd())
    run.add_argument("--task", required=True)
    run.add_argument("--model")
    run.add_argument("--base-url")
    run.add_argument("--api-key-env")
    run.add_argument("--session-id")
    run.add_argument("--continue-task", action="store_true")
    run.add_argument(
        "--verification-command",
        action="append",
        default=[],
        help="approved check argv as JSON or a shell-quoted line (repeatable)",
    )
    run.add_argument(
        "--access-profile",
        choices=[item.value for item in AccessProfile],
    )
    run.add_argument(
        "--effort-level",
        default="auto",
        choices=("auto", "low", "medium", "high", "extra-high", "ultra"),
    )
    run.add_argument("--mode", default="build", choices=("build", "plan", "ideate"))
    run.add_argument("--expect", choices=("auto", "change"), default="auto")
    run.add_argument("--max-steps", type=int)
    run.add_argument("--max-seconds", type=float)
    run.add_argument("--context-window", type=int)
    run.add_argument("--max-output-tokens", type=int)
    run.add_argument("--stream", action="store_true")
    run.add_argument("--stream-json", action="store_true")
    run.add_argument("--json", action="store_true")

    session = commands.add_parser("session", help="manage durable sessions")
    sessions = session.add_subparsers(dest="session_command", required=True)
    create = sessions.add_parser("create")
    create.add_argument("--repository", type=Path, default=Path.cwd())
    create.add_argument("--task", required=True)
    create.add_argument(
        "--access-profile",
        choices=[item.value for item in AccessProfile],
        default=AccessProfile.WORKSPACE_WRITE.value,
    )
    create.add_argument("--branch", default="")
    create.add_argument("--id")
    create.add_argument("--name", default="")
    create.add_argument("--json", action="store_true")
    listing = sessions.add_parser("list")
    listing.add_argument("--all", action="store_true")
    listing.add_argument("--json", action="store_true")
    show = sessions.add_parser("show")
    show.add_argument("session_id")
    show.add_argument("--json", action="store_true")
    fork = sessions.add_parser("fork")
    fork.add_argument("session_id")
    fork.add_argument("--new-session-id")
    fork.add_argument("--task")
    fork.add_argument("--name")
    fork.add_argument("--json", action="store_true")
    rename = sessions.add_parser("rename")
    rename.add_argument("session_id")
    rename.add_argument("--name", required=True)
    rename.add_argument("--json", action="store_true")
    for name in ("archive", "unarchive", "revoke"):
        item = sessions.add_parser(name)
        item.add_argument("session_id")
        item.add_argument("--json", action="store_true")
    delete = sessions.add_parser("delete")
    delete.add_argument("session_id")
    delete.add_argument("--confirm", required=True)
    delete.add_argument("--json", action="store_true")
    lock = sessions.add_parser("lock")
    lock.add_argument("session_id")
    lock.add_argument("--json", action="store_true")

    provider = commands.add_parser("provider", help="manage API providers")
    providers = provider.add_subparsers(dest="provider_command", required=True)
    providers.add_parser("presets").add_argument("--json", action="store_true")
    add = providers.add_parser("add")
    add.add_argument("provider_id")
    add.add_argument("--adapter", required=True)
    add.add_argument("--base-url", required=True)
    add.add_argument("--credential-ref")
    add.add_argument("--privacy-class", default="public")
    add.add_argument("--timeout-seconds", type=float, default=60.0)
    add.add_argument("--max-transport-retries", type=int, default=2)
    add.add_argument("--header", action="append", default=[])
    add.add_argument("--query", action="append", default=[])
    add.add_argument("--json", action="store_true")
    edit = providers.add_parser("edit")
    edit.add_argument("provider_id")
    edit.add_argument("--adapter")
    edit.add_argument("--base-url")
    edit.add_argument("--credential-ref")
    edit.add_argument("--privacy-class")
    edit.add_argument("--timeout-seconds", type=float)
    edit.add_argument("--max-transport-retries", type=int)
    edit.add_argument("--json", action="store_true")
    remove = providers.add_parser("remove")
    remove.add_argument("provider_id")
    remove.add_argument("--cascade", action="store_true")
    remove.add_argument("--delete-credential", action="store_true")
    remove.add_argument("--json", action="store_true")
    providers.add_parser("list").add_argument("--json", action="store_true")
    for name in ("show", "details"):
        item = providers.add_parser(name)
        item.add_argument("provider_id")
        item.add_argument("--json", action="store_true")
    test = providers.add_parser("test")
    test.add_argument("provider_id")
    test.add_argument("--model")
    test.add_argument("--json", action="store_true")
    credential_set = providers.add_parser("credential-set")
    credential_set.add_argument("provider_id")
    credential_set.add_argument("--name")
    credential_set.add_argument("--stdin", action="store_true")
    credential_set.add_argument("--json", action="store_true")
    credential_clear = providers.add_parser("credential-clear")
    credential_clear.add_argument("provider_id")
    credential_clear.add_argument("--delete-stored", action="store_true")
    credential_clear.add_argument("--json", action="store_true")
    setup = providers.add_parser("setup")
    setup.add_argument("preset_id")
    setup.add_argument("--provider-id")
    setup.add_argument("--base-url")
    setup.add_argument("--model", required=True)
    setup.add_argument("--context-window", type=int)
    setup.add_argument("--max-output-tokens", type=int)
    group = setup.add_mutually_exclusive_group()
    group.add_argument("--credential-ref")
    group.add_argument("--stdin-key", action="store_true")
    setup.add_argument("--no-test", action="store_true")
    setup.add_argument("--no-activate", action="store_true")
    setup.add_argument("--json", action="store_true")

    model = commands.add_parser("model", help="manage provider models")
    models = model.add_subparsers(dest="model_command", required=True)
    listing = models.add_parser("list")
    listing.add_argument("--provider")
    listing.add_argument("--json", action="store_true")
    inspect = models.add_parser("inspect")
    inspect.add_argument("provider_id")
    inspect.add_argument("model")
    inspect.add_argument("--json", action="store_true")
    select = models.add_parser("select")
    select.add_argument("provider_id")
    select.add_argument("model")
    select.add_argument("--json", action="store_true")
    add_model = models.add_parser("add")
    add_model.add_argument("provider_id")
    add_model.add_argument("model_id")
    add_model.add_argument("--alias", action="append", default=[])
    add_model.add_argument("--context-window", type=int)
    add_model.add_argument("--max-output-tokens", type=int)
    for capability in ("tools", "vision", "structured-output", "streaming"):
        add_model.add_argument(
            f"--{capability}", choices=("true", "false", "unknown"), default="unknown"
        )
    add_model.add_argument("--provenance", default="manual")
    add_model.add_argument("--json", action="store_true")
    remove_model = models.add_parser("remove")
    remove_model.add_argument("provider_id")
    remove_model.add_argument("model")
    remove_model.add_argument("--json", action="store_true")
    test_model = models.add_parser("test")
    test_model.add_argument("provider_id")
    test_model.add_argument("model")
    test_model.add_argument("--json", action="store_true")

    mcp = commands.add_parser("mcp", help="manage MCP servers and tools")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)
    server = mcp_commands.add_parser("server")
    servers = server.add_subparsers(dest="mcp_server_command", required=True)
    server_add = servers.add_parser("add")
    server_add.add_argument("server_id")
    server_add.add_argument("--namespace", required=True)
    server_add.add_argument("--transport", choices=("stdio", "streamable_http"), required=True)
    server_add.add_argument("--command", dest="mcp_stdio_command")
    server_add.add_argument("--arg", action="append", default=[])
    server_add.add_argument("--url")
    server_add.add_argument("--env", action="append", default=[])
    server_add.add_argument("--header", action="append", default=[])
    server_add.add_argument("--credential-ref")
    server_add.add_argument("--credential-target")
    server_add.add_argument("--credential-scheme", default="Bearer")
    server_add.add_argument("--oauth", action="store_true")
    server_add.add_argument("--read-only-tool", action="append", default=[])
    server_add.add_argument("--timeout-seconds", type=float, default=30.0)
    server_add.add_argument("--max-result-bytes", type=int, default=1_000_000)
    server_add.add_argument("--max-message-bytes", type=int, default=1_000_000)
    server_add.add_argument("--max-transport-retries", type=int, default=1)
    server_add.add_argument("--json", action="store_true")
    for name in ("remove", "show", "authorize", "inspect", "doctor", "list"):
        item = servers.add_parser(name)
        if name != "list":
            item.add_argument("server_id")
        if name == "authorize":
            item.add_argument("--force", action="store_true")
        if name in {"inspect", "doctor"}:
            item.add_argument("--repository", type=Path, default=Path.cwd())
        item.add_argument("--json", action="store_true")
    mcp_session = mcp_commands.add_parser("session")
    mcp_sessions = mcp_session.add_subparsers(dest="mcp_session_command", required=True)
    select_server = mcp_sessions.add_parser("select")
    select_server.add_argument("server_id")
    select_server.add_argument("--session-id", required=True)
    select_server.add_argument("--repository", type=Path, default=Path.cwd())
    select_server.add_argument("--permission", action="append", default=[])
    select_server.add_argument("--json", action="store_true")
    for name in ("deselect", "permissions"):
        item = mcp_sessions.add_parser(name)
        item.add_argument("server_id")
        item.add_argument("--session-id", required=True)
        item.add_argument("--repository", type=Path, default=Path.cwd())
        item.add_argument("--json", action="store_true")
    status = mcp_commands.add_parser("status")
    status.add_argument("--session-id")
    status.add_argument("--repository", type=Path, default=Path.cwd())
    status.add_argument("--probe", action="store_true")
    status.add_argument("--json", action="store_true")
    call = mcp_commands.add_parser("call")
    call.add_argument("tool")
    call.add_argument("--session-id", required=True)
    call.add_argument("--repository", type=Path, default=Path.cwd())
    call.add_argument("--arguments", default="{}")
    call.add_argument("--idempotency-key")
    call.add_argument("--deadline-seconds", type=float, default=120.0)
    call.add_argument("--json", action="store_true")
    mcp_credential = mcp_commands.add_parser("credential")
    mcp_credentials = mcp_credential.add_subparsers(dest="mcp_credential_command", required=True)
    for name in ("set", "show", "delete", "doctor"):
        item = mcp_credentials.add_parser(name)
        if name != "doctor":
            item.add_argument("name")
        if name == "set":
            item.add_argument("--stdin", action="store_true")
        item.add_argument("--json", action="store_true")

    skill = commands.add_parser("skill", help="discover and load Skills")
    skills = skill.add_subparsers(dest="skill_command", required=True)
    skills_list = skills.add_parser("list")
    skills_list.add_argument("--repository", type=Path, default=Path.cwd())
    skills_list.add_argument("--skill-dir", type=Path, action="append", default=[])
    skills_list.add_argument("--json", action="store_true")
    for name in ("show", "load"):
        item = skills.add_parser(name)
        item.add_argument("name")
        item.add_argument("--repository", type=Path, default=Path.cwd())
        item.add_argument("--skill-dir", type=Path, action="append", default=[])
        item.add_argument("--json", action="store_true")

    credential = commands.add_parser("credential", help="manage provider secrets")
    credentials = credential.add_subparsers(dest="credential_command", required=True)
    for name in ("set", "show", "delete", "doctor"):
        item = credentials.add_parser(name)
        if name != "doctor":
            item.add_argument("name")
        if name == "set":
            item.add_argument("--stdin", action="store_true")
        if name == "doctor":
            item.add_argument("--reference")
        item.add_argument("--json", action="store_true")

    commands.add_parser("paths", help="show resolved application paths")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    parser = _parser()
    if not arguments:
        if sys.stdin.isatty() and sys.stdout.isatty():
            from .tui import run_tui

            return run_tui(repository=str(Path.cwd().resolve()))
        parser.print_help()
        return 0
    args = parser.parse_args(arguments)
    try:
        if args.command == "tui":
            from .tui import run_tui

            return run_tui(
                session_id=args.session_id,
                repository=str(args.repository.expanduser().resolve()),
                language=args.language,
            )
        if args.command == "agent":
            return _handle_agent_run(args)
        if args.command == "session":
            return _handle_session(args)
        if args.command == "provider":
            return _handle_provider(args)
        if args.command == "model":
            return _handle_model(args)
        if args.command == "mcp":
            return _handle_mcp(args)
        if args.command == "skill":
            return _handle_skill(args)
        if args.command == "credential":
            return _handle_credential(args)
        if args.command == "paths":
            _emit(
                {
                    "config": str(config_dir()),
                    "runtime": str(runtime_dir()),
                    "sessions": str(session_dir()),
                },
                json_output=False,
            )
            return 0
        parser.print_help()
        return 0
    except (
        AgentError,
        CoreError,
        CredentialError,
        McpError,
        RegistryError,
        SessionError,
        SkillError,
        ValueError,
        OSError,
    ) as exc:
        print(f"karox: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())