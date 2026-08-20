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
from typing import Any, Callable, Iterable, Optional, Protocol, Sequence

from .core import CoreRuntime, ToolDefinition
from .event_bus import EventBus, event_bus
from .risk_engine import RiskEngine, risk_engine
from .core_tools import ExtendedCoreRuntime
from .models import Capability, CoreCommand, Origin, OriginKind
from .policy import CapabilityPolicy
from .project_registry import ProjectRegistry, ProjectRegistryError
from .proxy import ProxyToolDescriptor
from .security import redact
from .sessions import (
    SessionError,
    SessionRecord,
    SessionStore,
    current_mutation_lease,
)
from .task_state import TaskStateStore


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
    "karox.command.run": "dev.command",
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
        "karox.browser.fill_credential",
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
        "karox.checks.start",
        "karox.checks.status",
        "karox.checks.logs",
        "karox.checks.cancel",
        "karox.runtime.restart",
        "karox.artifact.get",
        "karox.artifact.read_image",
    }
)

AUTONOMY_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "karox.task.bootstrap",
        "karox.task.checkpoint",
        "karox.task.resume",
        "karox.task.status",
        "karox.task.workstreams",
        "karox.repo.inspect",
        "karox.task.execute_plan",
        "karox.checks.run_affected",
        # Universal memory: user/project/workstream/session scoped local
        # knowledge, served to every client of this runtime through one API.
        "karox.memory.remember",
        "karox.memory.recall",
        "karox.memory.context",
        "karox.memory.list",
        "karox.memory.forget",
    }
)

