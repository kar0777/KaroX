"""Hosted-client access to explicitly selected KaroX Core tools.

The external MCP proxy in :mod:`karox.proxy` exposes selected third-party MCP
tools.  This module covers the other half of the bridge contract: a hosted
client can call KaroX's own repository/check/Git tools through the same Core
Runtime boundary used by the native agent.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Protocol, Sequence

from .action_execution import CapabilityCoreRuntime
from .action_policy import ActionConfirmationRequired, ActionDecisionEngine
from .core import CoreRuntime, ToolDefinition
from .disk_maintenance import is_drive_root
from .event_bus import EventBus, event_bus
from .risk_engine import RiskEngine, risk_engine
from .models import AccessProfile, Capability, CoreCommand, Origin, OriginKind
from .paths import runtime_dir
from .policy import CapabilityPolicy, capability_requires_explicit_approval
from .risk_mapping import action_for_command
from .route_health import (
    DEFAULT_ROUTE_FAILURE_PROBE_INTERVAL_SECONDS,
    DEFAULT_ROUTE_FAILURE_THRESHOLD,
    DEFAULT_ROUTE_PROBE_INTERVAL_SECONDS,
)
from .project_registry import ProjectRegistry, ProjectRegistryError
from .proxy import ProxyToolDescriptor
from .repository_lease import (
    DEFAULT_REPOSITORY_LEASE_TTL_SECONDS,
    RepositoryLease,
    RepositoryLeaseConflict,
    RepositoryLeaseError,
    RepositoryLeaseStore,
    current_repository_lease,
)
from .security import redact
from .sessions import (
    SessionBusy,
    SessionError,
    SessionRecord,
    SessionStore,
    current_mutation_lease,
    mutation_lease_context,
)
from .task_state import FactOrigin, TaskFact, TaskStateStore
from .verification import discover_verification_commands


class HostedBridgeError(RuntimeError):
    pass


class HostedBridgeAccessDenied(HostedBridgeError, PermissionError):
    pass


class HostedApprovalRequired(HostedBridgeAccessDenied):
    """Exact-action user confirmation required before a hosted call may run."""

    def __init__(
        self,
        *,
        tool_name: str,
        action_digest: str,
        action_kind: str,
        risk: str,
        consequence: str,
        preview: Optional[dict[str, Any]] = None,
        message: Optional[str] = None,
    ) -> None:
        super().__init__(message or f"KaroX requires user approval for {action_kind}")
        self.tool_name = tool_name
        self.action_digest = action_digest
        self.action_kind = action_kind
        self.risk = risk
        self.consequence = consequence
        self.preview = dict(preview or {})
        self.message = message or f"KaroX requires user approval for {action_kind}."

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "tool_name": self.tool_name,
            "action_digest": self.action_digest,
            "action_kind": self.action_kind,
            "risk": self.risk,
            "consequence": self.consequence,
            "preview": dict(self.preview),
            "message": self.message,
            "continue_independent_work": True,
            "defer_until_blocked": True,
        }


def _hosted_bridge_health(session_id: str) -> dict[str, Any]:
    """Return safe public-edge diagnostics for this hosted bridge session."""
    root = runtime_dir() / "web-bridge"
    candidates: list[tuple[float, dict[str, Any]]] = []
    try:
        paths = tuple(root.glob("*.json"))
    except OSError:
        paths = ()
    for path in paths:
        if path.name.endswith(".last-exit.json"):
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("session_id") != session_id:
                continue
            candidates.append((path.stat().st_mtime, payload))
        except (OSError, json.JSONDecodeError, ValueError):
            continue
    if not candidates:
        return {
            "available": False,
            "state": "watchdog_unavailable",
            "recommended_action": "continue_local_diagnostics",
        }

    _mtime, record = max(candidates, key=lambda item: item[0])
    now = time.time()
    readiness = str(record.get("readiness_state") or "UNKNOWN")
    healthy = record.get("public_route_healthy")
    failures_raw = record.get("public_route_failures")
    failures = int(failures_raw) if isinstance(failures_raw, int) and failures_raw >= 0 else 0

    if readiness != "READY":
        state = "bridge_not_ready"
        action = "wait_for_bridge_ready"
    elif healthy is True and failures == 0:
        state = "healthy"
        action = "none"
    elif failures <= 1:
        state = "confirming_public_failure"
        action = "wait_for_fast_confirmation"
    else:
        state = "public_recovery_due"
        action = "supervisor_recovery_in_progress"

    def age(value: Any) -> Optional[float]:
        if isinstance(value, (int, float)):
            return round(max(0.0, now - float(value)), 3)
        return None

    return {
        "available": True,
        "state": state,
        "recommended_action": action,
        "saved_profile": record.get("saved_profile"),
        "owner_pid": record.get("owner_pid"),
        "bridge_pid": record.get("bridge_pid"),
        "readiness_state": readiness,
        "tunnel": record.get("tunnel"),
        "public_route_healthy": healthy,
        "public_route_failures": failures,
        "public_route_check_age_seconds": age(record.get("public_route_checked_at")),
        "last_public_route_ok_age_seconds": age(record.get("last_public_route_ok_at")),
        "local_bridge_healthy": record.get("local_bridge_healthy"),
        "local_bridge_check_age_seconds": age(record.get("local_bridge_checked_at")),
        "tunnel_recoveries": int(record.get("tunnel_recoveries") or 0),
        "last_tunnel_recovery_age_seconds": age(record.get("last_tunnel_recovery_at")),
        "last_tunnel_recovery_error": record.get("last_tunnel_recovery_error"),
        "probe_policy": {
            "healthy_interval_seconds": DEFAULT_ROUTE_PROBE_INTERVAL_SECONDS,
            "failure_confirmation_interval_seconds": DEFAULT_ROUTE_FAILURE_PROBE_INTERVAL_SECONDS,
            "failure_threshold": DEFAULT_ROUTE_FAILURE_THRESHOLD,
        },
    }


CORE_TOOL_NAMES: dict[str, str] = {
    "karox.repo.read_file": "repo.read_file",
    "karox.repo.read_lines": "repo.read_lines",
    "karox.repo.write_file": "repo.write_file",
    "karox.repo.edit_file": "repo.edit_file",
    "karox.repo.command": "repo.command",
    "karox.command.run": "dev.command",
    "karox.repo.list_files": "repo.list_files",
    "karox.repo.search": "repo.search",
    "karox.lsp.diagnostics": "lsp.diagnostics",
    "karox.checks.run": "checks.run",
    "karox.tests.run": "tests.run",
    "karox.runtime.status": "runtime.status",
    "karox.git.status": "git.status",
    "karox.git.diff": "git.diff",
    "karox.git.log": "git.log",
    "karox.git.commit": "git.commit",
    "karox.git.push": "git.push",
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
        "karox.dev_server.restart",
        "karox.checks.start",
        "karox.checks.status",
        "karox.checks.logs",
        "karox.checks.cancel",
        "karox.command.start",
        "karox.command.status",
        "karox.command.logs",
        "karox.command.cancel",
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
        # Read-only KaroX 5 orchestration discovery/planning. These tools expose
        # secret-free inventory and plans, but never start workers or widen the
        # session's low-level repository capabilities.
        "karox.intelligence.list",
        "karox.orchestrate.recipes",
        "karox.orchestrate.plan",
        "karox.orchestrate.start",
        "karox.orchestrate.status",
        "karox.orchestrate.control",
        # Universal memory: user/project/workstream/session scoped local
        # knowledge, served to every client of this runtime through one API.
        "karox.memory.remember",
        "karox.memory.recall",
        "karox.memory.context",
        "karox.memory.list",
        "karox.memory.forget",
        # Stable ChatGPT Project scope marker and compact continuation capsule.
        # ChatGPT does not currently send a trusted Project ID over MCP, so the
        # binding is an explicit project-instruction marker rather than auth.
        "karox.chatgpt_project.bind",
        "karox.chatgpt_project.resume",
    }
)

KNOWN_HOSTED_TOOL_NAMES: frozenset[str] = (
    frozenset(CORE_TOOL_NAMES) | HOSTED_EXTRA_TOOL_NAMES | AUTONOMY_TOOL_NAMES
)


# A hosted call's deadline is also the ceiling on how long checks.run may take,
# and no real repository verifies itself in thirty seconds.
DEFAULT_HOSTED_DEADLINE_SECONDS = 600.0

# Chat-native approval is an opt-in compatibility path for hosted clients that
# cannot carry MCP elicitation. The user's explicit yes/no stays in the chat;
# the agent records only an exact, short-lived push grant in the durable
# workstream. KaroX binds it to the current HEAD + remote + branch and consumes
# it atomically before execution. Browser/protocol approval remains available
# for users who want a model-independent confirmation channel.
_CHAT_APPROVAL_FACT = "chat_user_approval"
_CHAT_APPROVAL_MAX_AGE_SECONDS = 300.0
_CHAT_APPROVAL_EVIDENCE = "user.chat.explicit_approval"

# Mutation leases are intentionally short and kept alive by a heartbeat while
# a hosted call is actually running. A disconnected client or killed worker can
# therefore block the next write only briefly instead of for the remote request
# deadline (ClickUp commonly supplies about fifteen minutes).
_MUTATION_LEASE_TTL_SECONDS = 60.0
_MUTATION_LEASE_HEARTBEAT_SECONDS = 20.0
# Parallel hosted agents frequently collide for only a few milliseconds while a
# sibling records its durable mutation result. Treat that as queueing, not as a
# task failure. The wait is bounded so a genuinely long-running/stuck mutation
# cannot consume every bridge worker indefinitely.
_MUTATION_LEASE_WAIT_MAX_SECONDS = 45.0
_MUTATION_LEASE_RETRY_SECONDS = 0.15
_REPOSITORY_LEASE_WAIT_MAX_SECONDS = 3.0
_REPOSITORY_LEASE_HEARTBEAT_SECONDS = 20.0


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
        bypass_mode: bool = False,
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
        self.repository_leases = RepositoryLeaseStore()
        self._project_registry_loader = project_registry_loader
        self._runtime_cache: dict[str, CapabilityCoreRuntime] = {}
        self._runtime_lock = threading.Lock()
        self.hosted_origin = hosted_origin
        self.audit_path = audit_path
        self._allowed = tuple(allowed_tool_names)
        # A hosted client is the most remote agent source there is, so it gets
        # the same Smart Stop as a local one. Passing ``risk=None`` explicitly
        # is how a caller opts out; omitting it uses the process-wide engine.
        self._risk = risk_engine() if risk is None else risk
        self._action_decisions = ActionDecisionEngine(self._risk)
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
        # Advanced/elevated capability is not destructive autonomy. Only the
        # explicit saved-profile Bypass switch reaches this flag.
        self._bypass_mode = bool(bypass_mode)
        self.policy = CapabilityPolicy(AccessProfile(record.access_profile))
        # Core tool definitions and handlers are immutable for the lifetime of a
        # hosted bridge session. Keep one capability runtime per approved project
        # so hosted clients use the same consequence-based ActionDecisionEngine
        # as native agents instead of a second Smart Stop policy. Per-call
        # session/repository/capability checks still happen in CoreRuntime.execute().
        self._runtime = CapabilityCoreRuntime(
            self.repository,
            self.policy,
            self.sessions,
            self.audit_path,
            verification_commands=self._verification_commands_for(self.repository),
            risk=self._risk,
            action_decisions=self._action_decisions,
            bypass_mode=self._bypass_mode,
            checkpoint_factory=self._checkpoint_factory_for(self.repository),
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
            if capability_requires_explicit_approval(capability):
                # Explicit capabilities are intentionally absent from every
                # standing profile. Their tool may still be advertised because
                # the modern MCP approval round mints a one-shot token for the
                # exact action; execution without that token remains denied.
                continue
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

    def _verification_commands_for(
        self, path: Path
    ) -> Optional[tuple[tuple[str, ...], ...]]:
        """Approved verification argv for one project, not for the whole bridge.

        The saved profile's allowlist is written for the anchor repository, so a
        Python bridge anchored on KaroX handed ``pytest`` to a Node project and
        refused ``npm test`` there -- checks were unusable in every project but
        one.  Each project additionally approves what its own manifest actually
        declares, discovered by the same conservative rules the TUI uses when
        the user adds the project.
        """

        if self._verification_commands is None:
            return None
        try:
            discovered = discover_verification_commands(path)
        except (OSError, ValueError):
            discovered = ()
        return tuple(dict.fromkeys((*self._verification_commands, *discovered)))

    def _checkpoint_factory_for(self, repository: Path):
        """On-demand rollback checkpoint hook for one approved project.

        Hosted callers have no pre-turn TUI checkpoint, so every repository
        deletion used to fall to CONFIRM and surfaced as an opaque deny. The
        same Git-aware workspace checkpoint the TUI uses lets the decision
        engine lower a repository-scoped deletion to GUARDED_AUTO with a real
        rollback guarantee instead of refusing to act.
        """

        def create_checkpoint() -> Optional[str]:
            from .ellipsis_checkpoint import WorkspaceCheckpointStore
            from .paths import runtime_dir

            checkpoint = WorkspaceCheckpointStore(
                runtime_dir() / "vnext" / "checkpoints"
            ).create(
                repository,
                session_id=self.session_id,
                sessions=self.sessions,
            )
            return checkpoint.checkpoint_id

        return create_checkpoint

    def _runtime_for(self, project_id: str) -> CapabilityCoreRuntime:
        try:
            entry = self._current_project_registry().get(project_id)
        except ProjectRegistryError as exc:
            raise HostedBridgeAccessDenied(str(exc)) from exc
        with self._runtime_lock:
            cached = self._runtime_cache.get(entry.project_id)
            if cached is not None:
                return cached
            runtime = CapabilityCoreRuntime(
                Path(entry.path),
                self.policy,
                self.sessions,
                self.audit_path,
                verification_commands=self._verification_commands_for(Path(entry.path)),
                risk=self._risk,
                action_decisions=self._action_decisions,
                bypass_mode=self._bypass_mode,
                checkpoint_factory=self._checkpoint_factory_for(Path(entry.path)),
                events=self._events,
                session_repository_validator=self._validate_project_session,
            )
            self._runtime_cache[entry.project_id] = runtime
            return runtime

    def _project_for_arguments(
        self, arguments: dict[str, Any]
    ) -> tuple[str, Optional[str], str]:
        raw_workstream = arguments.pop("workstream_id", None)
        registry = self._current_project_registry()
        default = registry.default
        if default is None:
            raise HostedBridgeAccessDenied("bridge has no default project")
        if raw_workstream is None:
            state = self.task_states.load_optional(self.session_id)
            if state is None:
                # CapabilityCoreRuntime will fall back to SessionRecord.task.
                return default.project_id, None, ""
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
        objective = state.facts.get("objective")
        user_intent = ""
        if objective is not None:
            user_intent = str(objective.value or "").strip()[:8000]
        project_fact = state.facts.get("project_id")
        if project_fact is None:
            # Legacy task states predate project_id. They were created while the
            # durable session had exactly one repository, so keep them on that
            # anchor even if the user later changes the logical default project.
            anchor = registry.entry_for_path(self.repository)
            if anchor is None:
                raise HostedBridgeAccessDenied("session anchor is no longer approved")
            return anchor.project_id, workstream, user_intent
        try:
            entry = registry.get(str(project_fact.value))
        except ProjectRegistryError as exc:
            raise HostedBridgeAccessDenied(
                "workstream is bound to a project no longer approved by this profile"
            ) from exc
        return entry.project_id, workstream, user_intent

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

    def _approval_decision(self, command: CoreCommand, repository: Path):
        action = action_for_command(
            command,
            repository=repository,
            reversible_by_checkpoint=False,
        )
        return self._action_decisions.decide(
            action,
            user_intent=command.user_intent,
            bypass_mode=self._bypass_mode,
        )

    def _consume_chat_user_approval(
        self,
        *,
        command: CoreCommand,
        repository: Path,
        workstream_id: Optional[str],
        decision: Any,
    ) -> bool:
        """Consume one exact user approval relayed from the current chat.

        Some hosted ChatGPT sessions cannot carry MCP elicitation.  In that
        environment the user may explicitly answer yes/no in the chat instead.
        The agent records that answer as ``chat_user_approval`` in the active
        workstream.  This compatibility path is deliberately narrow: only
        ``git.push`` is accepted, the fact must be fresh and explicitly marked
        as a user-chat report, and the current HEAD/remote/branch must still
        match.  The fact is atomically replaced with a verified consumed record
        before a confirmation token is issued, so one chat approval cannot be
        replayed for a second push.

        This is an opt-in convenience boundary, not a model-independent proof of
        user presence.  Protocol elicitation/browser approval remains stronger
        because the model cannot manufacture those signals by itself.
        """
        if command.name != "git.push":
            return False
        state = self.task_states.load_optional(
            self.session_id,
            workstream_id=workstream_id,
        )
        if state is None:
            return False
        approval = state.facts.get(_CHAT_APPROVAL_FACT)
        if approval is None or approval.origin is not FactOrigin.REPORTED_BY_AGENT:
            return False
        if tuple(approval.evidence) != (_CHAT_APPROVAL_EVIDENCE,):
            return False
        age = time.time() - float(approval.recorded_at)
        if age < -30.0 or age > _CHAT_APPROVAL_MAX_AGE_SECONDS:
            return False
        value = approval.value
        if not isinstance(value, Mapping) or value.get("approved") is not True:
            return False

        action = action_for_command(
            command,
            repository=repository,
            reversible_by_checkpoint=False,
        )
        expected = {
            "action_kind": "git.push",
            "remote": command.arguments.get("remote"),
            "branch": command.arguments.get("branch"),
            "head": action.details.get("head"),
        }
        if any(value.get(key) != expected_value for key, expected_value in expected.items()):
            return False

        consumed = dict(value)
        consumed.update(
            {
                "approved": False,
                "consumed": True,
                "action_digest": decision.action_digest,
            }
        )
        try:
            self.task_states.checkpoint(
                self.session_id,
                {
                    _CHAT_APPROVAL_FACT: TaskFact(
                        consumed,
                        FactOrigin.VERIFIED,
                        evidence=("hosted.chat_approval.consume",),
                    )
                },
                expected_revision=state.revision,
                workstream_id=workstream_id,
            )
        except SessionError:
            return False
        return True

    @staticmethod
    def _approval_error(
        tool_name: str, command: CoreCommand, decision: Any
    ) -> HostedApprovalRequired:
        preview = dict(decision.impact)
        if command.name == "git.push":
            preview.update(
                {
                    "remote": command.arguments.get("remote"),
                    "branch": command.arguments.get("branch"),
                }
            )
            message = (
                "Allow one Git push of the current HEAD to "
                f"{command.arguments.get('remote')}/{command.arguments.get('branch')}? "
                "KaroX will not force-push, change remotes, or authenticate."
            )
        else:
            message = (
                f"Allow this one {decision.assessment.kind} action? "
                "The approval is bound to the exact action digest and cannot be replayed."
            )
        return HostedApprovalRequired(
            tool_name=tool_name,
            action_digest=decision.action_digest,
            action_kind=decision.assessment.kind,
            risk=decision.assessment.level.value,
            consequence=decision.consequence.value,
            preview=preview,
            message=message,
        )

    def execute_approved(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        expected_action_digest: str,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
    ) -> dict[str, Any]:
        """Execute one exact action after a trusted protocol-level user approval."""

        if not isinstance(expected_action_digest, str) or not expected_action_digest:
            raise HostedBridgeAccessDenied("approved action digest is missing")
        return self.execute(
            tool_name,
            arguments,
            idempotency_key=idempotency_key,
            deadline_seconds=deadline_seconds,
            _approved_action_digest=expected_action_digest,
        )

    @staticmethod
    def _cached_catalog_push_compat(arguments: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        """Translate one legacy ``command.run`` push into the guarded push tool.

        Durable ChatGPT connections can cache a pre-upgrade tool catalogue. The
        dedicated ``karox.git.push`` tool may therefore be implemented by the
        freshly restarted child before the client knows its name. Preserve the
        safety boundary by accepting only the unambiguous no-flag form
        ``git push <remote> <branch>`` and routing it to the *same* dedicated
        Core command/approval path. Every other Git mutation still reaches the
        developer-command guard and is refused.
        """

        argv = arguments.get("argv")
        if not isinstance(argv, list) or len(argv) != 4 or not all(
            isinstance(item, str) for item in argv
        ):
            return None
        executable = argv[0].strip().replace("\\", "/").rsplit("/", 1)[-1].casefold()
        for suffix in (".exe", ".cmd", ".bat", ".com"):
            if executable.endswith(suffix):
                executable = executable[: -len(suffix)]
                break
        if executable != "git" or argv[1].strip().casefold() != "push":
            return None
        remote = argv[2].strip()
        branch = argv[3].strip()
        if not remote or not branch:
            return None
        translated: dict[str, Any] = {"remote": remote, "branch": branch}
        workstream = arguments.get("workstream_id")
        if isinstance(workstream, str) and workstream:
            translated["workstream_id"] = workstream
        return translated

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
        _approved_action_digest: Optional[str] = None,
    ) -> dict[str, Any]:
        # A cached ChatGPT catalogue may not know the new guarded push tool name
        # yet. Route exactly one safe compatibility spelling through that tool;
        # never relax command_guard or let arbitrary Git argv through.
        if tool_name == "karox.command.run" and "karox.git.push" in self._allowed:
            translated_push = self._cached_catalog_push_compat(arguments)
            if translated_push is not None:
                return self.execute(
                    "karox.git.push",
                    translated_push,
                    idempotency_key=idempotency_key,
                    deadline_seconds=deadline_seconds,
                    _approved_action_digest=_approved_action_digest,
                )

        # CoreRuntime.execute() is the authoritative per-call session boundary:
        # it reloads the session, validates the repository binding, revocation,
        # and access profile before dispatching any handler. Repeating _record()
        # here performed the same disk/fingerprint work twice for every hosted
        # Core call without adding a second safety boundary.
        core_name = CORE_TOOL_NAMES.get(tool_name)
        if core_name is None or tool_name not in self._allowed:
            raise HostedBridgeAccessDenied(f"Core tool is not exposed: {tool_name}")
        call_arguments = dict(arguments)
        project_id, workstream_id, user_intent = self._project_for_arguments(call_arguments)
        try:
            project_entry = self._current_project_registry().get(project_id)
        except ProjectRegistryError as exc:
            raise HostedBridgeAccessDenied(str(exc)) from exc
        if is_drive_root(project_entry.path) and core_name.startswith("git."):
            raise HostedBridgeAccessDenied(
                "Git metadata is not applicable to a whole-drive project root; "
                "filesystem and approved command tools remain available inside the "
                "explicitly allowed drive boundary"
            )
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
            user_intent=user_intent,
        )
        runtime = self._core(project_id)
        approval_token: Optional[str] = None
        explicit_capabilities = tuple(
            capability
            for capability in (definition.capability, *definition.additional_capabilities)
            if capability_requires_explicit_approval(capability)
        )
        if _approved_action_digest is not None or explicit_capabilities:
            project_repository = Path(project_entry.path)
            decision = self._approval_decision(command, project_repository)
            if _approved_action_digest is None:
                if self._consume_chat_user_approval(
                    command=command,
                    repository=project_repository,
                    workstream_id=workstream_id,
                    decision=decision,
                ):
                    _approved_action_digest = decision.action_digest
                else:
                    raise self._approval_error(tool_name, command, decision)
            if decision.action_digest != _approved_action_digest:
                raise HostedBridgeAccessDenied(
                    "approved action no longer matches the current tool call"
                )
            if not decision.requires_confirmation and not explicit_capabilities:
                raise HostedBridgeAccessDenied(
                    "approval retry does not correspond to an approval-gated action"
                )
            grant = self._risk.ledger.issue(decision.assessment)
            approval_token = grant.token
            if explicit_capabilities:
                self.policy.add_token(
                    approval_token,
                    self.hosted_origin,
                    explicit_capabilities,
                    ttl_seconds=300.0,
                )
            command = replace(command, confirmation_token=approval_token)
        lease = None
        owns_lease = False
        heartbeat_stop: Optional[threading.Event] = None
        heartbeat_thread: Optional[threading.Thread] = None
        heartbeat_errors: list[Exception] = []
        repository_lease: Optional[RepositoryLease] = None
        owns_repository_lease = False
        repository_heartbeat_stop: Optional[threading.Event] = None
        repository_heartbeat_thread: Optional[threading.Thread] = None
        repository_heartbeat_errors: list[Exception] = []
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
                    # A second hosted agent is allowed to queue briefly behind a
                    # sibling mutation instead of surfacing SessionBusy as a
                    # seemingly random tool failure. Reads remain fully parallel;
                    # only the durable session mutation fence is queued here.
                    wait_budget = min(
                        _MUTATION_LEASE_WAIT_MAX_SECONDS,
                        max(0.0, float(deadline_seconds) * 0.25),
                    )
                    wait_deadline = time.monotonic() + wait_budget
                    owner = f"hosted-{os.getpid()}:{workstream_id or 'default'}"
                    while True:
                        try:
                            lease = self.sessions.acquire(
                                self.session_id,
                                owner,
                                ttl_seconds=_MUTATION_LEASE_TTL_SECONDS,
                            )
                            owns_lease = True
                            break
                        except SessionBusy:
                            remaining = wait_deadline - time.monotonic()
                            if remaining <= 0:
                                raise
                            time.sleep(min(_MUTATION_LEASE_RETRY_SECONDS, remaining))
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

            project_repository = Path(project_entry.path).expanduser().resolve(strict=True)
            task_state = self.task_states.load_optional(
                self.session_id,
                workstream_id=workstream_id,
            )
            task_id = (
                task_state.task_id
                if task_state is not None
                else f"task-{self.session_id}-{workstream_id or 'default'}"
            )
            try:
                inherited_repository = current_repository_lease(project_repository)
                if (
                    inherited_repository is not None
                    and inherited_repository.session_id == self.session_id
                    and inherited_repository.task_id == task_id
                ):
                    repository_lease = self.repository_leases.validate(
                        project_repository,
                        inherited_repository,
                    )
                else:
                    wait_budget = min(
                        _REPOSITORY_LEASE_WAIT_MAX_SECONDS,
                        max(0.0, float(deadline_seconds) * 0.25),
                    )
                    wait_deadline = time.monotonic() + wait_budget
                    while True:
                        try:
                            repository_lease, _recovered_stale = self.repository_leases.acquire(
                                project_repository,
                                session_id=self.session_id,
                                task_id=task_id,
                                connection_id=(
                                    f"{self.hosted_origin.key}:{workstream_id or 'default'}"
                                ),
                                current_operation=tool_name,
                                ttl_seconds=DEFAULT_REPOSITORY_LEASE_TTL_SECONDS,
                            )
                            owns_repository_lease = True
                            break
                        except RepositoryLeaseConflict:
                            remaining = wait_deadline - time.monotonic()
                            if remaining <= 0:
                                raise
                            time.sleep(min(_MUTATION_LEASE_RETRY_SECONDS, remaining))
            except RepositoryLeaseError as exc:
                # The session mutation fence was acquired first to preserve the
                # global lock order used by task.execute_plan. If the repository
                # fence cannot be acquired, release that first fence immediately
                # instead of leaving sibling hosted agents blocked until TTL.
                if heartbeat_stop is not None:
                    heartbeat_stop.set()
                if heartbeat_thread is not None:
                    heartbeat_thread.join(timeout=2.0)
                if lease is not None and owns_lease:
                    self.sessions.release(lease)
                    lease = None
                    owns_lease = False
                if isinstance(exc, RepositoryLeaseConflict):
                    operation = str(exc.details.get("current_operation") or "mutation")
                    raise HostedBridgeAccessDenied(
                        "repository is busy with another live mutation "
                        f"({operation}); continue read-only work or retry after it finishes"
                    ) from exc
                raise HostedBridgeAccessDenied(
                    f"repository mutation lease is unavailable: {str(redact(str(exc)))[:300]}"
                ) from exc

            if owns_repository_lease:
                repository_heartbeat_stop = threading.Event()

                def keep_repository_lease_alive() -> None:
                    assert repository_lease is not None
                    assert repository_heartbeat_stop is not None
                    current = repository_lease
                    while not repository_heartbeat_stop.wait(
                        _REPOSITORY_LEASE_HEARTBEAT_SECONDS
                    ):
                        try:
                            current = self.repository_leases.heartbeat(
                                project_repository,
                                current,
                                current_operation=tool_name,
                                ttl_seconds=DEFAULT_REPOSITORY_LEASE_TTL_SECONDS,
                            )
                        except Exception as exc:  # fail closed after the command returns
                            repository_heartbeat_errors.append(exc)
                            return

                repository_heartbeat_thread = threading.Thread(
                    target=keep_repository_lease_alive,
                    name=f"karox-repository-lease-{self.session_id}",
                    daemon=True,
                )
                repository_heartbeat_thread.start()
        try:
            try:
                if lease is None:
                    if approval_token is None:
                        result = runtime.execute(command, lease=None).to_dict()
                    else:
                        result = runtime.execute(
                            command,
                            capability_token=approval_token,
                            lease=None,
                        ).to_dict()
                else:
                    # Expose exactly this already-owned mutation fence to nested
                    # safety helpers (notably the on-demand rollback checkpoint).
                    # ContextVar scoping prevents sibling hosted requests from
                    # observing/reusing it, so concurrency fencing is unchanged.
                    with mutation_lease_context(lease):
                        if approval_token is None:
                            result = runtime.execute(command, lease=lease).to_dict()
                        else:
                            result = runtime.execute(
                                command,
                                capability_token=approval_token,
                                lease=lease,
                            ).to_dict()
            except ActionConfirmationRequired as stop:
                raise self._approval_error(tool_name, command, stop.decision) from stop
            except SessionError as exc:
                if "revoked" in str(exc).lower():
                    raise HostedBridgeAccessDenied("session access has been revoked") from exc
                raise
            if heartbeat_errors:
                raise HostedBridgeAccessDenied(
                    "mutation lease heartbeat failed; the write result is not trusted"
                )
            if repository_heartbeat_errors:
                raise HostedBridgeAccessDenied(
                    "repository lease heartbeat failed; the write result is not trusted"
                )
            if tool_name == "karox.runtime.status":
                result = dict(result)
                result["bridge_health"] = _hosted_bridge_health(self.session_id)
            return result
        finally:
            if repository_heartbeat_stop is not None:
                repository_heartbeat_stop.set()
            if repository_heartbeat_thread is not None:
                repository_heartbeat_thread.join(timeout=2.0)
            if repository_lease is not None and owns_repository_lease:
                self.repository_leases.release(
                    Path(project_entry.path).expanduser().resolve(strict=True),
                    repository_lease,
                )
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

    def execute_synchronous(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
    ) -> dict[str, Any]:
        """Execute the owning runtime directly, bypassing hosted compatibility.

        In-process planners use this surface while they hold a repository mutation
        lease. External MCP calls continue through execute(), where long
        command.run calls may detach into the durable compatibility worker.
        """
        return self._owner(tool_name).execute(
            tool_name,
            arguments,
            idempotency_key=idempotency_key,
            deadline_seconds=deadline_seconds,
        )

    def execute_approved(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        expected_action_digest: str,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
    ) -> dict[str, Any]:
        """Run one exact action through an owner's trusted approval entrypoint."""

        owner = self._owner(tool_name)
        approved = getattr(owner, "execute_approved", None)
        if not callable(approved):
            raise HostedBridgeAccessDenied(
                "this tool does not support protocol-level approval retries"
            )
        return dict(
            approved(
                tool_name,
                arguments,
                expected_action_digest=expected_action_digest,
                idempotency_key=idempotency_key,
                deadline_seconds=deadline_seconds,
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
        # Compatibility bridges keep old synchronous tool names safe on clients
        # with cached catalogues. Long verification/command work is detached into
        # the durable worker so an HTTP timeout or bridge recycle cannot strand a
        # request or hold the mutation fence for minutes.
        if tool_name in {"karox.tests.run", "karox.checks.run"} and isinstance(
            idempotency_key, str
        ) and idempotency_key:
            durable_owner = self._owners.get("karox.checks.start")
            compat = getattr(durable_owner, "execute_check_run_compat", None)
            if callable(compat):
                compat_result = compat(
                    tool_name,
                    dict(arguments),
                    idempotency_key=idempotency_key,
                    deadline_seconds=deadline_seconds,
                )
                if compat_result is not None:
                    return compat_result
        if tool_name == "karox.command.run" and isinstance(idempotency_key, str) and idempotency_key:
            durable_owner = self._owners.get("karox.command.start")
            compat = getattr(durable_owner, "execute_command_run_compat", None)
            if callable(compat):
                compat_result = compat(
                    dict(arguments),
                    idempotency_key=idempotency_key,
                    deadline_seconds=deadline_seconds,
                )
                if compat_result is not None:
                    return compat_result
        return self._owner(tool_name).execute(
            tool_name,
            arguments,
            idempotency_key=idempotency_key,
            deadline_seconds=deadline_seconds,
        )
