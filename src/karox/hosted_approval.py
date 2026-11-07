"""Hosted approval decisions, chat approval consumption and push compatibility."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping, Optional
from .models import CoreCommand
from .risk_mapping import action_for_command
from .sessions import (
    SessionError,
)
from .task_state import FactOrigin, TaskFact

from .hosted_contract import (
    DEFAULT_HOSTED_DEADLINE_SECONDS,
    HostedApprovalRequired,
    HostedBridgeAccessDenied,
)


_CHAT_APPROVAL_FACT = "chat_user_approval"

_CHAT_APPROVAL_MAX_AGE_SECONDS = 300.0

_CHAT_APPROVAL_EVIDENCE = "user.chat.explicit_approval"


class HostedApprovalMixin:
    """Methods composed into CoreToolBridge; see hosted_core.py."""

    def _approval_decision(self, command: CoreCommand, repository: Path):
        action = action_for_command(
            command,
            repository=repository,
            reversible_by_checkpoint=False,
        )
        return self._action_decisions.decide(
            action,
            user_intent=command.user_intent,
            bypass_mode=self._bypass_mode,
        )

    def _consume_chat_user_approval(
        self,
        *,
        command: CoreCommand,
        repository: Path,
        workstream_id: Optional[str],
        decision: Any,
    ) -> bool:
        """Consume one exact user approval relayed from the current chat.

        Some hosted ChatGPT sessions cannot carry MCP elicitation.  In that
        environment the user may explicitly answer yes/no in the chat instead.
        The agent records that answer as ``chat_user_approval`` in the active
        workstream.  This compatibility path is deliberately narrow: only
        ``git.push`` is accepted, the fact must be fresh and explicitly marked
        as a user-chat report, and the current HEAD/remote/branch must still
        match.  The fact is atomically replaced with a verified consumed record
        before a confirmation token is issued, so one chat approval cannot be
        replayed for a second push.

        This is an opt-in convenience boundary, not a model-independent proof of
        user presence.  Protocol elicitation/browser approval remains stronger
        because the model cannot manufacture those signals by itself.
        """
        if command.name != "git.push":
            return False
        state = self.task_states.load_optional(
            self.session_id,
            workstream_id=workstream_id,
        )
        if state is None:
            return False
        approval = state.facts.get(_CHAT_APPROVAL_FACT)
        if approval is None or approval.origin is not FactOrigin.REPORTED_BY_AGENT:
            return False
        if tuple(approval.evidence) != (_CHAT_APPROVAL_EVIDENCE,):
            return False
        age = time.time() - float(approval.recorded_at)
        if age < -30.0 or age > _CHAT_APPROVAL_MAX_AGE_SECONDS:
            return False
        value = approval.value
        if not isinstance(value, Mapping) or value.get("approved") is not True:
            return False

        action = action_for_command(
            command,
            repository=repository,
            reversible_by_checkpoint=False,
        )
        expected = {
            "action_kind": "git.push",
            "remote": command.arguments.get("remote"),
            "branch": command.arguments.get("branch"),
            "head": action.details.get("head"),
        }
        if any(value.get(key) != expected_value for key, expected_value in expected.items()):
            return False

        consumed = dict(value)
        consumed.update(
            {
                "approved": False,
                "consumed": True,
                "action_digest": decision.action_digest,
            }
        )
        try:
            self.task_states.checkpoint(
                self.session_id,
                {
                    _CHAT_APPROVAL_FACT: TaskFact(
                        consumed,
                        FactOrigin.VERIFIED,
                        evidence=("hosted.chat_approval.consume",),
                    )
                },
                expected_revision=state.revision,
                workstream_id=workstream_id,
            )
        except SessionError:
            return False
        return True

    @staticmethod
    def _approval_error(
        tool_name: str,
        command: CoreCommand,
        decision: Any,
        repository: Optional[Path] = None,
    ) -> HostedApprovalRequired:
        preview = dict(decision.impact)
        if command.name == "git.push":
            head = None
            if repository is not None:
                action = action_for_command(
                    command,
                    repository=repository,
                    reversible_by_checkpoint=False,
                )
                head = action.details.get("head")
            preview.update(
                {
                    "remote": command.arguments.get("remote"),
                    "branch": command.arguments.get("branch"),
                    "head": head,
                }
            )
            message = (
                "Allow one Git push of the current HEAD to "
                f"{command.arguments.get('remote')}/{command.arguments.get('branch')}? "
                "KaroX will not force-push, change remotes, or authenticate."
            )
        else:
            message = (
                f"Allow this one {decision.assessment.kind} action? "
                "The approval is bound to the exact action digest and cannot be replayed."
            )
        return HostedApprovalRequired(
            tool_name=tool_name,
            action_digest=decision.action_digest,
            action_kind=decision.assessment.kind,
            risk=decision.assessment.level.value,
            consequence=decision.consequence.value,
            preview=preview,
            message=message,
        )

    def execute_approved(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        expected_action_digest: str,
        idempotency_key: Optional[str] = None,
        deadline_seconds: float = DEFAULT_HOSTED_DEADLINE_SECONDS,
    ) -> dict[str, Any]:
        """Execute one exact action after a trusted protocol-level user approval."""

        if not isinstance(expected_action_digest, str) or not expected_action_digest:
            raise HostedBridgeAccessDenied("approved action digest is missing")
        return self.execute(
            tool_name,
            arguments,
            idempotency_key=idempotency_key,
            deadline_seconds=deadline_seconds,
            _approved_action_digest=expected_action_digest,
        )

    @staticmethod
    def _cached_catalog_push_compat(arguments: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        """Translate one legacy ``command.run`` push into the guarded push tool.

        Durable ChatGPT connections can cache a pre-upgrade tool catalogue. The
        dedicated ``karox.git.push`` tool may therefore be implemented by the
        freshly restarted child before the client knows its name. Preserve the
        safety boundary by accepting only the unambiguous no-flag form
        ``git push <remote> <branch>`` and routing it to the *same* dedicated
        Core command/approval path. Every other Git mutation still reaches the
        developer-command guard and is refused.
        """

        argv = arguments.get("argv")
        if not isinstance(argv, list) or len(argv) != 4 or not all(
            isinstance(item, str) for item in argv
        ):
            return None
        executable = argv[0].strip().replace("\\", "/").rsplit("/", 1)[-1].casefold()
        for suffix in (".exe", ".cmd", ".bat", ".com"):
            if executable.endswith(suffix):
                executable = executable[: -len(suffix)]
                break
        if executable != "git" or argv[1].strip().casefold() != "push":
            return None
        remote = argv[2].strip()
        branch = argv[3].strip()
        if not remote or not branch:
            return None
        translated: dict[str, Any] = {"remote": remote, "branch": branch}
        workstream = arguments.get("workstream_id")
        if isinstance(workstream, str) and workstream:
            translated["workstream_id"] = workstream
        return translated
