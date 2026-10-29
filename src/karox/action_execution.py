"""Capability-first execution layer over the stable Extended Core."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

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
            # Checkpoints remain useful evidence/rollback support, but they
            # never silently convert source deletion into ordinary autonomy.
            reversible_by_checkpoint = False
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
            raise ActionConfirmationRequired(
                stop.decision,
                guidance=(
                    "this action has consequences the session may not confirm "
                    "unattended (for example deleting or changing files it did "
                    "not create). Arguments cannot grant approval: confirmation "
                    "fields are ignored and retrying the same call only wastes "
                    "the step. Leave the target unchanged, or use a "
                    "non-destructive alternative, then finish with the work that "
                    "is already verified."
                ),
            ) from stop
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

__all__ = ["CapabilityCoreRuntime"]
