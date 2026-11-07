"""The release command surface: agent, sessions, providers, MCP, skills.

The handlers live in focused modules so one change has one owner:

* ``cli_agent``   — ``agent run`` (session, policy, Core, kernel report);
* ``cli_session`` — the durable-session command group;
* ``cli_provider``— ``provider``/``model`` presets, catalog and registry;
* ``cli_mcp``     — outbound MCP registry/selection/status/call;
* ``cli_bridge``  — the hosted-bridge lifecycle;
* ``cli_admin``   — skills, doctor, status and credentials;
* ``cli_runtime`` — provider route and MCP selection assembly;
* ``cli_present`` — argv aliases and human/NDJSON presentation;
* ``cli_context`` — the three durable registry constructors.

This module keeps the public ``karox.cli.main`` entrypoint and the historical
helper names that tests and embedders import. It deliberately holds almost no
behavior: a command implementation belongs in its own module.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

from . import __version__ as __version__
from .agent import AgentError
from .cli_admin import (
    _handle_credential as _handle_credential,
    _handle_doctor as _handle_doctor,
    _handle_skill as _handle_skill,
    _handle_status as _handle_status,
)
from .cli_agent import _agent_grants as _agent_grants
from .cli_agent import _handle_agent_run as _handle_agent_run
from .cli_bridge import handle_bridge as _handle_bridge
from .cli_common import emit as _emit
from .cli_common import verification_argv as _verification_argv
from .cli_context import (
    _controller as _controller,
    _mcp_registry as _mcp_registry,
    _registry as _registry,
)
from .cli_mcp import _handle_mcp as _handle_mcp
from .cli_parser import _parser as _parser
from .cli_present import (
    _load_pricing_registry as _load_pricing_registry,
    _normalize_argv as _normalize_argv,
    _pairs as _pairs,
    _pricing as _pricing,
    _print_agent_report as _print_agent_report,
    _record_summary as _record_summary,
    _stream_json as _stream_json,
    _stream_progress as _stream_progress,
)
from .cli_provider import (
    _endpoint_variables as _endpoint_variables,
    _handle_model as _handle_model,
    _handle_provider as _handle_provider,
)
from .cli_runtime import (
    _attach_mcp_servers as _attach_mcp_servers,
    _budget_currency as _budget_currency,
    _mcp_server_request as _mcp_server_request,
    _project_instructions as _project_instructions,
    _run_vision_capability as _run_vision_capability_impl,
    _selected_mcp_runtime as _selected_mcp_runtime,
)
from .cli_session import _handle_session as _handle_session
from .core import CoreError
from .credentials import CredentialError
from .mcp_client import McpError
from .paths import config_dir, runtime_dir, session_dir
from .providers import ProviderError
from .registry import RegistryError
from .sessions import SessionError
from .skills import SkillError


def _run_vision_capability(args: argparse.Namespace) -> bool:
    """Compatibility seam: the registry factory stays patchable in this module."""

    return _run_vision_capability_impl(args, _registry)


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


__all__ = [
    "__version__",
    "_agent_grants",
    "_attach_mcp_servers",
    "_budget_currency",
    "_controller",
    "_endpoint_variables",
    "_handle_agent_run",
    "_handle_bridge",
    "_handle_credential",
    "_handle_doctor",
    "_handle_mcp",
    "_handle_model",
    "_handle_provider",
    "_handle_session",
    "_handle_skill",
    "_handle_status",
    "_load_pricing_registry",
    "_mcp_registry",
    "_mcp_server_request",
    "_normalize_argv",
    "_pairs",
    "_parser",
    "_pricing",
    "_print_agent_report",
    "_project_instructions",
    "_record_summary",
    "_registry",
    "_run_vision_capability",
    "_selected_mcp_runtime",
    "_stream_json",
    "_stream_progress",
    "_verification_argv",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
