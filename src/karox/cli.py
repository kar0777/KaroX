"""Command-line entry point for the KaroX vNext foundation."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional, Sequence

from .agent import AgentError, AgentKernel, AgentLimits, AgentReport, SYSTEM_PROMPT
from .bridge import (
    BridgeConfigurationError,
    BridgeCredentialStore,
    BridgeError,
    BridgeRegistry,
)
from .core import CoreError, CoreRuntime
from .credentials import CredentialError, CredentialStore
from .migration import MigrationError, migrate_legacy_metadata
from .mcp_client import (
    McpAccessDenied,
    McpClient,
    McpCredentialStore,
    McpError,
    McpRegistry,
    McpRuntimeBinding,
    McpServerRecord,
    McpToolDescriptor,
    mcp_selection,
    validate_mcp_selection_registry,
)
from .models import AccessProfile, Capability, CoreCommand, Origin, OriginKind
from .packs import (
    PackAccessDenied,
    PackConfigurationError,
    PackError,
    PackRegistry,
    create_pack_template,
)
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
from .handoff import build_handoff, handoff_digest
from .security import redact
from .sessions import SessionError, SessionRecord, SessionStore
from .skills import (
    SkillCatalog,
    SkillError,
    SkillMetadata,
    SkillPermission,
    configure_skill_policy,
    skill_selection,
    skill_system_prompt,
    validate_selection,
)


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
    handoff = sessions.add_parser(
        "handoff", help="emit a secret-free structured handoff document"
    )
    handoff.add_argument("session_id")
    handoff.add_argument("--repository", type=Path, default=Path.cwd())
    handoff.add_argument("--json", action="store_true")
    locking = sessions.add_parser(
        "lock", help="inspect the active session mutation lease"
    )
    locking.add_argument("session_id")
    locking.add_argument("--json", action="store_true")

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

    skill = commands.add_parser("skill", help="discover and select Skills")
    skills = skill.add_subparsers(dest="skill_command", required=True)

    def add_skill_source_arguments(command: argparse.ArgumentParser) -> None:
        command.add_argument("--repository", type=Path, default=Path.cwd())
        command.add_argument(
            "--skill-dir",
            type=Path,
            action="append",
            default=[],
            help="additional Skill source directory (repeatable)",
        )

    skill_list = skills.add_parser("list", help="list discovered Skill metadata")
    add_skill_source_arguments(skill_list)
    skill_list.add_argument("--json", action="store_true")
    skill_show = skills.add_parser("show", help="show Skill metadata")
    skill_show.add_argument("name")
    add_skill_source_arguments(skill_show)
    skill_show.add_argument("--json", action="store_true")
    skill_load = skills.add_parser("load", help="explicitly load Skill content")
    skill_load.add_argument("name")
    add_skill_source_arguments(skill_load)
    skill_load.add_argument("--json", action="store_true")
    skill_select = skills.add_parser(
        "select", help="select a Skill and session permission decisions"
    )
    skill_select.add_argument("name")
    skill_select.add_argument("--session-id", required=True)
    skill_select.add_argument(
        "--permission",
        "--skill-permission",
        dest="skill_permission",
        action="append",
        default=[],
        help="CAPABILITY=allow|ask|deny (repeatable)",
    )
    add_skill_source_arguments(skill_select)
    skill_select.add_argument("--json", action="store_true")
    skill_deselect = skills.add_parser(
        "deselect", help="remove a Skill selection from a session"
    )
    skill_deselect.add_argument("name")
    skill_deselect.add_argument("--session-id", required=True)
    add_skill_source_arguments(skill_deselect)
    skill_deselect.add_argument("--json", action="store_true")

    mcp = commands.add_parser("mcp", help="manage external MCP servers and tools")
    mcp_commands = mcp.add_subparsers(dest="mcp_command", required=True)

    mcp_server = mcp_commands.add_parser("server", help="manage MCP servers")
    mcp_servers = mcp_server.add_subparsers(
        dest="mcp_server_command", required=True
    )
    mcp_server_add = mcp_servers.add_parser("add", help="add or replace a server")
    mcp_server_add.add_argument("server_id")
    mcp_server_add.add_argument("--namespace", required=True)
    mcp_server_add.add_argument(
        "--transport", choices=("stdio", "streamable_http"), required=True
    )
    mcp_server_add.add_argument("--command")
    mcp_server_add.add_argument("--arg", action="append", default=[])
    mcp_server_add.add_argument("--url")
    mcp_server_add.add_argument("--env", action="append", default=[])
    mcp_server_add.add_argument("--header", action="append", default=[])
    mcp_server_add.add_argument("--credential-ref")
    mcp_server_add.add_argument("--credential-target")
    mcp_server_add.add_argument("--credential-scheme", default="Bearer")
    mcp_server_add.add_argument("--read-only-tool", action="append", default=[])
    mcp_server_add.add_argument("--timeout-seconds", type=float, default=30.0)
    mcp_server_add.add_argument("--max-result-bytes", type=int, default=1_000_000)
    mcp_server_add.add_argument("--max-message-bytes", type=int, default=1_000_000)
    mcp_server_add.add_argument("--max-transport-retries", type=int, default=1)
    mcp_server_add.add_argument("--json", action="store_true")
    mcp_server_remove = mcp_servers.add_parser("remove", help="remove a server")
    mcp_server_remove.add_argument("server_id")
    mcp_server_remove.add_argument("--json", action="store_true")
    mcp_server_list = mcp_servers.add_parser("list", help="list servers")
    mcp_server_list.add_argument("--json", action="store_true")
    mcp_server_show = mcp_servers.add_parser("show", help="show a server")
    mcp_server_show.add_argument("server_id")
    mcp_server_show.add_argument("--json", action="store_true")
    for name, help_text in (
        ("inspect", "discover and show server tools"),
        ("doctor", "verify server connectivity and discovery"),
    ):
        command = mcp_servers.add_parser(name, help=help_text)
        command.add_argument("server_id")
        command.add_argument("--repository", type=Path, default=Path.cwd())
        command.add_argument("--json", action="store_true")

    mcp_session = mcp_commands.add_parser(
        "session", help="manage session MCP selections"
    )
    mcp_sessions = mcp_session.add_subparsers(
        dest="mcp_session_command", required=True
    )
    mcp_session_select = mcp_sessions.add_parser(
        "select", help="discover and select a server for a session"
    )
    mcp_session_select.add_argument("server_id")
    mcp_session_select.add_argument("--session-id", required=True)
    mcp_session_select.add_argument("--repository", type=Path, default=Path.cwd())
    mcp_session_select.add_argument(
        "--permission",
        action="append",
        default=[],
        help="REMOTE_TOOL=allow|ask|deny (repeatable)",
    )
    mcp_session_select.add_argument("--json", action="store_true")
    mcp_session_deselect = mcp_sessions.add_parser(
        "deselect", help="remove a server selection from a session"
    )
    mcp_session_deselect.add_argument("server_id")
    mcp_session_deselect.add_argument("--session-id", required=True)
    mcp_session_deselect.add_argument("--repository", type=Path, default=Path.cwd())
    mcp_session_deselect.add_argument("--json", action="store_true")
    mcp_session_permissions = mcp_sessions.add_parser(
        "permissions", help="show stored server permissions"
    )
    mcp_session_permissions.add_argument("server_id")
    mcp_session_permissions.add_argument("--session-id", required=True)
    mcp_session_permissions.add_argument(
        "--repository", type=Path, default=Path.cwd()
    )
    mcp_session_permissions.add_argument("--json", action="store_true")

    mcp_call = mcp_commands.add_parser("call", help="call a selected MCP tool")
    mcp_call.add_argument("tool", help="fully namespaced mcp.<namespace>.<tool> name")
    mcp_call.add_argument("--session-id", required=True)
    mcp_call.add_argument("--repository", type=Path, default=Path.cwd())
    mcp_call.add_argument("--arguments", default="{}", help="JSON object arguments")
    mcp_call.add_argument("--idempotency-key")
    mcp_call.add_argument("--deadline-seconds", type=float, default=120.0)
    mcp_call.add_argument("--json", action="store_true")

    mcp_credential = mcp_commands.add_parser(
        "credential", help="manage MCP secrets in the operating-system keyring"
    )
    mcp_credentials = mcp_credential.add_subparsers(
        dest="mcp_credential_command", required=True
    )
    mcp_credential_set = mcp_credentials.add_parser("set", help="store a secret")
    mcp_credential_set.add_argument("name")
    mcp_credential_set.add_argument("--stdin", action="store_true")
    mcp_credential_set.add_argument("--json", action="store_true")
    mcp_credential_show = mcp_credentials.add_parser(
        "show", help="show an opaque reference and fingerprint"
    )
    mcp_credential_show.add_argument("name")
    mcp_credential_show.add_argument("--json", action="store_true")
    mcp_credential_delete = mcp_credentials.add_parser(
        "delete", help="delete a secret"
    )
    mcp_credential_delete.add_argument("name")
    mcp_credential_delete.add_argument("--json", action="store_true")
    mcp_credential_doctor = mcp_credentials.add_parser(
        "doctor", help="verify secure MCP credential storage"
    )
    mcp_credential_doctor.add_argument("--json", action="store_true")

    bridge = commands.add_parser(
        "bridge", help="manage hosted-client bridge profiles and credentials"
    )
    bridge_commands = bridge.add_subparsers(dest="bridge_command", required=True)
    bridge_list = bridge_commands.add_parser("list", help="list bridge profiles")
    bridge_list.add_argument("--json", action="store_true")
    bridge_show = bridge_commands.add_parser("show", help="show a bridge profile")
    bridge_show.add_argument("name")
    bridge_show.add_argument("--json", action="store_true")
    bridge_doctor = bridge_commands.add_parser(
        "doctor", help="verify secure bridge credential storage"
    )
    bridge_doctor.add_argument("--json", action="store_true")
    bridge_credential = bridge_commands.add_parser(
        "credential", help="manage bridge secrets in the OS keyring"
    )
    bridge_credentials = bridge_credential.add_subparsers(
        dest="bridge_credential_command", required=True
    )
    bridge_credential_set = bridge_credentials.add_parser(
        "set", help="generate and store a bridge credential"
    )
    bridge_credential_set.add_argument("name")
    bridge_credential_set.add_argument("--json", action="store_true")
    bridge_credential_show = bridge_credentials.add_parser(
        "show", help="show an opaque reference and fingerprint"
    )
    bridge_credential_show.add_argument("name")
    bridge_credential_show.add_argument("--json", action="store_true")
    bridge_credential_rotate = bridge_credentials.add_parser(
        "rotate-key", help="replace a bridge credential"
    )
    bridge_credential_rotate.add_argument("name")
    bridge_credential_rotate.add_argument("--json", action="store_true")
    bridge_credential_revoke = bridge_credentials.add_parser(
        "revoke", help="delete a bridge credential"
    )
    bridge_credential_revoke.add_argument("name")
    bridge_credential_revoke.add_argument("--json", action="store_true")

    pack = commands.add_parser(
        "pack", help="manage installable KaroX Packs"
    )
    pack_commands = pack.add_subparsers(dest="pack_command", required=True)
    pack_create = pack_commands.add_parser("create", help="generate a sample pack template")
    pack_create.add_argument("target", type=Path)
    pack_create.add_argument("--name", required=True)
    pack_create.add_argument("--description", required=True)
    pack_create.add_argument("--json", action="store_true")
    pack_install = pack_commands.add_parser("install", help="install a pack from a directory")
    pack_install.add_argument("source", type=Path)
    pack_install.add_argument("--allow", action="append", default=[])
    pack_install.add_argument("--json", action="store_true")
    pack_remove = pack_commands.add_parser("remove", help="remove an installed pack")
    pack_remove.add_argument("identity")
    pack_remove.add_argument("--json", action="store_true")
    pack_list = pack_commands.add_parser("list", help="list installed packs")
    pack_list.add_argument("--json", action="store_true")
    pack_inspect = pack_commands.add_parser("inspect", help="show an installed pack")
    pack_inspect.add_argument("identity")
    pack_inspect.add_argument("--json", action="store_true")
    pack_doctor = pack_commands.add_parser("doctor", help="verify an installed pack")
    pack_doctor.add_argument("identity")
    pack_doctor.add_argument("--json", action="store_true")
    pack_enable = pack_commands.add_parser("enable", help="enable a pack")
    pack_enable.add_argument("identity")
    pack_enable.add_argument("--json", action="store_true")
    pack_disable = pack_commands.add_parser("disable", help="disable a pack")
    pack_disable.add_argument("identity")
    pack_disable.add_argument("--json", action="store_true")

    tui = commands.add_parser("tui", help="run the optional interactive TUI shell")
    tui.add_argument("--session-id")
    tui.add_argument("--repository", type=Path, default=Path.cwd())

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
    run.add_argument("--skill")
    run.add_argument(
        "--skill-dir", type=Path, action="append", default=[]
    )
    run.add_argument(
        "--skill-permission",
        action="append",
        default=[],
        help="CAPABILITY=allow|ask|deny for the active Skill (repeatable)",
    )
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


def _mcp_registry() -> McpRegistry:
    return McpRegistry(config_dir() / "vnext" / "mcp-servers.json")


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


def _skill_decisions(values: Sequence[str]) -> dict[Capability, SkillPermission]:
    pairs = _pairs(values, "Skill permission")
    result: dict[Capability, SkillPermission] = {}
    for raw_capability, raw_decision in pairs.items():
        try:
            capability = Capability(raw_capability)
        except ValueError as exc:
            raise ValueError(
                f"unknown Skill capability: {raw_capability}"
            ) from exc
        try:
            result[capability] = SkillPermission(raw_decision)
        except ValueError as exc:
            raise ValueError(
                "Skill permission decisions must use allow, ask, or deny"
            ) from exc
    return result


def _skill_catalog(args: argparse.Namespace) -> SkillCatalog:
    return SkillCatalog(
        args.repository,
        extra_directories=tuple(args.skill_dir),
    )


def _stored_skill(
    record: SessionRecord, name: str
) -> Optional[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for item in record.skills:
        if not isinstance(item, dict):
            raise SkillError("stored Skill selection must be an object")
        if item.get("name") == name:
            matches.append(item)
    if len(matches) > 1:
        raise SkillError(f"session contains duplicate Skill selections: {name}")
    return matches[0] if matches else None


def _replace_stored_skill(
    record: SessionRecord, name: str, selection: Optional[dict[str, Any]]
) -> bool:
    existing = _stored_skill(record, name)
    if existing is None and selection is None:
        return False
    retained = [item for item in record.skills if item is not existing]
    if selection is not None:
        retained.append(selection)
    record.skills = retained
    return True


def _compatible_previous_skill(
    metadata: SkillMetadata, previous: Optional[dict[str, Any]]
) -> Optional[dict[str, Any]]:
    if previous is None:
        return None
    try:
        validate_selection(metadata, previous)
    except SkillError:
        return None
    return previous


def _stored_mcp(
    record: SessionRecord, server_id: str
) -> Optional[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for item in record.mcp_servers:
        if not isinstance(item, dict):
            raise McpAccessDenied("stored MCP selection must be an object")
        if item.get("server_id") == server_id:
            matches.append(item)
    if len(matches) > 1:
        raise McpAccessDenied(
            f"session contains duplicate MCP selections: {server_id}"
        )
    return matches[0] if matches else None


def _replace_stored_mcp(
    record: SessionRecord,
    server_id: str,
    selection: Optional[dict[str, Any]],
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


def _mcp_decisions(values: Sequence[str]) -> dict[str, str]:
    decisions = _pairs(values, "MCP permission")
    invalid = {
        name: decision
        for name, decision in decisions.items()
        if decision not in {"allow", "ask", "deny"}
    }
    if invalid:
        raise ValueError("MCP permission decisions must use allow, ask, or deny")
    return decisions


def _mcp_repository(value: Path) -> Path:
    repository = value.expanduser().resolve(strict=True)
    if not repository.is_dir():
        raise ValueError(f"repository is not a directory: {repository}")
    return repository


def _selected_mcp_runtime(
    record: SessionRecord,
    repository: Path,
) -> tuple[Optional[McpRuntimeBinding], list[McpToolDescriptor]]:
    """Bind immutable registry records after validating every selection first."""
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
            raise McpAccessDenied(
                f"session contains duplicate MCP selections: {server_id}"
            )
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
                    f"selected MCP tool permission is malformed: "
                    f"{server.server_id}/{remote_name}"
                )
            # Only explicitly allowed tools are described to the provider.
            # There is no interactive approval path for ask/deny selections.
            if permission == "allow":
                descriptors.append(descriptor)
        servers.append(server)
    return McpRuntimeBinding(client, repository, descriptors, servers), descriptors


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


def _handle_skill(args: argparse.Namespace) -> int:
    repository = args.repository.expanduser().resolve(strict=True)
    if not repository.is_dir():
        raise ValueError(f"repository is not a directory: {repository}")
    command = args.skill_command
    if command == "deselect":
        store = SessionStore(session_dir())
        lease = store.acquire(
            args.session_id, f"skill-deselect-{os.getpid()}", ttl_seconds=5.0
        )
        try:
            record = store.load(args.session_id)
            store.validate_repository(record, repository)
            removed = _replace_stored_skill(record, args.name, None)
            if removed:
                store.save(record, record.revision, lease)
        finally:
            store.release(lease)
        payload = {
            "name": args.name,
            "session_id": args.session_id,
            "status": "deselected" if removed else "not_selected",
        }
        _emit(payload, json_output=args.json)
        return 0

    catalog = _skill_catalog(args)
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
    elif command == "load":
        payload = {
            "skill": catalog.load(args.name).to_dict(),
            "diagnostics": [item.to_dict() for item in catalog.diagnostics],
        }
    else:
        metadata = catalog.get(args.name)
        decisions = _skill_decisions(args.skill_permission)
        store = SessionStore(session_dir())
        selection: dict[str, Any]
        with store.mutate(
            args.session_id, f"skill-select-{os.getpid()}", ttl_seconds=5.0
        ) as record:
            store.validate_repository(record, repository)
            previous = _compatible_previous_skill(
                metadata, _stored_skill(record, metadata.name)
            )
            selection = skill_selection(
                metadata, decisions, previous=previous
            )
            _replace_stored_skill(record, metadata.name, selection)
        payload = {
            "session_id": args.session_id,
            "selection": selection,
            "diagnostics": [item.to_dict() for item in catalog.diagnostics],
        }
    _emit(payload, json_output=args.json)
    return 0


def _handle_mcp_server(args: argparse.Namespace) -> int:
    registry = _mcp_registry()
    command = args.mcp_server_command
    if command == "add":
        record = McpServerRecord(
            server_id=args.server_id,
            namespace=args.namespace,
            transport=args.transport,
            command=args.command,
            args=tuple(args.arg),
            url=args.url,
            environment=_pairs(args.env, "MCP environment"),
            headers=_pairs(args.header, "MCP header"),
            credential_ref=args.credential_ref,
            credential_target=args.credential_target,
            credential_scheme=args.credential_scheme,
            read_only_tools=tuple(args.read_only_tool),
            timeout_seconds=args.timeout_seconds,
            max_result_bytes=args.max_result_bytes,
            max_message_bytes=args.max_message_bytes,
            max_transport_retries=args.max_transport_retries,
        )
        payload: Any = registry.put(record).to_dict()
    elif command == "remove":
        removed = registry.remove(args.server_id)
        payload = {"server_id": removed.server_id, "status": "removed"}
    elif command == "list":
        payload = [item.to_dict() for item in registry.list()]
    elif command == "show":
        payload = registry.get(args.server_id).to_dict()
    else:
        repository = _mcp_repository(args.repository)
        record = registry.get(args.server_id)
        tools = McpClient(registry).discover_record(record, repository)
        payload = {
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
            raise McpAccessDenied(
                f"MCP server is not selected for this session: {args.server_id}"
            )
        _emit(selection, json_output=args.json)
        return 0
    if command == "deselect":
        lease = store.acquire(
            args.session_id, f"mcp-deselect-{os.getpid()}", ttl_seconds=5.0
        )
        try:
            record = store.load(args.session_id)
            store.validate_repository(record, repository)
            removed = _replace_stored_mcp(record, args.server_id, None)
            if removed:
                store.save(record, record.revision, lease)
        finally:
            store.release(lease)
        payload = {
            "server_id": args.server_id,
            "session_id": args.session_id,
            "status": "deselected" if removed else "not_selected",
        }
        _emit(payload, json_output=args.json)
        return 0

    registry = _mcp_registry()
    server = registry.get(args.server_id)
    tools = McpClient(registry).discover_record(server, repository)
    decisions = _mcp_decisions(args.permission)
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
    payload = {"session_id": args.session_id, "selection": selection}
    _emit(payload, json_output=args.json)
    return 0


def _handle_mcp_call(args: argparse.Namespace) -> int:
    repository = _mcp_repository(args.repository)
    def reject_constant(value: str) -> None:
        raise ValueError(f"non-finite JSON number is not allowed: {value}")

    try:
        arguments = json.loads(args.arguments, parse_constant=reject_constant)
    except (json.JSONDecodeError, ValueError) as exc:
        detail = exc.msg if isinstance(exc, json.JSONDecodeError) else str(exc)
        raise ValueError(f"MCP arguments are not valid JSON: {detail}") from exc
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
            record.session_id, f"mcp-call-{os.getpid()}", ttl_seconds=30.0
        )
    try:
        result = core.execute(command, lease=lease)
    finally:
        if lease is not None:
            store.release(lease)
    _emit(result.to_dict(), json_output=args.json)
    return 0


def _handle_mcp_credential(args: argparse.Namespace) -> int:
    store = McpCredentialStore()
    if args.mcp_credential_command == "set":
        secret = (
            sys.stdin.readline().rstrip("\r\n")
            if args.stdin
            else getpass.getpass("MCP credential: ")
        )
        payload = store.set(args.name, secret)
    elif args.mcp_credential_command == "show":
        reference = f"os-keyring:mcp/{args.name}"
        secret = store.resolve(reference)
        payload = {
            "reference": reference,
            "fingerprint": store.fingerprint(secret),
            "status": "available",
        }
    elif args.mcp_credential_command == "delete":
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
    return _handle_mcp_call(args)


def _handle_bridge(args: argparse.Namespace) -> int:
    if args.bridge_command == "list":
        payload: Any = [item.to_dict() for item in BridgeRegistry().list()]
    elif args.bridge_command == "show":
        payload = BridgeRegistry().get(args.name).to_dict()
    elif args.bridge_command == "doctor":
        payload = BridgeCredentialStore().doctor()
    else:
        store = BridgeCredentialStore()
        command = args.bridge_credential_command
        if command == "set":
            payload = store.set(args.name)
        elif command == "show":
            reference = f"os-keyring:bridge/{args.name}"
            secret = store.resolve(reference)
            payload = {
                "reference": reference,
                "fingerprint": store.fingerprint(secret),
                "status": "available",
            }
        elif command == "rotate-key":
            payload = store.rotate(args.name)
        else:
            payload = store.delete(args.name)
    _emit(payload, json_output=args.json)
    return 0


def _pack_registry() -> PackRegistry:
    return PackRegistry(runtime_dir() / "vnext" / "packs")


def _handle_pack(args: argparse.Namespace) -> int:
    command = args.pack_command
    if command == "create":
        target = create_pack_template(args.target, name=args.name, description=args.description)
        payload: Any = {"path": str(target), "name": args.name, "status": "created"}
    else:
        registry = _pack_registry()
        if command == "install":
            pack = registry.install(args.source, approved_permissions=args.allow)
            payload = pack.to_dict()
        elif command == "remove":
            payload = registry.remove(args.identity).to_dict()
        elif command == "list":
            payload = [item.to_dict() for item in registry.list()]
        elif command == "inspect":
            payload = registry.get(args.identity).to_dict()
        elif command == "doctor":
            payload = registry.doctor(args.identity)
        elif command == "enable":
            payload = registry.enable(args.identity).to_dict()
        else:
            payload = registry.disable(args.identity).to_dict()
    _emit(payload, json_output=args.json)
    return 0


def _run_agent(args: argparse.Namespace) -> AgentReport:
    repository = args.repository.expanduser().resolve(strict=True)
    if not repository.is_dir():
        raise ValueError(f"repository is not a directory: {repository}")
    limits = AgentLimits(max_steps=args.max_steps, max_seconds=args.max_seconds)
    store = SessionStore(session_dir())
    record: SessionRecord | None = None
    if args.session_id and store.state_path(args.session_id).exists():
        record = store.load(args.session_id)
        store.validate_repository(record, repository)
        if record.access_profile != AccessProfile.WORKSPACE_WRITE.value:
            raise SessionError("native agent requires a workspace_write session")
        if record.task != str(redact(args.task)):
            raise SessionError("resume task differs from the existing session task")
    provider, model = _agent_provider(args, record, limits)

    content = None
    selection: dict[str, Any] | None = None
    if args.skill is None:
        if args.skill_permission:
            raise ValueError("--skill-permission requires --skill")
    else:
        catalog = SkillCatalog(
            repository, extra_directories=tuple(args.skill_dir)
        )
        content = catalog.load(args.skill)
        decisions = _skill_decisions(args.skill_permission)
        previous = None
        if record is not None:
            previous = _compatible_previous_skill(
                content.metadata,
                _stored_skill(record, content.metadata.name),
            )
        selection = skill_selection(
            content.metadata, decisions, previous=previous
        )

    if record is None:
        record = store.create(
            repository,
            args.task,
            AccessProfile.WORKSPACE_WRITE,
            session_id=args.session_id,
        )

    mcp_binding, _ = _selected_mcp_runtime(record, repository)

    native_origin = Origin(OriginKind.NATIVE_AGENT, f"cli-{record.session_id}")
    origin = native_origin
    policy = CapabilityPolicy(AccessProfile.WORKSPACE_WRITE)
    policy.set_grants(
        native_origin,
        {
            Capability.REPO_READ,
            Capability.REPO_WRITE,
            Capability.PROCESS_RUN,
            Capability.CHECKS_RUN,
            Capability.GIT_READ,
            Capability.MCP_CALL,
        },
    )
    system_prompt = SYSTEM_PROMPT
    if content is not None and selection is not None:
        origin = configure_skill_policy(
            policy,
            content.metadata,
            selection,
            parent=native_origin.key,
        )
        system_prompt += skill_system_prompt(content)
        with store.mutate(
            record.session_id,
            f"agent-skill-{os.getpid()}",
            ttl_seconds=5.0,
        ) as current:
            store.validate_repository(current, repository)
            _replace_stored_skill(current, content.metadata.name, selection)
    core = CoreRuntime(
        repository,
        policy,
        store,
        runtime_dir() / "vnext" / "audit.jsonl",
        mcp_binding=mcp_binding,
    )
    return AgentKernel(
        provider=provider,
        model=model,
        core=core,
        sessions=store,
        origin=origin,
        limits=limits,
        system_prompt=system_prompt,
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

        if args.command == "skill":
            return _handle_skill(args)

        if args.command == "mcp":
            return _handle_mcp(args)

        if args.command == "bridge":
            return _handle_bridge(args)

        if args.command == "pack":
            return _handle_pack(args)

        if args.command == "tui":
            from .tui import run_tui

            return run_tui(
                session_id=args.session_id,
                repository=str(args.repository.expanduser().resolve()),
            )

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
        if args.session_command == "handoff":
            record = store.load(args.session_id)
            repository = args.repository.expanduser().resolve(strict=True)
            store.validate_repository(record, repository)
            document = build_handoff(record, repository=repository)
            _json(document) if args.json else _print_mapping(document)
            return 0
        if args.session_command == "lock":
            record = store.load(args.session_id)
            lease_path = store.lease_path(args.session_id)
            info: Any = {
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
            _json(info) if args.json else _print_mapping(info)
            return 0
        record = store.load(args.session_id)
        payload = record.to_dict()
        _json(payload) if args.json else _print_mapping(payload)
        return 0
    except (
        AgentError,
        BridgeError,
        CredentialError,
        CoreError,
        MigrationError,
        McpError,
        PackError,
        ProviderError,
        RegistryError,
        SessionError,
        SkillError,
        OSError,
        ValueError,
    ) as exc:
        print(f"karox: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
