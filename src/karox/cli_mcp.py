"""The `mcp` command groups: outbound server registry, per-session selection, status, calls and credentials."""

from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time
from typing import Any, Optional
from .cli_common import (
    emit as _emit,
)
from .core import CoreCommand, CoreRuntime
from .credentials import CredentialError
from .mcp_client import (
    McpAccessDenied,
    McpClient,
    McpConfigurationError,
    McpCredentialStore,
    McpError,
    McpServerRecord,
    mcp_selection,
)
from .mcp_status import (
    LIVENESS_FAILED,
    LIVENESS_LIVE,
    McpLiveness,
    build_mcp_status,
)
from .models import AccessProfile, Capability, Origin, OriginKind
from .paths import runtime_dir, session_dir
from .policy import CapabilityPolicy
from .risk_engine import RiskEngine
from .sessions import SessionStore

from .cli_context import _mcp_registry
from .cli_present import _pairs
from .cli_runtime import (
    _mcp_repository,
    _replace_stored_mcp,
    _selected_mcp_runtime,
    _stored_mcp,
)

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
        # A direct CLI MCP call is still a mutating Core command. The absence
        # of an explicit RiskEngine here used to make this one path skip the
        # consequence gate that every other Core entrypoint uses.
        risk=RiskEngine(),
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
