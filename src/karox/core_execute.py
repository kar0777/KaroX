"""Core command execution, risk publication, replay and session load."""

from __future__ import annotations

import re
import time
from typing import Any, Optional
from .event_bus import EventKind, EventLevel
from . import git_baseline
from .core_types import (
    InvalidCommand,
)
from .models import CoreCommand, CoreResult
from .risk_engine import ConfirmationRejected, SmartStopRequired
from .risk_mapping import action_for_command
from .sessions import MutationLease, SessionError, SessionRecord
from .core_support import (  # noqa: F401
    WRITE_MODE_CREATE as WRITE_MODE_CREATE,
    WRITE_MODE_APPEND as WRITE_MODE_APPEND,
    VerificationRule as VerificationRule,
    _ripgrep_executable as _ripgrep_executable,
)


class CoreExecuteMixin:
    """Methods composed into CoreRuntime; see core.py."""

    def _apply_smart_stop(self, command: CoreCommand) -> None:
        """Run the source-independent risk gate for one command.

        ``CoreRuntime`` is the single place every agent reaches the machine, so
        it is the only correct place to enforce Smart Stop. An API model, a
        sponsor API, ChatGPT Web, an MCP client and a subagent all arrive here,
        and all get the same verdict for the same action.

        No engine means no gate, which keeps every existing embedder working
        exactly as before; a runtime that wants Smart Stop passes one in.
        """

        if self._risk is None:
            return
        action = action_for_command(command)
        try:
            assessment = self._risk.authorize(
                action, confirmation_token=command.confirmation_token
            )
        except SmartStopRequired as stop:
            self._publish_risk(stop.assessment, allowed=False, reason="confirmation_required")
            self._audit(
                "core.command.stopped",
                {
                    "session_id": command.session_id,
                    "origin": command.origin.key,
                    "command": command.name,
                    "correlation_id": command.correlation_id,
                    "risk": stop.assessment.level.value,
                    "reasons": list(stop.assessment.reasons),
                },
            )
            raise
        except ConfirmationRejected as rejected:
            self._publish_risk(
                self._risk.assess(action), allowed=False, reason=rejected.reason
            )
            self._audit(
                "core.command.confirmation_rejected",
                {
                    "session_id": command.session_id,
                    "origin": command.origin.key,
                    "command": command.name,
                    "correlation_id": command.correlation_id,
                    "reason": rejected.reason,
                },
            )
            raise
        self._publish_risk(assessment, allowed=True, reason="allowed")

    def _publish_risk(self, assessment: Any, *, allowed: bool, reason: str) -> None:
        """Announce a risk decision on the event stream, never the token."""

        if self._events is None:
            return
        try:
            self._events.publish(
                EventKind.RISK_DECISION,
                session_id=assessment.session_id,
                summary=f"{assessment.kind}: {assessment.level.value}",
                level=EventLevel.INFO if allowed else EventLevel.WARNING,
                data={
                    "allowed": allowed,
                    "reason": reason,
                    "risk": assessment.level.value,
                    "reasons": list(assessment.reasons),
                    "action_digest": assessment.action_digest,
                    "preview": dict(assessment.preview),
                },
            )
        except Exception:
            # Observability must never be able to block or fail an action.
            return

    def execute(
        self,
        command: CoreCommand,
        capability_token: Optional[str] = None,
        lease: Optional[MutationLease] = None,
    ) -> CoreResult:
        definition = self._definitions.get(command.name)
        handler = self._handlers.get(command.name)
        is_mcp = (
            definition is not None
            and handler is None
            and self._mcp_binding is not None
            and command.name.startswith("mcp.")
        )
        if definition is None or (handler is None and not is_mcp):
            raise InvalidCommand(f"unknown Core command: {command.name}")
        command = self._normalize_command(command)
        self._validate_arguments(definition, command.arguments)
        try:
            input_digest = command.input_digest()
        except (TypeError, ValueError) as exc:
            raise InvalidCommand(
                "Core command arguments must contain strict JSON values"
            ) from exc
        record = self._load_session(command)
        decision = self.policy.require(
            command.origin, definition.capability, capability_token
        )
        for capability in definition.additional_capabilities:
            self.policy.require(command.origin, capability, capability_token)
        self._apply_smart_stop(command)
        # Smart Stop may strengthen a mutation by creating a durable rollback
        # checkpoint. That safety write advances the session revision under the
        # same fenced lease; continue from the fresh record so idempotency
        # reservation cannot race its own checkpoint into StaleSessionRevision.
        # Reloading also revalidates repository/session revocation before the
        # side effect, which is the safer boundary even for a no-op policy pass.
        if definition.mutates:
            record = self._load_session(command)
        started = time.perf_counter()
        self._audit(
            "core.command.started",
            {
                "session_id": command.session_id,
                "origin": command.origin.key,
                "command": command.name,
                "capability": definition.capability.value,
                "correlation_id": command.correlation_id,
                "decision": decision.reason,
                "input_digest": input_digest,
            },
        )
        if definition.mutates:
            if lease is None:
                raise SessionError("mutating Core commands require a mutation lease")
            if not command.idempotency_key:
                raise InvalidCommand("mutating Core commands require an idempotency key")
            self.sessions.validate_lease(lease)
            if lease.session_id != command.session_id:
                raise SessionError("mutation lease belongs to a different session")
            # Reject deterministic input failures before reserving the durable
            # idempotency intent. Once an intent exists, only execution failures
            # that may have caused a side effect require reconciliation.
            self._preflight_mutation(
                command.name,
                command.arguments,
                float(command.deadline_seconds),
            )
            if definition.replayable:
                replay = self.sessions.begin_idempotent(
                    record,
                    lease,
                    command.idempotency_key,
                    input_digest,
                )
                if replay is not None:
                    result = CoreResult.from_dict(replay)
                    self._validate_idempotent_replay(command, result)
                    result.idempotent_replay = True
                    self._audit(
                        "core.command.replayed",
                        {
                            "session_id": command.session_id,
                            "origin": command.origin.key,
                            "command": command.name,
                            "correlation_id": command.correlation_id,
                            "idempotency_key": command.idempotency_key,
                        },
                    )
                    return result
        try:
            if is_mcp:
                data = self._mcp_binding.execute(
                    command.name,
                    dict(command.arguments),
                    record,
                )
            else:
                assert handler is not None
                # The session's durable start revision, from the record this
                # call already loaded: a Git review is scoped to the tree the
                # run began with, and neither the model nor a runtime serving
                # another session can substitute a different base.
                with git_baseline.session_baseline(record):
                    data = handler(
                        dict(command.arguments), float(command.deadline_seconds)
                    )
        except Exception as exc:
            self._audit(
                "core.command.failed",
                {
                    "session_id": command.session_id,
                    "origin": command.origin.key,
                    "command": command.name,
                    "correlation_id": command.correlation_id,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                },
            )
            raise
        evidence = list(data.pop("_evidence", []))
        process_result = command.name in {
            "checks.run",
            "git.status",
            "git.diff",
            "git.commit",
        }
        result = CoreResult(
            ok=not (
                process_result
                and (data.get("timed_out") or data.get("exit_code") != 0)
            ),
            command=command.name,
            data=data,
            correlation_id=command.correlation_id,
            mutation=definition.mutates,
            evidence=evidence,
        )
        if definition.mutates:
            assert lease is not None and command.idempotency_key is not None
            self._record_mutation(record, command, result)
            if definition.replayable:
                self.sessions.complete_idempotent(
                    record,
                    lease,
                    command.idempotency_key,
                    result.to_dict(),
                )
            else:
                # There is no idempotency entry to complete, but the evidence and
                # the check log this call just appended still have to reach disk.
                self.sessions.save(record, record.revision, lease)
        self._audit(
            "core.command.completed",
            {
                "session_id": command.session_id,
                "origin": command.origin.key,
                "command": command.name,
                "correlation_id": command.correlation_id,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                "evidence_ids": [item.evidence_id for item in evidence],
            },
        )
        return result

    def _load_session(self, command: CoreCommand) -> SessionRecord:
        record = self.sessions.load(command.session_id)
        if self._session_repository_validator is None:
            self.sessions.validate_repository(record, self.repository)
        else:
            self._session_repository_validator(record, self.repository)
        if record.revoked:
            raise SessionError("session access has been revoked")
        if record.access_profile != self.policy.profile.value:
            raise SessionError("session and runtime access profiles differ")
        return record

    def _validate_idempotent_replay(
        self, command: CoreCommand, result: CoreResult
    ) -> None:
        """Refuse a stored file result after the repository moved past it.

        An immediate retry after a lost response must replay safely: the target
        file still has the digest produced by the first attempt.  A later edit,
        formatter, checkout, or second client may change the same file, though.
        Returning the old success in that state claims a mutation happened when
        it did not.  File mutations therefore replay only while their recorded
        post-state is still the current post-state.  Other replayable operations
        keep their existing semantics.
        """
        if command.name not in {"repo.write_file", "repo.edit_file"}:
            return
        path_value = result.data.get("path")
        expected_digest = result.data.get("sha256")
        if (
            not isinstance(path_value, str)
            or not isinstance(expected_digest, str)
            or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
        ):
            raise InvalidCommand("stored idempotent file result is invalid")
        path = self.safe_path(path_value)
        current_digest = self._file_sha256(path) if path.is_file() else None
        if current_digest != expected_digest:
            raise InvalidCommand(
                "stored idempotent result no longer matches repository state; "
                "use a new idempotency key"
            )
