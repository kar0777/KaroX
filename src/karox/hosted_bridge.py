"""Hosted-client access to explicitly selected KaroX Core tools.

The external MCP proxy in :mod:`karox.proxy` exposes selected third-party MCP
tools.  This module covers the other half of the bridge contract: a hosted
client can call KaroX's own repository/check/Git tools through the same Core
Runtime boundary used by the native agent.
"""

from __future__ import annotations

import hashlib
import os
import threading
from pathlib import Path
from typing import Any, Iterable, Optional, Protocol, Sequence

from .core import CoreRuntime, ToolDefinition
from .core_tools import ExtendedCoreRuntime
from .models import Capability, CoreCommand, Origin, OriginKind
from .policy import CapabilityPolicy
from .proxy import ProxyToolDescriptor
from .security import redact
from .sessions import SessionStore


class HostedBridgeError(RuntimeError):
    pass


class HostedBridgeAccessDenied(HostedBridgeError, PermissionError):
    pass


CORE_TOOL_NAMES: dict[str, str] = {
    "karox.repo.read_file": "repo.read_file",
    "karox.repo.read_lines": "repo.read_lines",
    "karox.repo.write_file": "repo.write_file",
    "karox.repo.edit_file": "repo.edit_file",
    "karox.repo.command": "repo.command",
    "karox.repo.list_files": "repo.list_files",
    "karox.repo.search": "repo.search",
    "karox.checks.run": "checks.run",
    "karox.tests.run": "tests.run",
    "karox.runtime.status": "runtime.status",
    "karox.git.status": "git.status",
    "karox.git.diff": "git.diff",
    "karox.git.log": "git.log",
    "karox.git.commit": "git.commit",
}

# Tools that are NOT Core commands but are still part of a hosted bridge
# contract: the stateful browser session, the managed dev server, and
# session-scoped artifact access.  They are served by ``HostedToolsRuntime`` and
# are validated against this set wherever a launch selects its tool bundle.
# The catalogue metadata (descriptions, input schemas, capabilities) lives in
# :mod:`karox.hosted_tools_runtime`; this is only the name universe used for
# allowlist validation, kept here to avoid an import cycle through that module.
HOSTED_EXTRA_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "karox.browser.command",
        "karox.browser.open",
        "karox.browser.tabs",
        "karox.browser.new_tab",
        "karox.browser.switch_tab",
        "karox.browser.close_tab",
        "karox.browser.snapshot",
        "karox.browser.click",
        "karox.browser.fill",
        "karox.browser.select",
        "karox.browser.press",
        "karox.browser.wait_for",
        "karox.browser.get_text",
        "karox.browser.screenshot",
        "karox.browser.console",
        "karox.browser.network_failures",
        "karox.browser.network_requests",
        "karox.browser.request_user_takeover",
        "karox.browser.resume_after_user_takeover",
        "karox.browser.close",
        "karox.dev_server.start",
        "karox.dev_server.status",
        "karox.dev_server.logs",
        "karox.dev_server.stop",
        "karox.artifact.get",
        "karox.artifact.read_image",
    }
)

KNOWN_HOSTED_TOOL_NAMES: frozenset[str] = frozenset(CORE_TOOL_NAMES) | HOSTED_EXTRA_TOOL_NAMES


# A hosted call's deadline is also the ceiling on how long checks.run may take,
# and no real repository verifies itself in thirty seconds.
DEFAULT_HOSTED_DEADLINE_SECONDS = 600.0

# Mutation leases are intentionally short and kept alive by a heartbeat while
# a hosted call is actually running. A disconnected client or killed worker can
# therefore block the next write only briefly instead of for the remote request
# deadline (ClickUp commonly supplies about fifteen minutes).
_MUTATION_LEASE_TTL_SECONDS = 60.0
_MUTATION_LEASE_HEARTBEAT_SECONDS = 20.0


