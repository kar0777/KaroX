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
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from .affected_checks import AffectedChecksEngine, AffectedChecksError
from .artifacts import ArtifactStore
from .client_capabilities import (
    ClientCapabilityStore,
    negotiate_client_capabilities,
)
from .chatgpt_project_binding import (
    ChatGPTProjectBindingError,
    ChatGPTProjectBindingStore,
)
from .hosted_bridge import (
    AUTONOMY_TOOL_NAMES,
    CORE_TOOL_NAMES,
    DEFAULT_HOSTED_DEADLINE_SECONDS,
    HostedBridgeAccessDenied,
)
from .models import AccessProfile, Capability, Origin
from .plan_executor import PlanExecutionError, PlanExecutor
from .paths import runtime_dir
from .policy import CapabilityPolicy
from .project_registry import ProjectRegistry, ProjectRegistryError
from .proxy import ProxyToolDescriptor
from .repo_context import RepositoryContextEngine
from .repository_lease import RepositoryLeaseStore
from .security import redact
from .sessions import IdempotencyConflict, SessionBusy, SessionStore, mutation_lease_context
from .memory import KaroXMemory, MemoryError, MemoryKind, MemoryScope
from .map_service import stored_map_digest
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
INTELLIGENCE_LIST = "karox.intelligence.list"
ORCHESTRATE_RECIPES = "karox.orchestrate.recipes"
ORCHESTRATE_PLAN = "karox.orchestrate.plan"
ORCHESTRATE_START = "karox.orchestrate.start"
ORCHESTRATE_STATUS = "karox.orchestrate.status"
ORCHESTRATE_CONTROL = "karox.orchestrate.control"
MEMORY_REMEMBER = "karox.memory.remember"
MEMORY_RECALL = "karox.memory.recall"
MEMORY_CONTEXT = "karox.memory.context"
MEMORY_LIST = "karox.memory.list"
MEMORY_FORGET = "karox.memory.forget"
CHATGPT_PROJECT_BIND = "karox.chatgpt_project.bind"
CHATGPT_PROJECT_RESUME = "karox.chatgpt_project.resume"

