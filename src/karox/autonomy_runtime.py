"""High-level, versioned hosted tools for ChatGPT Web autonomy.

This runtime composes beside the existing Core and hosted-tools runtimes.  It
adds a small high-level surface without renaming or weakening any low-level tool.
Phase D implements task bootstrap/checkpoint/resume/status; later phases extend
this same runtime with repository inspection, bounded plans and affected checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .affected_checks import AffectedChecksEngine, AffectedChecksError
from .artifacts import ArtifactStore
from .client_capabilities import (
    ClientCapabilityStore,
    negotiate_client_capabilities,
)
from .hosted_bridge import (
    AUTONOMY_TOOL_NAMES,
    CORE_TOOL_NAMES,
    DEFAULT_HOSTED_DEADLINE_SECONDS,
    HostedBridgeAccessDenied,
)
from .models import AccessProfile, Capability, Origin
from .plan_executor import PlanExecutionError, PlanExecutor
from .policy import CapabilityPolicy
from .project_registry import ProjectRegistry, ProjectRegistryError
from .proxy import ProxyToolDescriptor
from .repo_context import RepositoryContextEngine
from .repository_lease import RepositoryLeaseStore
from .security import redact
from .sessions import IdempotencyConflict, SessionStore, mutation_lease_context
from .memory import KaroXMemory, MemoryError, MemoryKind, MemoryScope
from .project_map import ProjectFactMap
from .task_state import FactOrigin, TaskFact, TaskStateStore, fact

TASK_BOOTSTRAP = "karox.task.bootstrap"
TASK_CHECKPOINT = "karox.task.checkpoint"
TASK_RESUME = "karox.task.resume"
TASK_STATUS = "karox.task.status"
TASK_WORKSTREAMS = "karox.task.workstreams"
REPO_INSPECT = "karox.repo.inspect"
TASK_EXECUTE_PLAN = "karox.task.execute_plan"
CHECKS_RUN_AFFECTED = "karox.checks.run_affected"
MEMORY_REMEMBER = "karox.memory.remember"
MEMORY_RECALL = "karox.memory.recall"
MEMORY_CONTEXT = "karox.memory.context"
MEMORY_LIST = "karox.memory.list"
MEMORY_FORGET = "karox.memory.forget"

_AUTONOMY_MUTATION_LEASE_TTL_SECONDS = 60.0
_AUTONOMY_MUTATION_LEASE_HEARTBEAT_SECONDS = 20.0


def _practical_output_size_limit_from_bridge_diagnostics(
    default: int = 64 * 1024,
) -> int:
    """Reuse the launcher's advertised transport limit when running behind a web bridge.

    The saved web bridge already publishes one authoritative diagnostics payload to
    the child process.  Autonomy snapshots must not silently fall back to 64 KiB
    when that same bridge advertises a larger verified transport budget, otherwise
    agents see contradictory capabilities for one live session.
    """
    raw = os.environ.get("KAROX_BRIDGE_DIAGNOSTICS_JSON", "").strip()
    if not raw:
        return default
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    if not isinstance(payload, dict):
        return default
    capabilities = payload.get("client_capabilities")
    if not isinstance(capabilities, dict):
        return default
    value = capabilities.get("practical_output_size_limit")
    if (
        isinstance(value, int)
        and not isinstance(value, bool)
        and 4096 <= value <= 16 * 1024 * 1024
    ):
        return value
    return default


if AUTONOMY_TOOL_NAMES != frozenset(
    {
        TASK_BOOTSTRAP,
        TASK_CHECKPOINT,
        TASK_RESUME,
        TASK_STATUS,
        TASK_WORKSTREAMS,
        REPO_INSPECT,
        TASK_EXECUTE_PLAN,
        CHECKS_RUN_AFFECTED,
        MEMORY_REMEMBER,
        MEMORY_RECALL,
        MEMORY_CONTEXT,
        MEMORY_LIST,
        MEMORY_FORGET,
    }
):
    raise RuntimeError("autonomy tool catalogue is out of sync")


@dataclass(frozen=True)
class _ToolMeta:
    description: str
    input_schema: dict[str, Any]
    read_only: bool
    # The capability tier the session profile must include for this tool to be
    # served at all. ``None`` marks tools that only touch session-scoped task
    # state or read the repository, mirroring the ``capability`` field of the
    # hosted-extra catalogue so ``profile_incompatible_tools`` can treat both
    # halves of the tool universe the same way.
    capability: Optional[Capability] = None
    additional_capabilities: tuple[Capability, ...] = ()


class _WorkstreamScopedDelegate:
    """Inject one workstream into delegated Core calls without touching extras."""

    def __init__(self, delegate: Any, workstream_id: Optional[str]) -> None:
        self._delegate = delegate
        self._workstream_id = workstream_id

    def descriptors(self) -> list[ProxyToolDescriptor]:
        return list(self._delegate.descriptors())

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
    ) -> dict[str, Any]:
        routed = dict(arguments)
        if self._workstream_id is not None and tool_name in CORE_TOOL_NAMES:
            routed["workstream_id"] = self._workstream_id
        return self._delegate.execute(
            tool_name,
            routed,
            idempotency_key=idempotency_key,
            deadline_seconds=deadline_seconds,
        )


_ORIGIN_ENUM = [
    FactOrigin.REPORTED_BY_AGENT.value,
    FactOrigin.INFERRED.value,
    FactOrigin.HISTORICAL.value,
    FactOrigin.STALE.value,
    FactOrigin.PENDING.value,
]

_MEMORY_SCOPE_ENUM = ["user", "project", "workstream", "session"]
_MEMORY_KIND_ENUM = ["fact", "preference", "decision", "note", "todo", "handoff"]

_TOOLS: dict[str, _ToolMeta] = {
    MEMORY_REMEMBER: _ToolMeta(
        description=(
            "Store one durable fact, preference, decision, note, todo, or handoff in "
            "KaroX memory. Local only, inspectable, forgettable; credential-shaped "
            "content is refused. A keyed remember replaces the previous entry."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": _MEMORY_SCOPE_ENUM},
                "kind": {"type": "string", "enum": _MEMORY_KIND_ENUM},
                "content": {"type": "string", "minLength": 1, "maxLength": 4000},
                "key": {"type": "string", "maxLength": 200},
                "sensitivity": {"type": "string", "enum": ["normal", "personal"]},
                "ttl_seconds": {"type": "number"},
                "source_path": {"type": "string"},
                "source_sha256": {"type": "string"},
                "workstream_id": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            "required": ["scope", "kind", "content"],
            "additionalProperties": False,
        },
        read_only=False,
    ),
    MEMORY_RECALL: _ToolMeta(
        description=(
            "Recall the most relevant KaroX memory entries for a query. Deterministic "
            "ranking under a byte budget; never a memory dump."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "minLength": 1, "maxLength": 1000},
                "scopes": {
                    "type": "array",
                    "items": {"type": "string", "enum": _MEMORY_SCOPE_ENUM},
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 20},
                "budget_chars": {"type": "integer", "minimum": 100, "maximum": 20000},
                "workstream_id": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        read_only=True,
    ),
    MEMORY_CONTEXT: _ToolMeta(
        description=(
            "Build a compact prompt-ready block of the memory most relevant to a "
            "task, across the selected scopes, within a byte budget."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "task": {"type": "string", "maxLength": 2000},
                "scopes": {
                    "type": "array",
                    "items": {"type": "string", "enum": _MEMORY_SCOPE_ENUM},
                },
                "budget_chars": {"type": "integer", "minimum": 100, "maximum": 20000},
                "workstream_id": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            "additionalProperties": False,
        },
        read_only=True,
    ),
    MEMORY_LIST: _ToolMeta(
        description=(
            "List every memory entry in one scope so the user can inspect exactly "
            "what the runtime knows."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": _MEMORY_SCOPE_ENUM},
                "workstream_id": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            "required": ["scope"],
            "additionalProperties": False,
        },
        read_only=True,
    ),
    MEMORY_FORGET: _ToolMeta(
        description=(
            "Permanently remove a memory entry by id or key. Deletion is immediate "
            "and on disk."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": _MEMORY_SCOPE_ENUM},
                "entry_id": {"type": "string"},
                "key": {"type": "string"},
                "workstream_id": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            "required": ["scope"],
            "additionalProperties": False,
        },
        read_only=False,
    ),
    TASK_BOOTSTRAP: _ToolMeta(
        description=(
            "Create or refresh authoritative task state and return a compact recovery "
            "snapshot: repository, branch, dirty state, objective, architecture, "
            "permissions, verification commands, services, blockers, and next safe action."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "objective": {"type": "string"},
                "connection_profile": {"type": "string"},
                "project_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "workstream_id": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.REPO_READ,
    ),
    TASK_CHECKPOINT: _ToolMeta(
        description=(
            "Atomically checkpoint task facts for cross-chat recovery. Agent-supplied "
            "facts retain reported/inferred/pending provenance and cannot self-assert verified."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "updates": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "object",
                        "properties": {
                            "value": {},
                            "origin": {"type": "string", "enum": _ORIGIN_ENUM},
                            "evidence": {
                                "type": "array",
                                "items": {"type": "string"},
                                "maxItems": 100,
                            },
                        },
                        "required": ["value"],
                        "additionalProperties": False,
                    },
                },
                "expected_revision": {"type": "integer"},
                "workstream_id": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            "required": ["updates"],
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.REPO_WRITE,
    ),
    TASK_RESUME: _ToolMeta(
        description=(
            "Resume a task from authoritative state and compare stored repository facts "
            "with the current branch/revision/dirty state before recommending a safe action."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "workstream_id": {"type": "string", "minLength": 1, "maxLength": 64}
            },
            "additionalProperties": False,
        },
        read_only=True,
        capability=Capability.REPO_READ,
    ),
    TASK_STATUS: _ToolMeta(
        description="Return the current authoritative task state with fact provenance.",
        input_schema={
            "type": "object",
            "properties": {
                "workstream_id": {"type": "string", "minLength": 1, "maxLength": 64}
            },
            "additionalProperties": False,
        },
        read_only=True,
        capability=Capability.REPO_READ,
    ),
    TASK_WORKSTREAMS: _ToolMeta(
        description=(
            "List compact read-only summaries of parallel task workstreams in this "
            "repository session so one chat can coordinate with sibling chats without "
            "loading or mutating their full context."
        ),
        input_schema={
            "type": "object",
            "properties": {"include_default": {"type": "boolean", "default": True}},
            "additionalProperties": False,
        },
        read_only=True,
        capability=Capability.REPO_READ,
    ),
    REPO_INSPECT: _ToolMeta(
        description=(
            "Deterministically inspect a repository goal with ripgrep, Python AST, "
            "call/import relationships, tests/docs ranking, excerpts, cache, and a full artifact. "
            "Start with focused (the default); escalate to standard or deep only when focused "
            "does not provide enough context, because broader depths scan substantially more data."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "goal": {"type": "string"},
                "workstream_id": {"type": "string", "minLength": 1, "maxLength": 64},
                "depth": {
                    "type": "string",
                    "enum": ["focused", "standard", "deep"],
                    "default": "focused",
                    "description": (
                        "Use focused first. Escalate only if the smaller result is insufficient."
                    ),
                },
            },
            "required": ["goal"],
            "additionalProperties": False,
        },
        read_only=True,
        capability=Capability.REPO_READ,
    ),
    TASK_EXECUTE_PLAN: _ToolMeta(
        description=(
            "Execute a bounded, journaled sequence/DAG of safe repository, check, "
            "managed-runtime, browser, and checkpoint operations with repository lease, "
            "idempotent replay, budgets, artifacts, and deterministic stop conditions. "
            "Prefer this for two or more related reads/searches/verification steps to reduce "
            "hosted MCP round trips; keep a single simple read as a direct tool call."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "operations": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {
                        "type": "object",
                        "properties": {
                            "operation_id": {"type": "string"},
                            "action": {
                                "type": "string",
                                "description": "One of inspect, search, read, patch, command, checks, dev_server, browser, runtime, checkpoint. Invalid values are reported with the operation index by KaroX.",
                            },
                            "inputs": {"type": "object"},
                            "depends_on": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                            "preconditions": {"type": "object"},
                            "expected_outcome": {"type": "object"},
                            "on_failure": {
                                "type": "string",
                                "description": "stop, continue, or skip_dependents; invalid values are reported with the operation index.",
                            },
                            "idempotency_key": {"type": "string"},
                            "output_policy": {
                                "type": "string",
                                "description": "summary, inline, or artifact; invalid values are reported with the operation index.",
                            },
                        },
                        "required": ["operation_id", "action", "inputs"],
                        "additionalProperties": True,
                    },
                },
                "budgets": {
                    "type": "object",
                    "properties": {
                        "max_steps": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                        "max_wall_time": {"type": "number", "minimum": 1, "maximum": 3600, "default": 300.0},
                        "max_inline_output_bytes": {"type": "integer", "minimum": 4096, "maximum": 1048576, "default": 32768},
                        "max_artifact_bytes": {"type": "integer", "minimum": 1024, "maximum": 104857600, "default": 26214400},
                        "max_write_operations": {"type": "integer", "minimum": 0, "maximum": 50, "default": 10},
                        "max_command_runs": {"type": "integer", "minimum": 0, "maximum": 50, "default": 10},
                        "max_browser_actions": {"type": "integer", "minimum": 0, "maximum": 100, "default": 10},
                        "max_fix_attempts": {"type": "integer", "minimum": 0, "maximum": 10, "default": 3},
                        "checkpoint_after_steps": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5}
                    },
                    "additionalProperties": False
                },
                "stop_on_error": {"type": "boolean"},
                "workstream_id": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            "required": ["operations"],
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.REPO_WRITE,
    ),
    CHECKS_RUN_AFFECTED: _ToolMeta(
        description=(
            "Select and run affected tests/checks from changed files, imports, naming, "
            "Git co-change history, project config, and the approved verification allowlist. "
            "Supports controlled baseline evidence and up to three distinct scoped fix candidates."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "mode": {"type": "string", "enum": ["baseline", "current"]},
                "changed_files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 500,
                },
                "baseline_artifact_id": {"type": "string"},
                "timeout_seconds": {"type": "number"},
                "allowed_fix_paths": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 500,
                },
                "fix_attempts": {
                    "type": "array",
                    "maxItems": 3,
                    "items": {
                        "type": "object",
                        "properties": {
                            "command": {"type": "object"},
                            "expected_paths": {
                                "type": "array",
                                "items": {"type": "string"},
                            },
                        },
                        "required": ["command", "expected_paths"],
                        "additionalProperties": False,
                    },
                },
                "workstream_id": {"type": "string", "minLength": 1, "maxLength": 64},
            },
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.CHECKS_RUN,
        additional_capabilities=(Capability.PROCESS_RUN,),
    ),
}


class AutonomyRuntime:
    """Session-scoped high-level runtime with strict idempotent mutations."""

    def __init__(
        self,
        repository: Path,
        sessions: SessionStore,
        session_id: str,
        allowed_tool_names: Sequence[str],
        *,
        access_profile: AccessProfile,
        hosted_origin: Origin,
        connection_profile: str,
        verification_commands: Sequence[Sequence[str]] = (),
        operation_runtime: Optional[Any] = None,
        client_kind: str = "hosted-mcp",
        project_registry: Optional[ProjectRegistry] = None,
        project_registry_loader: Optional[Callable[[], ProjectRegistry]] = None,
    ) -> None:
        unknown = set(allowed_tool_names).difference(AUTONOMY_TOOL_NAMES)
        if unknown:
            raise HostedBridgeAccessDenied(
                f"unknown autonomy tools: {sorted(unknown)}"
            )
        if not allowed_tool_names:
            raise HostedBridgeAccessDenied("autonomy tool allowlist must not be empty")
        # Fail fast on tools the session profile can never serve, the same way
        # CoreToolBridge and HostedToolsRuntime do. The executor underneath
        # enforces capabilities per operation, but a bridge that starts and
        # dies on the first call is exactly the "listed but never usable"
        # state capability negotiation exists to prevent.
        policy = CapabilityPolicy(access_profile)
        autonomy_grants: set[Capability] = set()
        for name in dict.fromkeys(allowed_tool_names):
            meta = _TOOLS[name]
            if meta.capability is not None:
                autonomy_grants.add(meta.capability)
            autonomy_grants.update(meta.additional_capabilities)
        if autonomy_grants:
            policy.set_grants(hosted_origin, autonomy_grants)
            for capability in sorted(autonomy_grants, key=lambda item: item.value):
                if not policy.decide(hosted_origin, capability).allowed:
                    raise HostedBridgeAccessDenied(
                        f"session profile does not allow {capability.value}"
                    )
        self.repository = repository.expanduser().resolve(strict=True)
        try:
            self.project_registry = project_registry or ProjectRegistry.single(self.repository)
            default_project = self.project_registry.default
            anchor_project = self.project_registry.entry_for_path(self.repository)
        except ProjectRegistryError as exc:
            raise HostedBridgeAccessDenied(f"invalid project registry: {exc}") from exc
        if default_project is None or anchor_project is None:
            raise HostedBridgeAccessDenied("autonomy session anchor must be an approved project")
        self.sessions = sessions
        self.session_id = session_id
        self.access_profile = access_profile
        self.hosted_origin = hosted_origin
        self.connection_profile = str(redact(connection_profile))[:256]
        self.verification_commands = tuple(tuple(item) for item in verification_commands)
        self._allowed = tuple(dict.fromkeys(allowed_tool_names))
        self.client_kind = client_kind.strip() or "hosted-mcp"
        self.task_states = TaskStateStore(sessions)
        # Universal memory lives beside the session store: durable across
        # sessions, shared by every client of this runtime, and isolated in
        # tests because tests always construct SessionStore on a temp root.
        self.memory = KaroXMemory(sessions.root / "memory")
        self._project_registry_loader = project_registry_loader
        self.artifacts = ArtifactStore(session_id)
        self.repo_context = RepositoryContextEngine(
            self.repository,
            self.artifacts,
            policy_profile=access_profile.value,
        )
        self.repository_leases = RepositoryLeaseStore()
        self.plan_executor = PlanExecutor(
            repository=self.repository,
            session_id=session_id,
            connection_id=self.connection_profile,
            delegate=operation_runtime,
            repo_context=self.repo_context,
            task_states=self.task_states,
            artifacts=self.artifacts,
            lease_store=self.repository_leases,
            session_directory=sessions.session_dir(session_id),
        )
        self.affected_checks = AffectedChecksEngine(
            repository=self.repository,
            session_id=session_id,
            connection_id=self.connection_profile,
            delegate=operation_runtime,
            verification_commands=self.verification_commands,
            artifacts=self.artifacts,
            repo_context=self.repo_context,
            task_states=self.task_states,
            lease_store=self.repository_leases,
        )
        self._operation_runtime = operation_runtime
        self._context_lock = threading.RLock()
        self._repo_contexts: dict[str, RepositoryContextEngine] = {
            anchor_project.project_id: self.repo_context
        }
        self._plan_executors: dict[tuple[str, Optional[str]], PlanExecutor] = {
            (anchor_project.project_id, None): self.plan_executor
        }
        self._affected_engines: dict[
            tuple[str, Optional[str]], AffectedChecksEngine
        ] = {(anchor_project.project_id, None): self.affected_checks}
        available_tools = set(self._allowed)
        if operation_runtime is not None:
            try:
                available_tools.update(
                    str(item.name) for item in operation_runtime.descriptors()
                )
            except Exception:
                pass
        self.client_capability_snapshot = negotiate_client_capabilities(
            client_kind=self.client_kind,
            available_tools=sorted(available_tools),
            access_profile=access_profile.value,
            practical_output_size_limit=_practical_output_size_limit_from_bridge_diagnostics(),
            persistent_session=True,
        )
        self.client_capabilities = ClientCapabilityStore(
            sessions.session_dir(session_id)
        )
        try:
            self.client_capabilities.save(self.client_capability_snapshot)
        except Exception:
            # Capability persistence is diagnostic and must not prevent startup.
            pass
        record = sessions.load(session_id)
        sessions.validate_repository(record, self.repository)
        if record.revoked:
            raise HostedBridgeAccessDenied("session access has been revoked")

    def descriptors(self) -> list[ProxyToolDescriptor]:
        self._assert_alive()
        return [
            ProxyToolDescriptor(
                name=name,
                description=_TOOLS[name].description,
                input_schema=_TOOLS[name].input_schema,
                read_only=_TOOLS[name].read_only,
            )
            for name in self._allowed
        ]

    def session_info(self) -> dict[str, Any]:
        record = self._assert_alive()
        return {
            "session_id": record.session_id,
            "task": record.task,
            "repository": str(self.repository),
            "branch": record.branch,
            "access_profile": record.access_profile,
            "revision": record.revision,
        }

    def close(self) -> None:
        seen: set[int] = set()
        for executor in self._plan_executors.values():
            if id(executor) in seen:
                continue
            seen.add(id(executor))
            executor.close()

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

    def _project_for_workstream(
        self,
        workstream_id: Optional[str],
        requested_project_id: Optional[Any] = None,
    ) -> Any:
        registry = self._current_project_registry()
        default = registry.default
        if default is None:
            raise HostedBridgeAccessDenied("no default project is configured")
        requested: Optional[str] = None
        if requested_project_id is not None:
            if not isinstance(requested_project_id, str):
                raise HostedBridgeAccessDenied("project_id must be a string")
            requested = requested_project_id.strip()
            try:
                requested = registry.get(requested).project_id
            except ProjectRegistryError as exc:
                raise HostedBridgeAccessDenied(str(exc)) from exc
        state = self.task_states.load_optional(
            self.session_id,
            workstream_id=workstream_id,
        )
        if state is not None:
            stored = state.facts.get("project_id")
            if stored is None:
                anchor = registry.entry_for_path(self.repository)
                if anchor is None:
                    raise HostedBridgeAccessDenied("session anchor is no longer approved")
                stored_project_id = anchor.project_id
            else:
                stored_project_id = str(stored.value)
            try:
                entry = registry.get(stored_project_id)
            except ProjectRegistryError as exc:
                raise HostedBridgeAccessDenied(
                    "workstream is bound to a project no longer approved by this profile"
                ) from exc
            if requested is not None and requested != entry.project_id:
                raise HostedBridgeAccessDenied(
                    "workstream project binding is immutable; create a new workstream"
                )
            return entry
        try:
            return registry.get(requested or default.project_id)
        except ProjectRegistryError as exc:
            raise HostedBridgeAccessDenied(str(exc)) from exc

    def _repo_context_for(self, project_id: str) -> RepositoryContextEngine:
        try:
            entry = self._current_project_registry().get(project_id)
        except ProjectRegistryError as exc:
            raise HostedBridgeAccessDenied(str(exc)) from exc
        with self._context_lock:
            cached = self._repo_contexts.get(project_id)
            if cached is not None and Path(cached.repository) == Path(entry.path):
                return cached
            context = RepositoryContextEngine(
                Path(entry.path),
                self.artifacts,
                policy_profile=self.access_profile.value,
            )
            self._repo_contexts[entry.project_id] = context
            return context

    def _scoped_delegate(self, workstream_id: Optional[str]) -> Optional[Any]:
        if self._operation_runtime is None:
            return None
        if workstream_id is None:
            return self._operation_runtime
        return _WorkstreamScopedDelegate(self._operation_runtime, workstream_id)

    def _plan_executor_for(
        self,
        project_id: str,
        workstream_id: Optional[str],
    ) -> PlanExecutor:
        entry = self._current_project_registry().get(project_id)
        key = (project_id, workstream_id)
        with self._context_lock:
            cached = self._plan_executors.get(key)
            if cached is not None and Path(cached.repository) == Path(entry.path):
                return cached
            if self._operation_runtime is None:
                base = self._plan_executors.get((project_id, None))
                if base is not None and Path(base.repository) == Path(entry.path):
                    return base
            executor = PlanExecutor(
                repository=Path(entry.path),
                session_id=self.session_id,
                connection_id=self.connection_profile,
                delegate=self._scoped_delegate(workstream_id),
                repo_context=self._repo_context_for(project_id),
                task_states=self.task_states,
                artifacts=self.artifacts,
                lease_store=self.repository_leases,
                session_directory=self.sessions.session_dir(self.session_id),
            )
            self._plan_executors[key] = executor
            return executor

    def _affected_engine_for(
        self,
        project_id: str,
        workstream_id: Optional[str],
    ) -> AffectedChecksEngine:
        entry = self._current_project_registry().get(project_id)
        key = (project_id, workstream_id)
        with self._context_lock:
            cached = self._affected_engines.get(key)
            if cached is not None and Path(cached.repository) == Path(entry.path):
                return cached
            if self._operation_runtime is None:
                base = self._affected_engines.get((project_id, None))
                if base is not None and Path(base.repository) == Path(entry.path):
                    return base
            engine = AffectedChecksEngine(
                repository=Path(entry.path),
                session_id=self.session_id,
                connection_id=self.connection_profile,
                delegate=self._scoped_delegate(workstream_id),
                verification_commands=self.verification_commands,
                artifacts=self.artifacts,
                repo_context=self._repo_context_for(project_id),
                task_states=self.task_states,
                lease_store=self.repository_leases,
            )
            self._affected_engines[key] = engine
            return engine

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
    ) -> dict[str, Any]:
        del deadline_seconds
        self._assert_alive()
        if tool_name not in self._allowed:
            raise HostedBridgeAccessDenied(f"autonomy tool is not exposed: {tool_name}")
        if tool_name == TASK_BOOTSTRAP:
            return self._mutating(
                tool_name,
                arguments,
                idempotency_key,
                self._bootstrap,
            )
        if tool_name == TASK_CHECKPOINT:
            return self._mutating(
                tool_name,
                arguments,
                idempotency_key,
                self._checkpoint,
            )
        if tool_name == TASK_RESUME:
            return self._resume(arguments)
        if tool_name == TASK_STATUS:
            return self._status(arguments)
        if tool_name == TASK_WORKSTREAMS:
            return self._workstreams(arguments)
        if tool_name == REPO_INSPECT:
            return self._inspect(arguments)
        if tool_name == TASK_EXECUTE_PLAN:
            return self._execute_plan(arguments, idempotency_key)
        if tool_name == CHECKS_RUN_AFFECTED:
            return self._mutating(
                tool_name,
                arguments,
                idempotency_key,
                lambda value: self._run_affected(value, idempotency_key),
            )
        if tool_name == MEMORY_REMEMBER:
            return self._memory_remember(arguments)
        if tool_name == MEMORY_RECALL:
            return self._memory_recall(arguments)
        if tool_name == MEMORY_CONTEXT:
            return self._memory_context(arguments)
        if tool_name == MEMORY_LIST:
            return self._memory_list(arguments)
        if tool_name == MEMORY_FORGET:
            return self._memory_forget(arguments)
        raise HostedBridgeAccessDenied(f"autonomy tool has no handler: {tool_name}")

    # -- project intelligence --------------------------------------------------

    def _project_fact_map_summary(
        self, project_id: str, repository: Path
    ) -> str:
        """A deterministic onboarding digest, built once and refreshed cheaply.

        Failures here must never take bootstrap down: an unreadable submodule
        or an exotic filesystem costs the digest, not the session.
        """

        try:
            safe = re.sub(r"[^A-Za-z0-9_.-]", "_", project_id) or "default"
            fact_map = ProjectFactMap(
                repository,
                self.sessions.root / "project-maps" / f"{safe}.json",
            )
            if fact_map.load() is None:
                fact_map.build()
            return fact_map.summary()
        except OSError:
            return ""

    # -- universal memory ----------------------------------------------------

    _MEMORY_SCOPES = {
        "user": MemoryScope.USER,
        "project": MemoryScope.PROJECT,
        "workstream": MemoryScope.WORKSTREAM,
        "session": MemoryScope.SESSION,
    }

    @staticmethod
    def _memory_error(exc: Exception) -> dict[str, Any]:
        return {
            "ok": False,
            "schema_version": 1,
            "error_code": "memory_rejected",
            "error": str(exc),
        }

    def _memory_scope_pair(
        self, scope_name: str, workstream: Optional[str]
    ) -> tuple[MemoryScope, str]:
        scope = self._MEMORY_SCOPES.get(scope_name)
        if scope is None:
            raise MemoryError(f"unknown memory scope: {scope_name!r}")
        if scope is MemoryScope.USER:
            return scope, "default"
        if scope is MemoryScope.PROJECT:
            project = self._project_for_workstream(workstream)
            return scope, project.project_id
        if scope is MemoryScope.WORKSTREAM:
            return scope, workstream or "default"
        return scope, self.session_id

    def _memory_scope_pairs(
        self, names: Any, workstream: Optional[str]
    ) -> tuple[tuple[MemoryScope, str], ...]:
        if names is None:
            names = list(self._MEMORY_SCOPES)
        if not isinstance(names, (list, tuple)):
            raise MemoryError("scopes must be an array of scope names")
        return tuple(
            self._memory_scope_pair(str(name), workstream) for name in names
        )

    def _memory_remember(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._assert_alive()
        workstream = self._workstream_id(arguments)
        try:
            scope, scope_id = self._memory_scope_pair(
                str(arguments.get("scope", "")), workstream
            )
            kind = MemoryKind(str(arguments.get("kind", "note")))
            ttl_raw = arguments.get("ttl_seconds")
            entry = self.memory.remember(
                scope=scope,
                scope_id=scope_id,
                kind=kind,
                content=str(arguments.get("content", "")),
                key=(
                    str(arguments["key"]) if arguments.get("key") is not None else None
                ),
                provenance=self.client_kind,
                sensitivity=str(arguments.get("sensitivity", "normal")),
                ttl_seconds=float(ttl_raw) if ttl_raw is not None else None,
                source_path=arguments.get("source_path"),
                source_sha256=arguments.get("source_sha256"),
            )
        except (MemoryError, ValueError) as exc:
            return self._memory_error(exc)
        return {
            "ok": True,
            "schema_version": 1,
            "entry": dict(redact(entry.to_dict())),
        }

    def _memory_recall(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._assert_alive()
        workstream = self._workstream_id(arguments)
        try:
            pairs = self._memory_scope_pairs(arguments.get("scopes"), workstream)
            entries = self.memory.recall(
                query=str(arguments.get("query", "")),
                scopes=pairs,
                limit=max(1, min(20, int(arguments.get("limit", 5)))),
                budget_chars=max(
                    100, min(20000, int(arguments.get("budget_chars", 2000)))
                ),
            )
        except (MemoryError, ValueError) as exc:
            return self._memory_error(exc)
        return {
            "ok": True,
            "schema_version": 1,
            "count": len(entries),
            "entries": [dict(redact(entry.to_dict())) for entry in entries],
        }

    def _memory_context(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._assert_alive()
        workstream = self._workstream_id(arguments)
        try:
            pairs = self._memory_scope_pairs(arguments.get("scopes"), workstream)
            block = self.memory.context(
                scopes=pairs,
                budget_chars=max(
                    100, min(20000, int(arguments.get("budget_chars", 1500)))
                ),
                task=str(arguments.get("task", "")),
            )
        except (MemoryError, ValueError) as exc:
            return self._memory_error(exc)
        return {"ok": True, "schema_version": 1, "context": str(redact(block))}

    def _memory_list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._assert_alive()
        workstream = self._workstream_id(arguments)
        try:
            scope, scope_id = self._memory_scope_pair(
                str(arguments.get("scope", "")), workstream
            )
            entries = self.memory.list(scope=scope, scope_id=scope_id)
        except (MemoryError, ValueError) as exc:
            return self._memory_error(exc)
        return {
            "ok": True,
            "schema_version": 1,
            "scope": scope.value,
            "scope_id": scope_id,
            "count": len(entries),
            "entries": [dict(redact(entry.to_dict())) for entry in entries],
        }

    def _memory_forget(self, arguments: dict[str, Any]) -> dict[str, Any]:
        self._assert_alive()
        workstream = self._workstream_id(arguments)
        try:
            scope, scope_id = self._memory_scope_pair(
                str(arguments.get("scope", "")), workstream
            )
            removed = self.memory.forget(
                scope=scope,
                scope_id=scope_id,
                entry_id=arguments.get("entry_id"),
                key=arguments.get("key"),
            )
        except (MemoryError, ValueError) as exc:
            return self._memory_error(exc)
        return {"ok": True, "schema_version": 1, "removed": removed}

    def _assert_alive(self) -> Any:
        record = self.sessions.load(self.session_id)
        self.sessions.validate_repository(record, self.repository)
        if record.revoked:
            raise HostedBridgeAccessDenied("session access has been revoked")
        return record

    @staticmethod
    def _digest(tool_name: str, arguments: Mapping[str, Any]) -> str:
        payload = json.dumps(
            {"tool": tool_name, "arguments": arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _mutating(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        idempotency_key: Optional[str],
        handler: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise HostedBridgeAccessDenied("mutating autonomy tools require an idempotency key")
        lease = self.sessions.acquire(
            self.session_id,
            f"autonomy-{tool_name}",
            ttl_seconds=_AUTONOMY_MUTATION_LEASE_TTL_SECONDS,
        )
        heartbeat_stop = threading.Event()
        heartbeat_errors: list[Exception] = []

        def keep_lease_alive() -> None:
            while not heartbeat_stop.wait(_AUTONOMY_MUTATION_LEASE_HEARTBEAT_SECONDS):
                try:
                    self.sessions.heartbeat(
                        lease,
                        ttl_seconds=_AUTONOMY_MUTATION_LEASE_TTL_SECONDS,
                    )
                except Exception as exc:  # fail closed after the handler returns
                    heartbeat_errors.append(exc)
                    return

        heartbeat_thread = threading.Thread(
            target=keep_lease_alive,
            name=f"karox-autonomy-lease-{self.session_id}",
            daemon=True,
        )
        heartbeat_thread.start()
        try:
            record = self.sessions.load(self.session_id)
            try:
                replay = self.sessions.begin_idempotent(
                    record,
                    lease,
                    idempotency_key,
                    self._digest(tool_name, arguments),
                )
            except IdempotencyConflict as exc:
                raise HostedBridgeAccessDenied(str(exc)) from exc
            if replay is not None:
                return {**replay, "idempotent_replay": True}
            # Nested checks/patches use the exact same fenced lease only inside
            # this synchronous execution context. A parallel hosted request does
            # not inherit the ContextVar and therefore remains blocked.
            with mutation_lease_context(lease):
                result = handler(arguments)
            if heartbeat_errors:
                raise HostedBridgeAccessDenied(
                    "autonomy mutation lease heartbeat failed; the result is not trusted"
                )
            # Nested delegated Core calls legitimately advance the session
            # revision while this outer idempotency intent is pending. Reload so
            # completing the parent intent preserves those changes instead of
            # failing with StaleSessionRevision or overwriting newer state.
            record = self.sessions.load(self.session_id)
            self.sessions.complete_idempotent(
                record,
                lease,
                idempotency_key,
                result,
            )
            return {**result, "idempotent_replay": False}
        finally:
            heartbeat_stop.set()
            heartbeat_thread.join(timeout=2.0)
            self.sessions.release(lease)

    def _git(
        self,
        *arguments: str,
        repository: Optional[Path] = None,
        allow_failure: bool = False,
    ) -> str:
        target = (repository or self.repository).expanduser().resolve(strict=True)
        completed = subprocess.run(
            ["git", "-C", str(target), *arguments],
            check=False,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode != 0:
            if allow_failure:
                return ""
            raise HostedBridgeAccessDenied("read-only Git inspection failed")
        return completed.stdout.strip()

    def _git_snapshot(self, repository: Optional[Path] = None) -> dict[str, Any]:
        branch = self._git("branch", "--show-current", repository=repository) or "detached"
        revision = self._git(
            "rev-parse",
            "--verify",
            "HEAD",
            repository=repository,
            allow_failure=True,
        ) or "unborn"
        status = self._git("status", "--porcelain=v1", repository=repository)
        dirty_lines = [line for line in status.splitlines() if line]
        return {
            "branch": branch,
            "revision": revision,
            "dirty": bool(dirty_lines),
            "dirty_count": len(dirty_lines),
            "dirty_summary": dirty_lines[:50],
            "dirty_truncated": len(dirty_lines) > 50,
        }

    def _architecture(self, repository: Optional[Path] = None) -> list[dict[str, Any]]:
        target = (repository or self.repository).expanduser().resolve(strict=True)
        candidates = (
            "src/karox/hosted_bridge.py",
            "src/karox/proxy_server.py",
            "src/karox/artifacts.py",
            "src/karox/sessions.py",
            "src/karox/checkpoints.py",
            "src/karox/transcript.py",
            "src/karox/verification.py",
            "src/karox/plan_act.py",
            "src/karox/smart_stop.py",
            "src/karox/session_view.py",
            "src/karox/cost_intelligence.py",
            "src/karox/connection_status.py",
        )
        return [
            {"path": path, "present": (target / path).is_file()}
            for path in candidates
        ]

    @staticmethod
    def _workstream_id(arguments: Mapping[str, Any]) -> Optional[str]:
        raw = arguments.get("workstream_id")
        if raw is None:
            return None
        if not isinstance(raw, str):
            raise HostedBridgeAccessDenied("workstream_id must be a string")
        value = raw.strip()
        if not value or len(value) > 64:
            raise HostedBridgeAccessDenied("workstream_id must be 1..64 characters")
        allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
        if any(char not in allowed for char in value):
            raise HostedBridgeAccessDenied("workstream_id contains unsafe characters")
        return value

    def _base_facts(
        self,
        arguments: Mapping[str, Any],
        *,
        project_id: str,
        repository: Path,
    ) -> tuple[dict[str, TaskFact], dict[str, Any]]:
        record = self._assert_alive()
        git = self._git_snapshot(repository)
        objective = arguments.get("objective", record.task)
        if not isinstance(objective, str) or not objective.strip() or len(objective) > 20_000:
            raise HostedBridgeAccessDenied("objective must be a non-empty string up to 20000 characters")
        profile = arguments.get("connection_profile", self.connection_profile)
        if not isinstance(profile, str) or not profile.strip() or len(profile) > 256:
            raise HostedBridgeAccessDenied("connection_profile is invalid")
        blockers = [
            item
            for item in record.failures
            if isinstance(item, dict) and item.get("resolved") is not True
        ]
        pending_gates = [
            item
            for item in record.unfinished_actions
            if isinstance(item, dict) and item.get("requires_user") is True
        ]
        facts = {
            "objective": fact(objective.strip(), FactOrigin.VERIFIED, "session.task"),
            "project_id": fact(project_id, FactOrigin.VERIFIED, "project.registry"),
            "repository": fact(str(repository), FactOrigin.VERIFIED, "project.registry"),
            "branch": fact(git["branch"], FactOrigin.OBSERVED, "git.branch"),
            "repository_revision": fact(git["revision"], FactOrigin.OBSERVED, "git.rev-parse"),
            "connection_profile": fact(profile.strip(), FactOrigin.VERIFIED, "bridge.profile"),
            "client_capabilities": fact(
                self.client_capability_snapshot.to_dict(),
                FactOrigin.OBSERVED,
                "runtime.descriptors",
            ),
            "access_profile": fact(record.access_profile, FactOrigin.VERIFIED, "session.access_profile"),
            "files_changed": fact(record.changed_files, FactOrigin.HISTORICAL, "session.changed_files"),
            "checks_executed": fact(record.checks, FactOrigin.HISTORICAL, "session.checks"),
            "current_blockers": fact(blockers, FactOrigin.HISTORICAL, "session.failures"),
            "pending_user_gates": fact(pending_gates, FactOrigin.PENDING, "session.unfinished_actions"),
            "next_safe_action": fact(
                "inspect current diff and architecture before the next mutation",
                FactOrigin.PENDING,
                "bootstrap.policy",
            ),
        }
        return facts, git

    def _bootstrap(self, arguments: dict[str, Any]) -> dict[str, Any]:
        workstream = self._workstream_id(arguments)
        project = self._project_for_workstream(
            workstream,
            arguments.get("project_id"),
        )
        repository = Path(project.path)
        facts, git = self._base_facts(
            arguments,
            project_id=project.project_id,
            repository=repository,
        )
        state = self.task_states.bootstrap(
            self.session_id,
            facts,
            workstream_id=workstream,
        )
        record = self._assert_alive()
        return {
            "ok": True,
            "schema_version": 1,
            "workstream_id": workstream or "default",
            "available_workstreams": list(self.task_states.list_workstreams(self.session_id)),
            "task": state.compact(),
            "project_id": project.project_id,
            "project_name": project.label,
            "project_fact_map": self._project_fact_map_summary(
                project.project_id, repository
            ),
            "repository": str(repository),
            "branch": git["branch"],
            "repository_revision": git["revision"],
            "dirty_summary": {
                "dirty": git["dirty"],
                "count": git["dirty_count"],
                "entries": git["dirty_summary"],
                "truncated": git["dirty_truncated"],
            },
            "objective": state.facts["objective"].value,
            "relevant_architecture": self._architecture(repository),
            "client_capabilities": self.client_capability_snapshot.to_dict(),
            "permissions": {
                "access_profile": self.access_profile.value,
                "repository_scoped": True,
                "unrestricted_developer_commands": self.access_profile == AccessProfile.ELEVATED,
                "git_push": False,
                "publish": False,
                "auth_commands": False,
                "deploy_release": False,
                "destructive_git": False,
            },
            "verification_commands": [list(item) for item in self.verification_commands],
            "running_services": record.jobs,
            "known_blockers": state.facts["current_blockers"].value,
            "recommended_first_safe_action": state.facts["next_safe_action"].value,
        }

    def _checkpoint(self, arguments: dict[str, Any]) -> dict[str, Any]:
        workstream = self._workstream_id(arguments)
        updates = arguments.get("updates")
        if not isinstance(updates, dict) or not updates:
            raise HostedBridgeAccessDenied("updates must be a non-empty object")
        parsed: dict[str, TaskFact] = {}
        for name, payload in updates.items():
            if not isinstance(name, str) or not isinstance(payload, dict):
                raise HostedBridgeAccessDenied("checkpoint updates are malformed")
            origin_raw = payload.get("origin", FactOrigin.REPORTED_BY_AGENT.value)
            try:
                origin = FactOrigin(str(origin_raw))
            except ValueError as exc:
                raise HostedBridgeAccessDenied("checkpoint fact origin is invalid") from exc
            if origin in {FactOrigin.VERIFIED, FactOrigin.OBSERVED}:
                raise HostedBridgeAccessDenied(
                    "agent checkpoint cannot self-assert verified or observed provenance"
                )
            evidence = payload.get("evidence", [])
            if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
                raise HostedBridgeAccessDenied("checkpoint evidence must be a string array")
            parsed[name] = TaskFact(
                value=payload.get("value"),
                origin=origin,
                evidence=tuple(evidence[:100]),
            )
        expected = arguments.get("expected_revision")
        if expected is not None and (
            not isinstance(expected, int) or isinstance(expected, bool) or expected < 0
        ):
            raise HostedBridgeAccessDenied("expected_revision must be a non-negative integer")
        auto_bootstrapped = False
        if self.task_states.load_optional(
            self.session_id,
            workstream_id=workstream,
        ) is None:
            # Older threads and newly-created named workstreams may checkpoint
            # before bootstrap. Establish verified base facts for exactly that
            # workstream instead of falling back to the legacy default task.
            project = self._project_for_workstream(workstream)
            facts, _git = self._base_facts(
                {},
                project_id=project.project_id,
                repository=Path(project.path),
            )
            self.task_states.bootstrap(
                self.session_id,
                facts,
                workstream_id=workstream,
            )
            auto_bootstrapped = True
        state = self.task_states.checkpoint(
            self.session_id,
            parsed,
            expected_revision=expected,
            workstream_id=workstream,
        )
        return {
            "ok": True,
            "schema_version": 1,
            "workstream_id": workstream or "default",
            "available_workstreams": list(self.task_states.list_workstreams(self.session_id)),
            "task": state.compact(),
            "auto_bootstrapped": auto_bootstrapped,
        }

    def _status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        unknown = set(arguments).difference({"workstream_id"})
        if unknown:
            raise HostedBridgeAccessDenied(
                f"task.status received unknown arguments: {sorted(unknown)}"
            )
        workstream = self._workstream_id(arguments)
        state = self.task_states.load_optional(
            self.session_id,
            workstream_id=workstream,
        )
        available = list(self.task_states.list_workstreams(self.session_id))
        if state is None:
            return {
                "ok": False,
                "schema_version": 1,
                "workstream_id": workstream or "default",
                "available_workstreams": available,
                "error_code": "task_not_initialized",
                "error": "task state is not initialized for this workstream",
                "next_safe_action": "call karox.task.bootstrap for this workstream before task.status or task.resume",
            }
        return {
            "ok": True,
            "schema_version": 1,
            "workstream_id": workstream or "default",
            "available_workstreams": available,
            "task": state.compact(),
        }

    def _workstream_summary(self, workstream_id: str, state: Any) -> dict[str, Any]:
        def value(name: str, default: Any = None) -> Any:
            item = state.facts.get(name)
            return item.value if item is not None else default

        registry = self._current_project_registry()
        default = registry.default
        project_id = value("project_id", default.project_id if default is not None else None)
        project_name: Optional[str] = None
        if isinstance(project_id, str):
            try:
                project_name = registry.get(project_id).label
            except ProjectRegistryError:
                project_name = None
        return {
            "project_id": project_id,
            "project_name": project_name,
            "workstream_id": workstream_id,
            "task_id": state.task_id,
            "revision": state.revision,
            "updated_at": state.updated_at,
            "objective": value("objective"),
            "current_phase": value("current_phase"),
            "current_blockers": value("current_blockers", []),
            "next_safe_action": value("next_safe_action"),
            "files_changed": value("files_changed", []),
        }

    def _workstreams(self, arguments: dict[str, Any]) -> dict[str, Any]:
        unknown = set(arguments).difference({"include_default"})
        if unknown:
            raise HostedBridgeAccessDenied(
                f"task.workstreams received unknown arguments: {sorted(unknown)}"
            )
        include_default = arguments.get("include_default", True)
        if not isinstance(include_default, bool):
            raise HostedBridgeAccessDenied("include_default must be a boolean")

        summaries: list[dict[str, Any]] = []
        if include_default:
            default = self.task_states.load_optional(self.session_id)
            if default is not None:
                summaries.append(self._workstream_summary("default", default))
        for workstream_id in self.task_states.list_workstreams(self.session_id):
            state = self.task_states.load_optional(
                self.session_id,
                workstream_id=workstream_id,
            )
            if state is not None:
                summaries.append(self._workstream_summary(workstream_id, state))
        registry = self._current_project_registry()
        projects = [
            {
                "project_id": entry.project_id,
                "project_name": entry.label,
                "default": entry.project_id == registry.default_project_id,
            }
            for entry in registry.projects
        ]
        return {
            "ok": True,
            "schema_version": 1,
            "session_id": self.session_id,
            "repository": str(self.repository),
            "default_project_id": registry.default_project_id,
            "projects": projects,
            "count": len(summaries),
            "workstreams": summaries,
        }

    def _inspect(self, arguments: dict[str, Any]) -> dict[str, Any]:
        workstream = self._workstream_id(arguments)
        project = self._project_for_workstream(workstream)
        goal = arguments.get("goal")
        depth = arguments.get("depth", "focused")
        if not isinstance(goal, str):
            raise HostedBridgeAccessDenied("repo.inspect goal must be a string")
        if not isinstance(depth, str):
            raise HostedBridgeAccessDenied("repo.inspect depth must be a string")
        try:
            return self._repo_context_for(project.project_id).inspect(goal, depth)
        except (OSError, RuntimeError, ValueError) as exc:
            raise HostedBridgeAccessDenied(str(exc)) from exc

    def _execute_plan_key(self, arguments: Mapping[str, Any]) -> str:
        """Return a plan identity stable across transient MCP request retries."""
        input_digest = self._digest(TASK_EXECUTE_PLAN, arguments)
        payload = f"execute-plan-v1\0{self.session_id}\0{input_digest}"
        return "execute-plan:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _execute_plan(
        self,
        arguments: dict[str, Any],
        idempotency_key: Optional[str],
    ) -> dict[str, Any]:
        # Hosted clients may generate a fresh transport idempotency key when the
        # previous HTTP response was lost.  Plan identity must survive that
        # retry, otherwise a completed patch/check sequence can run twice.
        del idempotency_key
        workstream = self._workstream_id(arguments)
        project = self._project_for_workstream(workstream)
        if self.task_states.load_optional(
            self.session_id,
            workstream_id=workstream,
        ) is None:
            facts, _git = self._base_facts(
                {},
                project_id=project.project_id,
                repository=Path(project.path),
            )
            self.task_states.bootstrap(
                self.session_id,
                facts,
                workstream_id=workstream,
            )
        plan_key = self._execute_plan_key(arguments)
        try:
            return self._plan_executor_for(project.project_id, workstream).execute(
                arguments,
                plan_key,
                workstream_id=workstream,
            )
        except PlanExecutionError as exc:
            return {
                "ok": False,
                "schema_version": 1,
                "error_code": exc.code,
                "error": str(exc),
                "details": exc.details,
            }

    def _run_affected(
        self,
        arguments: dict[str, Any],
        idempotency_key: Optional[str],
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise HostedBridgeAccessDenied(
                "checks.run_affected requires an idempotency key"
            )
        workstream = self._workstream_id(arguments)
        project = self._project_for_workstream(workstream)
        if self.task_states.load_optional(
            self.session_id,
            workstream_id=workstream,
        ) is None:
            facts, _git = self._base_facts(
                {},
                project_id=project.project_id,
                repository=Path(project.path),
            )
            self.task_states.bootstrap(
                self.session_id,
                facts,
                workstream_id=workstream,
            )
        engine_arguments = dict(arguments)
        engine_arguments.pop("workstream_id", None)
        try:
            return self._affected_engine_for(project.project_id, workstream).run(
                engine_arguments,
                idempotency_key,
                workstream_id=workstream,
            )
        except AffectedChecksError as exc:
            return {
                "ok": False,
                "schema_version": 1,
                "error_code": exc.code,
                "error": str(exc),
                "details": exc.details,
            }

    def _resume(self, arguments: dict[str, Any]) -> dict[str, Any]:
        unknown = set(arguments).difference({"workstream_id"})
        if unknown:
            raise HostedBridgeAccessDenied(
                f"task.resume received unknown arguments: {sorted(unknown)}"
            )
        workstream = self._workstream_id(arguments)
        state = self.task_states.load_optional(
            self.session_id,
            workstream_id=workstream,
        )
        available = list(self.task_states.list_workstreams(self.session_id))
        if state is None:
            return {
                "ok": False,
                "schema_version": 1,
                "workstream_id": workstream or "default",
                "available_workstreams": available,
                "error_code": "task_not_initialized",
                "error": "task state is not initialized for this workstream",
                "next_safe_action": "call karox.task.bootstrap for this workstream before task.status or task.resume",
            }
        project = self._project_for_workstream(workstream)
        git = self._git_snapshot(Path(project.path))
        stored_branch = state.facts.get("branch")
        stored_revision = state.facts.get("repository_revision")
        mismatches: list[dict[str, Any]] = []
        if stored_branch is None or stored_branch.value != git["branch"]:
            mismatches.append(
                {
                    "fact": "branch",
                    "stored": stored_branch.value if stored_branch else None,
                    "current": git["branch"],
                }
            )
        if stored_revision is None or stored_revision.value != git["revision"]:
            mismatches.append(
                {
                    "fact": "repository_revision",
                    "stored": stored_revision.value if stored_revision else None,
                    "current": git["revision"],
                }
            )
        return {
            "ok": True,
            "schema_version": 1,
            "workstream_id": workstream or "default",
            "available_workstreams": available,
            "task": state.compact(),
            "freshness": {
                "current": not mismatches,
                "mismatches": mismatches,
                "dirty": git["dirty"],
                "dirty_count": git["dirty_count"],
            },
            "next_safe_action": (
                "refresh task.bootstrap before mutating because repository facts are stale"
                if mismatches
                else state.facts.get("next_safe_action", fact(None, FactOrigin.PENDING)).value
            ),
        }