KNOWN_HOSTED_TOOL_NAMES: frozenset[str] = (
    frozenset(CORE_TOOL_NAMES) | HOSTED_EXTRA_TOOL_NAMES | AUTONOMY_TOOL_NAMES
)


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
        risk: Optional[RiskEngine] = None,
        events: Optional[EventBus] = None,
        advertise_unavailable: bool = False,
        project_registry: Optional[ProjectRegistry] = None,
        project_registry_loader: Optional[Callable[[], ProjectRegistry]] = None,
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
        try:
            self.project_registry = project_registry or ProjectRegistry.single(self.repository)
            default_project = self.project_registry.default
            anchor_project = self.project_registry.entry_for_path(self.repository)
        except ProjectRegistryError as exc:
            raise HostedBridgeAccessDenied(f"invalid project registry: {exc}") from exc
        if default_project is None or anchor_project is None:
            raise HostedBridgeAccessDenied("bridge session anchor must be an approved project")
        self.sessions = sessions
        self.session_id = session_id
        self.task_states = TaskStateStore(sessions)
        self._project_registry_loader = project_registry_loader
        self._runtime_cache: dict[str, ExtendedCoreRuntime] = {}
        self._runtime_lock = threading.Lock()
        self.hosted_origin = hosted_origin
        self.audit_path = audit_path
        self._allowed = tuple(allowed_tool_names)
        # A hosted client is the most remote agent source there is, so it gets
        # the same Smart Stop as a local one. Passing ``risk=None`` explicitly
        # is how a caller opts out; omitting it uses the process-wide engine.
        self._risk = risk_engine() if risk is None else risk
        self._events = event_bus() if events is None else events
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
        # Core tool definitions and handlers are immutable for the lifetime of a
        # hosted bridge session. Rebuilding ExtendedCoreRuntime for every
        # descriptor lookup and every execute call adds repeated setup work while
        # providing no additional security; per-call session/repository checks
        # still happen in _record() and CoreRuntime.execute().
        self._runtime = ExtendedCoreRuntime(
            self.repository,
            self.policy,
            self.sessions,
            self.audit_path,
            verification_commands=self._verification_commands,
            risk=self._risk,
            events=self._events,
            session_repository_validator=self._validate_project_session,
        )
        self._runtime_cache[anchor_project.project_id] = self._runtime
        self._definition_cache = {item.name: item for item in self._runtime.tools()}
        by_name = self._definition_cache
        grants: set[Capability] = set()
        for public_name in self._allowed:
            definition = by_name[CORE_TOOL_NAMES[public_name]]
            grants.add(definition.capability)
            grants.update(definition.additional_capabilities)
        self.policy.set_grants(self.hosted_origin, grants)
        for capability in grants:
            if not self.policy.decide(self.hosted_origin, capability).allowed:
                if advertise_unavailable:
                    # Stable-catalogue mode: descriptors remain visible so a
                    # client that caches tools/list does not need to reconnect
                    # when permissions are later elevated. CoreRuntime.execute()
                    # still performs the authoritative per-call policy check.
                    continue
                raise HostedBridgeAccessDenied(
                    f"session profile does not allow {capability.value}"
                )

    def _current_project_registry(self) -> ProjectRegistry:
        loader = self._project_registry_loader
        if loader is None:
            return self.project_registry
        try:
            registry = loader()
            anchor = registry.entry_for_path(self.repository)
        except (ProjectRegistryError, OSError, RuntimeError, ValueError) as exc:
            raise HostedBridgeAccessDenied(
                f"cannot refresh saved project registry: {exc}"
            ) from exc
        if anchor is None:
            raise HostedBridgeAccessDenied(
                "saved project registry no longer contains the durable session anchor"
            )
        self.project_registry = registry
        return registry

    def _validate_project_session(self, record: SessionRecord, repository: Path) -> None:
        # The durable session remains bound to the saved profile's default repo;
        # that identity/fingerprint check must stay intact even when a call is
        # routed to another user-approved project.
        self.sessions.validate_repository(record, self.repository)
        try:
            approved = self._current_project_registry().entry_for_path(repository)
        except ProjectRegistryError as exc:
            raise SessionError(f"cannot resolve approved project: {exc}") from exc
        if approved is None:
            raise SessionError("repository is not approved by this saved profile")

    def _runtime_for(self, project_id: str) -> ExtendedCoreRuntime:
        try:
            entry = self._current_project_registry().get(project_id)
        except ProjectRegistryError as exc:
            raise HostedBridgeAccessDenied(str(exc)) from exc
        with self._runtime_lock:
            cached = self._runtime_cache.get(entry.project_id)
            if cached is not None:
                return cached
            runtime = ExtendedCoreRuntime(
                Path(entry.path),
                self.policy,
                self.sessions,
                self.audit_path,
                verification_commands=self._verification_commands,
                risk=self._risk,
                events=self._events,
                session_repository_validator=self._validate_project_session,
            )
            self._runtime_cache[entry.project_id] = runtime
            return runtime

    def _project_for_arguments(self, arguments: dict[str, Any]) -> tuple[str, Optional[str]]:
        raw_workstream = arguments.pop("workstream_id", None)
        registry = self._current_project_registry()
        default = registry.default
        if default is None:
            raise HostedBridgeAccessDenied("bridge has no default project")
        if raw_workstream is None:
            state = self.task_states.load_optional(self.session_id)
            if state is None:
                return default.project_id, None
            workstream: Optional[str] = None
        else:
            if not isinstance(raw_workstream, str):
                raise HostedBridgeAccessDenied("workstream_id must be a string")
            workstream = raw_workstream.strip()
            try:
                state = self.task_states.load_optional(
                    self.session_id,
                    workstream_id=workstream,
                )
            except SessionError as exc:
                raise HostedBridgeAccessDenied(str(exc)) from exc
            if state is None:
                raise HostedBridgeAccessDenied(
                    "workstream is not initialized; call karox.task.bootstrap first"
                )
        project_fact = state.facts.get("project_id")
        if project_fact is None:
            # Legacy task states predate project_id. They were created while the
            # durable session had exactly one repository, so keep them on that
            # anchor even if the user later changes the logical default project.
            anchor = registry.entry_for_path(self.repository)
            if anchor is None:
                raise HostedBridgeAccessDenied("session anchor is no longer approved")
            return anchor.project_id, workstream
        try:
            entry = registry.get(str(project_fact.value))
        except ProjectRegistryError as exc:
            raise HostedBridgeAccessDenied(
                "workstream is bound to a project no longer approved by this profile"
            ) from exc
        return entry.project_id, workstream

    def _core(self, project_id: Optional[str] = None) -> CoreRuntime:
        if project_id is None:
            return self._runtime
        return self._runtime_for(project_id)

    def _definitions(self) -> dict[str, ToolDefinition]:
        return self._definition_cache

    def _record(self) -> SessionRecord:
        record = self.sessions.load(self.session_id)
        self.sessions.validate_repository(record, self.repository)
        if record.revoked:
            raise HostedBridgeAccessDenied("session access has been revoked")
        return record

    def descriptors(self) -> list[ProxyToolDescriptor]:
        self._record()
        definitions = self._definitions()
        descriptors: list[ProxyToolDescriptor] = []
        for public_name in self._allowed:
            definition = definitions[CORE_TOOL_NAMES[public_name]]
            schema = dict(definition.input_schema)
            properties = dict(schema.get("properties", {}))
            properties["workstream_id"] = {
                "type": "string",
                "minLength": 1,
                "maxLength": 64,
                "description": "Route this call to the project bound to an initialized workstream.",
            }
            schema["properties"] = properties
            descriptors.append(
                ProxyToolDescriptor(
                    name=public_name,
                    description=definition.description,
                    input_schema=schema,
                    read_only=not definition.mutates,
                )
            )
        return descriptors

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
        # CoreRuntime.execute() is the authoritative per-call session boundary:
        # it reloads the session, validates the repository binding, revocation,
        # and access profile before dispatching any handler. Repeating _record()
        # here performed the same disk/fingerprint work twice for every hosted
        # Core call without adding a second safety boundary.
        core_name = CORE_TOOL_NAMES.get(tool_name)
        if core_name is None or tool_name not in self._allowed:
            raise HostedBridgeAccessDenied(f"Core tool is not exposed: {tool_name}")
        call_arguments = dict(arguments)
        project_id, workstream_id = self._project_for_arguments(call_arguments)
        definition = self._definitions()[core_name]
        if definition.mutates and not idempotency_key:
            raise HostedBridgeAccessDenied(
                "mutating hosted calls require an idempotency key"
            )
        correlation = hashlib.sha256(
            (
                f"{self.session_id}\0{project_id}\0{workstream_id or 'default'}\0"
                f"{tool_name}\0{idempotency_key or ''}"
            ).encode("utf-8")
        ).hexdigest()[:32]
        command = CoreCommand(
            name=core_name,
            arguments=call_arguments,
            session_id=self.session_id,
            origin=self.hosted_origin,
            correlation_id=f"hosted-{correlation}",
            idempotency_key=idempotency_key,
            deadline_seconds=deadline_seconds,
        )
        lease = None
        owns_lease = False
        heartbeat_stop: Optional[threading.Event] = None
        heartbeat_thread: Optional[threading.Thread] = None
        heartbeat_errors: list[Exception] = []
        if definition.mutates:
            try:
                inherited = current_mutation_lease(self.session_id)
                if inherited is not None:
                    # High-level autonomy operations may synchronously delegate
                    # back into Core while they already hold this same session's
                    # mutation fence. Reuse only the ContextVar-scoped lease;
                    # unrelated threads/tasks cannot observe it and still get
                    # SessionBusy from SessionStore.acquire().
                    self.sessions.validate_lease(inherited)
                    lease = inherited
                else:
                    lease = self.sessions.acquire(
                        self.session_id,
                        f"hosted-{os.getpid()}",
                        ttl_seconds=_MUTATION_LEASE_TTL_SECONDS,
                    )
                    owns_lease = True
            except SessionError as exc:
                if "revoked" in str(exc).lower():
                    raise HostedBridgeAccessDenied("session access has been revoked") from exc
                raise
            if owns_lease:
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
            try:
                result = self._core(project_id).execute(command, lease=lease).to_dict()
            except SessionError as exc:
                if "revoked" in str(exc).lower():
                    raise HostedBridgeAccessDenied("session access has been revoked") from exc
                raise
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
            if lease is not None and owns_lease:
                self.sessions.release(lease)


