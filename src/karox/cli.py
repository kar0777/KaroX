"""Command-line entry point for the KaroX 5 hybrid runtime."""

from __future__ import annotations

import argparse
import contextlib
import getpass
import hashlib
import json
import os
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from .agent import (
    AgentError,
    AgentEvent,
    AgentEventKind,
    AgentKernel,
    AgentLimits,
    AgentReport,
    ContextBudget,
    SYSTEM_PROMPT,
)
from .browser_access import BrowserAccessPolicy
from .browser_credentials import BrowserCredentialStore
from . import clipboard
from .bridge import (
    BridgeCredentialStore,
    BridgeError,
    BridgeRegistry,
)
# Aliased on import: ``connections.ConnectionError`` is a ``RuntimeError``
# subclass, not the builtin, so binding it to the builtin's name here would
# shadow the builtin for the whole module -- and the builtin is caught via
# ``OSError`` in ``main``, while this one needs naming explicitly.
from .connections import ConnectionError as SavedConnectionError
from .connection_controller import connection_controller
from .connection_runtime import ConnectionRuntimeError
from .core import CoreError, CoreRuntime
from .core_tools import ExtendedCoreRuntime
from .credentials import CredentialError, CredentialStore
from .ecosystem import (
    INTEGRATION_PRESETS,
    TARGET_PRESETS,
    TOOL_PRESETS,
    EcosystemRegistry,
)
from .hosted_bridge import (
    AUTONOMY_TOOL_NAMES,
    CORE_TOOL_NAMES,
    DEFAULT_HOSTED_DEADLINE_SECONDS,
    HOSTED_EXTRA_TOOL_NAMES,
    KNOWN_HOSTED_TOOL_NAMES,
    CompositeHostedBridge,
    CoreToolBridge,
    HostedBridgeError,
)
from .autonomy_runtime import AutonomyRuntime
from .hosted_tools_runtime import (
    HostedToolsRuntime,
    ManagedServerProfile,
    default_server_profiles,
    server_profiles_for_repository,
)
from . import __version__
from .migration import MigrationError, migrate_legacy_metadata
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
from .models import AccessProfile, Capability, CoreCommand, Origin, OriginKind
from .promptql_outbound import (
    PromptQLInvocationError,
    PromptQLNaturalLanguageClient,
    load_promptql_target,
)
from .packs import (
    PackError,
    PackRegistry,
    create_pack_template,
)
from .paths import (
    config_dir,
    legacy_config_dir,
    migration_dir,
    oauth_state_dir,
    runtime_dir,
    session_dir,
)
from .policy import CapabilityPolicy
from .project_context import discover_project_context
from .project_registry import ProjectRegistry, ProjectRegistryError
from .research_subagent import ResearchLimits, ResearchSubagent, build_research_context
from .openapi_bridge import build_openapi_bridge_app
from .oauth_bridge import build_oauth_proxy_asgi_app
from .proxy import McpProxy
from .proxy_server import build_proxy_asgi_app, normalize_host
from .provider_controller import ProviderController
from .provider_factory import ProviderFactory
from .provider_presets import provider_preset, provider_presets
from .providers import (
    ModelMessage,
    ModelRequest,
    OpenAIChatCompletionsProvider,
    ProviderError,
    REASONING_EFFORTS,
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
from .handoff import build_handoff
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
from .tailscale import tailscale_doctor
from .verification import discover_verification_commands
from .web_bridge_launcher import (
    DEFAULT_WEB_TOOLS,
    WRITE_WEB_TOOLS,
    WEB_BRIDGE_PROFILES,
    WebBridgeConnectConfig,
    WebBridgeLaunchError,
    _include_stable_worker_commands,
    delete_saved_web_bridge_identity,
    reap_orphaned_web_bridges,
    run_web_bridge,
    saved_web_bridge_identity_exists,
    saved_web_bridge_session_id,
    web_bridge_diagnostics,
)
from .web_bridge_profiles import (
    SavedWebBridgeProfile,
    WebBridgeProfileStore,
)


def _write_line(text: str) -> None:
    """Print one line, degrading characters the console cannot encode.

    Windows gives a redirected or legacy-code-page stdout the system encoding,
    and ``cp866`` has no ``•``: printing a masked secret there raised
    ``UnicodeEncodeError`` and took the whole command down with exit code 2 --
    a display command failing over a decorative character.  Russian help text
    and model output are the same hazard on the same streams.  ``_stream_progress``
    already made this trade for stderr; this is the stdout half of it, so an
    unencodable character degrades to a replacement and the line still arrives.
    """
    try:
        print(text)
    except UnicodeEncodeError:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        print(text.encode(encoding, "replace").decode(encoding, "replace"))


def _json(value: Any) -> None:
    _write_line(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _verification_command(value: str) -> tuple[str, ...]:
    """Parse an approved command across PowerShell native argv boundaries."""
    candidates = [value]
    if "\\\"" in value:
        try:
            unescaped = json.loads(f'"{value}"')
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(unescaped, str):
                candidates.append(unescaped)

    decoded: Any = None
    last_error: Optional[Exception] = None
    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
            last_error = None
            break
        except (TypeError, json.JSONDecodeError) as exc:
            last_error = exc

    stripped = value.strip()
    if last_error is not None and stripped.startswith("[") and stripped.endswith("]"):
        inner = stripped[1:-1]
        parts = [item.strip() for item in inner.split(",")]
        if inner and parts and all(parts) and not any('"' in item for item in parts):
            decoded = parts
            last_error = None

    if last_error is not None:
        raise ValueError(
            "verification command must be a JSON array of strings; in Windows "
            "PowerShell escape embedded quotes with backslashes"
        ) from last_error
    if (
        not isinstance(decoded, list)
        or not decoded
        or len(decoded) > 100
        or not all(isinstance(item, str) and item for item in decoded)
    ):
        raise ValueError("verification command must contain 1-100 non-empty strings")
    return tuple(decoded)


def _cleanup_hosted_runtimes(runtimes: Sequence[Any], session_id: str) -> None:
    """Tear down browser sessions and stop dev servers owned by a bridge.

    ``bridge serve`` builds a list of runtimes composed into one bridge; when
    uvicorn returns (Ctrl+C or a fatal error), every runtime that owns a
    browser or a managed process must release them so the bridge exit leaves
    no orphaned Chromium or dev server behind.
    """
    if not session_id:
        return
    for runtime in runtimes:
        cleanup = getattr(runtime, "cleanup_session", None)
        if not callable(cleanup):
            continue
        try:
            cleanup()
        except Exception:
            # Teardown is best-effort: a failing stop must not mask the original
            # exit reason, and the launcher reaps orphans on the next run too.
            pass


def _server_profile(value: str) -> ManagedServerProfile:
    """Parse a user-approved dev-server profile across PowerShell argv boundaries.

    Mirrors :func:`_verification_command`: the JSON object may arrive with its
    quotes escaped by PowerShell, so both the raw and unescaped forms are
    tried.  The caller has already typed it as a ``str``; nothing else is
    accepted because a server profile carries an executable argv.
    """
    candidates = [value]
    if "\\\"" in value:
        try:
            unescaped = json.loads(f'"{value}"')
        except json.JSONDecodeError:
            pass
        else:
            if isinstance(unescaped, str):
                candidates.append(unescaped)
    decoded: Any = None
    last_error: Optional[Exception] = None
    for candidate in candidates:
        try:
            decoded = json.loads(candidate)
            last_error = None
            break
        except (TypeError, json.JSONDecodeError) as exc:
            last_error = exc
    if last_error is not None:
        raise ValueError(
            "server profile must be a JSON object; in Windows PowerShell escape "
            "embedded quotes with backslashes"
        ) from last_error
    if not isinstance(decoded, dict):
        raise ValueError("server profile must be a JSON object")
    name = decoded.get("name")
    argv = decoded.get("argv")
    if not isinstance(name, str) or not name:
        raise ValueError("server profile requires a non-empty name")
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(item, str) and item for item in argv)
    ):
        raise ValueError("server profile argv must be a non-empty string array")
    env_keys = decoded.get("env_keys", [])
    if not isinstance(env_keys, list):
        env_keys = []
    env_allowlist = decoded.get("env_allowlist", [])
    if not isinstance(env_allowlist, list):
        env_allowlist = []
    # The public form persists env *keys* (never values); rebuild a value-less
    # env map for the runtime object, which validates the rest.
    env = {str(k): "" for k in env_keys if isinstance(k, str)}
    return ManagedServerProfile(
        name=name,
        argv=tuple(str(item) for item in argv),
        env=env,
        env_allowlist=frozenset(str(item) for item in env_allowlist),
        host_hint=str(decoded.get("host_hint", "127.0.0.1")),
        ready_url=decoded.get("ready_url"),
    )


def _record_summary(record: SessionRecord) -> dict[str, Any]:
    usage = record.usage if isinstance(record.usage, dict) else {}
    costs = usage.get("costs")
    checks = [item for item in record.checks if isinstance(item, dict)]
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
        # Deciding which task to resume needs to know what it already did, not
        # only that it exists. All of this is already in the durable record, so
        # a caller no longer has to load every session individually to choose.
        "changed_files": len(record.changed_files),
        "checks": len(checks),
        "checks_failed": sum(1 for item in checks if item.get("ok") is False),
        "requests": usage.get("requests"),
        "total_tokens": usage.get("total_tokens"),
        "costs": dict(costs) if isinstance(costs, dict) else {},
    }


