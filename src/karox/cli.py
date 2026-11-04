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
import re
import shlex
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, Sequence

from . import __version__
from .action_execution import CapabilityCoreRuntime
from .cli_parser import _parser
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
from .agent_modes import mode_policy, mode_prompt_delta
from .bridge_profiles import WEB_BRIDGE_PROFILES
from .core import CoreCommand, CoreError, CoreRuntime
from .credentials import CredentialError, CredentialStore
from .disk_maintenance import is_drive_root, workspace_system_reason
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
    ProviderError,
)
from .registry import (
    ModelPricing,
    ModelRecord,
    ProviderRecord,
    ProviderRegistry,
    RegistryError,
)
from .routing import RouteTarget, RoutingPolicy, RoutedProvider
from .risk_engine import RiskEngine
from .security import redact
from .sessions import SessionError, SessionStore
from .skill_runtime import SkillSession, SkillSessionRuntime
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


_SHORT_COMMANDS = {
    "models": ("model", "list"),
    "providers": ("provider", "list"),
    "sessions": ("session", "list"),
    "skills": ("skill", "list"),
}
_DEFAULT_LIST_GROUPS = frozenset({"model", "provider", "session", "skill"})


def _normalize_argv(arguments: list[str]) -> list[str]:
    """Accept the short daily forms without a second command tree."""

    if not arguments:
        return arguments
    head = arguments[0]
    short = _SHORT_COMMANDS.get(head)
    if short is not None and len(arguments) == 1:
        return list(short)
    if head in _DEFAULT_LIST_GROUPS:
        if len(arguments) == 1:
            return [head, "list"]
        if arguments[1].startswith("-") and arguments[1] not in {"-h", "--help"}:
            return [head, "list", *arguments[1:]]
    return arguments


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


def _pricing(args: argparse.Namespace) -> Optional[ModelPricing]:
    values = (
        getattr(args, "pricing_version", None),
        getattr(args, "currency", None),
        getattr(args, "input_per_million", None),
        getattr(args, "output_per_million", None),
        getattr(args, "pricing_source", None),
    )
    if all(value is None for value in values):
        return None
    if any(value is None for value in values):
        raise ValueError(
            "pricing requires version, currency, input/output prices, and source"
        )
    return ModelPricing(
        args.pricing_version,
        args.currency,
        args.input_per_million,
        args.output_per_million,
        args.pricing_source,
        cache_read_per_million=getattr(args, "cache_read_per_million", None),
        cache_write_per_million=getattr(args, "cache_write_per_million", None),
    )


def _load_pricing_registry() -> Any:
    """Load the versioned pricing document; absence means unknown, not zero."""

    path = session_dir() / "pricing.json"
    try:
        from .provider_pricing import PricingRegistry

        registry = PricingRegistry.from_path(path)
    except ValueError as exc:
        print(f"warning: ignoring invalid pricing document {path}: {exc}", file=sys.stderr)
        return None
    return registry if len(registry) > 0 else None


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
            emit(event.reasoning_summary_delta or "")
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
            detail = event.detail or ""
            if "\t" in detail:
                detail = f"{len(detail.splitlines())} items"
            line(f"  -> {event.tool} {detail}".rstrip())
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
    costs = report.usage.get("costs")
    if isinstance(costs, dict) and costs:
        _write("costs: " + ", ".join(f"{key}={value}" for key, value in costs.items()) + "\n")
    if report.usage.get("cost_status") == "unknown":
        reason = str(report.usage.get("cost_unknown_reason") or "")
        detail = {
            "usage_missing": "the provider did not report usage",
            "no_pricing": "no pricing record for this model",
        }.get(reason, reason or "reason unavailable")
        suffix = "; the measured total is incomplete" if costs else ""
        _write(f"costs: unknown ({detail}){suffix}\n")
    if report.evidence:
        _write(f"evidence records: {len(report.evidence)}\n")
    if report.answer_basis:
        _write(f"answer basis: {len(report.answer_basis)}\n")
    sources = report.project_context.get("sources") if report.project_context else None
    if sources:
        names = [
            str(item.get("path")) if isinstance(item, dict) else str(item)
            for item in sources
        ]
        _write("project instructions: " + ", ".join(names) + "\n")
    if report.compaction:
        _write("context compacted: " + json.dumps(report.compaction, ensure_ascii=False) + "\n")
    if report.restored:
        _write(f"restored: {report.restored}\n")
    if report.unmet:
        _write("unmet: " + "; ".join(report.unmet) + "\n")
    if report.failure_map:
        from .check_report import render_failure_map

        rendered = render_failure_map(report.failure_map)
        if rendered:
            _write(rendered + "\n")
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


def _route(value: str) -> RouteTarget:
    if "/" not in value:
        raise ValueError("route must use provider/model")
    provider_id, model = value.split("/", 1)
    return RouteTarget(provider_id, model)


