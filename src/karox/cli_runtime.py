"""Run-time assembly for the agent command: provider route resolution, MCP selection, repository instructions and the skill catalogue. The Core/agent objects are created by `cli_agent`."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import Any, Optional, Sequence
from .mcp_client import (
    McpAccessDenied,
    McpClient,
    McpRuntimeBinding,
    McpServerRecord,
    McpToolDescriptor,
    mcp_selection,
    validate_mcp_selection_registry,
)
from .cli_context import _mcp_registry, _registry
from .provider_factory import ProviderFactory
from .providers import (
    OpenAIChatCompletionsProvider,
)
from .routing import RouteTarget, RoutingPolicy, RoutedProvider
from .sessions import SessionStore
from .skills import SkillCatalog, SkillError


def _route(value: str) -> RouteTarget:
    if "/" not in value:
        raise ValueError("route must use provider/model")
    provider_id, model = value.split("/", 1)
    return RouteTarget(provider_id, model)


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


def _run_vision_capability(
    args: argparse.Namespace, registry_loader: Any
) -> bool:
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
    registry = registry_loader()
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
