"""Persistent Mission Control state for local and mobile KaroX views."""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Optional

from .paths import runtime_dir
from .security import redact_content


_SCHEMA_VERSION = 1
COMMAND_TYPES = frozenset({"steer", "pause", "resume", "stop", "approve_request"})


class MissionControlError(RuntimeError):
    pass


@contextlib.contextmanager
def _exclusive_lock(path: Path):
    """Serialize Mission Control read-modify-write cycles across processes."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)  # type: ignore[attr-defined]
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


@dataclasses.dataclass(frozen=True)
class AgentStatus:
    step_id: str
    role: str
    endpoint_id: str
    status: str
    activity: str = ""
    started_at: Optional[float] = None
    updated_at: float = 0.0
    cost_usd: float = 0.0
    total_tokens: int = 0
    evidence_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AgentStatus":
        return cls(
            step_id=str(value["step_id"]),
            role=str(value["role"]),
            endpoint_id=str(value["endpoint_id"]),
            status=str(value["status"]),
            activity=str(value.get("activity", "")),
            started_at=value.get("started_at"),
            updated_at=float(value.get("updated_at", 0.0)),
            cost_usd=float(value.get("cost_usd", 0.0)),
            total_tokens=int(value.get("total_tokens", 0)),
            evidence_count=int(value.get("evidence_count", 0)),
        )


@dataclasses.dataclass(frozen=True)
class MobileCommand:
    command_id: str
    command_type: str
    target: str
    text: str
    created_at: float
    consumed_at: Optional[float] = None

    def __post_init__(self) -> None:
        if (
            not self.command_id
            or len(self.command_id) > 128
            or any(
                char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for char in self.command_id
            )
        ):
            raise ValueError("Mission Control command id is invalid")
        if self.command_type not in COMMAND_TYPES:
            raise ValueError(f"unsupported Mission Control command: {self.command_type}")
        if len(self.target) > 256 or len(self.text) > 4000:
            raise ValueError("Mission Control command is too large")

    @property
    def pending(self) -> bool:
        return self.consumed_at is None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MobileCommand":
        return cls(
            command_id=str(value["command_id"]),
            command_type=str(value["command_type"]),
            target=str(value.get("target", "orchestrator")),
            text=str(value.get("text", "")),
            created_at=float(value.get("created_at", 0.0)),
            consumed_at=value.get("consumed_at"),
        )


@dataclasses.dataclass(frozen=True)
class MissionSnapshot:
    run_id: str
    task_id: str
    objective: str
    recipe: str
    orchestrator_endpoint_id: str
    status: str
    agents: tuple[AgentStatus, ...]
    actual_cost_usd: float = 0.0
    total_tokens: int = 0
    context_reused_chars: int = 0
    cache_hit_rate: Optional[float] = None
    latest_screenshot_artifact_id: Optional[str] = None
    updated_at: float = 0.0

    @property
    def total_agents(self) -> int:
        return len(self.agents)

    @property
    def completed_agents(self) -> int:
        terminal = {"passed", "failed", "skipped", "blocked"}
        return sum(1 for item in self.agents if item.status in terminal)

    @property
    def running_agents(self) -> int:
        return sum(1 for item in self.agents if item.status == "running")

    @property
    def progress_percent(self) -> float:
        """Stable progress value shared by dict, TUI and mobile presentations."""

        if self.total_agents == 0:
            return 100.0 if self.status == "passed" else 0.0
        return round(self.completed_agents / self.total_agents * 100.0, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task_id": self.task_id,
            "objective": self.objective,
            "recipe": self.recipe,
            "orchestrator_endpoint_id": self.orchestrator_endpoint_id,
            "status": self.status,
            "agents": [item.to_dict() for item in self.agents],
            "completed_agents": self.completed_agents,
            "running_agents": self.running_agents,
            "total_agents": self.total_agents,
            "progress_percent": self.progress_percent,
            "actual_cost_usd": self.actual_cost_usd,
            "total_tokens": self.total_tokens,
            "context_reused_chars": self.context_reused_chars,
            "cache_hit_rate": self.cache_hit_rate,
            "latest_screenshot_artifact_id": self.latest_screenshot_artifact_id,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MissionSnapshot":
        return cls(
            run_id=str(value["run_id"]),
            task_id=str(value["task_id"]),
            objective=str(value.get("objective", "")),
            recipe=str(value.get("recipe", "")),
            orchestrator_endpoint_id=str(value.get("orchestrator_endpoint_id", "")),
            status=str(value.get("status", "unknown")),
            agents=tuple(
                AgentStatus.from_dict(item)
                for item in value.get("agents", [])
                if isinstance(item, Mapping)
            ),
            actual_cost_usd=float(value.get("actual_cost_usd", 0.0)),
            total_tokens=int(value.get("total_tokens", 0)),
            context_reused_chars=int(value.get("context_reused_chars", 0)),
            cache_hit_rate=value.get("cache_hit_rate"),
            latest_screenshot_artifact_id=value.get("latest_screenshot_artifact_id"),
            updated_at=float(value.get("updated_at", 0.0)),
        )


@dataclasses.dataclass(frozen=True)
class MissionOwnerBinding:
    session_id: str
    repo_fingerprint: str
    bound_at: float

    def __post_init__(self) -> None:
        if (
            not self.session_id
            or len(self.session_id) > 100
            or any(
                char
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for char in self.session_id
            )
        ):
            raise ValueError("Mission Control owner session id is invalid")
        if len(self.repo_fingerprint) != 64 or any(
            char not in "0123456789abcdefABCDEF" for char in self.repo_fingerprint
        ):
            raise ValueError("Mission Control repository fingerprint is invalid")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "MissionOwnerBinding":
        return cls(
            session_id=str(value.get("session_id", "")),
            repo_fingerprint=str(value.get("repo_fingerprint", "")),
            bound_at=float(value.get("bound_at", 0.0)),
        )


class MissionControlStore:
    def __init__(self, run_id: str, *, path: Optional[Path] = None) -> None:
        if (
            not run_id
            or len(run_id) > 256
            or any(
                char
                not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
                for char in run_id
            )
        ):
            raise ValueError("Mission Control run id is invalid")
        self.run_id = run_id
        self.path = (
            path
            or (runtime_dir() / "vnext" / "mission-control" / f"{run_id}.json")
        ).expanduser().resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")

    def _read_payload(self) -> dict[str, Any]:
        if not self.path.exists():
            return {
                "schema_version": _SCHEMA_VERSION,
                "owner": None,
                "snapshot": None,
                "commands": [],
            }
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MissionControlError(f"cannot read Mission Control state: {exc}") from exc
        if not isinstance(value, dict) or value.get("schema_version") != _SCHEMA_VERSION:
            raise MissionControlError("Mission Control state has unsupported schema")
        return value

    def _write_payload(self, payload: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temp = Path(raw)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def owner_binding(self) -> Optional[MissionOwnerBinding]:
        with _exclusive_lock(self.lock_path):
            raw = self._read_payload().get("owner")
        return MissionOwnerBinding.from_dict(raw) if isinstance(raw, Mapping) else None

    def bind_owner(
        self,
        *,
        session_id: str,
        repo_fingerprint: str,
    ) -> MissionOwnerBinding:
        candidate = MissionOwnerBinding(
            session_id=session_id,
            repo_fingerprint=repo_fingerprint,
            bound_at=time.time(),
        )
        with _exclusive_lock(self.lock_path):
            payload = self._read_payload()
            raw = payload.get("owner")
            if isinstance(raw, Mapping):
                existing = MissionOwnerBinding.from_dict(raw)
                if (
                    existing.session_id != candidate.session_id
                    or existing.repo_fingerprint.lower()
                    != candidate.repo_fingerprint.lower()
                ):
                    raise MissionControlError(
                        "Mission Control run is already bound to another KaroX session"
                    )
                return existing
            payload["owner"] = candidate.to_dict()
            payload.setdefault("snapshot", None)
            payload.setdefault("commands", [])
            self._write_payload(payload)
        return candidate

    def require_owner(
        self,
        *,
        session_id: str,
        repo_fingerprint: str,
    ) -> MissionOwnerBinding:
        owner = self.owner_binding()
        if owner is None:
            raise MissionControlError(
                "Mission Control run has no hosted session ownership binding"
            )
        if (
            owner.session_id != session_id
            or owner.repo_fingerprint.lower() != repo_fingerprint.lower()
        ):
            raise MissionControlError(
                "Mission Control run belongs to another KaroX session or repository"
            )
        return owner

    def snapshot(self) -> Optional[MissionSnapshot]:
        with _exclusive_lock(self.lock_path):
            raw = self._read_payload().get("snapshot")
        return MissionSnapshot.from_dict(raw) if isinstance(raw, Mapping) else None

    def update_snapshot(self, snapshot: MissionSnapshot) -> MissionSnapshot:
        if snapshot.run_id != self.run_id:
            raise ValueError("Mission snapshot belongs to another run")
        with _exclusive_lock(self.lock_path):
            payload = self._read_payload()
            payload["snapshot"] = snapshot.to_dict()
            payload.setdefault("commands", [])
            self._write_payload(payload)
        return snapshot

    def enqueue(self, command_type: str, *, target: str = "orchestrator", text: str = "", command_id: Optional[str] = None) -> MobileCommand:
        safe_text = redact_content(text)
        if not isinstance(safe_text, str):
            safe_text = str(safe_text)
        command = MobileCommand(
            command_id=command_id or f"cmd-{uuid.uuid4().hex}",
            command_type=command_type,
            target=target,
            text=safe_text,
            created_at=time.time(),
        )
        with _exclusive_lock(self.lock_path):
            payload = self._read_payload()
            raw = payload.get("commands")
            commands = [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
            for item in commands:
                existing = MobileCommand.from_dict(item)
                if existing.command_id != command.command_id:
                    continue
                if (
                    existing.command_type != command.command_type
                    or existing.target != command.target
                    or existing.text != command.text
                ):
                    raise MissionControlError(
                        "Mission Control command id was reused for different input"
                    )
                return existing
            commands.append(command.to_dict())
            payload["commands"] = commands[-500:]
            self._write_payload(payload)
        return command

    def pending_commands(self) -> list[MobileCommand]:
        with _exclusive_lock(self.lock_path):
            raw = self._read_payload().get("commands")
        if not isinstance(raw, list):
            return []
        return [
            MobileCommand.from_dict(item)
            for item in raw
            if isinstance(item, Mapping) and item.get("consumed_at") is None
        ]

    def consume(self, command_id: str) -> MobileCommand:
        with _exclusive_lock(self.lock_path):
            payload = self._read_payload()
            raw = payload.get("commands")
            if not isinstance(raw, list):
                raise MissionControlError("Mission Control command does not exist")
            found: Optional[MobileCommand] = None
            updated: list[dict[str, Any]] = []
            for item in raw:
                if not isinstance(item, Mapping):
                    continue
                command = MobileCommand.from_dict(item)
                if command.command_id == command_id:
                    if not command.pending:
                        raise MissionControlError("Mission Control command was already consumed")
                    command = dataclasses.replace(command, consumed_at=time.time())
                    found = command
                updated.append(command.to_dict())
            if found is None:
                raise MissionControlError("Mission Control command does not exist")
            payload["commands"] = updated
            self._write_payload(payload)
        return found


__all__ = [
    "AgentStatus",
    "COMMAND_TYPES",
    "MissionControlError",
    "MissionControlStore",
    "MissionSnapshot",
    "MobileCommand",
]
