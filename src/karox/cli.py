"""Command-line entry point for the KaroX vNext foundation."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional, Sequence

from .agent import AgentError, AgentKernel, AgentLimits, AgentReport
from .core import CoreError, CoreRuntime
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
from .providers import OpenAIChatCompletionsProvider, ProviderError
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

    agent = commands.add_parser("agent", help="run the bounded native agent")
    agents = agent.add_subparsers(dest="agent_command", required=True)
    run = agents.add_parser("run", help="run or resume a native-agent session")
    run.add_argument("--repository", type=Path, default=Path.cwd())
    run.add_argument("--task", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--base-url", required=True)
    run.add_argument(
        "--api-key-env",
        help="name of an environment variable containing the provider API key",
    )
    run.add_argument("--session-id")
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


def _run_agent(args: argparse.Namespace) -> AgentReport:
    repository = args.repository.expanduser().resolve(strict=True)
    if not repository.is_dir():
        raise ValueError(f"repository is not a directory: {repository}")
    credential = None
    if args.api_key_env:
        value = os.environ.get(args.api_key_env, "")
        if not value.strip():
            raise ValueError(
                f"provider API key environment variable is not set: {args.api_key_env}"
            )
        credential = lambda name=args.api_key_env: os.environ.get(name, "")

    store = SessionStore(session_dir())
    if args.session_id and store.state_path(args.session_id).exists():
        record = store.load(args.session_id)
        store.validate_repository(record, repository)
        if record.access_profile != AccessProfile.WORKSPACE_WRITE.value:
            raise SessionError("native agent requires a workspace_write session")
        if record.task != str(redact(args.task)):
            raise SessionError("resume task differs from the existing session task")
    else:
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
    limits = AgentLimits(max_steps=args.max_steps, max_seconds=args.max_seconds)
    provider = OpenAIChatCompletionsProvider(
        args.base_url,
        credential=credential,
        timeout_seconds=min(60.0, float(limits.max_seconds)),
    )
    return AgentKernel(
        provider=provider,
        model=args.model,
        core=core,
        sessions=store,
        origin=origin,
        limits=limits,
    ).run(record.session_id)


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
        CoreError,
        MigrationError,
        ProviderError,
        SessionError,
        OSError,
        ValueError,
    ) as exc:
        print(f"karox: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
