"""Engine kernel control mixin: cancellation, deadlines, visual grace and events."""

from __future__ import annotations

import logging

logger = logging.getLogger("karox.engine_kernel")

from typing import Any
#: Steps a refused finish keeps the visual grace window open: enough for a
#: probe page, a few browser reads and the attested finish the refusal asked
#: for. At 4, a measured 3D run (30/30 on the grader) re-probed for five steps
#: after its second refusal and was stopped one step before its finish.
_COVERAGE_GRACE_STEPS = 8

from .agent import (  # noqa: E402
    AgentEvent,
    AgentEventKind,
)


class EngineControlMixin:
    """Methods composed into EngineKernel; see engine_kernel.py."""

    def cancel(self) -> None:
        """Request cancellation; the loop stops at the next step boundary."""
        self._cancelled = True
        if self._cancel_event is not None:
            self._cancel_event.set()

    def note_visual_budget_exhausted(self) -> None:
        """End the run honestly once no further visual verdict can arrive.

        The checker's budget is per task. After it, every ``visual_check`` call
        returns ``method: limit`` and the completion gate can never see a
        passing vision verdict, so more edits cannot make the run verified.
        The aquarium run spent 26 steps editing after the first limit verdict
        and another 20 in a later run, then ended on the step limit with a
        broken resize. The report now says why it stopped.
        """

        self._visual_budget_hit = True
        self.cancel()

    def note_visual_limit(self, *, proven: bool) -> None:
        """React to a ``method: limit`` visual verdict.

        Without a pass on the current bytes no later verdict can make the run
        verified, so it ends (``note_visual_budget_exhausted``). With one, the
        limit changes nothing: the run can still finish verified without an
        edit, so it gets the same short grace as after the last real verdict
        (never a longer one). A measured run asked for one more look after its
        last pass and was stopped with a correct, still-proven game.
        """

        if not proven:
            self.note_visual_budget_exhausted()
            return
        if self._visual_grace_until is None:
            self._visual_grace_until = self._step + 1

    def note_visual_last_check(self, *, attest: bool = False) -> None:
        """Allow two more model calls after the last real visual verdict.

        The closing look can judge an edit made after the budget, but the model
        could also keep editing to the step limit (80 steps and 4.1M tokens in
        the karo-13 run). The grace window is the edit turn and the closing
        check; the next call stops the run honestly.

        ``attest``: the verdict is a pass that still covers the bytes of a
        deliverable whose finish owes a requirement attestation. Such a run is
        provable without another edit, but its finish needs fresh probes and
        the attestation, so it gets the window a refused finish gets. A
        measured 3D run (30/30 on the grader) got its last pass with no checks
        left, spent the two calls on the change review the pass asked for and
        was stopped before its first finish. The window is still bounded, and
        an edit inside it leaves the run unprovable: no further verdict can
        arrive except the closing look.
        """

        self._visual_grace_until = self._step + (
            _COVERAGE_GRACE_STEPS if attest else 1
        )

    def note_coverage_refusal(self) -> None:
        """Keep the visual grace window open for the attestation finish owes.

        A web deliverable's finish is refused until every requirement is
        attested against fresh results. If that refusal lands inside the grace
        window after the last visual verdict, the stop would come before the
        model could probe and attest, ending a correct run unverified.
        """

        if self._visual_grace_until is not None:
            self._visual_grace_until = max(
                self._visual_grace_until, self._step + _COVERAGE_GRACE_STEPS
            )

    def remaining_seconds(self) -> float:
        return max(0.1, self._deadline - self.monotonic())

    def closing_turn_active(self) -> bool:
        """Whether the run is in its one closing call for the final answer."""

        return self._closing_turn_until is not None

    def _cancel_requested(self) -> bool:
        if self._cancelled:
            return True
        closing = getattr(self, "_closing_turn_until", None)
        if closing is not None:
            return self._step >= closing or (
                self._cancel_event is not None and self._cancel_event.is_set()
            )
        if (
            self._visual_grace_until is not None
            and self._step > self._visual_grace_until
        ):
            self._visual_budget_hit = True
            return True
        return self._cancel_event is not None and self._cancel_event.is_set()

    def _model_event(self, kind: str, **fields: Any) -> None:
        try:
            event_kind = AgentEventKind(kind)
        except ValueError:
            return
        self._emit(event_kind, **fields)

    def _on_step_started(self, step: int) -> None:
        self._step = step
        self._last_step_usage = {}
        self._heartbeat()
        self._emit(AgentEventKind.STEP_STARTED)

    def _on_step_finished(self, step: int) -> None:
        self._step = step
        # The TUI accumulates per-step deltas and replaces the counter with the
        # final cumulative report. Emitting the cumulative record here would
        # double-count after the first step.
        usage = dict(self._last_step_usage)
        self._last_step_usage = {}
        self._emit(AgentEventKind.STEP_FINISHED, usage=usage)

    def _on_compacted(
        self, tokens_before: int, tokens_after: int, count: int, overflow: int = 0
    ) -> None:
        self._compaction = {
            "engine": True,
            "tokens_before": int(tokens_before),
            "tokens_after": int(tokens_after),
            "count": int(count),
            "over_budget_tokens": max(0, int(overflow)),
        }
        summary = f"engine history compacted to ~{tokens_after} tokens (event {count})"
        if overflow > 0:
            summary += f"; still ~{overflow} tokens over budget"
        self._emit(AgentEventKind.COMPACTED, summary=summary)

    def _emit(self, kind: AgentEventKind, **fields: Any) -> None:
        observer = self._on_event
        if observer is None:
            return
        try:
            observer(AgentEvent(kind, self._step, **fields))
        except Exception:
            # A display observer must never end the run, and one malformed
            # event must not permanently mute the stream: drop this event,
            # keep the observer for the next one.
            logger.debug("dropping one unrenderable kernel event", exc_info=True)
