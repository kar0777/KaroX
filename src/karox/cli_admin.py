"""Small command groups: skills, doctor, status and credentials."""

from __future__ import annotations

import argparse
import getpass
import sys
from typing import Any
from . import __version__
from .cli_common import (
    emit as _emit,
)
from .credentials import CredentialError, CredentialStore
from .mcp_client import (
    McpError,
)
from .mcp_status import (
    build_mcp_status,
)
from .paths import config_dir, runtime_dir, session_dir
from .registry import (
    RegistryError,
)
from .sessions import SessionError, SessionStore
from .skills import SkillCatalog

from .cli_context import _mcp_registry, _registry
from .cli_present import _record_summary

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