def _run_vision_capability(args: argparse.Namespace) -> bool:
    """Whether the model this run talks to may be shown images.

    The routed model decides, not the registry selection: a run sent to a
    text-only model must not offer visual.check. An ``unknown`` capability
    inherits the selected model's flag because the checker already degrades to
    measurements when a provider rejects images, and refusing every model the
    registry has not classified would remove the check from most custom routes.

    Direct ``--model/--base-url`` runs stay off in ``auto`` because nothing
    there says the endpoint accepts image parts. A direct run that knows its
    endpoint is vision-capable can opt in with ``--vision on``; this is what
    lets a custom OpenAI-compatible vision model use visual.check instead of
    ending every page task as ``unproducible_requirement``.
    """

    override = str(getattr(args, "vision", "auto") or "auto").lower()
    if override == "on":
        return True
    if override == "off":
        return False
    registry = _registry()
    direct = bool(args.model or args.base_url or args.api_key_env)
    routes = tuple(_route(value) for value in (getattr(args, "route", ()) or ()))
    if routes:
        provider_id, model_id = routes[0].provider_id, routes[0].model
        try:
            record = registry.model(provider_id, model_id)
        except Exception:
            record = None
        if record is not None and record.vision in {"true", "false"}:
            return record.vision == "true"
    elif direct:
        return False
    selected = registry.selected_model()
    return selected is not None and selected.vision == "true"


def _budget_currency(registry: Any, routes: Sequence[RouteTarget]) -> str:
    """The currency a ``--max-cost`` given without ``--currency`` is meant in.

    A budget is only enforceable on priced models, so an unpriced route is
    reported here, before a session starts, with the command that fixes it,
    instead of failing the first model call with a bare "requires pricing".
    """

    unpriced: list[str] = []
    currencies: set[str] = set()
    for target in routes:
        pricing = registry.model(target.provider_id, target.model).pricing
        if pricing is None:
            unpriced.append(f"{target.provider_id}/{target.model}")
        else:
            currencies.add(pricing.currency)
    if unpriced:
        raise ValueError(
            "--max-cost needs model prices and "
            + ", ".join(unpriced)
            + " has none; run karox provider catalog-sync to fill them from the "
            "catalog, or set them with karox model add ... --input-per-million "
            "--output-per-million"
        )
    if len(currencies) != 1:
        raise ValueError(
            "--max-cost needs --currency: the routes are priced in "
            + ", ".join(sorted(currencies))
        )
    return currencies.pop()


def _resolve_runtime(
    args: argparse.Namespace,
    *,
    timeout_seconds: float,
    record: Any = None,
) -> tuple[Any, str, Optional[int], Optional[int], bool]:
    """Return provider, model id, context window, output ceiling, bypass."""

    provider: Any
    direct = bool(args.model or args.base_url or args.api_key_env)
    routed_options = [
        name
        for name, supplied in (
            ("--route", bool(getattr(args, "route", None))),
            ("--privacy-limit", getattr(args, "privacy_limit", None) is not None),
            ("--max-total-tokens", getattr(args, "max_total_tokens", None) is not None),
            ("--max-cost", getattr(args, "max_cost", None) is not None),
            ("--currency", getattr(args, "currency", None) is not None),
        )
        if supplied
    ]
    if direct:
        if routed_options:
            raise ValueError(
                "direct provider mode cannot use routed option(s): "
                + ", ".join(routed_options)
            )
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
    routes = tuple(_route(value) for value in getattr(args, "route", ()))
    if not routes:
        selected = registry.selected_model()
        if selected is None:
            raise ValueError(
                "no model is selected; run karox model select or provider setup"
            )
        routes = (RouteTarget(selected.provider_id, selected.model_id),)
    windows: list[int] = []
    ceilings: list[int] = []
    for target in routes:
        registry.provider(target.provider_id)
        entry = registry.model(target.provider_id, target.model)
        if isinstance(entry.context_window, int) and entry.context_window > 0:
            windows.append(entry.context_window)
        if isinstance(entry.max_output_tokens, int) and entry.max_output_tokens > 0:
            ceilings.append(entry.max_output_tokens)
    context_window = (
        min(windows) if windows and len(windows) == len(routes) else None
    )
    max_output_tokens = (
        min(ceilings) if ceilings and len(ceilings) == len(routes) else None
    )
    usage = record.usage if record is not None and isinstance(record.usage, dict) else {}
    costs = usage.get("costs")
    max_cost = getattr(args, "max_cost", None)
    currency = getattr(args, "currency", None)
    if max_cost is not None and currency is None:
        currency = _budget_currency(registry, routes)
    provider = RoutedProvider(
        registry,
        ProviderFactory(),
        RoutingPolicy(
            routes=routes,
            privacy_limit=getattr(args, "privacy_limit", None) or "public",
            route_strategy=getattr(args, "route_strategy", "ordered"),
            max_total_tokens=getattr(args, "max_total_tokens", None),
            max_cost=max_cost,
            currency=currency,
        ),
        initial_usage=usage,
        initial_costs=costs if isinstance(costs, dict) else {},
    )
    first = registry.provider(routes[0].provider_id)
    return (
        provider,
        routes[0].model,
        args.context_window or context_window,
        args.max_output_tokens or max_output_tokens,
        bool(first.bypass),
    )


