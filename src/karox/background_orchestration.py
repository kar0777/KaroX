"""Durable detached orchestration launch records.

Background execution is still the normal ``karox orchestrate run`` process. This
module only starts it detached and records enough secret-free process identity for
Mission Control/TUI to know whether that exact child is still alive after the
launching window exits.

No raw environment, credential, prompt transcript, API key, or full argv is
persisted. Graceful pause/resume/steer/stop travels through Mission Control at safe
orchestration boundaries; this registry never PID-kills a process.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Optional, Sequence

from .detached_process import spawn_detached
from .mission_control import MissionControlStore, MissionSnapshot
from .paths import runtime_dir
from .process_identity import (
    ProcessIdentity,
    capture_process_identity,
    process_is_running,
    verify_process_identity,
)
from .security import redact_content

_SCHEMA_VERSION = 1


class BackgroundOrchestrationError(RuntimeError):
    pass


def _run_id(value: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 100
        or any(
            char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for char in value
        )
    ):
        raise BackgroundOrchestrationError("background orchestration run id is invalid")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temp = Path(raw)
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


@dataclasses.dataclass(frozen=True)
class BackgroundRunRecord:
    run_id: str
    repository: str
    pid: int
    process_identity: ProcessIdentity
    started_at: float
    launch_mechanism: str
    recipe: str = ""
    preset: str = ""
    label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "run_id": self.run_id,
            "repository": self.repository,
            "pid": self.pid,
            "process_identity": self.process_identity.to_dict(),
            "started_at": self.started_at,
            "launch_mechanism": self.launch_mechanism,
            "recipe": self.recipe,
            "preset": self.preset,
            "label": self.label,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "BackgroundRunRecord":
        if value.get("schema_version") != _SCHEMA_VERSION:
            raise BackgroundOrchestrationError("unsupported background run schema")
        identity = ProcessIdentity.from_dict(value.get("process_identity"))
        pid = int(value.get("pid", 0))
        if pid != identity.pid:
            raise BackgroundOrchestrationError("background PID and identity disagree")
        return cls(
            run_id=_run_id(str(value.get("run_id", ""))),
            repository=str(value.get("repository", "")),
            pid=pid,
            process_identity=identity,
            started_at=float(value.get("started_at", 0.0)),
            launch_mechanism=str(value.get("launch_mechanism", "")),
            recipe=str(value.get("recipe", "")),
            preset=str(value.get("preset", "")),
            label=str(value.get("label", "")),
        )


@dataclasses.dataclass(frozen=True)
class BackgroundRunView:
    record: BackgroundRunRecord
    alive: bool
    identity_proven: bool
    identity_reason: str
    snapshot: Optional[MissionSnapshot]

    @property
    def status(self) -> str:
        if not self.alive:
            if self.snapshot is not None and self.snapshot.status in {
                "passed",
                "failed",
                "skipped",
                "blocked",
                "stopped",
            }:
                return self.snapshot.status
            return "exited"
        if not self.identity_proven:
            return "identity_unverified"
        if self.snapshot is not None:
            return self.snapshot.status
        return "starting"

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.record.to_dict(),
            "alive": self.alive,
            "identity_proven": self.identity_proven,
            "identity_reason": self.identity_reason,
            "status": self.status,
            "snapshot": None if self.snapshot is None else self.snapshot.to_dict(),
        }


class BackgroundOrchestrationRegistry:
    def __init__(self, *, root: Optional[Path] = None) -> None:
        self.root = (
            root or (runtime_dir() / "vnext" / "background-orchestration")
        ).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, run_id: str) -> Path:
        return self.root / f"{_run_id(run_id)}.json"

    def put(self, record: BackgroundRunRecord) -> BackgroundRunRecord:
        _atomic_json(self.path(record.run_id), record.to_dict())
        return record

    def get(self, run_id: str) -> BackgroundRunRecord:
        path = self.path(run_id)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BackgroundOrchestrationError(
                f"cannot read background run {run_id}: {exc}"
            ) from exc
        if not isinstance(raw, dict):
            raise BackgroundOrchestrationError("background run record must be an object")
        return BackgroundRunRecord.from_dict(raw)

    def list_records(self) -> tuple[BackgroundRunRecord, ...]:
        rows: list[BackgroundRunRecord] = []
        for path in self.root.glob("*.json"):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    rows.append(BackgroundRunRecord.from_dict(raw))
            except (OSError, json.JSONDecodeError, ValueError, BackgroundOrchestrationError):
                continue
        rows.sort(key=lambda item: item.started_at, reverse=True)
        return tuple(rows)

    def view(self, run_id: str) -> BackgroundRunView:
        record = self.get(run_id)
        verdict = verify_process_identity(
            record.process_identity,
            pid_alive=process_is_running,
            expected_executable=sys.executable,
        )
        try:
            snapshot = MissionControlStore(record.run_id).snapshot()
        except Exception:
            snapshot = None
        return BackgroundRunView(
            record=record,
            alive=verdict.alive,
            identity_proven=verdict.proven,
            identity_reason=verdict.reason,
            snapshot=snapshot,
        )

    def list_views(self, *, limit: int = 50) -> tuple[BackgroundRunView, ...]:
        return tuple(self.view(item.run_id) for item in self.list_records()[: max(0, limit)])

    def request(
        self,
        run_id: str,
        command_type: str,
        *,
        target: str = "orchestrator",
        text: str = "",
        command_id: Optional[str] = None,
    ) -> dict[str, Any]:
        # Existence plus exact process identity are part of the guard: an
        # arbitrary Mission Control file, dead child, or reused PID must never
        # receive hosted control commands.
        view = self.view(run_id)
        if not view.alive:
            raise BackgroundOrchestrationError(
                f"background run is not alive: {run_id}"
            )
        if not view.identity_proven:
            raise BackgroundOrchestrationError(
                f"background process identity is not proven: {run_id}"
            )
        command = MissionControlStore(run_id).enqueue(
            command_type,
            target=target,
            text=text,
            command_id=command_id,
        )
        return command.to_dict()

    def start(
        self,
        *,
        run_id: str,
        repository: Path,
        cli_argv: Sequence[str],
        recipe: str = "",
        preset: str = "",
        label: str = "",
    ) -> BackgroundRunRecord:
        run_id = _run_id(run_id)
        repo = repository.expanduser().resolve(strict=True)
        if not repo.is_dir():
            raise BackgroundOrchestrationError("background repository is not a directory")
        if self.path(run_id).exists():
            existing = self.view(run_id)
            if existing.alive:
                raise BackgroundOrchestrationError(f"background run is already alive: {run_id}")
            raise BackgroundOrchestrationError(
                f"background run id already exists: {run_id}; use a new id"
            )
        # Reserve the run id before spawning. If the launcher dies after spawn but
        # before the durable process identity record is written, the claim stays
        # behind and retries fail closed instead of creating an orphan duplicate.
        claim_path = self.root / f".launch-{run_id}.claim"
        try:
            claim_fd = os.open(
                claim_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError as exc:
            raise BackgroundOrchestrationError(
                f"background run launch is already reserved: {run_id}"
            ) from exc
        else:
            os.close(claim_fd)
        request_path = self.root / (
            f".launch-{run_id}-{os.getpid()}-{time.time_ns()}.json"
        )
        try:
            _atomic_json(
                request_path,
                {
                    "schema_version": _SCHEMA_VERSION,
                    "argv": [str(item) for item in cli_argv],
                },
            )
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                claim_path.unlink()
            raise
        full_argv = [
            sys.executable,
            "-m",
            "karox.background_worker",
            "--request-file",
            str(request_path),
        ]
        process, mechanism = spawn_detached(full_argv, cwd=str(repo))
        if process is None:
            with contextlib.suppress(FileNotFoundError):
                request_path.unlink()
            with contextlib.suppress(FileNotFoundError):
                claim_path.unlink()
            raise BackgroundOrchestrationError(mechanism)
        identity = capture_process_identity(
            int(process.pid), executable=sys.executable, argv=full_argv
        )
        safe_label = redact_content(label)
        if not isinstance(safe_label, str):
            safe_label = str(safe_label)
        record = BackgroundRunRecord(
            run_id=run_id,
            repository=str(repo),
            pid=int(process.pid),
            process_identity=identity,
            started_at=time.time(),
            launch_mechanism=mechanism,
            recipe=str(recipe)[:100],
            preset=str(preset)[:100],
            label=" ".join(safe_label.split())[:240],
        )
        stored = self.put(record)
        with contextlib.suppress(FileNotFoundError):
            claim_path.unlink()
        return stored


__all__ = [
    "BackgroundOrchestrationError",
    "BackgroundOrchestrationRegistry",
    "BackgroundRunRecord",
    "BackgroundRunView",
]