class CompositeHostedBridge:
    """Combine built-in Core and external MCP bridge runtimes."""

    def __init__(self, runtimes: Sequence[HostedToolRuntime]) -> None:
        if not runtimes:
            raise HostedBridgeAccessDenied("hosted bridge exposes no runtimes")
        self._runtimes = tuple(runtimes)
        # Tool ownership is immutable for the lifetime of a bridge: each runtime
        # receives a fixed allowlist at construction. Build the routing table once
        # so every tools/call does not perform a second descriptors() scan merely
        # to rediscover the owner. Runtime.execute() still performs its own live
        # session/revocation/policy checks on every actual tool invocation.
        owners: dict[str, HostedToolRuntime] = {}
        for runtime in self._runtimes:
            for descriptor in runtime.descriptors():
                if descriptor.name in owners:
                    raise HostedBridgeError(
                        f"duplicate hosted tool name: {descriptor.name}"
                    )
                owners[descriptor.name] = runtime
        self._owners = owners

    def descriptors(self) -> list[ProxyToolDescriptor]:
        descriptors: list[ProxyToolDescriptor] = []
        seen: set[str] = set()
        for runtime in self._runtimes:
            for descriptor in runtime.descriptors():
                if descriptor.name in seen:
                    raise HostedBridgeError(
                        f"duplicate hosted tool name: {descriptor.name}"
                    )
                seen.add(descriptor.name)
                descriptors.append(descriptor)
        return descriptors

    def _owner(self, tool_name: str) -> HostedToolRuntime:
        owner = self._owners.get(tool_name)
        if owner is not None:
            return owner
        raise HostedBridgeAccessDenied(f"hosted tool is not exposed: {tool_name}")

    def session_info(self) -> dict[str, Any]:
        for runtime in self._runtimes:
            reader = getattr(runtime, "session_info", None)
            if callable(reader):
                return dict(reader())
        return {}

    def close(self) -> None:
        """Release optional runtime-owned resources without changing tool semantics."""
        for runtime in self._runtimes:
            closer = getattr(runtime, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    # Shutdown cleanup is best-effort; a failed optional closer
                    # must not prevent other runtimes from releasing resources.
                    pass

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
