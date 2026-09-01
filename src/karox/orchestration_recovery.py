"""Crash-safe orchestration journal and deterministic recovery decisions."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Optional

from .paths import runtime_dir


_SCHEMA_VERSION = 1
STEP_PENDING = "pending"
STEP_RUNNING = "running"
STEP_PASSED = "passed"
STEP_FAILED = "failed"
STEP_SKIPPED = "skipped"
STEP_RECONCILE = "reconcile_required"
STEP_STATUSES = frozenset(
    {STEP_PENDING, STEP_RUNNING, STEP_PASSED, STEP_FAILED, STEP_SKIPPED, STEP_RECONCILE}
)


class OrchestrationRecoveryError(RuntimeError):
    pass


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    ).hexdigest()


@dataclasses.dataclass(frozen=True)
class StepJournal:
    step_id: str
    endpoint_id: str
    status: str
    attempt: int
    idempotency_key: str
    request_digest: str
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    result_summary: str = ""

    def __post_init__(self) -> None:
        if self.status not in STEP_STATUSES:
            raise ValueError(f"unsupported orchestration step status: {self.status}")
        if self.attempt < 0:
            raise ValueError("step attempt must be non-negative")
        if not self.idempotency_key or not self.request_digest:
            raise ValueError("step journal requires idempotency key and request digest")

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "StepJournal":
        return cls(
            step_id=str(value["step_id"]),
            endpoint_id=str(value["endpoint_id"]),
            status=str(value["status"]),
            attempt=int(value.get("attempt", 0)),
            idempotency_key=str(value["idempotency_key"]),
            request_digest=str(value["request_digest"]),
            started_at=value.get("started_at"),
            finished_at=value.get("finished_at"),
            result_summary=str(value.get("result_summary", "")),
        )


@dataclasses.dataclass(frozen=True)
class RecoverySnapshot:
    run_id: str
    steps: tuple[StepJournal, ...]
    reconcile_required: tuple[str, ...]
    completed: tuple[str, ...]
    failed: tuple[str, ...]

    @property
    def safe_to_resume(self) -> bool:
        return not self.reconcile_required and not self.failed

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "steps": [item.to_dict() for item in self.steps],
            "reconcile_required": list(self.reconcile_required),
            "completed": list(self.completed),
            "failed": list(self.failed),
            "safe_to_resume": self.safe_to_resume,
        }


class OrchestrationJournal:
    def __init__(self, run_id: str, *, path: Optional[Path] = None) -> None:
        if not run_id or len(run_id) > 256:
            raise ValueError("orchestration run id is invalid")
        self.run_id = run_id
        self.path = (
            path
            or (runtime_dir() / "vnext" / "orchestration" / run_id / "journal.json")
        ).expanduser().resolve()

    def _load(self) -> dict[str, StepJournal]:
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OrchestrationRecoveryError(f"cannot read orchestration journal: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("schema_version") != _SCHEMA_VERSION:
            raise OrchestrationRecoveryError("orchestration journal has unsupported schema")
        raw = payload.get("steps")
        if not isinstance(raw, list):
            raise OrchestrationRecoveryError("orchestration journal steps must be an array")
        try:
            items = [StepJournal.from_dict(item) for item in raw if isinstance(item, Mapping)]
        except (KeyError, TypeError, ValueError) as exc:
            raise OrchestrationRecoveryError(f"orchestration journal is invalid: {exc}") from exc
        return {item.step_id: item for item in items}

    def _save(self, steps: Mapping[str, StepJournal]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw = tempfile.mkstemp(prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent)
        temp = Path(raw)
        try:
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    {
                        "schema_version": _SCHEMA_VERSION,
                        "run_id": self.run_id,
                        "steps": [steps[key].to_dict() for key in sorted(steps)],
                    },
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
        finally:
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def initialize(self, step_requests: Mapping[str, tuple[str, Mapping[str, Any]]]) -> None:
        existing = self._load()
        changed = False
        for step_id, (endpoint_id, request_payload) in step_requests.items():
            request_digest = _digest(request_payload)
            current = existing.get(step_id)
            if current is not None:
                if current.endpoint_id != endpoint_id or current.request_digest != request_digest:
                    raise OrchestrationRecoveryError(
                        f"orchestration step identity changed after journaling: {step_id}"
                    )
                continue
            existing[step_id] = StepJournal(
                step_id=step_id,
                endpoint_id=endpoint_id,
                status=STEP_PENDING,
                attempt=0,
                idempotency_key=f"{self.run_id}:{step_id}:1",
                request_digest=request_digest,
            )
            changed = True
        if changed:
            self._save(existing)

    def mark_started(self, step_id: str) -> StepJournal:
        steps = self._load()
        try:
            current = steps[step_id]
        except KeyError as exc:
            raise OrchestrationRecoveryError(f"unknown orchestration step: {step_id}") from exc
        if current.status == STEP_PASSED:
            return current
        if current.status not in {STEP_PENDING, STEP_RECONCILE}:
            raise OrchestrationRecoveryError(
                f"step {step_id} cannot start from state {current.status}"
            )
        attempt = current.attempt + 1
        updated = dataclasses.replace(
            current,
            status=STEP_RUNNING,
            attempt=attempt,
            idempotency_key=f"{self.run_id}:{step_id}:{attempt}",
            started_at=time.time(),
            finished_at=None,
            result_summary="",
        )
        steps[step_id] = updated
        self._save(steps)
        return updated

    def mark_finished(self, step_id: str, *, passed: bool, summary: str = "") -> StepJournal:
        steps = self._load()
        current = steps.get(step_id)
        if current is None:
            raise OrchestrationRecoveryError(f"unknown orchestration step: {step_id}")
        if current.status != STEP_RUNNING:
            raise OrchestrationRecoveryError(
                f"step {step_id} cannot finish from state {current.status}"
            )
        updated = dataclasses.replace(
            current,
            status=STEP_PASSED if passed else STEP_FAILED,
            finished_at=time.time(),
            result_summary=summary[:2000],
        )
        steps[step_id] = updated
        self._save(steps)
        return updated

    def reconcile_after_restart(self) -> RecoverySnapshot:
        steps = self._load()
        changed = False
        for step_id, current in list(steps.items()):
            if current.status == STEP_RUNNING:
                steps[step_id] = dataclasses.replace(current, status=STEP_RECONCILE)
                changed = True
        if changed:
            self._save(steps)
        return self.snapshot()

    def resolve_reconciliation(self, step_id: str, *, completed: bool, passed: bool, summary: str = "") -> StepJournal:
        steps = self._load()
        current = steps.get(step_id)
        if current is None or current.status != STEP_RECONCILE:
            raise OrchestrationRecoveryError(f"step {step_id} is not awaiting reconciliation")
        if completed:
            updated = dataclasses.replace(
                current,
                status=STEP_PASSED if passed else STEP_FAILED,
                finished_at=time.time(),
                result_summary=summary[:2000],
            )
        else:
            # The adapter proved there is no live/completed side effect. A fresh
            # attempt is safe and receives a new idempotency key on mark_started.
            updated = dataclasses.replace(
                current,
                status=STEP_PENDING,
                started_at=None,
                finished_at=None,
                result_summary=summary[:2000],
            )
        steps[step_id] = updated
        self._save(steps)
        return updated

    def snapshot(self) -> RecoverySnapshot:
        steps = self._load()
        values = tuple(steps[key] for key in sorted(steps))
        return RecoverySnapshot(
            run_id=self.run_id,
            steps=values,
            reconcile_required=tuple(item.step_id for item in values if item.status == STEP_RECONCILE),
            completed=tuple(item.step_id for item in values if item.status == STEP_PASSED),
            failed=tuple(item.step_id for item in values if item.status == STEP_FAILED),
        )


__all__ = [
    "OrchestrationJournal",
    "OrchestrationRecoveryError",
    "RecoverySnapshot",
    "STEP_FAILED",
    "STEP_PASSED",
    "STEP_PENDING",
    "STEP_RECONCILE",
    "STEP_RUNNING",
    "STEP_SKIPPED",
    "StepJournal",
]