def _add_browser_policy_arguments(command: argparse.ArgumentParser) -> None:
    """Add the explicit external-browser policy shared by bridge launch paths."""
    command.add_argument(
        "--browser-external-https",
        action="store_true",
        help="allow public HTTPS sites after DNS/private-address validation",
    )
    command.add_argument(
        "--browser-domain",
        action="append",
        default=[],
        help="allow one external domain and its subdomains for this browser session",
    )
    command.add_argument(
        "--browser-deny-domain",
        action="append",
        default=[],
        help="deny one domain and its subdomains for this browser session",
    )
    command.add_argument(
        "--browser-headed",
        action="store_true",
        help="show a dedicated Chromium window owned by this KaroX session",
    )
    command.add_argument(
        "--browser-user-takeover",
        action="store_true",
        help="allow pausing agent input while the user completes sensitive steps",
    )
    command.add_argument(
        "--browser-network-inspection",
        action="store_true",
        help="expose redacted request metadata and selected billing/model fields",
    )
    command.add_argument(
        "--browser-payment-confirmation",
        action="store_true",
        help="explicitly permit payment/checkout confirmation actions (off by default)",
    )
    command.add_argument(
        "--browser-allowed-email",
        action="append",
        default=[],
        help="email address the agent may fill in this browser session",
    )
    command.add_argument(
        "--browser-credential-ref",
        action="append",
        default=[],
        help=argparse.SUPPRESS,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="karox", description="KaroX hybrid runtime")
    # `karox --version` is the first thing anyone types to find out what they have
    # installed, and it answered with an argparse usage error because the parser
    # required a subcommand before it looked at any flag.
    parser.add_argument(
        "--version",
        action="version",
        version=f"karox {__version__}",
        help="show the installed runtime version and exit",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    paths = commands.add_parser("paths", help="show resolved application paths")
    paths.add_argument("--json", action="store_true")

    session = commands.add_parser("session", help="manage durable KaroX sessions")
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
    revoke = sessions.add_parser("revoke", help="emergency-revoke a session")
    revoke.add_argument("session_id")
    revoke.add_argument("--json", action="store_true")
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
    credentials = credential.add_subparsers(dest="credential_command", required=True)
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
    credential_doctor.add_argument(
        "--reference",
        help=(
            "report on one credential reference rather than the keyring; an "
            "env: reference needs no keyring at all"
        ),
    )
    credential_doctor.add_argument("--json", action="store_true")

    browser_credential = commands.add_parser(
        "browser-credential",
        help="manage test-account browser logins in the operating-system keyring",
    )
    browser_credentials = browser_credential.add_subparsers(
        dest="browser_credential_command", required=True
    )
    browser_credential_set = browser_credentials.add_parser(
        "set", help="store a browser username/password bundle locally"
    )
    browser_credential_set.add_argument("name")
    browser_credential_set.add_argument(
        "--stdin",
        action="store_true",
        help="read username and password from two lines of standard input",
    )
    browser_credential_set.add_argument("--json", action="store_true")
    browser_credential_delete = browser_credentials.add_parser(
        "delete", help="delete a stored browser login bundle"
    )
    browser_credential_delete.add_argument("name")
    browser_credential_delete.add_argument("--json", action="store_true")
    browser_credential_doctor = browser_credentials.add_parser(
        "doctor", help="verify secure browser credential storage availability"
    )
    browser_credential_doctor.add_argument("--json", action="store_true")

    provider = commands.add_parser("provider", help="manage API providers")
    providers = provider.add_subparsers(dest="provider_command", required=True)
    provider_add = providers.add_parser("add", help="add a provider")
    provider_add.add_argument("provider_id")
    provider_add.add_argument("--adapter", choices=sorted(ADAPTER_KINDS), required=True)
    provider_add.add_argument("--base-url", required=True)
    provider_add.add_argument("--credential-ref")
    provider_add.add_argument(
        "--privacy-class", choices=sorted(PRIVACY_CLASSES), default="public"
    )
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
    provider_remove.add_argument(
        "--delete-credential",
        action="store_true",
        help="delete an unshared OS-keyring credential after registry removal",
    )
    provider_remove.add_argument("--json", action="store_true")
    provider_list = providers.add_parser("list", help="list providers")
    provider_list.add_argument("--json", action="store_true")
    provider_show = providers.add_parser("show", help="show a provider")
    provider_show.add_argument("provider_id")
    provider_show.add_argument("--json", action="store_true")
    provider_details = providers.add_parser(
        "details", help="show provider, models, selection, and credential status"
    )
    provider_details.add_argument("provider_id")
    provider_details.add_argument("--json", action="store_true")
    provider_credential_set = providers.add_parser(
        "credential-set", help="attach or rotate a provider credential"
    )
    provider_credential_set.add_argument("provider_id")
    provider_credential_set.add_argument("--name")
    provider_credential_set.add_argument("--stdin", action="store_true")
    provider_credential_set.add_argument("--json", action="store_true")
    provider_credential_clear = providers.add_parser(
        "credential-clear", help="detach a provider credential reference"
    )
    provider_credential_clear.add_argument("provider_id")
    provider_credential_clear.add_argument("--delete-stored", action="store_true")
    provider_credential_clear.add_argument("--json", action="store_true")
    provider_test = providers.add_parser("test", help="send a minimal model request")
    provider_test.add_argument("provider_id")
    provider_test.add_argument("--model")
    provider_test.add_argument("--json", action="store_true")
    provider_presets_command = providers.add_parser(
        "presets", help="list native provider presets"
    )
    provider_presets_command.add_argument("--json", action="store_true")
    provider_preset_add = providers.add_parser(
        "add-preset", help="add a provider from a native preset"
    )
    provider_preset_add.add_argument("preset_id")
    provider_preset_add.add_argument("--provider-id")
    provider_preset_add.add_argument("--base-url")
    provider_preset_add.add_argument("--credential-ref")
    provider_preset_add.add_argument("--json", action="store_true")
    provider_setup = providers.add_parser(
        "setup",
        help="configure, verify, and select one provider/model in a single command",
    )
    provider_setup.add_argument(
        "preset_id",
        help="provider preset ID (for example openrouter, anthropic, or openai-compatible)",
    )
    provider_setup.add_argument("--provider-id")
    provider_setup.add_argument("--base-url")
    provider_setup.add_argument("--model", required=True)
    provider_setup.add_argument("--context-window", type=int)
    provider_setup.add_argument("--max-output-tokens", type=int)
    provider_setup_credential = provider_setup.add_mutually_exclusive_group()
    provider_setup_credential.add_argument(
        "--credential-ref",
        help="existing env:NAME or os-keyring:provider/NAME reference",
    )
    provider_setup_credential.add_argument(
        "--stdin-key",
        action="store_true",
        help="read the API key from stdin and store it in the OS keyring after verification",
    )
    provider_setup.add_argument(
        "--no-test",
        action="store_true",
        help="save without the minimal live model request",
    )
    provider_setup.add_argument(
        "--no-activate",
        action="store_true",
        help="save the model without making it the default",
    )
    provider_setup.add_argument("--pricing-version")
    provider_setup.add_argument("--currency")
    provider_setup.add_argument("--input-per-million", type=float)
    provider_setup.add_argument("--output-per-million", type=float)
    provider_setup.add_argument("--cache-read-per-million", type=float)
    provider_setup.add_argument("--cache-write-per-million", type=float)
    provider_setup.add_argument("--pricing-source")
    provider_setup.add_argument("--json", action="store_true")

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
    model_add.add_argument(
        "--cache-read-per-million",
        type=float,
        help=(
            "price of a prompt served from cache; defaults to a tenth of the "
            "input rate, which is what the providers publish"
        ),
    )
    model_add.add_argument(
        "--cache-write-per-million",
        type=float,
        help=(
            "price of a prompt written to cache; defaults to 1.25x the input "
            "rate, which is what the providers publish"
        ),
    )
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
    model_discover = models.add_parser(
        "discover", help="discover models from a configured provider"
    )
    model_discover.add_argument("--provider", required=True)
    model_discover.add_argument("--json", action="store_true")
    model_map = models.add_parser("map", help="map an alias to a provider model")
    model_map.add_argument("alias", choices=("sol", "fable", "kimi"))
    model_map.add_argument("model_id")
    model_map.add_argument("--provider", required=True)
    model_map.add_argument("--json", action="store_true")
    model_repair = models.add_parser(
        "repair-selection",
        help="select the first deterministic model when no default is selected",
    )
    model_repair.add_argument("--provider")
    model_repair.add_argument("--json", action="store_true")

    def add_ecosystem_commands(
        name: str, help_text: str, *, target: bool = False
    ) -> None:
        root = commands.add_parser(name, help=help_text)
        actions = root.add_subparsers(dest=f"{name}_command", required=True)
        for action_name, action_help in (
            ("presets", "list available presets"),
            ("list", "list configured entries"),
        ):
            action = actions.add_parser(action_name, help=action_help)
            action.add_argument("--json", action="store_true")
        add = actions.add_parser("add", help="add a disabled preset configuration")
        add.add_argument("preset_id")
        add.add_argument("--id")
        add.add_argument("--json", action="store_true")
        configure = actions.add_parser(
            "configure", help="configure non-secret settings"
        )
        configure.add_argument("item_id")
        configure.add_argument("--setting", action="append", default=[])
        configure.add_argument("--credential-ref")
        if name == "integration":
            configure.add_argument("--telemetry-field", action="append", default=[])
        configure.add_argument("--json", action="store_true")
        for action_name in ("doctor", "remove"):
            action = actions.add_parser(action_name)
            action.add_argument("item_id")
            action.add_argument("--json", action="store_true")
        if target:
            handoff = actions.add_parser(
                "handoff", help="generate target connection guidance"
            )
            handoff.add_argument("item_id")
            handoff.add_argument("--json", action="store_true")
            ask = actions.add_parser(
                "ask",
                help="invoke a hosted agent target (promptql Natural Language API)",
            )
            ask.add_argument("item_id")
            ask.add_argument("--message", required=True, help="user message to send")
            ask.add_argument(
                "--prior-interactions",
                default=None,
                help="JSON array of prior interaction objects for multi-turn context",
            )
            ask.add_argument(
                "--deadline-seconds",
                type=float,
                default=60.0,
                help="maximum request duration in seconds",
            )
            ask.add_argument("--json", action="store_true")
        else:
            for action_name in ("enable", "disable", "status"):
                action = actions.add_parser(action_name)
                action.add_argument("item_id")
                action.add_argument("--json", action="store_true")

    add_ecosystem_commands("target", "manage external agent targets", target=True)
    add_ecosystem_commands("tool", "manage optional tool providers")
    add_ecosystem_commands("integration", "manage opt-in CI and observability")

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
    mcp_servers = mcp_server.add_subparsers(dest="mcp_server_command", required=True)
    mcp_server_add = mcp_servers.add_parser("add", help="add or replace a server")
    mcp_server_add.add_argument("server_id")
    mcp_server_add.add_argument("--namespace", required=True)
    mcp_server_add.add_argument(
        "--transport", choices=("stdio", "streamable_http"), required=True
    )
    mcp_server_add.add_argument("--command", dest="mcp_stdio_command")
    mcp_server_add.add_argument("--arg", action="append", default=[])
    mcp_server_add.add_argument("--url")
    mcp_server_add.add_argument("--env", action="append", default=[])
    mcp_server_add.add_argument("--header", action="append", default=[])
    mcp_server_add.add_argument("--credential-ref")
    mcp_server_add.add_argument("--credential-target")
    mcp_server_add.add_argument("--credential-scheme", default="Bearer")
    mcp_server_add.add_argument(
        "--oauth",
        action="store_true",
        help="authenticate this Streamable HTTP server with OAuth 2.1/PKCE",
    )
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
    mcp_server_authorize = mcp_servers.add_parser(
        "authorize", help="complete OAuth authorization for a remote MCP server"
    )
    mcp_server_authorize.add_argument("server_id")
    mcp_server_authorize.add_argument(
        "--force",
        action="store_true",
        help="discard the stored OAuth grant and register a fresh client",
    )
    mcp_server_authorize.add_argument("--json", action="store_true")
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
    mcp_sessions = mcp_session.add_subparsers(dest="mcp_session_command", required=True)
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
    mcp_session_permissions.add_argument("--repository", type=Path, default=Path.cwd())
    mcp_session_permissions.add_argument("--json", action="store_true")

    mcp_status = mcp_commands.add_parser(
        "status", help="one aggregated screen: configured, live, allowed, blocked"
    )
    mcp_status.add_argument(
        "--session-id",
        help="without it the screen shows configuration only, no authorization",
    )
    mcp_status.add_argument("--repository", type=Path, default=Path.cwd())
    mcp_status.add_argument(
        "--probe",
        action="store_true",
        help="contact every server; without it liveness stays not_probed",
    )
    mcp_status.add_argument("--json", action="store_true")

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
    mcp_credential_delete = mcp_credentials.add_parser("delete", help="delete a secret")
    mcp_credential_delete.add_argument("name")
    mcp_credential_delete.add_argument("--json", action="store_true")
    mcp_credential_doctor = mcp_credentials.add_parser(
        "doctor", help="verify secure MCP credential storage"
    )
    mcp_credential_doctor.add_argument("--json", action="store_true")

    # The saved MCP *client* connections (ClickUp and friends) were reachable
    # only from the TUI's Connections screens.  That made the one value a user
    # cannot retype from memory -- an auto-generated bridge secret -- readable
    # nowhere: the card masks it, and a masked secret plus a clipboard button is
    # not enough when the paste target is another application's form field.
    connections = commands.add_parser(
        "connections",
        help="inspect saved MCP client connections outside the TUI",
    )
    connection_commands = connections.add_subparsers(
        dest="connections_command", required=True
    )
    connections_list = connection_commands.add_parser(
        "list", help="list saved connections"
    )
    connections_list.add_argument("--json", action="store_true")
    connections_show = connection_commands.add_parser(
        "show", help="show one connection, with the secret masked by default"
    )
    connections_show.add_argument("connection_id")
    connections_show.add_argument(
        "--reveal-secret",
        action="store_true",
        help=(
            "print the secret value instead of its mask; it is written to "
            "standard output, so redirect or clear the scrollback afterwards"
        ),
    )
    connections_show.add_argument("--json", action="store_true")
    connections_copy_auth = connection_commands.add_parser(
        "copy-auth",
        help="copy an authorization value to the clipboard without printing it",
    )
    connections_copy_auth.add_argument("connection_id")
    connections_copy_auth.add_argument("--json", action="store_true")
    connections_test = connection_commands.add_parser(
        "test", help="run the MCP handshake against a connection"
    )
    connections_test.add_argument("connection_id")
    connections_test.add_argument(
        "--url",
        help="override the endpoint URL (defaults to the connection's own)",
    )
    connections_test.add_argument("--timeout-seconds", type=float, default=15.0)
    connections_test.add_argument("--json", action="store_true")
    connections_support = connection_commands.add_parser(
        "launch-support",
        help="explain whether the saved connection has a managed launcher",
    )
    connections_support.add_argument("connection_id")
    connections_support.add_argument("--json", action="store_true")
    connections_start = connection_commands.add_parser(
        "start", help="idempotently start a saved connection"
    )
    connections_start.add_argument("connection_id")
    connections_start.add_argument("--json", action="store_true")
    connections_restart = connection_commands.add_parser(
        "restart", help="safely stop and relaunch a saved connection"
    )
    connections_restart.add_argument("connection_id")
    connections_restart.add_argument("--json", action="store_true")
    connections_status = connection_commands.add_parser(
        "status", help="show managed runtime state for one connection"
    )
    connections_status.add_argument("connection_id")
    connections_status.add_argument("--json", action="store_true")
    connections_stop = connection_commands.add_parser(
        "stop", help="stop a connection runtime owned by this KaroX process"
    )
    connections_stop.add_argument("connection_id")
    connections_stop.add_argument("--json", action="store_true")
    connections_remove = connection_commands.add_parser(
        "remove", help="remove a connection and its stored secret"
    )
    connections_remove.add_argument("connection_id")
    connections_remove.add_argument("--json", action="store_true")

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
        "doctor",
        help=(
            "verify secure bridge credential storage and revoke web bridges "
            "orphaned by a hard kill"
        ),
    )
    bridge_doctor.add_argument("--json", action="store_true")
    bridge_saved = bridge_commands.add_parser(
        "saved", help="create and manage reusable secret-free connection profiles"
    )
    bridge_saved_commands = bridge_saved.add_subparsers(
        dest="bridge_saved_command", required=True
    )
    bridge_saved_list = bridge_saved_commands.add_parser(
        "list", help="list saved connection profiles"
    )
    bridge_saved_list.add_argument("--json", action="store_true")
    bridge_saved_show = bridge_saved_commands.add_parser(
        "show", help="show one saved connection profile"
    )
    bridge_saved_show.add_argument("name")
    bridge_saved_show.add_argument("--json", action="store_true")
    bridge_saved_delete = bridge_saved_commands.add_parser(
        "delete", help="delete one saved connection profile"
    )
    bridge_saved_delete.add_argument("name")
    bridge_saved_delete.add_argument("--json", action="store_true")
    bridge_saved_validate = bridge_saved_commands.add_parser(
        "validate", help="validate a saved profile and print effective diagnostics"
    )
    bridge_saved_validate.add_argument("name")
    bridge_saved_validate.add_argument("--repository", type=Path)
    bridge_saved_validate.add_argument("--json", action="store_true")

    bridge_saved_developer = bridge_saved_commands.add_parser(
        "ensure-developer",
        help="create or update a durable full workspace developer profile",
    )
    bridge_saved_developer.add_argument("name")
    bridge_saved_developer.add_argument("--repository", type=Path, required=True)
    bridge_saved_developer.add_argument(
        "--target-profile",
        choices=WEB_BRIDGE_PROFILES,
        default="chatgpt-web",
    )
    bridge_saved_developer.add_argument(
        "--tunnel",
        choices=("tailscale", "custom"),
        default="tailscale",
    )
    bridge_saved_developer.add_argument("--public-url")
    bridge_saved_developer.add_argument("--language", choices=("en", "ru"), default="ru")
    bridge_saved_developer.add_argument("--port", type=int, default=8765)
    bridge_saved_developer.add_argument("--reset-identity", action="store_true")
    bridge_saved_developer.add_argument("--json", action="store_true")

    bridge_saved_create = bridge_saved_commands.add_parser(
        "create", help="create a saved connection profile"
    )
    _add_browser_policy_arguments(bridge_saved_create)
    bridge_saved_create.add_argument("name")
    bridge_saved_create.add_argument(
        "--target-profile",
        choices=WEB_BRIDGE_PROFILES,
        default="chatgpt-web",
    )
    bridge_saved_create.add_argument("--repository", type=Path)
    bridge_saved_create.add_argument(
        "--tool", action="append", choices=tuple(sorted(CORE_TOOL_NAMES))
    )
    bridge_saved_create.add_argument("--write", action="store_true")
    bridge_saved_create.add_argument(
        "--access-profile",
        choices=[item.value for item in AccessProfile],
    )
    bridge_saved_create.add_argument(
        "--tunnel",
        choices=("cloudflare", "tailscale", "custom"),
        default="cloudflare",
    )
    bridge_saved_create.add_argument("--public-url")
    bridge_saved_create.add_argument("--language", choices=("en", "ru"), default="en")
    bridge_saved_create.add_argument("--port", type=int, default=8765)
    bridge_saved_create.add_argument(
        "--deadline-preset",
        choices=("standard", "long", "full-suite"),
    )
    bridge_saved_create.add_argument("--deadline-seconds", type=float)
    bridge_saved_create.add_argument(
        "--tunnel-timeout-seconds", type=float, default=30.0
    )
    bridge_saved_create.add_argument(
        "--verification-command", action="append", default=[]
    )
    bridge_saved_create.add_argument(
        "--server-profile",
        action="append",
        default=[],
        help=(
            "user-approved dev-server profile as a JSON object "
            "({name, argv, env_keys, env_allowlist, host_hint}); repeatable"
        ),
    )
    bridge_saved_create.add_argument(
        "--mcp-server",
        action="append",
        default=[],
        help="external MCP server ID to proxy through this hosted bridge; repeatable",
    )
    bridge_saved_create.add_argument("--json", action="store_true")

    bridge_saved_edit = bridge_saved_commands.add_parser(
        "edit", help="change selected fields of a saved connection profile"
    )
    bridge_saved_edit.add_argument("name")
    bridge_saved_edit.add_argument(
        "--target-profile", choices=WEB_BRIDGE_PROFILES
    )
    bridge_saved_edit.add_argument("--repository", type=Path)
    bridge_saved_edit.add_argument("--clear-repository", action="store_true")
    bridge_saved_edit.add_argument(
        "--tool", action="append", choices=tuple(sorted(CORE_TOOL_NAMES))
    )
    bridge_saved_edit.add_argument("--write", action="store_true")
    bridge_saved_edit.add_argument(
        "--access-profile", choices=[item.value for item in AccessProfile]
    )
    bridge_saved_edit.add_argument(
        "--tunnel", choices=("cloudflare", "tailscale", "custom")
    )
    bridge_saved_edit.add_argument("--public-url")
    bridge_saved_edit.add_argument("--clear-public-url", action="store_true")
    bridge_saved_edit.add_argument("--language", choices=("en", "ru"))
    bridge_saved_edit.add_argument("--port", type=int)
    bridge_saved_edit.add_argument(
        "--deadline-preset", choices=("standard", "long", "full-suite")
    )
    bridge_saved_edit.add_argument("--deadline-seconds", type=float)
    bridge_saved_edit.add_argument("--tunnel-timeout-seconds", type=float)
    bridge_saved_edit.add_argument("--verification-command", action="append")
    bridge_saved_edit.add_argument(
        "--server-profile",
        action="append",
        help=(
            "user-approved dev-server profile as a JSON object; pass once to "
            "replace the list"
        ),
    )
    bridge_saved_edit.add_argument(
        "--clear-server-profiles", action="store_true"
    )
    bridge_saved_edit.add_argument(
        "--mcp-server",
        action="append",
        help="external MCP server ID to proxy; pass once to replace the list",
    )
    bridge_saved_edit.add_argument(
        "--clear-mcp-servers", action="store_true"
    )
    bridge_saved_edit.add_argument(
        "--clear-verification-commands", action="store_true"
    )
    bridge_saved_edit.add_argument(
        "--reset-identity",
        action="store_true",
        help=(
            "revoke the durable session/keyring identity before changing the "
            "repository or access profile; the external connector needs the new secret"
        ),
    )
    bridge_saved_edit.add_argument("--json", action="store_true")
    # Browser policy flags: tri-state so an edit can both enable and disable.
    # Neither passed = preserve the current value.
    for flag, help_en in [
        ("browser-external-https", "allow external HTTPS browser access"),
        ("browser-headed", "run browser with a visible window"),
        ("browser-user-takeover", "allow user takeover for login/CAPTCHA/2FA"),
        ("browser-network-inspection", "enable network request inspection"),
        ("browser-payment-confirmation", "require confirmation for payment pages"),
    ]:
        group = bridge_saved_edit.add_mutually_exclusive_group()
        group.add_argument(
            f"--{flag}", dest=flag.replace("-", "_"), action="store_true", default=None,
            help=help_en,
        )
        group.add_argument(
            f"--no-{flag}", dest=flag.replace("-", "_"), action="store_false",
            help=f"disable: {help_en}",
        )
    bridge_saved_edit.add_argument(
        "--browser-domain", action="append",
        help="allowed browser domain (pass once to replace the list)",
    )
    bridge_saved_edit.add_argument(
        "--browser-deny-domain", action="append",
        help="denied browser domain (pass once to replace the list)",
    )
    bridge_saved_edit.add_argument(
        "--browser-allowed-email", action="append",
        help="allowed browser email (pass once to replace the list)",
    )
    bridge_saved_edit.add_argument(
        "--clear-browser-domains", action="store_true",
        help="clear all browser domain allow/deny lists",
    )

    bridge_connect = bridge_commands.add_parser(
        "connect",
        help="launch a complete ChatGPT or Claude web bridge",
    )
    _add_browser_policy_arguments(bridge_connect)
    bridge_connect.add_argument(
        "profile",
        nargs="?",
        choices=WEB_BRIDGE_PROFILES,
    )
    bridge_connect.add_argument(
        "--saved", help="launch a reusable profile created by `karox bridge saved`"
    )
    bridge_connect.add_argument("--repository", type=Path)
    bridge_connect.add_argument("--session-id")
    bridge_connect.add_argument(
        "--access-profile",
        choices=[item.value for item in AccessProfile],
        help="defaults to read_only, or workspace_write with --write",
    )
    bridge_connect.add_argument(
        "--tunnel",
        choices=("cloudflare", "tailscale", "custom"),
    )
    bridge_connect.add_argument(
        "--public-url",
        help="public HTTPS origin when --tunnel custom is selected",
    )
    bridge_connect.add_argument(
        "--cloudflared",
        help="explicit cloudflared executable path",
    )
    bridge_connect.add_argument(
        "--tailscale", help="explicit tailscale executable path"
    )
    bridge_connect.add_argument("--port", type=int)
    bridge_connect.add_argument(
        "--tool",
        action="append",
        choices=tuple(sorted(KNOWN_HOSTED_TOOL_NAMES)),
        help="replace the safe default tool set (repeatable)",
    )
    bridge_connect.add_argument(
        "--write",
        action="store_true",
        help="add repository edit and write tools",
    )
    bridge_connect.add_argument(
        "--verification-command",
        action="append",
        help=(
            "user-approved checks.run command as a JSON array; required when "
            "karox.checks.run is exposed"
        ),
    )
    bridge_connect.add_argument(
        "--server-profile",
        action="append",
        help=(
            "user-approved dev-server profile as a JSON object "
            "({name, argv, env_keys, env_allowlist, host_hint}); required when "
            "karox.dev_server.start is exposed (repeatable)"
        ),
    )
    bridge_connect.add_argument(
        "--deadline-preset",
        choices=("standard", "long", "full-suite"),
    )
    bridge_connect.add_argument(
        "--deadline-seconds",
        type=float,
        help="effective ceiling on one tool call, including checks.run",
    )
    bridge_connect.add_argument("--tunnel-timeout-seconds", type=float)
    bridge_connect.add_argument("--language", choices=("en", "ru"))
    bridge_connect.add_argument(
        "--diagnostics-only",
        action="store_true",
        help="print effective machine-readable diagnostics without launching",
    )
    bridge_status = bridge_commands.add_parser(
        "status", help="report port and process ownership for a saved bridge"
    )
    bridge_status.add_argument("--saved", required=True, help="saved profile name")
    bridge_status.add_argument("--json", action="store_true")
    bridge_stop = bridge_commands.add_parser(
        "stop", help="stop a saved bridge that this KaroX owns"
    )
    bridge_stop.add_argument("--saved", required=True, help="saved profile name")
    bridge_stop.add_argument("--json", action="store_true")
    bridge_restart = bridge_commands.add_parser(
        "restart", help="controlled restart of a saved bridge this KaroX owns"
    )
    bridge_restart.add_argument("--saved", required=True, help="saved profile name")
    bridge_restart.add_argument("--json", action="store_true")
    bridge_attach = bridge_commands.add_parser(
        "attach", help="attach to a live saved bridge this KaroX owns"
    )
    bridge_attach.add_argument("--saved", required=True, help="saved profile name")
    bridge_attach.add_argument("--json", action="store_true")
    bridge_oauth = bridge_commands.add_parser(
        "oauth", help="manage OAuth approval password for a saved bridge"
    )
    bridge_oauth_commands = bridge_oauth.add_subparsers(
        dest="bridge_oauth_command", required=True
    )
    bridge_oauth_approval = bridge_oauth_commands.add_parser(
        "approval-password",
        help="copy the OAuth approval password without printing it",
    )
    bridge_oauth_approval.add_argument("--saved", required=True, help="saved profile name")
    bridge_oauth_approval.add_argument(
        "--copy", action="store_true", help="copy to clipboard with 120s auto-clear"
    )
    bridge_oauth_approval.add_argument("--quiet", action="store_true")
    bridge_oauth_approval.add_argument("--json", action="store_true")
    bridge_serve = bridge_commands.add_parser(
        "serve", help="serve selected Core/MCP tools over authenticated HTTP"
    )
    _add_browser_policy_arguments(bridge_serve)
    bridge_serve.add_argument("--repository", type=Path, required=True)
    bridge_serve.add_argument("--session-id", required=True)
    bridge_serve.add_argument(
        "--saved-profile-name",
        help=argparse.SUPPRESS,
    )
    bridge_serve.add_argument(
        "--project",
        action="append",
        default=[],
        help=argparse.SUPPRESS,
    )
    bridge_serve.add_argument(
        "--default-project-id",
        help=argparse.SUPPRESS,
    )
    bridge_serve.add_argument(
        "--profile",
        choices=tuple(item.name for item in BridgeRegistry().list()),
        default="generic-streamable-http",
    )
    bridge_serve.add_argument(
        "--protocol",
        choices=("mcp", "openapi"),
        help="defaults to openapi for PromptQL and mcp for other profiles",
    )
    bridge_serve.add_argument(
        "--tool",
        action="append",
        default=[],
        choices=tuple(sorted(KNOWN_HOSTED_TOOL_NAMES)),
        help="built-in KaroX Core tool to expose (repeatable)",
    )
    bridge_serve.add_argument(
        "--server",
        action="append",
        default=[],
        help="selected external MCP server to proxy (repeatable)",
    )
    bridge_serve.add_argument(
        "--verification-command",
        action="append",
        default=[],
        help=(
            "user-approved checks.run command as a JSON array; required when "
            "karox.checks.run is exposed"
        ),
    )
    bridge_serve.add_argument(
        "--server-profile",
        action="append",
        default=[],
        help=(
            "user-approved dev-server profile as a JSON object "
            "({name, argv, env_keys, env_allowlist, host_hint}); required when "
            "karox.dev_server.start is exposed (repeatable)"
        ),
    )
    bridge_serve.add_argument("--credential", required=True)
    bridge_serve.add_argument(
        "--public-url",
        help=(
            "stable public HTTPS origin for OAuth web profiles, for example "
            "https://karox.example.com"
        ),
    )
    bridge_serve.add_argument(
        "--allowed-redirect-hosts",
        help=(
            "comma-separated exact client hosts a strict OAuth profile may "
            "redirect authorization codes to (hyperagent-web pins "
            "hyperagent.com); leave empty for the permissive default"
        ),
    )
    bridge_serve.add_argument("--host", default="127.0.0.1")
    bridge_serve.add_argument("--port", type=int, default=8765)
    bridge_serve.add_argument(
        "--deadline-seconds",
        type=float,
        default=DEFAULT_HOSTED_DEADLINE_SECONDS,
        help=(
            "ceiling on one tool call, including checks.run; thirty seconds is "
            "shorter than the test suite of any real repository"
        ),
    )
    bridge_serve.add_argument("--allow-network-bind", action="store_true")
    bridge_serve.add_argument(
        "--tls-certfile",
        help=(
            "PEM certificate chain; required with --tls-keyfile for a "
            "non-loopback bind"
        ),
    )
    bridge_serve.add_argument(
        "--tls-keyfile",
        help="PEM private key for --tls-certfile",
    )
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
    bridge_credential_copy = bridge_credentials.add_parser(
        "copy",
        help="copy the current Bearer credential without printing the secret",
        description="Copy the current Bearer credential to the clipboard without printing the secret.",
    )
    bridge_credential_copy.add_argument("name")
    bridge_credential_copy.add_argument("--json", action="store_true")
    bridge_credential_copy.add_argument(
        "--quiet",
        action="store_true",
        help="suppress clipboard-unavailable notes; the secret is never printed",
    )
    bridge_credential_rotate = bridge_credentials.add_parser(
        "rotate-key", help="replace a bridge credential without printing the secret"
    )
    bridge_credential_rotate.add_argument("name")
    bridge_credential_rotate.add_argument("--json", action="store_true")
    bridge_credential_rotate.add_argument(
        "--copy",
        action="store_true",
        help="copy 'Bearer <secret>' to the clipboard and auto-clear after 120s; the secret is never printed",
    )
    bridge_credential_rotate.add_argument(
        "--quiet",
        action="store_true",
        help="suppress informational notes; still emits the JSON result",
    )
    bridge_credential_rotate.add_argument(
        "--reveal-secret",
        dest="reveal_secret",
        action="store_true",
        help="UNSAFE: include the new secret in output; requires explicit --yes",
    )
    bridge_credential_rotate.add_argument(
        "--yes",
        action="store_true",
        help="confirm an unsafe --reveal-secret",
    )
    bridge_credential_revoke = bridge_credentials.add_parser(
        "revoke", help="delete a bridge credential"
    )
    bridge_credential_revoke.add_argument("name")
    bridge_credential_revoke.add_argument("--json", action="store_true")

    # `karox connect` is the one-command path: it launches a complete ChatGPT,
    # Claude, or HyperAgent web bridge with Tailscale by default and brings
    # Tailscale online (restarting its service if needed) before publishing.
    # `bridge connect` stays the fully-specified form; this one just chooses the
    # sensible defaults so a user types one word, not eight flags.
    connect = commands.add_parser(
        "connect",
        help=(
            "one-command bridge launch (ChatGPT/Claude/Notion/HyperAgent + Tailscale, "
            "or ClickUp + Cloudflare)"
        ),
    )
    _add_browser_policy_arguments(connect)
    connect.add_argument(
        "connector",
        nargs="?",
        choices=("chatgpt", "claude", "notion", "clickup", "hyperagent"),
        default="chatgpt",
        help="target connector (defaults to chatgpt)",
    )
    connect.add_argument("--repository", type=Path)
    connect.add_argument(
        "--name",
        default="ClickUp",
        help="saved connection name for the ClickUp launcher",
    )
    connect.add_argument("--session-id")
    connect.add_argument(
        "--access-profile",
        choices=[item.value for item in AccessProfile],
        help="defaults to read_only, or workspace_write with --write",
    )
    connect.add_argument(
        "--tunnel",
        choices=("cloudflare", "tailscale", "custom"),
        default=None,
        help=(
            "defaults to tailscale for web OAuth clients and cloudflare for ClickUp"
        ),
    )
    connect.add_argument(
        "--public-url",
        help="public HTTPS origin when --tunnel custom is selected",
    )
    connect.add_argument(
        "--cloudflared", help="explicit cloudflared executable path"
    )
    connect.add_argument(
        "--tailscale", help="explicit tailscale executable path"
    )
    connect.add_argument("--port", type=int)
    connect.add_argument(
        "--tool",
        action="append",
        choices=tuple(sorted(KNOWN_HOSTED_TOOL_NAMES)),
        help="replace the safe default tool set (repeatable)",
    )
    connect.add_argument(
        "--write",
        action="store_true",
        help="add repository edit and write tools",
    )
    connect.add_argument(
        "--verification-command",
        action="append",
        help=(
            "user-approved checks.run command as a JSON array; required when "
            "karox.checks.run is exposed"
        ),
    )
    connect.add_argument(
        "--deadline-preset",
        choices=("standard", "long", "full-suite"),
    )
    connect.add_argument(
        "--deadline-seconds",
        type=float,
        help="effective ceiling on one tool call, including checks.run",
    )
    connect.add_argument("--tunnel-timeout-seconds", type=float)
    connect.add_argument("--language", choices=("en", "ru"))
    connect.add_argument(
        "--diagnostics-only",
        action="store_true",
        help="print effective machine-readable diagnostics without launching",
    )

    pack = commands.add_parser("pack", help="manage installable KaroX Packs")
    pack_commands = pack.add_subparsers(dest="pack_command", required=True)
    pack_create = pack_commands.add_parser(
        "create", help="generate a sample pack template"
    )
    pack_create.add_argument("target", type=Path)
    pack_create.add_argument("--name", required=True)
    pack_create.add_argument("--description", required=True)
    pack_create.add_argument("--json", action="store_true")
    pack_install = pack_commands.add_parser(
        "install", help="install a pack from a directory"
    )
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
    run.add_argument("--skill-dir", type=Path, action="append", default=[])
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
        "--route-strategy",
        choices=("ordered", "cheapest"),
        default="ordered",
        help=(
            "ordered keeps the declared route order; cheapest reorders only when "
            "all routes have comparable explicit pricing and output ceilings"
        ),
    )
    run.add_argument("--privacy-limit", choices=sorted(PRIVACY_CLASSES), default=None)
    run.add_argument("--max-total-tokens", type=int)
    run.add_argument("--max-cost", type=float)
    run.add_argument("--currency")
    run.add_argument("--max-steps", type=int, default=24)
    run.add_argument("--max-seconds", type=float, default=900.0)
    run.add_argument(
        "--context-window",
        type=int,
        default=None,
        help=(
            "model input window in tokens; routed models take this from the "
            "registry, so it is only needed for --base-url endpoints"
        ),
    )
    run.add_argument(
        "--max-output-tokens",
        type=int,
        default=None,
        help=(
            "cap on each answer in tokens; routed models take this from the "
            "registry, so it is only needed for --base-url endpoints"
        ),
    )
    run.add_argument(
        "--context-utilization",
        type=float,
        default=None,
        help=(
            "fraction of the model input window KaroX may fill before compacting; "
            "lower values trade more re-reads for lower repeated prompt cost"
        ),
    )
    run.add_argument(
        "--max-tool-result-chars",
        type=int,
        default=None,
        help=(
            "maximum characters from one tool result resent on later model turns; "
            "use narrower read/search calls to recover omitted detail"
        ),
    )
    run.add_argument(
        "--verification-command",
        action="append",
        required=True,
        help="user-approved verification command as a JSON array (repeatable)",
    )
    run.add_argument(
        "--expect",
        choices=("auto", "change"),
        default="auto",
        help=(
            "auto: a task that changed nothing succeeds as an evidence-backed "
            "answer; change: refuse to succeed unless a file actually changed"
        ),
    )
    run.add_argument(
        "--effort",
        choices=REASONING_EFFORTS,
        default=None,
        help=(
            "how hard the model should think before answering; sent to whichever "
            "provider serves the run, which may cap it at its own top level"
        ),
    )
    run.add_argument("--economy", action="store_true", help=argparse.SUPPRESS)
    run.add_argument(
        "--recursive-context",
        choices=("auto", "off", "on", "research"),
        default="off",
        help=argparse.SUPPRESS,
    )
    run.add_argument(
        "--no-project-context",
        action="store_true",
        help=(
            "skip CLAUDE.md, AGENTS.md and KAROX.md and the environment stanza; "
            "useful when reproducing a run that must not depend on them"
        ),
    )
    run.add_argument(
        "--stream",
        action="store_true",
        help=(
            "report progress on stderr while the run is still going: each step, "
            "each tool with how long it took, and the answer as it arrives; "
            "--json stays one final document on stdout"
        ),
    )
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
    doctor = commands.add_parser("doctor", help="run aggregate KaroX diagnostics")
    doctor.add_argument("--json", action="store_true")
    return parser


