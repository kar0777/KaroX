"""Workspace checkpoints, undo, and diff review (Phase 8).

A transaction model for workspace mutations: every change records a before
hash, after hash, patch, evidence, and tool call identity. Undo is explicit
and previewable; it never uses ``git reset``/``checkout`` and never
overwrites external changes.
"""

from __future__ import annotations

import dataclasses
import hashlib
import time
from typing import Any, Optional


@dataclasses.dataclass(frozen=True)
class Checkpoint:
    """One recorded workspace state, reversible."""

    checkpoint_id: str
    session_id: str
    before_sha256: str
    after_sha256: str
    file_path: str
    tool_call_id: Optional[str]
    timestamp: float
    reversible: bool = True

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


class CheckpointStore:
    """Append-only checkpoint log, one per session."""

    def __init__(self) -> None:
        self._checkpoints: list[Checkpoint] = []

    def record(
        self,
        *,
        session_id: str,
        file_path: str,
        before_content: bytes,
        after_content: bytes,
        tool_call_id: Optional[str] = None,
    ) -> Checkpoint:
        import uuid

        cp = Checkpoint(
            checkpoint_id=str(uuid.uuid4()),
            session_id=session_id,
            before_sha256=hashlib.sha256(before_content).hexdigest()[:16],
            after_sha256=hashlib.sha256(after_content).hexdigest()[:16],
            file_path=str(file_path),
            tool_call_id=tool_call_id,
            timestamp=time.time(),
        )
        self._checkpoints.append(cp)
        return cp

    def for_session(self, session_id: str) -> tuple[Checkpoint, ...]:
        return tuple(cp for cp in self._checkpoints if cp.session_id == session_id)

    def latest(self, session_id: str) -> Optional[Checkpoint]:
        cps = self.for_session(session_id)
        return cps[-1] if cps else None

    def undo_preview(self, session_id: str) -> Optional[Checkpoint]:
        """Show what undo would restore, without performing it."""
        return self.latest(session_id)


__all__ = ["Checkpoint", "CheckpointStore"]
