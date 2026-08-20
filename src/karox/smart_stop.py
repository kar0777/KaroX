"""Smart Stop — no-progress detection and safe halt (Phase 6).

A state machine that watches for signals indicating an agent run should stop:
repeated identical tool calls, no-progress loops, unchanged workspace,
verification failure loops, budget exceeded, provider/browser stalls. The
machine never kills a useful long build or a test run with progress.
"""

from __future__ import annotations

import dataclasses
import time
from enum import Enum
from typing import Any, Optional


class StopSignal(str, Enum):
    USER_CANCEL = "user_cancel"
    REPEATED_TOOL_CALLS = "repeated_tool_calls"
    NO_PROGRESS_LOOP = "no_progress_loop"
    UNCHANGED_WORKSPACE = "unchanged_workspace"
    VERIFICATION_FAILURE_LOOP = "verification_failure_loop"
    BUDGET_EXCEEDED = "budget_exceeded"
    PROVIDER_STALL = "provider_stall"
    BROWSER_STALL = "browser_stall"
    CONFIRMATION_WAITING = "confirmation_waiting"
    DEPENDENCY_WAITING = "dependency_waiting"
    COMPLETION = "completion"
    ACCEPTED_VERIFICATION = "accepted_verification"


class StopAction(str, Enum):
    CONTINUE = "continue"
    WARN = "warn"
    PAUSE = "pause"
    REQUEST_CONFIRMATION = "request_confirmation"
    STOP = "stop"
    CHECKPOINT = "checkpoint"


@dataclasses.dataclass(frozen=True)
class StopDecision:
    signal: StopSignal
    action: StopAction
    reason: str
    evidence: dict[str, Any]

    @property
    def should_stop(self) -> bool:
        return self.action in (StopAction.STOP, StopAction.PAUSE)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal": self.signal.value,
            "action": self.action.value,
            "reason": self.reason,
            "evidence": self.evidence,
        }


class SmartStopMachine:
    """Evaluates no-progress signals and decides whether to continue or halt."""

    def __init__(
        self,
        *,
        max_identical_calls: int = 3,
        max_verification_failures: int = 3,
        budget_usd: Optional[float] = None,
    ) -> None:
        self._max_identical = max_identical_calls
        self._max_verify_failures = max_verification_failures
        self._budget = budget_usd
        self._tool_call_history: list[str] = []
        self._verification_failures = 0
        self._last_workspace_hash: Optional[str] = None
        self._unchanged_count = 0
        self._last_progress_time = time.time()

    def observe_tool_call(self, tool_name: str, args_hash: str = "") -> None:
        key = f"{tool_name}:{args_hash}"
        self._tool_call_history.append(key)

    def observe_verification(self, accepted: bool) -> None:
        if accepted:
            self._verification_failures = 0
        else:
            self._verification_failures += 1

    def observe_workspace(self, workspace_hash: str) -> None:
        if workspace_hash == self._last_workspace_hash:
            self._unchanged_count += 1
        else:
            self._unchanged_count = 0
            self._last_workspace_hash = workspace_hash
            self._last_progress_time = time.time()

    def evaluate(
        self,
        *,
        user_cancelled: bool = False,
        cost_usd: float = 0.0,
        time_since_progress_seconds: float = 0.0,
        is_in_confirmation: bool = False,
        is_in_dependency_wait: bool = False,
        task_complete: bool = False,
        verification_accepted: bool = False,
    ) -> StopDecision:
        if user_cancelled:
            return StopDecision(StopSignal.USER_CANCEL, StopAction.STOP, "user cancelled", {})

        if is_in_confirmation:
            return StopDecision(StopSignal.CONFIRMATION_WAITING, StopAction.CONTINUE,
                                "waiting for user confirmation", {})

        if is_in_dependency_wait:
            return StopDecision(StopSignal.DEPENDENCY_WAITING, StopAction.CONTINUE,
                                "waiting for dependency", {})

        if task_complete:
            return StopDecision(StopSignal.COMPLETION, StopAction.STOP,
                                "task completed", {})

        if verification_accepted:
            return StopDecision(StopSignal.ACCEPTED_VERIFICATION, StopAction.STOP,
                                "verification accepted", {})

        # Repeated identical tool calls
        if len(self._tool_call_history) >= self._max_identical:
            recent = self._tool_call_history[-self._max_identical:]
            if len(set(recent)) == 1:
                return StopDecision(
                    StopSignal.REPEATED_TOOL_CALLS, StopAction.WARN,
                    f"same tool call repeated {self._max_identical} times",
                    {"calls": recent},
                )

        # Verification failure loop
        if self._verification_failures >= self._max_verify_failures:
            return StopDecision(
                StopSignal.VERIFICATION_FAILURE_LOOP, StopAction.PAUSE,
                f"{self._verification_failures} consecutive verification failures",
                {},
            )

        # Budget exceeded
        if self._budget is not None and cost_usd >= self._budget:
            return StopDecision(
                StopSignal.BUDGET_EXCEEDED, StopAction.STOP,
                f"budget ${self._budget:.2f} exceeded (spent ${cost_usd:.2f})",
                {},
            )

        # Unchanged workspace after many steps
        if self._unchanged_count >= 5:
            return StopDecision(
                StopSignal.UNCHANGED_WORKSPACE, StopAction.WARN,
                f"workspace unchanged for {self._unchanged_count} steps",
                {},
            )

        return StopDecision(
            StopSignal.ACCEPTED_VERIFICATION, StopAction.CONTINUE,
            "no stop signal", {},
        )


__all__ = [
    "SmartStopMachine",
    "StopAction",
    "StopDecision",
    "StopSignal",
]