class HostedToolRuntime(Protocol):
    def descriptors(self) -> list[ProxyToolDescriptor]: ...

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
    ) -> dict[str, Any]: ...


class CoreToolBridge:
    """Expose an explicit allowlist of built-in tools to one hosted session."""

    def __init__(
        self,
        repository: Path,
        sessions: SessionStore,
        session_id: str,
        allowed_tool_names: Sequence[str],
        *,
        hosted_origin: Optional[Origin] = None,
        audit_path: Optional[Path] = None,
        verification_commands: Optional[Iterable[Iterable[str]]] = None,
    ) -> None:
        if hosted_origin is None:
            hosted_origin = Origin(OriginKind.HOSTED_CLIENT, f"core-bridge-{session_id}")
        if hosted_origin.kind is not OriginKind.HOSTED_CLIENT:
            raise HostedBridgeAccessDenied("Core bridge origin must be a hosted client")
        if not allowed_tool_names:
            raise HostedBridgeAccessDenied("Core bridge tool allowlist must not be empty")
        if len(allowed_tool_names) != len(set(allowed_tool_names)):
            raise HostedBridgeAccessDenied("Core bridge tool allowlist contains duplicates")
        unknown = set(allowed_tool_names).difference(CORE_TOOL_NAMES)
        if unknown:
            raise HostedBridgeAccessDenied(
                f"unknown Core bridge tools: {sorted(unknown)}"
            )

        self.repository = repository.expanduser().resolve(strict=True)
        self.sessions = sessions
        self.session_id = session_id
        self.hosted_origin = hosted_origin
        self.audit_path = audit_path
        self._allowed = tuple(allowed_tool_names)
        self._verification_commands = (
            None
            if verification_commands is None
            else tuple(tuple(item) for item in verification_commands)
        )
        if (
            "karox.checks.run" in self._allowed
            and self._verification_commands is None
        ):
            raise HostedBridgeAccessDenied(
                "karox.checks.run requires user-approved verification commands"
            )

        record = sessions.load(session_id)
        sessions.validate_repository(record, self.repository)
        if record.revoked:
            raise HostedBridgeAccessDenied("session access has been revoked")
        from .models import AccessProfile

        self.policy = CapabilityPolicy(AccessProfile(record.access_profile))
        definitions = self._core().tools()
        by_name = {item.name: item for item in definitions}
        grants: set[Capability] = set()
        for public_name in self._allowed:
            definition = by_name[CORE_TOOL_NAMES[public_name]]
            grants.add(definition.capability)
            grants.update(definition.additional_capabilities)
        self.policy.set_grants(self.hosted_origin, grants)
        for capability in grants:
            if not self.policy.decide(self.hosted_origin, capability).allowed:
                raise HostedBridgeAccessDenied(
                    f"session profile does not allow {capability.value}"
                )

    def _core(self) -> CoreRuntime:
        return ExtendedCoreRuntime(
            self.repository,
            self.policy,
            self.sessions,
            self.audit_path,
            verification_commands=self._verification_commands,
        )

    def _definitions(self) -> dict[str, ToolDefinition]:
        return {item.name: item for item in self._core().tools()}

    def _record(self) -> None:
        record = self.sessions.load(self.session_id)
        self.sessions.validate_repository(record, self.repository)
        if record.revoked:
            raise HostedBridgeAccessDenied("session access has been revoked")

    def descriptors(self) -> list[ProxyToolDescriptor]:
        self._record()
        definitions = self._definitions()
        return [
            ProxyToolDescriptor(
                name=public_name,
                description=definitions[CORE_TOOL_NAMES[public_name]].description,
                input_schema=definitions[CORE_TOOL_NAMES[public_name]].input_schema,
                read_only=not definitions[CORE_TOOL_NAMES[public_name]].mutates,
            )
            for public_name in self._allowed
        ]

    def session_info(self) -> dict[str, Any]:
        self._record()
        record = self.sessions.load(self.session_id)
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
        core_name = CORE_TOOL_NAMES.get(tool_name)
        if core_name is None or tool_name not in self._allowed:
            raise HostedBridgeAccessDenied(f"Core tool is not exposed: {tool_name}")
        definition = self._definitions()[core_name]
        if definition.mutates and not idempotency_key:
            raise HostedBridgeAccessDenied(
                "mutating hosted calls require an idempotency key"
            )
        correlation = hashlib.sha256(
            f"{self.session_id}\0{tool_name}\0{idempotency_key or ''}".encode("utf-8")
        ).hexdigest()[:32]
        command = CoreCommand(
            name=core_name,
            arguments=dict(arguments),
            session_id=self.session_id,
            origin=self.hosted_origin,
            correlation_id=f"hosted-{correlation}",
            idempotency_key=idempotency_key,
            deadline_seconds=deadline_seconds,
        )
        lease = None
        heartbeat_stop: Optional[threading.Event] = None
        heartbeat_thread: Optional[threading.Thread] = None
        heartbeat_errors: list[Exception] = []
        if definition.mutates:
            lease = self.sessions.acquire(
                self.session_id,
                f"hosted-{os.getpid()}",
                ttl_seconds=_MUTATION_LEASE_TTL_SECONDS,
            )
            heartbeat_stop = threading.Event()

            def keep_lease_alive() -> None:
                assert lease is not None
                assert heartbeat_stop is not None
                while not heartbeat_stop.wait(_MUTATION_LEASE_HEARTBEAT_SECONDS):
                    try:
                        self.sessions.heartbeat(
                            lease, ttl_seconds=_MUTATION_LEASE_TTL_SECONDS
                        )
                    except Exception as exc:  # fail closed after the command returns
                        heartbeat_errors.append(exc)
                        return

            heartbeat_thread = threading.Thread(
                target=keep_lease_alive,
                name=f"karox-mutation-lease-{self.session_id}",
                daemon=True,
            )
            heartbeat_thread.start()
        try:
            result = self._core().execute(command, lease=lease).to_dict()
            if heartbeat_errors:
                raise HostedBridgeAccessDenied(
                    "mutation lease heartbeat failed; the write result is not trusted"
                )
            return result
        finally:
            if heartbeat_stop is not None:
                heartbeat_stop.set()
            if heartbeat_thread is not None:
                heartbeat_thread.join(timeout=2.0)
            if lease is not None:
                self.sessions.release(lease)


