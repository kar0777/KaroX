"""Command-line entry point for the KaroX vNext foundation."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, Sequence

from .agent import AgentError, AgentKernel, AgentLimits, AgentReport
from .core import CoreError, CoreRuntime
from .credentials import CredentialError, CredentialStore
from .migration import MigrationError, migrate_legacy_metadata
from .models import AccessProfile, Capability, Origin, OriginKind
from .paths import (
    config_dir,
    legacy_config_dir,
    migration_dir,
    runtime_dir,
    session_dir,
)
from .policy import CapabilityPolicy
from .provider_factory import ProviderFactory
from .providers import (
    ModelMessage,
    ModelRequest,
    OpenAIChatCompletionsProvider,
    ProviderError,
)
from .registry import (
    ADAPTER_KINDS,
    CAPABILITY_VALUES,
    PRIVACY_CLASSES,
    ModelPricing,
    ModelRecord,
    ProviderRecord,
    ProviderRegistry,
    RegistryError,
)
from .routing import RouteTarget, RoutedProvider, RoutingPolicy
from .security import redact
from .sessions import SessionError, SessionRecord, SessionStore


def _json(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _record_summary(record: SessionRecord) -> dict[str, Any]:
    return {
        "session_id": record.session_id,
        "repository": record.repository,
        "branch": record.branch,
        "access_profile": record.access_profile,
        "task": record.task,
        "status": record.status,
        "phase": record.phase,
        "revision": record.revision,
        "updated_at": record.updated_at,
        "revoked": record.revoked,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="karox", description="KaroX hybrid runtime")
    commands = parser.add_subparsers(dest="command", required=True)

    paths = commands.add_parser("paths", help="show resolved application paths")
    paths.add_argument("--json", action="store_true")

    session = commands.add_parser("session", help="manage durable vNext sessions")
    sessions = session.add_subparsers(dest="session_command", required=True)
    create = sessions.add_parser("create", help="create a repository-bound session")
    create.add_argument("--repository", type=Path, default=Path.cwd())
    create.add_argument("--task", required=True)
    create.add_argument(
        "--access-profile",
        choices=[item.value for item in AccessProfile],
        default=AccessProfile.WORKSPACE_WRITE.value,
    )
    create.add_argument("--branch", default="")
    create.add_argument("--id")
    create.add_argument("--json", action="store_true")
    listing = sessions.add_parser("list", help="list sessions")
    listing.add_argument("--json", action="store_true")
    show = sessions.add_parser("show", help="show one session")
    show.add_argument("session_id")
    show.add_argument("--json", action="store_true")

    credential = commands.add_parser(
        "credential", help="manage secrets in the operating-system keyring"
    )
    credentials = credential.add_subparsers(
        dest="credential_command", required=True
    )
    credential_set = credentials.add_parser("set", help="store a provider secret")
    credential_set.add_argument("name")
    credential_set.add_argument(
        "--stdin", action="store_true", help="read the secret from standard input"
    )
    credential_set.add_argument("--json", action="store_true")
    credential_show = credentials.add_parser(
        "show", help="show an opaque reference and fingerprint"
    )
    credential_show.add_argument("name")
    credential_show.add_argument("--json", action="store_true")
    credential_delete = credentials.add_parser(
        "delete", help="delete a provider secret"
    )
    credential_delete.add_argument("name")
    credential_delete.add_argument("--json", action="store_true")
    credential_doctor = credentials.add_parser(
        "doctor", help="verify secure credential storage availability"
    )
    credential_doctor.add_argument("--json", action="store_true")

    provider = commands.add_parser("provider", help="manage API providers")
    providers = provider.add_subparsers(dest="provider_command", required=True)
    provider_add = providers.add_parser("add", help="add a provider")
    provider_add.add_argument("provider_id")
    provider_add.add_argument("--adapter", choices=sorted(ADAPTER_KINDS), required=True)
    provider_add.add_argument("--base-url", required=True)
    provider_add.add_argument("--credential-ref")
    provider_add.add_argument("--privacy-class", choices=sorted(PRIVACY_CLASSES), default="public")
    provider_add.add_argument("--timeout-seconds", type=float, default=60.0)
    provider_add.add_argument("--max-transport-retries", type=int, default=2)
    provider_add.add_argument("--header", action="append", default=[])
    provider_add.add_argument("--query", action="append", default=[])
    provider_add.add_argument("--json", action="store_true")
    provider_edit = providers.add_parser("edit", help="edit a provider")
    provider_edit.add_argument("provider_id")
    provider_edit.add_argument("--adapter", choices=sorted(ADAPTER_KINDS))
    provider_edit.add_argument("--base-url")
    provider_edit.add_argument("--credential-ref")
    provider_edit.add_argument("--clear-credential", action="store_true")
    provider_edit.add_argument("--privacy-class", choices=sorted(PRIVACY_CLASSES))
    provider_edit.add_argument("--timeout-seconds", type=float)
    provider_edit.add_argument("--max-transport-retries", type=int)
    provider_edit.add_argument("--header", action="append")
    provider_edit.add_argument("--query", action="append")
    provider_edit.add_argument("--json", action="store_true")
    provider_remove = providers.add_parser("remove", help="remove a provider")
    provider_remove.add_argument("provider_id")
    provider_remove.add_argument("--cascade", action="store_true")
    provider_remove.add_argument("--json", action="store_true")
    provider_list = providers.add_parser("list", help="list providers")
    provider_list.add_argument("--json", action="store_true")
    provider_show = providers.add_parser("show", help="show a provider")
    provider_show.add_argument("provider_id")
    provider_show.add_argument("--json", action="store_true")
    provider_test = providers.add_parser("test", help="send a minimal model request")
    provider_test.add_argument("provider_id")
    provider_test.add_argument("--model")
    provider_test.add_argument("--json", action="store_true")

    model = commands.add_parser("model", help="manage provider models")
    models = model.add_subparsers(dest="model_command", required=True)
    model_add = models.add_parser("add", help="add or replace a model")
    model_add.add_argument("provider_id")
    model_add.add_argument("model_id")
    model_add.add_argument("--alias", action="append", default=[])
    model_add.add_argument("--context-window", type=int)
    model_add.add_argument("--max-output-tokens", type=int)
    for capability in ("tools", "vision", "structured-output", "streaming"):
        model_add.add_argument(
            f"--{capability}", choices=sorted(CAPABILITY_VALUES), default="unknown"
        )
    model_add.add_argument("--pricing-version")
    model_add.add_argument("--currency")
    model_add.add_argument("--input-per-million", type=float)
    model_add.add_argument("--output-per-million", type=float)
    model_add.add_argument("--pricing-source")
    model_add.add_argument("--provenance", default="manual")
    model_add.add_argument("--json", action="store_true")
    model_remove = models.add_parser("remove", help="remove a model")
    model_remove.add_argument("provider_id")
    model_remove.add_argument("model")
    model_remove.add_argument("--json", action="store_true")
    model_list = models.add_parser("list", help="list models")
    model_list.add_argument("--provider")
    model_list.add_argument("--json", action="store_true")
    model_inspect = models.add_parser("inspect", help="show a model")
    model_inspect.add_argument("provider_id")
    model_inspect.add_argument("model")
    model_inspect.add_argument("--json", action="store_true")
    model_select = models.add_parser("select", help="select the default model")
    model_select.add_argument("provider_id")
    model_select.add_argument("model")
    model_select.add_argument("--json", action="store_true")
    model_test = models.add_parser("test", help="send a minimal model request")
    model_test.add_argument("provider_id")
    model_test.add_argument("model")
    model_test.add_argument("--json", action="store_true")

    agent = commands.add_parser("agent", help="run the bounded native agent")
    agents = agent.add_subparsers(dest="agent_command", required=True)
    run = agents.add_parser("run", help="run or resume a native-agent session")
    run.add_argument("--repository", type=Path, default=Path.cwd())
    run.add_argument("--task", required=True)
    run.add_argument("--model")
    run.add_argument("--base-url")
    run.add_argument(
        "--api-key-env",
        help="name of an environment variable containing the provider API key",
    )
    run.add_argument("--session-id")
    run.add_argument(
        "--route", action="append", default=[], help="provider/model fallback route"
    )
    run.add_argument(
        "--privacy-limit", choices=sorted(PRIVACY_CLASSES), default=None
    )
    run.add_argument("--max-total-tokens", type=int)
    run.add_argument("--max-cost", type=float)
    run.add_argument("--currency")
    run.add_argument("--max-steps", type=int, default=24)
    run.add_argument("--max-seconds", type=float, default=900.0)
    run.add_argument("--json", action="store_true")

    migrate = commands.add_parser(
        "migrate", help="safely inspect or import legacy metadata"
    )
    migrate.add_argument("--legacy-config", type=Path, default=None)
    migrate.add_argument("--destination", type=Path, default=None)
    migrate.add_argument(
        "--apply",
        action="store_true",
        help="write sanitized metadata; default is dry-run",
    )
    migrate.add_argument("--json", action="store_true")
    return parser


def _print_mapping(value: dict[str, Any]) -> None:
    for key, item in value.items():
        print(f"{key}: {item}")


def _print_agent_report(report: AgentReport) -> None:
    print(f"session_id: {report.session_id}")
    print(f"status: {report.status}")
    print(f"verified: {str(report.verified).lower()}")
    print(f"reason: {report.reason}")
    print(f"steps: {report.steps}")
    if report.changed_files:
        print("changed_files: " + ", ".join(report.changed_files))
    print(f"evidence_records: {len(report.evidence)}")
    if report.provider_message:
        print(f"provider_message: {report.provider_message}")


def _registry() -> ProviderRegistry:
    return ProviderRegistry(config_dir() / "vnext" / "providers.json")


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


def _route(value: str) -> RouteTarget:
    if "/" not in value:
        raise ValueError("route must use provider/model")
    provider_id, model = value.split("/", 1)
    return RouteTarget(provider_id, model)


def _pricing(args: argparse.Namespace) -> Optional[ModelPricing]:
    values = (
        args.pricing_version,
        args.currency,
        args.input_per_million,
        args.output_per_million,
        args.pricing_source,
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
    )


def _emit(value: Any, *, json_output: bool) -> None:
    if json_output:
        _json(value)
    elif isinstance(value, dict):
        _print_mapping(value)
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, dict):
                identifier = item.get("provider_id", "")
                if item.get("model_id") is not None:
                    identifier = f"{identifier}/{item['model_id']}"
                print(identifier or json.dumps(item, ensure_ascii=False))
            else:
                print(item)
    else:
        print(value)


def _test_registered_model(
    registry: ProviderRegistry, provider_id: str, model_or_alias: str
) -> dict[str, Any]:
    provider_record = registry.provider(provider_id)
    model_record = registry.model(provider_id, model_or_alias)
    response = ProviderFactory().create(provider_record).complete(
        ModelRequest(
            model=model_record.model_id,
            messages=(ModelMessage("user", "Reply with exactly OK."),),
            max_output_tokens=8,
            deadline_seconds=min(60.0, provider_record.timeout_seconds),
        )
    )
    return {
        "status": "ok",
        "provider_id": provider_id,
        "model_id": model_record.model_id,
        "finish_reason": response.finish_reason,
        "usage": response.usage,
        "transport_attempts": response.transport_attempts,
    }


def _agent_provider(
    args: argparse.Namespace, record: SessionRecord | None, limits: AgentLimits
) -> tuple[Any, str]:
    direct = args.model is not None or args.base_url is not None or args.api_key_env
    if direct:
        routed_options = [
            name
            for name, supplied in (
                ("--route", bool(args.route)),
                ("--privacy-limit", args.privacy_limit is not None),
                ("--max-total-tokens", args.max_total_tokens is not None),
                ("--max-cost", args.max_cost is not None),
                ("--currency", args.currency is not None),
            )
            if supplied
        ]
        if routed_options:
            raise ValueError(
                "direct provider mode cannot use routed option(s): "
                + ", ".join(routed_options)
            )
        if not args.model or not args.base_url:
            raise ValueError("direct provider mode requires --model and --base-url")
        credential = None
        if args.api_key_env:
            value = os.environ.get(args.api_key_env, "")
            if not value.strip():
                raise ValueError(
                    "provider API key environment variable is not set: "
                    f"{args.api_key_env}"
                )
            credential = lambda name=args.api_key_env: os.environ.get(name, "")
        return (
            OpenAIChatCompletionsProvider(
                args.base_url,
                credential=credential,
                timeout_seconds=min(60.0, float(limits.max_seconds)),
            ),
            args.model,
        )

    registry = _registry()
    routes = tuple(_route(value) for value in args.route)
    if not routes:
        selected = registry.selected_model()
        if selected is None:
            raise ValueError(
                "no provider route was given and no default model is selected"
            )
        routes = (RouteTarget(selected.provider_id, selected.model_id),)
    for target in routes:
        registry.provider(target.provider_id)
        registry.model(target.provider_id, target.model)
    initial_usage = record.usage if record is not None else {}
    costs = initial_usage.get("costs")
    initial_costs = costs if isinstance(costs, dict) else {}
    provider = RoutedProvider(
        registry,
        ProviderFactory(),
        RoutingPolicy(
            routes=routes,
            privacy_limit=args.privacy_limit or "public",
            max_total_tokens=args.max_total_tokens,
            max_cost=args.max_cost,
            currency=args.currency,
        ),
        initial_usage=initial_usage,
        initial_costs=initial_costs,
    )
    return provider, routes[0].model


def _run_agent(args: argparse.Namespace) -> AgentReport:
    repository = args.repository.expanduser().resolve(strict=True)
    if not repository.is_dir():
        raise ValueError(f"repository is not a directory: {repository}")
    limits = AgentLimits(max_steps=args.max_steps, max_seconds=args.max_seconds)
    store = SessionStore(session_dir())
    if args.session_id and store.state_path(args.session_id).exists():
        record = store.load(args.session_id)
        store.validate_repository(record, repository)
        if record.access_profile != AccessProfile.WORKSPACE_WRITE.value:
            raise SessionError("native agent requires a workspace_write session")
        if record.task != str(redact(args.task)):
            raise SessionError("resume task differs from the existing session task")
        provider, model = _agent_provider(args, record, limits)
    else:
        provider, model = _agent_provider(args, None, limits)
        record = store.create(
            repository,
            args.task,
            AccessProfile.WORKSPACE_WRITE,
            session_id=args.session_id,
        )

    origin = Origin(OriginKind.NATIVE_AGENT, f"cli-{record.session_id}")
    policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
    policy.set_grants(
        origin,
        {
            Capability.REPO_READ,
            Capability.REPO_WRITE,
            Capability.PROCESS_RUN,
            Capability.CHECKS_RUN,
            Capability.GIT_READ,
        },
    )
    core = CoreRuntime(
        repository,
        policy,
        store,
        runtime_dir() / "vnext" / "audit.jsonl",
    )
    return AgentKernel(
        provider=provider,
        model=model,
        core=core,
        sessions=store,
        origin=origin,
        limits=limits,
    ).run(record.session_id)


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
        secret = store.resolve(f"os-keyring:provider/{args.name}")
        payload = {
            "reference": f"os-keyring:provider/{args.name}",
            "fingerprint": store.fingerprint(secret),
            "status": "available",
        }
    elif args.credential_command == "delete":
        payload = store.delete(args.name)
    else:
        payload = store.doctor()
    _emit(payload, json_output=args.json)
    return 0


def _handle_provider(args: argparse.Namespace) -> int:
    registry = _registry()
    command = args.provider_command
    if command == "add":
        record = ProviderRecord(
            provider_id=args.provider_id,
            adapter_kind=args.adapter,
            base_url=args.base_url,
            credential_ref=args.credential_ref,
            headers=_pairs(args.header, "header"),
            query=_pairs(args.query, "query"),
            privacy_class=args.privacy_class,
            timeout_seconds=args.timeout_seconds,
            max_transport_retries=args.max_transport_retries,
        )
        payload = asdict(registry.put_provider(record))
    elif command == "edit":
        current = registry.provider(args.provider_id)
        if args.credential_ref and args.clear_credential:
            raise ValueError(
                "--credential-ref cannot be combined with --clear-credential"
            )
        credential_ref = current.credential_ref
        if args.credential_ref is not None:
            credential_ref = args.credential_ref
        elif args.clear_credential:
            credential_ref = None
        record = ProviderRecord(
            provider_id=current.provider_id,
            adapter_kind=args.adapter or current.adapter_kind,
            base_url=args.base_url or current.base_url,
            credential_ref=credential_ref,
            headers=(
                _pairs(args.header, "header")
                if args.header is not None
                else current.headers
            ),
            query=(
                _pairs(args.query, "query")
                if args.query is not None
                else current.query
            ),
            privacy_class=args.privacy_class or current.privacy_class,
            timeout_seconds=(
                args.timeout_seconds
                if args.timeout_seconds is not None
                else current.timeout_seconds
            ),
            max_transport_retries=(
                args.max_transport_retries
                if args.max_transport_retries is not None
                else current.max_transport_retries
            ),
        )
        payload = asdict(registry.put_provider(record))
    elif command == "remove":
        removed = registry.remove_provider(args.provider_id, cascade=args.cascade)
        payload = {
            "provider_id": removed.provider_id,
            "status": "removed",
            "cascade": args.cascade,
        }
    elif command == "list":
        payload = [asdict(item) for item in registry.providers()]
    elif command == "show":
        payload = asdict(registry.provider(args.provider_id))
    else:
        model_name = args.model
        if model_name is None:
            candidates = registry.models(args.provider_id)
            if not candidates:
                raise RegistryError(
                    f"provider has no registered models: {args.provider_id}"
                )
            model_name = candidates[0].model_id
        payload = _test_registered_model(registry, args.provider_id, model_name)
    _emit(payload, json_output=args.json)
    return 0


def _handle_model(args: argparse.Namespace) -> int:
    registry = _registry()
    command = args.model_command
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
        payload = asdict(registry.put_model(record))
    elif command == "remove":
        removed = registry.remove_model(args.provider_id, args.model)
        payload = {
            "provider_id": removed.provider_id,
            "model_id": removed.model_id,
            "status": "removed",
        }
    elif command == "list":
        selected = registry.selected_model()
        payload = []
        for item in registry.models(args.provider):
            value = asdict(item)
            value["selected"] = selected is not None and (
                item.provider_id,
                item.model_id,
            ) == (selected.provider_id, selected.model_id)
            payload.append(value)
    elif command == "inspect":
        payload = asdict(registry.model(args.provider_id, args.model))
    elif command == "select":
        selected = registry.select_model(args.provider_id, args.model)
        payload = {
            "provider_id": selected.provider_id,
            "model_id": selected.model_id,
            "status": "selected",
        }
    else:
        payload = _test_registered_model(registry, args.provider_id, args.model)
    _emit(payload, json_output=args.json)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "paths":
            payload = {
                "config_dir": str(config_dir()),
                "runtime_dir": str(runtime_dir()),
                "session_dir": str(session_dir()),
                "legacy_config_dir": str(legacy_config_dir()),
            }
            _json(payload) if args.json else _print_mapping(payload)
            return 0

        if args.command == "migrate":
            payload = migrate_legacy_metadata(
                args.legacy_config or legacy_config_dir(),
                args.destination or migration_dir(),
                dry_run=not args.apply,
            )
            _json(payload) if args.json else _print_mapping(payload)
            return 0

        if args.command == "credential":
            return _handle_credential(args)

        if args.command == "provider":
            return _handle_provider(args)

        if args.command == "model":
            return _handle_model(args)

        if args.command == "agent":
            report = _run_agent(args)
            _json(report.to_dict()) if args.json else _print_agent_report(report)
            return 0 if report.verified else 1

        store = SessionStore(session_dir())
        if args.session_command == "create":
            record = store.create(
                args.repository,
                args.task,
                AccessProfile(args.access_profile),
                branch=args.branch,
                session_id=args.id,
            )
            payload = _record_summary(record)
            _json(payload) if args.json else _print_mapping(payload)
            return 0
        if args.session_command == "list":
            records = [_record_summary(item) for item in store.list()]
            if args.json:
                _json(records)
            else:
                for record in records:
                    print(
                        f"{record['session_id']}\t{record['status']}\t"
                        f"{record['access_profile']}\t{record['repository']}"
                    )
            return 0
        record = store.load(args.session_id)
        payload = record.to_dict()
        _json(payload) if args.json else _print_mapping(payload)
        return 0
    except (
        AgentError,
        CredentialError,
        CoreError,
        MigrationError,
        ProviderError,
        RegistryError,
        SessionError,
        OSError,
        ValueError,
    ) as exc:
        print(f"karox: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
