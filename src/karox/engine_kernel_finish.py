"""Engine kernel finish mixin: completion predicates, termination and report."""

from __future__ import annotations

import logging
import os

os.environ.setdefault("MSWEA_SILENT_STARTUP", "1")

logger = logging.getLogger("karox.engine_kernel")

import pathlib
from typing import Any, Dict, Optional
from .engine_agent import (  # noqa: E402
    _EngineProviderFailure,
)
from .engine_identity import ENGINE_IDENTITY as _ENGINE_IDENTITY  # noqa: E402
from . import completion_gate
from . import session_analysis
from .agent import (  # noqa: E402
    AgentError,
    AgentEventKind,
    AgentReport,
    _is_small_talk_task,
)
from .security import redact
from .sessions import MutationLease, SessionRecord


class EngineFinishMixin:
    """Methods composed into EngineKernel; see engine_kernel.py."""

    def _finish_checklist(self, record: Any) -> str:
        """The finish checklist for a predictable web deliverable, else "".

        Shown once with the task (it sits in the cached prefix), so the IDs the
        finish gate will ask for are known before the closing pass rather than
        learned from a refusal. Predicted, not owed: the gate itself still
        decides at finish from the files actually changed.
        """

        try:
            if not completion_gate.web_render_intent(record):
                return ""
            from .web_check import default_pages
            from . import requirement_coverage

            if not default_pages(pathlib.Path(self.core.repository)):
                return ""
            block = requirement_coverage.checklist_block(record.requirements)
        except Exception:  # noqa: BLE001 - the checklist is advisory
            return ""
        return f"\n\n{block}" if block else ""

    def _verification(self, record: SessionRecord) -> bool:
        return session_analysis.evidence_chain(record) and not completion_gate.floor(
            self.core, record
        )

    def _answer_complete(self, record: SessionRecord) -> bool:
        if self.require_change or record.changed_files:
            return False
        if session_analysis.attempted_mutation(record, self._mutating_mcp_tools()):
            return False
        # The finish tool's answer is the model's final message; the
        # transcript only carries it in the call arguments, so a terse model
        # that never wrote prose would otherwise leave a browser answer
        # unverifiable even after a successful inspection.
        if not (
            self._engine_submission
            or session_analysis.last_provider_message(record)
        ):
            return False
        if _is_small_talk_task(record.task):
            return True
        return bool(session_analysis.answer_basis(record))

    def _mutating_mcp_tools(self) -> frozenset[str]:
        """The bound MCP tools not classified read-only: they may change a server."""

        tools = getattr(self.core, "tools", None)
        if not callable(tools):
            return frozenset()
        return frozenset(
            definition.name
            for definition in tools()
            if definition.mutates and str(definition.name).startswith("mcp.")
        )

    def _completed(self, record: SessionRecord) -> bool:
        visual_ok = (
            not completion_gate.visual_required(record)
            or completion_gate.visual_proven(self.core, record)
        )
        return (self._verification(record) and visual_ok) or self._answer_complete(record)

    def _terminal_reason(self, session_id: str, exit_status: str) -> str:
        if exit_status == "LimitsExceeded":
            return "step_limit"
        if exit_status == "TimeExceeded":
            return "wall_time_limit"
        if exit_status == "UserInterruption":
            return "visual_budget_exhausted" if self._visual_budget_hit else "cancelled"
        if exit_status == "RepeatedFormatError":
            return "format_errors_exhausted"
        record = self.sessions.load(session_id)
        if self.require_change and not record.changed_files:
            return "no_changes"
        missing = completion_gate.floor(self.core, record)
        if completion_gate.only_unproducible(missing):
            return "unproducible_requirement"
        if self._failure_summary(record):
            return "verification_failed"
        return "unverified_changes" if record.changed_files else "no_changes"

    def _provider_message(self) -> Optional[str]:
        if self._engine_submission:
            return str(redact(self._engine_submission))
        return None

    def _record_provider_failure(
        self, session_id: str, lease: MutationLease, failure: _EngineProviderFailure
    ) -> AgentReport:
        def update(record: SessionRecord) -> None:
            failures = record.usage.get("provider_failures")
            if not isinstance(failures, list):
                failures = []
            failures.append(
                {
                    "at_step": self._step,
                    "kind": failure.kind.value,
                    "error": str(failure)[:400],
                }
            )
            record.usage["provider_failures"] = failures[-100:]

        self._update_session(session_id, lease, update)
        self._emit(
            AgentEventKind.ERROR,
            reason=f"provider_error:{failure.kind.value}",
            detail=str(failure)[:400],
        )
        return self._finish(
            session_id,
            lease,
            "failed",
            "provider",
            f"provider_error:{failure.kind.value}",
        )

    def _finish(
        self,
        session_id: str,
        lease: MutationLease,
        status: str,
        phase: str,
        reason: str,
        provider_message: Optional[str] = None,
    ) -> AgentReport:
        if status == "verified" and completion_gate.floor(
            self.core, self.sessions.load(session_id)
        ):
            status, phase, reason = "stopped", "verification", "completion_unverified"
        if status == "verified":
            current = self.sessions.load(session_id)
            if completion_gate.visual_required(current) and not completion_gate.visual_proven(
                self.core, current
            ):
                status, phase, reason = "stopped", "stopped", "render_unverified"
        restore_note: Optional[str] = None
        if status != "verified":
            current = self.sessions.load(session_id)
            if self._completed(current):
                # It is verified after all: this path is the one that ends as
                # an answer or a stop after the chain already held.
                restore_note = None
            else:
                restore_note = self._restore_verified_state(session_id, current)
        if restore_note is not None:
            self._emit(
                AgentEventKind.WARNING,
                reason="regression_restored",
                detail=restore_note,
            )

        def update(record: SessionRecord) -> None:
            record.status = status
            record.phase = phase
            if status != "verified":
                record.summary = f"Native agent stopped: {reason}."
                if restore_note is not None:
                    record.summary += " " + restore_note + "."
            elif reason == "answer":
                record.summary = "Native agent answered from recorded local evidence."
            else:
                record.summary = "Native agent completed with required local evidence."

        record = self._update_session(session_id, lease, update)
        verified = status == "verified" and self._completed(record)
        if status == "verified" and not verified:
            raise AgentError("session verification changed before final report")
        return self._report(
            record, reason=reason, provider_message=provider_message, restored=restore_note
        )

    def _report(
        self,
        record: SessionRecord,
        *,
        reason: str,
        provider_message: Optional[str] = None,
        restored: Optional[str] = None,
    ) -> AgentReport:
        verified = record.status == "verified" and self._completed(record)
        unmet: tuple[str, ...] = ()
        failure_map: Optional[Dict[str, Any]] = None
        if not verified:
            # A stopped run says why, and where the last approved check failed.
            # Those two facts are the difference between "not verified" and a
            # report a user can act on without opening the session directory.
            unmet = tuple(completion_gate.floor(self.core, record))
            failure_map = self._last_failed_check_map(record)
        self._emit(
            AgentEventKind.FINISHED,
            reason=reason,
            status=record.status,
            ok=verified,
            usage=dict(record.usage) if isinstance(record.usage, dict) else {},
        )
        return AgentReport(
            session_id=record.session_id,
            status=record.status,
            phase=record.phase,
            verified=verified,
            reason=reason,
            steps=session_analysis.step_count(record.provider_history),
            changed_files=tuple(record.changed_files),
            checks=tuple(record.checks),
            git_state=dict(record.git_state),
            evidence=tuple(record.evidence),
            usage=dict(record.usage),
            provider_message=provider_message
            or session_analysis.last_provider_message(record),
            answer_basis=session_analysis.answer_basis(record) if reason == "answer" else (),
            project_context=dict(self.project_context),
            compaction=dict(self._compaction) if self._compaction else None,
            restored=restored,
            unmet=unmet,
            failure_map=failure_map,
            mode=self.mode,
            engine=dict(_ENGINE_IDENTITY),
        )