class CompositeHostedBridge:
    """Combine built-in Core and external MCP bridge runtimes."""

    def __init__(self, runtimes: Sequence[HostedToolRuntime]) -> None:
        if not runtimes:
            raise HostedBridgeAccessDenied("hosted bridge exposes no runtimes")
        self._runtimes = tuple(runtimes)

    def descriptors(self) -> list[ProxyToolDescriptor]:
        descriptors: list[ProxyToolDescriptor] = []
        owners: dict[str, HostedToolRuntime] = {}
        for runtime in self._runtimes:
            for descriptor in runtime.descriptors():
                if descriptor.name in owners:
                    raise HostedBridgeError(
                        f"duplicate hosted tool name: {descriptor.name}"
                    )
                owners[descriptor.name] = runtime
                descriptors.append(descriptor)
        return descriptors

    def _owner(self, tool_name: str) -> HostedToolRuntime:
        for runtime in self._runtimes:
            if any(item.name == tool_name for item in runtime.descriptors()):
                return runtime
        raise HostedBridgeAccessDenied(f"hosted tool is not exposed: {tool_name}")

    def session_info(self) -> dict[str, Any]:
        for runtime in self._runtimes:
            reader = getattr(runtime, "session_info", None)
            if callable(reader):
                return dict(reader())
        return {}

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
    ) -> dict[str, Any]:
        return self._owner(tool_name).execute(
            tool_name,
            arguments,
            idempotency_key=idempotency_key,
            deadline_seconds=deadline_seconds,
        )