def _print_mapping(value: dict[str, Any]) -> None:
    for key, item in value.items():
        _write_line(f"{key}: {item}")


def _stream_progress() -> Callable[[AgentEvent], None]:
    """Narrate a run on stderr while it is still happening.

    Progress goes to stderr on purpose: ``--json`` promises one document on
    stdout, and a run that only speaks at the end is indistinguishable from a
    hung one.
    """

    text_open = [False]
    # Progress is exactly what gets redirected to a file, and on Windows that
    # stream defaults to the system code page: a model answering in anything but
    # ASCII would otherwise take the watcher down with UnicodeEncodeError.
    with contextlib.suppress(Exception):
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]

    def emit(text: str) -> None:
        try:
            sys.stderr.write(text)
        except UnicodeEncodeError:
            encoding = getattr(sys.stderr, "encoding", None) or "ascii"
            sys.stderr.write(
                text.encode(encoding, "replace").decode(encoding, "replace")
            )
        sys.stderr.flush()

    def write(line: str) -> None:
        if text_open[0]:
            emit("\n")
            text_open[0] = False
        emit(line + "\n")

    def observe(event: AgentEvent) -> None:
        if event.kind is AgentEventKind.TEXT_DELTA:
            # Deltas are fragments, not lines: printed as they arrive they read
            # as the model typing, which is the whole point of streaming.
            emit(event.text_delta or "")
            text_open[0] = True
        elif event.kind is AgentEventKind.STEP_STARTED:
            write(f"[step {event.step}]")
        elif event.kind is AgentEventKind.COMPACTED:
            write(f"[context: {event.summary}]")
        elif event.kind is AgentEventKind.TOOL_STARTED:
            write(f"  -> {event.tool}")
        elif event.kind is AgentEventKind.TOOL_FINISHED:
            mark = "ok" if event.ok else "failed"
            write(
                f"  <- {event.tool} {mark} in {event.duration_seconds:.2f}s"
                f" ({event.summary})"
            )
        elif event.kind is AgentEventKind.FINISHED:
            write(f"[{event.status}: {event.reason}]")

    return observe


def _print_agent_report(report: AgentReport) -> None:
    print(f"session_id: {report.session_id}")
    print(f"status: {report.status}")
    print(f"verified: {str(report.verified).lower()}")
    print(f"reason: {report.reason}")
    print(f"steps: {report.steps}")
    if report.changed_files:
        print("changed_files: " + ", ".join(report.changed_files))
    print(f"evidence_records: {len(report.evidence)}")
    # An answer carries no Core evidence records of its own, so the inspections
    # it rests on are named instead of leaving "verified" standing on nothing.
    if report.answer_basis:
        print(
            "answer_basis: "
            + ", ".join(
                str(item.get("tool")) + (f" {item['path']}" if item.get("path") else "")
                for item in report.answer_basis
            )
        )
    # Instructions written by whoever can commit to the repository shaped this
    # run, so they are named rather than applied silently.
    sources = report.project_context.get("sources") or []
    if sources:
        print(
            "project_instructions: "
            + ", ".join(
                str(item.get("path"))
                + (" (truncated)" if item.get("truncated") else "")
                for item in sources
            )
        )
    for skipped in report.project_context.get("skipped") or []:
        print(f"project_instructions_skipped: {skipped}")
    # A rewritten context changes what the model could see, so it is reported
    # rather than left to be inferred from a worse answer.
    if report.compaction:
        detail = report.compaction
        print(
            "context_compaction: {count}x, ~{before} -> ~{after} tokens, "
            "{turns} turn(s) summarized{note}".format(
                count=detail.get("count"),
                before=detail.get("before_tokens"),
                after=detail.get("after_tokens"),
                turns=detail.get("turns_summarized"),
                note="" if detail.get("within_ceiling") else " (still over the ceiling)",
            )
        )
    if report.provider_message:
        print(f"provider_message: {report.provider_message}")


def _registry() -> ProviderRegistry:
    return ProviderRegistry(config_dir() / "vnext" / "providers.json")


def _provider_controller() -> ProviderController:
    return ProviderController(registry=_registry(), credentials=CredentialStore())


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
        # Omitted, these derive from the input rate using the multipliers the
        # providers publish. They exist for a gateway that prices cache
        # differently, so its cost figures are its own rather than an assumption.
        cache_read_per_million=getattr(args, "cache_read_per_million", None),
        cache_write_per_million=getattr(args, "cache_write_per_million", None),
    )


def _skill_decisions(values: Sequence[str]) -> dict[Capability, SkillPermission]:
    pairs = _pairs(values, "Skill permission")
    result: dict[Capability, SkillPermission] = {}
    for raw_capability, raw_decision in pairs.items():
        try:
            capability = Capability(raw_capability)
        except ValueError as exc:
            raise ValueError(f"unknown Skill capability: {raw_capability}") from exc
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


def _stored_skill(record: SessionRecord, name: str) -> Optional[dict[str, Any]]:
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


