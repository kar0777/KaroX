"""Bounded, journaled autonomous execution plans for hosted coding clients."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol, Sequence

from mcp.types import CallToolResult

from .artifacts import ArtifactStore
from .cost_intelligence import BatchPlanner
from .evidence_packets import (
    ArtifactRef as EvidenceArtifactRef,
    Fidelity,
    packet_from_plan_result,
)
from .repository_lease import (
    RepositoryLease,
    RepositoryLeaseConflict,
    RepositoryLeaseError,
    RepositoryLeaseStore,
)
from .result_envelope import artifact_backed_result
from .security import redact
from .sessions import _atomic_json, _exclusive_file_lock
from .task_state import FactOrigin, TaskStateStore, fact
from .workspace_change_guard import WorkspaceChangeGuard, start_workspace_change_guard

PLAN_SCHEMA_VERSION = 1
PLAN_ACTIONS = frozenset(
    {
        "inspect",
        "search",
        "read",
        "patch",
        "command",
        "checks",
        "dev_server",
        "browser",
        "runtime",
        "checkpoint",
    }
)
_READ_ONLY_ACTIONS = frozenset({"inspect", "search", "read"})
# Plan actions expressed as the core tool names BatchPlanner classifies. The
# planner only groups read-only work; every other action stays sequential.
_BATCH_TOOL_NAMES = {
    "read": "repo.read_file",
    "search": "repo.search",
    "inspect": "repo.list_files",
}
_REPOSITORY_LEASE_ACTIONS = frozenset(
    {"patch", "command", "checks", "dev_server", "browser"}
)


class PlanExecutionError(RuntimeError):
    def __init__(self, code: str, message: str, details: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = details or {}


class ToolRuntime(Protocol):
    def descriptors(self) -> Sequence[Any]: ...

    def execute(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = 30.0,
    ) -> dict[str, Any] | CallToolResult: ...


@dataclass(frozen=True)
class PlanBudgets:
    max_steps: int = 20
    max_wall_time: float = 300.0
    max_inline_output_bytes: int = 32 * 1024
    max_artifact_bytes: int = 25 * 1024 * 1024
    max_write_operations: int = 10
    max_command_runs: int = 10
    max_browser_actions: int = 10
    max_fix_attempts: int = 3
    checkpoint_after_steps: int = 5

    @classmethod
    def parse(cls, payload: Any) -> "PlanBudgets":
        if payload is None:
            return cls()
        if not isinstance(payload, Mapping):
            raise PlanExecutionError("invalid_plan", "budgets must be an object")
        allowed = {
            "max_steps",
            "max_wall_time",
            "max_inline_output_bytes",
            "max_artifact_bytes",
            "max_write_operations",
            "max_command_runs",
            "max_browser_actions",
            "max_fix_attempts",
            "checkpoint_after_steps",
        }
        unknown = set(payload).difference(allowed)
        if unknown:
            raise PlanExecutionError("invalid_plan", f"unknown budget fields: {sorted(unknown)}")
        values = {
            "max_steps": (1, 100),
            "max_wall_time": (1, 3600),
            "max_inline_output_bytes": (4096, 1024 * 1024),
            "max_artifact_bytes": (1024, 100 * 1024 * 1024),
            "max_write_operations": (0, 50),
            "max_command_runs": (0, 50),
            "max_browser_actions": (0, 100),
            "max_fix_attempts": (0, 10),
            "checkpoint_after_steps": (1, 50),
        }
        parsed: dict[str, Any] = {}
        defaults = cls()
        for name, (minimum, maximum) in values.items():
            value = payload.get(name, getattr(defaults, name))
            if isinstance(getattr(defaults, name), float):
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise PlanExecutionError("invalid_plan", f"{name} must be numeric")
                number: Any = float(value)
            else:
                if isinstance(value, bool) or not isinstance(value, int):
                    raise PlanExecutionError("invalid_plan", f"{name} must be an integer")
                number = value
            if number < minimum or number > maximum:
                raise PlanExecutionError(
                    "invalid_plan", f"{name} must be between {minimum} and {maximum}"
                )
            parsed[name] = number
        return cls(**parsed)


@dataclass(frozen=True)
class PlanOperation:
    operation_id: str
    action: str
    inputs: dict[str, Any]
    depends_on: tuple[str, ...]
    preconditions: dict[str, Any]
    expected_outcome: dict[str, Any]
    on_failure: str
    idempotency_key: str
    output_policy: str

    @classmethod
    def parse(cls, payload: Any, index: int, plan_key: str) -> "PlanOperation":
        if not isinstance(payload, Mapping):
            raise PlanExecutionError("invalid_plan", f"operations[{index}] must be an object")
        allowed = {
            "operation_id",
            "action",
            "inputs",
            "depends_on",
            "preconditions",
            "expected_outcome",
            "on_failure",
            "idempotency_key",
            "output_policy",
        }
        unknown = set(payload).difference(allowed)
        if unknown:
            raise PlanExecutionError(
                "invalid_plan", f"operations[{index}] has unsupported fields: {sorted(unknown)}"
            )
        operation_id = payload.get("operation_id")
        action = payload.get("action")
        inputs = payload.get("inputs", {})
        depends_on = payload.get("depends_on", [])
        preconditions = payload.get("preconditions", {})
        expected = payload.get("expected_outcome", {})
        on_failure = payload.get("on_failure", "stop")
        output_policy = payload.get("output_policy", "summary")
        if not isinstance(operation_id, str) or not operation_id or len(operation_id) > 128:
            raise PlanExecutionError("invalid_plan", f"operations[{index}].operation_id is invalid")
        if action not in PLAN_ACTIONS:
            raise PlanExecutionError("invalid_plan", f"operations[{index}].action is invalid")
        if not isinstance(inputs, dict):
            raise PlanExecutionError("invalid_plan", f"operations[{index}].inputs must be object")
        if not isinstance(depends_on, list) or not all(isinstance(item, str) for item in depends_on):
            raise PlanExecutionError("invalid_plan", f"operations[{index}].depends_on must be strings")
        if not isinstance(preconditions, dict) or not isinstance(expected, dict):
            raise PlanExecutionError("invalid_plan", "preconditions and expected_outcome must be objects")
        if on_failure not in {"stop", "continue", "skip_dependents"}:
            raise PlanExecutionError("invalid_plan", f"operations[{index}].on_failure is invalid")
        if output_policy not in {"summary", "inline", "artifact", "evidence"}:
            raise PlanExecutionError("invalid_plan", f"operations[{index}].output_policy is invalid")
        supplied_key = payload.get("idempotency_key")
        if supplied_key is not None and (
            not isinstance(supplied_key, str) or not supplied_key or len(supplied_key) > 256
        ):
            raise PlanExecutionError("invalid_plan", f"operations[{index}].idempotency_key is invalid")
        stable_key = supplied_key or hashlib.sha256(
            f"{plan_key}\0{operation_id}".encode("utf-8")
        ).hexdigest()
        return cls(
            operation_id=operation_id,
            action=str(action),
            inputs=dict(inputs),
            depends_on=tuple(depends_on),
            preconditions=dict(preconditions),
            expected_outcome=dict(expected),
            on_failure=str(on_failure),
            idempotency_key=stable_key,
            output_policy=str(output_policy),
        )


class PlanJournalStore:
    def __init__(self, session_directory: Path) -> None:
        self.root = session_directory / "plans"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _name(plan_key: str) -> str:
        return hashlib.sha256(plan_key.encode("utf-8")).hexdigest()

    def _paths(self, plan_key: str) -> tuple[Path, Path]:
        name = self._name(plan_key)
        return self.root / f"{name}.json", self.root / f"{name}.lock"

    @staticmethod
    def _checksum(payload: Mapping[str, Any]) -> str:
        value = dict(payload)
        value.pop("checksum", None)
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def load(self, plan_key: str) -> Optional[dict[str, Any]]:
        path, _lock = self._paths(plan_key)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            raise PlanExecutionError("journal_corrupt", "plan journal is unreadable") from exc
        if not isinstance(payload, dict) or payload.get("checksum") != self._checksum(payload):
            raise PlanExecutionError("journal_corrupt", "plan journal checksum mismatch")
        return payload

    def save(self, plan_key: str, payload: Mapping[str, Any]) -> None:
        path, lock = self._paths(plan_key)
        with _exclusive_file_lock(lock):
            safe = dict(redact(dict(payload)))
            safe["checksum"] = self._checksum(safe)
            _atomic_json(path, safe)

    def mutate(self, plan_key: str, updater: Any) -> dict[str, Any]:
        path, lock = self._paths(plan_key)
        with _exclusive_file_lock(lock):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError:
                payload = {}
            if payload and payload.get("checksum") != self._checksum(payload):
                raise PlanExecutionError("journal_corrupt", "plan journal checksum mismatch")
            payload.pop("checksum", None)
            updated = updater(dict(payload))
            safe = dict(redact(updated))
            safe["checksum"] = self._checksum(safe)
            _atomic_json(path, safe)
            return safe


class PlanExecutor:
    def __init__(
        self,
        *,
        repository: Path,
        session_id: str,
        connection_id: str,
        delegate: Optional[ToolRuntime],
        repo_context: Any,
        task_states: TaskStateStore,
        artifacts: ArtifactStore,
        lease_store: RepositoryLeaseStore,
        session_directory: Path,
    ) -> None:
        self.repository = repository
        self.session_id = session_id
        self.connection_id = connection_id
        self.delegate = delegate
        self.repo_context = repo_context
        self.task_states = task_states
        self.artifacts = artifacts
        self.lease_store = lease_store
        self.journals = PlanJournalStore(session_directory)
        self._read_only_cache_lock = threading.RLock()
        self._read_only_cached_identity: Optional[dict[str, Any]] = None
        self._read_only_interplan_guard: Optional[WorkspaceChangeGuard] = None
        self._plan_flights_lock = threading.Lock()
        self._plan_flights: dict[str, dict[str, Any]] = {}

    def _take_read_only_cache(
        self,
    ) -> tuple[Optional[dict[str, Any]], Optional[WorkspaceChangeGuard]]:
        with self._read_only_cache_lock:
            identity = self._read_only_cached_identity
            guard = self._read_only_interplan_guard
            self._read_only_cached_identity = None
            self._read_only_interplan_guard = None
        if identity is None or guard is None or not guard.supported:
            if guard is not None:
                guard.close()
            return None, None
        return identity, guard

    def _store_read_only_cache(
        self,
        identity: dict[str, Any],
        guard: WorkspaceChangeGuard,
    ) -> None:
        if not guard.supported or guard.failed or guard.changed:
            guard.close()
            return
        with self._read_only_cache_lock:
            previous = self._read_only_interplan_guard
            self._read_only_cached_identity = identity
            self._read_only_interplan_guard = guard
        if previous is not None and previous is not guard:
            previous.close()

    def _invalidate_read_only_cache(self) -> None:
        with self._read_only_cache_lock:
            guard = self._read_only_interplan_guard
            self._read_only_cached_identity = None
            self._read_only_interplan_guard = None
        if guard is not None:
            guard.close()

    def close(self) -> None:
        """Release any inter-plan repository watcher owned by this executor."""
        self._invalidate_read_only_cache()

    def _acquire_plan_flight(self, plan_key: str) -> dict[str, Any]:
        """Serialize concurrent retries of the same logical plan in-process."""
        with self._plan_flights_lock:
            entry = self._plan_flights.get(plan_key)
            if entry is None:
                entry = {"lock": threading.Lock(), "users": 0}
                self._plan_flights[plan_key] = entry
            entry["users"] = int(entry["users"]) + 1
            lock = entry["lock"]
        lock.acquire()
        return entry

    def _release_plan_flight(self, plan_key: str, entry: dict[str, Any]) -> None:
        lock = entry["lock"]
        lock.release()
        with self._plan_flights_lock:
            entry["users"] = int(entry["users"]) - 1
            if entry["users"] <= 0 and self._plan_flights.get(plan_key) is entry:
                self._plan_flights.pop(plan_key, None)

    def _fresh_read_only_baseline(
        self,
        guard: Optional[WorkspaceChangeGuard],
    ) -> tuple[dict[str, Any], Optional[WorkspaceChangeGuard]]:
        """Build a fast identity with a gap-free watcher handoff.

        Git can occasionally emit a one-time metadata notification while status
        refreshes a freshly-created repository. Rather than filtering that event,
        retry the baseline under an overlapping successor guard. External changes
        are handled the same way: only a quiet synchronized baseline reaches the
        read-only operations. Unsupported/unstable layouts fall back to the
        established final second-identity check.
        """
        current = guard
        identity: dict[str, Any] = {}
        for _attempt in range(3):
            identity = self.repo_context._fast_revision_identity()
            if current is None or not current.supported:
                if current is not None:
                    current.close()
                return identity, None
            if identity.get("revision") == "unborn":
                current.close()
                return identity, None

            successor = start_workspace_change_guard(self.repository)
            quiet = current.finish()
            changed = current.changed
            failed = current.failed
            current.close()
            if quiet and not changed and not failed and successor.supported:
                return identity, successor
            if not successor.supported:
                successor.close()
                return identity, None
            current = successor

        if current is not None:
            current.close()
        return identity, None

    @staticmethod
    def _digest(arguments: Mapping[str, Any]) -> str:
        return hashlib.sha256(
            json.dumps(
                arguments,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        ).hexdigest()

    def _parse(
        self,
        arguments: Mapping[str, Any],
        plan_key: str,
    ) -> tuple[list[PlanOperation], PlanBudgets, bool]:
        operations_raw = arguments.get("operations")
        if not isinstance(operations_raw, list) or not operations_raw:
            raise PlanExecutionError("invalid_plan", "operations must be a non-empty array")
        budgets = PlanBudgets.parse(arguments.get("budgets"))
        if len(operations_raw) > budgets.max_steps:
            raise PlanExecutionError("budget_exceeded", "plan exceeds max_steps")
        stop_on_error = arguments.get("stop_on_error", True)
        if not isinstance(stop_on_error, bool):
            raise PlanExecutionError("invalid_plan", "stop_on_error must be boolean")
        operations = [
            PlanOperation.parse(item, index, plan_key)
            for index, item in enumerate(operations_raw)
        ]
        ids = [item.operation_id for item in operations]
        if len(ids) != len(set(ids)):
            raise PlanExecutionError("invalid_plan", "operation_id values must be unique")
        seen: set[str] = set()
        for operation in operations:
            unknown = set(operation.depends_on).difference(seen)
            if unknown:
                raise PlanExecutionError(
                    "invalid_plan",
                    f"operation {operation.operation_id} depends on future/unknown operations: {sorted(unknown)}",
                )
            seen.add(operation.operation_id)
        writes = sum(item.action == "patch" for item in operations)
        commands = sum(item.action in {"command", "checks"} for item in operations)
        browser = sum(item.action == "browser" for item in operations)
        if writes > budgets.max_write_operations:
            raise PlanExecutionError("budget_exceeded", "plan exceeds max_write_operations")
        if commands > budgets.max_command_runs:
            raise PlanExecutionError("budget_exceeded", "plan exceeds max_command_runs")
        if browser > budgets.max_browser_actions:
            raise PlanExecutionError("budget_exceeded", "plan exceeds max_browser_actions")
        if writes:
            last_write_index = max(
                index for index, item in enumerate(operations) if item.action == "patch"
            )
            if not any(
                index > last_write_index and item.action == "checks"
                for index, item in enumerate(operations)
            ):
                raise PlanExecutionError(
                    "verification_required",
                    "a plan with repository writes must include a checks operation after the final write",
                )
        return operations, budgets, stop_on_error

    def _delegate_names(self) -> set[str]:
        if self.delegate is None:
            return set()
        return {str(item.name) for item in self.delegate.descriptors()}

    def _tool_call(
        self,
        tool_name: str,
        inputs: dict[str, Any],
        operation: PlanOperation,
        budgets: PlanBudgets,
        remaining_seconds: float,
        delegate_names: set[str],
    ) -> dict[str, Any] | CallToolResult:
        if self.delegate is None or tool_name not in delegate_names:
            raise PlanExecutionError(
                "capability_unavailable", f"required low-level tool is not exposed: {tool_name}"
            )
        try:
            result = self.delegate.execute(
                tool_name,
                inputs,
                idempotency_key=None if tool_name in {
                    "karox.repo.search",
                    "karox.repo.read_file",
                    "karox.repo.read_lines",
                    "karox.git.status",
                    "karox.git.diff",
                    "karox.git.log",
                    "karox.runtime.status",
                    "karox.dev_server.status",
                    "karox.dev_server.logs",
                    "karox.browser.snapshot",
                    "karox.browser.console",
                    "karox.browser.network_failures",
                    "karox.browser.network_requests",
                    "karox.browser.tabs",
                    "karox.browser.get_text",
                } else operation.idempotency_key,
                deadline_seconds=max(1.0, min(remaining_seconds, 300.0)),
            )
        except PlanExecutionError:
            raise
        except Exception as exc:
            code = str(getattr(exc, "code", "delegate_error") or "delegate_error")[:120]
            message = str(redact(str(exc)))[:1000] or type(exc).__name__
            raise PlanExecutionError(
                code,
                f"{tool_name} delegate failed: {message}",
            ) from exc
        return artifact_backed_result(
            result,
            tool_name=tool_name,
            store=self.artifacts,
            threshold_bytes=budgets.max_inline_output_bytes,
        )

    @staticmethod
    def _normalize_result(result: dict[str, Any] | CallToolResult) -> dict[str, Any]:
        if isinstance(result, CallToolResult):
            structured = result.structuredContent
            if isinstance(structured, dict):
                value = dict(structured)
            else:
                value = {"content": result.model_dump(mode="json").get("content", [])}
            value.setdefault("ok", not bool(result.isError))
            value["is_error"] = bool(result.isError)
            return value
        return dict(result)

    def _dispatch(
        self,
        operation: PlanOperation,
        budgets: PlanBudgets,
        remaining_seconds: float,
        delegate_names: set[str],
        *,
        workstream_id: Optional[str] = None,
    ) -> dict[str, Any]:
        action = operation.action
        inputs = dict(operation.inputs)
        if action == "inspect":
            return self.repo_context.inspect(
                str(inputs.get("goal", "")), str(inputs.get("depth", "focused"))
            )
        if action == "search":
            return self._normalize_result(
                self._tool_call("karox.repo.search", inputs, operation, budgets, remaining_seconds, delegate_names)
            )
        if action == "read":
            mode = inputs.pop("mode", "file")
            if mode == "runtime_status":
                tool = "karox.runtime.status"
            else:
                tool = "karox.repo.read_lines" if mode == "lines" else "karox.repo.read_file"
            return self._normalize_result(
                self._tool_call(tool, inputs, operation, budgets, remaining_seconds, delegate_names)
            )
        if action == "patch":
            inputs.pop("expected_paths", None)
            if set(inputs).difference({"action", "payload"}):
                raise PlanExecutionError("invalid_operation", "patch inputs must be repo.command arguments")
            return self._normalize_result(
                self._tool_call("karox.repo.command", inputs, operation, budgets, remaining_seconds, delegate_names)
            )
        if action == "command":
            # Full Hyperagent sessions may keep an older cached MCP catalogue
            # that predates karox.command.run. task.execute_plan is already a
            # stable tool, so route command operations through the unrestricted
            # developer runner whenever the live bridge exposes it; protected
            # profiles continue to use the approved checks surface.
            access_profile = ""
            if self.delegate is not None:
                session_reader = getattr(self.delegate, "session_info", None)
                if callable(session_reader):
                    try:
                        access_profile = str(session_reader().get("access_profile") or "")
                    except Exception:
                        access_profile = ""
            tool_name = (
                "karox.command.run"
                if access_profile == "elevated" and "karox.command.run" in delegate_names
                else "karox.checks.run"
            )
            return self._normalize_result(
                self._tool_call(tool_name, inputs, operation, budgets, remaining_seconds, delegate_names)
            )
        if action == "checks":
            tool = str(inputs.pop("tool", "tests"))
            tool_name = "karox.tests.run" if tool == "tests" else "karox.checks.run"
            return self._normalize_result(
                self._tool_call(tool_name, inputs, operation, budgets, remaining_seconds, delegate_names)
            )
        if action == "dev_server":
            verb = inputs.pop("verb", "status")
            mapping = {
                "start": "karox.dev_server.start",
                "status": "karox.dev_server.status",
                "logs": "karox.dev_server.logs",
            }
            if verb == "stop":
                raise PlanExecutionError(
                    "session_grant_required",
                    "stopping an owned runtime requires a pre-issued Tier 2 session grant",
                )
            if verb not in mapping:
                raise PlanExecutionError("invalid_operation", "unsupported dev_server verb")
            return self._normalize_result(
                self._tool_call(mapping[verb], inputs, operation, budgets, remaining_seconds, delegate_names)
            )
        if action == "browser":
            verb = inputs.pop("verb", "snapshot")
            mapping = {
                "snapshot": "karox.browser.snapshot",
                "screenshot": "karox.browser.screenshot",
                "console": "karox.browser.console",
                "network_failures": "karox.browser.network_failures",
                "network_requests": "karox.browser.network_requests",
                "tabs": "karox.browser.tabs",
                "get_text": "karox.browser.get_text",
                "click": "karox.browser.click",
                "fill": "karox.browser.fill",
                "press": "karox.browser.press",
                "select": "karox.browser.select",
                "wait_for": "karox.browser.wait_for",
                "open": "karox.browser.open",
                "new_tab": "karox.browser.new_tab",
                "switch_tab": "karox.browser.switch_tab",
                "close_tab": "karox.browser.close_tab",
            }
            if verb not in mapping:
                raise PlanExecutionError(
                    "user_gate_required",
                    "browser close/takeover/auth/payment operations cannot run inside execute_plan",
                )
            return self._normalize_result(
                self._tool_call(mapping[verb], inputs, operation, budgets, remaining_seconds, delegate_names)
            )
        if action == "runtime":
            verb = inputs.pop("verb", "status")
            mapping = {
                "status": "karox.runtime.status",
                "restart": "karox.runtime.restart",
            }
            runtime_tool = mapping.get(str(verb))
            if runtime_tool is None:
                raise PlanExecutionError(
                    "invalid_operation",
                    "runtime verb must be status or restart",
                )
            return self._normalize_result(
                self._tool_call(runtime_tool, inputs, operation, budgets, remaining_seconds, delegate_names)
            )
        if action == "checkpoint":
            updates = inputs.get("updates")
            if not isinstance(updates, dict) or not updates:
                raise PlanExecutionError("invalid_operation", "checkpoint requires non-empty updates")
            parsed = {
                str(name): fact(value, FactOrigin.REPORTED_BY_AGENT, operation.operation_id)
                for name, value in updates.items()
            }
            auto_bootstrapped = False
            if self.task_states.load_optional(
                self.session_id,
                workstream_id=workstream_id,
            ) is None:
                record = self.task_states.sessions.load(self.session_id)
                identity = self.repo_context._fast_revision_identity()
                base = {
                    "objective": fact(record.task, FactOrigin.VERIFIED, "session.task"),
                    "repository": fact(str(self.repository), FactOrigin.VERIFIED, "session.repository"),
                    "branch": fact(record.branch, FactOrigin.VERIFIED, "session.branch"),
                    "repository_revision": fact(identity["revision"], FactOrigin.OBSERVED, "git.rev-parse"),
                    "connection_profile": fact(self.connection_id, FactOrigin.VERIFIED, "bridge.profile"),
                    "access_profile": fact(record.access_profile, FactOrigin.VERIFIED, "session.access_profile"),
                    "files_changed": fact(record.changed_files, FactOrigin.HISTORICAL, "session.changed_files"),
                    "checks_executed": fact(record.checks, FactOrigin.HISTORICAL, "session.checks"),
                }
                self.task_states.bootstrap(
                    self.session_id,
                    base,
                    workstream_id=workstream_id,
                )
                auto_bootstrapped = True
            state = self.task_states.checkpoint(
                self.session_id,
                parsed,
                workstream_id=workstream_id,
            )
            return {
                "ok": True,
                "task": state.compact(),
                "auto_bootstrapped": auto_bootstrapped,
            }
        raise PlanExecutionError("invalid_operation", f"unsupported action: {action}")

    def _preconditions(
        self,
        operation: PlanOperation,
        expected_fast_repository: Mapping[str, Any],
        expected_strict_repository: Optional[Mapping[str, Any]],
        completed: Mapping[str, dict[str, Any]],
        *,
        validate_repository: bool = True,
    ) -> Optional[dict[str, Any]]:
        for dependency in operation.depends_on:
            result = completed.get(dependency)
            if not result or result.get("status") != "success":
                raise PlanExecutionError(
                    "failed_precondition",
                    f"dependency did not succeed: {dependency}",
                )
        required_revision = operation.preconditions.get("repository_revision")
        if validate_repository:
            current_fast = self.repo_context._fast_revision_identity()
            if current_fast != expected_fast_repository:
                raise PlanExecutionError(
                    "scope_drift",
                    "repository changed outside the completed plan operations",
                    {"expected": expected_fast_repository, "current": current_fast},
                )
        else:
            # Consecutive read/search/inspect operations are validated as one
            # repository-stable block. Their baseline was already captured at
            # the beginning of the block, and the block is revalidated before a
            # mutation or at finalization. This avoids repeated expensive
            # ``git status`` scans while preserving fail-closed drift detection.
            current_fast = expected_fast_repository
        if required_revision is not None and required_revision != current_fast.get("revision"):
            raise PlanExecutionError("stale_repository_revision", "repository revision precondition failed")

        # Expensive content hashing is reserved for operations that can touch the
        # repository. The first lease-bearing operation establishes a strict
        # baseline; later ones must match it exactly.
        strict = dict(expected_strict_repository) if expected_strict_repository is not None else None
        if operation.action in _REPOSITORY_LEASE_ACTIONS:
            current_strict = self.repo_context._revision_identity()
            if strict is not None and current_strict != strict:
                raise PlanExecutionError(
                    "scope_drift",
                    "repository content changed outside the completed plan operations",
                    {"expected": strict, "current": current_strict},
                )
            strict = current_strict

        hashes = operation.preconditions.get("file_sha256", {})
        if hashes:
            if not isinstance(hashes, Mapping):
                raise PlanExecutionError("invalid_operation", "file_sha256 precondition must be object")
            for raw_path, expected in hashes.items():
                path = (self.repository / str(raw_path)).resolve()
                try:
                    path.relative_to(self.repository)
                except ValueError as exc:
                    raise PlanExecutionError("failed_precondition", "file hash path escaped repository") from exc
                actual = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
                if actual != expected:
                    raise PlanExecutionError(
                        "failed_precondition", f"file hash precondition failed: {raw_path}"
                    )
        return strict

    @staticmethod
    def _repository_changes(
        before: Mapping[str, Any],
        after: Mapping[str, Any],
    ) -> set[str]:
        def dirty_map(value: Mapping[str, Any]) -> dict[str, str]:
            entries = value.get("dirty", [])
            if not isinstance(entries, list):
                return {}
            return {
                str(item["path"]): str(item["sha256"])
                for item in entries
                if isinstance(item, Mapping)
                and isinstance(item.get("path"), str)
                and isinstance(item.get("sha256"), str)
            }

        before_files = dirty_map(before)
        after_files = dirty_map(after)
        paths = set(before_files) | set(after_files)
        changed = {
            path
            for path in paths
            if before_files.get(path) != after_files.get(path)
        }
        if before.get("revision") != after.get("revision"):
            changed.add("__repository_revision__")
        return changed

    @staticmethod
    def _success(result: Mapping[str, Any]) -> bool:
        if result.get("ok") is False or result.get("is_error") is True:
            return False
        data = result.get("data")
        if isinstance(data, Mapping) and data.get("ok") is False:
            return False
        return True

    @staticmethod
    def _expected(operation: PlanOperation, result: Mapping[str, Any]) -> None:
        expected = operation.expected_outcome
        if not expected:
            return
        if "ok" in expected and bool(expected["ok"]) != PlanExecutor._success(result):
            raise PlanExecutionError("unexpected_outcome", "operation ok outcome did not match")
        expected_exit = expected.get("exit_code")
        if expected_exit is not None:
            actual = result.get("exit_code")
            if actual is None and isinstance(result.get("data"), Mapping):
                actual = result["data"].get("exit_code")
            if actual != expected_exit:
                raise PlanExecutionError("unexpected_outcome", "operation exit_code did not match")
        minimum = expected.get("changed_files_min")
        if minimum is not None:
            changed = result.get("changed_files")
            if changed is None and isinstance(result.get("data"), Mapping):
                changed = result["data"].get("changed_files")
            if not isinstance(changed, list) or len(changed) < int(minimum):
                raise PlanExecutionError("unexpected_outcome", "too few files changed")

    def _apply_output_policy(
        self,
        operation: PlanOperation,
        result: dict[str, Any],
    ) -> dict[str, Any]:
        if operation.output_policy == "inline":
            return result
        if operation.output_policy == "artifact":
            if isinstance(result.get("artifact_id"), str):
                return result
            encoded = json.dumps(
                redact(result),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            ).encode("utf-8")
            record = self.artifacts.put(
                encoded,
                name=f"plan-{operation.operation_id}-result.json",
                mime="application/json",
            )
            return {
                "ok": self._success(result),
                "result_mode": "artifact",
                "artifact_id": record.artifact_id,
                "content_hash": record.sha256,
                "total_size": record.size,
                "persistence_policy": record.persistence_policy,
                "available_sections": sorted(str(key) for key in result),
            }
        if operation.output_policy == "evidence":
            packet = packet_from_plan_result(
                operation.action,
                result,
                success=self._success(result),
            )
            if packet is not None:
                encoded = json.dumps(
                    redact(result),
                    ensure_ascii=False,
                    sort_keys=True,
                    default=str,
                ).encode("utf-8")
                existing_id = result.get("artifact_id")
                content_hash: Optional[str] = None
                total_size: Optional[int] = None
                if isinstance(existing_id, str) and existing_id:
                    artifact_id = existing_id
                    raw_hash = result.get("content_hash")
                    if isinstance(raw_hash, str):
                        content_hash = raw_hash
                    raw_total = result.get("total_size")
                    if isinstance(raw_total, int) and not isinstance(raw_total, bool):
                        total_size = raw_total
                else:
                    record = self.artifacts.put(
                        encoded,
                        name=f"plan-{operation.operation_id}-result.json",
                        mime="application/json",
                    )
                    artifact_id = record.artifact_id
                    content_hash = record.sha256
                    total_size = record.size
                packet = replace(
                    packet,
                    artifact=EvidenceArtifactRef(
                        artifact_id=artifact_id,
                        sha256=content_hash,
                        raw_bytes=len(encoded),
                    ),
                )
                rendered = packet.render(Fidelity.AUTO)
                rendered_size = len(rendered.encode("utf-8"))
                return {
                    "ok": self._success(result),
                    "result_mode": "evidence",
                    "packet": json.loads(rendered),
                    "artifact_id": artifact_id,
                    "content_hash": content_hash,
                    "total_size": total_size,
                    "raw_bytes": len(encoded),
                    "packet_bytes": rendered_size,
                    "bytes_avoided": max(0, len(encoded) - rendered_size),
                    "output_policy": "evidence",
                }
        compact: dict[str, Any] = {}
        for key in (
            "ok",
            "error_code",
            "summary",
            "path",
            "command",
            "exit_code",
            "timed_out",
            "changed_files",
            "artifact_id",
            "result_mode",
            "total_size",
            "content_hash",
            "cache_hit",
            "idempotent_replay",
        ):
            if key in result:
                compact[key] = result[key]
        data = result.get("data")
        if isinstance(data, Mapping):
            for key in (
                "ok",
                "error_code",
                "exit_code",
                "timed_out",
                "changed_files",
                "artifact_id",
                "result_mode",
                "total_size",
                "content_hash",
                "cache_hit",
                "idempotent_replay",
            ):
                if key in data and key not in compact:
                    compact[key] = data[key]
        compact.setdefault("ok", self._success(result))
        compact["output_policy"] = "summary"
        if operation.output_policy == "evidence":
            # No typed packet form for this action yet: the downgrade to the
            # lossless summary form is recorded rather than silent.
            compact["requested_output_policy"] = "evidence"
        return compact

    def _checkpoint_progress(
        self,
        completed: Mapping[str, dict[str, Any]],
        next_action: str,
        *,
        workstream_id: Optional[str] = None,
    ) -> None:
        try:
            self.task_states.checkpoint(
                self.session_id,
                {
                    "operations_executed": fact(
                        [
                            {"operation_id": key, "status": value.get("status")}
                            for key, value in completed.items()
                        ],
                        FactOrigin.REPORTED_BY_AGENT,
                        "task.execute_plan",
                    ),
                    "next_safe_action": fact(next_action, FactOrigin.PENDING, "task.execute_plan"),
                },
                workstream_id=workstream_id,
            )
        except Exception:
            # The plan journal remains authoritative even when no task.bootstrap
            # has created task_state yet or the checkpoint projection is unavailable.
            pass

    def execute(
        self,
        arguments: dict[str, Any],
        plan_key: Optional[str],
        *,
        workstream_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if not plan_key:
            raise PlanExecutionError("idempotency_required", "execute_plan requires idempotency key")
        flight = self._acquire_plan_flight(plan_key)
        try:
            return self._execute_unlocked(
                arguments,
                plan_key,
                workstream_id=workstream_id,
            )
        finally:
            self._release_plan_flight(plan_key, flight)

    def _execute_unlocked(
        self,
        arguments: dict[str, Any],
        plan_key: str,
        *,
        workstream_id: Optional[str] = None,
    ) -> dict[str, Any]:
        digest = self._digest(arguments)
        operations, budgets, stop_on_error = self._parse(arguments, plan_key)
        pure_read_only = all(item.action in _READ_ONLY_ACTIONS for item in operations)
        existing = self.journals.load(plan_key)
        if existing is not None:
            if existing.get("input_digest") != digest:
                raise PlanExecutionError("idempotency_conflict", "plan key was used for different input")
            if existing.get("status") == "complete":
                final = existing.get("final_result")
                if isinstance(final, dict):
                    return {**final, "idempotent_replay": True}
        else:
            existing = {
                "schema_version": PLAN_SCHEMA_VERSION,
                "plan_id": f"plan-{hashlib.sha256(plan_key.encode()).hexdigest()[:24]}",
                "input_digest": digest,
                "status": "running",
                "started_at": time.time(),
                "updated_at": time.time(),
                "operations": {},
            }
            self.journals.save(plan_key, existing)
        if not pure_read_only:
            self._invalidate_read_only_cache()
        task_state = self.task_states.load_optional(
            self.session_id,
            workstream_id=workstream_id,
        )
        task_id = (
            task_state.task_id
            if task_state is not None
            else f"task-{self.session_id}-{workstream_id or 'default'}"
        )
        requires_lease = any(item.action in _REPOSITORY_LEASE_ACTIONS for item in operations)
        lease: Optional[RepositoryLease] = None
        recovered_stale = False
        if requires_lease:
            try:
                lease, recovered_stale = self.lease_store.acquire(
                    self.repository,
                    session_id=self.session_id,
                    task_id=task_id,
                    connection_id=self.connection_id,
                    current_operation="task.execute_plan",
                    ttl_seconds=max(30.0, min(budgets.max_wall_time + 30.0, 3600.0)),
                )
            except RepositoryLeaseConflict as exc:
                raise PlanExecutionError("repository_lease_conflict", str(exc), exc.details) from exc
        started = time.perf_counter()
        # Pure read-only plans keep a guarded identity warm across calls. A new
        # watcher starts before the previous inter-plan watcher is finished, so a
        # quiet synchronization barrier can prove there was no repository-change
        # gap and reuse the prior identity without running git status again.
        change_guard: Optional[WorkspaceChangeGuard] = None
        cached_guard: Optional[WorkspaceChangeGuard] = None
        cached_identity: Optional[dict[str, Any]] = None
        if pure_read_only:
            cached_identity, cached_guard = self._take_read_only_cache()
            change_guard = start_workspace_change_guard(self.repository)
        try:
            reused_cached_identity = False
            if (
                cached_identity is not None
                and cached_guard is not None
                and change_guard is not None
                and change_guard.supported
            ):
                interplan_quiet = cached_guard.finish()
                reused_cached_identity = (
                    interplan_quiet
                    and not cached_guard.changed
                    and not cached_guard.failed
                )
            if cached_guard is not None:
                cached_guard.close()
                cached_guard = None

            if reused_cached_identity:
                assert cached_identity is not None
                expected_fast_repository = cached_identity
            elif pure_read_only:
                expected_fast_repository, change_guard = self._fresh_read_only_baseline(
                    change_guard
                )
            else:
                expected_fast_repository = self.repo_context._fast_revision_identity()
        except Exception:
            if cached_guard is not None:
                cached_guard.close()
            if change_guard is not None:
                change_guard.close()
            raise
        expected_strict_repository: Optional[dict[str, Any]] = None
        completed: dict[str, dict[str, Any]] = dict(existing.get("operations") or {})
        # Tool exposure is fixed for one hosted bridge connection. Snapshot it
        # once for the pending operations in this plan instead of rebuilding the
        # descriptor set before every delegated low-level call. The delegate's
        # execute path still performs its per-call session/repository checks.
        needs_delegate = any(
            operation.action not in {"inspect", "checkpoint"}
            and completed.get(operation.operation_id, {}).get("status") != "success"
            for operation in operations
        )
        delegate_names = self._delegate_names() if needs_delegate else set()
        failed_signatures: dict[str, int] = {}
        artifact_bytes = 0
        next_interplan_guard: Optional[WorkspaceChangeGuard] = None
        try:
            for index, operation in enumerate(operations):
                prior = completed.get(operation.operation_id)
                if prior and prior.get("status") == "success":
                    continue
                prior_attempts = (
                    int(prior.get("attempts", 0))
                    if isinstance(prior, Mapping)
                    and isinstance(prior.get("attempts", 0), int)
                    else 0
                )
                max_attempts = max(1, budgets.max_fix_attempts)
                if prior_attempts >= max_attempts:
                    raise PlanExecutionError(
                        "attempt_budget_exceeded",
                        f"operation exhausted {max_attempts} bounded attempts: {operation.operation_id}",
                    )
                elapsed = time.perf_counter() - started
                if elapsed >= budgets.max_wall_time:
                    raise PlanExecutionError("budget_exceeded", "plan exceeded max_wall_time")
                if lease is not None:
                    lease = self.lease_store.heartbeat(
                        self.repository,
                        lease,
                        current_operation=operation.operation_id,
                        ttl_seconds=max(30.0, min(budgets.max_wall_time + 30.0, 3600.0)),
                    )
                try:
                    strict_before = self._preconditions(
                        operation,
                        expected_fast_repository,
                        expected_strict_repository,
                        completed,
                        validate_repository=operation.action not in _READ_ONLY_ACTIONS,
                    )
                    if strict_before is not None:
                        expected_strict_repository = strict_before
                    if operation.action not in _READ_ONLY_ACTIONS:
                        # A mutation must be fenced before dispatch. This also
                        # flushes any preceding read-only block so a crash cannot
                        # lose the boundary immediately before side effects.
                        self.journals.mutate(
                            plan_key,
                            lambda state: {
                                **state,
                                "updated_at": time.time(),
                                "current_operation": operation.operation_id,
                                "operations": {
                                    **completed,
                                    operation.operation_id: {
                                        "status": "running",
                                        "started_at": time.time(),
                                        "action": operation.action,
                                    },
                                },
                            },
                        )
                    result = self._dispatch(
                        operation,
                        budgets,
                        budgets.max_wall_time - elapsed,
                        delegate_names,
                        workstream_id=workstream_id,
                    )
                    self._expected(operation, result)
                    success = self._success(result)
                    if not success:
                        raise PlanExecutionError(
                            str(result.get("error_code") or "operation_failed"),
                            f"operation failed: {operation.operation_id}",
                        )
                    current_fast_repository = expected_fast_repository
                    observed_changes: set[str] = set()
                    current_strict_repository: Optional[dict[str, Any]] = None
                    if operation.action not in _READ_ONLY_ACTIONS:
                        current_fast_repository = self.repo_context._fast_revision_identity()
                        observed_changes = self._repository_changes(
                            expected_fast_repository,
                            current_fast_repository,
                        )
                    if operation.action in _REPOSITORY_LEASE_ACTIONS:
                        if expected_strict_repository is None:
                            raise PlanExecutionError(
                                "internal_error",
                                "strict repository baseline was not established",
                            )
                        current_strict_repository = self.repo_context._revision_identity()
                        observed_changes = self._repository_changes(
                            expected_strict_repository,
                            current_strict_repository,
                        )

                    changed = result.get("changed_files")
                    if changed is None and isinstance(result.get("data"), Mapping):
                        changed = result["data"].get("changed_files")
                    reported_changes = {
                        str(path) for path in changed
                    } if isinstance(changed, list) else set()
                    if operation.action != "patch" and observed_changes:
                        raise PlanExecutionError(
                            "scope_drift",
                            "a non-patch operation changed the repository",
                            {"observed_paths": sorted(observed_changes)},
                        )
                    if operation.action == "patch":
                        if "__repository_revision__" in observed_changes:
                            raise PlanExecutionError(
                                "scope_drift",
                                "patch operation changed repository revision",
                            )
                        unexplained = observed_changes.difference(reported_changes)
                        if unexplained:
                            raise PlanExecutionError(
                                "scope_drift",
                                "patch changed paths not reported by the guarded transaction: "
                                f"{sorted(unexplained)}",
                                {"unexplained_paths": sorted(unexplained)},
                            )
                        expected_paths = operation.inputs.get("expected_paths")
                        if expected_paths is not None:
                            if not isinstance(expected_paths, list) or not all(
                                isinstance(path, str) for path in expected_paths
                            ):
                                raise PlanExecutionError(
                                    "invalid_operation",
                                    "expected_paths must be a string array",
                                )
                            if not reported_changes.issubset(set(expected_paths)):
                                raise PlanExecutionError(
                                    "scope_drift",
                                    "operation changed unexpected paths",
                                )
                    stored_result = self._apply_output_policy(operation, result)
                    artifact_size = (
                        stored_result.get("total_size")
                        if stored_result.get("result_mode") in {"artifact", "evidence"}
                        else 0
                    )
                    if isinstance(artifact_size, int):
                        artifact_bytes += artifact_size
                    if artifact_bytes > budgets.max_artifact_bytes:
                        raise PlanExecutionError("budget_exceeded", "plan exceeded max_artifact_bytes")
                    compact = {
                        "status": "success",
                        "action": operation.action,
                        "completed_at": time.time(),
                        "result": stored_result,
                        "idempotency_key": operation.idempotency_key,
                    }
                    completed[operation.operation_id] = compact
                    expected_fast_repository = current_fast_repository
                    if current_strict_repository is not None:
                        expected_strict_repository = current_strict_repository
                    if operation.action not in _READ_ONLY_ACTIONS:
                        self.journals.mutate(
                            plan_key,
                            lambda state: {
                                **state,
                                "updated_at": time.time(),
                                "operations": dict(completed),
                            },
                        )
                    elif (
                        not pure_read_only
                        and (index + 1) % budgets.checkpoint_after_steps == 0
                    ):
                        # Mixed plans persist completed reads in batches so the
                        # boundary before later side effects remains recoverable.
                        # A pure read-only plan can safely replay reads after a
                        # hard crash and only needs the initial claim + final result.
                        self.journals.mutate(
                            plan_key,
                            lambda state: {
                                **state,
                                "updated_at": time.time(),
                                "current_operation": None,
                                "operations": dict(completed),
                            },
                        )
                except PlanExecutionError as exc:
                    signature = f"{exc.code}:{exc}"
                    failed_signatures[signature] = failed_signatures.get(signature, 0) + 1
                    previous_signature = (
                        prior.get("failure_signature")
                        if isinstance(prior, Mapping)
                        else None
                    )
                    failure = {
                        "status": "failed",
                        "action": operation.action,
                        "completed_at": time.time(),
                        "error_code": exc.code,
                        "error": str(exc),
                        "details": exc.details,
                        "attempts": prior_attempts + 1,
                        "failure_signature": signature,
                    }
                    completed[operation.operation_id] = failure
                    self.journals.mutate(
                        plan_key,
                        lambda state: {
                            **state,
                            "status": "running",
                            "updated_at": time.time(),
                            "operations": dict(completed),
                        },
                    )
                    repeated = (
                        failed_signatures[signature] > 1
                        or previous_signature == signature
                    )
                    if repeated:
                        raise PlanExecutionError(
                            "repeated_identical_failure",
                            f"operation repeated the same failure: {operation.operation_id}",
                            {"failure_signature": signature},
                        ) from exc
                    if stop_on_error or operation.on_failure == "stop":
                        raise
                if (index + 1) % budgets.checkpoint_after_steps == 0:
                    next_action = (
                        operations[index + 1].operation_id
                        if index + 1 < len(operations)
                        else "finalize plan"
                    )
                    self._checkpoint_progress(
                        completed,
                        next_action,
                        workstream_id=workstream_id,
                    )
            final_fast_repository: dict[str, Any]
            can_reuse_fast_identity = False
            if pure_read_only and change_guard is not None and change_guard.supported:
                # Start the next inter-plan watcher before finishing the current
                # one. Their overlap means a quiet finish proves this plan and the
                # following idle interval have no unmonitored repository gap.
                next_interplan_guard = start_workspace_change_guard(self.repository)
                if change_guard.changed:
                    change_guard.finish()
                    observed = set(change_guard.changed_paths) or {"__workspace_event__"}
                    raise PlanExecutionError(
                        "scope_drift",
                        "a read-only plan block observed a repository change",
                        {"observed_paths": sorted(observed)},
                    )
                if change_guard.failed:
                    change_guard.finish()
                    raise PlanExecutionError(
                        "scope_drift",
                        "repository change monitoring became uncertain during a read-only plan",
                        {"observed_paths": ["__workspace_monitor_uncertain__"]},
                    )

                repository_quiet = change_guard.finish()
                if change_guard.changed:
                    observed = set(change_guard.changed_paths) or {"__workspace_event__"}
                    raise PlanExecutionError(
                        "scope_drift",
                        "a read-only plan block observed a repository change",
                        {"observed_paths": sorted(observed)},
                    )
                if not repository_quiet or change_guard.failed:
                    raise PlanExecutionError(
                        "scope_drift",
                        "repository change monitoring became uncertain during a read-only plan",
                        {"observed_paths": ["__workspace_monitor_uncertain__"]},
                    )
                can_reuse_fast_identity = True
                self._store_read_only_cache(
                    expected_fast_repository,
                    next_interplan_guard,
                )
                next_interplan_guard = None
            elif change_guard is not None:
                change_guard.finish()
            final_fast_repository = (
                expected_fast_repository
                if can_reuse_fast_identity
                else self.repo_context._fast_revision_identity()
            )
            final_observed_changes = self._repository_changes(
                expected_fast_repository,
                final_fast_repository,
            )
            if final_observed_changes:
                raise PlanExecutionError(
                    "scope_drift",
                    "a read-only plan block changed the repository",
                    {"observed_paths": sorted(final_observed_changes)},
                )
            final_repository = (
                expected_strict_repository
                if expected_strict_repository is not None
                else final_fast_repository
            )
            full = {
                "schema_version": PLAN_SCHEMA_VERSION,
                "plan_id": existing["plan_id"],
                "operations": completed,
                "repository_revision": final_repository,
                "repository_revision_mode": (
                    "strict" if expected_strict_repository is not None else "fast"
                ),
                "duration_ms": round((time.perf_counter() - started) * 1000, 3),
                "recovered_stale_lease": recovered_stale,
            }
            record = self.artifacts.put(
                json.dumps(redact(full), ensure_ascii=False, sort_keys=True).encode("utf-8"),
                name="execute-plan.json",
                mime="application/json",
            )
            evidence_packets_stored = 0
            evidence_bytes_avoided = 0
            for item in completed.values():
                stored = item.get("result") if isinstance(item, Mapping) else None
                if isinstance(stored, Mapping) and stored.get("result_mode") == "evidence":
                    evidence_packets_stored += 1
                    avoided = stored.get("bytes_avoided")
                    if isinstance(avoided, int) and not isinstance(avoided, bool):
                        evidence_bytes_avoided += avoided
            # One hosted plan call replaced a sequential tool round trip per
            # operation; the batch planner additionally reports how many of
            # those operations were independent reads. Counted, not estimated
            # from tokens: these are the honest execute_plan economy numbers.
            batch_plan = BatchPlanner().plan(
                [
                    {"name": _BATCH_TOOL_NAMES.get(operation.action, operation.action)}
                    for operation in operations
                ]
            )
            final = {
                "ok": True,
                "schema_version": PLAN_SCHEMA_VERSION,
                "plan_id": existing["plan_id"],
                "status": "complete",
                "operations_total": len(operations),
                "economy": {
                    "model_round_trips_avoided": max(0, len(operations) - 1),
                    "parallel_read_candidates": len(batch_plan.parallel_reads),
                    "read_round_trips_saved": batch_plan.estimated_round_trips_saved,
                    "evidence_packets": evidence_packets_stored,
                    "evidence_bytes_avoided": evidence_bytes_avoided,
                },
                "operations_succeeded": sum(
                    item.get("status") == "success" for item in completed.values()
                ),
                "duration_ms": full["duration_ms"],
                "artifact_id": record.artifact_id,
                "content_hash": record.sha256,
                "total_size": record.size,
                "recovered_stale_lease": recovered_stale,
                "operation_summaries": [
                    {
                        "operation_id": operation.operation_id,
                        "action": operation.action,
                        "status": completed.get(operation.operation_id, {}).get("status", "pending"),
                        "error_code": completed.get(operation.operation_id, {}).get("error_code"),
                    }
                    for operation in operations
                ],
                "idempotent_replay": False,
            }
            self.journals.mutate(
                plan_key,
                lambda state: {
                    **state,
                    "status": "complete",
                    "updated_at": time.time(),
                    "current_operation": None,
                    "operations": dict(completed),
                    "final_result": final,
                },
            )
            self._checkpoint_progress(
                completed,
                "review execute_plan result",
                workstream_id=workstream_id,
            )
            return final
        except PlanExecutionError as exc:
            error_code = exc.code
            error_message = str(exc)
            error_details = exc.details
            self.journals.mutate(
                plan_key,
                lambda state: {
                    **state,
                    "status": "blocked",
                    "updated_at": time.time(),
                    "error_code": error_code,
                    "error": error_message,
                    "error_details": error_details,
                    "operations": completed,
                },
            )
            raise
        finally:
            if change_guard is not None:
                change_guard.close()
            if next_interplan_guard is not None:
                next_interplan_guard.close()
            if lease is not None:
                try:
                    self.lease_store.release(self.repository, lease)
                except RepositoryLeaseError:
                    # The plan journal already records the primary outcome. A lost
                    # lease during cleanup must not replace that more useful result.
                    pass