def _mcp_repository(value: Path) -> Path:
    repository = value.expanduser().resolve(strict=True)
    if not repository.is_dir():
        raise ValueError(f"repository is not a directory: {repository}")
    return repository


def _stored_mcp(record: Any, server_id: str) -> Optional[dict[str, Any]]:
    if any(not isinstance(item, dict) for item in record.mcp_servers):
        raise McpAccessDenied("stored MCP selection must be an object")
    matches = [
        item for item in record.mcp_servers if item.get("server_id") == server_id
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


def _mcp_server_request(value: str) -> tuple[str, str]:
    server_id, _, scope = str(value).partition("=")
    server_id, scope = server_id.strip(), (scope.strip().lower() or "read")
    if not server_id:
        raise ValueError("--mcp-server needs a server ID")
    if scope not in {"read", "all"}:
        raise ValueError(f"--mcp-server scope must be read or all, not {scope!r}")
    return server_id, scope


def _attach_mcp_servers(
    store: SessionStore, record: Any, repository: Path, requests: Sequence[str]
) -> Any:
    """Store ``--mcp-server`` choices in the session before the run binds tools.

    The flag is the caller's explicit approval, so each chosen tool is
    ``allow`` and every other tool ``deny``; ``ask`` is never written, since
    nothing can answer it in the middle of a run. A server that cannot be
    reached fails the run here, before any model call, instead of silently
    running without the tools the caller asked for.
    """

    wanted = dict(_mcp_server_request(item) for item in requests)
    registry = _mcp_registry()
    client = McpClient(registry)
    for server_id, scope in sorted(wanted.items()):
        server = registry.get(server_id)
        tools = client.discover_record(server, repository)
        decisions = {
            item.remote_name: "allow" if scope == "all" or item.read_only else "deny"
            for item in tools
        }
        with store.mutate(
            record.session_id, f"mcp-attach-{os.getpid()}", ttl_seconds=5.0
        ) as current:
            store.validate_repository(current, repository)
            selection = mcp_selection(
                server,
                tools,
                decisions,
                previous=_stored_mcp(current, server.server_id),
            )
            _replace_stored_mcp(current, server.server_id, selection)
    return store.load(record.session_id)


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
    skipped: list[dict[str, str]] = []
    total = 0
    per_file = 20_000
    total_limit = 64_000
    for name in ("AGENTS.md", "CLAUDE.md", "KAROX.md"):
        path = repository / name
        try:
            if path.is_symlink():
                skipped.append({"name": name, "reason": "symlink"})
                continue
            if not path.is_file():
                continue
            resolved = path.resolve(strict=True)
            try:
                resolved.relative_to(repository)
            except ValueError:
                skipped.append({"name": name, "reason": "outside_repository"})
                continue
            raw = resolved.read_bytes()[: per_file + 1]
        except OSError:
            continue
        text = raw.decode("utf-8", "replace").strip()
        if not text:
            continue
        if len(raw) > per_file or len(text) > per_file:
            text = text[:per_file] + "\n...[truncated]"
        remaining = total_limit - total
        if remaining <= 0:
            skipped.append({"name": name, "reason": "total_limit"})
            continue
        if len(text) > remaining:
            text = text[:remaining] + "\n...[truncated]"
        parts.append(f"--- {name} ---\n{text}")
        sources.append(name)
        total += len(text)
    metadata: dict[str, Any] = {
        "enabled": True,
        "sources": sources,
        "instruction_bytes": total,
        "limits": {"per_file": per_file, "total": total_limit},
        "skipped": skipped,
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

        vision = _run_vision_capability(args)
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
    if command == "export":
        from .session_export import export_session

        record = store.load(args.session_id)
        text = export_session(record, args.format)
        target = args.output or Path.cwd() / f"karox-session-{record.session_id}.{args.format}"
        target = target.expanduser()
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8", newline="\n")
        _emit(
            {"session_id": record.session_id, "format": args.format, "path": str(target.resolve()), "bytes": len(text.encode("utf-8"))},
            json_output=args.json,
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
    if command != "show":
        raise ValueError(f"unsupported session command: {command}")
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
    controller = _controller()
    existing_reference: Optional[str] = None
    with contextlib.suppress(RegistryError, ValueError):
        existing_reference = controller.details(provider_id).provider.credential_ref
    secret: Optional[str] = None
    probe_secret: Optional[str] = None
    credential_ref = args.credential_ref or existing_reference
    if args.stdin_key:
        secret = sys.stdin.readline().rstrip("\r\n")
        if not secret.strip():
            raise ValueError("stdin did not contain an API key")
        probe_secret = secret
    elif args.credential_ref:
        probe_secret = CredentialStore().resolve(args.credential_ref)
    elif existing_reference:
        with contextlib.suppress(CredentialError):
            probe_secret = CredentialStore().resolve(existing_reference)
    privacy = _privacy_class(base_url, preset.privacy_class)
    is_local = privacy == "local"
    if not is_local and not (probe_secret or credential_ref):
        raise ValueError(
            "remote provider setup requires --stdin-key, --credential-ref, "
            "or an existing saved credential"
        )
    verification: dict[str, Any] = {"status": "skipped"}
    if not args.no_test:
        if probe_secret is None and not is_local:
            raise ValueError(
                "a live test requires --stdin-key, --credential-ref, or a saved credential"
            )
        if probe_secret is None:
            verification = _probe_candidate(
                str(preset.adapter_kind),
                base_url,
                model_id,
                credential=None,
                timeout_seconds=60.0,
            )
        else:
            key = probe_secret
            verification = _probe_candidate(
                str(preset.adapter_kind),
                base_url,
                model_id,
                credential=lambda: key,
                timeout_seconds=60.0,
            )
    provider = ProviderRecord(
        provider_id=provider_id,
        adapter_kind=str(preset.adapter_kind),
        base_url=base_url,
        credential_ref=credential_ref,
        privacy_class=privacy,
    )
    model = ModelRecord(
        provider_id=provider_id,
        model_id=model_id,
        context_window=args.context_window,
        max_output_tokens=args.max_output_tokens,
        tools="true",
        streaming="true",
        pricing=_pricing(args),
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


def _handle_provider_catalog(args: argparse.Namespace) -> int:
    from .provider_catalog import provider_catalog
    from .provider_connect import connected_catalog_ids

    catalog = provider_catalog()
    connected = connected_catalog_ids(_registry(), catalog)
    if args.provider:
        entry = catalog.provider(args.provider)
        payload = entry.to_dict(include_models=True)
        payload["connected_as"] = connected.get(entry.provider_id)
        payload["snapshot"] = catalog.fetched_at
        if args.json:
            _emit(payload, json_output=True)
            return 0
        print(f"{entry.display_name} ({entry.provider_id}) - models.dev snapshot {catalog.fetched_at}")
        print(f"  endpoint: {entry.base_url or '-'}")
        print(f"  key: {', '.join(entry.env) or '-'}")
        if entry.unsupported_reason:
            print(f"  cannot connect: {entry.unsupported_reason}")
        for item in entry.models:
            price = (
                f"${item.input_per_million:g}/${item.output_per_million:g} per 1M"
                if item.priced else "no published price"
            )
            context = f"{item.context_window // 1000}k" if item.context_window else "?"
            flag = " (deprecated)" if item.deprecated else ""
            print(f"  {item.model_id:<48} {context:>6}  {price}{flag}")
        return 0
    rows = []
    for entry in catalog.search(args.query, installable_only=not args.all):
        row = entry.to_dict()
        row["connected_as"] = connected.get(entry.provider_id)
        rows.append(row)
    if args.json:
        _emit(rows, json_output=True)
        return 0
    print(f"models.dev snapshot {catalog.fetched_at}: {len(rows)} providers")
    for row in rows:
        state = "connected" if row["connected_as"] else (
            "ready" if row["installable"] else "unsupported"
        )
        note = row["unsupported_reason"] or ", ".join(row.get("key_env", row["env"])[:1])
        print(f"  {row['provider_id']:<28} {state:<11} {row['model_count']:>4} models  {note}")
    print("Connect one with: karox provider connect <id>")
    return 0


def _endpoint_variables(entry: Any, supplied: dict[str, str]) -> dict[str, str]:
    """``--var`` values, completed from same-named environment variables.

    The catalog names endpoint placeholders after the provider's own
    conventional variables (``AZURE_RESOURCE_NAME``), so an environment that is
    already configured for that provider needs no ``--var`` at all.
    """

    values = dict(supplied)
    for name in entry.url_variables:
        if not values.get(name) and os.environ.get(name, "").strip():
            values[name] = os.environ[name].strip()
    return values


def _handle_provider_connect(args: argparse.Namespace) -> int:
    from .provider_catalog import provider_catalog
    from .provider_connect import ConnectError, connect_from_catalog

    catalog = provider_catalog()
    entry = catalog.provider(args.catalog_id)
    controller = _controller()
    registry_id = (args.provider_id or entry.provider_id).strip()
    existing: Optional[ProviderRecord] = None
    with contextlib.suppress(RegistryError, ValueError):
        existing = controller.registry.provider(registry_id)
    secret: Optional[str] = None
    credential_ref: Optional[str] = args.credential_ref
    if args.stdin_key:
        secret = sys.stdin.readline().strip()
        if not secret:
            raise ValueError("stdin did not contain an API key")
    elif args.env_key:
        # Runtime references stay in KaroX's own namespace (see credentials.py);
        # the provider's conventional variable is imported once into the keyring.
        own = "KAROX_PROVIDER_" + re.sub(r"[^A-Z0-9]+", "_", registry_id.upper()).strip("_") + "_API_KEY"
        variable = next((name for name in entry.key_env if os.environ.get(name)), None)
        if os.environ.get(own):
            credential_ref = f"env:{own}"
        elif variable is not None:
            secret = os.environ[variable].strip()
        else:
            raise ValueError(
                "none of these environment variables is set: "
                + ", ".join([own, *entry.key_env])
            )
    elif not credential_ref and not (existing and existing.credential_ref) and entry.env:
        if not sys.stdin.isatty():
            raise ValueError("pass --stdin-key, --credential-ref or --env-key")
        secret = getpass.getpass(f"{entry.display_name} API key: ").strip()
        if not secret:
            raise ValueError("no API key entered")
    try:
        result = connect_from_catalog(
            controller,
            entry.provider_id,
            secret=secret,
            credential_ref=credential_ref,
            provider_id=registry_id,
            base_url=args.base_url,
            variables=_endpoint_variables(entry, _pairs(args.var, "var")),
            model=args.model,
            include_deprecated=bool(args.include_deprecated),
            test=not args.no_test,
            activate=False if args.no_activate else None,
            catalog=catalog,
            probe=_probe_candidate,
        )
    except ConnectError as exc:
        raise ValueError(str(exc)) from None
    payload = result.to_dict()
    if args.json:
        _emit(payload, json_output=True)
        return 0
    kept = (
        f"; {result.models_kept} already configured kept as entered"
        if result.models_kept
        else ""
    )
    print(
        f"Connected {result.display_name} as '{result.provider_id}': "
        f"{result.models_installed} models ({result.priced_models} with published prices{kept})."
    )
    if result.activated:
        print(f"Selected model: {result.provider_id}/{result.model_id}")
    else:
        print(f"Select a model with: karox model select {result.provider_id} <model>")
    return 0


def _handle_provider_catalog_sync(args: argparse.Namespace) -> int:
    from .provider_connect import sync_from_catalog

    payload = sync_from_catalog(_registry(), args.provider_ids or ())
    _emit(payload, json_output=args.json)
    return 0


def _program_name() -> str:
    """``karox`` or the suffixed command this copy was started as (``karox6``)."""

    stem = Path(sys.argv[0] or "").stem.lower() if sys.argv else ""
    return stem if re.fullmatch(r"karox[0-9a-z_-]*", stem) else "karox"


def _handle_provider_local(args: argparse.Namespace) -> int:
    import subprocess

    from .local_runtimes import KNOWN_RUNTIMES, scan_local_runtimes

    found = scan_local_runtimes()
    if args.json:
        _json({"found": [item.to_dict() for item in found]})
        return 0
    if not found:
        ports = ", ".join(f"{kind.display_name} {kind.port}" for kind in KNOWN_RUNTIMES)
        _write(f"No local model server answered on 127.0.0.1 ({ports}).\n")
        _write(f"Start one, then run: {_program_name()} provider local\n")
        return 0
    for item in found:
        guessed = " (by port)" if item.identified_by == "port" else ""
        listed = ", ".join(item.models[:5]) + (" ..." if len(item.models) > 5 else "")
        _write(f"{item.display_name}{guessed}: {item.base_url}\n")
        _write(f"  models ({len(item.models)}): {listed or 'none loaded'}\n")
        argv = item.setup_argv(program=_program_name())
        line = subprocess.list2cmdline(argv) if os.name == "nt" else " ".join(shlex.quote(part) for part in argv)
        _write("  connect: " + line + "\n")
    return 0


def _handle_provider(args: argparse.Namespace) -> int:
    controller = _controller()
    command = args.provider_command
    if command == "presets":
        _emit([item.to_dict() for item in provider_presets()], json_output=args.json)
        return 0
    if command == "setup":
        return _handle_provider_setup(args)
    if command == "local":
        return _handle_provider_local(args)
    if command == "catalog":
        return _handle_provider_catalog(args)
    if command == "connect":
        return _handle_provider_connect(args)
    if command == "catalog-sync":
        return _handle_provider_catalog_sync(args)
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
            credential_ref=None if args.clear_credential else args.credential_ref,
            credential_ref_supplied=bool(args.credential_ref or args.clear_credential),
            headers=_pairs(args.header, "header") if args.header else None,
            query=_pairs(args.query, "query") if args.query else None,
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
    if command != "credential-clear":
        raise ValueError(f"unsupported provider command: {command}")
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
            pricing=_pricing(args),
            provenance=args.provenance,
        )
        _emit(controller.put_model(record).to_dict(), json_output=args.json)
        return 0
    if command == "discover":
        from .model_discovery import discover_models_result, model_record

        provider = registry.provider(args.provider_id)
        result = discover_models_result(
            provider,
            controller.credentials,
            timeout_seconds=args.timeout_seconds,
        )
        if result.base_url != provider.base_url:
            controller.edit_provider(provider.provider_id, base_url=result.base_url)
        saved: list[str] = []
        if args.save:
            existing = {
                model.model_id for model in registry.models(provider.provider_id)
            }
            for discovered in result.models:
                if discovered.model_id in existing:
                    continue
                controller.put_model(model_record(provider.provider_id, discovered))
                saved.append(discovered.model_id)
        _emit(
            {
                "provider_id": provider.provider_id,
                "base_url": result.base_url,
                "models": [item.as_dict() for item in result.models],
                "saved": saved,
                "saved_count": len(saved),
            },
            json_output=args.json,
        )
        return 0
    if command == "map":
        _emit(
            controller.map_alias(args.provider_id, args.alias, args.model).to_dict(),
            json_output=args.json,
        )
        return 0
    if command == "repair-selection":
        repaired = controller.repair_selection(
            preferred_provider=args.preferred_provider
        )
        _emit(
            {
                "status": "selected" if repaired is not None else "no_model",
                "model": asdict(repaired) if repaired is not None else None,
            },
            json_output=args.json,
        )
        return 0
    if command == "remove":
        _emit(
            controller.remove_model(args.provider_id, args.model).to_dict(),
            json_output=args.json,
        )
        return 0
    if command != "test":
        raise ValueError(f"unsupported model command: {command}")
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
        record = store.load(args.session_id)
        store.validate_repository(record, repository)
        if _stored_mcp(record, args.server_id) is not None:
            with store.mutate(
                args.session_id, f"mcp-deselect-{os.getpid()}", ttl_seconds=5.0
            ) as record:
                store.validate_repository(record, repository)
                removed = _replace_stored_mcp(record, args.server_id, None)
        else:
            removed = False
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
    lease = None
    if descriptor.mutates:
        lease = store.acquire(
            record.session_id,
            f"mcp-call-{os.getpid()}",
            ttl_seconds=max(30.0, min(3600.0, float(args.deadline_seconds) + 10.0)),
        )
    try:
        result = core.execute(command, lease=lease)
    finally:
        if lease is not None:
            store.release(lease)
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
    if args.mcp_command == "call":
        return _handle_mcp_call(args)
    raise ValueError(f"unsupported mcp command: {args.mcp_command}")


# --------------------------------------------------------------------- bridge

# The read-only catalogue is the launcher's default. Write mode adds only the
# concrete mutations a hosted coding session needs; push/publish/auth remain
# outside every bridge profile.
_BRIDGE_WRITE_TOOLS = (
    "karox.repo.write_file",
    "karox.repo.edit_file",
    "karox.repo.command",
    "karox.checks.run",
    "karox.checks.start",
    "karox.checks.status",
    "karox.checks.logs",
    "karox.checks.cancel",
    "karox.tests.run",
    "karox.git.commit",
)
# Developer commands need Full developer access (the elevated profile); a
# workspace-write session would only ever see them refused, so they are not
# advertised there.
_BRIDGE_DEVELOPER_TOOLS = (
    "karox.command.run",
    "karox.command.start",
    "karox.command.status",
    "karox.command.logs",
    "karox.command.cancel",
)
_BRIDGE_CHECK_TOOLS = frozenset(
    {"karox.checks.run", "karox.checks.start", "karox.checks.status", "karox.checks.logs", "karox.checks.cancel"}
)


def _bridge_access_profile(args: argparse.Namespace) -> AccessProfile:
    requested = getattr(args, "access_profile", None)
    if requested:
        return AccessProfile(requested)
    return AccessProfile.WORKSPACE_WRITE if args.write else AccessProfile.READ_ONLY


def _bridge_tools(
    selected: Sequence[str],
    *,
    write: bool,
    verification: Sequence[Sequence[str]],
    access_profile: Optional[AccessProfile] = None,
) -> tuple[str, ...]:
    from .bridge_launcher import DEFAULT_BRIDGE_TOOLS, core_bridge_tool_names

    tools = list(selected) or list(DEFAULT_BRIDGE_TOOLS)
    known = core_bridge_tool_names()
    elevated = access_profile == AccessProfile.ELEVATED
    if write or elevated:
        tools.extend(name for name in _BRIDGE_WRITE_TOOLS if name in known)
    if elevated:
        tools.extend(name for name in _BRIDGE_DEVELOPER_TOOLS if name in known)
    if not verification:
        tools = [name for name in tools if name not in _BRIDGE_CHECK_TOOLS]
    return tuple(dict.fromkeys(tools))


def _bridge_verification(args: argparse.Namespace) -> tuple[tuple[str, ...], ...]:
    return tuple(_verification_argv(item) for item in getattr(args, "verification_command", ()))


def _copy_secret_to_clipboard(secret: str) -> None:
    import subprocess

    from .detached_process import windowless_flags

    commands: list[list[str]] = []
    if os.name == "nt":
        commands.append(["clip"])
    elif sys.platform == "darwin":
        commands.append(["pbcopy"])
    else:
        commands.append(["wl-copy"])
        commands.append(["xclip", "-selection", "clipboard"])
    for command in commands:
        try:
            completed = subprocess.run(
                command,
                input=secret,
                text=True,
                capture_output=True,
                timeout=10.0,
                creationflags=windowless_flags() if os.name == "nt" else 0,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if completed.returncode == 0:
            return
    raise ValueError("no clipboard command is available; use --json and the OS UI")


def _bridge_session_id_for(args: argparse.Namespace) -> str:
    from .bridge_profiles import saved_web_bridge_session_id

    if getattr(args, "saved", None):
        return saved_web_bridge_session_id(args.saved)
    session_id = getattr(args, "session_id", None)
    if not session_id:
        raise ValueError("--saved or --session-id is required")
    return str(session_id)


def _handle_bridge(args: argparse.Namespace) -> int:
    from .bridge_profiles import BridgeProfileStore
    from .bridge_profiles import BridgeRegistry

    command = args.bridge_command
    if command == "list":
        _emit([item.to_dict() for item in BridgeRegistry().list()], json_output=args.json)
        return 0
    if command == "show":
        _emit(BridgeRegistry().get(args.profile).to_dict(), json_output=args.json)
        return 0
    if command == "doctor":
        payload = BridgeProfileStore().doctor()
        _emit(payload, json_output=args.json)
        return 0 if payload.get("status") in {None, "ok"} else 1
    if command == "saved":
        store = BridgeProfileStore()
        sub = args.bridge_saved_command
        if sub == "list":
            _emit([item.to_dict() for item in store.list()], json_output=args.json)
            return 0
        if sub == "show":
            _emit(store.get(args.name).to_dict(), json_output=args.json)
            return 0
        if sub == "delete":
            removed = store.delete(args.name)
            _emit(
                {"name": removed.name, "status": "deleted"},
                json_output=args.json,
            )
            return 0
        if sub == "validate":
            from .bridge_launcher import bridge_diagnostics, config_from_saved_profile

            config = config_from_saved_profile(args.name)
            _emit(bridge_diagnostics(config), json_output=args.json)
            return 0
        if sub != "create":
            raise ValueError(f"unsupported bridge saved command: {sub}")
        from .bridge_profiles import SavedBridgeProfile

        verification = _bridge_verification(args)
        repository = args.repository.expanduser().resolve(strict=True)
        access_profile = _bridge_access_profile(args)
        profile = SavedBridgeProfile(
            name=args.name,
            target_profile=args.target_profile,
            tools=_bridge_tools(
                args.tool,
                write=args.write,
                verification=verification,
                access_profile=access_profile,
            ),
            repository=str(repository),
            verification_commands=verification,
            deadline_seconds=args.deadline_seconds,
            tunnel=args.tunnel,
            public_url=args.public_url,
            language=args.language,
            access_profile=access_profile,
            port=args.port,
            tunnel_timeout_seconds=args.tunnel_timeout_seconds,
        )
        saved = store.put(profile, replace_existing=bool(args.replace))
        _emit(saved.to_dict(), json_output=args.json)
        return 0
    if command == "connect":
        from .bridge_launcher import (
            DEFAULT_BRIDGE_DEADLINE_SECONDS,
            WebBridgeConnectConfig,
            bridge_diagnostics,
            config_from_saved_profile,
            connect_bridge,
        )

        if args.saved:
            config = config_from_saved_profile(
                args.saved, tailscale=args.tailscale, session_id=args.session_id
            )
        else:
            if not args.profile:
                raise ValueError("bridge connect requires a profile or --saved")
            if args.profile not in WEB_BRIDGE_PROFILES:
                raise ValueError(f"unknown web bridge profile: {args.profile}")
            verification = _bridge_verification(args)
            access_profile = _bridge_access_profile(args)
            config = WebBridgeConnectConfig(
                profile=args.profile,
                repository=args.repository.expanduser().resolve(strict=True),
                port=args.port,
                tools=_bridge_tools(
                    args.tool,
                    write=args.write,
                    verification=verification,
                    access_profile=access_profile,
                ),
                session_id=args.session_id,
                access_profile=access_profile,
                tunnel=args.tunnel,
                public_url=args.public_url,
                tailscale=args.tailscale,
                tunnel_timeout_seconds=args.tunnel_timeout_seconds,
                deadline_seconds=args.deadline_seconds
                or DEFAULT_BRIDGE_DEADLINE_SECONDS,
                verification_commands=verification,
                language=args.language,
            )
        if args.diagnostics_only:
            _emit(bridge_diagnostics(config), json_output=args.json)
            return 0
        return connect_bridge(config)
    if command == "serve":
        from .bridge_launcher import DEFAULT_BRIDGE_TOOLS
        from .bridge_serve import serve_bridge

        if not args.tool:
            args.tool = list(DEFAULT_BRIDGE_TOOLS)
        return serve_bridge(args)
    if command in {"status", "start", "stop", "restart"}:
        from .bridge_launcher import (
            restart_saved_bridge,
            start_saved_bridge,
            status_bridge,
            stop_bridge,
        )

        functions = {
            "status": status_bridge,
            "start": start_saved_bridge,
            "stop": stop_bridge,
            "restart": restart_saved_bridge,
        }
        payload = functions[command](args.saved)
        _emit(payload, json_output=args.json)
        return 0 if payload.get("verdict") not in {"error", "foreign"} else 1
    if command == "oauth":
        from .bridge_profiles import BridgeCredentialStore

        session_id = _bridge_session_id_for(args)
        reference = f"os-keyring:bridge/{session_id}"
        store = BridgeCredentialStore()
        secret = store.resolve(reference)
        payload = {
            "session_id": session_id,
            "reference": reference,
            "fingerprint": store.fingerprint(secret),
            "status": "available",
        }
        if args.copy:
            _copy_secret_to_clipboard(secret)
            payload["copied"] = True
        _emit(payload, json_output=args.json)
        if not args.quiet and not args.json:
            _write("The approval password was not printed. Use --copy to place it on the clipboard.\n")
        return 0
    if command == "credential":
        from .bridge_profiles import BridgeCredentialStore

        store = BridgeCredentialStore()
        session_id = _bridge_session_id_for(args)
        reference = f"os-keyring:bridge/{session_id}"
        if args.bridge_credential_command in {"set", "rotate"}:
            payload = store.set(session_id)
            payload.pop("secret", None)
        elif args.bridge_credential_command == "revoke":
            store.delete(session_id)
            payload = {"reference": reference, "status": "revoked"}
        else:
            secret = store.resolve(reference)
            payload = {
                "reference": reference,
                "status": "available",
                "fingerprint": store.fingerprint(secret),
            }
            if args.bridge_credential_command == "copy" or getattr(args, "copy", False):
                _copy_secret_to_clipboard(secret)
                payload["copied"] = True
        _emit(payload, json_output=args.json)
        return 0
    raise ValueError(f"unknown bridge command: {command}")


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


def _handle_doctor(args: argparse.Namespace) -> int:
    """One local, read-only health report across providers, MCP and sessions."""

    payload: dict[str, Any] = {"status": "ok", "checks": {}}
    try:
        registry = _registry()
        providers = registry.providers()
        selected = registry.selected_model()
        payload["checks"]["registry"] = {
            "status": "ok",
            "providers": len(providers),
            "models": len(registry.models()),
            "selected": (
                f"{selected.provider_id}/{selected.model_id}" if selected else None
            ),
        }
    except (RegistryError, OSError) as exc:
        payload["checks"]["registry"] = {"status": "failed", "error": str(exc)}
    try:
        credential = CredentialStore().doctor()
        payload["checks"]["credentials"] = credential
    except CredentialError as exc:
        payload["checks"]["credentials"] = {
            "status": "unavailable",
            "error": str(exc),
            "recovery": "Run 'karox credential setup'.",
        }
    try:
        servers = _mcp_registry().list()
        status = build_mcp_status(servers)
        payload["checks"]["mcp"] = {
            "status": "ok",
            "servers": len(servers),
            "attention": status.get("attention", []),
        }
    except (McpError, OSError) as exc:
        payload["checks"]["mcp"] = {"status": "failed", "error": str(exc)}
    try:
        sessions = SessionStore(session_dir()).list(include_archived=False)
        payload["checks"]["sessions"] = {"status": "ok", "count": len(sessions)}
    except (SessionError, OSError) as exc:
        payload["checks"]["sessions"] = {"status": "failed", "error": str(exc)}
    for check in payload["checks"].values():
        if check.get("status") in {"failed", "unavailable", "missing", "degraded"}:
            payload["status"] = "degraded"
    payload["paths"] = {
        "config_dir": str(config_dir()),
        "runtime_dir": str(runtime_dir()),
        "session_dir": str(session_dir()),
    }
    _emit(payload, json_output=args.json)
    # A degraded report is data, not a command failure: the caller decides
    # whether a missing model or keyring is fatal for its workflow.
    return 0


def _handle_status(args: argparse.Namespace) -> int:
    registry = _registry()
    selected = registry.selected_model()
    store = SessionStore(session_dir())
    records = store.list(include_archived=False)
    latest = records[0] if records else None
    payload = {
        "version": __version__,
        "model": (
            f"{selected.provider_id}/{selected.model_id}" if selected else None
        ),
        "providers": len(registry.providers()),
        "mcp_servers": len(_mcp_registry().list()),
        "sessions": len(records),
        "latest_session": _record_summary(latest) if latest is not None else None,
        "paths": {
            "config_dir": str(config_dir()),
            "runtime_dir": str(runtime_dir()),
            "session_dir": str(session_dir()),
        },
    }
    _emit(payload, json_output=args.json)
    return 0


def _handle_credential(args: argparse.Namespace) -> int:
    if args.credential_command == "setup":
        from .credential_session import (
            activate_secret_service,
            setup_plan,
            unlock_secret_service,
            verify_child_storage,
        )

        action = (
            activate_secret_service
            if args.activate
            else unlock_secret_service
            if args.unlock
            else verify_child_storage
            if args.verify
            else None
        )
        try:
            payload = action(consent=args.consent) if action else setup_plan()
        except CredentialError as exc:
            payload = {"status": "unavailable", "error": str(exc)}
        _emit(payload, json_output=args.json)
        return 0 if payload.get("status") in {"ok", "manual-setup"} else 1
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _normalize_argv(list(sys.argv[1:] if argv is None else argv))
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
        if args.command == "bridge":
            return _handle_bridge(args)
        if args.command == "skill":
            return _handle_skill(args)
        if args.command == "credential":
            return _handle_credential(args)
        if args.command == "doctor":
            return _handle_doctor(args)
        if args.command == "status":
            return _handle_status(args)
        if args.command == "paths":
            _emit(
                {
                    "config_dir": str(config_dir()),
                    "runtime_dir": str(runtime_dir()),
                    "session_dir": str(session_dir()),
                },
                json_output=bool(args.json),
            )
            return 0
        parser.print_help()
        return 0
    except (
        AgentError,
        CoreError,
        CredentialError,
        ProviderError,
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