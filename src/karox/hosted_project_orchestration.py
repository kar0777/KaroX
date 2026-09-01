"""ChatGPT-Project-specific supervision for KaroX orchestration.

The generic orchestration runtime is shared by CLI/TUI users. Hosted ChatGPT
Projects add one contract: Mission Control ``steer`` commands may target a role
or step. This subclass preserves the normal worktree, verification, recovery,
routing and Mission Control behavior while enforcing that delivery boundary.
"""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Mapping

from .context_bus import ContextDelta
from .orchestrator import OrchestrationRuntime, PlannedStep, STATUS_PAUSED, STATUS_RUNNING

_SAFE_TARGET_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_:."
)


class HostedProjectOrchestrationRuntime(OrchestrationRuntime):
    """OrchestrationRuntime with target-aware hosted steering."""

    @staticmethod
    def _safe_target(value: str) -> str:
        target = value.strip() or "all"
        if len(target) > 128 or any(char not in _SAFE_TARGET_CHARS for char in target):
            return "__invalid_target__"
        return target

    def _context_delta_for(self, item: PlannedStep, executor) -> ContextDelta:  # type: ignore[override]
        known_hashes: Mapping[str, str] = {}
        if self._executor_reuses_context(executor):
            known_hashes = self._known_context[item.endpoint.endpoint_id]

        projection = self.context_bus.projection(
            role=item.step.role,
            budget_chars=self.plan.policy.context_budget_chars,
        )
        target_names = {item.step.role, item.step.step_id}
        visible = []
        for context_item in projection.items:
            targets = {
                tag.split(":", 1)[1]
                for tag in context_item.tags
                if tag.startswith("target:") and len(tag) > len("target:")
            }
            if targets and not targets.intersection(target_names):
                continue
            visible.append(context_item)

        changed = []
        unchanged = []
        reused_chars = 0
        current_ids = {context_item.item_id for context_item in visible}
        for context_item in visible:
            if known_hashes.get(context_item.item_id) == context_item.content_hash:
                unchanged.append(context_item.item_id)
                reused_chars += len(context_item.content)
            else:
                changed.append(context_item)
        removed = sorted(set(known_hashes).difference(current_ids))
        return ContextDelta(
            role=item.step.role,
            changed=tuple(changed),
            unchanged_ids=tuple(sorted(unchanged)),
            removed_ids=tuple(removed),
            sent_chars=sum(len(context_item.content) for context_item in changed),
            reused_chars=reused_chars,
        )

    def _consume_mobile_commands(self):  # type: ignore[override]
        """Apply controls at safe boundaries and route steer by role/step."""

        def consume_once() -> None:
            for command in self.mission_control.pending_commands():
                if command.command_type == "pause":
                    self._paused = True
                elif command.command_type == "resume":
                    self._paused = False
                elif command.command_type == "stop":
                    self._stop_requested = True
                    self._paused = False
                elif command.command_type == "steer":
                    target = self._safe_target(command.target)
                    tags = ["mobile", "steer"]
                    if target != "all":
                        tags.append(f"target:{target}")
                    self.context_bus.put_text(
                        item_id=f"mobile-{command.command_id}",
                        kind="task",
                        content=command.text,
                        tags=tuple(tags),
                        priority=100,
                        stable=False,
                        source_ref="mission-control",
                    )
                elif command.command_type == "approve_request":
                    self.context_bus.put_text(
                        item_id=f"mobile-{command.command_id}",
                        kind="decision",
                        content=(
                            "The paired mobile user requested approval review. "
                            "This is not an approval token; use the normal KaroX Smart Stop flow."
                        ),
                        tags=("mobile", "approval-request"),
                        priority=100,
                        stable=False,
                        source_ref="mission-control",
                    )
                self.mission_control.consume(command.command_id)

        consume_once()
        if self._stop_requested:
            return "stopped_by_user"
        if not self._paused:
            return None

        snapshot = self.mission_control.snapshot()
        if snapshot is not None:
            self.mission_control.update_snapshot(
                dataclasses.replace(snapshot, status=STATUS_PAUSED, updated_at=time.time())
            )
        while self._paused and not self._stop_requested:
            time.sleep(0.25)
            consume_once()
        if self._stop_requested:
            return "stopped_by_user"
        snapshot = self.mission_control.snapshot()
        if snapshot is not None:
            self.mission_control.update_snapshot(
                dataclasses.replace(snapshot, status=STATUS_RUNNING, updated_at=time.time())
            )
        return None


__all__ = ["HostedProjectOrchestrationRuntime"]
