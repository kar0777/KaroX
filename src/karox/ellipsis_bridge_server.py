"""Authenticated local OpenAPI server for an Ellipsis remote agent.

This process has no Ellipsis token and no cloud repository. It owns only one
repository-scoped KaroX session and resolves the short-lived bearer value through
the OS keyring on every request, so expiry and revocation take effect without a
restart.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable, Optional, Sequence

from fastapi import Request
from fastapi.responses import JSONResponse

from .core import CoreRuntime, ToolDefinition
from .hosted_bridge import DEFAULT_HOSTED_DEADLINE_SECONDS, HostedBridgeAccessDenied
from .models import AccessProfile, Capability, CoreCommand, Origin, OriginKind
from .openapi_bridge import build_openapi_bridge_app
from .paths import runtime_dir, session_dir
from .policy import CapabilityPolicy
from .proxy import ProxyToolDescriptor
from .remote_lease import EllipsisLeaseError, EllipsisLeaseStore
from .remote_tools import RemoteCoreRuntime
from .security import redact
from .sessions import SessionStore


ELLIPSIS_TOOL_NAMES: dict[str, str] = {
    "karox.repo.read_file": "repo.read_file",
    "karox.repo.read_lines": "repo.read_lines",
    "karox.repo.write_file": "repo.write_file",
    "karox.repo.patch": "repo.edit_file",
    "karox.repo.list_files": "repo.list_files",
    "karox.repo.search": "repo.search",
    "karox.command.run": "command.run",
    "karox.checks.run": "checks.run",
    "karox.process.start": "process.start",
    "karox.process.status": "process.status",
    "karox.process.stop": "process.stop",
    "karox.browser.fetch": "browser.fetch",
    "karox.browser.actions": "browser.actions",
    "karox.git.status": "git.status",
    "karox.git.diff": "git.diff",
    "karox.git.log": "git.log",
    "karox.git.commit": "git.commit",
    "karox.report.get": "report.get",
}

READ_ONLY_TOOLS = (
    "karox.repo.read_file",
    "karox.repo.read_lines",
    "karox.repo.list_files",
    "karox.repo.search",
    "karox.git.status",
    "karox.git.diff",
    "karox.git.log",
    "karox.report.get",
)
WORKSPACE_TOOLS = (
    *READ_ONLY_TOOLS,
    "karox.repo.write_file",
    "karox.repo.patch",
    "karox.command.run",
    "karox.checks.run",
    "karox.process.start",
    "karox.process.status",
    "karox.process.stop",
)
ELEVATED_TOOLS = (
    *WORKSPACE_TOOLS,
    "karox.browser.fetch",
    "karox.browser.actions",
    "karox.git.commit",
)


def tools_for_profile(profile: AccessProfile) -> tuple[str, ...]:
    if profile is AccessProfile.READ_ONLY:
        return READ_ONLY_TOOLS
    if profile is AccessProfile.WORKSPACE_WRITE:
        return WORKSPACE_TOOLS
    return ELEVATED_TOOLS


class EllipsisCoreBridge:
    """Expose selected RemoteCoreRuntime tools for one immutable local session."""

    def __init__(
        self,
        repository: Path,
        sessions: SessionStore,
        session_id: str,
        allowed_tool_names: Sequence[str],
        *,
        verification_commands: Optional[Iterable[Iterable[str]]] = None,
        command_commands: Optional[Iterable[Iterable[str]]] = None,
        deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
    ) -> None:
        if not allowed_tool_names or len(set(allowed_tool_names)) != len(allowed_tool_names):
            raise HostedBridgeAccessDenied(
                "Ellipsis tool allowlist must be non-empty and unique"
            )
        unknown = set(allowed_tool_names).difference(ELLIPSIS_TOOL_NAMES)
        if unknown:
            raise HostedBridgeAccessDenied(
                "unknown Ellipsis KaroX tools: " + ", ".join(sorted(unknown))
            )
        self.repository = repository.expanduser().resolve(strict=True)
        self.sessions = sessions
        self.session_id = session_id
        self.hosted_origin = Origin(OriginKind.HOSTED_CLIENT, f"ellipsis-{session_id}")
        self.audit_path = runtime_dir() / "vnext" / "audit.jsonl"
        self._allowed = tuple(allowed_tool_names)
        self._verification_commands = (
            None
            if verification_commands is None
            else tuple(tuple(item) for item in verification_commands)
        )
        self._command_commands = tuple(tuple(item) for item in (command_commands or ()))
        self.deadline_seconds = float(deadline_seconds)
        if "karox.checks.run" in self._allowed and not self._verification_commands:
            raise HostedBridgeAccessDenied(
                "karox.checks.run requires a verification allowlist"
            )
        if (
            {"karox.command.run", "karox.process.start"}.intersection(self._allowed)
            and not self._command_commands
        ):
            raise HostedBridgeAccessDenied(
                "command/process tools require a command allowlist"
            )
        record = sessions.load(session_id)
        sessions.validate_repository(record, self.repository)
        if record.revoked:
            raise HostedBridgeAccessDenied("session access has been revoked")
        self.profile = AccessProfile(record.access_profile)
        allowed_by_profile = set(tools_for_profile(self.profile))
        disallowed = set(self._allowed).difference(allowed_by_profile)
        if disallowed:
            raise HostedBridgeAccessDenied(
                "session access profile blocks tools: " + ", ".join(sorted(disallowed))
            )
        self.policy = CapabilityPolicy(self.profile)
        definitions = self._definitions()
        grants: set[Capability] = set()
        for public_name in self._allowed:
            definition = definitions[ELLIPSIS_TOOL_NAMES[public_name]]
            grants.add(definition.capability)
            grants.update(definition.additional_capabilities)
        self.policy.set_grants(self.hosted_origin, grants)
        for capability in grants:
            if not self.policy.decide(self.hosted_origin, capability).allowed:
                raise HostedBridgeAccessDenied(
                    f"session profile does not allow {capability.value}"
                )

    def _core(self) -> CoreRuntime:
        return RemoteCoreRuntime(
            self.repository,
            self.policy,
            self.sessions,
            self.audit_path,
            verification_commands=self._verification_commands,
            command_commands=self._command_commands,
        )

    def _definitions(self) -> dict[str, ToolDefinition]:
        return {item.name: item for item in self._core().tools()}

    def _record(self):
        record = self.sessions.load(self.session_id)
        self.sessions.validate_repository(record, self.repository)
        if record.revoked:
            raise HostedBridgeAccessDenied("session access has been revoked")
        return record

    def descriptors(self) -> list[ProxyToolDescriptor]:
        self._record()
        definitions = self._definitions()
        return [
            ProxyToolDescriptor(
                name=public_name,
                description=definitions[ELLIPSIS_TOOL_NAMES[public_name]].description,
                input_schema=definitions[ELLIPSIS_TOOL_NAMES[public_name]].input_schema,
                read_only=not definitions[ELLIPSIS_TOOL_NAMES[public_name]].mutates,
            )
            for public_name in self._allowed
        ]

    def session_info(self) -> dict[str, Any]:
        record = self._record()
        return dict(
            redact(
                {
                    "session_id": record.session_id,
                    "task": record.task,
                    "repository": str(self.repository),
                    "branch": record.branch,
                    "access_profile": record.access_profile,
                    "status": record.status,
                    "revision": record.revision,
                    "changed_files": list(record.changed_files),
                    "checks": list(record.checks),
                }
            )
        )

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
    ) -> dict[str, Any]:
        self._record()
        core_name = ELLIPSIS_TOOL_NAMES.get(tool_name)
        if core_name is None or tool_name not in self._allowed:
            raise HostedBridgeAccessDenied(f"KaroX tool is not exposed: {tool_name}")
        definition = self._definitions()[core_name]
        if definition.mutates and not idempotency_key:
            raise HostedBridgeAccessDenied(
                "mutating remote calls require an idempotency key"
            )
        effective_deadline = min(max(float(deadline_seconds), 0.1), self.deadline_seconds)
        correlation = hashlib.sha256(
            f"{self.session_id}\0{tool_name}\0{idempotency_key or ''}".encode("utf-8")
        ).hexdigest()[:32]
        command = CoreCommand(
            name=core_name,
            arguments=dict(arguments),
            session_id=self.session_id,
            origin=self.hosted_origin,
            correlation_id=f"ellipsis-{correlation}",
            idempotency_key=idempotency_key,
            deadline_seconds=effective_deadline,
        )
        lease = None
        if definition.mutates:
            lease = self.sessions.acquire(
                self.session_id,
                f"ellipsis-{os.getpid()}",
                ttl_seconds=max(30.0, min(3600.0, effective_deadline + 10.0)),
            )
        try:
            return self._core().execute(command, lease=lease).to_dict()
        finally:
            if lease is not None:
                self.sessions.release(lease)


def build_ellipsis_bridge_app(
    runtime: EllipsisCoreBridge,
    *,
    credential_name: str,
    lease_store: Optional[EllipsisLeaseStore] = None,
    deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
):
    leases = lease_store or EllipsisLeaseStore()

    def credential() -> str:
        return leases.resolve(
            credential_name,
            local_session_id=runtime.session_id,
            repository=runtime.repository,
            access_profile=runtime.profile.value,
        )

    app = build_openapi_bridge_app(
        runtime,
        credential,
        deadline_seconds=deadline_seconds,
        title="KaroX Ellipsis local workspace",
    )

    @app.middleware("http")
    async def enforce_binding(request: Request, call_next):
        if request.url.path == "/":
            return await call_next(request)
        if request.headers.get("x-karox-remote-protocol") != "1":
            return JSONResponse(status_code=401, content={"error": "invalid_remote_protocol"})
        if request.headers.get("x-karox-session-id") != runtime.session_id:
            return JSONResponse(status_code=401, content={"error": "session_binding_mismatch"})
        try:
            leases.validate(
                credential_name,
                local_session_id=runtime.session_id,
                repository=runtime.repository,
                access_profile=runtime.profile.value,
            )
        except EllipsisLeaseError:
            return JSONResponse(
                status_code=401,
                content={"error": "credential_lease_unavailable"},
            )
        return await call_next(request)

    return app


def _json_argv(value: str) -> tuple[str, ...]:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError("allowlist entry must be a JSON array") from exc
    if (
        not isinstance(decoded, list)
        or not decoded
        or not all(isinstance(item, str) and item for item in decoded)
    ):
        raise ValueError("allowlist entry must contain non-empty strings")
    return tuple(decoded)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="karox-ellipsis-bridge")
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--credential-name", required=True)
    parser.add_argument(
        "--access-profile",
        choices=[item.value for item in AccessProfile],
        required=True,
    )
    parser.add_argument("--tool", action="append", default=[])
    parser.add_argument("--verification-command", action="append", default=[])
    parser.add_argument("--command-allow", action="append", default=[])
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument(
        "--deadline-seconds",
        type=float,
        default=DEFAULT_HOSTED_DEADLINE_SECONDS,
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    repository = args.repository.expanduser().resolve(strict=True)
    sessions = SessionStore(session_dir())
    record = sessions.load(args.session_id)
    sessions.validate_repository(record, repository)
    if record.access_profile != args.access_profile:
        raise SystemExit("session access profile mismatch")
    selected = tuple(args.tool) or tools_for_profile(AccessProfile(args.access_profile))
    verification = tuple(_json_argv(value) for value in args.verification_command)
    commands = tuple(_json_argv(value) for value in args.command_allow)
    runtime = EllipsisCoreBridge(
        repository,
        sessions,
        args.session_id,
        selected,
        verification_commands=verification or None,
        command_commands=commands,
        deadline_seconds=args.deadline_seconds,
    )
    app = build_ellipsis_bridge_app(
        runtime,
        credential_name=args.credential_name,
        deadline_seconds=args.deadline_seconds,
    )
    import uvicorn

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="warning",
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
