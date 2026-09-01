"""Authoritative, provenance-aware task state for cross-chat recovery.

SessionRecord remains the durable execution/session ledger.  TaskState is a
separate versioned projection optimized for an AI client resuming work in a new
chat.  Keeping it separate avoids a risky migration of the strict session schema
while still reusing the session directory, repository binding and OS file lock.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

from .project_registry import ProjectRegistryError, validate_project_id
from .security import redact
from .sessions import SessionError, SessionStore, _atomic_json, _exclusive_file_lock

TASK_STATE_SCHEMA_VERSION = 1
# Canonical facts KaroX itself understands. Checkpoints may also persist custom
# agent/user facts: the MCP schema has always advertised arbitrary property
# names, so rejecting them only after transport validation made valid-looking
# calls fail as a generic `denied`. Keep authoritative environment facts
# reserved while allowing bounded custom cross-chat memory.
TASK_FACT_NAMES = (
    "objective",
    "project_id",
    "repository",
    "branch",
    "repository_revision",
    "working_tree_fingerprint",
    "connection_profile",
    "client_capabilities",
    "access_profile",
    "accepted_decisions",
    "rejected_approaches",
    "completed_phases",
    "current_phase",
    "files_inspected",
    "files_changed",
    "operations_executed",
    "checks_executed",
    "known_pre_existing_failures",
    "current_blockers",
    "pending_user_gates",
    "next_safe_action",
    "relevant_artifact_ids",
    "last_checkpoint_timestamp",
)
SYSTEM_VERIFIED_TASK_FACTS = frozenset(
    {
        "project_id",
        "repository",
        "branch",
        "repository_revision",
        "working_tree_fingerprint",
        "connection_profile",
        "client_capabilities",
        "access_profile",
        "last_checkpoint_timestamp",
    }
)
MAX_TASK_FACTS = 256
MAX_TASK_FACT_NAME_LENGTH = 128
MAX_WORKSTREAM_ID_LENGTH = 64


def _validate_workstream_id(workstream_id: Optional[str]) -> Optional[str]:
    if workstream_id is None:
        return None
    if not isinstance(workstream_id, str):
        raise SessionError("workstream_id must be a string")
    value = workstream_id.strip()
    if not value or len(value) > MAX_WORKSTREAM_ID_LENGTH:
        raise SessionError("workstream_id must be 1..64 characters")
    allowed = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-"
    if any(char not in allowed for char in value):
        raise SessionError("workstream_id contains unsafe characters")
    # Public task APIs render the legacy/no-workstream task as `default`. Clients
    # naturally echo that value on later calls, so it must be an alias for None,
    # never a second named lane at workstreams/default.json.
    if value == "default":
        return None
    return value


def _task_project_id(facts: Mapping[str, "TaskFact"]) -> Optional[str]:
    item = facts.get("project_id")
    if item is None:
        return None
    try:
        return validate_project_id(str(item.value))
    except ProjectRegistryError as exc:
        raise SessionError(f"task state has an invalid project_id: {exc}") from exc


def _validate_task_fact_names(names: Mapping[str, Any] | set[str]) -> None:
    values = list(names)
    if len(values) > MAX_TASK_FACTS:
        raise SessionError(f"task state supports at most {MAX_TASK_FACTS} facts")
    for name in values:
        if not isinstance(name, str) or not name or len(name) > MAX_TASK_FACT_NAME_LENGTH:
            raise SessionError("task fact names must be non-empty strings up to 128 characters")
        if name != name.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in name):
            raise SessionError(f"invalid task fact name: {name!r}")


class FactOrigin(str, Enum):
    VERIFIED = "verified"
    OBSERVED = "observed"
    REPORTED_BY_AGENT = "reported_by_agent"
    INFERRED = "inferred"
    HISTORICAL = "historical"
    STALE = "stale"
    PENDING = "pending"


@dataclass(frozen=True)
class TaskFact:
    value: Any
    origin: FactOrigin
    recorded_at: float = field(default_factory=time.time)
    evidence: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": redact(self.value),
            "origin": self.origin.value,
            "recorded_at": self.recorded_at,
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TaskFact":
        if not isinstance(payload, Mapping):
            raise SessionError("task fact must be an object")
        unknown = set(payload).difference({"value", "origin", "recorded_at", "evidence"})
        if unknown:
            raise SessionError(f"unknown task fact fields: {sorted(unknown)}")
        try:
            origin = FactOrigin(str(payload["origin"]))
        except (KeyError, ValueError) as exc:
            raise SessionError("task fact has an invalid origin") from exc
        recorded_at = payload.get("recorded_at", 0.0)
        if not isinstance(recorded_at, (int, float)) or isinstance(recorded_at, bool):
            raise SessionError("task fact recorded_at must be numeric")
        evidence = payload.get("evidence", [])
        if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
            raise SessionError("task fact evidence must be a string array")
        return cls(
            value=payload.get("value"),
            origin=origin,
            recorded_at=float(recorded_at),
            evidence=tuple(evidence),
        )


def _fact_semantically_equal(left: TaskFact, right: TaskFact) -> bool:
    """Compare durable task meaning while ignoring observation timestamps."""

    if left.origin is not right.origin or left.evidence != right.evidence:
        return False
    try:
        return bool(left.value == right.value)
    except Exception:
        # Task facts are expected to be JSON-like. A custom object with unusual
        # equality semantics must never make a refresh look safely idempotent.
        return False


def _checksum(payload: Mapping[str, Any]) -> str:
    value = dict(payload)
    value.pop("checksum", None)
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TaskState:
    task_id: str
    session_id: str
    revision: int
    created_at: float
    updated_at: float
    facts: dict[str, TaskFact]
    schema_version: int = TASK_STATE_SCHEMA_VERSION
    checksum: str = ""

    def __post_init__(self) -> None:
        if not self.task_id or len(self.task_id) > 128:
            raise SessionError("task state has an invalid task_id")
        if not self.session_id or len(self.session_id) > 100:
            raise SessionError("task state has an invalid session_id")
        _validate_task_fact_names(self.facts)

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "task_id": self.task_id,
            "session_id": self.session_id,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "facts": {name: fact.to_dict() for name, fact in sorted(self.facts.items())},
            "schema_version": self.schema_version,
        }
        payload["checksum"] = _checksum(payload)
        return payload

    def compact(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "session_id": self.session_id,
            "revision": self.revision,
            "updated_at": self.updated_at,
            "facts": {
                name: {
                    "value": fact.value,
                    "origin": fact.origin.value,
                    "recorded_at": fact.recorded_at,
                    "evidence": list(fact.evidence),
                }
                for name, fact in sorted(self.facts.items())
            },
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "TaskState":
        if not isinstance(payload, Mapping):
            raise SessionError("task state must be an object")
        allowed = {
            "task_id",
            "session_id",
            "revision",
            "created_at",
            "updated_at",
            "facts",
            "schema_version",
            "checksum",
        }
        unknown = set(payload).difference(allowed)
        if unknown:
            raise SessionError(f"unknown task state fields: {sorted(unknown)}")
        if int(payload.get("schema_version", 0)) != TASK_STATE_SCHEMA_VERSION:
            raise SessionError("unsupported task state schema")
        checksum = payload.get("checksum")
        if not isinstance(checksum, str) or len(checksum) != 64:
            raise SessionError("task state has no valid checksum")
        if not hmac.compare_digest(checksum, _checksum(payload)):
            raise SessionError("task state checksum mismatch")
        facts_payload = payload.get("facts")
        if not isinstance(facts_payload, Mapping):
            raise SessionError("task state facts must be an object")
        return cls(
            task_id=str(payload["task_id"]),
            session_id=str(payload["session_id"]),
            revision=int(payload["revision"]),
            created_at=float(payload["created_at"]),
            updated_at=float(payload["updated_at"]),
            facts={
                str(name): TaskFact.from_dict(value)
                for name, value in facts_payload.items()
            },
            schema_version=int(payload["schema_version"]),
            checksum=checksum,
        )


class TaskStateStore:
    """Atomic task-state store sharing the SessionStore's cross-process lock."""

    def __init__(self, sessions: SessionStore) -> None:
        self.sessions = sessions

    def path(self, session_id: str, workstream_id: Optional[str] = None) -> Path:
        workstream = _validate_workstream_id(workstream_id)
        session_dir = self.sessions.session_dir(session_id)
        if workstream is None:
            # Backward-compatible default task state for existing chats/profiles.
            return session_dir / "task_state.json"
        return session_dir / "workstreams" / f"{workstream}.json"

    def load(
        self,
        session_id: str,
        *,
        workstream_id: Optional[str] = None,
    ) -> TaskState:
        try:
            payload = json.loads(
                self.path(session_id, workstream_id).read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            suffix = f"/{workstream_id}" if workstream_id else ""
            raise SessionError(f"task state does not exist: {session_id}{suffix}") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionError("task state is unreadable") from exc
        return TaskState.from_dict(payload)

    def load_optional(
        self,
        session_id: str,
        *,
        workstream_id: Optional[str] = None,
    ) -> Optional[TaskState]:
        try:
            return self.load(session_id, workstream_id=workstream_id)
        except SessionError as exc:
            if "does not exist" in str(exc):
                return None
            raise

    def list_workstreams(self, session_id: str) -> tuple[str, ...]:
        """Return named workstream IDs without treating the legacy default as one."""
        root = self.sessions.session_dir(session_id) / "workstreams"
        try:
            candidates = sorted(root.glob("*.json"))
        except OSError:
            return ()
        result: list[str] = []
        for path in candidates:
            candidate = path.stem
            try:
                normalized = _validate_workstream_id(candidate)
                if normalized is None:
                    # `default` is the public alias for the legacy task_state.json.
                    # Ignore a historical workstreams/default.json rather than
                    # exposing two indistinguishable default lanes.
                    continue
                self.load(session_id, workstream_id=normalized)
            except SessionError:
                continue
            result.append(normalized)
        return tuple(result)

    @staticmethod
    def task_id(
        session_id: str,
        objective: str,
        workstream_id: Optional[str] = None,
    ) -> str:
        workstream = _validate_workstream_id(workstream_id) or "default"
        digest = hashlib.sha256(
            f"{session_id}\0{workstream}\0{objective}".encode("utf-8")
        ).hexdigest()
        return f"task-{digest[:24]}"

    def bootstrap(
        self,
        session_id: str,
        facts: Mapping[str, TaskFact],
        *,
        workstream_id: Optional[str] = None,
    ) -> TaskState:
        workstream = _validate_workstream_id(workstream_id)
        requested_project_id = _task_project_id(facts)
        project_fact = facts.get("project_id")
        if project_fact is not None and project_fact.origin is not FactOrigin.VERIFIED:
            raise SessionError("workstream project_id must have verified provenance")
        session = self.sessions.load(session_id)
        self.sessions.validate_repository(session, Path(session.repository))
        with _exclusive_file_lock(self.sessions.lock_path(session_id)):
            existing = self.load_optional(session_id, workstream_id=workstream)
            now = time.time()
            if existing is None:
                objective = facts.get("objective")
                objective_text = str(objective.value) if objective is not None else session.task
                state = TaskState(
                    task_id=self.task_id(session_id, objective_text, workstream),
                    session_id=session_id,
                    revision=0,
                    created_at=now,
                    updated_at=now,
                    facts=dict(facts),
                )
            else:
                existing_project_id = _task_project_id(existing.facts)
                if (
                    existing_project_id is not None
                    and requested_project_id is not None
                    and requested_project_id != existing_project_id
                ):
                    raise SessionError(
                        "workstream project binding is immutable; create a new workstream"
                    )
                merged = dict(existing.facts)
                merged.update(facts)
                # Reconnect/bootstrap refreshes frequently rebuild the same
                # verified facts with new recorded_at timestamps. Advancing the
                # task revision for timestamp-only churn makes another client
                # holding expected_revision fail even though no task meaning or
                # repository state changed. Keep a semantic no-op truly read-like.
                unchanged = len(merged) == len(existing.facts) and all(
                    name in existing.facts
                    and _fact_semantically_equal(existing.facts[name], item)
                    for name, item in merged.items()
                )
                if unchanged:
                    return existing
                state = TaskState(
                    task_id=existing.task_id,
                    session_id=session_id,
                    revision=existing.revision + 1,
                    created_at=existing.created_at,
                    updated_at=now,
                    facts=merged,
                )
            _atomic_json(self.path(session_id, workstream), state.to_dict())
            return self.load(session_id, workstream_id=workstream)

    def checkpoint(
        self,
        session_id: str,
        updates: Mapping[str, TaskFact],
        *,
        expected_revision: Optional[int] = None,
        workstream_id: Optional[str] = None,
    ) -> TaskState:
        workstream = _validate_workstream_id(workstream_id)
        _validate_task_fact_names(set(updates))
        reserved = set(updates).intersection(SYSTEM_VERIFIED_TASK_FACTS)
        if reserved:
            raise SessionError(
                "agent checkpoint cannot overwrite system-verified task facts: "
                + ", ".join(sorted(reserved))
            )
        with _exclusive_file_lock(self.sessions.lock_path(session_id)):
            current = self.load(session_id, workstream_id=workstream)
            if expected_revision is not None and current.revision != expected_revision:
                raise SessionError(
                    f"stale task state revision: expected {expected_revision}, found {current.revision}"
                )
            merged = dict(current.facts)
            merged.update(updates)
            timestamp = TaskFact(
                value=time.time(),
                origin=FactOrigin.VERIFIED,
                evidence=("task.checkpoint",),
            )
            merged["last_checkpoint_timestamp"] = timestamp
            state = TaskState(
                task_id=current.task_id,
                session_id=current.session_id,
                revision=current.revision + 1,
                created_at=current.created_at,
                updated_at=timestamp.recorded_at,
                facts=merged,
            )
            _atomic_json(self.path(session_id, workstream), state.to_dict())
            return self.load(session_id, workstream_id=workstream)


def fact(
    value: Any,
    origin: FactOrigin,
    *evidence: str,
) -> TaskFact:
    return TaskFact(value=value, origin=origin, evidence=tuple(evidence))
