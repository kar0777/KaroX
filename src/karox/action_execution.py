"""Capability-first execution layer over the stable Extended Core."""

from __future__ import annotations

from typing import Any, Callable, Optional

from .action_policy import (
    ActionConfirmationRequired,
    ActionDecisionEngine,
    ActionDisposition,
    ActionHardBlocked,
)
from .core_tools import ExtendedCoreRuntime
from .models import CoreCommand
from .risk_engine import ConfirmationRejected
from .risk_mapping import action_for_command


class CapabilityCoreRuntime(ExtendedCoreRuntime):
    """Extended Core with consequence-based action authorization."""

    def __init__(
        self,
        *args: Any,
        action_decisions: ActionDecisionEngine | None = None,
        rollback_checkpoint_id: str | None = None,
        checkpoint_factory: Callable[[], Optional[str]] | None = None,
        **kwargs: Any,
    ) -> None:
        risk = kwargs.get("risk")
        if action_decisions is not None:
            if risk is None:
                kwargs["risk"] = action_decisions.risk
            elif risk is not action_decisions.risk:
                raise ValueError(
                    "action decision engine and risk engine must share one ledger"
                )
        self._action_decisions = action_decisions
        self._rollback_checkpoint_id = rollback_checkpoint_id
        self._checkpoint_factory = checkpoint_factory
        super().__init__(*args, **kwargs)

    def _apply_smart_stop(self, command: CoreCommand) -> None:
        engine = self._action_decisions
        if engine is None:
            super()._apply_smart_stop(command)
            return

        if command.name == "repo.command":
            payload = command.arguments.get("payload")
            if isinstance(payload, dict) and payload.get("dry_run") is True:
                # A dry run mutates nothing: it exists so the agent can inspect
                # the impact of a batch before executing it. Keeping the
                # preview free is what makes consequence-based decisions cheap.
                return

        try:
            record = self.sessions.load(command.session_id)
            # Hosted callers carry the active workstream objective out-of-band
            # on CoreCommand. Native callers intentionally leave it empty and
            # inherit the durable session task. This keeps scoped authority
            # current without putting user intent into a tool payload.
            user_intent = command.user_intent or record.task
            # Only the checkpoint created for *this* build turn counts as a
            # rollback guarantee. A stale checkpoint somewhere in session
            # history must never make today's deletion look reversible.
            reversible_by_checkpoint = bool(self._rollback_checkpoint_id)
        except Exception:
            user_intent = ""
            reversible_by_checkpoint = False
        action = action_for_command(
            command,
            repository=self.repository,
            reversible_by_checkpoint=reversible_by_checkpoint,
        )

        try:
            decision = engine.authorize(
                action,
                user_intent=user_intent,
                confirmation_token=command.confirmation_token,
            )
        except ActionConfirmationRequired as stop:
            recovered = self._auto_guard_with_checkpoint(command, stop, user_intent)
            if recovered is not None:
                # Reversibility adapter: the runtime created a rollback
                # checkpoint on demand, so this stop dissolves locally and the
                # engine's own audit trail already recorded the guarded grant.
                self._publish_risk(
                    recovered.assessment,
                    allowed=True,
                    reason=recovered.disposition.value,
                )
                return
            decision = stop.decision
            self._publish_risk(
                decision.assessment, allowed=False, reason="confirmation_required"
            )
            self._audit(
                "core.command.stopped",
                {
                    "session_id": command.session_id,
                    "origin": command.origin.key,
                    "command": command.name,
                    "correlation_id": command.correlation_id,
                    "risk": decision.assessment.level.value,
                    "disposition": decision.disposition.value,
                    "consequence": decision.consequence.value,
                    "reasons": list(decision.reasons),
                },
            )
            raise
        except ActionHardBlocked as blocked:
            decision = blocked.decision
            self._publish_risk(decision.assessment, allowed=False, reason="hard_block")
            self._audit(
                "core.command.hard_blocked",
                {
                    "session_id": command.session_id,
                    "origin": command.origin.key,
                    "command": command.name,
                    "correlation_id": command.correlation_id,
                    "risk": decision.assessment.level.value,
                    "disposition": decision.disposition.value,
                    "consequence": decision.consequence.value,
                    "reasons": list(decision.reasons),
                },
            )
            raise
        except ConfirmationRejected as rejected:
            decision = engine.decide(action, user_intent=user_intent)
            self._publish_risk(
                decision.assessment, allowed=False, reason=rejected.reason
            )
            self._audit(
                "core.command.confirmation_rejected",
                {
                    "session_id": command.session_id,
                    "origin": command.origin.key,
                    "command": command.name,
                    "correlation_id": command.correlation_id,
                    "reason": rejected.reason,
                    "disposition": decision.disposition.value,
                },
            )
            raise

        self._publish_risk(
            decision.assessment,
            allowed=True,
            reason=decision.disposition.value,
        )
        if decision.disposition is ActionDisposition.GUARDED_AUTO:
            self._audit(
                "core.command.guarded_auto",
                {
                    "session_id": command.session_id,
                    "origin": command.origin.key,
                    "command": command.name,
                    "correlation_id": command.correlation_id,
                    "risk": decision.assessment.level.value,
                    "consequence": decision.consequence.value,
                    "intent_authorized": decision.intent_authorized,
                    "reasons": list(decision.reasons),
                },
            )

    def _auto_guard_with_checkpoint(
        self,
        command: CoreCommand,
        stop: ActionConfirmationRequired,
        user_intent: str,
    ):
        """Reversibility adapter: dissolve a deletion CONFIRM with a checkpoint.

        When the only reason a repository-scoped deletion needs confirmation is
        that no rollback guarantee exists yet, the runtime creates one on demand
        and lets the decision engine re-evaluate honestly. Safety increases
        autonomy instead of prompting. System, external, user-data and
        out-of-repository boundaries are never widened: the engine re-decides
        with the same rules and only a WORKSPACE deletion backed by a real
        checkpoint becomes GUARDED_AUTO.
        """

        engine = self._action_decisions
        factory = self._checkpoint_factory
        if engine is None or factory is None:
            return None
        if self._rollback_checkpoint_id:
            # The current turn already has a checkpoint; the stop had a
            # different cause that a second checkpoint cannot dissolve.
            return None
        decision = stop.decision
        if "delete_not_scoped_by_user" not in decision.reasons:
            return None
        try:
            checkpoint_id = factory()
        except Exception:
            checkpoint_id = None
        if not isinstance(checkpoint_id, str) or not checkpoint_id:
            return None
        self._rollback_checkpoint_id = checkpoint_id
        guarded = action_for_command(
            command,
            repository=self.repository,
            reversible_by_checkpoint=True,
        )
        try:
            recovered = engine.authorize(
                guarded,
                user_intent=user_intent,
                confirmation_token=command.confirmation_token,
            )
        except (ActionConfirmationRequired, ActionHardBlocked):
            return None
        self._audit(
            "core.command.auto_guarded_checkpoint",
            {
                "session_id": command.session_id,
                "origin": command.origin.key,
                "command": command.name,
                "correlation_id": command.correlation_id,
                "rollback_checkpoint_id": checkpoint_id,
                "disposition": recovered.disposition.value,
                "consequence": recovered.consequence.value,
                "reasons": list(recovered.reasons),
            },
        )
        return recovered


__all__ = ["CapabilityCoreRuntime"]
