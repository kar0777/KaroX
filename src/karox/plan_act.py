"""Plan / Act mode separation (Phase 7).

Plan is read-only: analysis, risk preview, verification plan, no mutation.
Act performs mutations, confirmations, checks, evidence. The transition is
explicit, visible, permission-aware, and produces an audit event.
"""

from __future__ import annotations

import dataclasses
import time
from enum import Enum
from typing import Any


class Mode(str, Enum):
    PLAN = "plan"
    ACT = "act"


@dataclasses.dataclass(frozen=True)
class PlanResult:
    """A read-only analysis result from Plan mode."""

    summary: str
    risks: tuple[str, ...]
    verification_plan: tuple[str, ...]
    files_to_change: tuple[str, ...]
    timestamp: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclasses.dataclass(frozen=True)
class ModeTransition:
    """An auditable Plan → Act transition."""

    from_mode: Mode
    to_mode: Mode
    reason: str
    timestamp: float = 0.0
    approved: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_mode": self.from_mode.value,
            "to_mode": self.to_mode.value,
            "reason": self.reason,
            "timestamp": self.timestamp,
            "approved": self.approved,
        }


class PlanActController:
    """Tracks the current mode and enforces the transition contract.

    The interface stays task/chat-first: no dashboard. The mode is a property
    of the controller, not a UI screen. Transitions are logged for audit.
    """

    def __init__(self) -> None:
        self._mode = Mode.PLAN
        self._transitions: list[ModeTransition] = []

    @property
    def mode(self) -> Mode:
        return self._mode

    @property
    def in_plan(self) -> bool:
        return self._mode == Mode.PLAN

    @property
    def in_act(self) -> bool:
        return self._mode == Mode.ACT

    def transition_to_act(self, *, reason: str, approved: bool = False) -> ModeTransition:
        """Explicit, visible Plan → Act transition."""
        if self._mode == Mode.ACT:
            return ModeTransition(Mode.ACT, Mode.ACT, "already in act", time.time(), True)
        t = ModeTransition(Mode.PLAN, Mode.ACT, reason, time.time(), approved)
        self._transitions.append(t)
        self._mode = Mode.ACT
        return t

    def transition_to_plan(self, *, reason: str = "returning to plan") -> ModeTransition:
        t = ModeTransition(Mode.ACT, Mode.PLAN, reason, time.time(), True)
        self._transitions.append(t)
        self._mode = Mode.PLAN
        return t

    def audit_trail(self) -> tuple[ModeTransition, ...]:
        return tuple(self._transitions)


__all__ = ["Mode", "ModeTransition", "PlanActController", "PlanResult"]