_AUTONOMY_MUTATION_LEASE_TTL_SECONDS = 60.0
_AUTONOMY_MUTATION_LEASE_HEARTBEAT_SECONDS = 20.0
_AUTONOMY_MUTATION_LEASE_WAIT_SECONDS = 2.0
_WORKTREE_FINGERPRINT_MAX_FILES = 2048
_WORKTREE_FINGERPRINT_CONTENT_BUDGET_BYTES = 16 * 1024 * 1024
_WORKTREE_FINGERPRINT_MAX_FILE_BYTES = 2 * 1024 * 1024


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
        INTELLIGENCE_LIST,
        ORCHESTRATE_RECIPES,
        ORCHESTRATE_PLAN,
        ORCHESTRATE_START,
        ORCHESTRATE_STATUS,
        ORCHESTRATE_CONTROL,
        MEMORY_REMEMBER,
        MEMORY_RECALL,
        MEMORY_CONTEXT,
        MEMORY_LIST,
        MEMORY_FORGET,
        CHATGPT_PROJECT_BIND,
        CHATGPT_PROJECT_RESUME,
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
        if self._workstream_id is not None and (
            tool_name in CORE_TOOL_NAMES or tool_name.startswith("karox.dev_server.")
        ):
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
    INTELLIGENCE_LIST: _ToolMeta(
        description=(
            "List the secret-free KaroX 5 intelligence pool across configured API "
            "models, already-paid subscription agents, local models, and explicitly "
            "registered external agents. This is inventory only; no model is invoked."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "include_disabled": {"type": "boolean", "default": True},
            },
            "additionalProperties": False,
        },
        read_only=True,
    ),
    ORCHESTRATE_RECIPES: _ToolMeta(
        description=(
            "List built-in and user-installed data-only KaroX orchestration recipes. "
            "Listing a recipe never starts an agent or changes repository state."
        ),
        input_schema={
            "type": "object",
            "properties": {},
            "additionalProperties": False,
        },
        read_only=True,
    ),
    ORCHESTRATE_PLAN: _ToolMeta(
        description=(
            "Build a read-only KaroX orchestration plan from the current intelligence "
            "pool, verified routing evidence, quota observations, risk, and role/effort "
            "overrides. The plan selects workers but does not execute them."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 20000,
                },
                "recipe": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 128,
                    "default": "feature",
                },
                "preset": {
                    "type": "string",
                    "enum": [
                        "balanced",
                        "custom",
                        "maximum_economy",
                        "maximum_quality",
                    ],
                    "default": "balanced",
                },
                "risk": {
                    "type": "string",
                    "enum": ["low", "medium", "high", "critical"],
                    "default": "medium",
                },
                "orchestrator_endpoint_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                },
                "role_assignments": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 256,
                    },
                },
                "effort_assignments": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 64,
                    },
                },
                "run_id": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 256,
                },
            },
            "required": ["objective"],
            "additionalProperties": False,
        },
        read_only=True,
    ),
    ORCHESTRATE_START: _ToolMeta(
        description=(
            "Start a durable guarded KaroX multi-agent run for this bound ChatGPT Project. "
            "Implementers are always isolated in KaroX worktrees and the bridge's approved "
            "verification commands are reused; the client cannot disable either guard."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "binding": {"type": "string", "minLength": 20, "maxLength": 96},
                "objective": {"type": "string", "minLength": 1, "maxLength": 20000},
                "recipe": {"type": "string", "minLength": 1, "maxLength": 128},
                "preset": {"type": "string", "enum": ["balanced", "custom", "maximum_economy", "maximum_quality"]},
                "risk": {"type": "string", "enum": ["low", "medium", "high", "critical"]},
                "run_id": {"type": "string", "minLength": 1, "maxLength": 100},
                "orchestrator_endpoint_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "role_assignments": {"type": "object", "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 256}},
                "effort_assignments": {"type": "object", "additionalProperties": {"type": "string", "minLength": 1, "maxLength": 64}},
                "max_steps": {"type": "integer", "minimum": 1, "maximum": 200},
                "max_seconds": {"type": "number", "minimum": 1, "maximum": 7200},
            },
            "required": ["binding", "objective"],
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.PROCESS_RUN,
    ),
    ORCHESTRATE_STATUS: _ToolMeta(
        description=(
            "Return a compact status snapshot for a guarded background multi-agent run owned "
            "by this KaroX repository and bound ChatGPT Project."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "binding": {"type": "string", "minLength": 20, "maxLength": 96},
                "run_id": {"type": "string", "minLength": 1, "maxLength": 100},
            },
            "required": ["binding", "run_id"],
            "additionalProperties": False,
        },
        read_only=True,
    ),
    ORCHESTRATE_CONTROL: _ToolMeta(
        description=(
            "Pause, resume, stop, or steer a guarded background multi-agent run through "
            "Mission Control safe boundaries. This never kills an arbitrary PID."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "binding": {"type": "string", "minLength": 20, "maxLength": 96},
                "run_id": {"type": "string", "minLength": 1, "maxLength": 100},
                "command": {"type": "string", "enum": ["pause", "resume", "stop", "steer"]},
                "target": {"type": "string", "minLength": 1, "maxLength": 128},
                "text": {"type": "string", "maxLength": 4000},
            },
            "required": ["binding", "run_id", "command"],
            "additionalProperties": False,
        },
        read_only=False,
        capability=Capability.PROCESS_RUN,
    ),
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
    CHATGPT_PROJECT_BIND: _ToolMeta(
        description=(
            "Bind this durable KaroX session to one ChatGPT Project and return a short "
            "instruction capsule to paste into that ChatGPT Project. The binding is a "
            "scope marker for accidental cross-project use, not an authentication secret."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "project_name": {"type": "string", "minLength": 1, "maxLength": 120},
                "rotate": {"type": "boolean"},
            },
            "required": ["project_name"],
            "additionalProperties": False,
        },
        read_only=False,
    ),
    CHATGPT_PROJECT_RESUME: _ToolMeta(
        description=(
            "Verify the ChatGPT Project binding and return a compact continuation capsule "
            "covering the durable KaroX session and parallel workstreams, so a new chat or "
            "reconnect can continue without replaying large tool outputs."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "binding": {"type": "string", "minLength": 20, "maxLength": 96}
            },
            "required": ["binding"],
            "additionalProperties": False,
        },
        read_only=True,
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
            "Resume a task from authoritative state, self-heal missing or stale recovery metadata, "
            "and compare branch/revision/working-tree evidence before recommending a safe action."
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
        description=(
            "Return authoritative task state with provenance, using the same bounded "
            "reconnect/staleness self-heal as task.resume when recovery metadata drifted."
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
    TASK_WORKSTREAMS: _ToolMeta(
        description=(
            "List compact read-only summaries of parallel task workstreams so one chat can "
            "coordinate without loading sibling contexts. Large sessions return the most "
            "recent lanes first within a bounded limit (24 by default, up to 100) and report "
            "whether older lanes were omitted."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "include_default": {"type": "boolean", "default": True},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 24},
            },
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
        self.chatgpt_project = ChatGPTProjectBindingStore(sessions, session_id)
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
        if tool_name == INTELLIGENCE_LIST:
            return self._intelligence_list(arguments)
        if tool_name == ORCHESTRATE_RECIPES:
            return self._orchestration_recipes(arguments)
        if tool_name == ORCHESTRATE_PLAN:
            return self._orchestration_plan(arguments)
        if tool_name == ORCHESTRATE_START:
            return self._mutating(
                tool_name,
                arguments,
                idempotency_key,
                lambda value: self._orchestration_start(value, idempotency_key),
                reconcile_pending=lambda value: self._orchestration_start(
                    value, idempotency_key, reconcile_pending=True
                ),
            )
        if tool_name == ORCHESTRATE_STATUS:
            return self._orchestration_status(arguments)
        if tool_name == ORCHESTRATE_CONTROL:
            return self._mutating(
                tool_name,
                arguments,
                idempotency_key,
                lambda value: self._orchestration_control(value, idempotency_key),
                reconcile_pending=lambda value: self._orchestration_control(
                    value, idempotency_key
                ),
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
        if tool_name == CHATGPT_PROJECT_BIND:
            return self._chatgpt_project_bind(arguments)
        if tool_name == CHATGPT_PROJECT_RESUME:
            return self._chatgpt_project_resume(arguments)
        raise HostedBridgeAccessDenied(f"autonomy tool has no handler: {tool_name}")

    def _chatgpt_project_bind(self, arguments: dict[str, Any]) -> dict[str, Any]:
        unknown = set(arguments).difference({"project_name", "rotate"})
        if unknown:
            raise HostedBridgeAccessDenied(
                f"chatgpt_project.bind received unknown arguments: {sorted(unknown)}"
            )
        rotate = arguments.get("rotate", False)
        if not isinstance(rotate, bool):
            raise HostedBridgeAccessDenied("rotate must be a boolean")
        try:
            binding = self.chatgpt_project.bind(arguments.get("project_name", ""), rotate=rotate)
        except ChatGPTProjectBindingError as exc:
            raise HostedBridgeAccessDenied(str(exc)) from exc
        return {
            "ok": True,
            "schema_version": 1,
            "project_name": binding.project_name,
            "binding": binding.binding_id,
            "binding_is_authentication": False,
            "instruction_capsule": binding.instruction_capsule(),
            "next_safe_action": (
                "paste instruction_capsule into this ChatGPT Project instructions, then call "
                "karox.chatgpt_project.resume with the binding"
            ),
        }

    def _chatgpt_project_resume(self, arguments: dict[str, Any]) -> dict[str, Any]:
        unknown = set(arguments).difference({"binding"})
        if unknown:
            raise HostedBridgeAccessDenied(
                f"chatgpt_project.resume received unknown arguments: {sorted(unknown)}"
            )
        try:
            payload = self.chatgpt_project.compact_snapshot(arguments.get("binding", ""))
        except ChatGPTProjectBindingError as exc:
            raise HostedBridgeAccessDenied(str(exc)) from exc
        # Active/recent subagent runs are part of project continuity too. A new
        # ChatGPT chat sees them without replaying Mission Control history.
        payload["orchestration_runs"] = self._owned_orchestration_runs(limit=8)
        payload["continuation"]["orchestration_run_count"] = len(
            payload["orchestration_runs"]
        )
        payload["active_jobs"] = self._active_durable_jobs()
        payload["continuation"]["active_job_count"] = payload["active_jobs"]["count"]
        return payload

    # -- orchestration discovery/planning ------------------------------------

    @staticmethod
    def _short_mapping(
        value: Any,
        *,
        label: str,
        value_limit: int,
    ) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise HostedBridgeAccessDenied(f"{label} must be an object")
        if len(value) > 100:
            raise HostedBridgeAccessDenied(f"{label} has too many entries")
        result: dict[str, str] = {}
        for raw_key, raw_value in value.items():
            if (
                not isinstance(raw_key, str)
                or not raw_key.strip()
                or len(raw_key) > 256
            ):
                raise HostedBridgeAccessDenied(
                    f"{label} keys must be short non-empty strings"
                )
            if (
                not isinstance(raw_value, str)
                or not raw_value.strip()
                or len(raw_value) > value_limit
                or any(char in raw_value for char in "\r\n\x00")
            ):
                raise HostedBridgeAccessDenied(
                    f"{label} values must be short non-empty strings"
                )
            result[raw_key.strip()] = raw_value.strip()
        return result

    def _intelligence_list(self, arguments: dict[str, Any]) -> dict[str, Any]:
        unknown = set(arguments).difference({"include_disabled"})
        if unknown:
            raise HostedBridgeAccessDenied(
                f"intelligence.list received unknown arguments: {sorted(unknown)}"
            )
        include_disabled = arguments.get("include_disabled", True)
        if not isinstance(include_disabled, bool):
            raise HostedBridgeAccessDenied("include_disabled must be a boolean")

        from .intelligence_pool import IntelligencePool, IntelligencePoolError

        try:
            endpoints = IntelligencePool().list(include_disabled=include_disabled)
        except IntelligencePoolError as exc:
            return {
                "ok": False,
                "schema_version": 1,
                "error_code": "intelligence_pool_unavailable",
                "error": str(redact(str(exc))),
            }
        return {
            "ok": True,
            "schema_version": 1,
            "count": len(endpoints),
            "endpoints": [dict(redact(item.to_dict())) for item in endpoints],
        }

    def _orchestration_recipes(self, arguments: dict[str, Any]) -> dict[str, Any]:
        if arguments:
            raise HostedBridgeAccessDenied("orchestrate.recipes takes no arguments")

        from .recipe_registry import RecipeRegistry, RecipeRegistryError

        try:
            recipes = RecipeRegistry().list()
        except RecipeRegistryError as exc:
            return {
                "ok": False,
                "schema_version": 1,
                "error_code": "orchestration_recipes_unavailable",
                "error": str(redact(str(exc))),
            }
        return {
            "ok": True,
            "schema_version": 1,
            "count": len(recipes),
            "recipes": [dict(redact(item.to_dict())) for item in recipes],
        }

    def _orchestration_plan(self, arguments: dict[str, Any]) -> dict[str, Any]:
        allowed = {
            "objective",
            "recipe",
            "preset",
            "risk",
            "orchestrator_endpoint_id",
            "role_assignments",
            "effort_assignments",
            "run_id",
        }
        unknown = set(arguments).difference(allowed)
        if unknown:
            raise HostedBridgeAccessDenied(
                f"orchestrate.plan received unknown arguments: {sorted(unknown)}"
            )

        objective = arguments.get("objective")
        if (
            not isinstance(objective, str)
            or not objective.strip()
            or len(objective) > 20000
        ):
            raise HostedBridgeAccessDenied(
                "objective must be a non-empty string up to 20000 characters"
            )

        recipe_name = arguments.get("recipe", "feature")
        preset = arguments.get("preset", "balanced")
        risk = arguments.get("risk", "medium")
        for label, value, limit in (
            ("recipe", recipe_name, 128),
            ("preset", preset, 64),
            ("risk", risk, 64),
        ):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > limit
            ):
                raise HostedBridgeAccessDenied(
                    f"{label} must be a short non-empty string"
                )

        orchestrator_endpoint_id = arguments.get("orchestrator_endpoint_id")
        if orchestrator_endpoint_id is not None and (
            not isinstance(orchestrator_endpoint_id, str)
            or not orchestrator_endpoint_id.strip()
            or len(orchestrator_endpoint_id) > 256
        ):
            raise HostedBridgeAccessDenied("orchestrator_endpoint_id is invalid")

        run_id = arguments.get("run_id")
        if run_id is not None and (
            not isinstance(run_id, str)
            or not run_id.strip()
            or len(run_id) > 256
        ):
            raise HostedBridgeAccessDenied("run_id is invalid")

        role_assignments = self._short_mapping(
            arguments.get("role_assignments"),
            label="role_assignments",
            value_limit=256,
        )
        raw_efforts = self._short_mapping(
            arguments.get("effort_assignments"),
            label="effort_assignments",
            value_limit=64,
        )

        from .effort import normalize_effort
        from .intelligence_pool import IntelligencePool, IntelligencePoolError
        from .orchestration_routing import (
            RoutingError,
            RoutingTelemetry,
            VerifiedSmartRouter,
        )
        from .orchestrator import (
            OrchestrationError,
            OrchestrationPolicy,
            Orchestrator,
        )
        from .quota_brain import QuotaBrain
        from .risk_engine import RiskLevel

        try:
            effort_assignments = {
                name: normalize_effort(value)
                for name, value in raw_efforts.items()
            }
            pool = IntelligencePool()
            telemetry = RoutingTelemetry()
            router = VerifiedSmartRouter(
                pool=pool,
                telemetry=telemetry,
                quota_brain=QuotaBrain(),
            )
            plan = Orchestrator(
                pool=pool,
                router=router,
                telemetry=telemetry,
            ).plan(
                objective=objective.strip(),
                recipe_name=recipe_name.strip(),
                risk_level=RiskLevel(risk.strip()),
                policy=OrchestrationPolicy.from_preset(preset.strip()),
                orchestrator_endpoint_id=(
                    orchestrator_endpoint_id.strip()
                    if isinstance(orchestrator_endpoint_id, str)
                    else None
                ),
                role_assignments=role_assignments,
                effort_assignments=effort_assignments,
                run_id=run_id.strip() if isinstance(run_id, str) else None,
            )
        except (
            IntelligencePoolError,
            OrchestrationError,
            RoutingError,
            KeyError,
            ValueError,
        ) as exc:
            return {
                "ok": False,
                "schema_version": 1,
                "error_code": "orchestration_plan_rejected",
                "error": str(redact(str(exc))),
            }

        return {
            "ok": True,
            "schema_version": 1,
            "executed": False,
            "plan": dict(redact(plan.to_dict())),
        }

    @staticmethod
    def _hosted_run_id(
        session_id: str,
        requested: Any,
        *,
        stable_key: Optional[str] = None,
    ) -> str:
        """Return a globally collision-resistant Mission Control run id.

        ChatGPT Projects do not supply a trusted conversation/project id over MCP,
        so KaroX namespaces hosted run ids by the durable KaroX session. A friendly
        caller-provided suffix is preserved when possible, while the real run id
        stays safe for the global Mission Control namespace.
        """

        prefix = f"web-{hashlib.sha256(session_id.encode('utf-8')).hexdigest()[:10]}-"
        if requested is None:
            if stable_key:
                stable_material = f"{session_id}\0{stable_key}".encode("utf-8")
                suffix = hashlib.sha256(stable_material).hexdigest()[:20]
            else:
                suffix = uuid.uuid4().hex[:20]
        else:
            if not isinstance(requested, str):
                raise HostedBridgeAccessDenied("run_id must be text")
            suffix = requested.strip()
            if (
                not suffix
                or len(suffix) > 64
                or any(
                    char
                    not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                    for char in suffix
                )
            ):
                raise HostedBridgeAccessDenied(
                    "run_id must contain 1-64 letters, digits, '-' or '_'"
                )
        return prefix + suffix

    def _require_owned_orchestration_run(self, binding_id: Any, run_id: Any) -> tuple[Any, Any]:
        """Resolve one background run only after project + session ownership checks."""

        try:
            self.chatgpt_project.require(binding_id)
        except ChatGPTProjectBindingError as exc:
            raise HostedBridgeAccessDenied(str(exc)) from exc
        if not isinstance(run_id, str) or not run_id.strip():
            raise HostedBridgeAccessDenied("run_id is required")

        from .background_orchestration import (
            BackgroundOrchestrationError,
            BackgroundOrchestrationRegistry,
        )
        from .mission_control import MissionControlError, MissionControlStore

        registry = BackgroundOrchestrationRegistry()
        try:
            record = registry.get(run_id.strip())
        except BackgroundOrchestrationError as exc:
            raise HostedBridgeAccessDenied(f"unknown hosted orchestration run: {run_id}") from exc

        try:
            recorded_repo = Path(record.repository).expanduser().resolve(strict=True)
        except OSError as exc:
            raise HostedBridgeAccessDenied("hosted orchestration repository no longer exists") from exc
        if os.path.normcase(str(recorded_repo)) != os.path.normcase(str(self.repository)):
            raise HostedBridgeAccessDenied("hosted orchestration run belongs to another repository")

        session = self.sessions.load(self.session_id)
        mission = MissionControlStore(record.run_id)
        try:
            mission.require_owner(
                session_id=self.session_id,
                repo_fingerprint=session.repo_fingerprint,
            )
        except MissionControlError as exc:
            raise HostedBridgeAccessDenied(
                "hosted orchestration run belongs to another KaroX session"
            ) from exc
        return registry, record

    @staticmethod
    def _orchestration_recovery_hint(run_id: str, *, alive: bool) -> dict[str, Any]:
        """Return read-only crash/retry guidance from the durable journal."""

        try:
            from .orchestration_recovery import OrchestrationJournal

            recovery = OrchestrationJournal(run_id).snapshot()
        except Exception as exc:
            return {
                "available": False,
                "error": str(redact(type(exc).__name__)),
            }
        running = [item.step_id for item in recovery.steps if item.status == "running"]
        pending = [
            item.step_id
            for item in recovery.steps
            if item.status in {"pending", "failed", "reconcile_required"}
        ]
        requires_reconciliation = (not alive) and bool(running)
        return {
            "available": True,
            "running_at_last_journal": running,
            "pending_or_retryable": pending,
            "completed": list(recovery.completed),
            "requires_reconciliation": requires_reconciliation,
            "safe_against_blind_replay": True,
            "next_safe_action": (
                "run is alive; poll status or steer it"
                if alive
                else (
                    "reconcile the interrupted running step before any retry"
                    if requires_reconciliation
                    else "no in-flight journaled mutation remains; inspect evidence before restarting"
                )
            ),
        }

    @staticmethod
    def _compact_orchestration_view(view: Any) -> dict[str, Any]:
        snapshot = view.snapshot
        agents: list[dict[str, Any]] = []
        if snapshot is not None:
            for item in snapshot.agents[:32]:
                activity = " ".join(str(redact(item.activity)).split())
                if len(activity) > 320:
                    activity = activity[:319] + "…"
                agents.append(
                    {
                        "step_id": item.step_id,
                        "role": item.role,
                        "endpoint_id": item.endpoint_id,
                        "status": item.status,
                        "activity": activity,
                        "evidence_count": item.evidence_count,
                        "total_tokens": item.total_tokens,
                        "cost_usd": item.cost_usd,
                        "updated_at": item.updated_at,
                    }
                )
        return {
            "run_id": view.record.run_id,
            "status": view.status,
            "alive": view.alive,
            "identity_proven": view.identity_proven,
            "recipe": view.record.recipe,
            "preset": view.record.preset,
            "label": " ".join(str(redact(view.record.label)).split())[:500],
            "progress_percent": None if snapshot is None else snapshot.progress_percent,
            "completed_agents": 0 if snapshot is None else snapshot.completed_agents,
            "running_agents": 0 if snapshot is None else snapshot.running_agents,
            "total_agents": 0 if snapshot is None else snapshot.total_agents,
            "actual_cost_usd": 0.0 if snapshot is None else snapshot.actual_cost_usd,
            "total_tokens": 0 if snapshot is None else snapshot.total_tokens,
            "context_reused_chars": 0 if snapshot is None else snapshot.context_reused_chars,
            "agents": agents,
            "recovery": AutonomyRuntime._orchestration_recovery_hint(
                view.record.run_id, alive=view.alive
            ),
            "updated_at": view.record.started_at if snapshot is None else snapshot.updated_at,
        }

    def _owned_orchestration_runs(self, *, limit: int = 8) -> list[dict[str, Any]]:
        """List only runs cryptographically bound to this durable KaroX session."""

        from .background_orchestration import BackgroundOrchestrationRegistry
        from .mission_control import MissionControlError, MissionControlStore

        session = self.sessions.load(self.session_id)
        registry = BackgroundOrchestrationRegistry()
        result: list[dict[str, Any]] = []
        for record in registry.list_records():
            if len(result) >= max(0, limit):
                break
            try:
                recorded_repo = Path(record.repository).expanduser().resolve(strict=True)
                if os.path.normcase(str(recorded_repo)) != os.path.normcase(str(self.repository)):
                    continue
                MissionControlStore(record.run_id).require_owner(
                    session_id=self.session_id,
                    repo_fingerprint=session.repo_fingerprint,
                )
                result.append(self._compact_orchestration_view(registry.view(record.run_id)))
            except (OSError, MissionControlError, ValueError):
                continue
        return result

    def _orchestration_start(
        self,
        arguments: dict[str, Any],
        idempotency_key: Optional[str],
        *,
        reconcile_pending: bool = False,
    ) -> dict[str, Any]:
        allowed = {
            "binding",
            "objective",
            "recipe",
            "preset",
            "risk",
            "run_id",
            "orchestrator_endpoint_id",
            "role_assignments",
            "effort_assignments",
            "max_steps",
            "max_seconds",
        }
        unknown = set(arguments).difference(allowed)
        if unknown:
            raise HostedBridgeAccessDenied(
                f"orchestrate.start received unknown arguments: {sorted(unknown)}"
            )
        try:
            binding = self.chatgpt_project.require(arguments.get("binding", ""))
        except ChatGPTProjectBindingError as exc:
            raise HostedBridgeAccessDenied(str(exc)) from exc

        objective = arguments.get("objective")
        if not isinstance(objective, str) or not objective.strip() or len(objective) > 20000:
            raise HostedBridgeAccessDenied(
                "objective must be a non-empty string up to 20000 characters"
            )
        objective = objective.strip()
        recipe = arguments.get("recipe", "feature")
        preset = arguments.get("preset", "balanced")
        risk = arguments.get("risk", "medium")
        for label, value, limit in (
            ("recipe", recipe, 128),
            ("preset", preset, 64),
            ("risk", risk, 64),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > limit:
                raise HostedBridgeAccessDenied(f"{label} must be a short non-empty string")
        recipe = recipe.strip()
        preset = preset.strip()
        risk = risk.strip()

        orchestrator_endpoint_id = arguments.get("orchestrator_endpoint_id")
        if orchestrator_endpoint_id is not None and (
            not isinstance(orchestrator_endpoint_id, str)
            or not orchestrator_endpoint_id.strip()
            or len(orchestrator_endpoint_id) > 256
        ):
            raise HostedBridgeAccessDenied("orchestrator_endpoint_id is invalid")
        role_assignments = self._short_mapping(
            arguments.get("role_assignments"),
            label="role_assignments",
            value_limit=256,
        )
        effort_assignments = self._short_mapping(
            arguments.get("effort_assignments"),
            label="effort_assignments",
            value_limit=64,
        )
        try:
            max_steps = int(arguments.get("max_steps", 96))
            max_seconds = float(arguments.get("max_seconds", 3600.0))
        except (TypeError, ValueError) as exc:
            raise HostedBridgeAccessDenied("max_steps/max_seconds are invalid") from exc
        if not 1 <= max_steps <= 200:
            raise HostedBridgeAccessDenied("max_steps must be between 1 and 200")
        if not 1.0 <= max_seconds <= 7200.0:
            raise HostedBridgeAccessDenied("max_seconds must be between 1 and 7200")

        run_id = self._hosted_run_id(
            self.session_id,
            arguments.get("run_id"),
            stable_key=idempotency_key,
        )

        if reconcile_pending:
            try:
                existing_registry, existing_record = self._require_owned_orchestration_run(
                    binding.binding_id,
                    run_id,
                )
            except HostedBridgeAccessDenied as exc:
                if "unknown hosted orchestration run" not in str(exc):
                    raise
            else:
                view = existing_registry.view(existing_record.run_id)
                return {
                    "ok": True,
                    "schema_version": 1,
                    "project_name": binding.project_name,
                    "recovered_pending_start": True,
                    **self._compact_orchestration_view(view),
                    "guards": {
                        "implementers_isolated": True,
                        "routing_pinned_after_preflight": True,
                        "mission_control_owner_bound": True,
                        "raw_pid_control": False,
                        "blind_duplicate_launch": False,
                    },
                    "next_safe_action": "poll karox.orchestrate.status before any new launch",
                }

        from .orchestration_cli import _validate_cli_execution_plan
        from .orchestration_planning import build_orchestration_plan
        from .risk_engine import RiskLevel

        try:
            selection = build_orchestration_plan(
                repository=self.repository,
                objective=objective,
                recipe_name=recipe,
                preset=preset,
                risk_level=RiskLevel(risk),
                orchestrator_endpoint_id=(
                    orchestrator_endpoint_id.strip()
                    if isinstance(orchestrator_endpoint_id, str)
                    else None
                ),
                role_assignments=role_assignments,
                effort_assignments=effort_assignments,
                run_id=run_id,
                # ChatGPT Web is already the supervising planner. Do not spend a
                # second orchestrator turn merely to choose the same workers.
                delegate_workers=False,
            )
            _validate_cli_execution_plan(selection.plan, isolate_implementers=True)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": 1,
                "error_code": "orchestration_start_rejected",
                "error": str(redact(str(exc))),
            }

        # Pin the exact preflight plan so the detached child cannot re-route to a
        # different endpoint between this response and actual execution.
        pinned_assignments: dict[str, str] = {
            "orchestrator": selection.plan.orchestrator_endpoint.endpoint_id
        }
        pinned_efforts: dict[str, str] = {}
        planned_agents: list[dict[str, Any]] = []
        for item in selection.plan.steps:
            pinned_assignments[item.step.step_id] = item.endpoint.endpoint_id
            pinned_efforts[item.step.step_id] = item.effort_level
            planned_agents.append(
                {
                    "step_id": item.step.step_id,
                    "role": item.step.role,
                    "endpoint_id": item.endpoint.endpoint_id,
                    "effort": item.effort_level,
                }
            )

        argv = [
            "orchestrate",
            "run",
            "--repository",
            str(self.repository),
            "--objective",
            objective,
            "--recipe",
            recipe,
            "--preset",
            preset,
            "--risk",
            risk,
            "--run-id",
            run_id,
            "--no-delegate-workers",
            "--isolate-implementers",
            "--hosted-project-run",
            "--max-steps",
            str(max_steps),
            "--max-seconds",
            str(max_seconds),
            "--json",
        ]
        argv.extend(("--orchestrator", selection.plan.orchestrator_endpoint.endpoint_id))
        for name, endpoint_id in sorted(pinned_assignments.items()):
            argv.extend(("--assign", f"{name}={endpoint_id}"))
        for name, effort in sorted(pinned_efforts.items()):
            argv.extend(("--worker-effort", f"{name}={effort}"))
        for command in self.verification_commands:
            argv.extend(
                (
                    "--verification-command",
                    json.dumps(list(command), ensure_ascii=False, separators=(",", ":")),
                )
            )

        from .background_orchestration import (
            BackgroundOrchestrationError,
            BackgroundOrchestrationRegistry,
        )
        from .mission_control import MissionControlError, MissionControlStore

        session = self.sessions.load(self.session_id)
        mission = MissionControlStore(run_id)
        try:
            mission.bind_owner(
                session_id=self.session_id,
                repo_fingerprint=session.repo_fingerprint,
            )
            record = BackgroundOrchestrationRegistry().start(
                run_id=run_id,
                repository=self.repository,
                cli_argv=argv,
                recipe=recipe,
                preset=preset,
                label=f"{binding.project_name}: {objective[:180]}",
            )
        except (BackgroundOrchestrationError, MissionControlError, OSError, ValueError) as exc:
            return {
                "ok": False,
                "schema_version": 1,
                "error_code": "orchestration_start_failed",
                "error": str(redact(str(exc))),
                "run_id": run_id,
            }

        return {
            "ok": True,
            "schema_version": 1,
            "run_id": record.run_id,
            "status": "starting",
            "project_name": binding.project_name,
            "planned_agents": planned_agents[:32],
            "guards": {
                "implementers_isolated": True,
                "verification_commands": len(self.verification_commands),
                "routing_pinned_after_preflight": True,
                "mission_control_owner_bound": True,
                "raw_pid_control": False,
            },
            "next_safe_action": (
                "poll karox.orchestrate.status; use karox.orchestrate.control only for "
                "pause/resume/stop or a targeted steer"
            ),
        }

    def _orchestration_status(self, arguments: dict[str, Any]) -> dict[str, Any]:
        unknown = set(arguments).difference({"binding", "run_id"})
        if unknown:
            raise HostedBridgeAccessDenied(
                f"orchestrate.status received unknown arguments: {sorted(unknown)}"
            )
        registry, record = self._require_owned_orchestration_run(
            arguments.get("binding", ""), arguments.get("run_id", "")
        )
        try:
            view = registry.view(record.run_id)
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": 1,
                "error_code": "orchestration_status_unavailable",
                "error": str(redact(str(exc))),
                "run_id": record.run_id,
            }
        return {
            "ok": True,
            "schema_version": 1,
            **self._compact_orchestration_view(view),
        }

    def _orchestration_control(
        self,
        arguments: dict[str, Any],
        idempotency_key: Optional[str] = None,
    ) -> dict[str, Any]:
        unknown = set(arguments).difference({"binding", "run_id", "command", "target", "text"})
        if unknown:
            raise HostedBridgeAccessDenied(
                f"orchestrate.control received unknown arguments: {sorted(unknown)}"
            )
        registry, record = self._require_owned_orchestration_run(
            arguments.get("binding", ""), arguments.get("run_id", "")
        )
        command = arguments.get("command")
        if command not in {"pause", "resume", "stop", "steer"}:
            raise HostedBridgeAccessDenied("command must be pause, resume, stop, or steer")
        target = arguments.get("target", "all" if command == "steer" else "orchestrator")
        if (
            not isinstance(target, str)
            or not target
            or len(target) > 128
            or any(char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:" for char in target)
        ):
            raise HostedBridgeAccessDenied("target is invalid")
        text = arguments.get("text", "")
        if not isinstance(text, str) or len(text) > 4000:
            raise HostedBridgeAccessDenied("text must be at most 4000 characters")
        if command == "steer" and not text.strip():
            raise HostedBridgeAccessDenied("steer requires non-empty text")
        if command != "steer" and text.strip():
            raise HostedBridgeAccessDenied("text is accepted only for steer")

        try:
            view = registry.view(record.run_id)
        except Exception as exc:
            raise HostedBridgeAccessDenied(
                "cannot prove the owned orchestration process before control"
            ) from exc
        if not view.alive:
            raise HostedBridgeAccessDenied(
                "owned orchestration process is no longer alive; control was not queued"
            )
        if not view.identity_proven:
            raise HostedBridgeAccessDenied(
                "owned orchestration process identity is not proven; control was not queued"
            )

        # Once a snapshot exists, reject typos instead of silently broadcasting a
        # steer to a target that does not exist.
        if command == "steer":
            snapshot = view.snapshot
            if snapshot is not None and target not in {"all", "orchestrator"}:
                valid_targets = {
                    *(item.step_id for item in snapshot.agents),
                    *(item.role for item in snapshot.agents),
                }
                if target not in valid_targets:
                    raise HostedBridgeAccessDenied(
                        "steer target is not an active role or step; call orchestrate.status first"
                    )

        control_command_id = None
        if idempotency_key:
            material = f"{self.session_id}\0{record.run_id}\0{idempotency_key}".encode("utf-8")
            control_command_id = "cmd-" + hashlib.sha256(material).hexdigest()[:32]

        try:
            queued = registry.request(
                record.run_id,
                command,
                target=target,
                text=text.strip(),
                command_id=control_command_id,
            )
        except Exception as exc:
            return {
                "ok": False,
                "schema_version": 1,
                "error_code": "orchestration_control_failed",
                "error": str(redact(str(exc))),
                "run_id": record.run_id,
            }
        return {
            "ok": True,
            "schema_version": 1,
            "run_id": record.run_id,
            "queued": True,
            "command": command,
            "target": target,
            "command_id": queued.get("command_id"),
            "delivery": "safe orchestration boundary",
        }

    # -- project intelligence --------------------------------------------------

    def _project_fact_map_summary(
        self, project_id: str, repository: Path
    ) -> str:
        """A deterministic onboarding digest, built once and refreshed cheaply.

        Failures here must never take bootstrap down: an unreadable submodule
        or an exotic filesystem costs the digest, not the session.
        """

        try:
            # Prefer the durable semantic Project Map when one exists. It is the
            # same compact digest injected into normal agent project context, so
            # ChatGPT task.bootstrap and native runs start from one project model
            # instead of two drifting summaries. The digest labels a stale
            # revision honestly; bootstrap stays cheap and never launches a deep
            # semantic rebuild on the request path.
            stored = stored_map_digest(repository, budget_chars=1200)
            if stored is not None:
                text, _meta = stored
                if text:
                    return text

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
        reconcile_pending: Optional[
            Callable[[dict[str, Any]], dict[str, Any]]
        ] = None,
    ) -> dict[str, Any]:
        if not idempotency_key:
            raise HostedBridgeAccessDenied("mutating autonomy tools require an idempotency key")
        lease_deadline = time.monotonic() + _AUTONOMY_MUTATION_LEASE_WAIT_SECONDS
        while True:
            try:
                lease = self.sessions.acquire(
                    self.session_id,
                    f"autonomy-{tool_name}",
                    ttl_seconds=_AUTONOMY_MUTATION_LEASE_TTL_SECONDS,
                )
                break
            except SessionBusy as exc:
                if time.monotonic() >= lease_deadline:
                    raise HostedBridgeAccessDenied(
                        "mutation queue is briefly busy; no permission was lost and no "
                        "side effect was started. Retry the same idempotent request."
                    ) from exc
                time.sleep(0.05)
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
            input_digest = self._digest(tool_name, arguments)
            pending_recovery = False
            try:
                replay = self.sessions.begin_idempotent(
                    record,
                    lease,
                    idempotency_key,
                    input_digest,
                )
            except IdempotencyConflict as exc:
                entry = record.idempotency.get(idempotency_key)
                if (
                    reconcile_pending is not None
                    and isinstance(entry, Mapping)
                    and entry.get("input_digest") == input_digest
                    and entry.get("status") == "pending"
                ):
                    replay = None
                    pending_recovery = True
                else:
                    raise HostedBridgeAccessDenied(
                        "idempotency conflict, not a permission denial: a previous "
                        "attempt is recorded with different input or cannot be "
                        "safely reconciled. Use a new idempotency key only after "
                        f"inspecting the prior operation. ({exc})"
                    ) from exc
            if replay is not None:
                return {**replay, "idempotent_replay": True}
            # Nested checks/patches use the exact same fenced lease only inside
            # this synchronous execution context. A parallel hosted request does
            # not inherit the ContextVar and therefore remains blocked.
            selected_handler = reconcile_pending if pending_recovery else handler
            if selected_handler is None:  # defensive; pending_recovery implies one exists
                raise HostedBridgeAccessDenied("pending mutation cannot be reconciled")
            with mutation_lease_context(lease):
                result = selected_handler(arguments)
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
        strip_output: bool = True,
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
        return completed.stdout.strip() if strip_output else completed.stdout

    def _git_snapshot(self, repository: Optional[Path] = None) -> dict[str, Any]:
        target = (repository or self.repository).expanduser().resolve(strict=True)
        # A saved project is an allowed filesystem root, not necessarily a Git
        # repository. Never synthesize a repository (especially at C:\, D:\, or
        # a user directory) merely to satisfy bootstrap metadata.
        if not (target / ".git").exists():
            return {
                "repository_kind": "directory",
                "git_applicable": False,
                "branch": None,
                "revision": None,
                "dirty": None,
                "dirty_count": 0,
                "dirty_summary": [],
                "dirty_truncated": False,
                "working_tree_fingerprint": None,
            }
        branch = self._git("branch", "--show-current", repository=target) or "detached"
        revision = self._git(
            "rev-parse",
            "--verify",
            "HEAD",
            repository=target,
            allow_failure=True,
        ) or "unborn"
        status = self._git(
            "-c",
            "core.quotePath=false",
            "status",
            "--porcelain=v1",
            "-z",
            repository=target,
            strip_output=False,
        )
        dirty_lines, changed_paths = self._parse_git_status_z(status)
        working_tree_fingerprint = self._working_tree_fingerprint(
            target,
            branch=branch,
            revision=revision,
            status=status,
            paths=changed_paths,
        )
        return {
            "repository_kind": "git",
            "git_applicable": True,
            "branch": branch,
            "revision": revision,
            "dirty": bool(dirty_lines),
            "dirty_count": len(dirty_lines),
            "dirty_summary": dirty_lines[:50],
            "dirty_truncated": len(dirty_lines) > 50,
            "working_tree_fingerprint": working_tree_fingerprint,
        }

    @staticmethod
    def _parse_git_status_z(status: str) -> tuple[list[str], tuple[str, ...]]:
        """Parse porcelain-v1 -z without losing spaces or rename source paths."""

        summaries: list[str] = []
        paths: list[str] = []
        seen: set[str] = set()
        records = status.split("\x00")
        index = 0
        while index < len(records):
            entry = records[index]
            index += 1
            if not entry:
                continue
            if len(entry) < 3:
                summaries.append(entry)
                continue
            code = entry[:2]
            primary = entry[3:] if entry[2] == " " else entry[2:].lstrip()
            source: Optional[str] = None
            if ("R" in code or "C" in code) and index < len(records):
                source = records[index]
                index += 1
            for candidate in (primary, source):
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    paths.append(candidate)
            if source:
                summaries.append(f"{code} {source} -> {primary}")
            else:
                summaries.append(f"{code} {primary}")
        return summaries, tuple(paths)

    def _working_tree_fingerprint(
        self,
        repository: Path,
        *,
        branch: str,
        revision: str,
        status: str,
        paths: Sequence[str],
    ) -> str:
        """Return bounded evidence that changes when a dirty worktree changes."""

        digest = hashlib.sha256()
        for value in (branch, revision, status):
            digest.update(value.encode("utf-8", errors="surrogatepass"))
            digest.update(b"\0")

        # ``git status --porcelain=v1 -z`` is already the authoritative bounded
        # change inventory for this snapshot. Reuse those parsed paths instead of
        # issuing three more Git commands and accidentally disagreeing with the
        # status bytes that are part of this same fingerprint.
        root = os.path.normcase(os.path.abspath(str(repository)))
        content_budget = _WORKTREE_FINGERPRINT_CONTENT_BUDGET_BYTES
        ordered = sorted(set(paths))
        for relative in ordered[:_WORKTREE_FINGERPRINT_MAX_FILES]:
            digest.update(relative.encode("utf-8", errors="surrogatepass"))
            digest.update(b"\0")
            candidate = os.path.abspath(os.path.join(str(repository), relative))
            try:
                if os.path.commonpath((root, os.path.normcase(candidate))) != root:
                    digest.update(b"outside-root\0")
                    continue
                stat = os.lstat(candidate)
            except (OSError, ValueError):
                digest.update(b"missing\0")
                continue

            digest.update(
                f"{stat.st_mode}:{stat.st_size}:{stat.st_mtime_ns}".encode("ascii")
            )
            digest.update(b"\0")
            if os.path.islink(candidate):
                try:
                    digest.update(
                        os.readlink(candidate).encode("utf-8", errors="surrogatepass")
                    )
                except OSError:
                    digest.update(b"unreadable-link")
                digest.update(b"\0")
                continue
            if (
                not os.path.isfile(candidate)
                or stat.st_size > _WORKTREE_FINGERPRINT_MAX_FILE_BYTES
                or stat.st_size > content_budget
            ):
                continue
            try:
                with open(candidate, "rb") as handle:
                    while chunk := handle.read(1024 * 1024):
                        digest.update(chunk)
            except OSError:
                digest.update(b"unreadable\0")
            else:
                content_budget -= stat.st_size
            digest.update(b"\0")

        digest.update(f"paths:{len(ordered)}".encode("ascii"))
        if len(ordered) > _WORKTREE_FINGERPRINT_MAX_FILES:
            digest.update(b":truncated")
        return "sha256:" + digest.hexdigest()

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
        # Responses name the legacy/no-workstream task `default`. Treat an echoed
        # value as that same task instead of creating a second named default lane.
        if value == "default":
            return None
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
        git_source = "git" if git["git_applicable"] else "not_applicable.non_git"
        facts = {
            "objective": fact(objective.strip(), FactOrigin.VERIFIED, "session.task"),
            "project_id": fact(project_id, FactOrigin.VERIFIED, "project.registry"),
            "repository": fact(str(repository), FactOrigin.VERIFIED, "project.registry"),
            "branch": fact(git["branch"], FactOrigin.OBSERVED, f"{git_source}.branch"),
            "repository_revision": fact(
                git["revision"], FactOrigin.OBSERVED, f"{git_source}.revision"
            ),
            "working_tree_fingerprint": fact(
                git.get("working_tree_fingerprint"),
                FactOrigin.OBSERVED,
                f"{git_source}.working_tree_fingerprint",
            ),
            "repository_kind": fact(
                git["repository_kind"], FactOrigin.OBSERVED, "filesystem.root"
            ),
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
        existing = self.task_states.load_optional(
            self.session_id,
            workstream_id=workstream,
        )
        binding_recovery: Optional[dict[str, str]] = None
        if existing is not None:
            # A durable workstream's verified project binding is authoritative.
            # Hosted clients may replay a stale/default project_id after a bridge
            # reconnect; treating that hint as a requested rebind turned a safe
            # bootstrap refresh into an opaque permission denial.  Preserve the
            # immutable binding, while still validating that any explicit hint is
            # an approved project and reporting the recovery to the caller.
            project = self._project_for_workstream(workstream)
            requested_raw = arguments.get("project_id")
            if requested_raw is not None:
                if not isinstance(requested_raw, str):
                    raise HostedBridgeAccessDenied("project_id must be a string")
                requested_id = requested_raw.strip()
                try:
                    requested_project = self._current_project_registry().get(requested_id)
                except ProjectRegistryError as exc:
                    raise HostedBridgeAccessDenied(str(exc)) from exc
                if requested_project.project_id != project.project_id:
                    binding_recovery = {
                        "reason": "existing_workstream_binding_preserved",
                        "requested_project_id": requested_project.project_id,
                        "bound_project_id": project.project_id,
                        "recovery_action": "continued_with_saved_binding",
                    }
        else:
            project = self._project_for_workstream(
                workstream,
                arguments.get("project_id"),
            )
        repository = Path(project.path)
        effective_arguments = dict(arguments)
        if "objective" not in effective_arguments:
            existing_objective = existing.facts.get("objective") if existing is not None else None
            if existing_objective is not None:
                effective_arguments["objective"] = str(existing_objective.value)
        facts, git = self._base_facts(
            effective_arguments,
            project_id=project.project_id,
            repository=repository,
        )
        if workstream is not None:
            # Named workstreams are parallel task lanes, not aliases for the
            # session-global mutation ledger. Copying record.changed_files,
            # checks and failures into every lane made 60+ workstreams carry the
            # same large payload and falsely suggested every agent owned every
            # sibling's changes. Preserve already-scoped facts on refresh; start
            # new lanes empty while keeping verified repository/project facts.
            for name in (
                "files_changed",
                "checks_executed",
                "current_blockers",
                "pending_user_gates",
            ):
                prior = existing.facts.get(name) if existing is not None else None
                if prior is not None:
                    facts[name] = prior
                else:
                    facts[name] = fact([], FactOrigin.HISTORICAL, "workstream.initial")
            repository_fresh = False
            if existing is not None:
                prior_branch = existing.facts.get("branch")
                prior_revision = existing.facts.get("repository_revision")
                prior_worktree = existing.facts.get("working_tree_fingerprint")
                repository_fresh = (
                    prior_branch is not None
                    and prior_branch.value == git["branch"]
                    and prior_revision is not None
                    and prior_revision.value == git["revision"]
                    and prior_worktree is not None
                    and prior_worktree.value == git.get("working_tree_fingerprint")
                )
            if (
                existing is not None
                and repository_fresh
                and "next_safe_action" in existing.facts
            ):
                facts["next_safe_action"] = existing.facts["next_safe_action"]
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
            "project_binding_recovered": binding_recovery is not None,
            "binding_recovery": binding_recovery,
            "project_fact_map": self._project_fact_map_summary(
                project.project_id, repository
            ),
            "repository": str(repository),
            "repository_kind": git["repository_kind"],
            "git_applicable": git["git_applicable"],
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
            if workstream is not None:
                # The session mutation ledger belongs to all clients sharing the
                # durable session, not to this newly-created parallel lane.
                for name in (
                    "files_changed",
                    "checks_executed",
                    "current_blockers",
                    "pending_user_gates",
                ):
                    facts[name] = fact([], FactOrigin.HISTORICAL, "workstream.initial")
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
        # Status is the control-plane view agents poll most often. Reuse the
        # same bounded recovery path as resume so a reconnect cannot leave a
        # client staring at stale task metadata while the repository changed.
        return self._resume(arguments)

    def _bootstrap_missing_task_state(self, workstream: Optional[str]) -> Any:
        """Self-heal missing recovery metadata without touching repository state."""

        project = self._project_for_workstream(workstream)
        facts, _git = self._base_facts(
            {},
            project_id=project.project_id,
            repository=Path(project.path),
        )
        if workstream is not None:
            for name in (
                "files_changed",
                "checks_executed",
                "current_blockers",
                "pending_user_gates",
            ):
                facts[name] = fact([], FactOrigin.HISTORICAL, "workstream.initial")
        return self.task_states.bootstrap(
            self.session_id,
            facts,
            workstream_id=workstream,
        )

    @staticmethod
    def _repository_freshness_mismatches(
        state: Any,
        git: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        mismatches: list[dict[str, Any]] = []
        for fact_name, git_name in (
            ("branch", "branch"),
            ("repository_revision", "revision"),
            ("working_tree_fingerprint", "working_tree_fingerprint"),
        ):
            stored = state.facts.get(fact_name)
            current = git.get(git_name)
            if stored is None or stored.value != current:
                mismatches.append(
                    {
                        "fact": fact_name,
                        "stored": stored.value if stored else None,
                        "current": current,
                    }
                )
        return mismatches

    def _active_durable_jobs(
        self,
        *,
        limit: int = 8,
        workstream_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Return a bounded, secret-free continuation view of active durable jobs.

        ``None`` means project/session coordination and includes every lane. Task
        resume/status passes its concrete lane so sibling agents do not surface
        or accidentally act on each other's newly scoped durable work.
        """

        from .check_jobs import CheckJobError, CheckJobStore, effective_job_status

        final_statuses = {"passed", "failed", "cancelled", "timed_out"}
        bases = (
            ("check", runtime_dir() / "vnext" / "check-jobs"),
            ("command", runtime_dir() / "vnext" / "dev-command-jobs"),
        )
        jobs: list[dict[str, Any]] = []
        unreadable = 0
        stale = 0
        for kind, base in bases:
            store = CheckJobStore(self.session_id, root=base)
            try:
                paths = sorted(store.root.glob("job-*.json"))
            except OSError:
                unreadable += 1
                continue
            for path in paths:
                try:
                    state = store.get(path.stem)
                    scope = store.get_scope(path.stem)
                except (CheckJobError, OSError, ValueError):
                    unreadable += 1
                    continue
                job_workstream = (
                    str(scope.get("workstream_id")) if scope is not None else "default"
                )
                if workstream_id is not None and job_workstream != workstream_id:
                    continue
                effective_status, effective_error = effective_job_status(state)
                if effective_status in final_statuses:
                    if state.status not in final_statuses:
                        stale += 1
                    continue
                jobs.append(
                    {
                        "job_id": state.job_id,
                        "kind": kind,
                        "status": effective_status,
                        "updated_at": state.updated_at,
                        "command": Path(state.argv[0]).name if state.argv else None,
                        "error_code": effective_error,
                        "artifact_id": state.artifact_id,
                        "workstream_id": job_workstream,
                        "project_id": scope.get("project_id") if scope is not None else None,
                    }
                )
        jobs.sort(key=lambda item: float(item["updated_at"]), reverse=True)
        total = len(jobs)
        return {
            "count": min(total, limit),
            "total_count": total,
            "unreadable_count": unreadable,
            "stale_count": stale,
            "jobs": jobs[:limit],
            "truncated": total > limit,
        }

    def _workstream_summary(self, workstream_id: str, state: Any) -> dict[str, Any]:
        def value(name: str, default: Any = None) -> Any:
            item = state.facts.get(name)
            return item.value if item is not None else default

        def short_text(raw: Any, limit: int) -> Optional[str]:
            if raw is None:
                return None
            text = " ".join(str(redact(raw)).split())
            return text if len(text) <= limit else text[: limit - 1] + "…"

        def short_list(raw: Any, *, limit: int, item_limit: int) -> tuple[list[str], int]:
            if raw is None:
                return [], 0
            if isinstance(raw, (list, tuple, set)):
                items = list(raw)
            else:
                items = [raw]
            preview: list[str] = []
            for item in items[:limit]:
                text = " ".join(str(redact(item)).split())
                preview.append(text if len(text) <= item_limit else text[: item_limit - 1] + "…")
            return preview, len(items)

        registry = self._current_project_registry()
        default = registry.default
        project_id = value("project_id", default.project_id if default is not None else None)
        project_name: Optional[str] = None
        if isinstance(project_id, str):
            try:
                project_name = registry.get(project_id).label
            except ProjectRegistryError:
                project_name = None
        blockers_fact = state.facts.get("current_blockers")
        files_fact = state.facts.get("files_changed")
        legacy_session_fields: list[str] = []

        blockers, blockers_count = short_list(
            value("current_blockers", []), limit=4, item_limit=300
        )
        legacy_blockers_count = 0
        if (
            workstream_id != "default"
            and blockers_fact is not None
            and "session.failures" in blockers_fact.evidence
        ):
            # Early named lanes copied the session-wide failure ledger at creation.
            # Keep that evidence in task.status for audit, but do not present it as
            # lane-owned coordination state where sibling agents could act on it.
            legacy_blockers_count = blockers_count
            blockers, blockers_count = [], 0
            legacy_session_fields.append("current_blockers")

        files, files_count = short_list(
            value("files_changed", []), limit=8, item_limit=240
        )
        legacy_files_count = 0
        if (
            workstream_id != "default"
            and files_fact is not None
            and "session.changed_files" in files_fact.evidence
        ):
            # Same migration rule as blockers: old snapshots are truthful session
            # history, but false attribution when shown as one lane's own changes.
            legacy_files_count = files_count
            files, files_count = [], 0
            legacy_session_fields.append("files_changed")

        return {
            "project_id": project_id,
            "project_name": project_name,
            "workstream_id": workstream_id,
            "task_id": state.task_id,
            "revision": state.revision,
            "updated_at": state.updated_at,
            "objective": short_text(value("objective"), 800),
            "current_phase": short_text(value("current_phase"), 240),
            "current_blockers": blockers,
            "current_blockers_count": blockers_count,
            "current_blockers_truncated": blockers_count > len(blockers),
            "legacy_session_blockers_count": legacy_blockers_count,
            "next_safe_action": short_text(value("next_safe_action"), 800),
            "files_changed": files,
            "files_changed_count": files_count,
            "files_changed_truncated": files_count > len(files),
            "legacy_session_files_changed_count": legacy_files_count,
            "legacy_session_snapshot_fields": legacy_session_fields,
        }

    def _workstreams(self, arguments: dict[str, Any]) -> dict[str, Any]:
        unknown = set(arguments).difference({"include_default", "limit"})
        if unknown:
            raise HostedBridgeAccessDenied(
                f"task.workstreams received unknown arguments: {sorted(unknown)}"
            )
        include_default = arguments.get("include_default", True)
        if not isinstance(include_default, bool):
            raise HostedBridgeAccessDenied("include_default must be a boolean")

        limit = arguments.get("limit", 24)
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise HostedBridgeAccessDenied("limit must be an integer between 1 and 100")

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
        total_count = len(summaries)
        if total_count > limit:
            default_summary = next(
                (item for item in summaries if item["workstream_id"] == "default"),
                None,
            )
            named = [item for item in summaries if item["workstream_id"] != "default"]
            named.sort(
                key=lambda item: (item["updated_at"], str(item["workstream_id"])),
                reverse=True,
            )
            slots = limit - (1 if default_summary is not None else 0)
            selected_named = named[: max(0, slots)]
            selected_named.sort(key=lambda item: str(item["workstream_id"]))
            summaries = ([default_summary] if default_summary is not None else []) + selected_named

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
            "total_count": total_count,
            "limit": limit,
            "truncated": total_count > len(summaries),
            "omitted_count": total_count - len(summaries),
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
            self._bootstrap_missing_task_state(workstream)

        # Canonicalize the public legacy-default alias before deriving plan
        # identity. A retry that first omitted workstream_id and later echoes the
        # returned `default` label must address the same journal and operation
        # idempotency keys, not execute the same side effects twice.
        plan_arguments = dict(arguments)
        if workstream is None:
            plan_arguments.pop("workstream_id", None)
        else:
            plan_arguments["workstream_id"] = workstream
        plan_key = self._execute_plan_key(plan_arguments)
        try:
            return self._plan_executor_for(project.project_id, workstream).execute(
                plan_arguments,
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
            self._bootstrap_missing_task_state(workstream)
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
        auto_bootstrapped = False
        if state is None:
            state = self._bootstrap_missing_task_state(workstream)
            auto_bootstrapped = True

        project = self._project_for_workstream(workstream)
        git = self._git_snapshot(Path(project.path))
        mismatches = self._repository_freshness_mismatches(state, git)
        previous_mismatches = list(mismatches)
        recovered_stale_state = False
        if mismatches:
            refresh_arguments: dict[str, Any] = {}
            if workstream is not None:
                refresh_arguments["workstream_id"] = workstream
            self._bootstrap(refresh_arguments)
            state = self.task_states.load(
                self.session_id,
                workstream_id=workstream,
            )
            git = self._git_snapshot(Path(project.path))
            mismatches = self._repository_freshness_mismatches(state, git)
            recovered_stale_state = not mismatches

        available = list(self.task_states.list_workstreams(self.session_id))
        active_jobs = self._active_durable_jobs(workstream_id=workstream or "default")
        return {
            "ok": True,
            "schema_version": 1,
            "workstream_id": workstream or "default",
            "available_workstreams": available,
            "task": state.compact(),
            "auto_bootstrapped": auto_bootstrapped,
            "freshness": {
                "current": not mismatches,
                "mismatches": mismatches,
                "recovered": auto_bootstrapped or recovered_stale_state,
                "previous_mismatches": previous_mismatches,
                "dirty": git["dirty"],
                "dirty_count": git["dirty_count"],
            },
            "active_jobs": active_jobs,
            "next_safe_action": (
                "inspect current diff and sibling workstreams before mutating because "
                "repository state is changing concurrently"
                if mismatches
                else (
                    "reconcile the active durable job(s) before starting duplicate work"
                    if active_jobs["count"]
                    else state.facts.get(
                        "next_safe_action",
                        fact(None, FactOrigin.PENDING),
                    ).value
                )
            ),
        }
