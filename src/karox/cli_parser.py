"""The command surface: one parser, declarative and separate from handlers.

``cli.py`` builds the run; this module only declares what can be typed and
which defaults each option carries, so the surface can be reviewed without
reading any handler.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from . import __version__
from .bridge_profiles import WEB_BRIDGE_PROFILES
from .models import AccessProfile
from .registry import ADAPTER_KINDS, PRIVACY_CLASSES


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
    run.add_argument(
        "--route",
        action="append",
        default=[],
        help="provider/model route to try in order (repeatable; first is primary)",
    )
    run.add_argument(
        "--route-strategy", choices=("ordered", "cheapest"), default="ordered"
    )
    run.add_argument("--privacy-limit", choices=sorted(PRIVACY_CLASSES))
    run.add_argument("--max-total-tokens", type=int)
    run.add_argument("--max-cost", type=float)
    run.add_argument("--currency")
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
    run.add_argument("--no-project-context", action="store_true")
    run.add_argument("--no-memory", action="store_true")
    run.add_argument("--expect", choices=("auto", "change"), default="auto")
    run.add_argument("--max-steps", type=int)
    run.add_argument("--max-seconds", type=float)
    run.add_argument("--context-window", type=int)
    run.add_argument("--max-output-tokens", type=int)
    run.add_argument(
        "--image",
        action="append",
        default=[],
        help="attach one repository-confined PNG/JPEG/WebP (repeatable)",
    )
    run.add_argument("--stream", action="store_true")
    run.add_argument("--stream-json", action="store_true")
    run.add_argument(
        "--accept-discovered-checks",
        action="store_true",
        help="treat repository-discovered checks as approved verification evidence",
    )
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
    add.add_argument("--adapter", required=True, choices=sorted(ADAPTER_KINDS))
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
    edit.add_argument("--adapter", choices=sorted(ADAPTER_KINDS))
    edit.add_argument("--base-url")
    edit_credential = edit.add_mutually_exclusive_group()
    edit_credential.add_argument("--credential-ref")
    edit_credential.add_argument("--clear-credential", action="store_true")
    edit.add_argument("--header", action="append", default=[])
    edit.add_argument("--query", action="append", default=[])
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
    setup.add_argument("--pricing-version")
    setup.add_argument("--currency")
    setup.add_argument("--input-per-million", type=float)
    setup.add_argument("--output-per-million", type=float)
    setup.add_argument("--cache-read-per-million", type=float)
    setup.add_argument("--cache-write-per-million", type=float)
    setup.add_argument("--pricing-source")
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
    add_model.add_argument("--pricing-version")
    add_model.add_argument("--currency")
    add_model.add_argument("--input-per-million", type=float)
    add_model.add_argument("--output-per-million", type=float)
    add_model.add_argument("--cache-read-per-million", type=float)
    add_model.add_argument("--cache-write-per-million", type=float)
    add_model.add_argument("--pricing-source")
    add_model.add_argument("--provenance", default="manual")
    add_model.add_argument("--json", action="store_true")
    discover = models.add_parser("discover")
    discover.add_argument("provider_id")
    discover.add_argument("--timeout-seconds", type=float, default=15.0)
    discover.add_argument("--save", action="store_true")
    discover.add_argument("--json", action="store_true")
    map_model = models.add_parser("map")
    map_model.add_argument("provider_id")
    map_model.add_argument("alias")
    map_model.add_argument("model")
    map_model.add_argument("--json", action="store_true")
    repair = models.add_parser("repair-selection")
    repair.add_argument("--preferred-provider")
    repair.add_argument("--json", action="store_true")
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

    bridge = commands.add_parser(
        "bridge", help="hosted MCP bridge for ChatGPT, Claude and HyperAgent web"
    )
    bridge_commands = bridge.add_subparsers(dest="bridge_command", required=True)
    bridge_connect = bridge_commands.add_parser(
        "connect", help="start a managed bridge and keep it in the foreground"
    )
    bridge_connect.add_argument(
        "profile", nargs="?", choices=sorted(WEB_BRIDGE_PROFILES)
    )
    bridge_connect.add_argument("--saved")
    bridge_connect.add_argument("--repository", type=Path, default=Path.cwd())
    bridge_connect.add_argument("--session-id")
    bridge_connect.add_argument("--port", type=int, default=8765)
    bridge_connect.add_argument("--tool", action="append", default=[])
    bridge_connect.add_argument("--write", action="store_true")
    bridge_connect.add_argument(
        "--access-profile", choices=[item.value for item in AccessProfile]
    )
    bridge_connect.add_argument(
        "--tunnel", choices=("auto", "tailscale", "custom"), default="auto"
    )
    bridge_connect.add_argument("--public-url")
    bridge_connect.add_argument("--tailscale")
    bridge_connect.add_argument("--tunnel-timeout-seconds", type=float, default=30.0)
    bridge_connect.add_argument("--deadline-seconds", type=float, default=600.0)
    bridge_connect.add_argument("--verification-command", action="append", default=[])
    bridge_connect.add_argument("--language", choices=("en", "ru"), default="en")
    bridge_connect.add_argument("--diagnostics-only", action="store_true")
    bridge_connect.add_argument("--json", action="store_true")

    bridge_serve = bridge_commands.add_parser(
        "serve", help="serve one bridge child on loopback"
    )
    # One option definition shared with `python -m karox.bridge_serve`, so the
    # launcher's argv can never drift out of this parser again.
    from .bridge_serve import add_serve_arguments

    add_serve_arguments(bridge_serve, profile_required=False)
    bridge_serve.add_argument("--json", action="store_true")

    bridge_saved = bridge_commands.add_parser("saved", help="saved bridge profiles")
    saved_commands = bridge_saved.add_subparsers(
        dest="bridge_saved_command", required=True
    )
    saved_commands.add_parser("list").add_argument("--json", action="store_true")
    for name in ("show", "delete", "validate"):
        item = saved_commands.add_parser(name)
        item.add_argument("name")
        item.add_argument("--json", action="store_true")
    saved_create = saved_commands.add_parser("create")
    saved_create.add_argument("name")
    saved_create.add_argument("--target-profile", required=True, choices=sorted(WEB_BRIDGE_PROFILES))
    saved_create.add_argument("--repository", type=Path, default=Path.cwd())
    saved_create.add_argument("--tool", action="append", default=[])
    saved_create.add_argument("--write", action="store_true")
    saved_create.add_argument("--verification-command", action="append", default=[])
    saved_create.add_argument("--deadline-seconds", type=float, default=600.0)
    saved_create.add_argument(
        "--tunnel", choices=("auto", "tailscale", "custom"), default="tailscale"
    )
    saved_create.add_argument("--public-url")
    saved_create.add_argument("--language", choices=("en", "ru"), default="en")
    saved_create.add_argument("--port", type=int, default=8765)
    saved_create.add_argument("--tunnel-timeout-seconds", type=float, default=30.0)
    saved_create.add_argument("--replace", action="store_true")
    saved_create.add_argument("--json", action="store_true")

    for name in ("status", "start", "stop", "restart"):
        item = bridge_commands.add_parser(name)
        item.add_argument("--saved", required=True)
        item.add_argument("--json", action="store_true")
    bridge_commands.add_parser("list").add_argument("--json", action="store_true")
    bridge_show = bridge_commands.add_parser("show")
    bridge_show.add_argument("profile")
    bridge_show.add_argument("--json", action="store_true")
    bridge_commands.add_parser("doctor").add_argument("--json", action="store_true")

    bridge_oauth = bridge_commands.add_parser("oauth")
    oauth_commands = bridge_oauth.add_subparsers(
        dest="bridge_oauth_command", required=True
    )
    approval = oauth_commands.add_parser("approval-password")
    approval.add_argument("--saved")
    approval.add_argument("--session-id")
    approval.add_argument("--copy", action="store_true")
    approval.add_argument("--quiet", action="store_true")
    approval.add_argument("--json", action="store_true")

    bridge_credential = bridge_commands.add_parser("credential")
    bridge_credentials = bridge_credential.add_subparsers(
        dest="bridge_credential_command", required=True
    )
    for name in ("set", "show", "copy", "revoke", "rotate"):
        item = bridge_credentials.add_parser(name)
        item.add_argument("session_id")
        if name in {"show", "copy"}:
            item.add_argument("--copy", action="store_true")
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
    for name in ("set", "show", "delete", "doctor", "setup"):
        item = credentials.add_parser(name)
        if name not in {"doctor", "setup"}:
            item.add_argument("name")
        if name == "set":
            item.add_argument("--stdin", action="store_true")
        if name == "doctor":
            item.add_argument("--reference")
        if name == "setup":
            action = item.add_mutually_exclusive_group()
            action.add_argument("--activate", action="store_true")
            action.add_argument("--unlock", action="store_true")
            action.add_argument("--verify", action="store_true")
            item.add_argument("--consent", action="store_true")
        item.add_argument("--json", action="store_true")

    doctor = commands.add_parser(
        "doctor", help="check providers, credentials, MCP and sessions"
    )
    doctor.add_argument("--json", action="store_true")
    status = commands.add_parser("status", help="show the current local state")
    status.add_argument("--json", action="store_true")

    paths = commands.add_parser("paths", help="show resolved application paths")
    paths.add_argument("--json", action="store_true")
    return parser
