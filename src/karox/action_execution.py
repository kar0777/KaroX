"""Capability-first execution layer over the stable Extended Core."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
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

_NO_WINDOW: dict[str, Any] = (
    {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
)


class CapabilityCoreRuntime(ExtendedCoreRuntime):
    """Extended Core with consequence-based action authorization."""

    def __init__(
        self,
        *args: Any,
        action_decisions: ActionDecisionEngine | None = None,
        rollback_checkpoint_id: str | None = None,
        checkpoint_factory: Callable[[], Optional[str]] | None = None,
        bypass_mode: bool = False,
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
        self._bypass_mode = bool(bypass_mode)
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

        bypass_mode = self._bypass_mode
        created_files: tuple[str, ...] = ()
        try:
            record = self.sessions.load(command.session_id)
            created_files = tuple(getattr(record, "created_files", ()) or ())
            # Hosted callers carry the active workstream objective out-of-band
            # on CoreCommand. Native callers intentionally leave it empty and
            # inherit the durable session task. This keeps scoped authority
            # current without putting user intent into a tool payload.
            user_intent = command.user_intent or record.task
            # Checkpoints remain useful evidence/rollback support, but they no
            # longer silently convert source deletion into ordinary autonomy.
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
                bypass_mode=bypass_mode,
            )
        except ActionConfirmationRequired as stop:
            if self._own_scratch_delete(action, stop.decision, created_files):
                # The agent is removing files this very session created and
                # that Git has never tracked: undoing its own scratch work.
                # Nothing the user owned is at stake, so an unattended run can
                # leave the tree clean instead of stalling on a prompt nobody
                # will answer.
                self._publish_risk(
                    stop.decision.assessment,
                    allowed=True,
                    reason="session_scratch_delete",
                )
                self._audit(
                    "core.command.guarded_auto",
                    {
                        "session_id": command.session_id,
                        "origin": command.origin.key,
                        "command": command.name,
                        "correlation_id": command.correlation_id,
                        "consequence": stop.decision.consequence.value,
                        "reasons": ["session_scratch_delete"],
                        "paths": list(action.details.get("deletion_paths") or ()),
                    },
                )
                return
            # Confirmation-worthy operations stay pending. Do not mutate state
            # merely to try to dissolve the prompt: the agent can continue
            # independent work and surface the deferred gate only when needed.
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
            decision = engine.decide(
                action,
                user_intent=user_intent,
                bypass_mode=bypass_mode,
            )
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

    def _own_scratch_delete(
        self,
        action: Any,
        decision: Any,
        created_files: tuple[str, ...],
    ) -> bool:
        """Whether a deletion only removes this session's own untracked files.

        Every condition must hold: the stop is purely the workspace-delete
        gate, the action stays inside the repository, each deleted path was
        created by this session (the file did not exist when the session first
        wrote it) and Git does not track it. Anything else keeps the human gate.
        """

        if not created_files or action.outside_repository:
            return False
        # The policy adds exactly one reason of its own for this gate; any other
        # added reason (outside intent scope, external effect, hard boundary)
        # is a different stop and keeps the human in the loop.
        added = set(decision.reasons) - set(decision.assessment.reasons)
        if added != {"destructive_delete_requires_user"}:
            return False
        if decision.consequence.value != "workspace":
            return False
        details = action.details or {}
        if not details.get("deletion_requested") and not action.delete_count:
            return False
        targets = [str(item) for item in (details.get("deletion_paths") or ())]
        if not targets:
            return False
        repository = Path(self.repository).resolve()
        owned = {Path(item).as_posix() for item in created_files}
        normalized: list[str] = []
        for raw in targets:
            candidate = Path(raw)
            try:
                if candidate.is_absolute() or candidate.drive:
                    relative = candidate.resolve().relative_to(repository).as_posix()
                else:
                    resolved = (repository / candidate).resolve()
                    relative = resolved.relative_to(repository).as_posix()
            except (OSError, ValueError):
                return False
            if relative not in owned:
                return False
            if (repository / relative).is_dir():
                return False
            normalized.append(relative)
        try:
            tracked = subprocess.run(
                ["git", "ls-files", "--", *normalized],
                cwd=str(repository),
                capture_output=True,
                text=True,
                timeout=15,
                **_NO_WINDOW,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        if tracked.returncode != 0 or tracked.stdout.strip():
            return False
        return True

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