def _stored_mcp(record: SessionRecord, server_id: str) -> Optional[dict[str, Any]]:
    matches: list[dict[str, Any]] = []
    for item in record.mcp_servers:
        if not isinstance(item, dict):
            raise McpAccessDenied("stored MCP selection must be an object")
        if item.get("server_id") == server_id:
            matches.append(item)
    if len(matches) > 1:
        raise McpAccessDenied(f"session contains duplicate MCP selections: {server_id}")
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
                _write_line(identifier or json.dumps(item, ensure_ascii=False))
            else:
                _write_line(str(item))
    else:
        _write_line(str(value))


def _test_registered_model(
    registry: ProviderRegistry, provider_id: str, model_or_alias: str
) -> dict[str, Any]:
    provider_record = registry.provider(provider_id)
    model_record = registry.model(provider_id, model_or_alias)
    response = (
        ProviderFactory()
        .create(provider_record)
        .complete(
            ModelRequest(
                model=model_record.model_id,
                messages=(ModelMessage("user", "Reply with exactly OK."),),
                max_output_tokens=8,
                deadline_seconds=min(60.0, provider_record.timeout_seconds),
            )
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


def _agent_access_profile(args: argparse.Namespace) -> AccessProfile:
    """The session profile for a native agent run, honouring provider Bypass.

    Bypass is a KaroX-local policy recorded on the provider the run is routed
    to; it never touches provider HTTP auth, the API key, the base URL, the
    model id, or any SDK option.  Direct ``--base-url`` mode persists no
    provider record and so has nowhere to carry the preference: it keeps the
    normal protected profile.  Routed mode elevates only when every provider
    that could serve the session has Bypass ON (see
    ``access_mode.session_access_profile``).

    The TUI runs the agent through this same CLI entry point, so CLI, TUI, and
    a resumed session all read one contract rather than three.
    """

    from .access_mode import session_access_profile

    if args.model is not None or args.base_url is not None or args.api_key_env:
        return AccessProfile.WORKSPACE_WRITE
    registry = _registry()
    routes = tuple(_route(value) for value in args.route)
    if not routes:
        selected = registry.selected_model()
        if selected is None:
            return AccessProfile.WORKSPACE_WRITE
        routes = (RouteTarget(selected.provider_id, selected.model_id),)
    records = []
    for target in routes:
        try:
            records.append(registry.provider(target.provider_id))
        except Exception:
            # An unresolvable route is reported by _agent_provider with a far
            # better message; refusing to elevate here is the safe reading.
            return AccessProfile.WORKSPACE_WRITE
    profile = session_access_profile(records)
    assert isinstance(profile, AccessProfile)
    return profile


def _agent_provider(
    args: argparse.Namespace, record: SessionRecord | None, limits: AgentLimits
) -> tuple[Any, str, int | None, int | None]:
    """Resolve the provider, the model, and the model's two token ceilings.

    The input window drives history compaction and the output ceiling caps each
    answer, so both are read from the registry entry that is actually routed to
    rather than guessed. A direct ``--base-url`` endpoint publishes neither, so
    it relies on ``--context-window`` and ``--max-output-tokens``.
    """
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
            environment_name = args.api_key_env

            def credential() -> str:
                return os.environ.get(environment_name, "")

        return (
            OpenAIChatCompletionsProvider(
                args.base_url,
                credential=credential,
                timeout_seconds=min(60.0, float(limits.max_seconds)),
            ),
            args.model,
            args.context_window,
            args.max_output_tokens,
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
    windows: list[int] = []
    ceilings: list[int] = []
    for target in routes:
        registry.provider(target.provider_id)
        entry = registry.model(target.provider_id, target.model)
        window = getattr(entry, "context_window", None)
        if isinstance(window, int) and window > 0:
            windows.append(window)
        ceiling = getattr(entry, "max_output_tokens", None)
        if isinstance(ceiling, int) and ceiling > 0:
            ceilings.append(ceiling)
    # A fallback route may have a smaller window than the primary one. Compacting
    # to the smallest keeps a fallback from failing on a history the first model
    # accepted. An unknown window anywhere means the smallest is unknown.
    context_window = (
        min(windows) if windows and len(windows) == len(routes) else None
    )
    # The output ceiling is chosen the same way and for the same reason: asking
    # for more than a fallback model can produce turns a recoverable failure into
    # a rejected request on the route that was supposed to rescue the run.
    max_output_tokens = (
        min(ceilings) if ceilings and len(ceilings) == len(routes) else None
    )
    initial_usage = record.usage if record is not None else {}
    costs = initial_usage.get("costs")
    initial_costs = costs if isinstance(costs, dict) else {}
    route_strategy = getattr(args, "route_strategy", "ordered")
    provider = RoutedProvider(
        registry,
        ProviderFactory(),
        RoutingPolicy(
            routes=routes,
            privacy_limit=args.privacy_limit or "public",
            route_strategy=route_strategy,
            max_total_tokens=args.max_total_tokens,
            max_cost=args.max_cost,
            currency=args.currency,
        ),
        initial_usage=initial_usage,
        initial_costs=initial_costs,
    )
    return (
        provider,
        routes[0].model,
        args.context_window or context_window,
        args.max_output_tokens or max_output_tokens,
    )


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
        metadata = catalog.load(args.name).metadata
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
            selection = skill_selection(metadata, decisions, previous=previous)
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
        payload: Any = registry.put(record).to_dict()
    elif command == "remove":
        removed = registry.remove(args.server_id)
        payload = {"server_id": removed.server_id, "status": "removed"}
    elif command == "list":
        payload = [item.to_dict() for item in registry.list()]
    elif command == "show":
        payload = registry.get(args.server_id).to_dict()
    elif command == "authorize":
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
        payload = {
            "server_id": record.server_id,
            "status": "authorized",
            "server_name": authorization.server_name,
            "tool_count": len(authorization.tool_names),
            "tools": list(authorization.tool_names),
        }
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
            record.session_id,
            f"mcp-call-{os.getpid()}",
            ttl_seconds=max(30.0, min(3600.0, float(args.deadline_seconds) + 10.0)),
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


def _handle_mcp_status(args: argparse.Namespace) -> int:
    """Join registry, session authorization, and optional probes into one screen.

    Probing is opt-in because it costs a connection per server and can fail;
    without ``--probe`` liveness is reported as ``not_probed`` rather than
    guessed from the fact that a server is configured.
    """
    registry = _mcp_registry()
    servers = registry.list()
    try:
        credentials: Optional[McpCredentialStore] = McpCredentialStore()
    except CredentialError:
        # An unusable keyring is a fact to report, not a reason to fail the screen.
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
    payload = build_mcp_status(
        servers,
        session_id=args.session_id,
        selections=selections,
        resolve_credential=credentials.resolve if credentials is not None else None,
        liveness=liveness,
    )
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


_WEB_BRIDGE_DEADLINE_PRESETS = {
    "standard": DEFAULT_HOSTED_DEADLINE_SECONDS,
    "long": 1800.0,
    "full-suite": 3600.0,
}

# `karox connect` takes the short connector name a user types and maps it to the
# full web-bridge profile that the launcher expects. Keeping the mapping in one
# place means the parser choices and the handler cannot drift.
_CONNECTOR_PROFILES = {
    "chatgpt": "chatgpt-web",
    "claude": "claude-web",
    "notion": "notion",
    "hyperagent": "hyperagent-web",
}


def _web_bridge_deadline(
    explicit: Optional[float],
    preset: Optional[str],
    *,
    fallback: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
) -> float:
    if explicit is not None and preset is not None:
        raise ValueError("choose either --deadline-seconds or --deadline-preset")
    if explicit is not None:
        return float(explicit)
    if preset is not None:
        return _WEB_BRIDGE_DEADLINE_PRESETS[preset]
    return float(fallback)


def _web_bridge_tools(
    selected: Optional[Sequence[str]],
    *,
    write: bool,
    fallback: Sequence[str] = DEFAULT_WEB_TOOLS,
) -> tuple[str, ...]:
    tools = tuple(selected) if selected else tuple(fallback)
    if write:
        tools = tuple(dict.fromkeys((*tools, *WRITE_WEB_TOOLS)))
    return tools


def _upgrade_saved_profile_tools(profile: SavedWebBridgeProfile) -> tuple[str, ...]:
    """Add orchestration tools without widening the profile's low-level grants.

    Saved profiles persist an explicit tool snapshot. Without this compatibility
    upgrade, profiles created before ``task.execute_plan`` existed stay on the
    old chatty surface forever. The executor itself delegates only to tools the
    low-level runtime already exposes, so this adds orchestration rather than a
    new repository capability.
    """
    tools = list(profile.tools)
    names = set(tools)

    # A saved profile records concrete tool names, but these names are only a
    # projection of the capability family the user approved. Older TUI builds
    # wrote just read_file/status, which stranded hosted coding agents without
    # content search, bounded reads, repository inspection, Git history, or the
    # durable task-state reads. Upgrade those siblings without crossing into a
    # stronger capability tier.
    repo_read_surface = {
        "karox.repo.read_file",
        "karox.repo.read_lines",
        "karox.repo.search",
        "karox.repo.inspect",
    }
    if names.intersection(repo_read_surface):
        for name in (
            "karox.repo.read_lines",
            "karox.repo.search",
            "karox.repo.inspect",
            "karox.task.bootstrap",
            "karox.task.status",
            "karox.task.resume",
            "karox.task.workstreams",
        ):
            if name not in names:
                tools.append(name)
                names.add(name)

    if names.intersection({"karox.git.status", "karox.git.diff", "karox.git.log"}):
        if "karox.git.log" not in names:
            tools.append("karox.git.log")
            names.add("karox.git.log")

    if names.intersection({"karox.repo.write_file", "karox.repo.edit_file"}):
        for name in ("karox.repo.write_file", "karox.repo.edit_file"):
            if name not in names:
                tools.append(name)
                names.add(name)

    can_patch = "karox.repo.command" in names
    can_verify = bool({"karox.checks.run", "karox.tests.run"}.intersection(names))
    if can_patch and can_verify and "karox.task.execute_plan" not in names:
        tools.append("karox.task.execute_plan")
    return tuple(tools)


def _web_bridge_access_profile(
    explicit: Optional[str], *, write: bool, external_browser: bool
) -> AccessProfile:
    if explicit:
        return AccessProfile(explicit)
    if write:
        return AccessProfile.WORKSPACE_WRITE
    if external_browser:
        return AccessProfile.BROWSER_CONTROL
    return AccessProfile.READ_ONLY


def _browser_config_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "browser_external_https": bool(getattr(args, "browser_external_https", False)),
        "browser_allowed_domains": tuple(getattr(args, "browser_domain", []) or []),
        "browser_denied_domains": tuple(getattr(args, "browser_deny_domain", []) or []),
        "browser_headed": bool(getattr(args, "browser_headed", False)),
        "browser_user_takeover": bool(getattr(args, "browser_user_takeover", False)),
        "browser_network_inspection": bool(getattr(args, "browser_network_inspection", False)),
        "browser_payment_confirmation": bool(getattr(args, "browser_payment_confirmation", False)),
        "browser_allowed_emails": tuple(getattr(args, "browser_allowed_email", []) or []),
        "browser_credential_refs": tuple(getattr(args, "browser_credential_ref", []) or []),
    }


def _web_bridge_repository(value: Optional[Path], saved: Optional[str] = None) -> Path:
    repository = value or (Path(saved) if saved else Path.cwd())
    resolved = repository.expanduser().resolve(strict=True)
    if not resolved.is_dir():
        raise ValueError("web bridge repository must be a directory")
    return resolved


def _saved_profile_connect_config(
    profile: SavedWebBridgeProfile,
    *,
    repository: Optional[Path] = None,
    session_id: Optional[str] = None,
    tool: Optional[Sequence[str]] = None,
    write: bool = False,
    access_profile: Optional[str] = None,
    tunnel: Optional[str] = None,
    public_url: Optional[str] = None,
    cloudflared: Optional[str] = None,
    tailscale: Optional[str] = None,
    port: Optional[int] = None,
    verification_command: Optional[Sequence[str]] = None,
    server_profile: Optional[Sequence[str]] = None,
    deadline_seconds: Optional[float] = None,
    deadline_preset: Optional[str] = None,
    tunnel_timeout_seconds: Optional[float] = None,
    language: Optional[str] = None,
    browser_external_https: bool = False,
    browser_domain: Sequence[str] = (),
    browser_deny_domain: Sequence[str] = (),
    browser_headed: bool = False,
    browser_user_takeover: bool = False,
    browser_network_inspection: bool = False,
    browser_payment_confirmation: bool = False,
    browser_allowed_email: Sequence[str] = (),
) -> WebBridgeConnectConfig:
    effective_tunnel = tunnel or profile.tunnel
    effective_public_url = public_url
    if effective_public_url is None and effective_tunnel == "custom":
        effective_public_url = profile.public_url
    resolved_repository = _web_bridge_repository(repository, profile.repository)
    tools = _web_bridge_tools(
        tool, write=write, fallback=_upgrade_saved_profile_tools(profile)
    )

    if verification_command is not None:
        commands = tuple(_verification_command(value) for value in verification_command)
    else:
        commands = tuple(profile.verification_commands)
        if "karox.checks.run" in tools or "karox.tests.run" in tools:
            commands = tuple(
                dict.fromkeys((*commands, *discover_verification_commands(resolved_repository)))
            )

    if server_profile is not None:
        profiles = tuple(_server_profile(value) for value in server_profile)
    else:
        # Rebuild persisted profiles first, then migrate the one historical
        # project-specific default. A saved Hyperagent/Notion profile created
        # while KaroX still assumed Vacancy Control must not keep advertising a
        # nonexistent `npm run start:safe` after the repository changes.
        profiles = tuple(
            ManagedServerProfile(
                name=str(item["name"]),
                argv=tuple(str(arg) for arg in item["argv"]),
                env={str(k): "" for k in (item.get("env_keys") or [])},
                env_allowlist=frozenset(str(k) for k in (item.get("env_allowlist") or [])),
                host_hint=str(item.get("host_hint", "127.0.0.1")),
                ready_url=item.get("ready_url"),
            )
            for item in profile.server_profiles
        )
        discovered_profiles = server_profiles_for_repository(resolved_repository)
        legacy_vacancy_only = bool(profiles) and all(
            item.name == "vacancy-control-safe" for item in profiles
        )
        if legacy_vacancy_only or (
            not profiles and "karox.dev_server.start" in tools
        ):
            profiles = discovered_profiles
    if "karox.dev_server.start" in tools and not profiles:
        tools = tuple(
            name
            for name in tools
            if name not in {"karox.dev_server.start", "karox.dev_server.stop"}
        )
    effective_external_browser = profile.browser_external_https or browser_external_https
    effective_access = (
        AccessProfile(access_profile)
        if access_profile
        else (
            AccessProfile.WORKSPACE_WRITE
            if write
            else (
                AccessProfile.BROWSER_CONTROL
                if effective_external_browser and profile.access_profile == AccessProfile.READ_ONLY
                else profile.access_profile
            )
        )
    )
    return WebBridgeConnectConfig(
        profile=profile.target_profile,
        repository=resolved_repository,
        projects=profile.projects if repository is None else (),
        default_project_id=(
            profile.default_project_id if repository is None else None
        ),
        port=port if port is not None else profile.port,
        tools=tools,
        mcp_servers=profile.mcp_servers,
        session_id=session_id,
        access_profile=effective_access,
        tunnel=effective_tunnel,
        public_url=effective_public_url,
        cloudflared=cloudflared,
        tailscale=tailscale,
        tunnel_timeout_seconds=(
            tunnel_timeout_seconds
            if tunnel_timeout_seconds is not None
            else profile.tunnel_timeout_seconds
        ),
        deadline_seconds=_web_bridge_deadline(
            deadline_seconds,
            deadline_preset,
            fallback=profile.deadline_seconds,
        ),
        verification_commands=commands,
        server_profiles=profiles,
        browser_external_https=effective_external_browser,
        browser_allowed_domains=(
            tuple(browser_domain) if browser_domain else profile.browser_allowed_domains
        ),
        browser_denied_domains=(
            tuple(browser_deny_domain)
            if browser_deny_domain
            else profile.browser_denied_domains
        ),
        browser_headed=profile.browser_headed or browser_headed,
        browser_user_takeover=profile.browser_user_takeover or browser_user_takeover,
        browser_network_inspection=(
            profile.browser_network_inspection or browser_network_inspection
        ),
        browser_payment_confirmation=(
            profile.browser_payment_confirmation or browser_payment_confirmation
        ),
        browser_allowed_emails=(
            tuple(browser_allowed_email)
            if browser_allowed_email
            else profile.browser_allowed_emails
        ),
        browser_credential_refs=profile.browser_credential_refs,
        language=language or profile.language,
        saved_profile_name=profile.name,
    )


def _direct_connect_config(args: argparse.Namespace) -> WebBridgeConnectConfig:
    if args.profile is None:
        raise ValueError("bridge connect requires PROFILE or --saved NAME")
    repository = _web_bridge_repository(args.repository)
    tools = _web_bridge_tools(args.tool, write=args.write)
    commands = (
        tuple(_verification_command(value) for value in args.verification_command)
        if args.verification_command
        else (
            discover_verification_commands(repository)
            if "karox.checks.run" in tools
            else ()
        )
    )
    profiles = tuple(
        _server_profile(value) for value in (args.server_profile or [])
    )
    # Auto-discovery is repository-aware: never advertise Vacancy Control's
    # start:safe recipe to an unrelated Vite/React project.
    if "karox.dev_server.start" in tools and not profiles:
        profiles = server_profiles_for_repository(repository)
    if "karox.dev_server.start" in tools and not profiles:
        tools = tuple(
            name
            for name in tools
            if name not in {"karox.dev_server.start", "karox.dev_server.stop"}
        )
    browser_kwargs = _browser_config_kwargs(args)
    access = _web_bridge_access_profile(
        args.access_profile,
        write=args.write,
        external_browser=browser_kwargs["browser_external_https"],
    )
    return WebBridgeConnectConfig(
        profile=args.profile,
        repository=repository,
        port=args.port if args.port is not None else 8765,
        tools=tools,
        session_id=args.session_id,
        access_profile=access,
        tunnel=args.tunnel or "cloudflare",
        public_url=args.public_url,
        cloudflared=args.cloudflared,
        tailscale=args.tailscale,
        tunnel_timeout_seconds=(
            args.tunnel_timeout_seconds
            if args.tunnel_timeout_seconds is not None
            else 30.0
        ),
        deadline_seconds=_web_bridge_deadline(
            args.deadline_seconds, args.deadline_preset
        ),
        verification_commands=commands,
        server_profiles=profiles,
        **browser_kwargs,
        language=(
            args.language
            or (
                "ru"
                if os.environ.get("KAROX_UI_LANGUAGE", "en").lower().startswith("ru")
                else "en"
            )
        ),
    )


def _connection_summary(
    target: Any,
    *,
    secret: str,
    reveal: bool,
    runtime: Optional[dict[str, Any]] = None,
    endpoint: Optional[str] = None,
) -> dict[str, Any]:
    """Describe a connection the way an external client has to be configured.

    The fields are the ones a person retypes into another application's form:
    the endpoint URL, the auth scheme, the exact header, and the secret.  The
    secret is masked unless ``reveal`` is set, so the default output of every
    command here is safe to paste into a bug report.
    """
    from .connections import auth_headers, connection_test_endpoint, mask_secret

    if endpoint is None:
        endpoint = connection_test_endpoint(target)
    headers = auth_headers(target, secret) if secret else {}
    header_name = next(iter(headers), "")
    payload: dict[str, Any] = {
        "connection_id": target.connection_id,
        "name": target.name,
        "preset_id": target.preset_id,
        "transport": target.transport,
        "auth_scheme": target.auth_scheme,
        "tunnel": target.tunnel,
        "url_stability": target.url_stability,
        "url": endpoint or "",
        "endpoint_path": target.endpoint_path,
        "port": target.port,
        "credential_ref": target.credential_ref or "",
        "credential_fingerprint": target.credential_fingerprint or "",
        "header_name": header_name,
        "secret": (secret if reveal else mask_secret(secret)),
        "secret_revealed": bool(secret) and reveal,
        "runtime": dict(runtime or {}),
    }
    if header_name:
        value = headers[header_name]
        # The header line is what actually gets pasted, so it carries the same
        # masking decision as the secret rather than leaking the value through
        # the one field a reader would not think to check.
        payload["header"] = (
            f"{header_name}: {value}"
            if reveal
            else f"{header_name}: {value[: len(value) - len(secret)]}{mask_secret(secret)}"
        )
    return payload


def _handle_connections(args: argparse.Namespace) -> int:
    command = args.connections_command
    controller = connection_controller()

    if command == "list":
        rows = [
            _connection_summary(
                state.target,
                secret="",
                reveal=False,
                runtime=dict(state.runtime),
                endpoint=state.endpoint,
            )
            for state in controller.list()
        ]
        if args.json:
            _json(rows)
        else:
            for row in rows:
                _write_line(
                    f"{row['connection_id']}\t{row['name']}\t"
                    f"{row['auth_scheme']}\t{row['tunnel']}\t"
                    f"{row['url'] or '-'}"
                )
        return 0

    state = controller.get(args.connection_id)
    target = state.target

    if command == "launch-support":
        support = controller.launch_support(args.connection_id)
        _emit(support.to_dict(), json_output=args.json)
        return 0 if support.supported else 1

    if command == "start":
        _emit(controller.start(args.connection_id).to_dict(), json_output=args.json)
        return 0

    if command == "restart":
        _emit(controller.restart(args.connection_id).to_dict(), json_output=args.json)
        return 0

    if command == "status":
        _emit(dict(state.runtime), json_output=args.json)
        return 0

    if command == "stop":
        _emit(controller.stop(args.connection_id), json_output=args.json)
        return 0

    if command == "remove":
        _emit(controller.remove(args.connection_id), json_output=args.json)
        return 0

    secret = ""
    secret_error = ""
    if target.credential_ref:
        try:
            secret = controller.secret(args.connection_id)
        except CredentialError as exc:
            secret_error = str(exc)

    if command == "show":
        payload = _connection_summary(
            target,
            secret=secret,
            reveal=args.reveal_secret,
            runtime=dict(state.runtime),
            endpoint=state.endpoint,
        )
        if secret_error:
            payload["secret_error"] = secret_error
        if target.instructions:
            payload["instructions"] = target.instructions
        _emit(payload, json_output=args.json)
        return 0 if not secret_error else 1

    if secret_error:
        raise CredentialError(secret_error)
    if command == "copy-auth":
        if not secret:
            raise CredentialError("connection has no stored authorization credential")
        copied = clipboard.write_text(clipboard.bearer_value(secret))
        if copied:
            clipboard.schedule_clear()
        payload = {
            "status": "copied" if copied else "clipboard_unavailable",
            "connection_id": target.connection_id,
            "credential_fingerprint": (
                target.credential_fingerprint or CredentialStore.fingerprint(secret)
            ),
            "auto_clear_seconds": (
                clipboard.CLIPBOARD_AUTO_CLEAR_SECONDS if copied else 0
            ),
        }
        _emit(payload, json_output=args.json)
        return 0 if copied else 1
    result = controller.test(
        args.connection_id,
        endpoint_url=args.url,
        timeout_seconds=args.timeout_seconds,
    )
    _emit(result, json_output=args.json)
    return 0 if result.get("state") == "ok" else 1


def _handle_bridge_lifecycle(args: argparse.Namespace) -> int:
    """status / stop / restart / attach --saved NAME.

    All four are ownership-gated: only a proven KaroX-owned bridge may be
    stopped or restarted, and the verdict is reported before any action so a
    run never terminates a process it cannot prove it started.
    """
    from .port_ownership import check_port_ownership

    profile_name = args.saved
    # The port is part of the saved profile, not the command line. Read it so
    # the ownership check probes the port this profile actually uses.
    saved_profile = WebBridgeProfileStore().get(profile_name)
    port = saved_profile.port
    verdict = check_port_ownership(profile_name, port=port)

    command = args.bridge_command
    if command == "status":
        _emit(verdict.to_dict(), json_output=args.json)
        return 0
    if command == "attach":
        # Attach is the read-only "reuse" signal: it reports the live bridge's
        # metadata so a caller can point a client at it. It never starts or
        # stops anything.
        if verdict.verdict in {"reuse_same_profile"}:
            _emit(verdict.to_dict(), json_output=args.json)
            return 0
        _emit(verdict.to_dict(), json_output=args.json)
        return 1
    if command == "stop":
        # Only a proven, owned bridge may be stopped. An unrelated process or a
        # stale PID is left alone: the verdict explains why.
        if verdict.verdict != "reuse_same_profile":
            _emit(verdict.to_dict(), json_output=args.json)
            return 1 if verdict.verdict != "free" else 0
        # Controlled stop of the proven owner. The watchdog record's owner_pid
        # is the KaroX process that launched the bridge; terminating it lets
        # its finally-block clean up the tunnel and the watchdog file.
        metadata = verdict.metadata
        pid = metadata.pid
        if pid is None or not metadata.pid_proven:
            _emit(verdict.to_dict(), json_output=args.json)
            return 1
        import os
        import signal

        if os.name == "nt":
            # Windows has no SIGTERM equivalent for arbitrary PIDs; use the
            # same taskkill the launcher's own shutdown would, targeting only
            # this proven PID.
            import subprocess

            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
            )
        else:
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError as exc:
                result = {"state": "failed", "reason": str(exc), **verdict.to_dict()}
                _emit(result, json_output=args.json)
                return 1
        result = {
            "state": "ok",
            "action": "stopped",
            "profile": profile_name,
            "pid": pid,
        }
        _emit(result, json_output=args.json)
        return 0
    if command == "restart":
        # A controlled restart stops the proven owned bridge, then relaunches
        # the saved profile. It does not rotate the credential (Phase 0 rule).
        if verdict.verdict == "reuse_same_profile":
            stop_args = argparse.Namespace(**vars(args))
            stop_args.bridge_command = "stop"
            stop_code = _handle_bridge_lifecycle(stop_args)
            if stop_code != 0:
                return stop_code
        elif verdict.verdict not in {"free", "stale_owned_process"}:
            _emit(verdict.to_dict(), json_output=args.json)
            return 1
        # A dead owner can leave this profile's own bridge child holding the
        # port. It is provably ours, so the port is reclaimed before relaunching;
        # a holder that cannot prove it is ours is never touched.
        orphan_pid = getattr(verdict, "owned_orphan_pid", None)
        if isinstance(orphan_pid, int) and orphan_pid > 0:
            from .web_bridge_launcher import _reclaim_orphaned_bridge_listener

            orphan_session = verdict.metadata.session_id or saved_web_bridge_session_id(
                profile_name
            )
            if not _reclaim_orphaned_bridge_listener(
                orphan_pid, port=port, session_id=orphan_session
            ):
                failure = {
                    "state": "failed",
                    "reason": (
                        f"could not reclaim port {port} from this profile's "
                        "orphaned bridge process"
                    ),
                    **verdict.to_dict(),
                }
                _emit(failure, json_output=args.json)
                return 1
        # Relaunch: delegate to the existing connect --saved path.
        connect_args = argparse.Namespace(**vars(args))
        connect_args.bridge_command = "connect"
        connect_args.profile = None
        connect_args.diagnostics_only = False
        connect_args.repository = None
        connect_args.session_id = None
        connect_args.tool = None
        connect_args.write = False
        connect_args.access_profile = None
        connect_args.tunnel = None
        connect_args.public_url = None
        connect_args.cloudflared = None
        connect_args.tailscale = None
        connect_args.port = None
        connect_args.verification_command = None
        connect_args.server_profile = None
        connect_args.deadline_seconds = None
        connect_args.deadline_preset = None
        connect_args.tunnel_timeout_seconds = None
        connect_args.language = None
        connect_args.browser_external_https = False
        connect_args.browser_domain = None
        connect_args.browser_deny_domain = None
        connect_args.browser_headed = False
        connect_args.browser_user_takeover = False
        connect_args.browser_network_inspection = False
        connect_args.browser_payment_confirmation = False
        connect_args.browser_allowed_email = None
        return _handle_bridge(connect_args)
    _emit(verdict.to_dict(), json_output=args.json)
    return 1


def _handle_bridge_oauth(args: argparse.Namespace) -> int:
    """OAuth approval-password management for a saved bridge.

    The approval password IS the bridge credential: the OAuth consent page
    validates it with ``hmac.compare_digest`` against the same value the
    bearer proxy checks. Printing it at bridge startup was a leak and went
    stale after any rotation; this command resolves the CURRENT value at
    copy time and hands it to the clipboard without ever printing it.

    This CLI handler is a thin wrapper over the canonical
    :func:`karox.web_bridge_launcher.copy_oauth_approval_password` service,
    so the TUI P key and the CLI share one typed secret path.
    """
    from .web_bridge_launcher import copy_oauth_approval_password

    if args.bridge_oauth_command != "approval-password":
        _emit({"error": f"unknown oauth command: {args.bridge_oauth_command}"}, json_output=args.json)
        return 1

    result = copy_oauth_approval_password(args.saved)
    payload: dict[str, Any] = {
        "reference": result.reference,
        "fingerprint": result.fingerprint,
    }
    if getattr(args, "copy", False):
        payload["clipboard"] = "copied" if result.copied else "unavailable"
        if not getattr(args, "quiet", False) and not result.copied:
            if result.error:
                sys.stderr.write(result.error + "\n")
            else:
                sys.stderr.write(
                    "clipboard unavailable; the approval password was not printed\n"
                )
    if result.error and not result.copied:
        payload["error"] = result.error
    _emit(payload, json_output=args.json)
    return 0 if result.copied or result.reference else 1


def _handle_bridge_saved(args: argparse.Namespace) -> int:
    store = WebBridgeProfileStore()
    command = args.bridge_saved_command
    if command == "list":
        payload: Any = [profile.to_dict() for profile in store.list()]
    elif command == "show":
        payload = store.get(args.name).to_dict()
    elif command == "delete":
        # Durable profiles own a repository-bound session and keyring secret.
        # Clean those first so a running launcher can refuse deletion without
        # losing the user-visible profile needed to stop it.
        profile = store.get(args.name)
        identity = delete_saved_web_bridge_identity(args.name)
        payload = {
            "status": "deleted",
            "profile": store.delete(args.name).to_dict(),
            "identity_cleanup": identity,
            "session_id": saved_web_bridge_session_id(profile.name),
        }
    elif command == "validate":
        profile = store.get(args.name)
        config = _saved_profile_connect_config(
            profile, repository=args.repository
        )
        payload = {
            "status": "ok",
            "profile": profile.to_dict(),
            "diagnostics": web_bridge_diagnostics(config),
        }
        if config.tunnel == "tailscale":
            payload["tailscale"] = tailscale_doctor()
    elif command == "ensure-developer":
        repository = str(_web_bridge_repository(args.repository))
        tools = tuple(
            dict.fromkeys(
                (*DEFAULT_WEB_TOOLS, *WRITE_WEB_TOOLS, "karox.checks.run")
            )
        )
        commands = (
            ("python", "-m", "ruff", "check", "src", "tests", "scripts"),
            ("python", "-m", "mypy", "src/karox"),
            ("python", "-m", "build", "--wheel"),
        )
        profile = SavedWebBridgeProfile(
            name=args.name,
            target_profile=args.target_profile,
            repository=repository,
            tools=tools,
            verification_commands=commands,
            server_profiles=tuple(
                item.to_public_dict() for item in default_server_profiles()
            ),
            deadline_seconds=_WEB_BRIDGE_DEADLINE_PRESETS["full-suite"],
            tunnel=args.tunnel,
            public_url=args.public_url,
            language=args.language,
            access_profile=AccessProfile.WORKSPACE_WRITE,
            port=args.port,
            tunnel_timeout_seconds=30.0,
        )
        previous = next(
            (item for item in store.list() if item.name == args.name),
            None,
        )
        identity_reset = None
        reasons: list[str] = []
        if previous is not None:
            binding_changed = (
                previous.repository != profile.repository
                or previous.access_profile != profile.access_profile
            )
            identity_exists = saved_web_bridge_identity_exists(previous.name)
            if binding_changed and identity_exists and not args.reset_identity:
                raise ValueError(
                    "developer profile is bound to a different repository or "
                    "access profile; rerun with --reset-identity"
                )
            if args.reset_identity:
                identity_reset = delete_saved_web_bridge_identity(previous.name)
                if identity_reset.get("credential") != "not_found":
                    reasons.append("credential_rotated")
            if previous.tunnel != profile.tunnel or previous.public_url != profile.public_url:
                reasons.append("public_endpoint_changed")
            if previous.target_profile != profile.target_profile:
                reasons.append("target_profile_changed")
            store.put(profile)
            status = "updated"
        else:
            store.put(profile, replace_existing=False)
            status = "created"
        payload = {
            "status": status,
            "profile": profile.to_dict(),
            "launch_command": f"karox bridge connect --saved {profile.name}",
            "identity_reset": identity_reset,
            "connector_reconfigure_required": bool(reasons),
            "connector_reconfigure_reasons": reasons,
        }
    elif command == "create":
        commands = tuple(
            _verification_command(value) for value in args.verification_command
        )
        tools = _web_bridge_tools(args.tool, write=args.write)
        profiles = tuple(
            _server_profile(value) for value in (args.server_profile or [])
        )
        if "karox.dev_server.start" in tools and not profiles:
            profiles = default_server_profiles()
        browser_kwargs = _browser_config_kwargs(args)
        access = _web_bridge_access_profile(
            args.access_profile,
            write=args.write,
            external_browser=browser_kwargs["browser_external_https"],
        )
        repository = (
            str(_web_bridge_repository(args.repository))
            if args.repository is not None
            else None
        )
        profile = SavedWebBridgeProfile(
            name=args.name,
            target_profile=args.target_profile,
            repository=repository,
            tools=tools,
            mcp_servers=tuple(args.mcp_server or []),
            verification_commands=commands,
            server_profiles=tuple(p.to_public_dict() for p in profiles),
            **browser_kwargs,
            deadline_seconds=_web_bridge_deadline(
                args.deadline_seconds, args.deadline_preset
            ),
            tunnel=args.tunnel,
            public_url=args.public_url,
            language=args.language,
            access_profile=access,
            port=args.port,
            tunnel_timeout_seconds=args.tunnel_timeout_seconds,
        )
        store.put(profile, replace_existing=False)
        payload = {
            "status": "created",
            "profile": profile.to_dict(),
            "launch_command": f"karox bridge connect --saved {profile.name}",
        }
    else:
        current = store.get(args.name)
        if args.clear_repository and args.repository is not None:
            raise ValueError("choose --repository or --clear-repository")
        if args.clear_public_url and args.public_url is not None:
            raise ValueError("choose --public-url or --clear-public-url")
        if args.clear_verification_commands and args.verification_command is not None:
            raise ValueError(
                "choose --verification-command or --clear-verification-commands"
            )
        if args.clear_server_profiles and args.server_profile is not None:
            raise ValueError(
                "choose --server-profile or --clear-server-profiles"
            )
        if args.clear_mcp_servers and args.mcp_server is not None:
            raise ValueError("choose --mcp-server or --clear-mcp-servers")
        repository = current.repository
        projects = current.projects
        default_project_id = current.default_project_id
        if args.clear_repository:
            repository = None
            projects = ()
            default_project_id = None
        elif args.repository is not None:
            # The legacy CLI edit means "rebind this saved identity to another
            # repository", not "change the multi-project default". Keep that
            # historical contract by rebuilding a one-project registry here;
            # Ctrl+W manages the allowlist/default without rebinding identity.
            repository = str(_web_bridge_repository(args.repository))
            projects = ()
            default_project_id = None
        tools = _web_bridge_tools(
            args.tool,
            write=args.write,
            fallback=current.tools,
        )
        access = (
            AccessProfile(args.access_profile)
            if args.access_profile
            else (
                AccessProfile.WORKSPACE_WRITE
                if args.write
                else current.access_profile
            )
        )
        commands = current.verification_commands
        if args.clear_verification_commands:
            commands = ()
        elif args.verification_command is not None:
            commands = tuple(
                _verification_command(value) for value in args.verification_command
            )
        profiles = current.server_profiles
        if args.clear_server_profiles:
            profiles = ()
        elif args.server_profile is not None:
            profiles = tuple(
                _server_profile(value).to_public_dict()
                for value in args.server_profile
            )
        if "karox.dev_server.start" in tools and not profiles:
            profiles = tuple(p.to_public_dict() for p in default_server_profiles())
        mcp_servers = current.mcp_servers
        if args.clear_mcp_servers:
            mcp_servers = ()
        elif args.mcp_server is not None:
            mcp_servers = tuple(args.mcp_server)
        effective_tunnel = args.tunnel or current.tunnel
        public_url = current.public_url
        if args.clear_public_url or effective_tunnel != "custom":
            public_url = None
        elif args.public_url is not None:
            public_url = args.public_url
        # Browser policy: tri-state merge. None = preserve current.
        browser_external_https = (
            args.browser_external_https
            if args.browser_external_https is not None
            else current.browser_external_https
        )
        browser_headed = (
            args.browser_headed
            if args.browser_headed is not None
            else current.browser_headed
        )
        browser_user_takeover = (
            args.browser_user_takeover
            if args.browser_user_takeover is not None
            else current.browser_user_takeover
        )
        browser_network_inspection = (
            args.browser_network_inspection
            if args.browser_network_inspection is not None
            else current.browser_network_inspection
        )
        browser_payment_confirmation = (
            args.browser_payment_confirmation
            if args.browser_payment_confirmation is not None
            else current.browser_payment_confirmation
        )
        if getattr(args, "clear_browser_domains", False):
            browser_allowed_domains: tuple[str, ...] = ()
            browser_denied_domains: tuple[str, ...] = ()
            browser_allowed_emails: tuple[str, ...] = ()
        else:
            browser_allowed_domains = (
                tuple(args.browser_domain) if args.browser_domain is not None
                else current.browser_allowed_domains
            )
            browser_denied_domains = (
                tuple(args.browser_deny_domain) if args.browser_deny_domain is not None
                else current.browser_denied_domains
            )
            browser_allowed_emails = (
                tuple(args.browser_allowed_email) if args.browser_allowed_email is not None
                else current.browser_allowed_emails
            )
        updated = replace(
            current,
            target_profile=args.target_profile or current.target_profile,
            repository=repository,
            projects=projects,
            default_project_id=default_project_id,
            tools=tools,
            mcp_servers=mcp_servers,
            verification_commands=commands,
            server_profiles=profiles,
            deadline_seconds=_web_bridge_deadline(
                args.deadline_seconds,
                args.deadline_preset,
                fallback=current.deadline_seconds,
            ),
            tunnel=effective_tunnel,
            public_url=public_url,
            language=args.language or current.language,
            access_profile=access,
            port=args.port if args.port is not None else current.port,
            tunnel_timeout_seconds=(
                args.tunnel_timeout_seconds
                if args.tunnel_timeout_seconds is not None
                else current.tunnel_timeout_seconds
            ),
            browser_external_https=browser_external_https,
            browser_headed=browser_headed,
            browser_user_takeover=browser_user_takeover,
            browser_network_inspection=browser_network_inspection,
            browser_payment_confirmation=browser_payment_confirmation,
            browser_allowed_domains=browser_allowed_domains,
            browser_denied_domains=browser_denied_domains,
            browser_allowed_emails=browser_allowed_emails,
        )
        binding_changed = (
            updated.repository != current.repository
            or updated.access_profile != current.access_profile
        )
        identity_exists = saved_web_bridge_identity_exists(current.name)
        if binding_changed and identity_exists and not args.reset_identity:
            raise ValueError(
                "changing a launched saved profile's repository or access profile "
                "requires --reset-identity; this rotates the connector secret"
            )
        identity_reset = None
        if args.reset_identity:
            identity_reset = delete_saved_web_bridge_identity(current.name)
        store.put(updated)
        payload = {
            "status": "updated",
            "profile": updated.to_dict(),
            "launch_command": f"karox bridge connect --saved {updated.name}",
            "identity_reset": identity_reset,
            "connector_reconfigure_required": bool(
                identity_reset
                and identity_reset.get("credential") != "not_found"
            ),
        }
    _emit(payload, json_output=args.json)
    return 0


def _handle_clickup_connect(args: argparse.Namespace) -> int:
    """Run ClickUp as an independent foreground bridge on Cloudflare.

    This process deliberately owns its own bridge and tunnel lifecycle instead
    of using the TUI's single ``bridge_process`` slot.  A ChatGPT OAuth bridge
    can therefore keep running on 8765 while a second terminal owns ClickUp on
    8766.  Ctrl+C in this terminal stops only the ClickUp children.
    """
    from .clickup_setup import CLICKUP_DEVELOPER_TOOLS, setup_clickup_connection
    from .connections import (
        ClickupDefaults,
        remove_connection,
        resolve_connection_secret,
    )

    tunnel = args.tunnel or "cloudflare"
    if tunnel != "cloudflare":
        raise ValueError("karox connect clickup currently requires --tunnel cloudflare")
    if args.public_url:
        raise ValueError("ClickUp Cloudflare quick tunnels do not use --public-url")
    if args.session_id:
        raise ValueError("ClickUp creates and manages its own bridge session")
    if args.tool or args.write or args.verification_command or args.access_profile:
        raise ValueError(
            "ClickUp uses its fixed guarded workspace developer profile; "
            "--tool/--write/--verification-command/--access-profile do not apply"
        )
    browser_options = _browser_config_kwargs(args)
    if any(bool(value) for value in browser_options.values()):
        raise ValueError("browser policy flags do not apply to the ClickUp bridge")

    repository = _web_bridge_repository(args.repository)
    port = args.port if args.port is not None else 8766
    if not 1 <= port <= 65_535:
        raise ValueError("ClickUp bridge port must be between 1 and 65535")
    name = str(args.name or "ClickUp").strip()
    if not name:
        raise ValueError("ClickUp connection name must not be empty")

    defaults = ClickupDefaults(
        transport="streamable_http",
        auth_scheme="bearer",
        tunnel="cloudflare",
        port=port,
        endpoint_path="/mcp",
        url_stability="temporary",
        secret_source="generate",
        tunnel_reason="separate Cloudflare quick tunnel selected by karox connect clickup",
        tunnel_action="none",
    )
    if args.diagnostics_only:
        _json(
            {
                "connector": "clickup",
                "repository": str(repository),
                "name": name,
                "transport": defaults.transport,
                "auth_scheme": defaults.auth_scheme,
                "authentication_method": "Authorization header",
                "tunnel": defaults.tunnel,
                "port": defaults.port,
                "endpoint_path": defaults.endpoint_path,
                "url_stability": defaults.url_stability,
                "independent_process": True,
                "public_verification_required": True,
                "temporary_connection": True,
                "tools": list(CLICKUP_DEVELOPER_TOOLS),
                "verification_commands": [
                    [sys.executable, "-m", "pytest", "-q"]
                ],
            }
        )
        return 0

    def progress(step: str, state: str, detail: Optional[str]) -> None:
        suffix = f": {detail}" if detail else ""
        print(f"[{step}] {state}{suffix}", file=sys.stderr, flush=True)

    outcome = setup_clickup_connection(
        defaults,
        name=name,
        repository=repository,
        on_progress=progress,
        keep_public_pending=True,
    )
    if not outcome.success or outcome.target is None or not outcome.public_endpoint:
        detail = outcome.failure_detail or "ClickUp setup failed"
        remediation = f"; action: {outcome.remediation}" if outcome.remediation else ""
        raise SavedConnectionError(
            f"{outcome.failure_kind or 'setup_failed'}: {detail}{remediation}"
        )

    secret = resolve_connection_secret(outcome.target)
    handshake_state = str((outcome.handshake or {}).get("state", ""))
    _write_line("")
    if handshake_state == "ok":
        _write_line("ClickUp MCP bridge is publicly verified and ready.")
    else:
        _write_line("ClickUp MCP bridge is locally verified and kept online.")
        _write_line(
            "This PC could not verify the fresh public hostname yet. Create the "
            "new connector now and use ClickUp's connection test as the "
            "authoritative external verification."
        )
    _write_line(f"URL: {outcome.public_endpoint}")
    _write_line("Authentication Method: Authorization header")
    _write_line(f"Secret: {secret}")
    _write_line(f"Local port: {port}")
    _write_line(
        "This Cloudflare Quick Tunnel URL is temporary and changes after restart."
    )
    _write_line("Create a new ClickUp connector for this URL and token.")
    _write_line("Keep this terminal open. Press Ctrl+C to stop only ClickUp.")

    try:
        while True:
            time.sleep(3600.0)
    except KeyboardInterrupt:
        _write_line("Stopping ClickUp bridge and Cloudflare tunnel...")
    finally:
        if outcome.stop is not None:
            with contextlib.suppress(Exception):
                outcome.stop()
        with contextlib.suppress(Exception):
            remove_connection(outcome.target.connection_id)
    _write_line(
        "ClickUp bridge stopped and its temporary KaroX connection was removed. "
        "The current ChatGPT bridge was not touched."
    )
    return 0


def _persist_auto_hosted_profile(
    config: WebBridgeConnectConfig, *, prefix: str
) -> WebBridgeConnectConfig:
    """Persist a secret-free hosted OAuth launch so the TUI can rediscover it."""
    repository_key = str(config.repository).casefold().encode("utf-8")
    digest = hashlib.sha256(repository_key).hexdigest()[:10]
    profile_name = f"{prefix}-auto-{digest}-{config.access_profile.value}"
    profile = SavedWebBridgeProfile(
        name=profile_name,
        target_profile=config.profile,
        repository=str(config.repository),
        tools=tuple(config.tools),
        verification_commands=tuple(config.verification_commands),
        server_profiles=tuple(
            item.to_public_dict() for item in config.server_profiles
        ),
        browser_external_https=config.browser_external_https,
        browser_allowed_domains=tuple(config.browser_allowed_domains),
        browser_denied_domains=tuple(config.browser_denied_domains),
        browser_headed=config.browser_headed,
        browser_user_takeover=config.browser_user_takeover,
        browser_network_inspection=config.browser_network_inspection,
        browser_payment_confirmation=config.browser_payment_confirmation,
        browser_allowed_emails=tuple(config.browser_allowed_emails),
        deadline_seconds=config.deadline_seconds,
        tunnel=config.tunnel,
        public_url=config.public_url,
        language=config.language,
        access_profile=config.access_profile,
        port=config.port,
        tunnel_timeout_seconds=config.tunnel_timeout_seconds,
    )
    WebBridgeProfileStore().put(profile)
    return replace(config, saved_profile_name=profile_name)


def _persist_auto_notion_profile(config: WebBridgeConnectConfig) -> WebBridgeConnectConfig:
    return _persist_auto_hosted_profile(config, prefix="notion")


def _persist_auto_hyperagent_profile(
    config: WebBridgeConnectConfig,
) -> WebBridgeConnectConfig:
    return _persist_auto_hosted_profile(config, prefix="hyperagent")


def _handle_connect(args: argparse.Namespace) -> int:
    """Launch a hosted-client bridge or the independent ClickUp bridge."""
    if args.connector == "clickup":
        return _handle_clickup_connect(args)

    profile = _CONNECTOR_PROFILES[args.connector]
    repository = _web_bridge_repository(args.repository)
    tools = _web_bridge_tools(args.tool, write=args.write)
    commands = (
        tuple(_verification_command(value) for value in args.verification_command)
        if args.verification_command
        else (
            discover_verification_commands(repository)
            if "karox.checks.run" in tools
            else ()
        )
    )
    profiles = (
        server_profiles_for_repository(repository)
        if "karox.dev_server.start" in tools
        else ()
    )
    if "karox.dev_server.start" in tools and not profiles:
        tools = tuple(
            name
            for name in tools
            if name not in {"karox.dev_server.start", "karox.dev_server.stop"}
        )
    browser_kwargs = _browser_config_kwargs(args)
    access = _web_bridge_access_profile(
        args.access_profile,
        write=args.write,
        external_browser=browser_kwargs["browser_external_https"],
    )
    language = args.language or (
        "ru"
        if os.environ.get("KAROX_UI_LANGUAGE", "en").lower().startswith("ru")
        else "en"
    )
    config = WebBridgeConnectConfig(
        profile=profile,
        repository=repository,
        port=args.port if args.port is not None else 8765,
        tools=tools,
        session_id=args.session_id,
        access_profile=access,
        tunnel=args.tunnel or "tailscale",
        public_url=args.public_url,
        cloudflared=args.cloudflared,
        tailscale=args.tailscale,
        tunnel_timeout_seconds=(
            args.tunnel_timeout_seconds
            if args.tunnel_timeout_seconds is not None
            else 30.0
        ),
        deadline_seconds=_web_bridge_deadline(
            args.deadline_seconds, args.deadline_preset
        ),
        verification_commands=commands,
        server_profiles=profiles,
        **browser_kwargs,
        language=language,
    )
    if args.diagnostics_only:
        _json(web_bridge_diagnostics(config))
        return 0
    if args.connector == "notion":
        config = _persist_auto_notion_profile(config)
    elif args.connector == "hyperagent":
        config = _persist_auto_hyperagent_profile(config)
    return run_web_bridge(config)


def _handle_bridge(args: argparse.Namespace) -> int:
    if args.bridge_command == "saved":
        return _handle_bridge_saved(args)
    if args.bridge_command in {"status", "stop", "restart", "attach"}:
        return _handle_bridge_lifecycle(args)
    if args.bridge_command == "oauth":
        return _handle_bridge_oauth(args)
    if args.bridge_command == "connect":
        if args.saved and args.profile:
            raise ValueError("choose a positional PROFILE or --saved NAME, not both")
        if args.saved:
            saved = WebBridgeProfileStore().get(args.saved)
            config = _saved_profile_connect_config(
                saved,
                repository=args.repository,
                session_id=args.session_id,
                tool=args.tool,
                write=args.write,
                access_profile=args.access_profile,
                tunnel=args.tunnel,
                public_url=args.public_url,
                cloudflared=args.cloudflared,
                tailscale=args.tailscale,
                port=args.port,
                verification_command=args.verification_command,
                server_profile=args.server_profile,
                deadline_seconds=args.deadline_seconds,
                deadline_preset=args.deadline_preset,
                tunnel_timeout_seconds=args.tunnel_timeout_seconds,
                language=args.language,
                browser_external_https=args.browser_external_https,
                browser_domain=args.browser_domain,
                browser_deny_domain=args.browser_deny_domain,
                browser_headed=args.browser_headed,
                browser_user_takeover=args.browser_user_takeover,
                browser_network_inspection=args.browser_network_inspection,
                browser_payment_confirmation=args.browser_payment_confirmation,
                browser_allowed_email=args.browser_allowed_email,
            )
        else:
            config = _direct_connect_config(args)
        if args.diagnostics_only:
            _json(web_bridge_diagnostics(config))
            return 0
        return run_web_bridge(config)
    if args.bridge_command == "serve":
        loopback_bind = args.host in {"127.0.0.1", "::1", "localhost"}
        if not loopback_bind and not args.allow_network_bind:
            raise ValueError("non-loopback bridge bind requires --allow-network-bind")
        if (args.tls_certfile is None) != (args.tls_keyfile is None):
            raise ValueError("bridge TLS requires both --tls-certfile and --tls-keyfile")
        # Off the loopback interface the bridge is reachable by anything that can
        # route to this host, and the bearer token plus every file it returns
        # would travel in clear text. --allow-network-bind gates that bind, so it
        # may not be the whole gate.
        if not loopback_bind and args.tls_certfile is None:
            raise ValueError(
                "non-loopback bridge bind requires TLS: pass --tls-certfile and "
                "--tls-keyfile, or keep --host on loopback behind a tunnel"
            )
        for option, value in (
            ("--tls-certfile", args.tls_certfile),
            ("--tls-keyfile", args.tls_keyfile),
        ):
            if value is not None and not Path(value).is_file():
                raise ValueError(f"{option} must point to an existing PEM file")
        if not 1 <= args.port <= 65_535:
            raise ValueError("bridge port must be between 1 and 65535")
        if not args.tool and not args.server:
            raise ValueError("bridge serve requires at least one --tool or --server")
        bridge_profile = BridgeRegistry().get(args.profile)
        protocol = args.protocol or ("openapi" if args.profile == "promptql" else "mcp")
        if bridge_profile.auth_scheme == "oauth":
            if protocol != "mcp":
                raise ValueError("OAuth web bridge profiles require --protocol mcp")
            if not args.public_url:
                raise ValueError(
                    "OAuth web bridge profiles require --public-url with the public HTTPS origin"
                )
        elif args.public_url:
            raise ValueError("--public-url is only valid for OAuth web bridge profiles")
        repository = _mcp_repository(args.repository)
        raw_projects: list[dict[str, Any]] = []
        for raw_project in args.project:
            try:
                decoded_project = json.loads(raw_project)
            except json.JSONDecodeError as exc:
                raise ValueError("--project must be a JSON object") from exc
            if not isinstance(decoded_project, dict):
                raise ValueError("--project must be a JSON object")
            raw_projects.append(decoded_project)
        try:
            project_registry = ProjectRegistry.from_profile(
                repository=str(repository),
                projects=raw_projects,
                default_project_id=args.default_project_id,
            )
        except ProjectRegistryError as exc:
            raise ValueError(f"invalid bridge project registry: {exc}") from exc
        project_registry_loader: Optional[Callable[[], ProjectRegistry]] = None
        if args.saved_profile_name:
            try:
                saved_project_profile = WebBridgeProfileStore().get(args.saved_profile_name)
                saved_anchor = (
                    Path(saved_project_profile.repository).expanduser().resolve(strict=True)
                    if saved_project_profile.repository
                    else None
                )
            except (OSError, ValueError):
                saved_anchor = None
            if saved_anchor is not None and os.path.normcase(str(saved_anchor)) == os.path.normcase(str(repository)):
                def load_saved_project_registry() -> ProjectRegistry:
                    current = WebBridgeProfileStore().get(args.saved_profile_name)
                    return ProjectRegistry.from_profile(
                        repository=current.repository,
                        projects=current.projects,
                        default_project_id=current.default_project_id,
                    )

                project_registry_loader = load_saved_project_registry
        sessions = SessionStore(session_dir())
        record = sessions.load(args.session_id)
        sessions.validate_repository(record, repository)
        audit_path = runtime_dir() / "vnext" / "audit.jsonl"
        runtimes: list[Any] = []
        # ``--tool`` now accepts both Core tools (repo/git/checks) and the
        # hosted browser/dev-server/artifact tools.  They are served by two
        # different runtimes composed into one bridge, so split the selection
        # along that boundary: CoreToolBridge would reject an extra name as
        # unknown, and HostedToolsRuntime would reject a Core name.
        access_profile = AccessProfile(record.access_profile)
        parsed_verification_commands = tuple(
            _verification_command(value) for value in args.verification_command
        )
        effective_tools = _include_stable_worker_commands(
            tuple(args.tool),
            access_profile=access_profile,
            verification_commands=parsed_verification_commands,
        )
        core_tools = [name for name in effective_tools if name in CORE_TOOL_NAMES]
        extra_tools = [
            name for name in effective_tools if name in HOSTED_EXTRA_TOOL_NAMES
        ]
        autonomy_tools = [
            name for name in effective_tools if name in AUTONOMY_TOOL_NAMES
        ]
        if core_tools:
            verification_commands = (
                parsed_verification_commands
                if parsed_verification_commands
                else None
            )
            runtimes.append(
                CoreToolBridge(
                    repository,
                    sessions,
                    record.session_id,
                    core_tools,
                    hosted_origin=Origin(
                        OriginKind.HOSTED_CLIENT,
                        f"{args.profile}-core-{record.session_id}",
                    ),
                    audit_path=audit_path,
                    verification_commands=verification_commands,
                    advertise_unavailable=(args.profile == "hyperagent-web"),
                    project_registry=project_registry,
                    project_registry_loader=project_registry_loader,
                )
            )
        elif args.verification_command:
            raise ValueError("--verification-command requires a Core --tool")
        if extra_tools:
            server_profiles = tuple(
                _server_profile(value) for value in (args.server_profile or [])
            )
            if "karox.dev_server.start" in extra_tools and not server_profiles:
                server_profiles = default_server_profiles()
            browser_policy = BrowserAccessPolicy(
                session_id=record.session_id,
                localhost=True,
                external_https=args.browser_external_https,
                allowed_domains=tuple(args.browser_domain or []),
                denied_domains=tuple(args.browser_deny_domain or []),
                headed=args.browser_headed,
                user_takeover=args.browser_user_takeover,
                network_inspection=args.browser_network_inspection,
                payment_confirmation=args.browser_payment_confirmation,
                allowed_emails=tuple(args.browser_allowed_email or []),
                allowed_credential_refs=tuple(args.browser_credential_ref or []),
                backend=os.environ.get("KAROX_BROWSER_BACKEND", "playwright"),
                saved_profile_id=args.saved_profile_name or "ad-hoc",
            )
            runtimes.append(
                HostedToolsRuntime(
                    repository,
                    sessions,
                    record.session_id,
                    extra_tools,
                    access_profile=access_profile,
                    hosted_origin=Origin(
                        OriginKind.HOSTED_CLIENT,
                        f"{args.profile}-tools-{record.session_id}",
                    ),
                    server_profiles=server_profiles,
                    browser_policy=browser_policy,
                    verification_commands=parsed_verification_commands,
                    audit_path=audit_path,
                    saved_profile_name=args.saved_profile_name,
                )
            )
        if autonomy_tools:
            runtimes.append(
                AutonomyRuntime(
                    repository,
                    sessions,
                    record.session_id,
                    autonomy_tools,
                    access_profile=access_profile,
                    hosted_origin=Origin(
                        OriginKind.HOSTED_CLIENT,
                        f"{args.profile}-autonomy-{record.session_id}",
                    ),
                    connection_profile=args.profile,
                    verification_commands=parsed_verification_commands,
                    operation_runtime=(
                        CompositeHostedBridge(tuple(runtimes)) if runtimes else None
                    ),
                    client_kind=args.profile,
                    project_registry=project_registry,
                    project_registry_loader=project_registry_loader,
                )
            )
        if args.server:
            profile = AccessProfile(record.access_profile)
            policy = CapabilityPolicy(profile)
            hosted = Origin(
                OriginKind.HOSTED_CLIENT,
                f"{args.profile}-mcp-{record.session_id}",
            )
            proxied = Origin(
                OriginKind.PROXIED_MCP,
                f"{args.profile}-external-{record.session_id}",
            )
            policy.set_grants(hosted, {Capability.MCP_CALL})
            policy.set_grants(proxied, {Capability.MCP_CALL})
            runtimes.append(
                McpProxy(
                    McpClient(_mcp_registry()),
                    repository,
                    sessions,
                    record.session_id,
                    args.server,
                    policy=policy,
                    hosted_origin=hosted,
                    proxied_origin=proxied,
                    audit_path=audit_path,
                )
            )
        bridge_runtime = CompositeHostedBridge(runtimes)
        bridge_runtime.descriptors()
        credential_store = BridgeCredentialStore()
        credential_reference = f"os-keyring:bridge/{args.credential}"
        credential_store.resolve(credential_reference)

        def credential() -> str:
            return credential_store.resolve(credential_reference)

        if bridge_profile.auth_scheme == "oauth":
            allowed_redirect_hosts: Optional[frozenset[str]] = None
            if args.allowed_redirect_hosts:
                hosts = frozenset(
                    normalize_host(item.strip())
                    for item in args.allowed_redirect_hosts.split(",")
                    if item.strip()
                )
                if not hosts:
                    raise ValueError(
                        "--allowed-redirect-hosts must list at least one host"
                    )
                allowed_redirect_hosts = hosts
            app = build_oauth_proxy_asgi_app(
                bridge_runtime,
                credential,
                public_url=args.public_url,
                path="/mcp",
                deadline_seconds=args.deadline_seconds,
                # Without this the connector the user just added in ChatGPT or
                # Claude stops working when this process exits.
                state_dir=oauth_state_dir(),
                allowed_redirect_hosts=allowed_redirect_hosts,
            )
        elif protocol == "mcp":
            app = build_proxy_asgi_app(
                bridge_runtime,
                credential,
                deadline_seconds=args.deadline_seconds,
                # RFC 6750 requires a 401 from a bearer-protected resource to
                # name the scheme it wants; without it the response says only
                # "no" and a client cannot tell bearer from OAuth from an API
                # key.  The OAuth profile has always sent this header (pointing
                # at its resource metadata) and the OpenAPI bridge sends a bare
                # ``Bearer``; only this path, the one every generic MCP client
                # uses, stayed silent.  A client that probes a server to decide
                # which auth methods it supports therefore learned nothing here.
                unauthorized_headers={"WWW-Authenticate": "Bearer"},
                # ChatGPT Web pays for large tool output twice: across the hosted
                # transport and again in model context. Keep its inline ceiling
                # aligned with execute_plan's 32 KiB budget; larger results stay
                # fully available through the session-scoped artifact store.
                inline_result_bytes=(32 * 1024 if args.profile == "chatgpt-web" else None),
            )
        else:
            app = build_openapi_bridge_app(
                bridge_runtime,
                credential,
                deadline_seconds=args.deadline_seconds,
                title=f"KaroX {args.profile} bridge",
            )
        import uvicorn

        endpoint = "/mcp" if protocol == "mcp" else "/openapi.json"
        scheme = "https" if args.tls_certfile else "http"
        print(
            f"KaroX bridge: profile={args.profile} protocol={protocol} "
            f"session={record.session_id}",
            flush=True,
        )
        print(
            f"Local endpoint: {scheme}://{args.host}:{args.port}{endpoint}",
            flush=True,
        )
        # The dev servers started through karox.dev_server.start are children of
        # *this* bridge process, so a Ctrl+C here would orphan them without an
        # explicit teardown.  Close every browser session and stop every server
        # this session started before uvicorn returns.
        try:
            uvicorn.run(
                app,
                host=args.host,
                port=args.port,
                # The TUI already shows bridge lifecycle and actionable errors.
                # Per-request access lines (POST /mcp 200 OK) turn normal MCP
                # polling into hundreds of useless transcript rows, so keep
                # warnings/errors while disabling only the access log.
                log_level="warning",
                access_log=False,
                # GPT Web and other hosted MCP clients may keep several pooled
                # HTTP/1.1 connections while the model reasons between tool calls.
                # A one-minute timeout is still short enough for an idle pooled
                # socket to go stale while other connections stay active, so keep
                # bridge sockets reusable across normal multi-minute agent gaps.
                # Five minutes is bounded while avoiding the observed ~60-second
                # stale-socket failures on the hosted connector path.
                timeout_keep_alive=300,
                ssl_certfile=args.tls_certfile,
                ssl_keyfile=args.tls_keyfile,
            )
        finally:
            _cleanup_hosted_runtimes(runtimes, record.session_id)
        return 0
    if args.bridge_command == "list":
        payload: Any = [item.to_dict() for item in BridgeRegistry().list()]
    elif args.bridge_command == "show":
        payload = BridgeRegistry().get(args.name).to_dict()
    elif args.bridge_command == "doctor":
        # Reaping only on the next connect leaves a survivor of a hard kill
        # holding a public tunnel until someone happens to start another bridge,
        # so the diagnostic anyone runs first reaps before it reports.
        reaped = reap_orphaned_web_bridges()
        payload = {
            **BridgeCredentialStore().doctor(),
            "reaped_web_bridge_sessions": list(reaped),
            "saved_profiles": WebBridgeProfileStore().doctor(),
            "tailscale": tailscale_doctor(),
        }
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
        elif command == "copy":
            reference = f"os-keyring:bridge/{args.name}"
            secret = store.resolve(reference)
            copied = clipboard.write_text(clipboard.bearer_value(secret))
            if copied:
                clipboard.schedule_clear()
            elif not getattr(args, "quiet", False):
                sys.stderr.write(
                    "clipboard unavailable; the bridge secret remains in the OS keyring and was not printed\n"
                )
            payload = {
                "reference": reference,
                "fingerprint": store.fingerprint(secret),
                "status": "copied" if copied else "clipboard_unavailable",
                "auto_clear_seconds": (
                    clipboard.CLIPBOARD_AUTO_CLEAR_SECONDS if copied else 0
                ),
            }
            if not copied:
                _emit(payload, json_output=args.json)
                return 1
        elif command == "rotate-key":
            # ``--reveal-secret`` is the only path that may surface the new
            # value, and it is gated on an explicit second flag so a stray
            # toggle or a copied command cannot leak it. Validate before we
            # generate so a refused run leaves the previous credential intact.
            if getattr(args, "reveal_secret", False) and not getattr(
                args, "yes", False
            ):
                raise SystemExit(
                    "--reveal-secret requires explicit --yes confirmation; "
                    "the new secret is not printed"
                )
            new_secret = store.generate()
            # ``set`` with an explicit value writes atomically (the backend
            # either replaces the entry or raises, leaving the old value) and,
            # crucially, does NOT echo the secret back in its result.
            payload = store.set(args.name, new_secret)
            payload["status"] = "rotated"
            if getattr(args, "copy", False):
                copied = clipboard.write_text(clipboard.bearer_value(new_secret))
                clipboard.schedule_clear()
                payload["clipboard"] = "copied" if copied else "unavailable"
                if not getattr(args, "quiet", False) and not copied:
                    sys.stderr.write(
                        "clipboard unavailable; the new bridge secret was "
                        "stored in the OS keyring and not printed\n"
                    )
            if getattr(args, "reveal_secret", False):
                if not getattr(args, "quiet", False):
                    sys.stderr.write(
                        "WARNING: --reveal-secret prints the bridge secret "
                        "to stdout\n"
                    )
                payload["secret"] = new_secret
        else:
            payload = store.delete(args.name)
    _emit(payload, json_output=args.json)
    return 0


def _provider_credential_doctor() -> dict[str, Any]:
    """Report the credentials the configured providers actually reference.

    Probing the keyring unconditionally reported a headless host as degraded
    even when every provider authenticates from the environment and every model
    call works -- a diagnostic that fails on a healthy machine teaches its
    reader to ignore it.
    """
    references = sorted(
        {
            record.credential_ref
            for record in _registry().providers()
            if record.credential_ref
        }
    )
    store = CredentialStore()
    if not references:
        return store.doctor()
    entries = [store.doctor(reference) for reference in references]
    degraded = [item for item in entries if item.get("status") != "ok"]
    return {
        "status": "unavailable" if degraded else "ok",
        "credentials": entries,
    }


def _handle_doctor(args: argparse.Namespace) -> int:
    checks: dict[str, Any] = {}
    probes = {
        "provider_credentials": _provider_credential_doctor,
        "mcp_credentials": lambda: McpCredentialStore().doctor(),
        "bridge_credentials": lambda: BridgeCredentialStore().doctor(),
        "sessions": lambda: {
            "status": "ok",
            "count": len(SessionStore(session_dir()).list()),
        },
        "packs": lambda: {
            "status": "ok",
            "count": len(_pack_registry().list()),
        },
    }
    for name, probe in probes.items():
        try:
            checks[name] = probe()
        except Exception as exc:
            checks[name] = {
                "status": "unavailable",
                "error_type": type(exc).__name__,
                "error": str(redact(str(exc))),
            }
    payload = {
        "status": (
            "ok"
            if all(item.get("status") != "unavailable" for item in checks.values())
            else "degraded"
        ),
        "checks": checks,
    }
    _emit(payload, json_output=args.json)
    return 0


def _pack_registry() -> PackRegistry:
    return PackRegistry(runtime_dir() / "vnext" / "packs")


def _handle_pack(args: argparse.Namespace) -> int:
    command = args.pack_command
    if command == "create":
        target = create_pack_template(
            args.target, name=args.name, description=args.description
        )
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
        if record.access_profile not in {
            AccessProfile.WORKSPACE_WRITE.value,
            AccessProfile.ELEVATED.value,
        }:
            raise SessionError("native agent requires a workspace_write session")
        if record.task != str(redact(args.task)):
            raise SessionError("resume task differs from the existing session task")
    provider, model, context_window, max_output_tokens = _agent_provider(
        args, record, limits
    )

    content = None
    selection: dict[str, Any] | None = None
    if args.skill is None:
        if args.skill_permission:
            raise ValueError("--skill-permission requires --skill")
    else:
        catalog = SkillCatalog(repository, extra_directories=tuple(args.skill_dir))
        content = catalog.load(args.skill)
        decisions = _skill_decisions(args.skill_permission)
        previous = None
        if record is not None:
            previous = _compatible_previous_skill(
                content.metadata,
                _stored_skill(record, content.metadata.name),
            )
        selection = skill_selection(content.metadata, decisions, previous=previous)

    # A resumed session keeps the profile it was created with: changing a
    # provider's Bypass mode must never silently re-permission a run already
    # in flight.  Only a new session picks up the current preference.
    access_profile = (
        AccessProfile(record.access_profile)
        if record is not None
        else _agent_access_profile(args)
    )
    if record is None:
        record = store.create(
            repository,
            args.task,
            access_profile,
            session_id=args.session_id,
        )

    mcp_binding, _ = _selected_mcp_runtime(record, repository)

    native_origin = Origin(OriginKind.NATIVE_AGENT, f"cli-{record.session_id}")
    origin = native_origin
    policy = CapabilityPolicy(access_profile)
    grants = {
        Capability.REPO_READ,
        Capability.REPO_WRITE,
        Capability.PROCESS_RUN,
        Capability.CHECKS_RUN,
        Capability.GIT_READ,
        Capability.MCP_CALL,
    }
    if access_profile == AccessProfile.ELEVATED:
        # Bypass grants the elevated developer capabilities the policy layer
        # already defines -- and nothing beyond it.  Push, publish, and auth
        # commands stay outside every profile by design.
        grants.update(
            {
                Capability.DEV_COMMAND,
                Capability.GIT_COMMIT,
                Capability.NETWORK,
            }
        )
    policy.set_grants(native_origin, grants)
    verification_commands = [
        _verification_command(value) for value in args.verification_command
    ]
    # Project instructions and environment facts join the request-only part of
    # the prompt, exactly like Skill content: the durable history keeps the base
    # prompt, so an edited AGENTS.md cannot retroactively change what a past run
    # was told and a handoff document carries no third-party text.
    system_prompt = SYSTEM_PROMPT
    project_context: dict[str, Any] = {"enabled": False}
    if not args.no_project_context:
        project = discover_project_context(
            repository,
            branch=record.branch,
            verification_commands=verification_commands,
            goal=args.task,
            session_id=record.session_id,
            recursive_context=getattr(args, "recursive_context", "off"),
        )
        system_prompt += project.prompt_suffix
        project_context = {"enabled": True, **project.to_dict()}
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
    # The native agent gets the same extended tool table the hosted bridge hands
    # to a web client: exact-string editing, windowed reads, commit history and a
    # file listing that skips dependency directories. Running KaroX's own agent
    # on the narrower base runtime made it rewrite whole files and walk .venv.
    core = ExtendedCoreRuntime(
        repository,
        policy,
        store,
        runtime_dir() / "vnext" / "audit.jsonl",
        mcp_binding=mcp_binding,
        verification_commands=verification_commands,
    )
    if getattr(args, "recursive_context", "off") == "research" and project_context.get(
        "enabled"
    ):
        raw_map = project_context.get("project_map")
        map_metadata = raw_map if isinstance(raw_map, dict) else {}
        raw_focuses = map_metadata.get("implementation")
        focuses = tuple(
            item
            for item in raw_focuses
            if isinstance(item, str) and item.strip()
        ) if isinstance(raw_focuses, list) else ()
        research_metadata: dict[str, Any] = {
            "enabled": False,
            "strategy": "read_only_subagent",
            "depth": 0,
            "requested_branches": 0,
            "successful_branches": 0,
            "branches": [],
        }
        if focuses:
            branch_count = min(2, len(focuses))
            research_total_seconds = min(
                60.0, max(1.0, float(args.max_seconds) * 0.10)
            )
            research_branch_seconds = research_total_seconds / branch_count
            research_limits = ResearchLimits(
                max_steps=3,
                max_seconds=research_branch_seconds,
                max_output_tokens=min(2_000, max_output_tokens or 2_000),
            )
            try:
                research_block, research_metadata = build_research_context(
                    ResearchSubagent(
                        provider=provider,
                        model=model,
                        core=core,
                        limits=research_limits,
                        reasoning_effort=getattr(args, "effort", None),
                    ),
                    session_id=record.session_id,
                    goal=args.task,
                    focuses=focuses,
                    parent_origin=origin,
                    max_branches=2,
                )
            except Exception as exc:
                research_block = ""
                research_metadata = {
                    **research_metadata,
                    "error": type(exc).__name__,
                }
            research_metadata["budget"] = {
                "branches": branch_count,
                "total_seconds": research_total_seconds,
                "per_branch_seconds": research_branch_seconds,
                "max_steps_per_branch": research_limits.max_steps,
                "max_output_tokens_per_branch": research_limits.max_output_tokens,
            }
            if research_block:
                system_prompt += "\n\n" + research_block
        project_context["research"] = research_metadata
    # Phase 3 shadow mode: publish typed events to the transcript store
    # alongside the existing stream observer. The typed stream runs in
    # parallel with _poll_agent_history; parity evidence decides when the
    # poll path can be removed.
    from .transcript_shadow import make_transcript_observer

    base_observer = _stream_progress() if getattr(args, "stream", False) else None
    transcript_observer = make_transcript_observer(
        record.session_id,
        next_observer=base_observer,
    )
    context_options: dict[str, Any] = {}
    if args.context_utilization is not None:
        context_options["utilization"] = args.context_utilization
    if args.max_tool_result_chars is not None:
        context_options["max_tool_result_chars"] = args.max_tool_result_chars

    return AgentKernel(
        provider=provider,
        model=model,
        core=core,
        sessions=store,
        origin=origin,
        limits=limits,
        system_prompt=system_prompt,
        context=ContextBudget(max_input_tokens=context_window, **context_options),
        max_output_tokens=max_output_tokens,
        project_context=project_context,
        require_change=args.expect == "change",
        reasoning_effort=getattr(args, "effort", None),
        economy_mode=bool(getattr(args, "economy", False)),
        on_event=transcript_observer,
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
        payload = store.doctor(getattr(args, "reference", None))
    _emit(payload, json_output=args.json)
    return 0


def _handle_browser_credential(args: argparse.Namespace) -> int:
    store = BrowserCredentialStore()
    command = args.browser_credential_command
    warning = (
        "Use only fake/test/non-important accounts in KaroX Browser. Never store "
        "primary, personal, financial, work-critical, or otherwise high-value credentials."
    )
    if command == "set":
        if args.stdin:
            username = sys.stdin.readline().rstrip("\r\n")
            password = sys.stdin.readline().rstrip("\r\n")
            if not username or not password:
                raise ValueError("stdin must contain username and password on two non-empty lines")
        else:
            if not args.json:
                print(warning, file=sys.stderr)
            username = input("Test account username/email: ").strip()
            password = getpass.getpass("Test account password: ")
        payload = store.set(args.name, username=username, password=password)
        payload = {**payload, "warning": warning}
        # Drop local references promptly; Python strings are immutable so this is
        # best-effort lifetime reduction rather than a memory-erasure guarantee.
        username = ""
        password = ""
    elif command == "delete":
        payload = store.delete(args.name)
    else:
        payload = {**store.doctor(), "warning": warning}
    _emit(payload, json_output=args.json)
    return 0


def _handle_provider_setup(
    args: argparse.Namespace,
    controller: ProviderController,
) -> dict[str, Any]:
    """Configure, verify, persist, and optionally select one provider/model.

    The live probe runs before any new secret is written to the OS keyring. An
    existing environment/keyring reference is resolved only for that probe and
    the raw value is never emitted or stored in provider JSON.
    """
    preset = provider_preset(args.preset_id)
    if not preset.installable:
        raise ValueError(
            f"provider preset requires a documented specialized adapter: {preset.display_name}"
        )

    base_url = (args.base_url or preset.base_url or "").strip().rstrip("/")
    if not base_url:
        raise ValueError(
            f"preset {preset.display_name} requires --base-url from the provider documentation"
        )
    provider_id = (args.provider_id or preset.preset_id).strip()
    model_id = args.model.strip()
    if not provider_id or not model_id:
        raise ValueError("provider ID and model ID must not be blank")

    existing_reference: Optional[str] = None
    try:
        existing_reference = controller.details(provider_id).provider.credential_ref
    except Exception:
        existing_reference = None

    secret: Optional[str] = None
    probe_secret: Optional[str] = None
    if args.stdin_key:
        secret = sys.stdin.readline().rstrip("\r\n")
        if not secret.strip():
            raise ValueError("stdin did not contain an API key")
        probe_secret = secret
    elif args.credential_ref:
        probe_secret = controller.credentials.resolve(args.credential_ref)
    elif existing_reference:
        probe_secret = controller.credentials.resolve(existing_reference)

    is_local = base_url.startswith(("http://127.0.0.1", "http://localhost"))
    if not is_local and not (probe_secret or existing_reference):
        raise ValueError(
            "remote provider setup requires --stdin-key, --credential-ref, "
            "or an existing saved credential"
        )

    verification: dict[str, Any] = {"status": "skipped"}
    if not args.no_test:
        from .tui import ProviderSetup, _probe_provider

        verification = {
            "status": "ok",
            **_probe_provider(
                ProviderSetup(
                    provider_id=provider_id,
                    adapter=str(preset.adapter_kind),
                    base_url=base_url,
                    model_id=model_id,
                    api_key=probe_secret or "",
                    context_window=args.context_window,
                    max_output_tokens=args.max_output_tokens,
                )
            ),
        }

    mutation = controller.configure_provider_model(
        ProviderRecord(
            provider_id=provider_id,
            adapter_kind=str(preset.adapter_kind),
            base_url=base_url,
            credential_ref=args.credential_ref or existing_reference,
            privacy_class="local" if is_local else preset.privacy_class,
        ),
        ModelRecord(
            provider_id=provider_id,
            model_id=model_id,
            aliases=(),
            context_window=args.context_window,
            max_output_tokens=args.max_output_tokens,
            tools="true",
            streaming="true",
            pricing=_pricing(args),
            provenance=f"preset-setup:{preset.preset_id}",
        ),
        secret=secret,
        activate=not args.no_activate,
    )
    return {
        "status": mutation.status,
        "preset": preset.preset_id,
        "provider": asdict(mutation.provider) if mutation.provider is not None else None,
        "model": asdict(mutation.model) if mutation.model is not None else None,
        "selected_model": (
            asdict(mutation.selected_model)
            if mutation.selected_model is not None
            else None
        ),
        "verification": verification,
        "credential_fingerprint": mutation.credential_fingerprint or "",
    }


def _handle_provider(args: argparse.Namespace) -> int:
    controller = _provider_controller()
    command = args.provider_command
    if command == "presets":
        payload = [item.to_dict() for item in provider_presets()]
    elif command == "setup":
        payload = _handle_provider_setup(args, controller)
    elif command == "add-preset":
        preset = provider_preset(args.preset_id)
        if not preset.installable:
            raise ValueError(
                f"provider preset requires a documented specialized adapter: {preset.display_name}"
            )
        base_url = args.base_url or preset.base_url
        if not base_url:
            raise ValueError(
                f"preset {preset.display_name} requires --base-url from the provider documentation"
            )
        provider_id = args.provider_id or preset.preset_id
        mutation = controller.put_provider(
            ProviderRecord(
                provider_id=provider_id,
                adapter_kind=str(preset.adapter_kind),
                base_url=base_url,
                credential_ref=args.credential_ref,
                privacy_class=preset.privacy_class,
            )
        )
        assert mutation.provider is not None
        payload = {
            **asdict(mutation.provider),
            "preset": preset.preset_id,
            "preset_status": preset.status,
            "privacy_note": preset.privacy_note,
        }
    elif command == "add":
        mutation = controller.put_provider(
            ProviderRecord(
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
        )
        assert mutation.provider is not None
        payload = asdict(mutation.provider)
    elif command == "edit":
        if args.credential_ref and args.clear_credential:
            raise ValueError(
                "--credential-ref cannot be combined with --clear-credential"
            )
        mutation = controller.edit_provider(
            args.provider_id,
            adapter_kind=args.adapter,
            base_url=args.base_url,
            credential_ref=(None if args.clear_credential else args.credential_ref),
            credential_ref_supplied=(
                args.clear_credential or args.credential_ref is not None
            ),
            headers=(
                _pairs(args.header, "header")
                if args.header is not None
                else None
            ),
            query=(
                _pairs(args.query, "query")
                if args.query is not None
                else None
            ),
            privacy_class=args.privacy_class,
            timeout_seconds=args.timeout_seconds,
            max_transport_retries=args.max_transport_retries,
        )
        assert mutation.provider is not None
        payload = asdict(mutation.provider)
    elif command == "remove":
        mutation = controller.remove_provider(
            args.provider_id,
            cascade=args.cascade,
            delete_credential=args.delete_credential,
        )
        assert mutation.provider is not None
        payload = {
            "provider_id": mutation.provider.provider_id,
            "status": "removed",
            "cascade": args.cascade,
            "credential_cleanup": mutation.credential_cleanup,
            "selected_model": (
                asdict(mutation.selected_model)
                if mutation.selected_model is not None
                else None
            ),
        }
    elif command == "list":
        payload = [asdict(item.provider) for item in controller.list()]
    elif command == "show":
        payload = asdict(controller.details(args.provider_id).provider)
    elif command == "details":
        payload = controller.details(args.provider_id).to_dict()
    elif command == "credential-set":
        secret = (
            sys.stdin.readline().rstrip("\r\n")
            if args.stdin
            else getpass.getpass("Provider credential: ")
        )
        payload = controller.set_credential(
            args.provider_id,
            secret,
            credential_name=args.name,
        ).to_dict()
    elif command == "credential-clear":
        payload = controller.clear_credential(
            args.provider_id,
            delete_stored=args.delete_stored,
        ).to_dict()
    else:
        payload = controller.test_provider(
            args.provider_id,
            model_or_alias=args.model,
        )
    _emit(payload, json_output=args.json)
    return 0


def _ecosystem_registry(kind: str) -> EcosystemRegistry:
    presets = {
        "target": TARGET_PRESETS,
        "tool": TOOL_PRESETS,
        "integration": INTEGRATION_PRESETS,
    }[kind]
    return EcosystemRegistry(config_dir() / "vnext" / f"{kind}s.json", presets)


def _handle_ecosystem(args: argparse.Namespace, kind: str) -> int:
    registry = _ecosystem_registry(kind)
    presets = registry.presets
    command = getattr(args, f"{kind}_command")
    if command == "presets":
        payload = [asdict(item) for item in presets.values()]
    elif command == "list":
        payload = [asdict(item) for item in registry.list()]
    elif command == "add":
        payload = asdict(registry.add(args.preset_id, args.id))
    elif command == "configure":
        telemetry_fields = (
            tuple(args.telemetry_field) if kind == "integration" else None
        )
        payload = asdict(
            registry.configure(
                args.item_id,
                _pairs(args.setting, "setting"),
                args.credential_ref,
                telemetry_fields,
            )
        )
    elif command == "doctor":
        payload = registry.doctor(args.item_id)
    elif command == "remove":
        removed = registry.remove(args.item_id)
        payload = {"item_id": removed.item_id, "status": "removed"}
    elif command == "handoff":
        item = registry.get(args.item_id)
        preset = presets[item.preset_id]
        payload = {
            "item_id": item.item_id,
            "target": preset.display_name,
            "status": preset.status,
            "transports": list(preset.transports),
            "instructions": (
                "Run /connect in KaroX and choose the matching MCP/OpenAPI transport. "
                "Copy only the generated endpoint and one-time credential into the external target."
            ),
        }
    elif command == "ask":
        if kind != "target":
            raise ValueError("ask is only available for agent targets")
        config = load_promptql_target(config_dir(), args.item_id)
        prior_interactions = None
        if args.prior_interactions is not None:
            try:
                decoded = json.loads(args.prior_interactions)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"--prior-interactions must be a JSON array: {exc.msg}"
                ) from exc
            if not isinstance(decoded, list):
                raise ValueError("--prior-interactions must be a JSON array")
            prior_interactions = decoded
        client = PromptQLNaturalLanguageClient.from_config(
            config, timeout_seconds=args.deadline_seconds
        )
        response = client.ask(
            args.message, prior_interactions=prior_interactions
        )
        payload = {
            "item_id": args.item_id,
            "assistant_actions": response.assistant_actions,
            "modified_artifacts": response.modified_artifacts,
            "raw": response.raw,
        }
    elif command == "enable":
        current = registry.get(args.item_id)
        preset = presets[current.preset_id]
        if preset.status == "documentation_required":
            raise ValueError(
                f"cannot enable {preset.display_name}: official contract is required"
            )
        payload = asdict(registry.enable(args.item_id, True))
    elif command == "disable":
        payload = asdict(registry.enable(args.item_id, False))
    else:
        item = registry.get(args.item_id)
        payload = {
            **asdict(item),
            "preset_status": presets[item.preset_id].status,
        }
    _emit(payload, json_output=args.json)
    return 0


def _handle_model(args: argparse.Namespace) -> int:
    controller = _provider_controller()
    registry = controller.registry
    command = args.model_command
    if command == "discover":
        provider = registry.provider(args.provider)
        from .tui import ProviderSetup, _discover_models_result

        result = _discover_models_result(
            ProviderSetup(
                provider_id=provider.provider_id,
                adapter=provider.adapter_kind,
                base_url=provider.base_url,
                model_id="",
            )
        )
        if result.base_url != provider.base_url:
            controller.edit_provider(
                provider.provider_id,
                base_url=result.base_url,
            )
        payload = {
            "provider_id": provider.provider_id,
            "base_url": result.base_url,
            "models": [asdict(item) for item in result.models],
            "saved": False,
        }
    elif command == "map":
        mutation = controller.map_alias(args.provider, args.alias, args.model_id)
        assert mutation.model is not None
        payload = asdict(mutation.model)
    elif command == "add":
        mutation = controller.put_model(
            ModelRecord(
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
        )
        assert mutation.model is not None
        payload = asdict(mutation.model)
    elif command == "remove":
        mutation = controller.remove_model(args.provider_id, args.model)
        assert mutation.model is not None
        payload = {
            "provider_id": mutation.model.provider_id,
            "model_id": mutation.model.model_id,
            "status": "removed",
            "selected_model": (
                asdict(mutation.selected_model)
                if mutation.selected_model is not None
                else None
            ),
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
        mutation = controller.select_model(args.provider_id, args.model)
        assert mutation.selected_model is not None
        payload = {
            "provider_id": mutation.selected_model.provider_id,
            "model_id": mutation.selected_model.model_id,
            "status": "selected",
        }
    elif command == "repair-selection":
        selected = controller.repair_selection(preferred_provider=args.provider)
        payload = {
            "status": "selected" if selected is not None else "no_models",
            "selected_model": asdict(selected) if selected is not None else None,
        }
    else:
        payload = controller.test_provider(
            args.provider_id,
            model_or_alias=args.model,
        )
    _emit(payload, json_output=args.json)
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        from .tui import run_tui

        return run_tui(repository=str(Path.cwd().resolve()))

    # Documented plural spelling; preserve the original singular command.
    if arguments[0] == "models":
        arguments[0] = "model"

    args = _parser().parse_args(arguments)
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

        if args.command == "browser-credential":
            return _handle_browser_credential(args)

        if args.command == "provider":
            return _handle_provider(args)

        if args.command == "model":
            return _handle_model(args)

        if args.command in {"target", "tool", "integration"}:
            return _handle_ecosystem(args, args.command)

        if args.command == "skill":
            return _handle_skill(args)

        if args.command == "mcp":
            return _handle_mcp(args)

        if args.command == "connections":
            return _handle_connections(args)

        if args.command == "bridge":
            return _handle_bridge(args)

        if args.command == "connect":
            return _handle_connect(args)

        if args.command == "doctor":
            return _handle_doctor(args)

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
        if args.session_command == "revoke":
            record = store.revoke(args.session_id)
            payload = _record_summary(record)
            _json(payload) if args.json else _print_mapping(payload)
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
        SavedConnectionError,
        ConnectionRuntimeError,
        CoreError,
        HostedBridgeError,
        MigrationError,
        McpError,
        PackError,
        PromptQLInvocationError,
        ProviderError,
        RegistryError,
        SessionError,
        SkillError,
        WebBridgeLaunchError,
        OSError,
        ValueError,
    ) as exc:
        print(f"karox: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
